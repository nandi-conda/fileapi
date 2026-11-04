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
from fileapi.storage import StorageError

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
    """A real server process; FILEAPI_TOKEN stands in for an OAuth sign-in."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "FILEAPI_ROOT": str(tmp_path / "data"), "FILEAPI_STATE_DIR": str(tmp_path / "state"),
           "FILEAPI_GIT_ROOT": str(tmp_path / "repos"),
           "HOST": "127.0.0.1", "PORT": str(port), "FILEAPI_PUBLIC_URL": f"http://127.0.0.1:{port}"}
    env["FILEAPI_TOKEN"] = TOKEN
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
async def test_mcp_requires_sign_in(server_url):
    h = httpx.get(f"{server_url}/health").json()
    assert h["auth"] == "atproto" and h["admin"].startswith("did:") and h["storage"] in ("disk", "postgres")

    # No token or a wrong one: 401 that points MCP clients at the OAuth metadata.
    for headers, url in [({}, f"{server_url}/mcp"), ({"Authorization": "Bearer nope"}, f"{server_url}/mcp"),
                         ({}, f"{server_url}/mcp?token=nope")]:
        r = httpx.post(url, headers={"content-type": "application/json", **headers}, content="{}")
        assert r.status_code == 401
        assert f'resource_metadata="{server_url}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    meta = httpx.get(f"{server_url}/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{server_url}/mcp" and meta["authorization_servers"] == [server_url]
    asm = httpx.get(f"{server_url}/.well-known/oauth-authorization-server").json()
    assert asm["issuer"] == server_url and asm["registration_endpoint"] == f"{server_url}/register"

    for via in ("header", "query"):
        async with connect(server_url, TOKEN, via=via) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"whoami", *FILE_TOOLS} <= names and "claim_server" not in names
            assert text(await c.call_tool("list_files", {})) == "(empty)"


@pytest.mark.anyio
async def test_mcp_tools_end_to_end(server_url):
    async with connect(server_url, TOKEN) as c:
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


def test_rest_api_rejects_everything_without_sign_in(tmp_path):
    with TestClient(app_for(tmp_path, preset_token="")) as c:
        assert c.get("/files").status_code == 401
        assert c.get("/files", headers=AUTH).status_code == 401


def test_file_kv_pop_is_single_use(tmp_path):
    from fileapi.tokens import FileKV

    kv = FileKV(tmp_path)
    kv.put("k", {"v": 1})
    assert kv.pop("k") == {"v": 1} and kv.pop("k") is None and kv.get("k") is None


# ---- atproto OAuth ------------------------------------------------------------

ADMIN = "did:plc:admin"


class FakeAtproto:
    """Stands in for the admin's atproto authorization server."""

    def __init__(self):
        from fileapi.auth import AuthServer

        self.server = AuthServer("https://as.example", "https://as.example/par", "https://as.example/authorize",
                                 "https://as.example/token")
        self.pars, self.sub = [], ADMIN

    async def auth_server_for(self, did):
        assert did == ADMIN
        return self.server

    async def par(self, server, params, key):
        self.pars.append(params)
        return "urn:req:1", "nonce1"

    async def redeem(self, server, params, key, nonce):
        assert params["code"] == "atcode" and nonce == "nonce1"
        return {"sub": self.sub, "scope": "atproto", "access_token": "x", "token_type": "DPoP"}


