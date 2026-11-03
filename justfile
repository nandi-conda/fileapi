# fileapi tasks. Needs: just, Python 3.11+, Node (for npx neonctl).
set dotenv-load

neon := "npx -y neonctl@8"
# FastAPI CLI (includes the FastAPI Cloud commands); falls back to uvx if it isn't installed
fastapi := if `command -v fastapi >/dev/null 2>&1 && echo yes || echo no` == "yes" { "fastapi" } else { "uvx --from 'fastapi[standard]' fastapi" }
neon_project := env("NEON_PROJECT_NAME", "fileapi")

default:
    @just --list

# Install the package with dev dependencies into the current environment
install:
    pip install -e '.[dev]'

# Run the test suite (set TEST_DATABASE_URL to also test against Postgres)
test:
    pytest -q

# Run locally with auto-reload (uses DATABASE_URL from .env if present)
dev:
    {{fastapi}} dev

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
    id=$({{neon}} projects list $org_flag -o json | python3 -c '
    import json, sys
    data = json.load(sys.stdin)
    projects = data.get("projects", []) if isinstance(data, dict) else data
    print(next((p["id"] for p in projects if p.get("name") == sys.argv[1]), ""))
    ' "{{neon_project}}")
    if [ -z "$id" ]; then
        echo "Creating Neon project '{{neon_project}}'..."
        id=$({{neon}} projects create --name "{{neon_project}}" $org_flag -o json | python3 -c '
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

# Deploy to FastAPI Cloud (logs in first if needed; the first run creates the app)
deploy: login
    {{fastapi}} deploy

# Same as neon-db, then store DATABASE_URL on FastAPI Cloud (this redeploys the app)
cloud-db: neon-db login
    #!/usr/bin/env bash
    set -euo pipefail
    url=$(grep '^DATABASE_URL=' .env | cut -d= -f2-)
    {{fastapi}} cloud env set DATABASE_URL "$url"
    echo "DATABASE_URL set on FastAPI Cloud."

# Neon database + first deploy in one go: login, deploy, set DATABASE_URL
launch: deploy cloud-db

# Claim a fresh deployment and print its access token (once!), e.g. just claim https://fileapi.fastapicloud.dev
claim url:
    uv run --with 'mcp>=2.3,<3' python scripts/claim.py {{url}}

# Stream logs from the deployed app
logs:
    {{fastapi}} cloud logs --follow
