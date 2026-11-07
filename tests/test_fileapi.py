import base64
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
from fileapi.deploy import parse_jobs
from fileapi.storage import StorageError

# Repo secrets are on in every test app (and the server processes they start) unless a test removes it.
os.environ.setdefault("FILEAPI_SECRETS_KEY", "q2Vz1fVv0Hc0dWl3mT1tYk4cQyB3hGvKkq8xR0b8m3E=")

# Run jobs run on Depot. In tests a stand-in for the Depot CLI does what that build would: runs the job's
# script in the checkout with the env file the server passes as a BuildKit secret, forge-release on PATH.
FAKE_DEPOT_RUN = """#!/bin/sh
tools=""; envf=""; prev=""; ctx=""
for a in "$@"; do
  case "$prev" in --build-context) tools="${a#forge-tools=}";; --secret) envf="${a#*src=}";; esac
  prev="$a"; ctx="$a"
done
[ -n "$tools" ] || { echo "fake depot: not a run job"; exit 2; }
echo "Build: https://depot.dev/orgs/org1/projects/proj9/builds/run1"
cd "$ctx" && exec env -i PATH="$tools/bin:$PATH" HOME="$HOME" sh -c 'set -a; . "$1"; set +a; exec bash -e "$2"' x "$envf" "$tools/job.sh"
"""
_fake_dir = Path(__import__("tempfile").mkdtemp(prefix="fake-depot-"))
(_fake_dir / "depot").write_text(FAKE_DEPOT_RUN)
(_fake_dir / "depot").chmod(0o755)
os.environ.setdefault("FILEAPI_DEPOT_CLI", str(_fake_dir / "depot"))
DEPOT_SECRETS = {"DEPOT_TOKEN": "depot-test-token", "DEPOT_PROJECT_ID": "proj9"}


def depot_secrets(put) -> None:
    """Give the signed-in owner the Depot user secrets run jobs need; put is client.put or httpx.put."""
    for name, value in DEPOT_SECRETS.items():
        r = put(f"/api/forge/user/secrets/{name}", json={"value": value}, headers=AUTH)
        assert r.status_code in (200, 201), r.text


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
            c.execute("DROP TABLE IF EXISTS fileapi_files, fileapi_kv, fileapi_git_objects, fileapi_git_refs, fileapi_lfs_chunks, fileapi_lfs_objects, fileapi_release_chunks, fileapi_releases, fileapi_git_repos, fileapi_git_aliases")
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
    remote = server_url.replace("://", f"://x:{token}@") + "/git/nandi.uk/blog.git"

    async with connect(server_url, token) as c:
        r = await c.call_tool("create_repo", {"name": "blog", "description": "My blog"})
        assert not r.is_error and f"{server_url}/git/nandi.uk/blog.git" in text(r)
        assert (await c.call_tool("create_repo", {"name": "blog"})).is_error
        assert (await c.call_tool("create_repo", {"name": "../x"})).is_error
        assert "nandi.uk" in text(await c.call_tool("whoami", {}))
        assert "empty" in text(await c.call_tool("list_branches", {"repo": "blog"}))
        r = await c.call_tool("commit_files", {"repo": "blog", "message": "init",
                                               "files": {"README.md": "# Blog\n", "posts/one.md": "one\n"}})
        assert not r.is_error, text(r)
        assert text(await c.call_tool("list_repos", {})).startswith("blog\tprivate")
        # The full '<owner>/<name>' still works as a repo argument.
        assert "empty" not in text(await c.call_tool("list_branches", {"repo": "nandi.uk/blog"}))

    # Clone, commit and push with the real git CLI.
    work = tmp_path / "work"
    git("clone", remote, str(work))
    assert (work / "posts" / "one.md").read_text() == "one\n"
    (work / "posts" / "two.md").write_text("two\n")
    git("add", ".", cwd=work)
    git("commit", "-m", "second post", cwd=work)
    git("push", "origin", "main", "main:draft", cwd=work)

    async with connect(server_url, token) as c:
        listing = text(await c.call_tool("browse_repo", {"repo": "blog", "path": "posts"}))
        assert "one.md" in listing and "two.md" in listing
        assert text(await c.call_tool("browse_repo", {"repo": "blog", "path": "posts/two.md"})) == "two\n"
        log = text(await c.call_tool("repo_log", {"repo": "blog"}))
        assert log.index("second post") < log.index("init")
        assert "branch draft" in text(await c.call_tool("list_branches", {"repo": "blog"}))
        r = await c.call_tool("commit_files", {"repo": "blog", "message": "drop one",
                                               "files": {"posts/one.md": None}})
        assert not r.is_error, text(r)

    git("pull", "origin", "main", cwd=work)
    assert not (work / "posts" / "one.md").exists()
    assert git("fsck", cwd=work).returncode == 0

    # Bad token: git gets a Basic-auth challenge and fails; a Bearer header works too.
    bad = server_url.replace("://", "://x:nope@") + "/git/nandi.uk/blog.git"
    assert git("ls-remote", bad, check=False).returncode != 0
    r = git("-c", f"http.extraHeader=Authorization: Bearer {token}", "ls-remote", f"{server_url}/git/nandi.uk/blog.git")
    assert "refs/heads/draft" in r.stdout

    # Public repos clone anonymously; pushing still needs the token.
    anon = f"{server_url}/git/nandi.uk/blog.git"
    assert git("ls-remote", anon, check=False).returncode != 0
    async with connect(server_url, token) as c:
        r = await c.call_tool("set_repo_visibility", {"repo": "blog", "visibility": "public"})
        assert not r.is_error, text(r)
        assert "blog\tpublic" in text(await c.call_tool("list_repos", {}))
    git("clone", anon, str(tmp_path / "anon"))
    assert (tmp_path / "anon" / "posts" / "two.md").exists()
    assert git("push", anon, "main:anon", cwd=tmp_path / "anon", check=False).returncode != 0
    async with connect(server_url, token) as c:
        assert not (await c.call_tool("set_repo_visibility", {"repo": "blog", "visibility": "private"})).is_error
    assert git("ls-remote", anon, check=False).returncode != 0

    async with connect(server_url, token) as c:
        assert not (await c.call_tool("delete_repo", {"repo": "blog"})).is_error
    assert git("ls-remote", remote, check=False).returncode != 0


@pytest.mark.anyio
async def test_git_big_push_in_one_go(server_url, tmp_path):
    """A push far bigger than the spool and insert batches goes through in one request and clones back intact."""
    async with connect(server_url, TOKEN) as c:
        assert not (await c.call_tool("create_repo", {"name": "big"})).is_error
    remote = server_url.replace("://", f"://x:{TOKEN}@") + "/git/nandi.uk/big.git"
    work = tmp_path / "work"
    git("init", "-q", "-b", "main", str(work))
    for i in range(3):  # incompressible blobs, like the committed .wasm.gz files that broke the mirror
        (work / f"blob{i}.bin").write_bytes(os.urandom(12 * 1024 * 1024))
        (work / "log.txt").write_text(f"commit {i}\n")
        git("add", ".", cwd=work)
        git("commit", "-q", "-m", f"c{i}", cwd=work)
    git("push", remote, "main", cwd=work)
    git("clone", "-q", remote, str(tmp_path / "back"))
    for i in range(3):
        assert (tmp_path / "back" / f"blob{i}.bin").read_bytes() == (work / f"blob{i}.bin").read_bytes()
    assert git("fsck", cwd=tmp_path / "back").returncode == 0


