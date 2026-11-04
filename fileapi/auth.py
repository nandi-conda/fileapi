"""Sign-in with atproto (Bluesky) OAuth, limited to one admin account.

fileapi is an OAuth 2.1 authorization server for its own MCP clients (dynamic client registration, PKCE,
refresh tokens), as the MCP spec expects: a client hits /mcp, gets a 401 pointing at
/.well-known/oauth-protected-resource, registers, and sends the user to /authorize. There fileapi itself
becomes an atproto OAuth client (PAR + PKCE + DPoP, public client) and sends the user to the admin's own
authorization server. On the way back it checks that the account that signed in is the admin's DID
(not the handle, so a handle change neither locks the admin out nor lets someone else in) and only then
hands the MCP client an authorization code. Everyone else gets "access denied".

The tokens fileapi issues are opaque random strings; only their SHA-256 is stored, in the KV store
(Postgres on FastAPI Cloud), so they survive deploys and work across replicas. The same access token
works as a Bearer header on /files and as the git Basic-auth password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

ADMIN_DID = "did:plc:ngokl2gnmpbvuvrfckja3g7p"  # nandi.uk, resolved 2026-11-04 (DNS _atproto + plc.directory)
ADMIN_HANDLE = "nandi.uk"

SCOPE = "fileapi"
ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
CODE_TTL = 300
LOGIN_TTL = 600
CALLBACK = "/oauth/atproto/callback"
CLIENT_METADATA = "/oauth/atproto/client-metadata.json"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def base_url(request: Request, public_url: str | None) -> str:
    if public_url:
        return public_url.rstrip("/")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{proto}://{host}"


def _is_loopback(base: str) -> bool:
    return urlparse(base).hostname in ("localhost", "127.0.0.1", "[::1]", "::1")


# ---- atproto identity + OAuth client --------------------------------------------------------------

class AtprotoError(Exception):
    pass


@dataclass
class AuthServer:
    issuer: str
    par_endpoint: str
    authorization_endpoint: str
    token_endpoint: str


class Atproto:
    """The pieces of the atproto OAuth client profile fileapi needs: resolve a DID to its authorization
    server, push an authorization request, and redeem the code for the signed-in account's DID."""

    def __init__(self, http: httpx.AsyncClient | None = None):
        self.http = http or httpx.AsyncClient(timeout=10, follow_redirects=False)

    async def _json(self, url: str) -> dict:
        r = await self.http.get(url, headers={"accept": "application/json"})
        if r.status_code != 200:
            raise AtprotoError(f"GET {url} returned {r.status_code}")
        return r.json()

    async def did_document(self, did: str) -> dict:
        if did.startswith("did:plc:"):
            return await self._json(f"https://plc.directory/{did}")
        if did.startswith("did:web:"):
            return await self._json(f"https://{did.removeprefix('did:web:')}/.well-known/did.json")
        raise AtprotoError(f"Unsupported DID method: {did}")

    async def auth_server_for(self, did: str) -> AuthServer:
        doc = await self.did_document(did)
        if doc.get("id") != did:
            raise AtprotoError("DID document id mismatch")
        pds = next(
            (s["serviceEndpoint"] for s in doc.get("service", [])
             if s.get("id", "").endswith("#atproto_pds") and s.get("type") == "AtprotoPersonalDataServer"),
            None,
        )
        if not pds:
            raise AtprotoError(f"{did} has no PDS")
        resource = await self._json(f"{pds.rstrip('/')}/.well-known/oauth-protected-resource")
        issuer = (resource.get("authorization_servers") or [None])[0]
        if not issuer or not issuer.startswith("https://"):
            raise AtprotoError("PDS names no authorization server")
        meta = await self._json(f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server")
        if meta.get("issuer") != issuer:
            raise AtprotoError("Authorization server issuer mismatch")
        return AuthServer(issuer, meta["pushed_authorization_request_endpoint"], meta["authorization_endpoint"],
                          meta["token_endpoint"])

    @staticmethod
    def new_dpop_key() -> str:
        key = ec.generate_private_key(ec.SECP256R1())
        return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()

    @staticmethod
    def dpop_proof(key_pem: str, method: str, url: str, nonce: str | None) -> str:
        key = serialization.load_pem_private_key(key_pem.encode(), None)
        jwk = jwt.algorithms.ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
        claims = {"jti": secrets.token_urlsafe(16), "htm": method, "htu": url, "iat": int(time.time())}
        if nonce:
            claims["nonce"] = nonce
        return jwt.encode(claims, key, algorithm="ES256", headers={"typ": "dpop+jwt", "jwk": jwk})

    async def _dpop_post(self, url: str, data: dict, key_pem: str, nonce: str | None) -> tuple[dict, str | None]:
        for _ in range(2):
            r = await self.http.post(url, data=data, headers={"DPoP": self.dpop_proof(key_pem, "POST", url, nonce)})
            nonce = r.headers.get("dpop-nonce", nonce)
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code == 400 and body.get("error") == "use_dpop_nonce":
                continue
            if r.status_code >= 300:
                raise AtprotoError(f"{url}: {body.get('error', r.status_code)} {body.get('error_description', '')}".strip())
            return body, nonce
        raise AtprotoError(f"{url} kept asking for a new DPoP nonce")

    async def par(self, server: AuthServer, params: dict, key_pem: str) -> tuple[str, str | None]:
        body, nonce = await self._dpop_post(server.par_endpoint, params, key_pem, None)
        return body["request_uri"], nonce

    async def redeem(self, server: AuthServer, params: dict, key_pem: str, nonce: str | None) -> dict:
        body, _ = await self._dpop_post(server.token_endpoint, params, key_pem, nonce)
        return body


# ---- fileapi's own authorization server -----------------------------------------------------------

class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


class Auth:
    def __init__(self, kv, *, admin_did: str = ADMIN_DID, admin_handle: str = ADMIN_HANDLE,
                 preset_token: str | None = None, atproto: Atproto | None = None):
        self.kv = kv
        self.admin_did = admin_did
        self.admin_handle = admin_handle
        self.preset = preset_token or None
        self.atproto = atproto or Atproto()

    # Tokens ---------------------------------------------------------------------------------------
    def identify(self, token: str | None) -> str | None:
        """The admin DID if the token is a live fileapi access token (or the FILEAPI_TOKEN preset), else None."""
        if not token:
            return None
        if self.preset and hmac.compare_digest(token.encode(), self.preset.encode()):
            return self.admin_did
        record = self.kv.get(f"oauth_at_{_sha(token)}")
        if not record or record["exp"] < time.time() or record["did"] != self.admin_did:
            return None
        return record["did"]

    def scope(self, token: str | None) -> str | None:
        """Used by the git/MCP project-scope hook: "" (everything) for the admin, None for anyone else."""
        return "" if self.identify(token) else None

    def _issue(self, client_id: str, did: str) -> dict:
        if did != self.admin_did:
            raise OAuthError("access_denied", "Only the admin account can use this server")
        access, refresh = "fa_" + secrets.token_urlsafe(32), "fr_" + secrets.token_urlsafe(32)
        now = time.time()
        self.kv.put(f"oauth_at_{_sha(access)}", {"did": did, "client_id": client_id, "exp": now + ACCESS_TTL})
        self.kv.put(f"oauth_rt_{_sha(refresh)}", {"did": did, "client_id": client_id, "exp": now + REFRESH_TTL,
                                                  "access": _sha(access)})
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": SCOPE}

    # Clients --------------------------------------------------------------------------------------
    def register(self, meta: dict) -> dict:
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
            raise OAuthError("invalid_redirect_uri", "redirect_uris must be a non-empty list")
        for u in uris:
            p = urlparse(u)
            if p.fragment or not p.scheme or (p.scheme == "http" and p.hostname not in ("localhost", "127.0.0.1", "::1")):
                raise OAuthError("invalid_redirect_uri", f"Not allowed: {u}")
        method = meta.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError("invalid_client_metadata", f"Unsupported token_endpoint_auth_method {method}")
        client_id = "fc_" + secrets.token_urlsafe(16)
        record = {
            "client_id": client_id,
            "client_name": str(meta.get("client_name", ""))[:200],
            "redirect_uris": uris,
            "token_endpoint_auth_method": method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": SCOPE,
            "client_id_issued_at": int(time.time()),
        }
        out = dict(record)
        if method != "none":
            secret = "fs_" + secrets.token_urlsafe(32)
            record["secret_sha256"] = _sha(secret)
            out.update(client_secret=secret, client_secret_expires_at=0)
        self.kv.put(f"oauth_client_{_sha(client_id)}", record)
        return out

    def client(self, client_id: str | None) -> dict | None:
        return self.kv.get(f"oauth_client_{_sha(client_id)}") if client_id else None

    def authenticate_client(self, request_headers, form) -> dict:
        client_id, secret = form.get("client_id"), form.get("client_secret")
        scheme, _, value = request_headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                from urllib.parse import unquote

                cid, _, sec = base64.b64decode(value).decode().partition(":")
                client_id, secret = unquote(cid), unquote(sec)
            except Exception:
                raise OAuthError("invalid_client", "Bad Basic credentials", 401) from None
        client = self.client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "Unknown client", 401)
        if client["token_endpoint_auth_method"] != "none":
            if not secret or not hmac.compare_digest(_sha(secret), client["secret_sha256"]):
                raise OAuthError("invalid_client", "Bad client secret", 401)
        return client

    # Token endpoint ---------------------------------------------------------------------------------
    def token(self, client: dict, form) -> dict:
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = self.kv.pop(f"oauth_code_{_sha(form.get('code') or '')}")
            if not code or code["exp"] < time.time() or code["client_id"] != client["client_id"]:
                raise OAuthError("invalid_grant", "Unknown or expired code")
            if form.get("redirect_uri", code["redirect_uri"]) != code["redirect_uri"]:
                raise OAuthError("invalid_grant", "redirect_uri mismatch")
            verifier = form.get("code_verifier") or ""
            if not hmac.compare_digest(_b64(hashlib.sha256(verifier.encode()).digest()), code["code_challenge"]):
                raise OAuthError("invalid_grant", "PKCE verification failed")
            return self._issue(client["client_id"], code["did"])
        if grant == "refresh_token":
            rt = self.kv.pop(f"oauth_rt_{_sha(form.get('refresh_token') or '')}")
            if not rt or rt["exp"] < time.time() or rt["client_id"] != client["client_id"]:
                raise OAuthError("invalid_grant", "Unknown or expired refresh token")
            self.kv.pop(f"oauth_at_{rt['access']}")
            return self._issue(client["client_id"], rt["did"])
        raise OAuthError("unsupported_grant_type", f"Unsupported grant_type {grant}")

    def revoke(self, token: str) -> None:
        rt = self.kv.pop(f"oauth_rt_{_sha(token)}")
        if rt:
            self.kv.pop(f"oauth_at_{rt['access']}")
        self.kv.pop(f"oauth_at_{_sha(token)}")

    # Authorize + atproto round trip -----------------------------------------------------------------
    def atproto_client_id(self, base: str) -> tuple[str, str]:
        redirect = f"{base}{CALLBACK}"
        if _is_loopback(base):
            # atproto's loopback client: no metadata document, redirect must be on 127.0.0.1.
            redirect = redirect.replace("://localhost", "://127.0.0.1", 1)
            return f"http://localhost?redirect_uri={quote(redirect, safe='')}&scope=atproto", redirect
        return f"{base}{CLIENT_METADATA}", redirect

    def client_metadata(self, base: str) -> dict:
        client_id, redirect = self.atproto_client_id(base)
        return {
            "client_id": client_id,
            "client_name": "fileapi",
            "client_uri": base,
            "redirect_uris": [redirect],
            "scope": "atproto",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "web",
            "dpop_bound_access_tokens": True,
        }

    async def start_login(self, base: str, mcp_request: dict) -> str:
        """Push an atproto authorization request for the admin account; returns where to send the browser."""
        server = await self.atproto.auth_server_for(self.admin_did)
        client_id, redirect = self.atproto_client_id(base)
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        key = Atproto.new_dpop_key()
        request_uri, nonce = await self.atproto.par(server, {
            "client_id": client_id, "response_type": "code", "redirect_uri": redirect, "scope": "atproto",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "login_hint": self.admin_handle or self.admin_did,
        }, key)
        self.kv.put(f"oauth_login_{_sha(state)}", {
            "exp": time.time() + LOGIN_TTL, "verifier": verifier, "dpop_key": key, "dpop_nonce": nonce,
            "server": server.__dict__, "client_id": client_id, "redirect": redirect, "mcp": mcp_request,
        })
        return f"{server.authorization_endpoint}?{urlencode({'client_id': client_id, 'request_uri': request_uri})}"

    async def finish_login(self, params) -> tuple[dict, str]:
        """Redeem the atproto code. Returns (the pending MCP authorization request, the signed-in DID)."""
        login = self.kv.pop(f"oauth_login_{_sha(params.get('state') or '')}")
        if not login or login["exp"] < time.time():
            raise OAuthError("invalid_request", "This sign-in link expired. Start again from your MCP client.")
        server = AuthServer(**login["server"])
        if params.get("error"):
            return login["mcp"], ""
        if params.get("iss") != server.issuer:
            raise OAuthError("invalid_request", "Authorization server mismatch")
        tokens = await self.atproto.redeem(server, {
            "grant_type": "authorization_code", "code": params.get("code") or "", "redirect_uri": login["redirect"],
            "client_id": login["client_id"], "code_verifier": login["verifier"],
        }, login["dpop_key"], login["dpop_nonce"])
        if "atproto" not in str(tokens.get("scope", "")).split():
            raise OAuthError("invalid_request", "Authorization server did not grant the atproto scope")
        did = tokens.get("sub") or ""
        # The issuer was resolved from the admin's DID document, so a `sub` equal to the admin DID is
        # authoritative. Any other account is refused, without resolving it further.
        return login["mcp"], did

    def issue_code(self, mcp_request: dict, did: str) -> str:
        code = "fo_" + secrets.token_urlsafe(32)
        self.kv.put(f"oauth_code_{_sha(code)}", {
            "exp": time.time() + CODE_TTL, "client_id": mcp_request["client_id"], "did": did,
            "redirect_uri": mcp_request["redirect_uri"], "code_challenge": mcp_request["code_challenge"],
        })
        return code


