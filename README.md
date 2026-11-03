# fileapi

A central file store you can create, read, list, and edit through MCP (for Claude, Claude Code and other MCP clients) or through a plain REST API. Built on FastAPI and the official MCP Python SDK.

- **MCP:** Streamable HTTP at `POST /mcp` (stateless, JSON responses). `fileapi stdio` runs the same tools over stdio for local use.
- **REST:** `/files/...` endpoints, documented at `/docs`.
- **Storage:** plain files on disk under `FILEAPI_ROOT`. Paths are sandboxed to that folder (no `..`, no symlink escapes). Writes are atomic.
- **Auth:** web sign-in with Google, limited to an email allowlist. MCP clients (Claude, Claude Code, ...) discover it automatically through the standard MCP OAuth flow: you click "Continue with Google" once and the client gets a token. Uses Google's drop-in "Sign in with Google" button, so only a Google **client ID** is needed (no client secret). An optional static token is available for scripts.

## MCP tools

| Tool | What it does |
| --- | --- |
| `list_files` | List a directory (`path`, optional `recursive`) |
| `read_file` | Read a text file |
| `create_file` | Create a new file; fails if it exists |
| `write_file` | Create or overwrite a file |
| `edit_file` | Exact find/replace (`edits: [{old_text, new_text}]`); each `old_text` must match once; all-or-nothing |
| `append_file` | Append to a file |
| `move_file` | Move or rename (`source`, `destination`, `overwrite`) |
| `delete_file` | Delete a file or empty directory |

## REST API

| Method & path | Does |
| --- | --- |
| `GET /files?recursive=true` | List the root |
| `GET /files/{path}` | File contents as text, or a JSON listing for a directory |
| `PUT /files/{path}` | Write the raw body (text/plain). `?overwrite=false` fails with 409 if it exists |
| `PATCH /files/{path}` | `{"edits": [{"old_text": "...", "new_text": "..."}]}` |
| `POST /files/{path}:append` | Append the raw body |
| `POST /files/{path}:move` | `{"destination": "...", "overwrite": false}` |
| `DELETE /files/{path}` | Delete |

## Google setup (client ID only)

1. Google Cloud Console → APIs & Services → Credentials → **Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. **Authorized JavaScript origins:** your server's public URL, e.g. `https://files.example.com` (add `http://localhost:8000` too for local testing). No redirect URI is needed.
4. Copy the **Client ID** (`....apps.googleusercontent.com`). You do not need the client secret.

## Run

```sh
pip install .
export FILEAPI_PUBLIC_URL=https://files.example.com
export GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
export FILEAPI_ALLOWED_EMAILS=you@gmail.com,friend@gmail.com
fileapi
```

Or with Docker:

```sh
docker build -t fileapi .
docker run -p 8000:8000 -v fileapi-data:/data -v fileapi-state:/state \
  -e FILEAPI_PUBLIC_URL=https://files.example.com \
  -e GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com \
  -e FILEAPI_ALLOWED_EMAILS=you@gmail.com fileapi
```

| Env var | Default | Notes |
| --- | --- | --- |
| `FILEAPI_PUBLIC_URL` | (required) | The https URL clients use; also the OAuth issuer. `http://localhost` works for testing |
| `GOOGLE_CLIENT_ID` | | Google OAuth client ID (web application) |
| `FILEAPI_ALLOWED_EMAILS` | | Comma-separated Google accounts allowed to sign in. Removing one revokes its tokens |
| `FILEAPI_TOKEN` | unset | Optional static bearer token for scripts/headless clients |
| `FILEAPI_ROOT` | `./data` | Where files live |
| `FILEAPI_STATE_DIR` | `./state` | OAuth clients and tokens (SQLite, tokens stored hashed). Must be outside `FILEAPI_ROOT` |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Run behind an HTTPS reverse proxy in production |
| `FILEAPI_MAX_BODY` | `10485760` | Max MCP request size in bytes |

At least one of `GOOGLE_CLIENT_ID` or `FILEAPI_TOKEN` is required. Health check: `GET /health` (no auth).

### How sign-in works

1. The MCP client hits `/mcp`, gets a 401 pointing at the OAuth metadata, registers itself (`/register`) and opens `/authorize` in your browser.
2. You see which app is asking, then click **Continue with Google**.
3. Google returns a signed ID token to the page; the server checks Google's signature, audience (your client ID), a per-request nonce, `email_verified`, and the allowlist.
4. The client receives an access token (1 hour) and refresh token (30 days, rotated on use).

## Connect a client

Claude (claude.ai / desktop): Settings → Connectors → Add custom connector → URL `https://your-host/mcp`. Leave the OAuth client ID/secret fields empty; it registers itself and sends you to the Google sign-in page.

Claude Code:

```sh
claude mcp add --transport http fileapi https://your-host/mcp
# then run /mcp inside Claude Code and choose Authenticate
```

Scripts can use the static token instead: header `Authorization: Bearer <FILEAPI_TOKEN>`, on `/mcp` or `/files`.

Local stdio (no auth, same storage folder):

```sh
claude mcp add fileapi -e FILEAPI_ROOT=/path/to/data -- fileapi stdio
```

## Test

```sh
pip install -e '.[dev]'
pytest
```