@pytest.mark.anyio
async def test_git_stream_keeps_slow_pushes_alive():
    """While a push is stored, side-band clients get progress lines, so proxies don't time the request out."""
    import anyio
    from fileapi.forge import GitStream

    class SlowForge:
        def serve(self, repo, service, recv, write, progress=None):
            got = b""
            while chunk := recv(7):
                got += chunk
            progress(42)
            time.sleep(0.3)
            write(b"0009\x01done" + b"0000")
            assert got.endswith(b"PACK...")

    cmd = b"0" * 40 + b" " + b"1" * 40 + b" refs/heads/main\0report-status side-band-64k"
    body = f"{len(cmd) + 4:04x}".encode() + cmd + b"0000PACK..."
    for sideband in (True, False):
        stream = GitStream(SlowForge(), "o/r", "git-receive-pack")
        stream.KEEPALIVE = 0.05
        payload = body if sideband else body.replace(b" side-band-64k", b"")
        for i in range(0, len(payload), 5):
            assert await stream.feed(payload[i:i + 5])
        await stream.feed(None)
        out = b"".join([part async for part in stream.response()])
        assert out.endswith(b"0009\x01done0000")
        assert (b"\x02fileapi: storing objects: 42" in out) is sideband
        await anyio.sleep(0)


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
        log = client.get("/api/forge/repos/web/site/commits").json()["commits"]
        assert client.get("/api/forge/repos/web/site/log").json()["commits"] == log  # old name still works
        assert [c["message"] for c in log] == ["second", "first"]
        f = client.get("/api/forge/repos/web/site/tree", params={"path": "src/a.py", "ref": log[1]["sha"]}).json()
        assert f["content"] == "x = 1\n"
        d = client.get(f"/api/forge/repos/web/site/commits/{log[0]['sha'][:12]}").json()
        assert d["sha"] == log[0]["sha"] and d["message"] == "second" and d["parents"] == [log[1]["sha"]]
        assert d["stats"] == {"files": 1, "additions": 1, "deletions": 1} and d["truncated"] is False
        [fd] = d["files"]
        assert (fd["path"], fd["status"], fd["old_path"]) == ("src/a.py", "modified", None)
        assert fd["hunks"] == [{"old_start": 1, "old_lines": 1, "new_start": 1, "new_lines": 1,
                                "lines": ["-x = 1", "+x = 2"]}]
        root = client.get(f"/api/forge/repos/web/site/commits/{log[1]['sha']}").json()
        assert [(f["path"], f["status"], f["additions"]) for f in root["files"]] == [
            ("README.md", "added", 1), ("src/a.py", "added", 1)]
        assert root["files"][0]["hunks"][0]["old_start"] == 0
        assert client.get("/api/forge/repos/web/site/commits/abcdef1").status_code == 404
        raw = client.get("/api/forge/repos/web/site/raw", params={"path": "README.md"})
        assert raw.status_code == 200 and raw.content == b"hi\n" and raw.headers["content-type"].startswith("text/markdown")
        assert "sandbox" in raw.headers["content-security-policy"] and raw.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/forge/repos/web/site/raw", params={"path": "src"}).status_code == 404
        md = client.get("/api/forge/repos/web/site/markdown", params={"path": "README.md"}).json()
        assert md["html"] == "<p>hi</p>\n" and md["path"] == "README.md"
        # No code navigation for Markdown, but asking isn't an error.
        assert client.get("/api/forge/repos/web/site/symbols", params={"path": "README.md"}).status_code == 200
        assert client.get("/api/forge/repos/web/secret/raw", params={"path": "README.md"}).status_code == 404
        assert client.get("/api/forge/repos/web/secret").status_code == 404
        # The UI's "not found" page relies on a private repo looking exactly like a missing one, even
        # with a stale/bad token: same status, same message shape.
        for headers in ({}, {"Authorization": "Bearer not-a-real-token"}):
            hidden = client.get("/api/forge/repos/web/secret", headers=headers)
            missing = client.get("/api/forge/repos/web/nope", headers=headers)
            assert hidden.status_code == missing.status_code == 404
            assert hidden.json()["detail"].replace("secret", "X") == missing.json()["detail"].replace("nope", "X")

        # Signed in: private repos too.
        assert client.get("/api/forge/me", headers=AUTH).json()["signed_in"] is True
        assert len(client.get("/api/forge/repos", headers=AUTH).json()["repos"]) == 2
        assert client.get("/api/forge/repos/web/secret", headers=AUTH).json()["branches"] == {}
        assert client.get("/api/forge/repos/web/secret/tree", headers=AUTH).status_code == 404  # empty repo

        # Settings: admin only.
        url = "/api/forge/repos/web/site"
        assert client.patch(url, json={"public": False}).status_code == 401
        assert client.delete(url).status_code == 401
        forge.commit_files("web/site", "dev", {"x": "y\n"}, "dev")
        r = client.patch(url, headers=AUTH, json={"public": False, "description": " New ", "default_branch": "dev"})
        assert r.status_code == 200, r.text
        assert (r.json()["public"], r.json()["description"], r.json()["default"]) == (False, "New", "dev")
        assert client.patch(url, headers=AUTH, json={"default_branch": "nope"}).status_code == 404
        assert client.patch(url, headers=AUTH, json={"name": "secret"}).status_code == 409
        r = client.patch(url, headers=AUTH, json={"name": "blog"})
        assert r.json()["repo"] == "web/blog" and r.json()["default"] == "dev" and r.json()["description"] == "New"
        # The old name is now an alias: it still answers, as the renamed repo.
        assert client.get(url, headers=AUTH).json()["repo"] == "web/blog"
        assert client.get("/git/web/site.git/info/refs?service=git-upload-pack", headers=AUTH).status_code == 200
        # Repos can't move to another owner.
        assert client.patch("/api/forge/repos/web/blog", headers=AUTH, json={"name": "docs/site"}).status_code == 400
        r = client.patch("/api/forge/repos/web/blog", headers=AUTH, json={"name": "web/site2", "default_branch": "main"})
        assert r.json()["repo"] == "web/site2" and r.json()["default"] == "main"
        assert client.get(url, headers=AUTH).json()["repo"] == "web/site2"  # aliases follow later renames
        assert len(client.get("/api/forge/repos/web/site2/commits", headers=AUTH).json()["commits"]) == 2
        assert client.patch("/api/forge/repos/web/secret", headers=AUTH, json={"default_branch": "trunk"}).json()["default"] == "trunk"
        assert client.delete("/api/forge/repos/web/blog", headers=AUTH).status_code == 204  # via the alias
        assert client.get("/api/forge/repos/web/site2", headers=AUTH).status_code == 404
        assert client.get(url, headers=AUTH).status_code == 404  # its aliases went with it

        # Creating needs only a name: the repo lands under the signed-in handle.
        assert client.get("/api/forge/me", headers=AUTH).json()["owner"] == "nandi.uk"
        assert client.post("/api/forge/repos", json={"name": "new"}).status_code == 401
        r = client.post("/api/forge/repos", headers=AUTH, json={"name": "new", "public": True})
        assert r.status_code == 201 and r.json()["repo"] == "nandi.uk/new" and r.json()["public"] is True
        assert r.json()["clone_url"].endswith("/git/nandi.uk/new.git")
        assert client.post("/api/forge/repos", headers=AUTH, json={"name": "new"}).status_code == 409
        assert client.post("/api/forge/repos", headers=AUTH, json={"name": "web/x"}).status_code == 400

        # The committed Qt for WebAssembly build is served from /forge/.
        r = client.get("/forge", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/forge/"
        page = client.get("/forge/")
        assert page.status_code == 200 and "qtLoad" in page.text and "fileapiToken" in page.text
        deep = client.get("/forge/nope/deep")
        assert deep.status_code == 200 and "qtLoad" in deep.text and '"/forge/forgeui.js"' in deep.text
        wasm = client.get("/forge/forgeui.wasm", headers={"Accept-Encoding": "gzip"})
        assert wasm.status_code == 200 and wasm.headers["content-type"] == "application/wasm"
        assert wasm.content[:4] == b"\0asm"  # httpx undoes the gzip
        assert int(wasm.headers["x-uncompressed-length"]) == len(wasm.content)
        assert client.get("/forge/qtloader.js").headers["content-type"].startswith("text/javascript")

        # Favicon and app icons live at the root, and both pages link them.
        assert '/favicon.svg' in page.text and '/apple-touch-icon.png' in page.text
        assert '/favicon.svg' in client.get("/").text
        ico = client.get("/favicon.ico")
        assert ico.status_code == 200 and ico.headers["content-type"] == "image/x-icon" and ico.content[:4] == b"\0\0\1\0"
        assert client.get("/favicon.svg").headers["content-type"].startswith("image/svg+xml")
        for name in ("apple-touch-icon.png", "apple-touch-icon-precomposed.png", "icon-192.png", "icon-512.png"):
            r = client.get(f"/{name}")
            assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n", name
        assert client.get("/site.webmanifest").json()["start_url"] == "/forge/"


def _og(text: str) -> dict:
    import html, re
    return {m[0]: html.unescape(m[1]) for m in re.findall(r'<meta (?:property|name)="((?:og|twitter):[^"]+)" content="([^"]*)"', text)}


def test_forge_link_previews(tmp_path):
    """Forge pages carry Open Graph / Twitter tags for the route they were opened at, as an anonymous
    visitor sees it: private repos look exactly like missing ones."""
    with TestClient(app_for(tmp_path)) as client:
        forge, releases = client.app.state.forge, client.app.state.releases
        forge.create("web", "site", "The site & <friends>")
        forge.commit_files("web/site", "main", {"README.md": "# Hello\n\nworld\n", "src/a.py": "x = 1\n"}, "first")
        c2 = forge.commit_files("web/site", "main", {"src/a.py": "x = 2\ny = 3\n"}, "Tune a\n\nBecause x was too small.")
        forge.set_public("web/site", True)
        releases.create("web/site", "v1.0", name="First one", notes="## New\n- **fast** [docs](https://x.y)")
        forge.create("web", "secret", "Top secret plans")
        forge.commit_files("web/secret", "main", {"plan.txt": "world domination\n"}, "the plan")

        generic = _og(client.get("/forge/").text)
        assert generic["og:title"] == "fileapi forge" and generic["twitter:card"] == "summary_large_image"
        assert generic["og:image"].startswith("http://testserver/api/forge/card.png?")

        repo = _og(client.get("/forge/web/site").text)
        assert repo["og:title"] == "web/site" and repo["og:description"] == "The site & <friends>"
        assert repo["og:url"] == "http://testserver/forge/web/site"
        assert "&lt;friends&gt;" in client.get("/forge/web/site").text  # escaped in the HTML

        sha = c2["commit"] if isinstance(c2, dict) and "commit" in c2 else forge.log("web/site")[0]["sha"]
        commit = _og(client.get(f"/forge/web/site/-/commits/{sha[:12]}").text)
        assert commit["og:title"] == "Tune a"
        assert "1 file changed, +2 −1" in commit["og:description"] and "Because x was too small." in commit["og:description"]

        rel = _og(client.get("/forge/web/site/-/releases/v1.0").text)
        assert rel["og:title"] == "First one" and rel["og:description"] == "New fast docs"

        f = _og(client.get("/forge/web/site/src/a.py", params={"ref": "main"}).text)
        assert f["og:title"] == "a.py · web/site" and "x = 2 y = 3" in f["og:description"] and "at main" in f["og:description"]
        assert f["og:url"] == "http://testserver/forge/web/site/src/a.py?ref=main"
        d = _og(client.get("/forge/web/site/src").text)
        assert d["og:title"] == "src/ · web/site"
        # A path that isn't there (any more) falls back to the repo's card.
        assert _og(client.get("/forge/web/site/gone.txt").text)["og:title"] == "web/site"

        # Private: nothing about the repo leaks, at any route, not even to a signed-in browser.
        for route in ("web/secret", "web/secret/plan.txt", "web/secret/-/commits", "web/nope"):
            for headers in ({}, AUTH):
                page = client.get(f"/forge/{route}", headers=headers).text
                tags = _og(page)
                assert tags["og:title"] == "fileapi forge" and tags["og:description"] == generic["og:description"]
                assert "secret plans" not in page.lower() and "plan.txt" not in page.split("<script")[0].replace(route, "")

        # The card image.
        img = client.get(repo["og:image"].removeprefix("http://testserver"))
        assert img.status_code == 200 and img.headers["content-type"] == "image/png"
        assert img.content[:8] == b"\x89PNG\r\n\x1a\n"
        from PIL import Image
        import io
        assert Image.open(io.BytesIO(img.content)).size == (1200, 630)
        hidden = client.get("/api/forge/card.png", params={"r": "web/secret"}).content
        assert hidden == client.get("/api/forge/card.png", params={"r": "web/nope"}).content
        assert hidden == client.get("/api/forge/card.png").content


def test_repos_move_under_the_owner_and_keep_old_urls(tmp_path, backend):
    from fileapi.forge import RepoPasses

    db = backend.get("DATABASE_URL", "")
    with TestClient(app_for(tmp_path, database_url=db)) as client:
        forge = client.app.state.forge
        for owner, name in [("smelly", "shelly-qt"), ("web", "site"), ("other", "site"), ("nandi.uk", "mine")]:
            forge.create(owner, name)
        forge.commit_files("smelly/shelly-qt", "main", {"a": "1\n"}, "one")
        forge.set_public("web/site", True)
    # Next start: everything moves under the admin's handle; a clash gets the old owner as a prefix
    # (owners are taken in alphabetical order, so other/site keeps "site").
    with TestClient(app_for(tmp_path, database_url=db)) as client:
        forge = client.app.state.forge
        assert {r.full_name for r in forge.list()} == {
            "nandi.uk/shelly-qt", "nandi.uk/site", "nandi.uk/web-site", "nandi.uk/mine"}
        assert forge.canonical("smelly/shelly-qt") == "nandi.uk/shelly-qt"
        assert forge.canonical("web/site") == "nandi.uk/web-site"
        assert forge.info("web/site").public is True
        # Old clone URLs and UI links keep working.
        r = client.get("/git/smelly/shelly-qt.git/info/refs?service=git-upload-pack", headers=AUTH)
        assert r.status_code == 200 and b"refs/heads/main" in r.content
        assert client.get("/git/web/site.git/info/refs?service=git-upload-pack").status_code == 200  # public, anon
        assert client.get("/api/forge/repos/smelly/shelly-qt", headers=AUTH).json()["repo"] == "nandi.uk/shelly-qt"
        # Short-lived passes are issued for the current name and accepted on the old URL too.
        p, _ = RepoPasses(client.app.state.auth.kv).issue("nandi.uk/shelly-qt", 5)
        assert client.get("/git/smelly/shelly-qt.git/info/refs?service=git-upload-pack", auth=("x", p)).status_code == 200
        # A new repo with an alias's name takes the name over.
        forge.create("smelly", "shelly-qt")
        assert forge.canonical("smelly/shelly-qt") == "smelly/shelly-qt"
    # Idempotent: a third start moves only what is new.
    with TestClient(app_for(tmp_path, database_url=db)) as client:
        names = {r.full_name for r in client.app.state.forge.list()}
        assert "nandi.uk/smelly-shelly-qt" in names and len(names) == 5


# ---- git credential helper ----------------------------------------------------

FAKE_BROWSER = r'''
import re, sys, httpx
url = sys.argv[1]
with httpx.Client(follow_redirects=True, timeout=10) as c:
    page = c.get(url)
    rid = re.search(r'name=rid value="([^"]+)"', page.text).group(1)
    base = url.split("/authorize")[0]
    # Follows fileapi -> fake atproto AS -> fileapi callback -> the helper's 127.0.0.1 listener.
    c.post(base + "/authorize", data={"rid": rid}, headers={"origin": base})
'''


def test_git_credential_helper_signs_in_in_browser(tmp_path):
    """Plain `git clone` of a private repo: git asks the helper, the helper runs the browser sign-in over a
    loopback redirect, stores the tokens, and later git commands reuse or refresh them without a browser."""
    import json
    import threading

    import uvicorn
    from fastapi.responses import RedirectResponse

    from fileapi import git_credential

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    fake = FakeAtproto()
    fake.server.authorization_endpoint = f"{url}/fake-as/authorize"
    app = app_for(tmp_path, preset_token="", public_url=url, atproto=fake, admin_did=ADMIN)

    def fake_as():  # the admin approves at their atproto authorization server
        state = fake.pars[-1]["state"]
        return RedirectResponse(f"{url}/oauth/atproto/callback?code=atcode&state={state}&iss=https://as.example")

    app.add_api_route("/fake-as/authorize", fake_as)
    app.router.routes.insert(0, app.router.routes.pop())  # ahead of the catch-all MCP mount

    app.state.forge.create("web", "site")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{url}/health", timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    try:
        # The helper is served as a standalone file; install it from the server like a user would.
        r = httpx.get(f"{url}/git-credential-fileapi")
        assert r.status_code == 200 and "def main" in r.text
        bindir = tmp_path / "bin"
        bindir.mkdir()
        helper = bindir / "git-credential-fileapi"
        helper.write_text(r.text.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        helper.chmod(0o755)
        browser = tmp_path / "browser.py"
        browser.write_text(FAKE_BROWSER)
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text("[credential]\n\thelper = store --file /nonexistent/should-not-be-used\n")
        env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_GLOBAL": str(gitconfig), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
               "BROWSER": f"{sys.executable} {browser} %s"}

        def run(*args):
            return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)

        assert run("git-credential-fileapi", "setup", url).returncode == 0
        helpers = run("git", "config", "--get-all", f"credential.{url}.helper").stdout.splitlines()
        assert helpers == ["", "fileapi"]

        r = run("git", "clone", f"{url}/git/web/site.git", str(tmp_path / "site"))
        assert r.returncode == 0, r.stderr
        assert "Opening your browser" in r.stderr and "signed in" in r.stderr
        tokens = json.loads((tmp_path / "xdg" / "git-credential-fileapi" / "tokens.json").read_text())
        entry = tokens[url]
        assert entry["access_token"].startswith("fa_") and entry["refresh_token"].startswith("fr_")
        assert app.state.auth.identify(entry["access_token"]) == ADMIN

        # Second command: the stored token is reused, no browser.
        r = run("git", "ls-remote", f"{url}/git/web/site.git")
        assert r.returncode == 0 and "Opening" not in r.stderr, r.stderr

        # Expired access token: refreshed silently with the refresh token.
        entry["expires_at"] = 0
        (tmp_path / "xdg" / "git-credential-fileapi" / "tokens.json").write_text(json.dumps(tokens))
        r = run("git", "ls-remote", f"{url}/git/web/site.git")
        assert r.returncode == 0 and "Opening" not in r.stderr, r.stderr
        fresh = json.loads((tmp_path / "xdg" / "git-credential-fileapi" / "tokens.json").read_text())[url]
        assert fresh["access_token"] != entry["access_token"] and fresh["refresh_token"] != entry["refresh_token"]

        # Server forgot the token (revoked): git erases it, and the next command signs in again.
        app.state.auth.revoke(fresh["refresh_token"])
        assert run("git", "ls-remote", f"{url}/git/web/site.git").returncode != 0
        r = run("git", "ls-remote", f"{url}/git/web/site.git")
        assert r.returncode == 0 and "Opening your browser" in r.stderr, r.stderr

        assert "refresh token present" in run("git-credential-fileapi", "status").stdout
        assert git_credential._base("fileapi.example/x/y") == "https://fileapi.example"
    finally:
        server.should_exit = True