def _redirect_with(uri: str, params: dict) -> RedirectResponse:
    sep = "&" if urlparse(uri).query else "?"
    return RedirectResponse(uri + sep + urlencode({k: v for k, v in params.items() if v is not None}), status_code=302)


def _page(title: str, message: str, status: int) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:36rem;margin:10vh auto;padding:0 1rem;background:#fafafa;color:#1a1a1a}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#eee}}}}</style></head>
<body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p></body></html>""", status_code=status)


def _oauth_json(e: OAuthError) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="fileapi"'} if e.status == 401 else None
    return JSONResponse({"error": e.error, "error_description": e.description}, status_code=e.status,
                        headers=headers)


NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, mcp-protocol-version"}


def resource_metadata_url(base: str) -> str:
    return f"{base}/.well-known/oauth-protected-resource/mcp"


def install(app: FastAPI, auth: Auth, public_url: str | None) -> None:
    """Add the OAuth endpoints to the app."""

    def base(request: Request) -> str:
        return base_url(request, public_url)

    def protected_resource(request: Request):
        b = base(request)
        return JSONResponse({"resource": f"{b}/mcp", "authorization_servers": [b], "scopes_supported": [SCOPE],
                             "bearer_methods_supported": ["header"], "resource_name": "fileapi"}, headers=CORS)

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        app.add_api_route(path, protected_resource, methods=["GET"], include_in_schema=False)

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def as_metadata(request: Request):
        b = base(request)
        return JSONResponse({
            "issuer": b,
            "authorization_endpoint": f"{b}/authorize",
            "token_endpoint": f"{b}/token",
            "registration_endpoint": f"{b}/register",
            "revocation_endpoint": f"{b}/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }, headers=CORS)

    def preflight():
        return Response(status_code=204, headers=CORS)

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-authorization-server", "/token", "/register", "/revoke"):
        app.add_api_route(path, preflight, methods=["OPTIONS"], include_in_schema=False)

    @app.get(CLIENT_METADATA, include_in_schema=False)
    def atproto_client_metadata(request: Request):
        return JSONResponse(auth.client_metadata(base(request)))

    @app.post("/register", include_in_schema=False)
    async def register(request: Request):
        try:
            meta = await request.json()
            if not isinstance(meta, dict):
                raise ValueError
        except ValueError:
            return _oauth_json(OAuthError("invalid_client_metadata", "Body must be a JSON object"))
        try:
            return JSONResponse(auth.register(meta), status_code=201, headers={**CORS, **NO_STORE})
        except OAuthError as e:
            return _oauth_json(e)

    @app.get("/authorize", include_in_schema=False)
    async def authorize(request: Request):
        q = request.query_params
        client = auth.client(q.get("client_id"))
        if client is None:
            return _page("Unknown app", "This app is not registered with fileapi. Reconnect it from your MCP client.", 400)
        redirect_uri = q.get("redirect_uri")
        if redirect_uri is None and len(client["redirect_uris"]) == 1:
            redirect_uri = client["redirect_uris"][0]
        if redirect_uri not in client["redirect_uris"]:
            return _page("Bad redirect", "The app asked to return to an address it did not register.", 400)
        state = q.get("state")
        b = base(request)

        def fail(error: str, description: str):
            return _redirect_with(redirect_uri, {"error": error, "error_description": description, "state": state, "iss": b})

        if q.get("response_type") != "code":
            return fail("unsupported_response_type", "Only response_type=code is supported")
        if not q.get("code_challenge") or q.get("code_challenge_method", "S256") != "S256":
            return fail("invalid_request", "PKCE with S256 is required")
        mcp_request = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "state": state,
                       "code_challenge": q["code_challenge"]}
        # Anyone can register a client, so show which app is asking and where it sends the result before
        # signing in; otherwise a stranger's app could get a token from a link the admin clicked.
        rid = secrets.token_urlsafe(24)
        auth.kv.put(f"oauth_pending_{_sha(rid)}", {"exp": time.time() + LOGIN_TTL, "mcp": mcp_request})
        name = client.get("client_name") or "An MCP client"
        target = urlparse(redirect_uri)
        dest = f"{target.scheme}://{target.netloc}" if target.netloc else redirect_uri.split("?")[0]
        who = auth.admin_handle or auth.admin_did
        body = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Sign in to fileapi</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:36rem;margin:10vh auto;padding:0 1rem;background:#fafafa;color:#1a1a1a}}
code{{background:#eee;padding:.1rem .3rem;border-radius:4px;word-break:break-all}}
button{{font:inherit;padding:.5rem 1rem;border-radius:6px;border:1px solid #888;cursor:pointer}}
@media (prefers-color-scheme:dark){{body{{background:#121212;color:#eee}}code{{background:#2a2a2a}}}}</style></head>
<body><h1>Sign in to fileapi</h1>
<p><b>{html.escape(name)}</b> wants full access to the files and repos on this server. After sign-in it
returns to <code>{html.escape(dest)}</code>.</p>
<p>Only <code>{html.escape(who)}</code> can sign in. If you didn't just connect this app, close this page.</p>
<form method=post action="/authorize"><input type=hidden name=rid value="{rid}">
<button type=submit>Continue with Bluesky / atproto</button></form></body></html>"""
        return HTMLResponse(body, headers={"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                                           **NO_STORE})

    @app.post("/authorize", include_in_schema=False)
    async def authorize_continue(request: Request):
        b = base(request)
        origin = request.headers.get("origin")
        if origin and origin != b:
            return _page("Not allowed", "This sign-in has to be started from fileapi's own page.", 403)
        form = await request.form()
        pending = auth.kv.pop(f"oauth_pending_{_sha(form.get('rid') or '')}")
        if not pending or pending["exp"] < time.time():
            return _page("Sign-in expired", "This sign-in link expired. Start again from your MCP client.", 400)
        try:
            return RedirectResponse(await auth.start_login(b, pending["mcp"]), status_code=303)
        except (AtprotoError, httpx.HTTPError, KeyError) as e:
            return _page("Sign-in unavailable", f"Could not reach the atproto sign-in server: {e}", 502)

    @app.get(CALLBACK, include_in_schema=False)
    async def atproto_callback(request: Request):
        try:
            mcp_request, did = await auth.finish_login(request.query_params)
        except OAuthError as e:
            return _page("Sign-in failed", e.description, 400)
        except (AtprotoError, httpx.HTTPError) as e:
            return _page("Sign-in failed", str(e), 502)
        b = base(request)
        back = {"state": mcp_request["state"], "iss": b}
        if did != auth.admin_did:
            if not did:  # the user cancelled at their authorization server
                return _redirect_with(mcp_request["redirect_uri"], {"error": "access_denied", **back})
            return _page("Not allowed", f"This fileapi server only accepts {auth.admin_handle or auth.admin_did}. "
                                        "The account you signed in with has no access.", 403)
        return _redirect_with(mcp_request["redirect_uri"], {"code": auth.issue_code(mcp_request, did), **back})

    @app.post("/token", include_in_schema=False)
    async def token(request: Request):
        form = await request.form()
        try:
            client = auth.authenticate_client(request.headers, form)
            return JSONResponse(auth.token(client, form), headers={**CORS, **NO_STORE})
        except OAuthError as e:
            return _oauth_json(e)

    @app.post("/revoke", include_in_schema=False)
    async def revoke(request: Request):
        form = await request.form()
        try:
            auth.authenticate_client(request.headers, form)
        except OAuthError as e:
            return _oauth_json(e)
        auth.revoke(form.get("token") or "")
        return Response(status_code=200, headers=CORS)
