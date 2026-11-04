#!/usr/bin/env python3
"""git-credential-fileapi: sign in to a fileapi server in the browser when git needs credentials.

Install it once and point git at it for the fileapi host:

    curl -fsSL https://<host>/git-credential-fileapi -o ~/.local/bin/git-credential-fileapi
    chmod +x ~/.local/bin/git-credential-fileapi
    git-credential-fileapi setup https://<host>

After that a plain `git clone https://<host>/git/<project>/<repo>.git` works: when the server answers
401, git asks this helper, which opens fileapi's sign-in page (atproto / Bluesky) in the browser, catches
the result on a one-off listener on 127.0.0.1, and hands git the access token as the password.

It is an ordinary OAuth client of fileapi (dynamic client registration, PKCE, loopback redirect). The
access token (24 h) and refresh token (90 days, rotated on use) are kept in
~/.config/git-credential-fileapi/tokens.json (mode 600), so later git commands refresh silently and the
browser only opens again when the refresh token is gone or rejected.

Commands: get / store / erase (the git credential protocol), setup [url], login [url], logout [url],
status. Standard library only, so the file runs anywhere python3 does.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

DEFAULT_URL = "https://fileapi.fastapicloud.dev"
NAME = "git-credential-fileapi"
LOGIN_TIMEOUT = 300
EARLY_REFRESH = 120  # refresh this many seconds before the access token expires


def _log(msg: str) -> None:
    print(f"{NAME}: {msg}", file=sys.stderr, flush=True)


def _state_path() -> str:
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, NAME, "tokens.json")


def _load() -> dict:
    try:
        with open(_state_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _state_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def _base(url: str) -> str:
    p = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    return f"{p.scheme}://{p.netloc}"


def _post(url: str, data: dict, as_json: bool = False) -> dict:
    if as_json:
        body, ctype = json.dumps(data).encode(), "application/json"
    else:
        body, ctype = urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": ctype, "Accept": "application/json", "User-Agent": NAME})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e)
        except ValueError:
            detail = {}
        raise RuntimeError(f"{url}: {detail.get('error', e.code)} {detail.get('error_description', '')}".strip()) from None


def _metadata(base: str) -> dict:
    req = urllib.request.Request(f"{base}/.well-known/oauth-authorization-server",
                                 headers={"Accept": "application/json", "User-Agent": NAME})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _remember(base: str, entry: dict, tokens: dict) -> dict:
    entry = dict(entry, access_token=tokens["access_token"],
                 expires_at=int(time.time()) + int(tokens.get("expires_in", 3600)))
    if tokens.get("refresh_token"):
        entry["refresh_token"] = tokens["refresh_token"]
    data = _load()
    data[base] = entry
    _save(data)
    return entry


def _refresh(base: str, entry: dict) -> dict | None:
    if not entry.get("refresh_token") or not entry.get("client_id"):
        return None
    try:
        meta = _metadata(base)
        tokens = _post(meta["token_endpoint"], {"grant_type": "refresh_token", "client_id": entry["client_id"],
                                                "refresh_token": entry["refresh_token"]})
    except (RuntimeError, OSError, KeyError, ValueError) as e:
        _log(f"refresh failed ({e}); signing in again")
        return None
    return _remember(base, entry, tokens)


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict | None = None
    done = threading.Event()

    def do_GET(self):  # noqa: N802
        url = urllib.parse.urlparse(self.path)
        if url.path != "/callback":
            self.send_error(404)
            return
        params = dict(urllib.parse.parse_qsl(url.query))
        ok = "code" in params
        msg = ("Signed in. You can close this tab and go back to git." if ok
               else f"Sign-in failed: {params.get('error_description') or params.get('error') or 'no code'}")
        body = (f"<!doctype html><meta charset=utf-8><title>fileapi</title>"
                f"<body style='font:16px system-ui;margin:10vh auto;max-width:30rem'><h1>fileapi</h1><p>{msg}</p>").encode()
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        type(self).result = params
        type(self).done.set()

    def log_message(self, *args):
        pass


def _open_browser(url: str) -> None:
    if os.environ.get("FILEAPI_NO_BROWSER"):
        return
    # git reads this helper's stdout until EOF, and a browser started from here would inherit it (and
    # might keep running for hours), so point stdin/stdout at /dev/null while launching it.
    saved = [os.dup(0), os.dup(1)]
    devnull = os.open(os.devnull, os.O_RDWR)
    try:
        sys.stdout.flush()
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        webbrowser.open(url)
    except Exception:
        pass
    finally:
        os.dup2(saved[0], 0)
        os.dup2(saved[1], 1)
        for fd in (*saved, devnull):
            os.close(fd)


def login(base: str) -> dict:
    """Browser sign-in with a loopback redirect. Returns the stored entry."""
    meta = _metadata(base)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Catcher)
    _Catcher.result, _Catcher.done = None, threading.Event()
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = _post(meta["registration_endpoint"], {
            "client_name": f"git on {os.uname().nodename if hasattr(os, 'uname') else 'this computer'}",
            "redirect_uris": [redirect], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        }, as_json=True)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
            "response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256", "scope": "fileapi",
        })
        _log(f"git needs to sign in to {base}. Opening your browser; if it doesn't open, visit:\n  {url}")
        _open_browser(url)
        if not _Catcher.done.wait(LOGIN_TIMEOUT):
            raise RuntimeError(f"no sign-in within {LOGIN_TIMEOUT // 60} minutes")
        params = _Catcher.result or {}
    finally:
        server.shutdown()
        server.server_close()
    if params.get("state") != state:
        raise RuntimeError("sign-in answer had the wrong state; try again")
    if "code" not in params:
        raise RuntimeError(params.get("error_description") or params.get("error") or "sign-in was cancelled")
    tokens = _post(meta["token_endpoint"], {"grant_type": "authorization_code", "code": params["code"],
                                            "redirect_uri": redirect, "client_id": client["client_id"],
                                            "code_verifier": verifier})
    entry = _remember(base, {"client_id": client["client_id"]}, tokens)
    _log("signed in.")
    return entry


def token_for(base: str, interactive: bool = True) -> dict | None:
    entry = _load().get(base) or {}
    if entry.get("access_token") and entry.get("expires_at", 0) > time.time() + EARLY_REFRESH:
        return entry
    entry = _refresh(base, entry) if entry else None
    if entry or not interactive:
        return entry
    return login(base)


def _read_attrs() -> dict:
    attrs = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        attrs.setdefault(key, value)
    return attrs


def _git_get(attrs: dict) -> int:
    if attrs.get("protocol") not in ("https", "http") or not attrs.get("host"):
        return 0
    base = f"{attrs['protocol']}://{attrs['host']}"
    try:
        entry = token_for(base)
    except Exception as e:  # never crash git; it falls back to its own prompt
        _log(f"could not sign in: {e}")
        return 0
    if entry:
        print(f"username={attrs.get('username') or 'fileapi'}")
        print(f"password={entry['access_token']}")
        print(f"password_expiry_utc={entry['expires_at']}")
    return 0


def _git_erase(attrs: dict) -> int:
    # git calls erase after the server rejected the password: drop that access token, keep the refresh
    # token so the next attempt can refresh silently before falling back to the browser.
    base = f"{attrs.get('protocol')}://{attrs.get('host')}"
    data = _load()
    entry = data.get(base)
    if entry and attrs.get("password") and attrs["password"] == entry.get("access_token"):
        entry.pop("access_token", None)
        entry["expires_at"] = 0
        _save(data)
    return 0


def setup(base: str) -> int:
    key = f"credential.{base}.helper"
    # An empty helper first resets any global helpers (store, cache, libsecret) for this host, so a stale
    # copy of an expired token can't shadow this helper.
    subprocess.run(["git", "config", "--global", "--unset-all", key], check=False)
    subprocess.run(["git", "config", "--global", "--add", key, ""], check=True)
    subprocess.run(["git", "config", "--global", "--add", key, "fileapi"], check=True)
    subprocess.run(["git", "config", "--global", f"credential.{base}.username", "fileapi"], check=True)
    _log(f"git now asks {NAME} for {base}. Try: git clone {base}/git/<project>/<repo>.git")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    base = _base(argv[1] if len(argv) > 1 else os.environ.get("FILEAPI_URL", DEFAULT_URL))
    if cmd == "get":
        return _git_get(_read_attrs())
    if cmd == "erase":
        return _git_erase(_read_attrs())
    if cmd == "store":
        _read_attrs()  # tokens are already saved by get
        return 0
    if cmd == "setup":
        return setup(base)
    if cmd == "login":
        try:
            login(base)
        except Exception as e:
            _log(f"could not sign in: {e}")
            return 1
        return 0
    if cmd == "logout":
        data = _load()
        if data.pop(base, None) is not None:
            _save(data)
        _log(f"forgot the tokens for {base}.")
        return 0
    if cmd == "status":
        for b, e in _load().items():
            left = int(e.get("expires_at", 0) - time.time())
            print(f"{b}: access token {'valid for %dm' % (left // 60) if left > 0 else 'expired'}, "
                  f"refresh token {'present' if e.get('refresh_token') else 'missing'}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
