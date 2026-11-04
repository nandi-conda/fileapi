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

TOKEN = "test-static-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FILE_TOOLS = ["append_file", "create_file", "delete_file", "edit_file", "list_files", "move_file", "read_file", "write_file"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def app_for(tmp_path, **kw):
    kw.setdefault("state_dir", str(tmp_path / "state"))
    kw.setdefault("preset_token", TOKEN)
    kw.setdefault("git_root", str(tmp_path / "repos"))
    return create_app(str(tmp_path / "data"), **kw)


PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(params=["filesystem", "postgres"])
def backend(request):
    if request.param == "postgres":
        if not PG_URL:
            pytest.skip("set TEST_DATABASE_URL to run Postgres tests")
        import psycopg

        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP TABLE IF EXISTS fileapi_files, fileapi_kv, fileapi_git_objects, fileapi_git_refs, fileapi_git_repos")
        return {"DATABASE_URL": PG_URL}
    return {}


@pytest.fixture
def server_url(tmp_path, backend):
    """A real server process that starts unclaimed."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "FILEAPI_ROOT": str(tmp_path / "data"), "FILEAPI_STATE_DIR": str(tmp_path / "state"),
           "FILEAPI_GIT_ROOT": str(tmp_path / "repos"),
           "HOST": "127.0.0.1", "PORT": str(port), "FILEAPI_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    env.pop("FILEAPI_TOKEN", None)
    env.pop("DATABASE_URL", None)
    env.update(backend)
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


def connect(url: str, token: str | None = None, via: str = "header") -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token and via == "header" else {}
    endpoint = f"{url}/mcp" + (f"?token={token}" if token and via == "query" else "")
    return Client(streamable_http_client(endpoint, http_client=httpx2.AsyncClient(headers=headers)))


def text(result) -> str:
    return result.content[0].text


@pytest.mark.anyio
async def test_first_client_claims_then_token_required(server_url):
    h = httpx.get(f"{server_url}/health").json()
    assert h["claimed"] is False and h["storage"] in ("disk", "postgres")

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
    # old token keeps working during the grace period
    async with connect(server_url, token) as c:
        assert not (await c.call_tool("list_files", {})).is_error
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


def test_rotated_token_expires_after_grace(tmp_path):
    from fileapi.tokens import FileKV, TokenStore

    store = TokenStore(FileKV(tmp_path))
    first = store.claim()
    assert first and store.claim() is None
    store.ROTATE_GRACE = 0
    second = store.rotate()
    assert store.check(second) and not store.check(first) and not store.check("fa_wrong")


def test_rest_api(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as c:
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


def test_directory_semantics(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as c:
        for p in ["a/b/one.txt", "a/b/two.txt", "a/three.txt", "a_b.txt"]:
            assert c.put(f"/files/{p}", content=p, headers=AUTH).status_code == 201
        assert [e["path"] for e in c.get("/files", headers=AUTH).json()] == ["a/", "a_b.txt"]
        assert [e["path"] for e in c.get("/files/a", headers=AUTH).json()] == ["a/b/", "a/three.txt"]
        assert c.put("/files/a/three.txt/x", content="x", headers=AUTH).status_code == 400  # parent is a file
        assert c.put("/files/a", content="x", headers=AUTH).status_code == 400  # is a directory
        assert c.delete("/files/a", headers=AUTH).status_code == 409  # not empty
        assert c.post("/files/a:move", json={"destination": "z"}, headers=AUTH).status_code == 200
        assert c.get("/files/z/b/one.txt", headers=AUTH).text == "a/b/one.txt"
        assert c.get("/files/a/three.txt", headers=AUTH).status_code == 404
        assert c.get("/files/a_b.txt", headers=AUTH).text == "a_b.txt"


def test_index_page(tmp_path):
    with TestClient(app_for(tmp_path, preset_token="")) as c:
        r = c.get("/")
        assert r.status_code == 200 and "Not claimed yet" in r.text and "/mcp" in r.text


def test_cloud_entrypoint_refuses_ephemeral_disk():
    env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "FILEAPI_ROOT")}
    r = subprocess.run([sys.executable, "-c", "import fileapi.main"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "DATABASE_URL is not set" in r.stderr


# ---- git forge ---------------------------------------------------------------


def git(*args, cwd=None, check=True):
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=check)


@pytest.mark.anyio
async def test_git_forge_end_to_end(server_url, tmp_path):
    async with connect(server_url) as c:
        token = next(w for w in text(await c.call_tool("claim_server", {})).split() if w.startswith("fa_"))
    remote = server_url.replace("://", f"://x:{token}@") + "/git/web/blog.git"

    async with connect(server_url, token) as c:
        r = await c.call_tool("create_repo", {"project": "web", "name": "blog", "description": "My blog"})
        assert not r.is_error and f"{server_url}/git/web/blog.git" in text(r)
        assert (await c.call_tool("create_repo", {"project": "web", "name": "blog"})).is_error
        assert (await c.call_tool("create_repo", {"project": "../x", "name": "blog"})).is_error
        assert "empty" in text(await c.call_tool("list_branches", {"repo": "web/blog"}))
        r = await c.call_tool("commit_files", {"repo": "web/blog", "message": "init",
                                               "files": {"README.md": "# Blog\n", "posts/one.md": "one\n"}})
        assert not r.is_error, text(r)
        assert "web/blog" in text(await c.call_tool("list_repos", {}))

    # Clone, commit and push with the real git CLI.
    work = tmp_path / "work"
    git("clone", remote, str(work))
    assert (work / "posts" / "one.md").read_text() == "one\n"
    (work / "posts" / "two.md").write_text("two\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "second post", cwd=work)
    git("push", "origin", "main", "main:draft", cwd=work)

    async with connect(server_url, token) as c:
        listing = text(await c.call_tool("browse_repo", {"repo": "web/blog", "path": "posts"}))
        assert "one.md" in listing and "two.md" in listing
        assert text(await c.call_tool("browse_repo", {"repo": "web/blog", "path": "posts/two.md"})) == "two\n"
        log = text(await c.call_tool("repo_log", {"repo": "web/blog"}))
        assert log.index("second post") < log.index("init")
        assert "branch draft" in text(await c.call_tool("list_branches", {"repo": "web/blog"}))
        r = await c.call_tool("commit_files", {"repo": "web/blog", "message": "drop one",
                                               "files": {"posts/one.md": None}})
        assert not r.is_error, text(r)

    git("pull", "origin", "main", cwd=work)
    assert not (work / "posts" / "one.md").exists()
    assert git("fsck", cwd=work).returncode == 0

    # Bad token: git gets a Basic-auth challenge and fails; a Bearer header works too.
    bad = server_url.replace("://", "://x:nope@") + "/git/web/blog.git"
    assert git("ls-remote", bad, check=False).returncode != 0
    r = git("-c", f"http.extraHeader=Authorization: Bearer {token}", "ls-remote", f"{server_url}/git/web/blog.git")
    assert "refs/heads/draft" in r.stdout

    async with connect(server_url, token) as c:
        assert not (await c.call_tool("delete_repo", {"repo": "web/blog"})).is_error
    assert git("ls-remote", remote, check=False).returncode != 0


def test_git_http_auth_and_project_scope(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("web", "site")
        forge.create("other", "secret")
        refs = "/git/web/site.git/info/refs?service=git-upload-pack"
        r = client.get(refs)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert client.get(refs, auth=("anyone", TOKEN)).status_code == 200
        assert client.get(refs, headers=AUTH).headers["content-type"] == "application/x-git-upload-pack-advertisement"
        assert client.get("/git/web/nope.git/info/refs?service=git-upload-pack", headers=AUTH).status_code == 404
        assert client.get("/git/web/site.git/info/refs", headers=AUTH).status_code == 403  # dumb protocol

        # Per-project tokens (TokenStore.scope) only reach their own project.
        client.app.state.tokens.scope = lambda t: {"ptok": "web", TOKEN: ""}.get(t)
        assert client.get(refs, auth=("x", "ptok")).status_code == 200
        assert client.get("/git/other/secret.git/info/refs?service=git-upload-pack",
                          auth=("x", "ptok")).status_code == 403
