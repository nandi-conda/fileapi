import base64
import hashlib
import os
import secrets
import socket
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlparse

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fileapi.app import create_app

TOKEN = "test-static-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ALLOWED = "allowed@gmail.com"
PUBLIC = "http://127.0.0.1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def app_for(tmp_path, **kw):
    defaults = dict(
        public_url=PUBLIC,
        google_client_id="test-client-id.apps.googleusercontent.com",
        allowed_emails={ALLOWED},
        static_token=TOKEN,
        state_dir=str(tmp_path / "state"),
        # Test double for Google: the "ID token" is "<email>|<nonce>".
        verify_id_token=lambda tok, nonce: tok.split("|")[0] if tok.endswith("|" + nonce) else None,
    )
    defaults.update(kw)
    return create_app(str(tmp_path / "data"), **defaults)


# ---------------------------------------------------------------- MCP over real HTTP

@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("srv")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {
        **os.environ,
        "FILEAPI_ROOT": str(tmp / "data"),
        "FILEAPI_STATE_DIR": str(tmp / "state"),
        "FILEAPI_PUBLIC_URL": f"http://127.0.0.1:{port}",
        "FILEAPI_TOKEN": TOKEN,
        "GOOGLE_CLIENT_ID": "x.apps.googleusercontent.com",
        "FILEAPI_ALLOWED_EMAILS": ALLOWED,
        "HOST": "127.0.0.1",
        "PORT": str(port),
    }
    proc = subprocess.Popen([sys.executable, "-m", "fileapi"], env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/health", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield url
    proc.terminate()
    proc.wait(5)


def text(result) -> str:
    return result.content[0].text


def test_mcp_requires_auth_and_advertises_oauth(server_url):
    for headers in ({}, {"Authorization": "Bearer nope"}):
        r = httpx.post(f"{server_url}/mcp", headers={"content-type": "application/json", **headers}, content="{}")
        assert r.status_code == 401, headers
        assert "resource_metadata" in r.headers.get("www-authenticate", "")
    meta = httpx.get(f"{server_url}/.well-known/oauth-authorization-server").json()
    assert meta["authorization_endpoint"].endswith("/authorize")
    assert meta["registration_endpoint"].endswith("/register")
    assert httpx.get(f"{server_url}/.well-known/oauth-protected-resource/mcp").status_code == 200


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(server_url):
    http = httpx2.AsyncClient(headers=AUTH)
    async with Client(streamable_http_client(f"{server_url}/mcp", http_client=http)) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        assert tools == sorted(
            ["append_file", "create_file", "delete_file", "edit_file", "list_files", "move_file", "read_file", "write_file"]
        )
        assert "Created" in text(await c.call_tool("create_file", {"path": "notes/a.md", "content": "hello world\n"}))
        assert (await c.call_tool("create_file", {"path": "notes/a.md", "content": "x"})).is_error
        assert text(await c.call_tool("read_file", {"path": "notes/a.md"})) == "hello world\n"
        listing = text(await c.call_tool("list_files", {"recursive": True}))
        assert "notes/\n" in listing and "notes/a.md\t12B" in listing
        await c.call_tool("edit_file", {"path": "notes/a.md", "edits": [{"old_text": "world", "new_text": "there"}]})
        assert text(await c.call_tool("read_file", {"path": "notes/a.md"})) == "hello there\n"
        bad = await c.call_tool("edit_file", {"path": "notes/a.md", "edits": [{"old_text": "zzz", "new_text": ""}]})
        assert bad.is_error and "not found" in text(bad)
        await c.call_tool("append_file", {"path": "notes/a.md", "content": "more\n"})
        assert text(await c.call_tool("read_file", {"path": "notes/a.md"})) == "hello there\nmore\n"
        assert "Overwrote" in text(await c.call_tool("write_file", {"path": "notes/a.md", "content": "new"}))
        await c.call_tool("move_file", {"source": "notes/a.md", "destination": "archive/b.md"})
        assert text(await c.call_tool("read_file", {"path": "archive/b.md"})) == "new"
        await c.call_tool("delete_file", {"path": "archive/b.md"})
        assert (await c.call_tool("read_file", {"path": "archive/b.md"})).is_error
        for p in ["../etc/passwd", "a/../../x", "/../x"]:
            assert (await c.call_tool("read_file", {"path": p})).is_error, p


# ---------------------------------------------------------------- OAuth flow

def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _start_login(c: TestClient):
    reg = c.post("/register", json={"redirect_uris": ["http://localhost:9999/cb"], "client_name": "Test App",
                                     "token_endpoint_auth_method": "none"})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    r = c.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": "http://localhost:9999/cb",
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "client-state", "scope": "files",
    }, follow_redirects=False)
    assert r.status_code == 302, r.text
    login_url = urlparse(r.headers["location"])
    assert login_url.path == "/oauth/login"
    state = parse_qs(login_url.query)["state"][0]
    page = c.get(f"/oauth/login?state={state}")
    assert page.status_code == 200 and "Test App" in page.text and "accounts.google.com/gsi/client" in page.text
    nonce = page.text.split('data-nonce="')[1].split('"')[0]
    return client_id, verifier, state, nonce


