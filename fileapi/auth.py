"""OAuth 2.1 authorization server for MCP clients, using Google sign-in plus an email allowlist.

Google is used through the "Sign in with Google" drop-in button (Google Identity Services), so only a
Google OAuth *client ID* is needed: no client secret and no redirect URI, just an authorized JavaScript origin.

Flow:
  MCP client --/authorize--> our sign-in page with the Google button
  button --(ID token, bound to this request by nonce)--> POST /oauth/google/callback
  ID token verified with Google's public keys, email checked against allowlist --> our auth code --> client redirect_uri
  MCP client --/token--> our access + refresh tokens (opaque, stored hashed in SQLite)
"""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

from typing import Callable

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE = "files"
CODE_TTL = 300
PENDING_TTL = 600


GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# (id_token, expected_nonce) -> verified email, or None
IdTokenVerifier = Callable[[str, str], "str | None"]


def google_id_token_verifier(client_id: str, certs_url: str = GOOGLE_CERTS_URL) -> IdTokenVerifier:
    jwks = jwt.PyJWKClient(certs_url, cache_keys=True)

    def verify(token: str, nonce: str) -> str | None:
        try:
            key = jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], audience=client_id, options={"require": ["exp", "iss", "aud"]}
            )
        except jwt.PyJWTError:
            return None
        if claims.get("iss") not in GOOGLE_ISSUERS:
            return None
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            return None
        if claims.get("email_verified") not in (True, "true"):
            return None
        return claims.get("email")

    return verify


