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

fileapi also hosts git repos. Each repo belongs to the signed-in account and lives under its atproto handle: `<handle>/<repo>`, e.g. `nandi.uk/blog`, cloned from `https://<host>/git/nandi.uk/blog.git`. MCP tools and the web UI only need the repo name. Clone and push them with plain git over HTTPS, and create and browse them over MCP.

fileapi itself is mirrored there as `nandi.uk/fileapi` (GitHub stays primary). `just forge-mirror` pushes every GitHub branch and tag to the `forge` remote, and `just deploy` runs it after each deploy.

Repos from before owners existed (`<project>/<repo>`) move under the admin's handle the next time the server starts; a name that is already taken becomes `<old project>-<repo>`. The old name stays as an alias, so old clone URLs, remotes and `/forge/#…` links keep working. Renames leave an alias the same way; creating a repo with an alias's name takes the name over. `FILEAPI_FORGE_OWNER` overrides the namespace (default: the admin handle). Git is pure Python ([dulwich](https://www.dulwich.io)), so the host needs no git binary.

### Sign in from git in the browser

Install the credential helper once and plain `git clone` of a private repo opens the atproto sign-in page:

```sh
mkdir -p ~/.local/bin
curl -fsSL https://<host>/git-credential-fileapi -o ~/.local/bin/git-credential-fileapi
chmod +x ~/.local/bin/git-credential-fileapi     # needs python3 on PATH; stdlib only
git-credential-fileapi setup https://<host>      # or `just git-setup` from a checkout
git clone https://<host>/git/<handle>/<repo>.git
```

When the server answers `401`, git asks the helper. It registers itself as an OAuth client of fileapi, opens `/authorize` in the browser with a one-off redirect to `http://127.0.0.1:<random port>/callback`, swaps the code (PKCE) for tokens and gives git the access token as the password. Tokens live in `~/.config/git-credential-fileapi/tokens.json` (mode 600): the access token (24 h) is reused, then refreshed silently with the refresh token (90 days, rotated), so the browser only opens again after that. `setup` puts an empty `credential.<url>.helper` first, which keeps global helpers (store, cache, libsecret) from shadowing it with a stale token. Other commands: `git-credential-fileapi login|logout [url]`, `status`. On a machine with no browser, open the printed URL on the same machine (the redirect goes to 127.0.0.1).

### Big files go to Git LFS automatically

Every repo has a Git LFS endpoint at `https://<host>/git/<handle>/<repo>.git/info/lfs` (the batch API with the basic transfer; git-lfs finds it from the remote URL and signs in through the same credential helper). Download works wherever clone does, so public repos serve LFS files anonymously; upload needs push access. Objects are stored per repo: chunked rows in Postgres (`fileapi_lfs_objects`, `fileapi_lfs_chunks`) or `<repo>.git/lfs/objects/` on disk, and are deleted with the repo. `FILEAPI_LFS_MAX_OBJECT` caps one object (default 2 GiB).

With [git-lfs](https://git-lfs.com) installed (`just git-setup` copies the one pixi provides to `~/.local/bin` if git can't find one already), `git-credential-fileapi setup` also runs `git lfs install` and adds a pre-commit hook to git's template dir (`init.templateDir`, unless you already have one, in which case the hook goes into yours), so every repo cloned or created afterwards has it. On commit, in a repo with a fileapi remote, staged files over the threshold are added to `.gitattributes` with `git lfs track --filename` and re-staged as LFS pointers; `git push` then uploads them. Nothing else to do, and the hook never blocks a commit.

```sh
git config --global fileapi.lfsThreshold 50m   # default 10m; 0 turns it off
git config fileapi.autoLfs true                # auto (default: repos with a fileapi remote) | true | false
git-credential-fileapi lfs-install             # in a repo cloned before setup
```

Files committed before they crossed the threshold stay in git history; `git lfs migrate import --include=<path>` moves them. The repo browser and `browse_repo` show LFS files as such (size and sha256) rather than the pointer text.

### Tokens by hand

```sh
# The access token is the password; the username is ignored.
git clone https://x:<token>@<host>/git/<handle>/<repo>.git
# Or keep the token out of the URL and out of .git/config:
git -c http.extraHeader="Authorization: Bearer <token>" clone https://<host>/git/<handle>/<repo>.git
```

| Tool | What it does |
| --- | --- |
| `create_repo` | Create an empty repo under your handle (`name`, optional `description`, `default_branch`) and print its clone URL |
| `list_repos` | List your repos |
| `list_branches` | Branches and tags of a repo |
| `browse_repo` | A directory listing or a file's content at a `ref` (branch, tag or sha) |
| `repo_log` | Recent commits, optionally only those touching a `path` |
| `commit_files` | Commit `{path: content}` (null deletes) to a branch, for clients without git. `expected_head` guards against races |
| `push_bundle` | Push without git credentials: upload a `git bundle create` file (base64). Fast-forward only unless `force` |
| `fetch_bundle` | Pull or clone without git credentials: returns a base64 bundle. Pass `have` (shas you already have) to get only new history |
| `repo_remote_url` | A git URL with a short-lived credential for one repo (default 60 min), for plain `git push`/`pull`/`clone` |
| `set_repo_visibility` | `public`: anyone can clone and fetch without a token. `private` (the default): token needed. Pushing always needs a token |
| `delete_repo` | Delete a repo and its history |

Every `repo` argument takes the bare name (`blog`); `<owner>/<name>` and old aliases work too.

### Push and pull with only the MCP connection

An MCP client that is signed in can move commits without anyone typing a token into git:

- **`repo_remote_url` (any size):** the tool returns `https://x:fg_…@<host>/git/<handle>/<repo>.git`, a credential that works for that one repo and expires (1 to 1440 minutes). The client runs `git push <url> main` with it. Passes are HMAC-signed under a secret in the KV store (`forge_secret`), so they work across replicas; delete that record to revoke all of them.
- **Bundles (small changes):** `git bundle create /tmp/p.bundle main ^origin/main`, base64 it, call `push_bundle`. For pulls, `fetch_bundle` with `have: [<sha of origin/main>]`, decode, `git fetch /tmp/f.bundle 'refs/heads/*:refs/remotes/origin/*'`. The bundle travels inside the tool call, so it costs model tokens; keep it to small pushes.

Storage follows the file store. With `DATABASE_URL`, objects and refs are Postgres rows, so repos survive FastAPI Cloud deploys and every replica sees the same repos; ref updates are compare-and-swap, so concurrent pushes cannot clobber each other. Without it, repos are ordinary bare repos under `FILEAPI_GIT_ROOT`. Pushes are capped at `FILEAPI_GIT_MAX_PUSH` bytes. Requests stream through git rather than sitting in memory: the pushed pack is spooled to a temp file (`FILEAPI_GIT_TMPDIR`) and stored in batches of at most 8 MiB, so a whole-history push fits a 500 MiB Nano instance, and while it is stored the client sees `remote: fileapi: storing objects: N` every few seconds, which also keeps proxies from timing the request out. The forge is meant for small and medium repos: Postgres stores each object as its own row, so very large histories clone slowly.

Access: the admin reaches every repo; public repos clone anonymously. Nobody else has access.

### Web UI

`/forge/` is a small browser UI for the forge: the repo list, files and their contents at any branch, tag or commit, and the commit log. Anyone sees the public repos; **Sign in** (atproto, same admin-only rule as MCP) shows the private ones too, and lets you create repos (just a name) and change their settings (rename keeps the owner). The URL tracks where you are (`/forge/#<handle>/<repo>/<path>`), so links can be shared. A folder's README (`README.md`, `.rst`, `.txt`, …) shows rendered under its file list, like on GitHub, and Markdown files open rendered with a **Preview / Code** switch; relative links and images resolve inside the repo.

It is a Qt for WebAssembly app (Qt Widgets, C++, in `ui/`) on a small JSON API:

| Endpoint | Returns |
| --- | --- |
| `GET /api/forge/me` | Whether the request is signed in, and `owner` (the handle repos live under) |
| `GET /api/forge/repos` | Repos the caller can see (public ones when anonymous) |
| `POST /api/forge/repos` | Signed in: create `{name, description?, default_branch?, public?}` under your handle |
| `PATCH`/`DELETE /api/forge/repos/<owner>/<repo>` | Signed in: change `description`, `default_branch`, `public`, `name` (same owner), or delete |
| `GET /api/forge/repos/<owner>/<repo>` | Description, visibility, default branch, branches, tags, clone URL |
| `GET /api/forge/repos/<owner>/<repo>/tree?ref=&path=` | A directory listing or a file's content |
| `GET /api/forge/repos/<owner>/<repo>/markdown?ref=&path=` | A Markdown file as sanitized HTML (GitHub-flavoured; no script, styles or event handlers) |
| `GET /api/forge/repos/<owner>/<repo>/raw?ref=&path=` | A file's bytes (README images), sandboxed by CSP |
| `GET /api/forge/repos/<owner>/<repo>/commits?ref=&path=&limit=` | Recent commits (also at `/log`, but ad blockers block that path) |

Send `Authorization: Bearer <access token>` for private repos; to anonymous callers a private repo is a 404.
The page signs in as a public OAuth client of fileapi itself (PKCE, redirect back to `/forge/`) and keeps the token in the browser's localStorage.

The built app is committed in `fileapi/static/forge/` (the wasm gzipped, about 5 MB), so `just deploy` needs no build step. After changing `ui/`, rebuild with:

```sh
just ui   # pixi `ui` env: Qt 6.11 for WebAssembly + emsdk from prefix.dev/nandi-testing (Linux x86_64), cached by pixi
```

### Secrets and deploy jobs

Each repo can hold **secrets** (env-var names like `FLY_API_TOKEN`), set in the web UI (**Settings → Secrets**), over the API, or with the `set_repo_secret` MCP tool. They are encrypted with AES-256-GCM under `FILEAPI_SECRETS_KEY` before they're stored, and write-only: you can list, replace and delete them, never read them back. Without `FILEAPI_SECRETS_KEY` secrets are off (the rest of the forge works); `just secrets-key` generates one and sets it locally and on FastAPI Cloud.

**User secrets** work the same way but belong to you, not a repo: every repo you own gets them in its jobs, so a token like `DEPOT_TOKEN` or `FLY_API_TOKEN` is set once. Manage them on the web UI's **Settings** page (top bar), over the API, or with the `set_user_secret` / `list_user_secrets` / `delete_user_secret` MCP tools. A repo secret with the same name overrides the user secret for that repo; the repo's Secrets list shows the user secrets it inherits, dimmed, and struck through where overridden.

A repo opts in to **auto-deploy** by committing `.forge/jobs.toml`. Every push (git, `push_bundle` or `commit_files`) reads it from the pushed commit and runs the jobs whose `branches` match, in the background on the server:

```toml
[[job]]                        # a shell script (sh -e), run in a checkout of the pushed commit
name = "deploy"
branches = ["main"]            # default: the default branch. Globs work: ["release/*"], ["*"]
timeout = 300                  # seconds (default 600, max 3600)
run = """
python scripts/deploy.py       # secrets are env vars, plus FORGE_REPO, FORGE_BRANCH, FORGE_SHA, FORGE_BEFORE...
"""

[[job]]                        # an HTTP POST: deploy hooks (Render, Vercel, Netlify, Coolify...), no curl needed
name = "render"
post = "${RENDER_DEPLOY_HOOK}"
headers = { Authorization = "Bearer ${SOME_TOKEN}" }
body = '{"sha": "${FORGE_SHA}"}'
```

Jobs get a clean environment (only `PATH` from the server, never `DATABASE_URL` or the key), and secret values are masked as `***` in their output. The **Deploys** tab (and the `deploy_runs` / `run_deploy` MCP tools) shows the last 30 runs with their output and has **Run now**. A run lives in the replica that took the push, so a long job can be cut off if FastAPI Cloud scales that replica down; it then shows as `lost`. A job that can't apply, like a mirror whose token secret isn't set, shows as `skipped`. Set `FILEAPI_JOBS=off` to turn jobs off.

#### Container builds on Depot

A `depot` job builds a Docker image of the pushed commit on [Depot](https://depot.dev): the build runs on Depot's remote BuildKit builders (native amd64 and arm64, shared layer cache), so the server needs no Docker. The server downloads the Depot CLI the first time a job needs it (pinned to 2.103.9; `FILEAPI_DEPOT_CLI_VERSION` changes it, `FILEAPI_DEPOT_CLI` points at your own binary).

```toml
[[job]]
name = "image"
[job.depot]
project = "abc123xyz"            # Depot project ID; default: the DEPOT_PROJECT_ID secret
file = "Dockerfile"              # default: ./Dockerfile
context = "."
platforms = ["linux/amd64", "linux/arm64"]
tags = ["ghcr.io/me/app:${FORGE_SHA}", "ghcr.io/me/app:latest"]
push = true                      # push the tags (default false)
save = false                     # keep it in the Depot registry (default: true unless push)
build_args = { VERSION = "${FORGE_SHA}" }
target = "runtime"
login = { server = "ghcr.io", username = "me", password = "${GHCR_TOKEN}" }  # registry login for push
args = ["--no-cache"]            # any other `depot build` flags
```

`depot = true` alone builds `./Dockerfile` and saves the image to the Depot registry as `registry.depot.dev/<project>:<build id>`. Every depot job needs the **`DEPOT_TOKEN`** secret. The easy way is `just depot-token <repo> [depot project]`: it signs you in to Depot and to fileapi in the browser, finds or creates the Depot project (default: named after the repo), creates a project token for it, and stores `DEPOT_TOKEN` and `DEPOT_PROJECT_ID` as the repo's secrets (`DEPOT_PROJECT_ID` isn't masked in output). For all your repos at once, `just depot-user-token [repo]` stores your Depot user token (`depot login token`, which can build in any project you can see) as the user secret `DEPOT_TOKEN`; with a repo it also sets that repo's `DEPOT_PROJECT_ID`. Each repo still needs its project: `project` in the job, a `DEPOT_PROJECT_ID` secret, or one run of that command per repo. By hand: a project token from Depot → the project → **Settings → Project tokens**, or an organization token from **Organization settings → API tokens**. The run's output shows the Depot build link, and the run record gets a `depot` object (`build_id`, `url`, `image`, `digest`). Shell jobs whose script mentions `depot` get the CLI on `PATH` too, for `depot bake` or anything else.

A `mirror` job pushes every branch and tag of the repo to another git host, say a backup on GitHub. Only fast-forwards and new refs go: a ref that moved on the other side is reported in the run and left alone (`force = true` overwrites instead). Nothing is deleted there. `token` is the HTTPS password (username `x-access-token` unless you set `username`; if the secret it names isn't set, the run is skipped); for GitHub, a fine-grained personal access token with **Contents: Read and write** on that one repo.

```toml
[[job]]
name = "github-mirror"
branches = ["*"]
tags = ["*"]
[job.mirror]
url = "https://github.com/me/app.git"
token = "${GITHUB_MIRROR_TOKEN}"
```

A `depot_ci` job starts a [Depot CI](https://depot.dev/docs/ci/overview) workflow (`depot ci dispatch`, so the workflow needs `on: workflow_dispatch`) and follows it to the end. Depot CI reads code from GitHub, so pair it with a mirror job and `after`: a job with `after = "<job>"` waits for that job of the same push and runs only if it succeeded (otherwise it shows as `skipped`). `secrets` are copied into Depot CI's secrets for that repo before each dispatch, so the forge stays the one place you set them. It needs `DEPOT_TOKEN` as an organization API token or your user token (a project token can't start CI runs). The run's output shows the Depot links and the record gets `depot.ci_run_id` and `depot.url`. Because the work happens on Depot, a deploy of this server itself is fine: when the new version replaces the replica that was following the run, the new server picks the wait back up at startup, and the outcome still lands in the run.

```toml
[[job]]
name = "deploy"
branches = ["main"]
after = "github-mirror"
[job.depot_ci]
repo = "me/app"                   # the GitHub repo, with Depot's GitHub app installed
workflow = "deploy.yml"           # .depot/workflows/deploy.yml
inputs = { sha = "${FORGE_SHA}" } # workflow_dispatch inputs
secrets = ["FASTAPI_TOKEN"]
# ref = "main"                    # default: the pushed branch or tag
# wait = false                    # just start it
```

Running jobs save their output every couple of seconds, so a run's links show up in the Deploys tab while it's going.

| Endpoint (signed in) | Does |
| --- | --- |
| `GET /api/forge/repos/<owner>/<repo>/secrets` | Secret names and update times (`enabled: false` without the key), plus `inherited`: your user secrets with `overridden` |
| `PUT /api/forge/repos/<owner>/<repo>/secrets/<NAME>` | `{"value": "..."}` create or replace |
| `DELETE /api/forge/repos/<owner>/<repo>/secrets/<NAME>` | Delete |
| `GET /api/forge/user/secrets` | Your user secrets (every repo you own gets them) |
| `PUT /api/forge/user/secrets/<NAME>` | `{"value": "..."}` create or replace |
| `DELETE /api/forge/user/secrets/<NAME>` | Delete |
| `GET /api/forge/repos/<owner>/<repo>/runs[/<id>]` | Recent runs, or one with its output |
| `POST /api/forge/repos/<owner>/<repo>/runs` | `{"ref"?: branch, "tag"?: tag, "job"?: name}` run now |

### Releases

Each repo can have releases: a tag (e.g. `v1.2.0`; it doesn't have to exist in git), a title, Markdown notes
and uploaded files. They show in the web UI's **Releases** tab, where the owner can make, edit, publish and
delete them and upload files. Anyone who can see the repo can download: public repos anonymously, private
ones signed in or through the signed `download_url` (valid an hour) the API hands out. Drafts are only
visible to you; `latest` in a URL is the newest release that isn't a draft or prerelease.

Files are stored with the repo (Postgres chunks, or `<repo>.git/releases/` on disk), so they follow renames
and go with deletes. Up to `FILEAPI_RELEASE_MAX_ASSET` bytes each (default 2 GiB). Uploads can come in
pieces (`offset`, `final=false`), which keeps each request small however big the file; mind the Postgres
plan's storage quota.

```sh
# curl: the body is the file (Bearer = your access token, or a repo pass / FORGE_TOKEN)
curl -H "Authorization: Bearer $TOKEN" -T dist/app.tar.gz \
  "https://<host>/api/forge/repos/<owner>/<repo>/releases/v1.2.0/assets/app.tar.gz?create=true"
# the client (`just git-setup` installs it as fileapi-release): splits big files, signs in in the browser
fileapi-release upload --create v1.2.0 dist/*
fileapi-release create v1.3.0 --notes-file CHANGES.md --draft
fileapi-release download latest -o ./bin
```

From CI: a job with `tags` runs when a matching tag is pushed, and every job gets `FORGE_TOKEN`,
`FORGE_URL`, `FORGE_API` and `forge-release` on PATH:

```toml
[[job]]
name = "release"
tags = ["v*"]
run = "make dist && forge-release upload --create $FORGE_TAG dist/*"
```

MCP: `list_releases`, `create_release`, `update_release`, `delete_release`, `upload_release_asset` (small
files, base64 or text), `delete_release_asset`, and `release_upload_url` (a short-lived credential plus
curl commands for big files).

| Endpoint | |
|---|---|
| `GET /api/forge/repos/<owner>/<repo>/releases[/<tag>]` | Releases (newest first), or one; `latest` works |
| `POST /api/forge/repos/<owner>/<repo>/releases` | `{"tag", "name"?, "notes"?, "ref"?, "prerelease"?, "draft"?}` |
| `PATCH /api/forge/repos/<owner>/<repo>/releases/<tag>` | Change `tag`, `name`, `notes`, `prerelease`, `draft` |
| `DELETE /api/forge/repos/<owner>/<repo>/releases/<tag>` | Delete it and its files |
| `PUT .../releases/<tag>/assets/<name>?offset=&final=&sha256=&create=` | Upload (raw body) |
| `GET .../releases/<tag>/assets/<name>` | Download |
| `DELETE .../releases/<tag>/assets/<name>` | Delete one file |

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

Tooling is managed with [pixi](https://pixi.sh): it installs Python, the dependencies, the FastAPI Cloud CLI and Node (for `npx neonctl`) into `.pixi/`, pinned by `pixi.lock`. You only need [just](https://github.com/casey/just) and pixi:

```sh
curl -fsSL https://pixi.sh/install.sh | sh   # or: sudo pacman -S pixi
just install      # pixi install (any recipe also does this on first use)
just launch       # FastAPI Cloud browser login, first deploy (creates the app),
                  # Neon browser login, creates a "fileapi" Neon project if needed,
                  # writes DATABASE_URL to .env and sets it on FastAPI Cloud (which redeploys)
```

After that, deploys go through CI (next section); `just deploy-only` still deploys from your machine and `just logs` streams logs. The pieces also run on their own: `just login`, `just deploy`, `just cloud-db`.

**Running on a remote or headless machine?** The Neon browser login needs a callback to `localhost` on the machine running `just`, which fails over SSH. Use an API key instead: Neon Console → Account settings → **API keys** → Create new API key (a personal key; an organization key also works), then put it in `.env`:

```sh
NEON_API_KEY=napi_...
```

`neonctl` reads `NEON_API_KEY` automatically, and `just` loads `.env`, so no browser login is needed. (`fastapi login` uses a device code, so it already works remotely.)

`just neon-db` does the same without touching FastAPI Cloud (handy for local runs; `just dev` reads `.env`). Set `NEON_ORG_ID` if your Neon account has several organizations, or `NEON_PROJECT_NAME` to use another project name.

Or by hand, using the Neon/Supabase integration in the FastAPI Cloud dashboard (it sets `DATABASE_URL` for you):

```sh
pixi run fastapi login
pixi run fastapi deploy             # entrypoint comes from [tool.fastapi] in pyproject.toml
# In the FastAPI Cloud dashboard: add the Neon (or Supabase) Postgres integration -> sets DATABASE_URL
pixi run fastapi deploy             # redeploy so it picks up DATABASE_URL
```

Then connect a client (below) and sign in as the admin. Tables are created automatically on startup.

### Deploys from CI

fileapi's own repo lives on its forge as `nandi.uk/fileapi`, and that's where to push. Its [`.forge/jobs.toml`](.forge/jobs.toml) deploys every push to `main` from a [Depot](https://depot.dev) build: [`ci/Dockerfile.deploy`](ci/Dockerfile.deploy) installs the FastAPI CLI and runs [`ci/deploy.py`](ci/deploy.py) on Depot's builders, with the `FASTAPI_TOKEN` secret mounted as a build secret (never in a layer; nothing is pushed or saved). The code goes straight from the forge to Depot, no GitHub involved, and a deploy that replaces the server running the job doesn't matter: once `fastapi deploy` has uploaded, FastAPI Cloud finishes on its own.

`ci/deploy.py` deploys to the app `fileapi` (its ID is in the Dockerfile: deploy tokens can't look an app up by name), checks `DATABASE_URL` and `FILEAPI_SECRETS_KEY` are set there when the token may read env vars (a deploy token gets 403, so it warns; the app refuses to start without `DATABASE_URL` anyway, and FastAPI Cloud then keeps the old version), checks the committed forge UI build is present (CI doesn't run `just ui`), writes the commit to `fileapi/BUILD` (served as `build` in `/health`) and the forge run's ID to `fileapi/BUILD_RUN`, runs `fastapi deploy`, and waits for `/health` to report the commit. The new server marks that run successful when it starts, in case the job died with the replica the deploy replaced.

`just deploy` pushes your current commit to `main` on the forge and prints the CI run links (`/forge/#nandi.uk/fileapi/-/deploys/<run>`, plus the Depot build once it starts), following them to the end. Secrets used: `DEPOT_TOKEN` and `DEPOT_PROJECT_ID` (user secrets, from `just depot-user-token`) and `FASTAPI_TOKEN` (repo secret: a deploy token from `pixi run fastapi cloud tokens create --name forge-ci`).

A `github-mirror` job backs every push up to `github.com/nandi-conda/fileapi`, fast-forwards only. It's skipped until there's a `GITHUB_MIRROR_TOKEN` secret: a GitHub fine-grained personal access token (resource owner `nandi-conda`, only the `fileapi` repo, **Contents: Read and write**).

### Blob storage on Backblaze B2

With Postgres, the bulky bytes live in a [Backblaze B2](https://www.backblaze.com/cloud-storage) bucket instead of the database: git blobs of 32 KiB or more (compressed), Git LFS objects, release assets and stored files of 32 KiB or more (`FILEAPI_BLOB_MIN_BYTES`). Refs, metadata, commits, trees and small blobs stay in Postgres, so clones and listings stay one query. Each blob gets a random 128-bit name recorded in its row, and the app serves everything itself with the account key, so **keep the bucket private**.

Configuration ([`fileapi/blobs.py`](fileapi/blobs.py)): `B2_KEY_ID` (the application key's ID, not secret), `B2_APPLICATION_KEY` or `B2_TOKEN` (the key itself), `B2_BUCKET` (default `fileapi`). They're read from env vars, then the forge owner's user secrets, then `fileapi/B2_CONFIG`, which `ci/deploy.py` writes from the `B2_KEY_ID`/`B2_BUCKET` build args in `.forge/jobs.toml`. So for this deployment the key ID is in the CI config and the key is the `B2_TOKEN` user secret; nothing is set on FastAPI Cloud. B2 is used through its native API, so no endpoint or region is needed.

Moving existing data ([`fileapi/blob_migrate.py`](fileapi/blob_migrate.py), MCP tool `blob_storage`):

- **copy** runs on its own after each start (`FILEAPI_BLOB_COPY=off` turns that off): rows still only in Postgres are uploaded and get their B2 key, and the Postgres copy stays. Git blobs no branch or tag reaches are skipped (that's `gc_repo`'s job).
- **free** (`blob_storage action=free`) drops the Postgres copy of each row whose blob is verified in B2 (it exists, same size), then runs `VACUUM`. This is the step that deletes data from the database.
- **sweep** lists blobs no row points at (and with `apply=true` deletes them); deleting a repo, a release asset, a file or gc'd objects deletes their blobs right away, and sweep catches what a failed delete left.
- **status** shows where bytes live per table and the last run; `/health` reports `blobs` and `blob_copy`.

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
| `FILEAPI_GIT_TMPDIR` | system temp dir | Where incoming packs are spooled while a push is stored |
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
just test                                               # filesystem backend (pixi run pytest -q)
TEST_DATABASE_URL=postgresql://... just test            # also runs everything against Postgres
```
