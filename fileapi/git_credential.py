#!/usr/bin/env python3
"""git-credential-fileapi: sign in to a fileapi server in the browser when git needs credentials.

Install it once and point git at it for the fileapi host:

    curl -fsSL https://<host>/git-credential-fileapi -o ~/.local/bin/git-credential-fileapi
    chmod +x ~/.local/bin/git-credential-fileapi
    git-credential-fileapi setup https://<host>

After that a plain `git clone https://<host>/git/<handle>/<repo>.git` works: when the server answers
401, git asks this helper, which opens fileapi's sign-in page (atproto / Bluesky) in the browser, catches
the result on a one-off listener on 127.0.0.1, and hands git the access token as the password.

It is an ordinary OAuth client of fileapi (dynamic client registration, PKCE, loopback redirect). The
access token (24 h) and refresh token (90 days, rotated on use) are kept in
~/.config/git-credential-fileapi/tokens.json (mode 600), so later git commands refresh silently and the
browser only opens again when the refresh token is gone or rejected.

Large files go to Git LFS automatically. `setup` also turns on git-lfs (if it is installed) and adds a
pre-commit hook to git's template dir, so every repo cloned or created from then on gets it. On commit,
in a repo with a fileapi remote, the hook finds staged files bigger than the threshold (default 10 MiB),
adds them to .gitattributes with `git lfs track --filename` and re-stages them as LFS pointers; git-lfs
then uploads the content to fileapi's LFS endpoint on push, signing in through this same helper.
Settings (git config, any scope): fileapi.lfsThreshold (bytes, k/m/g suffixes ok, e.g. 50m; 0 turns it
off) and fileapi.autoLfs (auto = repos with a fileapi remote, true = every repo, false = never).
For a repo cloned before setup, run `git-credential-fileapi lfs-install` inside it.

Commands: get / store / erase (the git credential protocol), setup [url], login [url], logout [url],
status, lfs-install, pre-commit (the hook). Standard library only, so the file runs anywhere python3 does.
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
DEFAULT_LFS_THRESHOLD = 10 * 1024 * 1024
HOOK_MARK = "# git-credential-fileapi: track big files with Git LFS"


def _log(msg: str) -> None:
    print(f"{NAME}: {msg}", file=sys.stderr, flush=True)


def _config_dir() -> str:
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(root, NAME)


def _state_path() -> str:
    return os.path.join(_config_dir(), "tokens.json")


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


# ---- Git LFS for big files ---------------------------------------------------------------------------


def _git(*args: str, check: bool = False, input: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check, input=input)


def _git_z(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], capture_output=True, check=True).stdout
    return [p.decode("utf-8", "surrogateescape") for p in out.split(b"\0") if p]


def _have_lfs() -> bool:
    try:
        return _git("lfs", "version").returncode == 0
    except OSError:
        return False


def _parse_size(text: str) -> int | None:
    text = text.strip().lower().removesuffix("b").removesuffix("i")
    mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(text[-1:], 1)
    try:
        return int(float(text[:-1] if mult > 1 else text) * mult)
    except ValueError:
        return None


def lfs_threshold() -> int:
    """Files bigger than this many bytes go to LFS. 0 (or less) turns it off."""
    raw = _git("config", "--get", "fileapi.lfsThreshold").stdout.strip() or os.environ.get("FILEAPI_LFS_THRESHOLD", "")
    value = _parse_size(raw) if raw else None
    return DEFAULT_LFS_THRESHOLD if value is None else value


def _fileapi_remote() -> str | None:
    """The first remote whose host uses this helper, if any."""
    for line in _git("config", "--get-regexp", r"^remote\..*\.url$").stdout.splitlines():
        url = line.partition(" ")[2].strip()
        if "://" not in url:
            continue
        helpers = _git("config", "--get-urlmatch", "credential.helper", url).stdout.strip()
        if helpers.endswith("fileapi") or NAME in helpers:
            return url
    return None


def _auto_lfs_enabled() -> bool:
    mode = (_git("config", "--get", "fileapi.autoLfs").stdout.strip() or "auto").lower()
    if mode in ("false", "no", "off", "0", "never"):
        return False
    if mode in ("true", "yes", "on", "1", "always"):
        return True
    return _fileapi_remote() is not None


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def big_staged_files(threshold: int) -> list[tuple[str, int]]:
    """Staged (added or changed) regular files over `threshold` bytes that aren't LFS pointers already."""
    fields = _git_z("diff", "--cached", "--raw", "-z", "--no-abbrev", "--no-renames", "--diff-filter=AM")
    staged = []  # (path, blob sha)
    for meta, path in zip(fields[0::2], fields[1::2]):
        _, new_mode, _, sha, _ = meta.lstrip(":").split(" ")
        if new_mode in ("100644", "100755"):
            staged.append((path, sha))
    if not staged:
        return []
    out = _git("cat-file", "--batch-check", input="".join(f"{sha}\n" for _, sha in staged), check=True).stdout
    sizes = [int(line.split()[2]) for line in out.splitlines()]
    big = [(path, size) for (path, _), size in zip(staged, sizes) if size > threshold]
    if not big:
        return []
    # Already tracked: the staged blob would be a small pointer, but skip them anyway (e.g. lfs not set up).
    attrs = _git_z("check-attr", "-z", "filter", "--", *[p for p, _ in big])
    tracked = {path for path, _, value in zip(attrs[0::3], attrs[1::3], attrs[2::3]) if value == "lfs"}
    return [(p, s) for p, s in big if p not in tracked]


