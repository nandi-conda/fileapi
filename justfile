# fileapi tasks. Needs: just and pixi (https://pixi.sh). pixi provides Python, the deps,
# the FastAPI Cloud CLI and Node (for npx neonctl); run `just install` once, or let any recipe do it.
set dotenv-load

pixi := if `command -v pixi >/dev/null 2>&1 && echo yes || echo no` == "yes" { "pixi" } else { error("pixi isn't installed. Install it with: curl -fsSL https://pixi.sh/install.sh | sh  (or: sudo pacman -S pixi), then open a new shell.") }
run := pixi + " run"
neon := run + " npx -y neonctl@8"
fastapi := run + " fastapi"
python := run + " python"
neon_project := env("NEON_PROJECT_NAME", "fileapi")

default:
    @just --list

# Create or update the pixi environment (.pixi/) from pixi.lock
install:
    {{pixi}} install

# Run the test suite (set TEST_DATABASE_URL to also test against Postgres)
test:
    {{run}} pytest -q

# Rebuild the forge web UI (Qt for WebAssembly) into fileapi/static/forge/; commit the result
ui:
    {{run}} ui/build.sh

# Generate ui/build/compile_commands.json so clangd resolves Qt headers (uses the host Qt; editing only)
ui-lsp:
    cmake -S ui -B ui/build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -DCMAKE_BUILD_TYPE=Debug

# Run locally with auto-reload (uses DATABASE_URL from .env if present)
dev:
    FILEAPI_ROOT="${FILEAPI_ROOT:-./data}" {{fastapi}} dev

# Log in to Neon in the browser, unless NEON_API_KEY is set (use that on remote/headless machines)
neon-login:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -n "${NEON_API_KEY:-}" ]; then
        {{neon}} me >/dev/null || { echo "NEON_API_KEY was rejected by Neon. Create a new one: Neon Console > Account settings > API keys." >&2; exit 1; }
        echo "Using NEON_API_KEY."
    elif {{neon}} me >/dev/null 2>&1; then
        echo "Already logged in to Neon."
    else
        echo "Opening Neon login in your browser. Finish it promptly: the login times out after a short wait."
        {{neon}} auth || { echo "Neon login didn't complete (it may have timed out). Run the same just command again to retry." >&2; exit 1; }
    fi

# Log in to Neon, create the "fileapi" project if needed, and write DATABASE_URL to .env
neon-db: neon-login
    #!/usr/bin/env bash
    set -euo pipefail
    org_flag=""
    if [ -n "${NEON_ORG_ID:-}" ]; then org_flag="--org-id $NEON_ORG_ID"; fi
    id=$({{neon}} projects list $org_flag -o json | {{python}} -c '
    import json, sys
    data = json.load(sys.stdin)
    projects = data.get("projects", []) if isinstance(data, dict) else data
    print(next((p["id"] for p in projects if p.get("name") == sys.argv[1]), ""))
    ' "{{neon_project}}")
    if [ -z "$id" ]; then
        echo "Creating Neon project '{{neon_project}}'..."
        id=$({{neon}} projects create --name "{{neon_project}}" $org_flag -o json | {{python}} -c '
    import json, sys
    data = json.load(sys.stdin)
    print(data.get("project", data)["id"])
    ')
    else
        echo "Using existing Neon project '{{neon_project}}' ($id)."
    fi
    url=$({{neon}} connection-string --project-id "$id" | tr -d '[:space:]')
    case "$url" in postgres*) ;; *) echo "Could not read a connection string from neonctl" >&2; exit 1;; esac
    touch .env
    grep -v '^DATABASE_URL=' .env > .env.tmp || true
    echo "DATABASE_URL=$url" >> .env.tmp
    mv .env.tmp .env
    chmod 600 .env
    echo "Wrote DATABASE_URL to .env (project $id)."

# Log in to FastAPI Cloud in the browser (skipped if already logged in)
login:
    @{{fastapi}} cloud whoami >/dev/null 2>&1 && echo "Already logged in to FastAPI Cloud." || {{fastapi}} login

# Link this folder to an existing FastAPI Cloud app (writes .fastapicloud/cloud.json, which isn't committed)
link app_id="": login
    {{fastapi}} cloud link {{app_id}}

# Deploy to FastAPI Cloud. Makes sure DATABASE_URL is set there first so state survives the deploy.
deploy: login
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f .fastapicloud/cloud.json ]; then
        echo "This folder isn't linked to a FastAPI Cloud app yet. Pick the existing fileapi app below."
        echo "(Never deployed before? Press Ctrl-C and run 'just launch' instead.)"
        just link
    fi
    if ! {{fastapi}} cloud env list 2>/dev/null | grep -q 'DATABASE_URL'; then
        echo "DATABASE_URL is not set on FastAPI Cloud, so a deploy would start on a wiped disk. Setting it up now..."
        just cloud-db
    fi
    {{fastapi}} deploy

# Same as neon-db, then store DATABASE_URL on FastAPI Cloud (this redeploys the app)
cloud-db: neon-db login
    #!/usr/bin/env bash
    set -euo pipefail
    url=$(grep '^DATABASE_URL=' .env | cut -d= -f2-)
    {{fastapi}} cloud env set DATABASE_URL "$url"
    echo "DATABASE_URL set on FastAPI Cloud."

# First deploy: creates the app, then sets DATABASE_URL (which redeploys with Postgres)
launch: login
    #!/usr/bin/env bash
    set -euo pipefail
    # The first build refuses to start without DATABASE_URL; that's expected, it just creates the app.
    {{fastapi}} deploy || echo "(First deploy can't start without DATABASE_URL yet; setting it now.)"
    just cloud-db

fileapi_url := env("FILEAPI_URL", "https://fileapi.fastapicloud.dev")

# Add fileapi to Claude Code (sign in afterwards with /mcp in Claude Code). scope: user, project or local
claude-add url=fileapi_url scope="user":
    #!/usr/bin/env bash
    set -euo pipefail
    command -v claude >/dev/null || { echo "The claude CLI isn't installed: https://docs.claude.com/en/docs/claude-code" >&2; exit 1; }
    url="{{url}}"; url="${url%/}"; url="${url%/mcp}"
    claude mcp remove fileapi -s {{scope}} >/dev/null 2>&1 || true
    claude mcp add --transport http --scope {{scope}} fileapi "$url/mcp"
    echo "Added fileapi to Claude Code ({{scope}} scope). Run /mcp in Claude Code and pick fileapi to sign in as nandi.uk."

# Install the git credential helper to ~/.local/bin and use it for this server: plain `git clone` then signs in in the browser
git-setup url=fileapi_url:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v python3 >/dev/null || { echo "git-credential-fileapi needs python3 on PATH." >&2; exit 1; }
    mkdir -p ~/.local/bin
    install -m 755 fileapi/git_credential.py ~/.local/bin/git-credential-fileapi
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to PATH so git can find git-credential-fileapi." >&2;; esac
    ~/.local/bin/git-credential-fileapi setup "{{url}}"
