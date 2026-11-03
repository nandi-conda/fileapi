"""FastAPI app: MCP at /mcp plus a REST API at /files.

Auth: MCP clients sign in through OAuth (Google "Sign in with Google" + email allowlist), handled by
fileapi.auth. REST callers send the same OAuth access token, or the optional static FILEAPI_TOKEN.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from .auth import SCOPE, AuthConfig, GoogleOAuthProvider, IdTokenVerifier
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


def _env_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("FILEAPI_ALLOWED_EMAILS", "").split(",") if e.strip()}


def create_app(
    root: str | None = None,
    *,
    public_url: str | None = None,
    google_client_id: str | None = None,
    allowed_emails: set[str] | None = None,
    static_token: str | None = None,
    state_dir: str | None = None,
    verify_id_token: IdTokenVerifier | None = None,
) -> FastAPI:
    root = root or os.environ.get("FILEAPI_ROOT", "./data")
    public_url = (public_url or os.environ.get("FILEAPI_PUBLIC_URL", "")).rstrip("/")
    google_client_id = google_client_id if google_client_id is not None else os.environ.get("GOOGLE_CLIENT_ID", "")
    allowed_emails = allowed_emails if allowed_emails is not None else _env_emails()
    static_token = static_token if static_token is not None else os.environ.get("FILEAPI_TOKEN") or None
    state_dir = state_dir or os.environ.get("FILEAPI_STATE_DIR", "./state")

    if not public_url:
        raise RuntimeError("FILEAPI_PUBLIC_URL is required (the https URL clients use to reach this server)")
    if not google_client_id and not static_token:
        raise RuntimeError("Set GOOGLE_CLIENT_ID (for Google sign-in) and/or FILEAPI_TOKEN (static bearer token)")
    if google_client_id and not allowed_emails:
        raise RuntimeError("FILEAPI_ALLOWED_EMAILS is empty, so nobody could sign in")

    storage = Storage(root)
    storage.init()
    if Path(state_dir).resolve() == storage.root or storage.root in Path(state_dir).resolve().parents:
        raise RuntimeError("FILEAPI_STATE_DIR must be outside FILEAPI_ROOT (it holds auth tokens)")

    provider = GoogleOAuthProvider(
        AuthConfig(
            public_url=public_url,
            google_client_id=google_client_id,
            allowed_emails=allowed_emails,
            state_dir=Path(state_dir),
            static_token=static_token,
            verify_id_token=verify_id_token,
        )
    )
    auth_settings = AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        validate_token_resource=False,
        required_scopes=[SCOPE],
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    mcp = build_mcp(storage, provider, auth_settings)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=int(os.environ.get("FILEAPI_MAX_BODY", 10 * 1024 * 1024)),
        # Every request carries a bearer token, so Host-header DNS-rebinding checks are not needed.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="fileapi", version="1.1.0", lifespan=lifespan)
    app.state.storage = storage
    app.state.auth = provider

    bearer = HTTPBearer(auto_error=False)

    async def require_token(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
        if not creds or not await provider.load_access_token(creds.credentials):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(StorageError)
    async def storage_error(_req: Request, exc: StorageError):
        status = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 400
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/health")
    def health():
        return {"ok": True}

    # ---- sign-in pages (the OAuth endpoints themselves come from the MCP SDK) ----
    @app.get("/oauth/login", include_in_schema=False)
    async def login(state: str):
        pending = provider.store.get("pending", state)
        client = await provider.get_client(pending["client_id"]) if pending else None
        status, body = provider.login_page(state, client)
        return HTMLResponse(body, status_code=status, headers={"Referrer-Policy": "no-referrer"})

    @app.post("/oauth/google/callback", include_in_schema=False)
    async def google_callback(state: str = Form(...), credential: str = Form("")):
        url, page = provider.google_callback(state, credential)
        if url:
            return RedirectResponse(url, status_code=303)
        return HTMLResponse(page[1], status_code=page[0])

    @app.post("/oauth/deny", include_in_schema=False)
    async def deny(state: str = Form(...)):
        url, page = provider.deny(state)
        if url:
            return RedirectResponse(url, status_code=303)
        return HTMLResponse(page[1], status_code=page[0])

    # ---- REST API ------------------------------------------------------
    auth = [Depends(require_token)]

    @app.get("/files", dependencies=auth)
    def list_root(recursive: bool = False):
        return [e.to_dict() for e in storage.list("", recursive)]

    @app.get("/files/{path:path}", dependencies=auth)
    def get_file(path: str, recursive: bool = False):
        """Return file contents as text, or a JSON listing if the path is a directory."""
        if storage.resolve(path).is_dir():
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

    # Mount last so the routes above take precedence. It serves /mcp, /authorize, /token,
    # /register, /revoke and the /.well-known OAuth metadata.
    app.mount("/", mcp_app)
    return app