def _sign_in(c, fake, client, redirect="http://127.0.0.1:9/cb"):
    import base64 as b64
    import hashlib
    from urllib.parse import parse_qs, urlparse

    verifier = "v" * 50
    challenge = b64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client["client_id"], "redirect_uri": redirect,
                                    "code_challenge": challenge, "code_challenge_method": "S256", "state": "s1"},
              follow_redirects=False)
    assert r.status_code == 200 and "Continue with" in r.text and "http://127.0.0.1:9" in r.text
    assert r.headers["x-frame-options"] == "DENY"
    rid = r.text.split('name=rid value="')[1].split('"')[0]
    assert c.post("/authorize", data={"rid": rid}, headers={"origin": "https://evil.example"}).status_code == 403
    r = c.post("/authorize", data={"rid": rid}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://as.example/authorize?")
    assert c.post("/authorize", data={"rid": rid}).status_code == 400  # single use
    par = fake.pars[-1]
    assert par["login_hint"] and par["scope"] == "atproto" and par["code_challenge_method"] == "S256"
    r = c.get("/oauth/atproto/callback", params={"code": "atcode", "state": par["state"], "iss": "https://as.example"},
              follow_redirects=False)
    return r, verifier


def test_atproto_oauth_flow(tmp_path, backend):
    from urllib.parse import parse_qs, urlparse

    fake = FakeAtproto()
    app = app_for(tmp_path, preset_token="", admin_did=ADMIN, atproto=fake, public_url="http://127.0.0.1:8000",
                  database_url=backend.get("DATABASE_URL", ""))
    with TestClient(app) as c:
        meta = c.get("/oauth/atproto/client-metadata.json").json()
        assert meta["client_id"].startswith("http://localhost?redirect_uri=http%3A%2F%2F127.0.0.1%3A8000")
        assert meta["dpop_bound_access_tokens"] is True

        assert c.post("/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400
        client = c.post("/register", json={"redirect_uris": ["http://127.0.0.1:9/cb"], "client_name": "t",
                                           "token_endpoint_auth_method": "none"}).json()
        assert "client_secret" not in client
        assert c.get("/authorize", params={"client_id": client["client_id"], "redirect_uri": "http://127.0.0.1:9/x",
                                           "response_type": "code", "code_challenge": "c"}).status_code == 400

        r, verifier = _sign_in(c, fake, client)
        assert r.status_code == 302
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["state"] == ["s1"] and q["iss"] == ["http://127.0.0.1:8000"]
        code = q["code"][0]

        form = {"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                "redirect_uri": "http://127.0.0.1:9/cb"}
        assert c.post("/token", data={**form, "code_verifier": "wrong"}).json()["error"] == "invalid_grant"
        # A failed attempt burns the code; sign in again.
        r, verifier = _sign_in(c, fake, client)
        form["code"] = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
        tok = c.post("/token", data={**form, "code_verifier": verifier}).json()
        assert tok["token_type"] == "Bearer" and tok["access_token"].startswith("fa_")
        assert c.post("/token", data={**form, "code_verifier": verifier}).status_code == 400  # code is single-use

        bearer = {"Authorization": f"Bearer {tok['access_token']}"}
        assert c.put("/files/a.txt", content="hi", headers=bearer).status_code == 201
        app.state.forge.create("p", "r")
        assert c.get("/git/p/r.git/info/refs?service=git-upload-pack",
                     auth=("x", tok["access_token"])).status_code == 200

        # Refresh rotates both tokens; the old ones stop working.
        new = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                     "client_id": client["client_id"]}).json()
        assert c.get("/files/a.txt", headers=bearer).status_code == 401
        assert c.post("/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                      "client_id": client["client_id"]}).status_code == 400
        bearer = {"Authorization": f"Bearer {new['access_token']}"}
        assert c.get("/files/a.txt", headers=bearer).text == "hi"
        assert c.post("/revoke", data={"token": new["refresh_token"], "client_id": client["client_id"]}).status_code == 200
        assert c.get("/files/a.txt", headers=bearer).status_code == 401

        # Anyone else who signs in at the authorization server is refused, and gets no code.
        fake.sub = "did:plc:someone-else"
        r, _ = _sign_in(c, fake, client)
        assert r.status_code == 403 and "location" not in r.headers
        # A replayed or unknown state is refused too.
        assert c.get("/oauth/atproto/callback", params={"code": "atcode", "state": fake.pars[-1]["state"],
                                                        "iss": "https://as.example"}).status_code == 400


def test_confidential_client_needs_its_secret(tmp_path):
    fake = FakeAtproto()
    with TestClient(app_for(tmp_path, preset_token="", admin_did=ADMIN, atproto=fake)) as c:
        client = c.post("/register", json={"redirect_uris": ["https://app.example/cb"],
                                           "token_endpoint_auth_method": "client_secret_post"}).json()
        assert client["client_secret"].startswith("fs_")
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x", "client_id": client["client_id"]})
        assert r.status_code == 401
        r = c.post("/token", data={"grant_type": "refresh_token", "refresh_token": "x"},
                   auth=(client["client_id"], client["client_secret"]))
        assert r.json()["error"] == "invalid_grant"


def test_dpop_proof_is_valid_jwt():
    import jwt
    from fileapi.auth import Atproto

    key = Atproto.new_dpop_key()
    proof = Atproto.dpop_proof(key, "POST", "https://as.example/par", "n1")
    header = jwt.get_unverified_header(proof)
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256" and header["jwk"]["kty"] == "EC"
    pub = jwt.algorithms.ECAlgorithm.from_jwk(header["jwk"])
    claims = jwt.decode(proof, pub, algorithms=["ES256"])
    assert claims["htm"] == "POST" and claims["htu"] == "https://as.example/par" and claims["nonce"] == "n1"


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
        assert r.status_code == 200 and "nandi.uk" in r.text and "/mcp" in r.text


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
    token = TOKEN
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

    # Public repos clone anonymously; pushing still needs the token.
    anon = f"{server_url}/git/web/blog.git"
    assert git("ls-remote", anon, check=False).returncode != 0
    async with connect(server_url, token) as c:
        r = await c.call_tool("set_repo_visibility", {"repo": "web/blog", "visibility": "public"})
        assert not r.is_error, text(r)
        assert "web/blog\tpublic" in text(await c.call_tool("list_repos", {}))
    git("clone", anon, str(tmp_path / "anon"))
    assert (tmp_path / "anon" / "posts" / "two.md").exists()
    assert git("push", anon, "main:anon", cwd=tmp_path / "anon", check=False).returncode != 0
    async with connect(server_url, token) as c:
        assert not (await c.call_tool("set_repo_visibility", {"repo": "web/blog", "visibility": "private"})).is_error
    assert git("ls-remote", anon, check=False).returncode != 0

    async with connect(server_url, token) as c:
        assert not (await c.call_tool("delete_repo", {"repo": "web/blog"})).is_error
    assert git("ls-remote", remote, check=False).returncode != 0


def test_git_http_auth(tmp_path, backend):
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

        # Public: anonymous upload-pack (clone/fetch) works, receive-pack (push) does not.
        forge.set_public("web/site", True)
        assert client.get(refs).status_code == 200
        assert client.get("/git/web/site.git/info/refs?service=git-receive-pack").status_code == 401
        assert client.post("/git/web/site.git/git-receive-pack", content=b"0000",
                           headers={"content-type": "application/x-git-receive-pack-request"}).status_code == 401
        assert [r.public for r in forge.list("web")] == [True]
        forge.set_public("web/site", False)
        assert client.get(refs).status_code == 401
        assert client.get(refs, auth=("x", "nope")).status_code == 401


def test_git_bundles_and_repo_passes(tmp_path, backend):
    from fileapi.forge import RepoPasses, fetch_bundle, push_bundle

    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("b", "r")
        w = tmp_path / "w"
        git("init", "-q", "-b", "main", str(w))
        (w / "a").write_text("1\n")
        git("add", ".", cwd=w)
        git("commit", "-qm", "one", cwd=w)
        base = git("rev-parse", "main", cwd=w).stdout.strip()
        git("bundle", "create", str(tmp_path / "1.bundle"), "main", cwd=w)
        assert push_bundle(forge, "b/r", (tmp_path / "1.bundle").read_bytes())[0]["status"] == "created"

        # Incremental bundle (thin pack against what the server has), then a rejected non-fast-forward.
        (w / "a").write_text("2\n")
        git("commit", "-qam", "two", cwd=w)
        git("bundle", "create", str(tmp_path / "2.bundle"), "main", f"^{base}", cwd=w)
        assert push_bundle(forge, "b/r", (tmp_path / "2.bundle").read_bytes())[0]["status"] == "updated"
        git("reset", "-q", "--hard", base, cwd=w)
        git("commit", "-q", "--allow-empty", "-m", "diverge", cwd=w)
        git("bundle", "create", str(tmp_path / "3.bundle"), "main", cwd=w)
        three = (tmp_path / "3.bundle").read_bytes()
        assert push_bundle(forge, "b/r", three)[0]["status"] == "rejected"
        assert push_bundle(forge, "b/r", three, force=True)[0]["status"] == "forced"
        with pytest.raises(StorageError):
            push_bundle(forge, "b/r", b"not a bundle")

        # Fetch: a full bundle clones (HEAD included); with `have` at the tip there is nothing to send.
        data, tips = fetch_bundle(forge, "b/r")
        (tmp_path / "f.bundle").write_bytes(data)
        git("clone", "-q", str(tmp_path / "f.bundle"), str(tmp_path / "c"))
        assert "diverge" in git("log", "--oneline", cwd=tmp_path / "c").stdout
        assert fetch_bundle(forge, "b/r", have=[tips["refs/heads/main"]])[0] == b""

        # Short-lived passes work for one repo only, and expire.
        passes = RepoPasses(client.app.state.auth.kv)
        p, _ = passes.issue("b/r", 5)
        refs = "/git/b/r.git/info/refs?service=git-upload-pack"
        assert client.get(refs, auth=("x", p)).status_code == 200
        forge.create("b", "other")
        assert client.get("/git/b/other.git/info/refs?service=git-upload-pack", auth=("x", p)).status_code == 401
        assert not passes.check(p.replace(".", ".0", 1), "b/r")
        expired = passes._sig("b/r|1")
        import base64 as b64
        assert not passes.check("fg_" + b64.urlsafe_b64encode(b"b/r|1").decode().rstrip("=") + "." + expired, "b/r")


def test_forge_json_api_and_web_ui(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("web", "site", "The site")
        forge.commit_files("web/site", "main", {"README.md": "hi\n", "src/a.py": "x = 1\n"}, "first")
        forge.commit_files("web/site", "main", {"src/a.py": "x = 2\n"}, "second")
        forge.create("web", "secret")

        # Anonymous: only public repos exist.
        assert client.get("/api/forge/me").json()["signed_in"] is False
        assert client.get("/api/forge/repos").json() == {"repos": []}
        assert client.get("/api/forge/repos/web/site").status_code == 404
        assert client.get("/api/forge/repos/web/site/tree").status_code == 404
        forge.set_public("web/site", True)
        assert [r["repo"] for r in client.get("/api/forge/repos").json()["repos"]] == ["web/site"]
        info = client.get("/api/forge/repos/web/site").json()
        assert info["default"] == "main" and set(info["branches"]) == {"main"} and info["public"] is True
        assert info["clone_url"].endswith("/git/web/site.git") and info["description"] == "The site"
        tree = client.get("/api/forge/repos/web/site/tree").json()
        assert tree["type"] == "dir" and [e["name"] for e in tree["entries"]] == ["src/", "README.md"]
        f = client.get("/api/forge/repos/web/site/tree", params={"path": "src/a.py"}).json()
        assert f["type"] == "file" and f["content"] == "x = 2\n"
        old = client.get("/api/forge/repos/web/site/tree", params={"path": "src/a.py", "ref": "main~1"})
        assert old.status_code == 404  # only branch, tag or sha refs
        log = client.get("/api/forge/repos/web/site/log").json()["commits"]
        assert [c["message"] for c in log] == ["second", "first"]
        f = client.get("/api/forge/repos/web/site/tree", params={"path": "src/a.py", "ref": log[1]["sha"]}).json()
        assert f["content"] == "x = 1\n"
        assert client.get("/api/forge/repos/web/secret").status_code == 404

        # Signed in: private repos too.
        assert client.get("/api/forge/me", headers=AUTH).json()["signed_in"] is True
        assert len(client.get("/api/forge/repos", headers=AUTH).json()["repos"]) == 2
        assert client.get("/api/forge/repos/web/secret", headers=AUTH).json()["branches"] == {}
        assert client.get("/api/forge/repos/web/secret/tree", headers=AUTH).status_code == 404  # empty repo

        # The committed Qt for WebAssembly build is served from /forge/.
        r = client.get("/forge", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/forge/"
        page = client.get("/forge/")
        assert page.status_code == 200 and "qtLoad" in page.text and "fileapiToken" in page.text
        assert client.get("/forge/nope/deep").text == page.text
        wasm = client.get("/forge/forgeui.wasm", headers={"Accept-Encoding": "gzip"})
        assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
        assert wasm.content[:4] == b"\0asm"  # httpx undoes the gzip
        assert int(wasm.headers["x-uncompressed-length"]) == len(wasm.content)
        assert client.get("/forge/qtloader.js").headers["content-type"].startswith("text/javascript")
