# fileapi

An MCP server that gives any MCP client (Claude, Claude Code, etc.) a central place to create, read, list, and edit files.

- **Transport:** Streamable HTTP at `POST /mcp` (stateless). A stdio entry point is also included for local use.
- **Storage:** plain files on disk under `FILEAPI_ROOT`. Paths are sandboxed to that folder (no `..`, no symlink escapes). Writes are atomic.
- **Auth:** `Authorization: Bearer <FILEAPI_TOKEN>` header on every request.

## Tools

| Tool | What it does |
| --- | --- |
| `list_files` | List a directory (`path`, optional `recursive`) |
| `read_file` | Read a file |
| `create_file` | Create a new file; fails if it exists |
| `write_file` | Create or overwrite a file |
| `edit_file` | Exact find/replace edits; each `old_text` must match once; all-or-nothing |
| `append_file` | Append to a file |
| `move_file` | Move or rename |
| `delete_file` | Delete a file or empty directory |

## Run

```sh
npm install
npm run build
FILEAPI_TOKEN=$(openssl rand -hex 32) FILEAPI_ROOT=./data npm start
```

Or with Docker:

```sh
docker build -t fileapi .
docker run -p 3000:3000 -v fileapi-data:/data -e FILEAPI_TOKEN=your-secret fileapi
```

| Env var | Default | Notes |
| --- | --- | --- |
| `FILEAPI_TOKEN` | (required) | Bearer token clients must send |
| `FILEAPI_ROOT` | `./data` | Where files live |
| `PORT` / `HOST` | `3000` / `0.0.0.0` | |
| `FILEAPI_MAX_BODY` | `10mb` | Max request size |
| `FILEAPI_ALLOW_NO_AUTH` | unset | `1` disables auth (local testing only) |

Health check: `GET /health`.

## Connect a client

Claude Code:

```sh
claude mcp add --transport http fileapi https://your-host/mcp --header "Authorization: Bearer your-secret"
```

Any client that supports remote MCP servers with custom headers: URL `https://your-host/mcp`, header `Authorization: Bearer your-secret`.

Local stdio (no auth, same storage):

```sh
claude mcp add fileapi -e FILEAPI_ROOT=/path/to/data -- node /path/to/fileapi/dist/stdio.js
```

## Test

```sh
npm test
```