def test_oauth_allowlisted_google_account_gets_working_token(tmp_path):
    with TestClient(app_for(tmp_path)) as c:
        client_id, verifier, state, nonce = _start_login(c)
        r = c.post("/oauth/google/callback", data={"state": state, "credential": f"{ALLOWED}|{nonce}"},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["state"] == ["client-state"]

        tok = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": client_id,
                                     "redirect_uri": "http://localhost:9999/cb", "code_verifier": verifier})
        assert tok.status_code == 200, tok.text
        access, refresh = tok.json()["access_token"], tok.json()["refresh_token"]
        h = {"Authorization": f"Bearer {access}"}

        # code is single-use
        again = c.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "client_id": client_id,
                                       "redirect_uri": "http://localhost:9999/cb", "code_verifier": verifier})
        assert again.status_code == 400

        # token works for REST and MCP
        assert c.put("/files/hi.txt", content="hi", headers=h).status_code == 201
        rpc = c.post("/mcp", headers={**h, "Accept": "application/json, text/event-stream"},
                     json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}}})
        assert rpc.status_code == 200, rpc.text

        # refresh rotates: old access token dies, new one works
        new = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        assert new.status_code == 200, new.text
        assert c.get("/files", headers=h).status_code == 401
        assert c.get("/files", headers={"Authorization": f"Bearer {new.json()['access_token']}"}).status_code == 200


def test_oauth_rejects_account_not_on_allowlist(tmp_path):
    with TestClient(app_for(tmp_path)) as c:
        _, _, state, nonce = _start_login(c)
        r = c.post("/oauth/google/callback", data={"state": state, "credential": f"intruder@gmail.com|{nonce}"},
                   follow_redirects=False)
        assert r.status_code == 403 and "not on the allowlist" in r.text


def test_oauth_rejects_token_with_wrong_nonce(tmp_path):
    with TestClient(app_for(tmp_path)) as c:
        _, _, state, _ = _start_login(c)
        r = c.post("/oauth/google/callback", data={"state": state, "credential": f"{ALLOWED}|other-nonce"},
                   follow_redirects=False)
        assert r.status_code == 401
        # state is consumed, so it cannot be retried
        r = c.post("/oauth/google/callback", data={"state": state, "credential": f"{ALLOWED}|x"})
        assert r.status_code == 400


def test_removing_email_from_allowlist_revokes_tokens(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app) as c:
        client_id, verifier, state, nonce = _start_login(c)
        r = c.post("/oauth/google/callback", data={"state": state, "credential": f"{ALLOWED}|{nonce}"},
                   follow_redirects=False)
        code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        access = c.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                        "redirect_uri": "http://localhost:9999/cb", "code_verifier": verifier}
                        ).json()["access_token"]
        h = {"Authorization": f"Bearer {access}"}
        assert c.get("/files", headers=h).status_code == 200
        app.state.auth.config.allowed_emails.clear()
        assert c.get("/files", headers=h).status_code == 401


