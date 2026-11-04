# fileapi

A central file store you can create, read, list, and edit through MCP (Claude, Claude Code, any MCP client) or a plain REST API. Built on FastAPI and the official MCP Python SDK.

- **MCP:** Streamable HTTP at `/mcp` (stateless). `fileapi stdio` runs the same tools over stdio for local use.
- **REST:** `/files/...`, documented at `/docs`.
- **Storage:** Postgres when `DATABASE_URL` is set (needed on hosts without a persistent disk, like FastAPI Cloud), otherwise plain files under `FILEAPI_ROOT`.
- **Auth:** sign in with atproto (Bluesky) OAuth. Only the admin account (`nandi.uk`) gets in. See below.

## Auth: atproto sign-in, admin only

fileapi is an OAuth 2.1 authorization server for its MCP clients, the way the MCP spec expects, and signs people in through atproto:

1. Add `https://<host>/mcp` to an MCP client (Claude Code, claude.ai, Claude desktop) with no credentials. The server answers `401` with `WWW-Authenticate: Bearer resource_metadata=...`, so the client discovers `/.well-known/oauth-authorization-server`, registers itself (`/register`, dynamic client registration) and opens `/authorize` in the browser, which shows which app is asking and where it will return.
2. fileapi sends the browser to the admin's own atproto authorization server (PAR + PKCE + DPoP; for `nandi.uk` that's bsky.social), with the handle prefilled.
3. On the way back fileapi checks the signed-in account's **DID**, not its handle: only `did:plc:ngokl2gnmpbvuvrfckja3g7p` (`nandi.uk`) is accepted. Anyone else gets "Not allowed" and no token. Pinning the DID means a handle change neither locks the admin out nor lets a new owner of the handle in.
4. The client gets a fileapi access token (24 h) and refresh token (90 days, rotated on use). The access token also works for `/files` (`Authorization: Bearer <token>` or `?token=`) and as a git Basic-auth password.

Only SHA-256 hashes of tokens are stored, in the KV store (`fileapi_kv` in Postgres, or `$FILEAPI_STATE_DIR`). Delete the `oauth_at_*` / `oauth_rt_*` records to sign everything out.

To change the admin set `FILEAPI_ADMIN_DID` (and `FILEAPI_ADMIN_HANDLE` for the login hint). `FILEAPI_TOKEN` optionally sets a static admin token for scripts and CI; it is unset by default.

The atproto client metadata is served at `/oauth/atproto/client-metadata.json` and the authorization server fetches it, so the host must be reachable over HTTPS (set `FILEAPI_PUBLIC_URL` if it sits behind a proxy that rewrites the host). On `http://127.0.0.1` / `localhost` fileapi uses atproto's loopback client instead, so local development works without a public URL.

## MCP tools

| Tool | What it does |
| --- | --- |
| `whoami` | Which atproto account this connection is signed in as |
| `list_files` | List a directory (`path`, optional `recursive`) |
| `read_file` | Read a text file |
| `create_file` | Create a new file; fails if it exists |
| `write_file` | Create or overwrite a file |
| `edit_file` | Exact find/replace (`edits: [{old_text, new_text}]`); each `old_text` must match once; all-or-nothing |
| `append_file` | Append to a file |
| `move_file` | Move or rename a file or directory (`source`, `destination`, `overwrite`) |
| `delete_file` | Delete a file or empty directory |

## Git forge

