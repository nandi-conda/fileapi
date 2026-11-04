"""FastAPI app: MCP at /mcp plus a REST API at /files.

Auth is atproto OAuth limited to one admin account (see fileapi.auth). MCP clients discover it from the
401 on /mcp and sign in in the browser; the access token they get also works for /files
(`Authorization: Bearer <token>` or `?token=`) and as the git Basic-auth password. Public repos clone
anonymously.
"""

from __future__ import annotations

import base64
import contextlib
import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel
from mcp.server.transport_security import TransportSecuritySettings

from .forge import SERVICES, DiskForgeStore, Forge, PgForgeStore, RepoPasses, split_repo
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
    forge.store.init()
    passes = RepoPasses(kv)
    # The admin is pinned by DID; the handle is only a login hint and display name.
    admin_did = admin_did or os.environ.get("FILEAPI_ADMIN_DID") or oauth.ADMIN_DID
    admin_handle = os.environ.get("FILEAPI_ADMIN_HANDLE") or (oauth.ADMIN_HANDLE if admin_did == oauth.ADMIN_DID else "")
    auth = Auth(kv, admin_did=admin_did, admin_handle=admin_handle, preset_token=preset_token, atproto=atproto)

    mcp = build_mcp(storage, public_url, forge, passes)
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

    app = FastAPI(title="fileapi", version="1.4.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.auth = auth
    app.state.forge = forge

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
            request.scope["state"]["scope"] = ""
            request.scope["state"]["did"] = did
        return await call_next(request)

    def require_token(request: Request) -> None:
        if auth.identify(_request_token(request)) is None:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(StorageError)
    async def storage_error(_req: Request, exc: StorageError):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
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
<p><b>Git repos:</b> <code>{base}/git/&lt;project&gt;/&lt;repo&gt;.git</code> (public repos clone anonymously; for the rest use the <code>repo_remote_url</code> MCP tool)</p>
<p><b>REST API docs:</b> <a href="/docs">/docs</a></p></body></html>""")

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
        full = "/".join(split_repo(f"{project}/{repo}"))
        if passes.check(presented, full):  # short-lived pass from the repo_remote_url tool
            return full
        # Public repos: anyone may clone and fetch without credentials. Pushing always needs a token.
        if service == "git-upload-pack" and not presented and forge.is_public(full):
            return full
        scope = auth.scope(presented)
        if scope is None:
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="fileapi git"'})
        if scope and scope != full.split("/")[0]:
            raise HTTPException(403, f"This token cannot access project {project}")
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
        data = await run_in_threadpool(forge.rpc, full, service, bytes(body), request.headers.get("content-encoding"))
        return Response(data, media_type=f"application/x-{service}-result", headers={"Cache-Control": "no-cache"})

    oauth.install(app, auth, public_url)

    # Mount last so the routes above take precedence.
    app.mount("/", mcp_app)
    return app
