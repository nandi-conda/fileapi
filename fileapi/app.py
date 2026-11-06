"""FastAPI app: MCP at /mcp plus a REST API at /files.

Auth is atproto OAuth limited to one admin account (see fileapi.auth). MCP clients discover it from the
401 on /mcp and sign in in the browser; the access token they get also works for /files
(`Authorization: Bearer <token>` or `?token=`) and as the git Basic-auth password. Public repos clone
anonymously.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel
from mcp.server.transport_security import TransportSecuritySettings

from .deploy import KEY_ENV, JOBS_FILE, Jobs, RepoSecrets, SecretsUnavailable, parse_key
from .forge import SERVICES, DiskForgeStore, Forge, PgForgeStore, RepoPasses, owner_name, split_repo
from .lfs import DiskLfsStore, PgLfsStore, Spool, check_oid
from .releases import DiskReleaseStore, PgReleaseStore, Releases
from .mcp_server import build_mcp
from .storage import Conflict, Edit, NotFound, Storage, StorageError
from . import auth as oauth
from .auth import Auth
from .tokens import FileKV


class EditBody(BaseModel):
    old_text: str
    new_text: str


class EditsBody(BaseModel):
    edits: list[EditBody]


class MoveBody(BaseModel):
    destination: str
    overwrite: bool = False


class RepoSettingsBody(BaseModel):
    description: str | None = None
    default_branch: str | None = None
    public: bool | None = None
    name: str | None = None  # new repo name (it stays under the same owner)


class SecretBody(BaseModel):
    value: str


class RunBody(BaseModel):
    ref: str | None = None  # branch to run for (default: the default branch)
    job: str | None = None  # one job by name, whatever its branches say
    tag: str | None = None  # run the jobs for this pushed tag instead (their `tags` globs), as a tag push would


class NewReleaseBody(BaseModel):
    tag: str
    name: str = ""
    notes: str = ""
    ref: str | None = None  # commit to point at when the tag isn't in git (default: the default branch)
    prerelease: bool = False
    draft: bool = False


class ReleaseUpdateBody(BaseModel):
    tag: str | None = None  # rename
    name: str | None = None
    notes: str | None = None
    prerelease: bool | None = None
    draft: bool | None = None


class NewRepoBody(BaseModel):
    name: str
    description: str = ""
    default_branch: str = "main"
    public: bool = False


def _request_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    if scheme.lower() == "basic":
        # git sends the token as the Basic-auth password (any username): https://x:<token>@host/git/...
        try:
            _, _, password = base64.b64decode(value.strip()).decode().partition(":")
        except Exception:
            password = ""
        if password:
            return password
    return request.query_params.get("token") or None


def create_app(
    root: str | None = None,
    *,
    state_dir: str | None = None,
    preset_token: str | None = None,
    public_url: str | None = None,
    database_url: str | None = None,
    git_root: str | None = None,
    admin_did: str | None = None,
    atproto=None,
) -> FastAPI:
    root = root or os.environ.get("FILEAPI_ROOT", "./data")
    state_dir = state_dir or os.environ.get("FILEAPI_STATE_DIR", "./state")
    preset_token = preset_token if preset_token is not None else os.environ.get("FILEAPI_TOKEN") or None
    public_url = public_url or os.environ.get("FILEAPI_PUBLIC_URL") or None

    database_url = database_url if database_url is not None else os.environ.get("DATABASE_URL") or None
    backend = "postgres" if database_url else "disk"
    if database_url:
        # Hosts without a persistent disk (FastAPI Cloud + Neon/Supabase): files and token live in Postgres.
        from .storage_pg import PostgresKV, PostgresStorage

        storage = PostgresStorage(database_url)
        storage.init()
        kv = PostgresKV(storage)
        forge = Forge(PgForgeStore(storage.pool))
        lfs = PgLfsStore(storage.pool)
        release_store = PgReleaseStore(storage.pool)
    else:
        storage = Storage(root)
        storage.init()
        state = Path(state_dir).resolve()
        if state == storage.root or storage.root in state.parents:
            raise RuntimeError("FILEAPI_STATE_DIR must be outside FILEAPI_ROOT (it holds sign-in state)")
        kv = FileKV(state)
        git = Path(git_root or os.environ.get("FILEAPI_GIT_ROOT", "./repos")).resolve()
        if git == storage.root or storage.root in git.parents:
            raise RuntimeError("FILEAPI_GIT_ROOT must be outside FILEAPI_ROOT")
        forge = Forge(DiskForgeStore(git))
        lfs = DiskLfsStore(git)
        release_store = DiskReleaseStore(git)
    forge.store.init()
    lfs.init()
    release_store.init()
    releases = Releases(forge, release_store, int(os.environ.get("FILEAPI_RELEASE_MAX_ASSET", 2 * 1024 * 1024 * 1024)))
    passes = RepoPasses(kv)
    # Repo secrets are encrypted under FILEAPI_SECRETS_KEY; without it they're off (the rest still works).
    repo_secrets = RepoSecrets(kv, parse_key(os.environ.get(KEY_ENV)))
    jobs_on = os.environ.get("FILEAPI_JOBS", "on").strip().lower() not in ("0", "off", "false", "no")
    jobs = Jobs(forge, kv, repo_secrets, enabled=jobs_on, workers=int(os.environ.get("FILEAPI_JOB_WORKERS", 2)))
    jobs.passes = passes
    jobs.base_url = public_url.rstrip("/") if public_url else None  # else learned from requests (see below)
    # The admin is pinned by DID; the handle is only a login hint and display name.
    admin_did = admin_did or os.environ.get("FILEAPI_ADMIN_DID") or oauth.ADMIN_DID
    admin_handle = os.environ.get("FILEAPI_ADMIN_HANDLE") or (oauth.ADMIN_HANDLE if admin_did == oauth.ADMIN_DID else "")
    auth = Auth(kv, admin_did=admin_did, admin_handle=admin_handle, preset_token=preset_token, atproto=atproto)
    # Repos are owned by the signed-in account and live under its handle: /git/<handle>/<repo>.git.
    # Only the admin can sign in, so that is one namespace. Repos from before owners (or from an older
    # handle) move into it at startup and keep their old names as aliases, so old clone URLs still work.
    owner = owner_name(os.environ.get("FILEAPI_FORGE_OWNER") or admin_handle, admin_did)
    forge.migrate_to_owner(owner)

    def owner_of(did: str | None) -> str | None:
        return owner if did and did == auth.admin_did else None

    mcp = build_mcp(storage, public_url, forge, passes, owner, jobs, releases)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=int(os.environ.get("FILEAPI_MAX_BODY", 10 * 1024 * 1024)),
        # Requests carry a token, so Host-header DNS-rebinding checks are not needed.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="fileapi", version="1.6.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.auth = auth
    app.state.forge = forge
    app.state.jobs = jobs
    app.state.releases = releases

    def learn_base(request: Request) -> None:
        """Jobs need this server's public URL (FORGE_URL); without FILEAPI_PUBLIC_URL, take it from a
        signed-in request (a push, a run), which came in through the public address."""
        if not public_url:
            jobs.base_url = oauth.base_url(request, None)

    @app.middleware("http")
    async def mcp_auth(request: Request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            did = auth.identify(_request_token(request))
            if did is None:
                # MCP clients follow resource_metadata to our OAuth server and sign the user in.
                meta = oauth.resource_metadata_url(oauth.base_url(request, public_url))
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{meta}", scope="{oauth.SCOPE}"'},
                )
            request.scope.setdefault("state", {})["authed"] = True
            request.scope["state"]["did"] = did
            request.scope["state"]["owner"] = owner_of(did)
            learn_base(request)
        return await call_next(request)

    def require_token(request: Request) -> None:
        if auth.identify(_request_token(request)) is None:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(StorageError)
    async def storage_error(_req: Request, exc: StorageError):
        status = (404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict)
                  else 503 if isinstance(exc, SecretsUnavailable) else 400)
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/", include_in_schema=False)
    def index(request: Request):
        base = str(request.base_url).rstrip("/")
        proto = request.headers.get("x-forwarded-proto")
        if proto:
            base = proto.split(",")[0].strip() + base[base.index("://"):]
        who = auth.admin_handle or auth.admin_did
        status = (
            f"Sign in with atproto (Bluesky). Only <code>{who}</code> has access. Add the MCP endpoint below to "
            "your MCP client and it will open the sign-in page."
        )
        storage_note = (
            "Postgres (DATABASE_URL)"
            if backend == "postgres"
            else "local disk. On hosts without a persistent disk (e.g. FastAPI Cloud) files and sign-ins are lost on restart; set DATABASE_URL."
        )
        return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>fileapi</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:36rem;margin:10vh auto;padding:0 1rem;background:#fafafa;color:#1a1a1a}}
code{{background:#eee;padding:.1rem .3rem;border-radius:4px;word-break:break-all}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#eee}}code{{background:#2a2a2a}}}}</style></head>
<body><h1>fileapi</h1><p>A central file store for MCP clients.</p>
<p><b>Status:</b> {status}</p>
<p><b>Storage:</b> {storage_note}</p>
<p><b>MCP endpoint:</b> <code>{base}/mcp</code></p>
<p><b>Git repos:</b> <code>{base}/git/{owner}/&lt;repo&gt;.git</code> (public repos clone anonymously; for the rest install the <a href="/git-credential-fileapi">git credential helper</a> and git signs you in in the browser, or use the <code>repo_remote_url</code> MCP tool)</p>
<p><b>Repo browser:</b> <a href="/forge/">/forge/</a></p>
<p><b>REST API docs:</b> <a href="/docs">/docs</a></p></body></html>""")

    @app.get("/git-credential-fileapi", include_in_schema=False)
    def git_credential_helper():
        # The helper is one stdlib-only file, so it can be installed with curl on any machine with python3.
        return FileResponse(Path(__file__).with_name("git_credential.py"), media_type="text/x-python; charset=utf-8",
                            filename="git-credential-fileapi", content_disposition_type="inline")

    @app.get("/health")
    def health():
        return {"ok": True, "auth": "atproto", "admin": auth.admin_did, "storage": backend, "git": forge.store.backend}

    # ---- REST API ------------------------------------------------------
    authed = [Depends(require_token)]

    @app.get("/files", dependencies=authed)
    def list_root(recursive: bool = False):
        return [e.to_dict() for e in storage.list("", recursive)]

    @app.get("/files/{path:path}", dependencies=authed)
    def get_file(path: str, recursive: bool = False):
        """Return file contents as text, or a JSON listing if the path is a directory."""
        if storage.is_dir(path):
            return [e.to_dict() for e in storage.list(path, recursive)]
        return PlainTextResponse(storage.read(path))

    @app.put("/files/{path:path}", dependencies=authed)
    def put_file(path: str, content: str = Body(..., media_type="text/plain"), overwrite: bool = Query(True)):
        """Create (or overwrite) a file with the raw request body. ?overwrite=false to fail if it exists."""
        created, n = storage.write(path, content, overwrite=overwrite)
        return JSONResponse({"path": path, "created": created, "bytes": n}, status_code=201 if created else 200)

    @app.patch("/files/{path:path}", dependencies=authed)
    def patch_file(path: str, body: EditsBody):
        if not body.edits:
            raise StorageError("At least one edit is required")
        n = storage.edit(path, [Edit(e.old_text, e.new_text) for e in body.edits])
        return {"path": path, "edits": n}

    @app.post("/files/{path:path}:append", dependencies=authed)
    def append_file(path: str, content: str = Body(..., media_type="text/plain")):
        return {"path": path, "bytes": storage.append(path, content)}

    @app.post("/files/{path:path}:move", dependencies=authed)
    def move_file(path: str, body: MoveBody):
        storage.move(path, body.destination, body.overwrite)
        return {"path": body.destination}

    @app.delete("/files/{path:path}", dependencies=authed, status_code=204)
    def delete_file(path: str):
        storage.delete(path)

    # ---- Git smart HTTP ---------------------------------------------------
    # Clone/push with an OAuth access token (or a repo_remote_url pass) as the Basic-auth password (any
    # username), or an Authorization: Bearer header via `git -c http.extraHeader=...`.
    max_push = int(os.environ.get("FILEAPI_GIT_MAX_PUSH", 200 * 1024 * 1024))

    def git_auth(request: Request, project: str, repo: str, service: str) -> str:
        presented = _request_token(request)
        full = forge.canonical(f"{project}/{repo}")  # old names (aliases) are served as the repo they became
        if passes.check(presented, full):  # short-lived pass from the repo_remote_url tool
            return full
        # Public repos: anyone may clone and fetch without credentials. Pushing always needs a token.
        if service == "git-upload-pack" and not presented and forge.is_public(full):
            return full
        if auth.identify(presented) is None:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="fileapi git"'})
        return full

    @app.get("/git/{project}/{repo}/info/refs", include_in_schema=False)
    async def git_info_refs(request: Request, project: str, repo: str, service: str = ""):
        full = git_auth(request, project, repo, service)
        if service not in SERVICES:
            raise HTTPException(403, "Only the smart HTTP protocol is supported (git 1.6.6+)")
        data = await run_in_threadpool(forge.advertise, full, service)
        return Response(data, media_type=f"application/x-{service}-advertisement",
                        headers={"Cache-Control": "no-cache"})

    @app.post("/git/{project}/{repo}/{service}", include_in_schema=False)
    async def git_rpc(request: Request, project: str, repo: str, service: str):
        full = git_auth(request, project, repo, service)
        if service not in SERVICES:
            raise HTTPException(404, "Unknown git service")
        if request.headers.get("content-type") != f"application/x-{service}-request":
            raise HTTPException(415, "Invalid Content-Type")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > max_push:
                raise HTTPException(413, f"Request larger than FILEAPI_GIT_MAX_PUSH ({max_push} bytes)")
        if service == "git-receive-pack":
            learn_base(request)
        data = await run_in_threadpool(forge.rpc, full, service, bytes(body), request.headers.get("content-encoding"))
        return Response(data, media_type=f"application/x-{service}-result", headers={"Cache-Control": "no-cache"})

    # ---- Git LFS ----------------------------------------------------------
    # git-lfs derives its endpoint from the remote URL (<remote>.git/info/lfs) and authenticates the same
    # way as git (it asks the same credential helper). Download is allowed wherever clone is (public repos
    # anonymously); upload needs push access. Only the "basic" transfer adapter is offered.
    lfs_max = int(os.environ.get("FILEAPI_LFS_MAX_OBJECT", 2 * 1024 * 1024 * 1024))
    LFS_JSON = "application/vnd.git-lfs+json"

    def lfs_auth(request: Request, project: str, repo: str, upload: bool) -> tuple[str, str] | Response:
        try:
            full = git_auth(request, project, repo, "git-receive-pack" if upload else "git-upload-pack")
        except HTTPException as e:
            if e.status_code != 401:
                raise
            return JSONResponse({"message": "Credentials needed"}, status_code=401, media_type=LFS_JSON,
                                headers={"WWW-Authenticate": 'Basic realm="fileapi git"',
                                         "LFS-Authenticate": 'Basic realm="fileapi git"'})
        try:
            forge.open(full).close()
        except StorageError:
            return JSONResponse({"message": "Repository not found"}, status_code=404, media_type=LFS_JSON)
        return split_repo(full)

    @app.post("/git/{project}/{repo}/info/lfs/objects/batch", include_in_schema=False)
    async def lfs_batch(request: Request, project: str, repo: str):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"message": "Body must be JSON"}, status_code=422, media_type=LFS_JSON)
        op = body.get("operation")
        if op not in ("upload", "download"):
            return JSONResponse({"message": "operation must be upload or download"}, status_code=422, media_type=LFS_JSON)
        parts = lfs_auth(request, project, repo, op == "upload")
        if isinstance(parts, Response):
            return parts
        if "basic" not in (body.get("transfers") or ["basic"]):
            return JSONResponse({"message": "Only the basic transfer adapter is supported"}, status_code=422,
                                media_type=LFS_JSON)
        href_base = f"{oauth.base_url(request, public_url)}/git/{parts[0]}/{parts[1]}.git/info/lfs/objects"
        # The client already authenticated for the batch call; hand the same credentials to the actions.
        header = {"Authorization": request.headers["authorization"]} if request.headers.get("authorization") else {}
        out = []
        for obj in body.get("objects") or []:
            oid, size = str(obj.get("oid", "")), obj.get("size")
            item = {"oid": oid, "size": size}
            try:
                check_oid(oid)
                if not isinstance(size, int) or size < 0:
                    raise StorageError("size must be a non-negative integer")
            except StorageError as e:
                out.append({**item, "error": {"code": 422, "message": str(e)}})
                continue
            have = await run_in_threadpool(lfs.size, *parts, oid)
            action = {"href": f"{href_base}/{oid}", "header": header, "expires_in": 3600}
            if op == "download":
                if have is None:
                    item["error"] = {"code": 404, "message": "Object does not exist"}
                else:
                    item.update(size=have, authenticated=True, actions={"download": action})
            elif have is None:
                if size > lfs_max:
                    item["error"] = {"code": 422, "message": f"Larger than FILEAPI_LFS_MAX_OBJECT ({lfs_max} bytes)"}
                else:
                    item.update(authenticated=True, actions={"upload": {**action, "header": {
                        **header, "Content-Type": "application/octet-stream"}}})
            out.append(item)  # an upload the server already has gets no actions: nothing to send
        return JSONResponse({"transfer": "basic", "objects": out, "hash_algo": "sha256"}, media_type=LFS_JSON)

    @app.put("/git/{project}/{repo}/info/lfs/objects/{oid}", include_in_schema=False)
    async def lfs_upload(request: Request, project: str, repo: str, oid: str):
        parts = lfs_auth(request, project, repo, True)
        if isinstance(parts, Response):
            return parts
        check_oid(oid)
        spool = Spool(lfs_max)
        try:
            try:
                async for chunk in request.stream():
                    spool.write(chunk)
            except StorageError as e:
                return JSONResponse({"message": str(e)}, status_code=413, media_type=LFS_JSON)
            if spool.sha.hexdigest() != oid:
                return JSONResponse({"message": "Content does not match the object id (sha256)"}, status_code=422,
                                    media_type=LFS_JSON)
            spool.file.seek(0)
            await run_in_threadpool(lfs.put, *parts, oid, spool.size, spool.file)
        finally:
            spool.close()
        return Response(status_code=200)

    @app.get("/git/{project}/{repo}/info/lfs/objects/{oid}", include_in_schema=False)
    async def lfs_download(request: Request, project: str, repo: str, oid: str):
        parts = lfs_auth(request, project, repo, False)
        if isinstance(parts, Response):
            return parts
        size = await run_in_threadpool(lfs.size, *parts, check_oid(oid))
        if size is None:
            return JSONResponse({"message": "Object does not exist"}, status_code=404, media_type=LFS_JSON)
        return StreamingResponse(lfs.read(*parts, oid), media_type="application/octet-stream",
                                 headers={"Content-Length": str(size)})

    # File locking isn't supported, but git-lfs checks locks before every push; answering "no locks"
    # keeps it from printing a warning each time.
    @app.post("/git/{project}/{repo}/info/lfs/locks/verify", include_in_schema=False)
    def lfs_locks_verify(request: Request, project: str, repo: str):
        parts = lfs_auth(request, project, repo, True)
        if isinstance(parts, Response):
            return parts
        return JSONResponse({"ours": [], "theirs": []}, media_type=LFS_JSON)

    @app.get("/git/{project}/{repo}/info/lfs/locks", include_in_schema=False)
    def lfs_locks(request: Request, project: str, repo: str):
        parts = lfs_auth(request, project, repo, False)
        if isinstance(parts, Response):
            return parts
        return JSONResponse({"locks": []}, media_type=LFS_JSON)

    # ---- Forge JSON API (used by the web UI at /forge/) ---------
    # Public repos are readable by anyone; everything else needs a signed-in token. A private repo looks
    # exactly like a missing one to anonymous callers.
    def forge_viewer(request: Request) -> bool:
        return auth.identify(_request_token(request)) is not None

    def forge_repo(request: Request, project: str, repo: str) -> str:
        full = forge.canonical(f"{project}/{repo}")
        if not forge_viewer(request) and not forge.is_public(full):
            raise NotFound(f"No such repo: {full}")
        return full

    def repo_json(info) -> dict:
        return {"repo": info.full_name, "project": info.project, "name": info.name,
                "description": info.description, "created": str(info.created), "public": info.public}

    @app.get("/api/forge/me", tags=["forge"])
    def forge_me(request: Request):
        did = auth.identify(_request_token(request))
        return {"signed_in": did is not None, "did": did, "admin": auth.admin_handle or auth.admin_did,
                "owner": owner_of(did) if did else owner}

    @app.get("/api/forge/repos", tags=["forge"])
    def forge_repos(request: Request, project: str | None = None):
        signed_in = forge_viewer(request)
        repos = forge.list(project)
        return {"repos": [repo_json(i) for i in repos if signed_in or i.public]}

    @app.get("/api/forge/repos/{project}/{repo}", tags=["forge"])
    def forge_repo_info(request: Request, project: str, repo: str):
        full = forge_repo(request, project, repo)
        info = forge.info(full)
        base = oauth.base_url(request, public_url)
        return {**repo_json(info), **forge.branches(full), "clone_url": f"{base}/git/{full}.git"}

    @app.get("/api/forge/repos/{project}/{repo}/tree", tags=["forge"])
    def forge_tree(request: Request, project: str, repo: str, ref: str = "HEAD", path: str = ""):
        return forge.browse(forge_repo(request, project, repo), path, ref)

    # Code navigation (fileapi/codenav.py): a file's outline, and what the identifier at line/col refers to.
    @app.get("/api/forge/repos/{project}/{repo}/symbols", tags=["forge"])
    async def forge_symbols(request: Request, project: str, repo: str, path: str, ref: str = "HEAD"):
        full = forge_repo(request, project, repo)
        return await run_in_threadpool(forge.symbols, full, path, ref)

    @app.get("/api/forge/repos/{project}/{repo}/symbol", tags=["forge"])
    async def forge_symbol(request: Request, project: str, repo: str, path: str, line: int = Query(..., ge=1),
                           col: int = Query(..., ge=0), ref: str = "HEAD", references: bool = True):
        full = forge_repo(request, project, repo)
        return await run_in_threadpool(forge.symbol_at, full, path, line, col, ref, references)

    # "/commits", not "/log": ad blockers drop URLs ending in /log (tracking-beacon filter rules).
    @app.get("/api/forge/repos/{project}/{repo}/commits", tags=["forge"])
    @app.get("/api/forge/repos/{project}/{repo}/log", include_in_schema=False)  # old name, kept for clients
    def forge_log(request: Request, project: str, repo: str, ref: str = "HEAD", path: str | None = None,
                  limit: int = Query(50, ge=1, le=200)):
        return {"commits": forge.log(forge_repo(request, project, repo), ref, path, limit)}

    # Repo settings (signed-in admin only; the only identity that can sign in is the admin).
    def forge_admin(request: Request) -> str:
        me = owner_of(auth.identify(_request_token(request)))
        if me is None:
            raise HTTPException(401, "Sign in to change repos", headers={"WWW-Authenticate": "Bearer"})
        return me

    @app.post("/api/forge/repos", tags=["forge"], status_code=201)
    def forge_create_repo(request: Request, body: NewRepoBody):
        """Create a repo owned by the signed-in account: only the name is needed."""
        me = forge_admin(request)
        name = body.name.strip().strip("/")
        if "/" in name and name.split("/")[0] != me:
            raise StorageError(f"Repos are created under your handle ({me}); give just the repo name")
        info = forge.create(me, name.split("/")[-1], body.description, body.default_branch.strip() or "main")
        if body.public:
            forge.set_public(info.full_name, True)
        return forge_repo_info(request, info.project, info.name)

    @app.patch("/api/forge/repos/{project}/{repo}", tags=["forge"])
    def forge_update_repo(request: Request, project: str, repo: str, body: RepoSettingsBody):
        forge_admin(request)
        full = forge.canonical(f"{project}/{repo}")
        forge.open(full).close()  # 404 before changing anything
        if body.default_branch is not None:
            forge.set_default_branch(full, body.default_branch.strip())
        if body.description is not None:
            forge.set_description(full, body.description)
        if body.public is not None:
            forge.set_public(full, body.public)
        if body.name:
            new, owner_part = body.name.strip().strip("/"), full.split("/")[0]
            if "/" in new and new.split("/")[0] != owner_part:
                raise StorageError(f"Repos stay under {owner_part}; give just the new repo name")
            full = forge.rename(full, f"{owner_part}/{new.split('/')[-1]}")  # the old name becomes an alias
        return forge_repo_info(request, *full.split("/"))

    @app.delete("/api/forge/repos/{project}/{repo}", tags=["forge"], status_code=204)
    def forge_delete_repo(request: Request, project: str, repo: str):
        forge_admin(request)
        forge.delete(f"{project}/{repo}")
        return Response(status_code=204)

    # ---- Releases -----------------------------------------------------------
    # Read like the repo (public: anyone; private: signed in, or a signed download link); write as the
    # admin or with a pass for the repo (repo_remote_url, or FORGE_TOKEN inside a deploy job).
    def release_reader(request: Request, project: str, repo: str) -> tuple[str, bool]:
        """(repo, whether the caller may see drafts)."""
        full = forge.canonical(f"{project}/{repo}")
        token = _request_token(request)
        if passes.check(token, full) or forge_viewer(request):
            forge.open(full).close()
            return full, True
        if not forge.is_public(full):
            raise NotFound(f"No such repo: {full}")
        return full, False

    def release_writer(request: Request, project: str, repo: str) -> str:
        full = forge.canonical(f"{project}/{repo}")
        if not passes.check(_request_token(request), full):
            forge_admin(request)
        forge.open(full).close()
        return full

    def with_links(request: Request, full: str, rel: dict) -> dict:
        base = f"{oauth.base_url(request, public_url)}/api/forge/repos/{full}/releases"
        private = not forge.is_public(full)
        for a in rel["assets"]:
            path = f"{full}/{rel['tag']}/{a['name']}"
            url = f"{base}/{quote(rel['tag'])}/assets/{quote(a['name'])}"
            a["download_url"] = f"{url}?dl={passes.sign_link(path)}" if private else url
        return rel

    @app.get("/api/forge/repos/{project}/{repo}/releases", tags=["forge"])
    def forge_releases(request: Request, project: str, repo: str):
        full, drafts = release_reader(request, project, repo)
        return {"releases": [with_links(request, full, r) for r in releases.list(full, drafts)],
                "can_edit": drafts and (passes.check(_request_token(request), full)
                                        or owner_of(auth.identify(_request_token(request))) is not None),
                "max_asset": releases.max_asset}

    @app.post("/api/forge/repos/{project}/{repo}/releases", tags=["forge"], status_code=201)
    def forge_create_release(request: Request, project: str, repo: str, body: NewReleaseBody):
        """Create a release. If the tag exists in git it points at that commit, else at `ref`'s."""
        full = release_writer(request, project, repo)
        rel = releases.create(full, body.tag, body.name, body.notes, body.ref, body.prerelease, body.draft)
        return with_links(request, full, rel)

    @app.get("/api/forge/repos/{project}/{repo}/releases/{tag}", tags=["forge"])
    def forge_release(request: Request, project: str, repo: str, tag: str):
        """One release; `latest` is the newest that isn't a draft or prerelease."""
        full, drafts = release_reader(request, project, repo)
        return with_links(request, full, releases.get(full, tag, drafts))

    @app.patch("/api/forge/repos/{project}/{repo}/releases/{tag}", tags=["forge"])
    def forge_update_release(request: Request, project: str, repo: str, tag: str, body: ReleaseUpdateBody):
        full = release_writer(request, project, repo)
        rel = releases.update(full, tag, name=body.name, notes=body.notes, prerelease=body.prerelease,
                              draft=body.draft, new_tag=body.tag)
        return with_links(request, full, rel)

    @app.delete("/api/forge/repos/{project}/{repo}/releases/{tag}", tags=["forge"], status_code=204)
    def forge_delete_release(request: Request, project: str, repo: str, tag: str):
        releases.delete(release_writer(request, project, repo), tag)
        return Response(status_code=204)

    @app.put("/api/forge/repos/{project}/{repo}/releases/{tag}/assets/{name}", tags=["forge"])
    async def forge_upload_asset(request: Request, project: str, repo: str, tag: str, name: str,
                                 offset: int = Query(0, ge=0), final: bool = True, sha256: str | None = None,
                                 create: bool = False):
        """Upload an asset as the raw request body (`curl -T file`). Big files can come in pieces: the first
        with offset=0, the next with offset=<bytes sent so far>, all but the last with final=false.
        create=true makes the release first if it doesn't exist."""
        full = await run_in_threadpool(release_writer, request, project, repo)
        if create and offset == 0:
            try:
                await run_in_threadpool(releases.get, full, tag)
            except NotFound:
                try:
                    await run_in_threadpool(releases.create, full, tag)
                except Conflict:
                    pass  # made by a parallel upload meanwhile
        limit = releases.max_asset - offset
        spool = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, prefix="release-")
        try:
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"Asset larger than FILEAPI_RELEASE_MAX_ASSET ({releases.max_asset} bytes)")
                spool.write(chunk)
            spool.seek(0)
            asset = await run_in_threadpool(lambda: releases.upload(
                full, tag, name, spool, size, offset=offset, final=final,
                content_type=request.headers.get("content-type"), sha256=sha256))
        finally:
            spool.close()
        return JSONResponse(asset, status_code=201 if offset == 0 and final else 200)

    @app.get("/api/forge/repos/{project}/{repo}/releases/{tag}/assets/{name}", tags=["forge"])
    def forge_download_asset(request: Request, project: str, repo: str, tag: str, name: str, dl: str | None = None):
        """Download an asset. Private repos: sign in, or use the asset's download_url (a signed link)."""
        full = forge.canonical(f"{project}/{repo}")
        if dl is not None and passes.check_link(dl, f"{full}/{tag}/{name}"):
            drafts = True
        else:
            full, drafts = release_reader(request, project, repo)
        asset, data = releases.open_asset(full, tag, name, drafts)
        disposition = "attachment; filename*=UTF-8''" + quote(asset["name"])
        return StreamingResponse(data, media_type=asset["content_type"] or "application/octet-stream",
                                 headers={"Content-Length": str(asset["size"]), "Content-Disposition": disposition,
                                          "X-Checksum-Sha256": asset["sha256"] or "", "Cache-Control": "no-cache"})

    @app.delete("/api/forge/repos/{project}/{repo}/releases/{tag}/assets/{name}", tags=["forge"], status_code=204)
    def forge_delete_asset(request: Request, project: str, repo: str, tag: str, name: str):
        releases.delete_asset(release_writer(request, project, repo), tag, name)
        return Response(status_code=204)

    # Secrets (write-only: names and dates come back, never values) and deploy jobs. Admin only, runs too,
    # since a job's output can say things about the deploy target.
    def admin_repo(request: Request, project: str, repo: str) -> str:
        forge_admin(request)
        full = forge.canonical(f"{project}/{repo}")
        forge.open(full).close()
        return full

    @app.get("/api/forge/repos/{project}/{repo}/secrets", tags=["forge"])
    def forge_secrets(request: Request, project: str, repo: str):
        full = admin_repo(request, project, repo)
        # inherited: the owner's user secrets (names only); overridden = this repo has its own of that name.
        return {"enabled": repo_secrets.enabled, "key_env": KEY_ENV, "secrets": repo_secrets.list(full),
                "inherited": repo_secrets.inherited(full)}

    @app.put("/api/forge/repos/{project}/{repo}/secrets/{name}", tags=["forge"])
    def forge_set_secret(request: Request, project: str, repo: str, name: str, body: SecretBody):
        """Create or replace a secret. The value can't be read back; jobs get it as an env var."""
        full = admin_repo(request, project, repo)
        out = repo_secrets.set(full, name, body.value)
        return JSONResponse({"name": out["name"], "updated": out["updated"]}, status_code=201 if out["created"] else 200)

    @app.delete("/api/forge/repos/{project}/{repo}/secrets/{name}", tags=["forge"], status_code=204)
    def forge_delete_secret(request: Request, project: str, repo: str, name: str):
        repo_secrets.delete(admin_repo(request, project, repo), name)
        return Response(status_code=204)

    # User secrets: every repo the signed-in account owns gets them; a repo secret of the same name wins.
    @app.get("/api/forge/user/secrets", tags=["forge"])
    def forge_user_secrets(request: Request):
        me = forge_admin(request)
        return {"enabled": repo_secrets.enabled, "key_env": KEY_ENV, "owner": me,
                "secrets": repo_secrets.list_user(me)}

    @app.put("/api/forge/user/secrets/{name}", tags=["forge"])
    def forge_set_user_secret(request: Request, name: str, body: SecretBody):
        """Create or replace a user secret, which jobs of all your repos get (unless a repo sets the same name)."""
        out = repo_secrets.set_user(forge_admin(request), name, body.value)
        return JSONResponse({"name": out["name"], "updated": out["updated"]}, status_code=201 if out["created"] else 200)

    @app.delete("/api/forge/user/secrets/{name}", tags=["forge"], status_code=204)
    def forge_delete_user_secret(request: Request, name: str):
        repo_secrets.delete_user(forge_admin(request), name)
        return Response(status_code=204)

    @app.get("/api/forge/repos/{project}/{repo}/runs", tags=["forge"])
    def forge_runs(request: Request, project: str, repo: str, limit: int = Query(30, ge=1, le=30)):
        full = admin_repo(request, project, repo)
        return {"enabled": jobs.enabled, "jobs_file": JOBS_FILE, "runs": jobs.list_runs(full, limit)}

    @app.get("/api/forge/repos/{project}/{repo}/runs/{run_id}", tags=["forge"])
    def forge_run(request: Request, project: str, repo: str, run_id: str):
        return jobs.get_run(admin_repo(request, project, repo), run_id)

    @app.post("/api/forge/repos/{project}/{repo}/runs", tags=["forge"], status_code=202)
    def forge_start_run(request: Request, project: str, repo: str, body: RunBody | None = None):
        """Run the jobs for a branch's current commit now (all that match it, or one by name)."""
        full = admin_repo(request, project, repo)
        learn_base(request)
        body = body or RunBody()
        if body.tag:
            tags = forge.branches(full).get("tags", {})
            if body.tag not in tags:
                raise NotFound(f"No such tag: {body.tag}")
            sha = jobs._commit_of(full, tags[body.tag])
            return {"runs": jobs.start(full, "", sha, trigger="manual", job=body.job or None, tag=body.tag)}
        branch = (body.ref or forge.branches(full).get("default") or "main").strip()
        heads = forge.branches(full).get("branches", {})
        if branch not in heads:
            raise NotFound(f"No such branch: {branch}")
        return {"runs": jobs.start(full, branch, heads[branch], trigger="manual", job=body.job or None)}

    # ---- Web UI (Qt for WebAssembly build in fileapi/static/forge) --------
    ui_dir = Path(__file__).parent / "static" / "forge"
    ui_types = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".wasm": "application/wasm",
                ".svg": "image/svg+xml", ".css": "text/css"}

    @app.get("/forge", include_in_schema=False)
    def forge_ui_root():
        return RedirectResponse("/forge/")

    @app.get("/forge/{name:path}", include_in_schema=False)
    def forge_ui(request: Request, name: str = ""):
        name = name or "index.html"
        if "/" in name or name.startswith(".") or Path(name).suffix not in ui_types:
            name = "index.html"  # deep links like /forge/<project>/<repo> load the app, which routes itself
        path = ui_dir / name
        headers = {"Cache-Control": "no-cache"}
        # Large assets are committed gzipped only; serve them as-is to browsers that accept gzip.
        gz = path.with_name(path.name + ".gz")
        if gz.is_file() and "gzip" in request.headers.get("accept-encoding", ""):
            with gz.open("rb") as f:  # gzip's trailer holds the uncompressed size, for the page's progress bar
                f.seek(-4, 2)
                size = int.from_bytes(f.read(4), "little")
            return FileResponse(gz, media_type=ui_types[path.suffix],
                                headers={**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding",
                                         "X-Uncompressed-Length": str(size)})
        if path.is_file():
            return FileResponse(path, media_type=ui_types[path.suffix], headers=headers)
        if gz.is_file():
            return Response(gzip.decompress(gz.read_bytes()), media_type=ui_types[path.suffix], headers=headers)
        raise HTTPException(404, "Web UI not built: run `just ui`")

    oauth.install(app, auth, public_url)

    # Mount last so the routes above take precedence.
    app.mount("/", mcp_app)
    return app
