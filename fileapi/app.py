"""FastAPI app: MCP at /mcp plus a REST API at /files.

Auth is trust-on-first-use (see fileapi.tokens): the first client calls the claim_server tool and gets
the token. Tools and /files then need it, sent as `Authorization: Bearer <token>` or `?token=<token>`.
A /mcp request without a token can connect and list tools, but every tool except claim_server refuses.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from mcp.server.transport_security import TransportSecuritySettings

from .mcp_server import build_mcp
from .storage import Conflict, Edit, NotFound, Storage, StorageError
from .tokens import FileKV, TokenStore


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
    return request.query_params.get("token") or None


def create_app(
    root: str | None = None,
    *,
    state_dir: str | None = None,
    preset_token: str | None = None,
    public_url: str | None = None,
    database_url: str | None = None,
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
        tokens = TokenStore(PostgresKV(storage), preset_token)
    else:
        storage = Storage(root)
        storage.init()
        state = Path(state_dir).resolve()
        if state == storage.root or storage.root in state.parents:
            raise RuntimeError("FILEAPI_STATE_DIR must be outside FILEAPI_ROOT (it holds the token)")
        tokens = TokenStore(FileKV(state), preset_token)

    mcp = build_mcp(storage, tokens, public_url)
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

    app = FastAPI(title="fileapi", version="1.2.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.tokens = tokens

    @app.middleware("http")
    async def mcp_auth(request: Request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            presented = _request_token(request)
            authed = tokens.check(presented)
            if presented and not authed:
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # No token: the connection stays up (so the claiming client doesn't break mid-session),
            # but every tool except claim_server refuses until the client reconnects with the token.
            request.scope.setdefault("state", {})["authed"] = authed
        return await call_next(request)

    def require_token(request: Request) -> None:
        if not tokens.check(_request_token(request)):
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
        claimed = tokens.claimed()
        status = (
            "Claimed. Connect with the access token."
            if claimed
            else "Not claimed yet. Connect an MCP client with no credentials and call <code>claim_server</code>."
        )
        storage_note = (
            "Postgres (DATABASE_URL)"
            if backend == "postgres"
            else "local disk. On hosts without a persistent disk (e.g. FastAPI Cloud) files and the token are lost on restart; set DATABASE_URL."
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
<p><b>REST API docs:</b> <a href="/docs">/docs</a></p></body></html>""")

    @app.get("/health")
    def health():
        return {"ok": True, "claimed": tokens.claimed(), "storage": backend}

    # ---- REST API ------------------------------------------------------
    auth = [Depends(require_token)]

    @app.get("/files", dependencies=auth)
    def list_root(recursive: bool = False):
        return [e.to_dict() for e in storage.list("", recursive)]

    @app.get("/files/{path:path}", dependencies=auth)
    def get_file(path: str, recursive: bool = False):
        """Return file contents as text, or a JSON listing if the path is a directory."""
        if storage.is_dir(path):
            return [e.to_dict() for e in storage.list(path, recursive)]
        return PlainTextResponse(storage.read(path))

    @app.put("/files/{path:path}", dependencies=auth)
    def put_file(path: str, content: str = Body(..., media_type="text/plain"), overwrite: bool = Query(True)):
        """Create (or overwrite) a file with the raw request body. ?overwrite=false to fail if it exists."""
        created, n = storage.write(path, content, overwrite=overwrite)
        return JSONResponse({"path": path, "created": created, "bytes": n}, status_code=201 if created else 200)

    @app.patch("/files/{path:path}", dependencies=auth)
    def patch_file(path: str, body: EditsBody):
        if not body.edits:
            raise StorageError("At least one edit is required")
        n = storage.edit(path, [Edit(e.old_text, e.new_text) for e in body.edits])
        return {"path": path, "edits": n}

    @app.post("/files/{path:path}:append", dependencies=auth)
    def append_file(path: str, content: str = Body(..., media_type="text/plain")):
        return {"path": path, "bytes": storage.append(path, content)}

    @app.post("/files/{path:path}:move", dependencies=auth)
    def move_file(path: str, body: MoveBody):
        storage.move(path, body.destination, body.overwrite)
        return {"path": body.destination}

    @app.delete("/files/{path:path}", dependencies=auth, status_code=204)
    def delete_file(path: str):
        storage.delete(path)

    # Mount last so the routes above take precedence.
    app.mount("/", mcp_app)
    return app
