"""FastAPI app: MCP at /mcp plus a REST API at /files, behind one bearer token."""

from __future__ import annotations

import contextlib
import hmac
import os

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from mcp.server.transport_security import TransportSecuritySettings

from .mcp_server import build_mcp
from .storage import Conflict, Edit, NotFound, Storage, StorageError


class EditBody(BaseModel):
    old_text: str
    new_text: str


class EditsBody(BaseModel):
    edits: list[EditBody]


class MoveBody(BaseModel):
    destination: str
    overwrite: bool = False



def create_app(root: str | None = None, token: str | None = None) -> FastAPI:
    root = root or os.environ.get("FILEAPI_ROOT", "./data")
    token = token if token is not None else os.environ.get("FILEAPI_TOKEN", "")
    if not token and os.environ.get("FILEAPI_ALLOW_NO_AUTH") != "1":
        raise RuntimeError("FILEAPI_TOKEN is not set. Set it, or FILEAPI_ALLOW_NO_AUTH=1 for local testing.")

    storage = Storage(root)
    storage.init()
    mcp = build_mcp(storage)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=int(os.environ.get("FILEAPI_MAX_BODY", 10 * 1024 * 1024)),
        # Auth is a bearer token, so Host-header DNS-rebinding checks are not needed.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="fileapi", version="1.0.0", lifespan=lifespan)
    app.state.storage = storage

    def token_ok(value: str | None) -> bool:
        if not token:
            return True
        return value is not None and hmac.compare_digest(value.encode(), token.encode())

    # MCP is a mounted Starlette app, so guard it with middleware.
    @app.middleware("http")
    async def mcp_auth(request: Request, call_next):
        if request.url.path.rstrip("/") == "/mcp":
            header = request.headers.get("authorization", "")
            scheme, _, value = header.partition(" ")
            if scheme.lower() != "bearer" or not token_ok(value.strip()):
                return JSONResponse(
                    {"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    bearer = HTTPBearer(auto_error=False)

    def require_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if not token_ok(creds.credentials if creds else None):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(StorageError)
    async def storage_error(_req: Request, exc: StorageError):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/health")
    def health():
        return {"ok": True}

    # ---- REST API ------------------------------------------------------
    auth = [Depends(require_token)]

    @app.get("/files", dependencies=auth)
    def list_root(recursive: bool = False):
        return [e.to_dict() for e in storage.list("", recursive)]

    @app.get("/files/{path:path}", dependencies=auth)
    def get_file(path: str, recursive: bool = False):
        """Return file contents as text, or a JSON listing if the path is a directory."""
        target = storage.resolve(path)
        if target.is_dir():
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
