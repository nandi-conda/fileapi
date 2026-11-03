import os
import socket
import subprocess
import sys
import time

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from fileapi.app import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    root = tmp_path_factory.mktemp("store")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "FILEAPI_ROOT": str(root), "FILEAPI_TOKEN": TOKEN, "HOST": "127.0.0.1", "PORT": str(port)}
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


def mcp_client(url: str, token: str = TOKEN) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(f"{url}/mcp", http_client=http))


def text(result) -> str:
    return result.content[0].text


def test_mcp_rejects_bad_token(server_url):
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Basic {TOKEN}"}):
        r = httpx.post(f"{server_url}/mcp", headers={"content-type": "application/json", **headers}, content="{}")
        assert r.status_code == 401, headers


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(server_url):
    async with mcp_client(server_url) as c:
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


def test_rest_api(tmp_path):
    with TestClient(create_app(str(tmp_path), TOKEN)) as c:
        assert c.get("/files").status_code == 401
        assert c.get("/files", headers={"Authorization": "Bearer nope"}).status_code == 401

        r = c.put("/files/docs/x.txt", content="abc abc def", headers=AUTH)
        assert r.status_code == 201 and r.json()["created"] is True
        assert c.put("/files/docs/x.txt?overwrite=false", content="z", headers=AUTH).status_code == 409

        assert c.get("/files/docs/x.txt", headers=AUTH).text == "abc abc def"
        assert [e["path"] for e in c.get("/files?recursive=true", headers=AUTH).json()] == ["docs/", "docs/x.txt"]
        assert [e["path"] for e in c.get("/files/docs", headers=AUTH).json()] == ["docs/x.txt"]

        # ambiguous match is rejected, unique match applies
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
