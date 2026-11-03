# fileapi

A central file store you can create, read, list, and edit through MCP (for Claude, Claude Code and other MCP clients) or through a plain REST API. Built on FastAPI and the official MCP Python SDK.

- **MCP:** Streamable HTTP at `POST /mcp` (stateless, JSON responses). `fileapi stdio` runs the same tools over stdio for local use.
- **REST:** `/files/...` endpoints, documented at `/docs`.
- **Storage:** plain files on disk under `FILEAPI_ROOT`. Paths are sandboxed to that folder (no `..`, no symlink escapes). Writes are atomic.
- **Auth:** one shared secret. Every `/mcp` and `/files` request must send `Authorization: Bearer <FILEAPI_TOKEN>`.

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

## Run

```sh
pip install .
FILEAPI_TOKEN=$(openssl rand -hex 32) FILEAPI_ROOT=./data fileapi
```

Or with Docker:

```sh
docker build -t fileapi .
docker run -p 8000:8000 -v fileapi-data:/data -e FILEAPI_TOKEN=your-secret fileapi
```

| Env var | Default | Notes |
| --- | --- | --- |
| `FILEAPI_TOKEN` | (required) | Bearer token clients must send |
| `FILEAPI_ROOT` | `./data` | Where files live |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |
| `FILEAPI_MAX_BODY` | `10485760` | Max MCP request size in bytes |
| `FILEAPI_ALLOW_NO_AUTH` | unset | `1` disables auth (local testing only) |

Health check: `GET /health` (no auth).

## Connect a client

Claude Code:

```sh
claude mcp add --transport http fileapi https://your-host/mcp --header "Authorization: Bearer your-secret"
```

Any client that supports remote MCP servers with custom headers: URL `https://your-host/mcp`, header `Authorization: Bearer your-secret`.

Local stdio (no auth, same storage folder):

```sh
claude mcp add fileapi -e FILEAPI_ROOT=/path/to/data -- fileapi stdio
```

## Test

```sh
pip install -e '.[dev]'
pytest
```