def test_forge_code_navigation(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("web", "code")
        forge.commit_files("web/code", "main", {
            "pkg/util.py": 'def helper(x: int) -> int:\n    """Doubles x.\n\n    Really."""\n    return x * 2\n\n\nclass Box:\n    def open(self):\n        return helper(1)\n',
            "pkg/main.py": "from .util import helper, Box\n\nprint(helper(3))  # helper in a comment\ns = 'helper'\n",
            "lib/helper.c": "// C has its own helper\nint helper(int x) { return x; }\n",
            "web/app.ts": "// Adds one.\nexport function inc(n: number): number { return n + 1 }\nconst y = inc(2)\n",
        }, "code")
        forge.set_public("web/code", True)
        base = "/api/forge/repos/web/code"

        out = client.get(f"{base}/symbols", params={"path": "pkg/util.py"}).json()
        assert out["supported"] and out["language"] == "Python"
        assert [(s["name"], s["kind"], s["line"]) for s in out["symbols"]] == [
            ("helper", "function", 1), ("Box", "class", 8), ("open", "method", 9)]
        assert out["symbols"][0]["signature"] == "def helper(x: int) -> int"
        assert out["symbols"][0]["doc"] == "Doubles x.\n\nReally."
        assert out["symbols"][2]["container"] == "Box"

        # helper(3) in main.py: defined in util.py (not in the C file); used in both Python files.
        r = client.get(f"{base}/symbol", params={"path": "pkg/main.py", "line": 3, "col": 7}).json()
        assert r["symbol"] == {"name": "helper", "line": 3, "col": 6, "end_col": 12}
        assert [(d["path"], d["line"]) for d in r["definitions"]] == [("pkg/util.py", 1)]
        refs = [(x["path"], x["line"]) for x in r["references"]]
        assert refs == [("pkg/main.py", 1), ("pkg/main.py", 3), ("pkg/util.py", 1), ("pkg/util.py", 10)]
        assert [x["definition"] for x in r["references"]] == [False, False, True, False]
        # Comments and strings aren't identifiers.
        assert client.get(f"{base}/symbol", params={"path": "pkg/main.py", "line": 3, "col": 25}).json()["symbol"] is None
        assert client.get(f"{base}/symbol", params={"path": "pkg/main.py", "line": 4, "col": 7}).json()["symbol"] is None
        # On the definition itself: that definition; hover can skip references.
        r = client.get(f"{base}/symbol", params={"path": "pkg/util.py", "line": 1, "col": 4, "references": False}).json()
        assert [(d["path"], d["line"]) for d in r["definitions"]] == [("pkg/util.py", 1)] and r["references"] == []

        r = client.get(f"{base}/symbol", params={"path": "web/app.ts", "line": 3, "col": 10}).json()
        assert r["definitions"][0]["line"] == 2 and r["definitions"][0]["doc"] == "Adds one."
        assert r["definitions"][0]["signature"] == "function inc(n: number): number"
        r = client.get(f"{base}/symbol", params={"path": "lib/helper.c", "line": 2, "col": 5}).json()
        assert [d["path"] for d in r["definitions"]] == ["lib/helper.c"]
        assert r["definitions"][0]["signature"] == "int helper(int x)" and r["definitions"][0]["doc"] == "C has its own helper"

        # Parameters and local variables resolve inside their function only.
        r = client.get(f"{base}/symbol", params={"path": "pkg/util.py", "line": 5, "col": 11}).json()
        assert r["local"] and [(d["line"], d["kind"]) for d in r["definitions"]] == [(1, "parameter")]
        assert [(x["line"], x["col"]) for x in r["references"]] == [(1, 11), (5, 11)]
        r = client.get(f"{base}/symbol", params={"path": "web/app.ts", "line": 2, "col": 48}).json()
        assert [(d["line"], d["kind"]) for d in r["definitions"]] == [(2, "parameter")]

        assert client.get(f"{base}/symbols", params={"path": "nope.py"}).status_code == 404
        assert client.get(f"{base}/symbols", params={"path": "pkg"}).status_code == 404
        forge.create("web", "hidden")
        assert client.get("/api/forge/repos/web/hidden/symbols", params={"path": "a.py"}).status_code == 404


# ---- Git LFS -------------------------------------------------------------------


def test_lfs_batch_api(tmp_path, backend):
    import hashlib

    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("web", "site")
        data = os.urandom(5000)
        oid = hashlib.sha256(data).hexdigest()
        batch = "/git/web/site.git/info/lfs/objects/batch"
        hdr = {"Accept": "application/vnd.git-lfs+json", "Content-Type": "application/vnd.git-lfs+json"}
        up = {"operation": "upload", "transfers": ["basic"], "objects": [{"oid": oid, "size": len(data)}]}

        r = client.post(batch, json=up, headers=hdr)
        assert r.status_code == 401 and r.headers["lfs-authenticate"].startswith("Basic")
        r = client.post(batch, json=up, headers=hdr, auth=("x", TOKEN))
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.git-lfs+json")
        action = r.json()["objects"][0]["actions"]["upload"]
        assert action["href"].endswith(f"/git/web/site.git/info/lfs/objects/{oid}")
        assert action["header"]["Authorization"].startswith("Basic ")

        href = f"/git/web/site.git/info/lfs/objects/{oid}"
        assert client.put(href, content=data).status_code == 401
        assert client.put(href, content=data[:-1], headers=AUTH).status_code == 422  # wrong content
        assert client.put(href, content=data, headers=AUTH).status_code == 200
        assert client.put(href, content=data, headers=AUTH).status_code == 200  # idempotent
        # Already stored: nothing to upload.
        assert "actions" not in client.post(batch, json=up, headers={**hdr, **AUTH}).json()["objects"][0]

        down = {"operation": "download", "objects": [{"oid": oid, "size": len(data)}, {"oid": "0" * 64, "size": 1},
                                                      {"oid": "nope", "size": 1}]}
        objs = client.post(batch, json=down, headers={**hdr, **AUTH}).json()["objects"]
        assert "download" in objs[0]["actions"] and objs[1]["error"]["code"] == 404 and objs[2]["error"]["code"] == 422
        r = client.get(href, headers=AUTH)
        assert r.status_code == 200 and r.content == data
        assert client.get(href).status_code == 401
        assert client.get(f"/git/web/site.git/info/lfs/objects/{'0' * 64}", headers=AUTH).status_code == 404
        assert client.get("/git/web/nope.git/info/lfs/objects/" + oid, headers=AUTH).status_code == 404
        assert client.post("/git/web/site.git/info/lfs/locks/verify", json={}, headers=AUTH).json() == {"ours": [], "theirs": []}

        # Public repos: anonymous download, but still no anonymous upload.
        forge.set_public("web/site", True)
        assert client.get(href).content == data
        assert client.post(batch, json=up, headers=hdr).status_code == 401

        # The pointer shows up as an LFS file when browsing; LFS objects go away with the repo.
        pointer = f"version https://git-lfs.github.com/spec/v1\noid sha256:{oid}\nsize {len(data)}\n"
        forge.commit_files("web/site", "main", {"big.bin": pointer}, "big")
        f = client.get("/api/forge/repos/web/site/tree", params={"path": "big.bin"}).json()
        assert f["lfs"] == {"oid": oid, "size": len(data)} and f["size"] == len(data) and f["content"] is None
        forge.rename("web/site", "web/renamed")
        assert client.get(f"/git/web/renamed.git/info/lfs/objects/{oid}").content == data
        forge.delete("web/renamed")
        forge.create("web", "renamed")
        assert client.app.state.forge.is_public("web/renamed") is False
        assert client.get(f"/git/web/renamed.git/info/lfs/objects/{oid}", headers=AUTH).status_code == 404


@pytest.mark.skipif(subprocess.run(["git", "lfs", "version"], capture_output=True).returncode != 0,
                    reason="git-lfs not installed")
def test_helper_moves_big_files_to_lfs(server_url, tmp_path):
    """After `setup`, a clone of a fileapi repo commits files over the threshold as LFS pointers, and
    git-lfs pushes and fetches their content through the server, authenticated by the helper."""
    import json

    r = httpx.post(f"{server_url}/api/forge/repos", json={"name": "media"}, headers=AUTH)
    assert r.status_code == 201, r.text
    bindir = tmp_path / "bin"
    bindir.mkdir()
    helper = bindir / "git-credential-fileapi"
    src = (Path(__file__).parent.parent / "fileapi" / "git_credential.py").read_text()
    helper.write_text(src.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
    helper.chmod(0o755)
    xdg = tmp_path / "xdg"
    (xdg / "git-credential-fileapi").mkdir(parents=True)
    # A signed-in helper: the preset server token stands in for the browser sign-in.
    (xdg / "git-credential-fileapi" / "tokens.json").write_text(json.dumps(
        {server_url: {"access_token": TOKEN, "expires_at": int(time.time()) + 86400}}))
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = T\n\temail = t@x\n")
    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_GLOBAL": str(gitconfig), "XDG_CONFIG_HOME": str(xdg), "HOME": str(tmp_path)}

    def run(*args, cwd=None):
        r = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, f"{args}: {r.stdout}\n{r.stderr}"
        return r

    run("git-credential-fileapi", "setup", server_url)
    assert "filter.lfs.clean" in run("git", "config", "--global", "--list").stdout
    work = tmp_path / "media"
    run("git", "clone", f"{server_url}/git/nandi.uk/media.git", str(work))
    assert "git-credential-fileapi" in (work / ".git" / "hooks" / "pre-commit").read_text()
    run("git", "config", "fileapi.lfsThreshold", "4k", cwd=work)

    big = os.urandom(10_000)
    (work / "notes.txt").write_text("small\n")
    (work / "assets").mkdir()
    (work / "assets" / "big file.bin").write_bytes(big)
    run("git", "add", ".", cwd=work)
    r = run("git", "commit", "-m", "add media", cwd=work)
    assert "stored with Git LFS" in r.stderr and "assets/big file.bin" in r.stderr
    attrs = (work / ".gitattributes").read_text()
    assert "filter=lfs" in attrs and "notes.txt" not in attrs
    blob = run("git", "cat-file", "-p", "HEAD:assets/big file.bin", cwd=work).stdout
    assert blob.startswith("version https://git-lfs.github.com/spec/v1")
    assert run("git", "cat-file", "-p", "HEAD:notes.txt", cwd=work).stdout == "small\n"
    assert run("git", "status", "--porcelain", cwd=work).stdout == ""
    run("git", "push", "origin", "HEAD:main", cwd=work)

    # A fresh clone gets the real bytes back from the LFS store.
    again = tmp_path / "again"
    run("git", "clone", f"{server_url}/git/nandi.uk/media.git", str(again))
    assert (again / "assets" / "big file.bin").read_bytes() == big

    # Under the threshold, or turned off: normal blobs.
    (work / "mid.bin").write_bytes(os.urandom(3000))
    run("git", "add", "mid.bin", cwd=work)
    assert "LFS" not in run("git", "commit", "-m", "mid", cwd=work).stderr
    run("git", "config", "fileapi.autoLfs", "false", cwd=work)
    (work / "off.bin").write_bytes(os.urandom(10_000))
    run("git", "add", "off.bin", cwd=work)
    run("git", "commit", "-m", "off", cwd=work)
    assert run("git", "cat-file", "-s", "HEAD:off.bin", cwd=work).stdout.strip() == "10000"
    assert "off.bin" not in (work / ".gitattributes").read_text()

    # Repos without a fileapi remote are left alone.
    other = tmp_path / "other"
    run("git", "init", "-q", str(other))
    (other / "big.bin").write_bytes(big)
    run("git", "add", ".", cwd=other)
    run("git", "-c", "fileapi.lfsThreshold=4k", "commit", "-m", "x", cwd=other)
    assert not (other / ".gitattributes").exists()


JOBS_TOML = """
[[job]]
name = "build"
run = '''
echo "deploying $FORGE_REPO $FORGE_BRANCH $FORGE_SHA"
echo "token=$DEPLOY_TOKEN"
test -f app.py && cat app.py
test -z "$DATABASE_URL" && test -z "$FILEAPI_SECRETS_KEY" && echo "env is clean"
'''

[[job]]
name = "hook"
post = "https://deploy.example/hooks/${HOOK_ID}"
headers = { Authorization = "Bearer ${DEPLOY_TOKEN}" }
body = '{"sha": "${FORGE_SHA}"}'

[[job]]
name = "other-branch"
branches = ["release/*"]
run = "exit 3"
"""


def test_repo_secrets_and_deploy_jobs(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge, jobs = client.app.state.forge, client.app.state.jobs
        sent = []

        def hook(request):
            sent.append(request)
            return httpx.Response(200, text="queued deploy for " + request.headers["authorization"])

        jobs.http = httpx.Client(transport=httpx.MockTransport(hook))
        forge.create("nandi.uk", "app")
        base = "/api/forge/repos/nandi.uk/app"

        # Admin only, even for listing names.
        assert client.get(f"{base}/secrets").status_code == 401
        assert client.put(f"{base}/secrets/X", json={"value": "v"}).status_code == 401
        assert client.get(f"{base}/runs").status_code == 401
        assert client.get("/api/forge/repos/nandi.uk/nope/secrets", headers=AUTH).status_code == 404

        r = client.put(f"{base}/secrets/DEPLOY_TOKEN", json={"value": "s3cr3t-value"}, headers=AUTH)
        assert r.status_code == 201 and r.json()["name"] == "DEPLOY_TOKEN" and "value" not in r.json()
        assert client.put(f"{base}/secrets/DEPLOY_TOKEN", json={"value": "tok-12345"}, headers=AUTH).status_code == 200
        assert client.put(f"{base}/secrets/HOOK_ID", json={"value": "srv-1"}, headers=AUTH).status_code == 201
        for bad in ("1X", "has-dash", "FORGE_SHA"):
            assert client.put(f"{base}/secrets/{bad}", json={"value": "v"}, headers=AUTH).status_code == 400
        listed = client.get(f"{base}/secrets", headers=AUTH).json()
        assert listed["enabled"] is True and [s["name"] for s in listed["secrets"]] == ["DEPLOY_TOKEN", "HOOK_ID"]
        assert "tok-12345" not in str(listed)
        # Encrypted at rest: the stored record doesn't contain the value.
        raw = jobs.kv.get("forge_secrets_" + "nandi.uk/app".encode().hex())
        assert raw and "tok-12345" not in str(raw)

        # No jobs file: pushes run nothing, a manual run says why.
        forge.commit_files("nandi.uk/app", "main", {"app.py": "print('hi')\n"}, "first")
        assert client.get(f"{base}/runs", headers=AUTH).json()["runs"] == []
        r = client.post(f"{base}/runs", json={}, headers=AUTH)
        assert r.status_code == 404 and ".forge/jobs.toml" in r.json()["detail"]

        # Committing the jobs file is a push: the main-branch jobs run. Run scripts only ever run on Depot:
        # without a Depot token the server refuses to run one itself.
        forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": JOBS_TOML}, "add jobs")
        jobs.wait()
        runs = {r["job"]: r for r in client.get(f"{base}/runs", headers=AUTH).json()["runs"]}
        assert runs["build"]["status"] == "error" and "run on Depot" in runs["build"]["error"]
        assert "DEPOT_TOKEN" in runs["build"]["error"] and runs["hook"]["status"] == "success"
        depot_secrets(client.put)
        sha = forge.commit_files("nandi.uk/app", "main", {"app.py": "print('hi')\n", "b.txt": "b"}, "again")["commit"]
        jobs.wait()
        runs = client.get(f"{base}/runs", headers=AUTH).json()["runs"][:2]
        assert sorted(r["job"] for r in runs) == ["build", "hook"]
        assert all(r["status"] == "success" and r["sha"] == sha and r["trigger"] == "push" for r in runs), [(r["job"], r["status"], r.get("error")) for r in runs]
        build = next(r for r in runs if r["job"] == "build")
        out = client.get(f"{base}/runs/{build['id']}", headers=AUTH).json()["output"]
        assert f"deploying nandi.uk/app main {sha}" in out and "print('hi')" in out and "env is clean" in out
        assert build["kind"] == "run" and "Running on Depot (project proj9)" in out
        assert build["depot"]["url"] == "https://depot.dev/orgs/org1/projects/proj9/builds/run1"
        assert "token=***" in out and "tok-12345" not in out
        hook_run = next(r for r in runs if r["job"] == "hook")
        out = client.get(f"{base}/runs/{hook_run['id']}", headers=AUTH).json()["output"]
        assert "HTTP 200" in out and "Bearer ***" in out and "tok-12345" not in out
        sent = sent[1:]  # the first push's hook
        assert str(sent[0].url) == "https://deploy.example/hooks/srv-1"
        assert sent[0].headers["authorization"] == "Bearer tok-12345"
        assert sent[0].content == f'{{"sha": "{sha}"}}'.encode()

        # Branch filters, a failing job, manual runs by name.
        forge.commit_files("nandi.uk/app", "release/1", {".forge/jobs.toml": JOBS_TOML}, "release")
        jobs.wait()
        runs = client.get(f"{base}/runs", headers=AUTH).json()["runs"]
        assert runs[0]["job"] == "other-branch" and runs[0]["status"] == "failed" and runs[0]["exit_code"] == 3
        r = client.post(f"{base}/runs", json={"job": "build"}, headers=AUTH)
        assert r.status_code == 202 and [x["job"] for x in r.json()["runs"]] == ["build"]
        jobs.wait()
        assert client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]["trigger"] == "manual"
        assert client.post(f"{base}/runs", json={"ref": "nope"}, headers=AUTH).status_code == 404

        # A bad jobs file fails the manual run with the parse error (and a push just skips it).
        forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": "[[job]]\nname='x'\n"}, "broken")
        r = client.post(f"{base}/runs", json={}, headers=AUTH)
        assert r.status_code == 400 and "exactly one of run" in r.json()["detail"]

        # Secrets and runs follow a rename, and go with a delete.
        assert client.patch(base, json={"name": "app2"}, headers=AUTH).status_code == 200
        base2 = "/api/forge/repos/nandi.uk/app2"
        names = [s["name"] for s in client.get(f"{base2}/secrets", headers=AUTH).json()["secrets"]]
        assert names == ["DEPLOY_TOKEN", "HOOK_ID"]
        assert len(client.get(f"{base2}/runs", headers=AUTH).json()["runs"]) == 6
        assert client.delete(f"{base2}/secrets/HOOK_ID", headers=AUTH).status_code == 204
        assert client.delete(f"{base2}/secrets/HOOK_ID", headers=AUTH).status_code == 404
        assert client.delete(base2, headers=AUTH).status_code == 204
        forge.create("nandi.uk", "app2")
        assert client.get(f"{base2}/secrets", headers=AUTH).json()["secrets"] == []
        assert client.get(f"{base2}/runs", headers=AUTH).json()["runs"] == []


def test_user_secrets_apply_to_every_repo(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge, jobs = client.app.state.forge, client.app.state.jobs
        forge.create("nandi.uk", "one")
        forge.create("nandi.uk", "two")
        forge.create("someone", "else")
        user = "/api/forge/user/secrets"

        assert client.get(user).status_code == 401
        assert client.put(f"{user}/X", json={"value": "v"}).status_code == 401
        r = client.put(f"{user}/SHARED_TOKEN", json={"value": "user-wide-1"}, headers=AUTH)
        assert r.status_code == 201 and "value" not in r.json()
        assert client.put(f"{user}/SHARED_TOKEN", json={"value": "user-wide-2"}, headers=AUTH).status_code == 200
        assert client.put(f"{user}/REGION", json={"value": "ams-user"}, headers=AUTH).status_code == 201
        assert client.put(f"{user}/FORGE_X", json={"value": "v"}, headers=AUTH).status_code == 400
        listed = client.get(user, headers=AUTH).json()
        assert listed["enabled"] and listed["owner"] == "nandi.uk"
        assert [s["name"] for s in listed["secrets"]] == ["REGION", "SHARED_TOKEN"] and "user-wide" not in str(listed)
        raw = jobs.kv.get("forge_user_secrets_" + "nandi.uk".encode().hex())
        assert raw and "user-wide" not in str(raw)

        # Repo two overrides REGION; the repo listing shows what it inherits.
        assert client.put("/api/forge/repos/nandi.uk/two/secrets/REGION", json={"value": "fra-repo"},
                          headers=AUTH).status_code == 201
        inh = client.get("/api/forge/repos/nandi.uk/two/secrets", headers=AUTH).json()["inherited"]
        assert [(s["name"], s["overridden"]) for s in inh] == [("REGION", True), ("SHARED_TOKEN", False)]

        secrets = jobs.secrets
        assert secrets.values("nandi.uk/one") == {"SHARED_TOKEN": "user-wide-2", "REGION": "ams-user"}
        assert secrets.values("nandi.uk/two") == {"SHARED_TOKEN": "user-wide-2", "REGION": "fra-repo"}
        depot_secrets(client.put)
        assert secrets.values("someone/else") == {}  # another owner's repo doesn't get them

        # A job sees the merged set, masked in its output.
        job = '[[job]]\nname = "show"\nrun = "echo tok=$SHARED_TOKEN region=${REGION}"\n'
        forge.commit_files("nandi.uk/two", "main", {".forge/jobs.toml": job}, "jobs")
        jobs.wait()
        run = client.get("/api/forge/repos/nandi.uk/two/runs", headers=AUTH).json()["runs"][0]
        out = client.get(f"/api/forge/repos/nandi.uk/two/runs/{run['id']}", headers=AUTH).json()["output"]
        assert run["status"] == "success" and "tok=*** region=***" in out and "user-wide" not in out

        # Deleting a repo leaves the user secrets alone.
        assert client.delete("/api/forge/repos/nandi.uk/one", headers=AUTH).status_code == 204
        assert len(client.get(user, headers=AUTH).json()["secrets"]) == 4
        assert client.delete(f"{user}/REGION", headers=AUTH).status_code == 204
        assert client.delete(f"{user}/REGION", headers=AUTH).status_code == 404
        assert secrets.values("nandi.uk/two")["REGION"] == "fra-repo"


FAKE_DEPOT = """#!/bin/sh
# Stands in for the Depot CLI: records its arguments and environment, writes build metadata.
printf '%s\\n' "$@" > "$FAKE_DEPOT_LOG"
prev=""; for a in "$@"; do [ "$prev" = "--file" ] && cat "$a" >> "$FAKE_DEPOT_LOG"; prev="$a"; done
echo "token=$DEPOT_TOKEN project=$DEPOT_PROJECT_ID" >> "$FAKE_DEPOT_LOG"
cat "$HOME/.docker/config.json" >> "$FAKE_DEPOT_LOG" 2>/dev/null
meta=""; prev=""
for a in "$@"; do [ "$prev" = "--metadata-file" ] && meta="$a"; prev="$a"; done
echo "#1 [internal] load build definition from Dockerfile"
echo "Build: https://depot.dev/orgs/org1/projects/proj9/builds/b123"
test -f Dockerfile || { echo "no Dockerfile in $(pwd)"; exit 1; }
if [ -n "$meta" ]; then
  printf '{"depot.build": {"buildID": "b123", "projectID": "proj9"}, "containerimage.digest": "sha256:abc"}' > "$meta"
fi
"""


def test_depot_build_job(tmp_path, monkeypatch):
    fake = tmp_path / "depot"
    fake.write_text(FAKE_DEPOT.replace("$FAKE_DEPOT_LOG", str(tmp_path / "depot.log")))
    fake.chmod(0o755)
    monkeypatch.setenv("FILEAPI_DEPOT_CLI", str(fake))
    log = tmp_path / "depot.log"
    with TestClient(app_for(tmp_path)) as client:
        forge, jobs = client.app.state.forge, client.app.state.jobs
        forge.create("nandi.uk", "img")
        base = "/api/forge/repos/nandi.uk/img"
        forge.commit_files("nandi.uk/img", "main", {"Dockerfile": "FROM alpine\n"}, "first")
        toml = """
[[job]]
name = "image"
[job.depot]
platforms = ["linux/amd64", "linux/arm64"]
tags = ["ghcr.io/me/img:${FORGE_SHA}"]
push = true
build_args = { VERSION = "${FORGE_SHA}" }
login = { server = "ghcr.io", username = "me", password = "${GHCR_TOKEN}" }
"""
        # No DEPOT_TOKEN secret: the run says exactly what's missing.
        sha = forge.commit_files("nandi.uk/img", "main", {".forge/jobs.toml": toml}, "jobs")["commit"]
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["kind"] == "depot" and run["status"] == "error" and "DEPOT_TOKEN" in run["error"]

        for name, value in (("DEPOT_TOKEN", "depot-tok-1"), ("DEPOT_PROJECT_ID", "proj9"), ("GHCR_TOKEN", "ghcr-pw-1")):
            assert client.put(f"{base}/secrets/{name}", json={"value": value}, headers=AUTH).status_code == 201
        assert client.post(f"{base}/runs", json={}, headers=AUTH).status_code == 202
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["status"] == "success", run
        assert run["depot"]["build_id"] == "b123" and run["depot"]["digest"] == "sha256:abc"
        # DEPOT_PROJECT_ID is a secret for convenience only, so it isn't masked (the link keeps working).
        assert run["depot"]["url"] == "https://depot.dev/orgs/org1/projects/proj9/builds/b123"
        assert "image" not in run["depot"]  # pushed, not saved to the Depot registry
        args = log.read_text()
        assert args.startswith("build\n--metadata-file\n")
        for want in ("--project\nproj9\n", "--platform\nlinux/amd64\n--platform\nlinux/arm64\n",
                     f"--tag\nghcr.io/me/img:{sha}\n", f"--build-arg\nVERSION={sha}\n", "--push\n.\n",
                     "token=depot-tok-1 project=proj9"):
            assert want in args, (want, args)
        assert "--save" not in args
        assert base64.b64encode(b"me:ghcr-pw-1").decode() in args  # registry login for the push
        out = client.get(f"{base}/runs/{run['id']}", headers=AUTH).json()["output"]
        assert "$ depot build --project proj9 --progress plain" in out and "pushed ghcr.io/me/img:" in out
        assert "depot-tok-1" not in out and "ghcr-pw-1" not in out

        # Bare `depot = true`: ./Dockerfile, saved to the Depot registry.
        forge.commit_files("nandi.uk/img", "main", {".forge/jobs.toml": "[[job]]\nname='img'\ndepot = true\n"}, "b")
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["status"] == "success" and run["depot"]["image"] == "registry.depot.dev/proj9:b123"
        # A project ID in the jobs file wins over the secret.
        toml2 = "[[job]]\nname='img'\ndepot = { project = 'pub7' }\n"
        forge.commit_files("nandi.uk/img", "main", {".forge/jobs.toml": toml2}, "b2")
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["depot"]["image"] == "registry.depot.dev/pub7:b123" and "--project\npub7\n" in log.read_text()
        assert "--save\n.\n" in log.read_text()

        # Run jobs are Depot builds of a generated Dockerfile; scripts that use depot get it installed there.
        toml3 = "[[job]]\nname='sh'\nimage='alpine:3'\nplatform='linux/arm64'\nrun='depot build .'\n"
        forge.commit_files("nandi.uk/img", "main", {".forge/jobs.toml": toml3}, "c")
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["status"] == "success" and run["kind"] == "run", run
        args = log.read_text()
        assert "--build-context\nforge-tools=" in args and "--secret\nid=forge_env,src=" in args
        assert f"--build-arg\nFORGE_RUN_ID={run['id']}\n" in args and "--platform\nlinux/arm64\n" in args
        assert "FROM alpine:3" in args and "dl.depot.dev/cli/download/Linux/" in args and "job.sh" in args
        assert "depot-tok-1" not in args.split("token=")[0]  # secrets go in the secret file, not the command line

        for bad, msg in (("depot = 3", "must be a table"), ("depot = { push = true }", "needs tags"),
                         ("depot = { colour = 'red' }", "unknown depot setting"),
                         ("depot = { context = '../x' }", "relative to the repo root"),
                         ("depot = true\nrun = 'x'", "exactly one of"),
                         ("depot = true\nimage = 'alpine'", "for run jobs"), ("run = 'x'\nimage = 3", "is a string")):
            with pytest.raises(StorageError, match=msg):
                parse_jobs(f"[[job]]\nname = 'x'\n{bad}\n")


def test_secrets_need_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("FILEAPI_SECRETS_KEY")
    with TestClient(app_for(tmp_path)) as client:
        client.app.state.forge.create("nandi.uk", "app")
        base = "/api/forge/repos/nandi.uk/app"
        assert client.get(f"{base}/secrets", headers=AUTH).json()["enabled"] is False
        r = client.put(f"{base}/secrets/X", json={"value": "v"}, headers=AUTH)
        assert r.status_code == 503 and "FILEAPI_SECRETS_KEY" in r.json()["detail"]
    monkeypatch.setenv("FILEAPI_SECRETS_KEY", "too-short")
    with pytest.raises(RuntimeError, match="FILEAPI_SECRETS_KEY"):
        app_for(tmp_path)


@pytest.mark.anyio
async def test_git_push_runs_deploy_job(server_url, tmp_path):
    async with connect(server_url, TOKEN) as c:
        assert not (await c.call_tool("create_repo", {"name": "site"})).is_error
        r = await c.call_tool("set_repo_secret", {"repo": "site", "name": "API_KEY", "value": "abcd-efgh"})
        assert not r.is_error and "Added" in text(r)
        assert "API_KEY" in text(await c.call_tool("list_repo_secrets", {"repo": "site"}))
        assert "abcd-efgh" not in text(await c.call_tool("list_repo_secrets", {"repo": "site"}))
        assert "No runs yet" in text(await c.call_tool("deploy_runs", {"repo": "site"}))

    depot_secrets(lambda path, **kw: httpx.put(server_url + path, **kw))
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-b", "main", cwd=work)
    (work / ".forge").mkdir()
    (work / ".forge" / "jobs.toml").write_text('[[job]]\nname = "ship"\nrun = "echo key=$API_KEY on $FORGE_BRANCH"\n')
    git("add", ".", cwd=work)
    git("commit", "-m", "jobs", cwd=work)
    remote = server_url.replace("://", f"://x:{TOKEN}@") + "/git/nandi.uk/site.git"
    git("push", remote, "main", cwd=work)

    async with connect(server_url, TOKEN) as c:
        for _ in range(100):
            listing = text(await c.call_tool("deploy_runs", {"repo": "site"}))
            if "success" in listing or "failed" in listing:
                break
            time.sleep(0.1)
        assert "success" in listing and "ship" in listing and "push" in listing, listing
        run_id = listing.split()[0]
        out = text(await c.call_tool("deploy_runs", {"repo": "site", "run_id": run_id}))
        assert "key=*** on main" in out and "abcd-efgh" not in out
        r = await c.call_tool("run_deploy", {"repo": "site"})
        assert not r.is_error and "ship" in text(r)
        assert not (await c.call_tool("delete_repo_secret", {"repo": "site", "name": "API_KEY"})).is_error
        r = await c.call_tool("set_user_secret", {"name": "ALL_REPOS", "value": "every-where"})
        assert not r.is_error and "Added" in text(r)
        assert "ALL_REPOS" in text(await c.call_tool("list_user_secrets", {}))
        listing = text(await c.call_tool("list_repo_secrets", {"repo": "site"}))
        assert "ALL_REPOS" in listing and "every-where" not in listing
        assert not (await c.call_tool("delete_user_secret", {"name": "ALL_REPOS"})).is_error
        assert "ALL_REPOS" not in text(await c.call_tool("list_user_secrets", {}))


def test_depot_token_setup(tmp_path, monkeypatch):
    from fileapi import depot_setup

    state = tmp_path / "projects.json"
    state.write_text('[{"id": "old1", "name": "other"}]')
    fake = tmp_path / "bin" / "depot"
    fake.parent.mkdir()
    fake.write_text(f"""#!/bin/sh
case "$1 $2" in
  "login --quiet") exit 0 ;;
  "login token") echo user-tok-9 ;;
  "projects list") cat {state} ;;
  "projects create") printf '[{{"id": "old1", "name": "other"}}, {{"id": "new2", "name": "%s"}}]' "$3" > {state} ;;
  *) exit 9 ;;
esac
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(depot_setup.git_credential, "token_for", lambda base: {"access_token": "fa-tok"})
    calls, api = [], []

    def fileapi(base, token, method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/forge/repos":
            return {"repos": [{"repo": "nandi.uk/site", "name": "site"}]}
        return {"enabled": True}

    def depot_api(method, body, token):
        api.append((method, body, token))
        return {"tokenId": "t1", "secret": "proj-secret-1"}

    monkeypatch.setattr(depot_setup, "fileapi", fileapi)
    monkeypatch.setattr(depot_setup, "depot_api", depot_api)
    assert depot_setup.main(["site"]) == 0
    assert api == [("depot.core.v1.ProjectService/CreateToken",
                    {"projectId": "new2", "description": "fileapi forge nandi.uk/site"}, "user-tok-9")]
    assert ("PUT", "/api/forge/repos/nandi.uk/site/secrets/DEPOT_TOKEN", {"value": "proj-secret-1"}) in calls
    assert ("PUT", "/api/forge/repos/nandi.uk/site/secrets/DEPOT_PROJECT_ID", {"value": "new2"}) in calls
    # An existing project, by ID, isn't created again.
    api.clear()
    assert depot_setup.main(["nandi.uk/site", "old1"]) == 0
    assert api[0][1]["projectId"] == "old1"
    with pytest.raises(SystemExit):
        depot_setup.main(["nope"])
    # --user: the Depot user token becomes a user secret; a repo given too gets only its project ID.
    calls.clear()
    api.clear()
    assert depot_setup.main(["--user"]) == 0
    assert calls[-1] == ("PUT", "/api/forge/user/secrets/DEPOT_TOKEN", {"value": "user-tok-9"}) and api == []
    calls.clear()
    assert depot_setup.main(["--user", "site", "old1"]) == 0
    assert ("PUT", "/api/forge/user/secrets/DEPOT_TOKEN", {"value": "user-tok-9"}) in calls
    assert ("PUT", "/api/forge/repos/nandi.uk/site/secrets/DEPOT_PROJECT_ID", {"value": "old1"}) in calls
    assert not any(p.endswith("/secrets/DEPOT_TOKEN") and "/repos/" in p for _, p, _ in calls) and api == []
    with pytest.raises(SystemExit):
        depot_setup.main([])


# ---- releases ------------------------------------------------------------------


def test_releases_api(tmp_path, backend):
    import hashlib

    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("nandi.uk", "tool")
        sha = forge.commit_files("nandi.uk/tool", "main", {"a.txt": "a\n"}, "first")["commit"]
        base = "/api/forge/repos/nandi.uk/tool/releases"

        # Private repo: invisible and read-only without signing in.
        assert client.get(base).status_code == 404
        assert client.post(base, json={"tag": "v1.0.0"}).status_code == 401
        assert client.get(base, headers=AUTH).json() == {"releases": [], "can_edit": True,
                                                         "max_asset": 2 * 1024 * 1024 * 1024}

        r = client.post(base, json={"tag": "v1.0.0", "name": "First", "notes": "# Hello"}, headers=AUTH)
        assert r.status_code == 201, r.text
        rel = r.json()
        assert rel["tag"] == "v1.0.0" and rel["target"] == sha and rel["tagged"] is False and rel["assets"] == []
        assert client.post(base, json={"tag": "v1.0.0"}, headers=AUTH).status_code == 409
        for bad in ("latest", "../x", "a b", ""):
            assert client.post(base, json={"tag": bad}, headers=AUTH).status_code == 400, bad

        # One-shot upload (curl -T), then a replacement.
        data = os.urandom(300_000)
        r = client.put(f"{base}/v1.0.0/assets/tool-linux.tar.gz", content=data, headers=AUTH)
        assert r.status_code == 201, r.text
        assert r.json()["size"] == len(data) and r.json()["sha256"] == hashlib.sha256(data).hexdigest()
        assert r.json()["state"] == "ready" and r.json()["content_type"] == "application/x-tar"
        r = client.put(f"{base}/v1.0.0/assets/tool-linux.tar.gz", content=data[:10], headers=AUTH)
        assert r.json()["size"] == 10
        client.put(f"{base}/v1.0.0/assets/tool-linux.tar.gz", content=data, headers=AUTH)

        # In pieces, with the offset checked and the sha256 verified at the end.
        big = os.urandom(9 * 1024 * 1024 + 5)
        url = f"{base}/v1.0.0/assets/big.bin"
        assert client.put(url, params={"final": "false"}, content=big[:4_000_000], headers=AUTH).status_code == 200
        r = client.put(url, params={"offset": 123, "final": "false"}, content=big[4_000_000:], headers=AUTH)
        assert r.status_code == 409 and "offset=4000000" in r.json()["detail"]
        listed = client.get(base, headers=AUTH).json()["releases"][0]["assets"]
        assert [(a["name"], a["state"]) for a in listed] == [("tool-linux.tar.gz", "ready"), ("big.bin", "uploading")]
        assert client.get(url, headers=AUTH).status_code == 404  # not downloadable until complete
        r = client.put(url, params={"offset": 4_000_000, "sha256": hashlib.sha256(big).hexdigest()},
                       content=big[4_000_000:], headers=AUTH)
        assert r.status_code == 200 and r.json()["state"] == "ready" and r.json()["size"] == len(big)
        r = client.get(url, headers=AUTH)
        assert r.content == big and r.headers["content-length"] == str(len(big))
        assert "attachment" in r.headers["content-disposition"]
        bad = client.put(f"{base}/v1.0.0/assets/x.bin", params={"sha256": "0" * 64}, content=b"abc", headers=AUTH)
        assert bad.status_code == 400 and "mismatch" in bad.json()["detail"]
        assert "x.bin" not in str(client.get(base, headers=AUTH).json())
        for bad_name in (".hidden", "a\\b"):
            assert client.put(f"{base}/v1.0.0/assets/{bad_name}", content=b"x", headers=AUTH).status_code == 400
        assert client.put(f"{base}/v9/assets/x.bin", content=b"x", headers=AUTH).status_code == 404
        r = client.put(f"{base}/v9/assets/x.bin", params={"create": "true"}, content=b"x", headers=AUTH)
        assert r.status_code == 201 and client.get(f"{base}/v9", headers=AUTH).json()["assets"][0]["name"] == "x.bin"

        # Private repo: download_url is a signed link that works without a header (and only for that file).
        signed = client.get(f"{base}/v1.0.0", headers=AUTH).json()["assets"][0]["download_url"]
        assert "?dl=" in signed
        path = signed.split("testserver", 1)[1]
        assert client.get(path).content == data
        assert client.get(path.replace("tool-linux.tar.gz", "big.bin")).status_code == 404
        assert client.get(path.split("?")[0]).status_code == 404

        # Drafts and prereleases; `latest` is the newest published release.
        client.post(base, json={"tag": "v2.0.0-rc1", "prerelease": True}, headers=AUTH)
        client.post(base, json={"tag": "v2.0.0", "draft": True}, headers=AUTH)
        client.delete(f"{base}/v9", headers=AUTH)
        assert client.get(f"{base}/latest", headers=AUTH).json()["tag"] == "v1.0.0"
        forge.set_public("nandi.uk/tool", True)
        tags = [r["tag"] for r in client.get(base).json()["releases"]]
        assert tags == ["v2.0.0-rc1", "v1.0.0"]  # anonymous: no drafts
        assert client.get(base).json()["can_edit"] is False
        assert client.get(f"{base}/v2.0.0").status_code == 404
        assert "?dl=" not in client.get(f"{base}/latest").json()["assets"][0]["download_url"]
        assert client.get(f"{base}/latest/assets/tool-linux.tar.gz").content == data
        assert client.put(f"{base}/v1.0.0/assets/y", content=b"y").status_code == 401
        r = client.patch(f"{base}/v2.0.0", json={"draft": False, "notes": "Done"}, headers=AUTH)
        assert r.status_code == 200 and r.json()["draft"] is False
        assert client.get(f"{base}/latest").json()["tag"] == "v2.0.0"
        downloads = client.get(f"{base}/v1.0.0").json()["assets"][0]["downloads"]
        assert downloads >= 2

        # A repo pass (repo_remote_url, a job's FORGE_TOKEN) can publish; for this repo only.
        passes = client.app.state.forge  # noqa: F841 - the pass issuer lives in the app closure
        from fileapi.forge import RepoPasses

        cred, _ = RepoPasses(client.app.state.auth.kv).issue("nandi.uk/tool", 5)
        r = client.put(f"{base}/v2.0.0/assets/ci.txt", content=b"from ci", headers={"Authorization": f"Bearer {cred}"})
        assert r.status_code == 201, r.text
        other, _ = RepoPasses(client.app.state.auth.kv).issue("nandi.uk/other", 5)
        assert client.put(f"{base}/v2.0.0/assets/ci.txt", content=b"x",
                          headers={"Authorization": f"Bearer {other}"}).status_code == 401

        # Rename a release onto a git tag: it points at the tagged commit.
        sha2 = forge.commit_files("nandi.uk/tool", "main", {"a.txt": "b\n"}, "second")["commit"]
        r = forge.open("nandi.uk/tool")
        r.refs[b"refs/tags/v2.0.1"] = sha2.encode()
        r.close()
        r = client.patch(f"{base}/v2.0.0", json={"tag": "v2.0.1"}, headers=AUTH)
        assert r.json()["tag"] == "v2.0.1" and r.json()["target"] == sha2 and r.json()["tagged"] is True

        # Releases follow a repo rename and go with a delete.
        assert client.patch("/api/forge/repos/nandi.uk/tool", json={"name": "tool2"}, headers=AUTH).status_code == 200
        base2 = "/api/forge/repos/nandi.uk/tool2/releases"
        assert client.get(f"{base2}/v1.0.0/assets/big.bin", headers=AUTH).content == big
        assert client.delete(f"{base2}/v1.0.0/assets/big.bin", headers=AUTH).status_code == 204
        assert client.get(f"{base2}/v1.0.0/assets/big.bin", headers=AUTH).status_code == 404
        assert client.delete(f"{base2}/v1.0.0", headers=AUTH).status_code == 204
        assert [r["tag"] for r in client.get(base2, headers=AUTH).json()["releases"]] == ["v2.0.1", "v2.0.0-rc1"]
        assert client.delete("/api/forge/repos/nandi.uk/tool2", headers=AUTH).status_code == 204
        forge.create("nandi.uk", "tool2")
        assert client.get(base2, headers=AUTH).json()["releases"] == []


RELEASE_JOBS = """
[[job]]
name = "publish"
tags = ["v*"]
run = '''
echo "built for $FORGE_TAG" > notes.txt
forge-release upload --create "$FORGE_TAG" notes.txt
echo "token=$FORGE_TOKEN"
'''

[[job]]
name = "deploy"
run = "echo deploy"
"""


@pytest.mark.anyio
async def test_releases_from_tag_jobs_cli_and_mcp(server_url, tmp_path):
    """A pushed tag runs the jobs with `tags`, which publish with forge-release; the CLI and MCP tools
    see the result."""
    r = httpx.post(f"{server_url}/api/forge/repos", json={"name": "rel"}, headers=AUTH)
    assert r.status_code == 201, r.text
    depot_secrets(lambda path, **kw: httpx.put(server_url + path, **kw))
    remote = server_url.replace("://", f"://x:{TOKEN}@") + "/git/nandi.uk/rel.git"
    work = tmp_path / "work"
    work.mkdir()
    git("init", "-b", "main", cwd=work)
    (work / ".forge").mkdir()
    (work / ".forge" / "jobs.toml").write_text(RELEASE_JOBS)
    git("add", ".", cwd=work)
    git("commit", "-m", "jobs", cwd=work)
    git("push", remote, "main", cwd=work)
    git("tag", "-a", "v1.0", "-m", "one", cwd=work)
    git("push", remote, "v1.0", cwd=work)

    api = f"{server_url}/api/forge/repos/nandi.uk/rel"
    for _ in range(200):
        runs = httpx.get(f"{api}/runs", headers=AUTH).json()["runs"]
        if len(runs) == 2 and all(x["status"] not in ("queued", "running") for x in runs):
            break
        time.sleep(0.1)
    by_job = {x["job"]: x for x in runs}
    assert set(by_job) == {"publish", "deploy"}, runs  # main push -> deploy only; tag push -> publish only
    pub = httpx.get(f"{api}/runs/{by_job['publish']['id']}", headers=AUTH).json()
    assert pub["status"] == "success" and pub["tag"] == "v1.0" and pub["trigger"] == "tag", pub
    assert "token=***" in pub["output"]
    rel = httpx.get(f"{api}/releases/v1.0", headers=AUTH).json()
    assert rel["tagged"] is True and [a["name"] for a in rel["assets"]] == ["notes.txt"]
    assert httpx.get(rel["assets"][0]["download_url"]).text == "built for v1.0\n"

    # The CLI, signed in with a token, inside the clone (repo and server come from the remote).
    git("remote", "add", "origin", f"{server_url}/git/nandi.uk/rel.git", cwd=work)
    big = work / "big.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024 + 7))
    cli = [sys.executable, str(Path(__file__).parent.parent / "fileapi" / "git_credential.py"), "release"]
    env = {**os.environ, "FILEAPI_TOKEN": TOKEN, "GIT_CONFIG_GLOBAL": os.devnull}
    for k in ("FORGE_URL", "FORGE_REPO", "FORGE_TOKEN"):
        env.pop(k, None)

    def run(*args):
        return subprocess.run([*cli, *args], cwd=work, env=env, capture_output=True, text=True)

    r = run("create", "v1.1", "--notes", "Second", "--draft")
    assert r.returncode == 0, r.stderr
    r = run("upload", "--piece", "1m", "v1.1", str(big))
    assert r.returncode == 0, r.stderr
    assert "v1.1\tv1.1 [draft]" in run("list").stdout
    r = run("download", "v1.1", "-o", str(tmp_path / "dl"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "dl" / "big.bin").read_bytes() == big.read_bytes()
    assert run("publish", "v1.1").returncode == 0
    assert run("upload", "nope", str(big)).returncode == 1
    r = run("changelog", "v9")
    assert r.returncode == 0 and "## What's changed" in r.stdout, (r.stdout, r.stderr)

    async with connect(server_url, TOKEN) as c:
        listing = text(await c.call_tool("list_releases", {"repo": "rel"}))
        assert listing.index("v1.1") < listing.index("v1.0") and "big.bin  3.0 MiB" in listing
        r = await c.call_tool("upload_release_asset", {"repo": "rel", "tag": "v2", "name": "sum.txt",
                                                       "text": "abc\n", "create": True})
        assert not r.is_error, text(r)
        assert "Bearer fg_" in text(await c.call_tool("release_upload_url", {"repo": "rel", "tag": "v2"}))
        log = text(await c.call_tool("release_changelog", {"repo": "rel", "tag": "v3", "since": "v1.0"}))
        assert "0 commits from v1.0 to v3" in log and "No changes since v1.0." in log, log
        assert "Done" in text(await c.call_tool("update_release", {"repo": "rel", "tag": "v2", "name": "Done"}))
        assert not (await c.call_tool("delete_release_asset", {"repo": "rel", "tag": "v2", "name": "sum.txt"})).is_error
        assert not (await c.call_tool("delete_release", {"repo": "rel", "tag": "v2"})).is_error
        assert (await c.call_tool("delete_release", {"repo": "rel", "tag": "v2"})).is_error


def test_mirror_job_and_after(tmp_path, backend):
    from dulwich.repo import Repo

    target = tmp_path / "github.git"
    Repo.init_bare(str(target), mkdir=True)
    toml = f"""
[[job]]
name = "github"
branches = ["*"]
tags = ["*"]
mirror = {{ url = "{target}" }}

[[job]]
name = "deploy"
after = "github"
run = "echo deploying $FORGE_SHA"
"""
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge, jobs = client.app.state.forge, client.app.state.jobs
        forge.create("nandi.uk", "app")
        depot_secrets(client.put)
        base = "/api/forge/repos/nandi.uk/app"
        sha = forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": toml}, "jobs")["commit"]
        jobs.wait()
        runs = {r["job"]: r for r in client.get(f"{base}/runs", headers=AUTH).json()["runs"]}
        assert runs["github"]["status"] == "success" and runs["github"]["kind"] == "mirror", runs
        assert runs["deploy"]["status"] == "success" and runs["deploy"]["after"] == "github"
        mirrored = Repo(str(target))
        assert mirrored.refs[b"refs/heads/main"] == sha.encode()

        # Other branches mirror too (deploy only runs for main); a fast-forward goes through.
        forge.commit_files("nandi.uk/app", "dev", {"a.txt": "a"}, "dev")
        sha2 = forge.commit_files("nandi.uk/app", "main", {"b.txt": "b"}, "more")["commit"]
        jobs.wait()
        assert mirrored.refs[b"refs/heads/main"] == sha2.encode() and b"refs/heads/dev" in mirrored.refs.allkeys()

        # GitHub moved on by itself: main there isn't overwritten, the mirror fails, deploy is skipped.
        from dulwich.objects import Commit, Tree
        tree = Tree()
        mirrored.object_store.add_object(tree)
        c = Commit()
        c.tree, c.parents = tree.id, [sha2.encode()]
        c.author = c.committer = b"x <x@x>"
        c.author_time = c.commit_time = 0
        c.author_timezone = c.commit_timezone = 0
        c.message = b"made on github"
        mirrored.object_store.add_object(c)
        mirrored.refs[b"refs/heads/main"] = c.id
        forge.commit_files("nandi.uk/app", "main", {"c.txt": "c"}, "diverged")
        jobs.wait()
        runs = client.get(f"{base}/runs", headers=AUTH).json()["runs"]
        latest = {r["job"]: r for r in runs[:2]}
        assert latest["github"]["status"] == "failed" and latest["deploy"]["status"] == "skipped"
        out = client.get(f"{base}/runs/{latest['github']['id']}", headers=AUTH).json()["output"]
        assert "refs/heads/main: kept as it is there" in out
        assert Repo(str(target)).refs[b"refs/heads/main"] == c.id

        # A token secret that isn't set: the mirror is skipped (not failed), and so is what waits for it.
        toml2 = toml.replace(f'url = "{target}"', f'url = "{target}", token = "${{NOT_SET_TOKEN}}"')
        forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": toml2}, "token")
        jobs.wait()
        latest = {r["job"]: r for r in client.get(f"{base}/runs", headers=AUTH).json()["runs"][:2]}
        assert latest["github"]["status"] == "skipped" and "NOT_SET_TOKEN" in latest["github"]["error"]
        assert latest["deploy"]["status"] == "skipped"

    for bad, msg in (("[[job]]\nname='a'\nafter='nope'\nrun='x'\n", "no such job"),
                     ("[[job]]\nname='a'\nafter='b'\nrun='x'\n[[job]]\nname='b'\nafter='a'\nrun='x'\n", "wait for each other"),
                     ("[[job]]\nname='a'\nmirror={colour='x'}\n", "unknown mirror setting"),
                     ("[[job]]\nname='a'\ndepot_ci={repo='a/b'}\n", "depot_ci.workflow is required")):
        with pytest.raises(StorageError, match=msg):
            parse_jobs(bad)


FAKE_DEPOT_CI = r"""#!/bin/sh
echo "$@ token=$DEPOT_TOKEN" >> "$FAKE_LOG"
case "$1 $2" in
  "ci secrets") cat >> "$FAKE_LOG"; echo >> "$FAKE_LOG"; echo "ok" ;;
  "ci dispatch") echo '{"org_id": "org1", "run_id": "run42"}' ;;
  "ci status")
    n=$(cat "$FAKE_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$FAKE_STATE"
    s=running; [ "$n" -ge "$FAKE_DONE_AFTER" ] && s="$FAKE_END"
    echo "{\"org_id\":\"org1\",\"run_id\":\"run42\",\"status\":\"$s\",\"workflows\":[{\"workflow_id\":\"wf7\",\"name\":\"Deploy\",\"status\":\"$s\",\"jobs\":[{\"job_display_name\":\"deploy\",\"attempts\":[{\"view_url\":\"https://depot.dev/orgs/org1/workflows/wf7?job=j1\"}]}]}]}"
    ;;
  *) exit 2 ;;
esac
"""


def test_depot_ci_job_and_resume(tmp_path, monkeypatch):
    import fileapi.deploy as deploy

    fake = tmp_path / "depot"
    fake.write_text(FAKE_DEPOT_CI)
    fake.chmod(0o755)
    log, state = tmp_path / "depot.log", tmp_path / "ci-count"
    monkeypatch.setenv("FILEAPI_DEPOT_CLI", str(fake))
    monkeypatch.setattr(deploy, "DEPOT_CI_POLL", 0.01)
    # The fake reads these from the job's environment, which is scrubbed: pass them through PATH's sibling vars.
    real_env = deploy.Jobs._depot_ci_env
    monkeypatch.setattr(deploy.Jobs, "_depot_ci_env", lambda self, env, home: {
        **real_env(self, env, home), "FAKE_LOG": str(log), "FAKE_STATE": str(state),
        "FAKE_DONE_AFTER": os.environ.get("FAKE_DONE_AFTER", "3"), "FAKE_END": os.environ.get("FAKE_END", "finished")})
    toml = """
[[job]]
name = "deploy"
[job.depot_ci]
repo = "me/app"
workflow = "deploy.yml"
inputs = { sha = "${FORGE_SHA}" }
secrets = ["FASTAPI_TOKEN"]
"""
    with TestClient(app_for(tmp_path)) as client:
        forge, jobs = client.app.state.forge, client.app.state.jobs
        forge.create("nandi.uk", "app")
        base = "/api/forge/repos/nandi.uk/app"
        for name, value in (("DEPOT_TOKEN", "depot-org-tok"), ("FASTAPI_TOKEN", "fastapi-deploy-tok")):
            assert client.put(f"{base}/secrets/{name}", json={"value": value}, headers=AUTH).status_code == 201
        sha = forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": toml}, "jobs")["commit"]
        jobs.wait()
        run = client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]
        assert run["status"] == "success" and run["kind"] == "depot_ci", run
        assert run["depot"]["ci_run_id"] == "run42" and run["depot"]["url"] == "https://depot.dev/orgs/org1/workflows/wf7"
        calls = log.read_text()
        assert "ci secrets add FASTAPI_TOKEN --repo me/app token=depot-org-tok\nfastapi-deploy-tok\n" in calls
        assert f"ci dispatch --repo me/app --workflow deploy.yml --ref main -o json --input sha={sha}" in calls
        out = client.get(f"{base}/runs/{run['id']}", headers=AUTH).json()["output"]
        assert "https://depot.dev/orgs/org1/workflows/wf7" in out and "Depot CI: finished" in out
        assert "fastapi-deploy-tok" not in out and "depot-org-tok" not in out
        assert (jobs.kv.get(deploy.WATCH_KEY) or {}).get("runs") == []

        # A failed Depot run fails the forge run.
        state.unlink()
        monkeypatch.setenv("FAKE_END", "failed")
        client.post(f"{base}/runs", json={}, headers=AUTH)
        jobs.wait()
        assert client.get(f"{base}/runs", headers=AUTH).json()["runs"][0]["status"] == "failed"

        # The server following a run stopped (it deployed itself): the next one picks the wait back up.
        monkeypatch.setenv("FAKE_END", "finished")
        state.unlink()
        stuck = {"id": "ab" * 8, "repo": "nandi.uk/app", "job": "deploy", "kind": "depot_ci", "status": "running",
                 "started_ts": time.time(), "timeout": 600, "output": "Depot CI run run42 started\n",
                 "depot": {"ci_run_id": "run42", "org": "org1", "url": "https://depot.dev/orgs/org1/workflows/wf7"}}
        jobs.kv.put(f"forge_run_{stuck['id']}", stuck)
        jobs._add_to_index("nandi.uk/app", stuck["id"])
        jobs._watch_add(stuck)
        assert jobs.resume() == 1
        jobs.wait()
        got = client.get(f"{base}/runs/{stuck['id']}", headers=AUTH).json()
        assert got["status"] == "success" and "picked up the wait" in got["output"]
        assert jobs.resume() == 0


def test_finish_self_deploy(tmp_path):
    with TestClient(app_for(tmp_path)) as client:
        jobs = client.app.state.jobs
        run = {"id": "cd" * 8, "repo": "nandi.uk/app", "job": "deploy", "status": "running", "sha": "a" * 40,
               "started_ts": time.time() - 30, "output": "deploying\n"}
        jobs.kv.put(f"forge_run_{run['id']}", run)
        assert not jobs.finish_self_deploy(run["id"], "b" * 40)  # some other commit is live
        assert jobs.finish_self_deploy(run["id"], "a" * 40)
        got = jobs.kv.get(f"forge_run_{run['id']}")
        assert got["status"] == "success" and "is live" in got["output"]
        assert not jobs.finish_self_deploy(run["id"], "a" * 40)  # already recorded
        assert not jobs.finish_self_deploy("nope", None)


def test_forge_gc_collects_unreachable_objects(tmp_path, backend):
    from fileapi.storage import StorageError

    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("o", "r")
        if not backend:
            with pytest.raises(StorageError):
                forge.gc("o/r")
            return
        forge.commit_files("o/r", "main", {"a.txt": "one\n", "keep/b.txt": "b\n"}, "first")
        first = forge.log("o/r")[0]["sha"]
        forge.commit_files("o/r", "main", {"a.txt": "two\n" * 1000}, "second")
        forge.commit_files("o/r", "topic", {"t.txt": "topic\n"}, "on a branch")
        r = forge.open("o/r")
        try:  # force-push main back to the first commit: the second commit, its tree and blob are orphaned
            r.refs.set_if_equals(b"refs/heads/main", r.refs[b"refs/heads/main"], first.encode())
        finally:
            r.close()

        assert forge.gc("o/r")["collectable"] == 0  # everything is younger than the grace period
        dry = forge.gc("o/r", grace=0)
        assert dry["unreachable"] == 3 and dry["collectable"] == 3 and dry["deleted"] == 0
        done = forge.gc("o/r", apply=True, grace=0)
        assert done["deleted"] == 3 and done["deleted_bytes"] == dry["collectable_bytes"] > 0
        assert forge.gc("o/r", grace=0)["unreachable"] == 0
        # Both branches are intact.
        assert forge.browse("o/r", "a.txt")["content"] == "one\n"
        assert forge.browse("o/r", "t.txt", ref="topic")["content"] == "topic\n"
        usage = forge.usage()
        assert usage["database_bytes"] > 0 and usage["repos"][0]["repo"] == "o/r"

        # A push that re-sends an old orphaned object refreshes it, so gc leaves it for the grace period.
        from dulwich.objects import Blob

        blob = Blob.from_string(b"orphan\n")
        r = forge.open("o/r")
        try:
            r.object_store.add_object(blob)
        finally:
            r.close()
        import psycopg

        with psycopg.connect(backend["DATABASE_URL"], autocommit=True) as c:
            c.execute("UPDATE fileapi_git_objects SET created = now() - interval '1 day' WHERE sha = %s",
                      (blob.sha().digest(),))
        r = forge.open("o/r")
        try:
            r.object_store.add_object(blob)
        finally:
            r.close()
        assert forge.gc("o/r", grace=3600)["collectable"] == 0


def test_markdown_rendering_is_sanitized():
    from fileapi import markdown

    html = markdown.render(
        "# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- [x] done\n- [ ] todo\n\n~~old~~\n\n"
        '<p align="center"><img src="logo.png" width="80" onerror="alert(1)"></p>\n\n'
        "<script>alert(1)</script>\n\n[x](javascript:alert(1)) [y](docs/a.md#b) <a href=\"#top\" style=\"color:red\">t</a>\n"
    )
    assert "<h1>Title</h1>" in html and "<table>" in html and "<s>old</s>" in html
    assert "\u2611 done" in html and "\u2610 todo" in html
    assert '<p align="center"><img src="logo.png" width="80"></p>' in html
    assert "<script" not in html and "onerror" not in html and "href=\"javascript" not in html and "style" not in html
    assert '<a href="docs/a.md#b">y</a>' in html and '<a href="#top">t</a>' in html


def test_blob_storage_offload_and_migration(tmp_path, monkeypatch):
    """Big bytes go to the blob store (memory here, B2 in production); existing ones copy over, then free."""
    import hashlib

    import psycopg

    from fileapi import blobs as blobs_mod

    if not PG_URL:
        pytest.skip("set TEST_DATABASE_URL to run Postgres tests")
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS fileapi_files, fileapi_kv, fileapi_git_objects, fileapi_git_refs, fileapi_lfs_chunks, fileapi_lfs_objects, fileapi_release_chunks, fileapi_releases, fileapi_git_repos, fileapi_git_aliases")
    monkeypatch.setattr(blobs_mod, "MIN_OBJECT", 1000)
    monkeypatch.setattr(blobs_mod, "GIT_TO_BLOBS", True)
    monkeypatch.setenv("FILEAPI_BLOB_COPY", "off")
    big_text = os.urandom(3000).hex() + "\n"
    zblob = os.urandom(5000)
    payload = os.urandom(6000)

    # 1. Without B2: everything in Postgres, as before.
    monkeypatch.delenv("FILEAPI_BLOBS", raising=False)
    for k in ("B2_KEY_ID", "B2_APPLICATION_KEY", "B2_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:
        assert client.get("/health").json()["blobs"] == "postgres"
        forge = client.app.state.forge
        forge.create("o", "r")
        forge.commit_files("o/r", "main", {"big.txt": big_text, "small.txt": "s\n"}, "first")
        client.app.state.storage.write("notes/big.md", big_text, overwrite=True)
        oid = hashlib.sha256(payload).hexdigest()
        assert client.put(f"/git/o/r.git/info/lfs/objects/{oid}", content=payload, headers=AUTH).status_code == 200
        base = "/api/forge/repos/o/r/releases"
        client.post(base, json={"tag": "v1"}, headers=AUTH)
        assert client.put(f"{base}/v1/assets/a.bin", content=payload, headers=AUTH).status_code == 201

    # 2. With a blob store: new big things go there, old ones are copied, then freed.
    monkeypatch.setenv("FILEAPI_BLOBS", "memory")
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:
        assert client.get("/health").json()["blobs"] == "memory"
        forge, storage = client.app.state.forge, client.app.state.storage
        store = forge.store.blobs.store
        mig = client.app.state.migrator
        counts = mig.counts()
        assert counts["fileapi_git_objects"]["b2_only"][0] == 0 and counts["fileapi_files"]["postgres_only"][0] == 1

        # New writes
        forge.commit_files("o/r", "main", {"big2.txt": os.urandom(3000).hex()}, "second")
        storage.write("notes/new.md", big_text, overwrite=True)
        storage.append("notes/new.md", "more\n")
        assert storage.read("notes/new.md") == big_text + "more\n"
        storage.edit("notes/new.md", [__import__("fileapi.storage").storage.Edit("more", "less")])
        assert storage.read("notes/new.md").endswith("less\n")
        n_after_writes = len(store.data)
        assert n_after_writes == 2  # big2.txt's blob + the current notes/new.md (old versions deleted)

        # Copy existing rows (non-destructive), skipping an unreachable big blob
        from dulwich.objects import Blob

        r = forge.open("o/r")
        try:
            r.object_store.add_objects([(Blob.from_string(zblob), None)])  # goes to the store directly
        finally:
            r.close()
        with psycopg.connect(PG_URL, autocommit=True) as c:  # pretend it's an old orphan still in Postgres
            c.execute("UPDATE fileapi_git_objects SET created = now() - interval '1 day'")
            sha = Blob.from_string(zblob).sha().digest()
            key = c.execute("SELECT b2key FROM fileapi_git_objects WHERE sha = %s", (sha,)).fetchone()[0]
            c.execute("UPDATE fileapi_git_objects SET b2key = NULL, size = NULL, data = %s WHERE sha = %s",
                      (__import__("zlib").compress(zblob), sha))
            store.delete([key])
        rep = mig.copy()
        assert rep["fileapi_git_objects"]["copied"] == 1 and rep["fileapi_git_objects"]["skipped_unreachable"] == 1
        assert rep["fileapi_lfs_chunks"]["copied"] == 1 and rep["fileapi_release_chunks"]["copied"] == 1
        assert rep["fileapi_files"]["copied"] == 1
        assert mig.copy()["fileapi_lfs_chunks"]["copied"] == 0  # idempotent

        freed = mig.free()
        assert freed["fileapi_git_objects"]["freed"] == 1 and freed["fileapi_files"]["freed"] == 1
        assert freed["vacuum"]["fileapi_git_objects"] == "vacuumed"
        counts = mig.counts()
        assert counts["fileapi_lfs_chunks"]["both"][0] == 0 and counts["fileapi_lfs_chunks"]["b2_only"][0] == 1

        # Everything still reads back, now from the store
        import fileapi.forge as forge_mod

        forge_mod._OBJ_CACHE.clear()
        assert forge.browse("o/r", "big.txt")["content"] == big_text
        assert storage.read("notes/big.md") == big_text
        assert client.get(f"/git/o/r.git/info/lfs/objects/{oid}", headers=AUTH).content == payload
        assert client.get(f"{base}/v1/assets/a.bin", headers=AUTH, follow_redirects=True).content == payload
        out = forge.gc("o/r", grace=0, apply=True)
        assert out["deleted"] == 1 and out["deleted_bytes"] > 0

        # Deleting things deletes their blobs; sweep finds leftovers.
        storage.delete("notes/big.md")
        store.put("git/" + "0" * 32, b"stray")
        store.data["git/" + "0" * 32] = (b"stray", 0)
        assert mig.sweep()["orphans"] == 1 and mig.sweep(apply=True)["deleted"] == 1
        forge.delete("o/r")
        assert set(store.data) == {k for (k,) in psycopg.connect(PG_URL).execute(
            "SELECT b2key FROM fileapi_files WHERE b2key IS NOT NULL")}


def test_b2_config_sources(monkeypatch):
    from fileapi.blobs import B2Blobs, from_config

    assert from_config({}, {}, {}) is None
    s = from_config({"FILEAPI_BLOB_CACHE_MAX": "0"}, {"B2_TOKEN": "K005secret"}, {"B2_KEY_ID": "0053abc", "B2_BUCKET": "fileapi"})
    assert isinstance(s, B2Blobs) and (s.key_id, s.app_key, s.bucket) == ("0053abc", "K005secret", "fileapi")
    s = from_config({"B2_BUCKET": "other", "FILEAPI_BLOB_CACHE_MAX": "0"}, {"B2_TOKEN": "id1:key1"}, {})
    assert (s.key_id, s.app_key, s.bucket) == ("id1", "key1", "other")
    assert from_config({}, {"B2_TOKEN": "K005secret"}, {}) is None  # no key ID anywhere


def test_b2_client_against_fake_api():
    import httpx

    from fileapi.blobs import B2Blobs

    files: dict[str, dict] = {}
    calls = {"auth": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url.endswith("b2_authorize_account"):
            calls["auth"] += 1
            return httpx.Response(200, json={"accountId": "acc", "authorizationToken": f"tok{calls['auth']}",
                                             "apiUrl": "https://api.b2", "downloadUrl": "https://f.b2",
                                             "allowed": {"bucketId": None, "bucketName": None}})
        tok = req.headers.get("Authorization")
        if url.startswith("https://api.b2") and tok == "tok1" and "b2_list_file_names" in url:
            return httpx.Response(401, json={"code": "expired_auth_token"})
        if url.endswith("b2_list_buckets"):
            return httpx.Response(200, json={"buckets": [{"bucketId": "bid", "bucketName": "fileapi"}]})
        if url.endswith("b2_get_upload_url"):
            return httpx.Response(200, json={"uploadUrl": "https://up.b2/x", "authorizationToken": "up"})
        if url == "https://up.b2/x":
            name = req.headers["X-Bz-File-Name"]
            import hashlib
            assert req.headers["X-Bz-Content-Sha1"] == hashlib.sha1(req.content).hexdigest()
            files[name] = {"data": req.content, "id": "id-" + name}
            return httpx.Response(200, json={"fileId": "id-" + name})
        if url.startswith("https://f.b2/file/fileapi/"):
            name = url.split("/file/fileapi/", 1)[1]
            if name not in files:
                return httpx.Response(404)
            d = files[name]["data"]
            return httpx.Response(200, content=b"" if req.method == "HEAD" else d, headers={"Content-Length": str(len(d))})
        import json
        body = json.loads(req.content or b"{}")
        if url.endswith("b2_list_file_names"):
            return httpx.Response(200, json={"files": [{"fileName": n, "contentLength": len(f["data"]),
                                                        "uploadTimestamp": 0, "action": "upload"} for n, f in files.items()
                                                       if n.startswith(body.get("prefix", ""))], "nextFileName": None})
        if url.endswith("b2_list_file_versions"):
            return httpx.Response(200, json={"files": [{"fileName": n, "fileId": f["id"]} for n, f in files.items()]})
        if url.endswith("b2_delete_file_version"):
            files.pop(body["fileName"])
            return httpx.Response(200, json={})
        return httpx.Response(400, json={"message": url})

    b = B2Blobs("kid", "key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert b.check()["bucket_id"] == "bid"
    b.put("git/abc", b"hello")
    assert b.get("git/abc") == b"hello" and b.size("git/abc") == 5 and b.size("git/nope") is None
    assert [k for k, *_ in b.list()] == ["git/abc"]  # survives an expired token (re-authorizes)
    assert calls["auth"] == 2
    b.delete(["git/abc"])
    assert files == {}


def test_ci_can_ask_for_one_free(tmp_path, monkeypatch):
    import json
    import time as _t

    import psycopg

    from fileapi import blobs as blobs_mod

    if not PG_URL:
        pytest.skip("set TEST_DATABASE_URL to run Postgres tests")
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS fileapi_files, fileapi_kv, fileapi_git_objects, fileapi_git_refs, fileapi_lfs_chunks, fileapi_lfs_objects, fileapi_release_chunks, fileapi_releases, fileapi_git_repos, fileapi_git_aliases")
    monkeypatch.setattr(blobs_mod, "MIN_OBJECT", 1000)
    monkeypatch.setenv("FILEAPI_BLOB_COPY", "off")
    monkeypatch.delenv("FILEAPI_BLOBS", raising=False)
    big = os.urandom(3000).hex()
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:
        client.app.state.storage.write("big.txt", big, overwrite=True)
    cfg = tmp_path / "B2_CONFIG"
    cfg.write_text(json.dumps({"B2_FREE": "t1"}))
    monkeypatch.setattr(blobs_mod, "BUILD_CONFIG", cfg)
    monkeypatch.setenv("FILEAPI_BLOBS", "memory")
    monkeypatch.setenv("FILEAPI_BLOB_COPY", "on")
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:
        for _ in range(100):
            h = client.get("/health").json()
            if h["blob_free"]["state"] != "running" and h["blob_copy"] != "running":
                break
            _t.sleep(0.05)
        assert h["blob_free"]["state"] == "done", h
        assert client.app.state.migrator.counts()["fileapi_files"]["b2_only"][0] == 1
        assert client.app.state.storage.read("big.txt") == big
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:  # same token: not again
        mig = client.app.state.migrator
        for _ in range(100):
            if mig.status.get("state") == "done":
                break
            _t.sleep(0.05)
        assert "copy" in mig.status["result"] and "free" not in mig.status["result"]


def test_git_objects_come_back_from_blobs(tmp_path, monkeypatch):
    """Git objects an earlier version put in B2 are read back into Postgres (and new ones stay there)."""
    import psycopg

    from fileapi import blobs as blobs_mod

    if not PG_URL:
        pytest.skip("set TEST_DATABASE_URL to run Postgres tests")
    with psycopg.connect(PG_URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS fileapi_files, fileapi_kv, fileapi_git_objects, fileapi_git_refs, fileapi_lfs_chunks, fileapi_lfs_objects, fileapi_release_chunks, fileapi_releases, fileapi_git_repos, fileapi_git_aliases")
    monkeypatch.setattr(blobs_mod, "MIN_OBJECT", 1000)
    monkeypatch.setenv("FILEAPI_BLOB_COPY", "off")
    monkeypatch.setenv("FILEAPI_BLOBS", "memory")
    a, b = os.urandom(3000).hex(), os.urandom(3000).hex()
    with TestClient(app_for(tmp_path, database_url=PG_URL)) as client:
        forge, mig = client.app.state.forge, client.app.state.migrator
        monkeypatch.setattr(blobs_mod, "GIT_TO_BLOBS", True)
        forge.commit_files("o/r" if forge.create("o", "r") else "o/r", "main", {"a.txt": a, "b.txt": b}, "one")
        assert mig.counts()["fileapi_git_objects"]["b2_only"][0] == 2
        monkeypatch.setattr(blobs_mod, "GIT_TO_BLOBS", False)
        import fileapi.forge as forge_mod

        forge_mod._OBJ_CACHE.clear()
        assert forge.browse("o/r", "a.txt")["content"] == a  # read-through puts it back
        assert mig.counts()["fileapi_git_objects"]["b2_only"][0] == 1
        assert mig.restore_git()["restored"] == 1
        assert mig.counts()["fileapi_git_objects"]["b2_only"][0] == 0
        forge.commit_files("o/r", "main", {"c.txt": os.urandom(3000).hex()}, "two")
        assert mig.counts()["fileapi_git_objects"]["b2_only"][0] == 0
        assert mig.free()["fileapi_lfs_chunks"]["freed"] == 0 and "fileapi_git_objects" not in mig.free()


def test_blob_disk_cache(tmp_path):
    from fileapi.blobs import CachedBlobs, MemoryBlobs

    inner = MemoryBlobs()
    calls = []
    real_get = inner.get
    inner.get = lambda k: (calls.append(k), real_get(k))[1]
    c = CachedBlobs(inner, tmp_path / "cache", 4000)
    c.put("lfs/a", b"x" * 900)
    assert c.get("lfs/a") == b"x" * 900 and calls == []  # written through
    inner.put("lfs/b", b"y" * 900)
    assert c.get("lfs/b") == c.get("lfs/b") and calls == ["lfs/b"]  # one download
    for i in range(6):
        c.put(f"lfs/n{i}", b"z" * 900)
    assert sum(f.stat().st_size for f in (tmp_path / "cache").iterdir()) <= 4000
    c.delete(["lfs/n5"])
    assert inner.size("lfs/n5") is None and not (tmp_path / "cache" / "lfs_n5").exists()


def test_upstream_pull_mirror(tmp_path, backend):
    """A repo with [upstream] follows that host's branch: new commits there move the forge branch (a push for
    the forge's listeners, so deploy jobs run); a forge that's ahead or diverged is left alone."""
    from dulwich.objects import Blob as GBlob, Commit as GCommit, Tree as GTree
    from dulwich.repo import Repo

    from fileapi.upstream import parse_upstream

    up_path = tmp_path / "github.git"
    up = Repo.init_bare(str(up_path), mkdir=True)

    def commit_on(repo, parent: bytes, name: str, text: str) -> bytes:
        base = repo[repo[parent].tree]
        blob = GBlob.from_string(text.encode())
        tree = GTree()
        for item in base.items():
            tree.add(item.path, item.mode, item.sha)
        tree.add(name.encode(), 0o100644, blob.id)
        c = GCommit()
        c.tree, c.parents = tree.id, [parent]
        c.author = c.committer = b"x <x@x>"
        c.author_time = c.commit_time = 0
        c.author_timezone = c.commit_timezone = 0
        c.message = f"add {name}".encode()
        for o in (blob, tree, c):
            repo.object_store.add_object(o)
        return c.id

    toml = f'[upstream]\nurl = "{up_path}"\nbranches = ["main"]\ninterval = 30\n'
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge, upstream = client.app.state.forge, client.app.state.upstream
        upstream.stop()  # drive it by hand here
        pushes = []

        class Spy:
            def on_push(self, repo, updates):
                pushes.append((repo, updates))

        forge.listeners.append(Spy())
        forge.create("nandi.uk", "app")
        c1 = forge.commit_files("nandi.uk/app", "main", {".forge/jobs.toml": toml}, "jobs")["commit"].encode()
        r = forge.open("nandi.uk/app")
        try:
            for sha in list(r.object_store):
                up.object_store.add_object(r[sha])
        finally:
            r.close()
        up.refs[b"refs/heads/main"] = c1
        pushes.clear()

        # Nothing new upstream.
        assert upstream.sync("nandi.uk/app")[0]["status"] == "up to date" and not pushes

        # A commit lands upstream: the forge fast-forwards and tells its listeners (deploy jobs).
        c2 = commit_on(up, c1, "a.txt", "from github")
        up.refs[b"refs/heads/main"] = c2
        res = upstream.sync("nandi.uk/app")
        assert res[0]["status"] == "updated" and res[0]["sha"] == c2.decode(), res
        assert forge.branches("nandi.uk/app")["branches"]["main"] == c2.decode()
        assert pushes == [("nandi.uk/app", [("refs/heads/main", c1.decode(), c2.decode())])]
        assert forge.raw("nandi.uk/app", "a.txt")[0] == b"from github"
        assert upstream.sync("nandi.uk/app")[0]["status"] == "up to date" and len(pushes) == 1

        # The forge moved past the upstream: kept. Then the upstream diverges: kept too.
        c3 = forge.commit_files("nandi.uk/app", "main", {"b.txt": "b"}, "on the forge")["commit"]
        pushes.clear()
        res = upstream.sync("nandi.uk/app")
        assert res[0]["status"] == "kept" and "ahead" in res[0]["reason"], res
        up.refs[b"refs/heads/main"] = commit_on(up, c2, "c.txt", "elsewhere")
        res = upstream.sync("nandi.uk/app")
        assert res[0]["status"] == "kept" and "diverged" in res[0]["reason"], res
        assert forge.branches("nandi.uk/app")["branches"]["main"] == c3 and not pushes

        # run_once honours the interval; poke (the webhook endpoint) makes the next pass check again.
        upstream._due.clear()
        upstream.run_once(now=1000.0)
        assert upstream._due["nandi.uk/app"] == 1030.0
        assert client.post("/api/forge/repos/nandi.uk/app/upstream/sync").status_code == 202
        assert upstream._due["nandi.uk/app"] == 0
        assert client.post("/api/forge/repos/nope/nope/upstream/sync").status_code == 202

    assert parse_upstream("[[job]]\nname='a'\nrun='x'\n") is None
    for bad, msg in (("[upstream]\nbranches=['main']\n", "url is required"),
                     ("[upstream]\nurl='x'\ninterval=5\n", "interval"),
                     ("[upstream]\nurl='x'\ncolour='red'\n", "unknown upstream setting")):
        with pytest.raises(StorageError, match=msg):
            parse_upstream(bad)


def test_forge_url_survives_restart(tmp_path, backend):
    """Without FILEAPI_PUBLIC_URL the server learns its address from a signed-in request and keeps it, so a job
    a fresh server starts on its own (an [upstream] pull) still gets FORGE_URL."""
    db = backend.get("DATABASE_URL", "")
    with TestClient(app_for(tmp_path, database_url=db), base_url="https://forge.example") as client:
        client.app.state.forge.create("nandi.uk", "app")
        client.post("/api/forge/repos/nandi.uk/app/runs", json={}, headers=AUTH)
        assert client.app.state.jobs.base_url == "https://forge.example"
    with TestClient(app_for(tmp_path, database_url=db)) as client:
        assert client.app.state.jobs.base_url == "https://forge.example"


def test_release_changelog(tmp_path, backend):
    with TestClient(app_for(tmp_path, database_url=backend.get("DATABASE_URL", ""))) as client:
        forge = client.app.state.forge
        forge.create("nandi.uk", "cl")
        repo = "nandi.uk/cl"
        c1 = forge.commit_files(repo, "main", {"a": "1"}, "Initial import")["commit"]
        r = forge.open(repo)
        try:
            r.refs[b"refs/tags/v0.1.0"] = c1.encode()
        finally:
            r.close()
        c2 = forge.commit_files(repo, "main", {"a": "2"}, "feat(ui): dark mode\n\nbody")["commit"]
        c3 = forge.commit_files(repo, "main", {"a": "3"}, "fix: crash on empty repo")["commit"]
        c4 = forge.commit_files(repo, "main", {"a": "4"}, "Tidy the README")["commit"]
        base = "/api/forge/repos/nandi.uk/cl/releases"

        # A release that doesn't exist yet: the commits since the git tag v0.1.0, grouped.
        r = client.get(f"{base}/v0.2.0/changelog", headers=AUTH)
        assert r.status_code == 200, r.text
        log = r.json()
        assert log["previous"] == "v0.1.0" and log["previous_sha"] == c1 and log["target"] == c4
        assert [c["sha"] for c in log["commits"]] == [c4, c3, c2]
        notes = log["notes"]
        assert "### Features" in notes and "**ui:** dark mode" in notes and "### Fixes" in notes
        assert "### Other changes" in notes and "Tidy the README" in notes and "v0.1.0...v0.2.0" in notes
        assert f"/forge/nandi.uk/cl/-/commits/{c2}" in notes
        assert client.get(f"{base}/v0.2.0/changelog").status_code == 404  # private repo

        # Creating with generate_notes appends it to the given notes; the next release starts from this one.
        r = client.post(base, json={"tag": "v0.2.0", "notes": "Hand-written intro.", "generate_notes": True},
                        headers=AUTH)
        assert r.status_code == 201, r.text
        assert r.json()["notes"].startswith("Hand-written intro.\n\n## What's changed")
        c5 = forge.commit_files(repo, "main", {"a": "5"}, "Plain change")["commit"]
        log = client.get(f"{base}/v0.3.0/changelog", headers=AUTH).json()
        assert log["previous"] == "v0.2.0" and [c["sha"] for c in log["commits"]] == [c5]
        assert "### " not in log["notes"] and "- Plain change" in log["notes"]
        # An existing release's changelog stays fixed at its own commit; since= overrides the start.
        log = client.get(f"{base}/v0.2.0/changelog", params={"since": c3}, headers=AUTH).json()
        assert [c["sha"] for c in log["commits"]] == [c4]
        # No earlier tag or release: everything up to the commit.
        log = client.get(f"{base}/v0.1.0/changelog", headers=AUTH).json()
        assert log["previous"] is None and [c["sha"] for c in log["commits"]] == [c1]