fileapi also hosts git repos, grouped by project and named `<project>/<repo>`. Clone and push them with plain git over HTTPS, and create and browse them over MCP. Git is pure Python ([dulwich](https://www.dulwich.io)), so the host needs no git binary.

```sh
# The access token is the password; the username is ignored.
git clone https://x:<token>@<host>/git/<project>/<repo>.git
# Or keep the token out of the URL and out of .git/config:
git -c http.extraHeader="Authorization: Bearer <token>" clone https://<host>/git/<project>/<repo>.git
```

| Tool | What it does |
| --- | --- |
| `create_repo` | Create an empty repo (`project`, `name`, optional `description`, `default_branch`) and print its clone URL |
| `list_repos` | List repos, optionally one `project`'s |
| `list_branches` | Branches and tags of a repo |
| `browse_repo` | A directory listing or a file's content at a `ref` (branch, tag or sha) |
| `repo_log` | Recent commits, optionally only those touching a `path` |
| `commit_files` | Commit `{path: content}` (null deletes) to a branch, for clients without git. `expected_head` guards against races |
| `push_bundle` | Push without git credentials: upload a `git bundle create` file (base64). Fast-forward only unless `force` |
| `fetch_bundle` | Pull or clone without git credentials: returns a base64 bundle. Pass `have` (shas you already have) to get only new history |
| `repo_remote_url` | A git URL with a short-lived credential for one repo (default 60 min), for plain `git push`/`pull`/`clone` |
| `set_repo_visibility` | `public`: anyone can clone and fetch without a token. `private` (the default): token needed. Pushing always needs a token |
| `delete_repo` | Delete a repo and its history |

### Push and pull with only the MCP connection

An MCP client that is signed in can move commits without anyone typing a token into git:

- **`repo_remote_url` (any size):** the tool returns `https://x:fg_…@<host>/git/<project>/<repo>.git`, a credential that works for that one repo and expires (1 to 1440 minutes). The client runs `git push <url> main` with it. Passes are HMAC-signed under a secret in the KV store (`forge_secret`), so they work across replicas; delete that record to revoke all of them.
- **Bundles (small changes):** `git bundle create /tmp/p.bundle main ^origin/main`, base64 it, call `push_bundle`. For pulls, `fetch_bundle` with `have: [<sha of origin/main>]`, decode, `git fetch /tmp/f.bundle 'refs/heads/*:refs/remotes/origin/*'`. The bundle travels inside the tool call, so it costs model tokens; keep it to small pushes.

Storage follows the file store. With `DATABASE_URL`, objects and refs are Postgres rows, so repos survive FastAPI Cloud deploys and every replica sees the same repos; ref updates are compare-and-swap, so concurrent pushes cannot clobber each other. Without it, repos are ordinary bare repos under `FILEAPI_GIT_ROOT`. Pushes are capped at `FILEAPI_GIT_MAX_PUSH` bytes. The forge is meant for small and medium repos: Postgres stores each object as its own row, so very large histories clone slowly.

Access: the admin reaches every project; public repos clone anonymously. Nobody else has access.

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

FastAPI Cloud has no persistent disk (it's replaced on every deploy, restart and scale event), so files and the token go in Postgres. To make it impossible to lose state silently:

- The cloud entrypoint (`fileapi.main:app`) **refuses to start** without `DATABASE_URL`, unless you set `FILEAPI_ROOT` to say a local disk is intended.
- `just deploy` checks that `DATABASE_URL` is set on FastAPI Cloud and runs `just cloud-db` first if it isn't.
- Startup only runs `CREATE TABLE IF NOT EXISTS`, so deploys never touch existing data. `just cloud-db` reuses the existing Neon project.

With [just](https://github.com/casey/just), Node (for `npx neonctl`) and either the `fastapi` CLI or `uv` installed:

```sh
just launch       # FastAPI Cloud browser login, first deploy (creates the app),
                  # Neon browser login, creates a "fileapi" Neon project if needed,
                  # writes DATABASE_URL to .env and sets it on FastAPI Cloud (which redeploys)
```

After that, `just deploy` redeploys and `just logs` streams logs. The pieces also run on their own: `just login`, `just deploy`, `just cloud-db`.

**Running on a remote or headless machine?** The Neon browser login needs a callback to `localhost` on the machine running `just`, which fails over SSH. Use an API key instead: Neon Console → Account settings → **API keys** → Create new API key (a personal key; an organization key also works), then put it in `.env`:

```sh
NEON_API_KEY=napi_...
```

`neonctl` reads `NEON_API_KEY` automatically, and `just` loads `.env`, so no browser login is needed. (`fastapi login` uses a device code, so it already works remotely.)

`just neon-db` does the same without touching FastAPI Cloud (handy for local runs; `just dev` reads `.env`). Set `NEON_ORG_ID` if your Neon account has several organizations, or `NEON_PROJECT_NAME` to use another project name.

Or by hand, using the Neon/Supabase integration in the FastAPI Cloud dashboard (it sets `DATABASE_URL` for you):

```sh
pip install "fastapi[standard]"
fastapi login
fastapi deploy                      # entrypoint comes from [tool.fastapi] in pyproject.toml
# In the FastAPI Cloud dashboard: add the Neon (or Supabase) Postgres integration -> sets DATABASE_URL
fastapi deploy                      # redeploy so it picks up DATABASE_URL
```

Then connect a client (below) and sign in as the admin. Tables are created automatically on startup.

## Run elsewhere

```sh
pip install .
fileapi                              # files in ./data, sign-in records in ./state
# or: DATABASE_URL=postgresql://user:pass@host/db fileapi
```

Docker:

```sh
docker build -t fileapi .
docker run -p 8000:8000 -v fileapi-data:/data -v fileapi-state:/state fileapi
```

| Env var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | unset | Postgres connection string. When set, files, repos and sign-ins live in Postgres |
| `FILEAPI_ROOT` | `./data` | Filesystem backend: where files live |
| `FILEAPI_STATE_DIR` | `./state` | Filesystem backend: OAuth clients and tokens. Must be outside `FILEAPI_ROOT` |
| `FILEAPI_GIT_ROOT` | `./repos` | Filesystem backend: bare git repos. Must be outside `FILEAPI_ROOT` |
| `FILEAPI_GIT_MAX_PUSH` | `209715200` | Max git push (request body) in bytes |
| `FILEAPI_ADMIN_DID` | `did:plc:ngokl2gnmpbvuvrfckja3g7p` | The only atproto account allowed in (`nandi.uk`) |
| `FILEAPI_ADMIN_HANDLE` | `nandi.uk` | Login hint shown at the authorization server |
| `FILEAPI_TOKEN` | unset | Optional static admin token for scripts/CI |
| `FILEAPI_PUBLIC_URL` | from request | Public base URL (OAuth issuer, atproto client_id, git URLs) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | For `fileapi` CLI |
| `FILEAPI_MAX_BODY` | `10485760` | Max MCP request size in bytes |

Health check: `GET /health` returns `{"ok": true, "auth": "atproto", "admin": "<did>", "storage": "postgres" | "disk", "git": "postgres" | "disk"}`.

## Connect a client

Claude Code: `just claude-add` (or `claude mcp add --transport http fileapi https://<host>/mcp`), then run `/mcp` in Claude Code and pick fileapi to sign in in the browser.

claude.ai / Claude desktop: Settings → Connectors → Add custom connector → `https://<host>/mcp`, then Connect and sign in as the admin.

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