@dataclass
class AuthConfig:
    public_url: str  # e.g. https://files.example.com (no trailing slash)
    google_client_id: str
    allowed_emails: set[str]
    state_dir: Path
    static_token: str | None = None  # optional extra bearer token for scripts
    access_ttl: int = 3600
    refresh_ttl: int = 30 * 24 * 3600
    verify_id_token: IdTokenVerifier | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.verify_id_token is None:
            self.verify_id_token = google_id_token_verifier(self.google_client_id)

    def allowed(self, email: str | None) -> bool:
        return bool(email) and email.strip().lower() in self.allowed_emails


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Store:
    """Tiny SQLite key/value store with expiry, one table per kind."""

    KINDS = ("clients", "pending", "codes", "access", "refresh")

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            for k in self.KINDS:
                self._db.execute(f"CREATE TABLE IF NOT EXISTS {k} (key TEXT PRIMARY KEY, value TEXT, expires REAL)")

    def put(self, kind: str, key: str, value: dict, expires: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                f"INSERT OR REPLACE INTO {kind} VALUES (?, ?, ?)", (key, json.dumps(value), expires)
            )

    def get(self, kind: str, key: str) -> dict | None:
        with self._lock:
            row = self._db.execute(f"SELECT value, expires FROM {kind} WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        if row[1] is not None and row[1] < time.time():
            self.delete(kind, key)
            return None
        return json.loads(row[0])

    def pop(self, kind: str, key: str) -> dict | None:
        v = self.get(kind, key)
        if v is not None:
            self.delete(kind, key)
        return v

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self._db.execute(f"DELETE FROM {kind} WHERE key = ?", (key,))

    def delete_where(self, kind: str, field_name: str, value: str) -> None:
        with self._lock:
            self._db.execute(f"DELETE FROM {kind} WHERE json_extract(value, ?) = ?", (f"$.{field_name}", value))


class GoogleOAuthProvider:
    """Implements mcp's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, config: AuthConfig):
        self.config = config
        self.store = _Store(config.state_dir / "auth.db")

    # -- clients (dynamic registration) -----------------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.put("clients", client_info.client_id, client_info.model_dump(mode="json"))

    # -- authorize: park the request, show a consent page --------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self.config.google_client_id:
            raise AuthorizeError(error="server_error", error_description="Google sign-in is not configured")
        state = secrets.token_urlsafe(32)
        self.store.put(
            "pending",
            state,
            {"client_id": client.client_id, "params": params.model_dump(mode="json")},
            time.time() + PENDING_TTL,
        )
        return f"{self.config.public_url}/oauth/login?{urlencode({'state': state})}"

    @staticmethod
    def _nonce(state: str) -> str:
        return hashlib.sha256(b"fileapi-nonce:" + state.encode()).hexdigest()

    def login_page(self, state: str, client: OAuthClientInformationFull | None) -> tuple[int, str]:
        pending = self.store.get("pending", state)
        if not pending or not client:
            return 400, _page("Sign-in expired", "<p>This sign-in link has expired. Start again from your app.</p>")
        name = html.escape(client.client_name or client.client_id)
        redirect = html.escape(str(pending["params"]["redirect_uri"]))
        st = html.escape(state)
        body = f"""
<p><b>{name}</b> wants to read and change files in this file store.</p>
<p class=muted>After you sign in it will be sent back to:<br><code>{redirect}</code></p>
<div id=g_id_onload data-client_id="{html.escape(self.config.google_client_id)}" data-nonce="{self._nonce(state)}"
     data-callback=onGoogle data-auto_prompt=false data-ux_mode=popup data-context=signin></div>
<div class=g_id_signin data-type=standard data-theme=filled_blue data-size=large data-text=continue_with></div>
<form id=done method=post action="/oauth/google/callback">
  <input type=hidden name=state value="{st}"><input type=hidden name=credential id=credential>
</form>
<form method=post action="/oauth/deny"><input type=hidden name=state value="{st}">
  <button class=secondary>Cancel</button></form>
<script>function onGoogle(r){{document.getElementById('credential').value=r.credential;document.getElementById('done').submit();}}</script>
<script src="https://accounts.google.com/gsi/client" async></script>"""
        return 200, _page("Sign in to fileapi", body)

    def deny(self, state: str) -> tuple[str | None, tuple[int, str] | None]:
        pending = self.store.pop("pending", state)
        if not pending:
            return None, (400, _page("Sign-in expired", "<p>Start again from your app.</p>"))
        params = pending["params"]
        return construct_redirect_uri(params["redirect_uri"], error="access_denied", state=params.get("state")), None

    def google_callback(self, state: str, credential: str) -> tuple[str | None, tuple[int, str] | None]:
        """Verify the Google ID token from the button and, if allowed, redirect to the client with a code."""
        pending = self.store.pop("pending", state)
        if not pending:
            return None, (400, _page("Sign-in expired", "<p>Start again from your app.</p>"))
        params = pending["params"]
        email = self.config.verify_id_token(credential, self._nonce(state)) if credential else None
        if not email:
            return None, (401, _page("Sign-in failed", "<p>Google sign-in could not be verified. Start again from your app.</p>"))
        if not self.config.allowed(email):
            return None, (
                403,
                _page("Not allowed", f"<p><b>{html.escape(email)}</b> is not on the allowlist for this file store.</p>"),
            )

        our_code = secrets.token_urlsafe(32)
        auth_code = AuthorizationCode(
            code=our_code,
            scopes=params.get("scopes") or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending["client_id"],
            code_challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=params.get("resource"),
            subject=email.lower(),
        )
        self.store.put("codes", our_code, auth_code.model_dump(mode="json"), auth_code.expires_at)
        return construct_redirect_uri(params["redirect_uri"], code=our_code, state=params.get("state")), None

    # -- codes and tokens ----------------------------------------------------
    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        data = self.store.get("codes", authorization_code)
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(data)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        if self.store.pop("codes", authorization_code.code) is None:
            raise TokenError(error="invalid_grant", error_description="Authorization code already used")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.subject, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        data = self.store.get("refresh", _hash(refresh_token))
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        if not self.config.allowed(refresh_token.subject):
            raise TokenError(error="invalid_grant", error_description="Account is no longer allowed")
        self.store.delete("refresh", _hash(refresh_token.token))
        self.store.delete_where("access", "refresh_hash", _hash(refresh_token.token))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.subject, refresh_token.resource)

    def _issue(self, client_id: str, scopes: list[str], subject: str | None, resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        common = {"client_id": client_id, "scopes": scopes, "subject": subject, "resource": resource}
        self.store.put(
            "refresh", _hash(refresh), {**common, "expires_at": now + self.config.refresh_ttl}, now + self.config.refresh_ttl
        )
        self.store.put(
            "access",
            _hash(access),
            {**common, "expires_at": now + self.config.access_ttl, "refresh_hash": _hash(refresh)},
            now + self.config.access_ttl,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.config.access_ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        static = self.config.static_token
        if static and secrets.compare_digest(token.encode(), static.encode()):
            return AccessToken(token=token, client_id="static-token", scopes=[SCOPE], subject="static-token")
        data = self.store.get("access", _hash(token))
        if not data or not self.config.allowed(data.get("subject")):
            return None
        data.pop("refresh_hash", None)
        return AccessToken.model_validate({**data, "token": token})

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        h = _hash(token.token)
        if isinstance(token, RefreshToken):
            self.store.delete("refresh", h)
            self.store.delete_where("access", "refresh_hash", h)
        else:
            data = self.store.get("access", h)
            self.store.delete("access", h)
            if data and data.get("refresh_hash"):
                self.store.delete("refresh", data["refresh_hash"])


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:28rem;margin:10vh auto;padding:0 1rem;color:#1a1a1a;background:#fafafa}}
h1{{font-size:1.4rem}} .muted{{color:#666;font-size:.9rem}} code{{word-break:break-all}}
button{{font:inherit;padding:.6rem 1rem;border-radius:6px;border:0;background:#1a73e8;color:#fff;cursor:pointer;margin-right:.5rem}}
button.secondary{{background:#e0e0e0;color:#1a1a1a}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#eee}} .muted{{color:#aaa}} button.secondary{{background:#333;color:#eee}}}}
</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""
