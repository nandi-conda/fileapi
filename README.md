# fileapi

A central file store you can create, read, list, and edit through MCP (Claude, Claude Code, any MCP client) or a plain REST API. Built on FastAPI and the official MCP Python SDK.

- **MCP:** Streamable HTTP at `/mcp` (stateless). `fileapi stdio` runs the same tools over stdio for local use.
- **REST:** `/files/...`, documented at `/docs`.
- **Storage:** Postgres when `DATABASE_URL` is set (needed on hosts without a persistent disk, like FastAPI Cloud), otherwise plain files under `FILEAPI_ROOT`.
- **Auth:** first client claims the server over MCP and gets the token. See below.

## Auth: first client claims the server

1. Deploy. The server starts **unclaimed**.
2. Connect any MCP client to `https://<host>/mcp` with no credentials and ask it to call **`claim_server`**. It returns the access token (once, so save it) plus exact reconnect instructions.
3. From then on the file tools and `/files` need the token, either as:
   - header `Authorization: Bearer <token>`, or
   - query string `https://<host>/mcp?token=<token>` for clients that can't set headers (e.g. a claude.ai custom connector).
4. `claim_server` only works once. A request with no token can still connect and list tools, but every tool refuses. A wrong token gets `401`.
5. `rotate_token` (needs the current token) issues a new one. The old one keeps working for 60 seconds.

Only a SHA-256 hash of the token is stored. To reset ownership, delete the `access_token` record (`fileapi_kv` table in Postgres, or `$FILEAPI_STATE_DIR/access_token.json`). To skip the claim step, set `FILEAPI_TOKEN` to a token of your choice.

Whoever reaches a fresh deployment first owns it, so claim it right after deploying.

## MCP tools

| Tool | What it does |
| --- | --- |
| `claim_server` | First-time setup: returns the access token. Works once |
| `rotate_token` | Replace the token |
| `list_files` | List a directory (`path`, optional `recursive`) |
| `read_file` | Read a text file |
| `create_file` | Create a new file; fails if it exists |
| `write_file` | Create or overwrite a file |
| `edit_file` | Exact find/replace (`edits: [{old_text, new_text}]`); each `old_text` must match once; all-or-nothing |
| `append_file` | Append to a file |
| `move_file` | Move or rename a file or directory (`source`, `destination`, `overwrite`) |
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

## Deploy to FastAPI Cloud

FastAPI Cloud has no persistent disk, so files and the token go in Postgres.

With [just](https://github.com/casey/just), Node (for `npx neonctl`) and either the `fastapi` CLI or `uv` installed:

```sh
just launch       # FastAPI Cloud browser login, first deploy (creates the app),
                  # Neon browser login, creates a "fileapi" Neon project if needed,
                  # writes DATABASE_URL to .env and sets it on FastAPI Cloud (which redeploys)
```

After that, `just deploy` redeploys and `just logs` streams logs. The pieces also run on their own: `just login`, `just deploy`, `just cloud-db`.

`just neon-db` does the same without touching FastAPI Cloud (handy for local runs; `just dev` reads `.env`). Set `NEON_ORG_ID` if your Neon account has several organizations, or `NEON_PROJECT_NAME` to use another project name.

Or by hand, using the Neon/Supabase integration in the FastAPI Cloud dashboard (it sets `DATABASE_URL` for you):

```sh
pip install "fastapi[standard]"
fastapi login
fastapi deploy                      # entrypoint comes from [tool.fastapi] in pyproject.toml
# In the FastAPI Cloud dashboard: add the Neon (or Supabase) Postgres integration -> sets DATABASE_URL
fastapi deploy                      # redeploy so it picks up DATABASE_URL
```

Then connect a client to `https://<app>.fastapicloud.dev/mcp` and call `claim_server`. Tables are created automatically on startup.

## Run elsewhere

```sh
pip install .
fileapi                              # files in ./data, token record in ./state
# or: DATABASE_URL=postgresql://user:pass@host/db fileapi
```

Docker:

```sh
docker build -t fileapi .
docker run -p 8000:8000 -v fileapi-data:/data -v fileapi-state:/state fileapi
```

| Env var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | unset | Postgres connection string. When set, files and the token live in Postgres |
| `FILEAPI_ROOT` | `./data` | Filesystem backend: where files live |
| `FILEAPI_STATE_DIR` | `./state` | Filesystem backend: token record. Must be outside `FILEAPI_ROOT` |
| `FILEAPI_TOKEN` | unset | Pin the token yourself and skip `claim_server` |
| `FILEAPI_PUBLIC_URL` | from request | Base URL shown in reconnect instructions |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | For `fileapi` CLI |
| `FILEAPI_MAX_BODY` | `10485760` | Max MCP request size in bytes |

Health check: `GET /health` returns `{"ok": true, "claimed": ...}`.

## Connect a client

Claude Code:

```sh
claude mcp add --transport http fileapi https://<host>/mcp                    # first time, then ask it to claim_server
claude mcp add --transport http fileapi https://<host>/mcp --header "Authorization: Bearer <token>"
```

claude.ai / Claude desktop: Settings → Connectors → Add custom connector → `https://<host>/mcp?token=<token>`.

Local stdio (no auth, filesystem backend):

```sh
claude mcp add fileapi -e FILEAPI_ROOT=/path/to/data -- fileapi stdio
```

## Test

```sh
pip install -e '.[dev]'
pytest                                                  # filesystem backend
TEST_DATABASE_URL=postgresql://... pytest               # also runs everything against Postgres
```