def pre_commit() -> int:
    """The pre-commit hook: move big staged files to LFS. Never blocks a commit."""
    try:
        threshold = lfs_threshold()
        if threshold <= 0 or not _auto_lfs_enabled():
            return 0
        big = big_staged_files(threshold)
        if not big:
            return 0
        listing = ", ".join(f"{p} ({_human(s)})" for p, s in big)
        if not _have_lfs():
            _log(f"these files are over {_human(threshold)} but git-lfs isn't installed, so they're committed "
                 f"as normal files: {listing}. Install git-lfs (https://git-lfs.com) to store them with LFS.")
            return 0
        paths = [p for p, _ in big]
        if not _git("config", "--get", "filter.lfs.clean").stdout.strip():
            _git("lfs", "install", "--local")  # the lfs filter isn't configured for this repo yet
        r = _git("lfs", "track", "--filename", "--", *paths)
        if r.returncode != 0:
            _log(f"git lfs track failed, committing as normal files: {r.stderr.strip()}")
            return 0
        _git("add", "--", ".gitattributes", check=True)
        # --renormalize runs the (now lfs) clean filter again even though the files look unchanged.
        _git("add", "--renormalize", "--", *paths, check=True)
        _log(f"stored with Git LFS (over {_human(threshold)}): {listing}")
    except (OSError, subprocess.CalledProcessError, ValueError) as e:
        _log(f"auto-LFS skipped: {e}")
    return 0


def _hook_script() -> str:
    lines = ["#!/bin/sh", f"{HOOK_MARK} (settings: run `{NAME}` with no arguments)"]
    me = os.path.abspath(sys.argv[0]) if sys.argv and os.path.basename(sys.argv[0]).startswith(NAME) else ""
    if me and os.access(me, os.X_OK) and "'" not in me:
        lines.append(f"[ -x '{me}' ] && exec '{me}' pre-commit")
    lines += [f"command -v {NAME} >/dev/null 2>&1 && exec {NAME} pre-commit", "exit 0", ""]
    return "\n".join(lines)


def _install_hook(hooks_dir: str) -> str:
    """Write the pre-commit hook into `hooks_dir`. Returns what happened."""
    path = os.path.join(hooks_dir, "pre-commit")
    if os.path.exists(path):
        with open(path, errors="replace") as f:
            current = f.read()
        if HOOK_MARK not in current:
            return (f"{path} already exists, so it was left alone; add this line to it to get auto-LFS:\n"
                    f"  {NAME} pre-commit")
    os.makedirs(hooks_dir, exist_ok=True)
    with open(path, "w") as f:
        f.write(_hook_script())
    os.chmod(path, 0o755)
    return f"installed {path}"


def lfs_install_repo() -> int:
    """`lfs-install`: set up auto-LFS in the current repo (for repos cloned before `setup`)."""
    r = _git("rev-parse", "--git-path", "hooks")
    if r.returncode != 0:
        _log("not inside a git repository")
        return 1
    if _have_lfs():
        _git("lfs", "install", "--local")
    else:
        _log("git-lfs isn't installed (https://git-lfs.com); big files stay normal files until it is.")
    _log(_install_hook(os.path.abspath(r.stdout.strip())))
    return 0


def setup_lfs() -> None:
    if _have_lfs():
        _git("lfs", "install", "--skip-repo")
    else:
        _log("git-lfs isn't installed; install it (https://git-lfs.com) and run setup again so files over "
             f"{_human(lfs_threshold())} go to Git LFS automatically.")
    # New clones and `git init` copy hooks from init.templateDir. Use ours unless one is set already.
    template = _git("config", "--global", "--get", "init.templateDir").stdout.strip()
    if not template:
        template = os.path.join(_config_dir(), "template")
        _git("config", "--global", "init.templateDir", template, check=True)
    msg = _install_hook(os.path.join(os.path.expanduser(template), "hooks"))
    _log(f"auto-LFS: files over {_human(lfs_threshold())} are stored with Git LFS in fileapi repos "
         f"({msg}; repos cloned before now: run `{NAME} lfs-install` in them).")


def setup(base: str) -> int:
    key = f"credential.{base}.helper"
    # An empty helper first resets any global helpers (store, cache, libsecret) for this host, so a stale
    # copy of an expired token can't shadow this helper.
    subprocess.run(["git", "config", "--global", "--unset-all", key], check=False)
    subprocess.run(["git", "config", "--global", "--add", key, ""], check=True)
    subprocess.run(["git", "config", "--global", "--add", key, "fileapi"], check=True)
    subprocess.run(["git", "config", "--global", f"credential.{base}.username", "fileapi"], check=True)
    _log(f"git now asks {NAME} for {base}. Try: git clone {base}/git/<handle>/<repo>.git")
    setup_lfs()
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
    if cmd == "pre-commit":
        return pre_commit()
    if cmd == "lfs-install":
        return lfs_install_repo()
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
        t = lfs_threshold()
        print(f"auto-LFS: {'files over ' + _human(t) if t > 0 else 'off'}"
              f"{'' if _have_lfs() else ' (git-lfs not installed)'}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
