import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fileapi.app import create_app

TOKEN = "test-static-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FILE_TOOLS = ["append_file", "create_file", "delete_file", "edit_file", "list_files", "move_file", "read_file", "write_file"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def app_for(tmp_path, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("preset_token", TOKEN)
    return create_app(str(tmp_path / "data"), **kw)


@pytest.fixture
def server_url(tmp_path):
    """A real server process that starts unclaimed."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "FILEAPI_ROOT": str(tmp_path / "data"), "FILEAPI_STATE_DIR": str(tmp_path / "state"),
           "HOST": "127.0.0.1", "PORT": str(port), "FILEAPI_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    env.pop("FILEAPI_TOKEN", None)
    proc = subprocess.Popen([sys.executable, "-m", "fileapi"], env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(f"{url}/health", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    _STATE[url] = str(tmp_path / "state")
    yield url
    proc.terminate()
    proc.wait(5)


_STATE: dict[str, str] = {}


def server_state(url: str) -> str:
    return _STATE[url]


def connect(url: str, token: str | None = None, via: str = "header") -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token and via == "header" else {}
    endpoint = f"{url}/mcp" + (f"?token={token}" if token and via == "query" else "")
    return Client(streamable_http_client(endpoint, http_client=httpx2.AsyncClient(headers=headers)))


def text(result) -> str:
    return result.content[0].text


@pytest.mark.anyio
async def test_first_client_claims_then_token_required(server_url):
    assert httpx.get(f"{server_url}/health").json()["claimed"] is False

    # 1. First client connects with no token: file tools refuse, claim_server hands out the token.
    async with connect(server_url) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"claim_server", "rotate_token", *FILE_TOOLS} <= names
        r = await c.call_tool("list_files", {})
        assert r.is_error and "claim_server" in text(r)
        r = await c.call_tool("claim_server", {})
        assert not r.is_error
        token = next(w for w in text(r).split() if w.startswith("fa_"))
        assert f"{server_url}/mcp?token={token}" in text(r)

    assert httpx.get(f"{server_url}/health").json()["claimed"] is True

    # 2. Now tokenless clients get refused by every tool, and wrong tokens are rejected outright.
    async with connect(server_url) as c:
        r = await c.call_tool("list_files", {})
        assert r.is_error and "access token" in text(r)
        assert (await c.call_tool("claim_server", {})).is_error
    r = httpx.post(f"{server_url}/mcp", headers={"content-type": "application/json", "Authorization": "Bearer nope"},
                   content="{}")
    assert r.status_code == 401
    assert httpx.post(f"{server_url}/mcp?token=nope", headers={"content-type": "application/json"},
                      content="{}").status_code == 401

    # 3. The token works as a header and as ?token=, and a second claim fails.
    async with connect(server_url, token, via="query") as c:
        assert text(await c.call_tool("list_files", {})) == "(empty)"
        r = await c.call_tool("claim_server", {})
        assert r.is_error and "already claimed" in text(r)

    async with connect(server_url, token) as c:
        r = await c.call_tool("rotate_token", {})
        new_token = next(w for w in text(r).split() if w.startswith("fa_"))
    # old token is in its grace period; expire it
    import json as _json
    state = Path(server_state(server_url)) / "token.json"
    rec = _json.loads(state.read_text())
    rec["previous"]["until"] = 0
    state.write_text(_json.dumps(rec))
    assert httpx.post(f"{server_url}/mcp", headers={"content-type": "application/json",
                      "Authorization": f"Bearer {token}"}, content="{}").status_code == 401
    async with connect(server_url, new_token) as c:
        assert text(await c.call_tool("list_files", {})) == "(empty)"


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(server_url):
    async with connect(server_url) as c:
        token = next(w for w in text(await c.call_tool("claim_server", {})).split() if w.startswith("fa_"))
    async with connect(server_url, token) as c:
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


def test_rest_api_rejects_everything_while_unclaimed(tmp_path):
    with TestClient(app_for(tmp_path, preset_token="")) as c:
        assert c.get("/files").status_code == 401


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