def test_cancel_returns_access_denied(tmp_path):
    with TestClient(app_for(tmp_path)) as c:
        _, _, state, _ = _start_login(c)
        r = c.post("/oauth/deny", data={"state": state}, follow_redirects=False)
        assert r.status_code == 303 and "error=access_denied" in r.headers["location"]


def test_google_id_token_verifier(monkeypatch):
    """The real verifier, against a locally signed token and a fake Google JWKS."""
    import json

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    from fileapi import auth

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="k1", alg="RS256", use="sig")
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    verify = auth.google_id_token_verifier("cid")

    def make(**over):
        claims = {"iss": "https://accounts.google.com", "aud": "cid", "exp": int(time.time()) + 300,
                  "email": ALLOWED, "email_verified": True, "nonce": "n1", **over}
        return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})

    assert verify(make(), "n1") == ALLOWED
    assert verify(make(), "n2") is None
    assert verify(make(aud="other"), "n1") is None
    assert verify(make(iss="https://evil.example"), "n1") is None
    assert verify(make(email_verified=False), "n1") is None
    assert verify(make(exp=int(time.time()) - 10), "n1") is None
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": "accounts.google.com", "aud": "cid", "exp": int(time.time()) + 300,
                         "email": ALLOWED, "email_verified": True, "nonce": "n1"}, other, algorithm="RS256",
                        headers={"kid": "k1"})
    assert verify(forged, "n1") is None


# ---------------------------------------------------------------- REST API

def test_rest_api(tmp_path):
    with TestClient(app_for(tmp_path)) as c:
        assert c.get("/files").status_code == 401
        assert c.get("/files", headers={"Authorization": "Bearer nope"}).status_code == 401

        r = c.put("/files/docs/x.txt", content="abc abc def", headers=AUTH)
        assert r.status_code == 201 and r.json()["created"] is True
        assert c.put("/files/docs/x.txt?overwrite=false", content="z", headers=AUTH).status_code == 409
        assert c.get("/files/docs/x.txt", headers=AUTH).text == "abc abc def"
        assert [e["path"] for e in c.get("/files?recursive=true", headers=AUTH).json()] == ["docs/", "docs/x.txt"]
        assert [e["path"] for e in c.get("/files/docs", headers=AUTH).json()] == ["docs/x.txt"]

        r = c.patch("/files/docs/x.txt", json={"edits": [{"old_text": "abc", "new_text": "X"}]}, headers=AUTH)
        assert r.status_code == 400
        r = c.patch("/files/docs/x.txt", json={"edits": [{"old_text": "def", "new_text": "ghi"}]}, headers=AUTH)
        assert r.status_code == 200
        assert c.get("/files/docs/x.txt", headers=AUTH).text == "abc abc ghi"

        c.post("/files/docs/x.txt:append", content="!", headers=AUTH)
        assert c.get("/files/docs/x.txt", headers=AUTH).text == "abc abc ghi!"
        assert c.post("/files/docs/x.txt:move", json={"destination": "y.txt"}, headers=AUTH).status_code == 200
        assert c.get("/files/docs/x.txt", headers=AUTH).status_code == 404
        assert c.delete("/files/y.txt", headers=AUTH).status_code == 204
        assert c.get("/files/y.txt", headers=AUTH).status_code == 404
        assert c.get("/files/..%2F..%2Fetc%2Fpasswd", headers=AUTH).status_code in (400, 404)


def test_state_dir_must_be_outside_storage(tmp_path):
    with pytest.raises(RuntimeError):
        app_for(tmp_path, state_dir=str(tmp_path / "data" / "state"))
