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
    {{pixi}} run -e ui ui/build.sh

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

# Deploy through CI: push this branch's commit to main on the forge (nandi.uk/fileapi), which runs the FastAPI Cloud deploy on Depot CI (.forge/jobs.toml). Prints the CI run links and follows
# them. Signs in through git-credential-fileapi (`just git-setup` once). `just deploy-only` deploys from here.
deploy url=fileapi_url repo="nandi.uk/fileapi":
    #!/usr/bin/env bash
    set -euo pipefail
    base="{{url}}"; base="${base%/}"; base="${base%/mcp}"
    forge="$base/git/{{repo}}.git"
    git remote get-url forge >/dev/null 2>&1 || git remote add forge "$forge"
    git fetch --quiet forge main
    if ! git merge-base --is-ancestor FETCH_HEAD HEAD; then
        echo "main on the forge has commits this branch doesn't: git pull --rebase forge main, then retry." >&2
        exit 1
    fi
    sha=$(git rev-parse HEAD)
    if [ "$(git rev-parse FETCH_HEAD)" = "$sha" ]; then
        echo "The forge already has $sha on main. To deploy it again: Run now in the forge UI (Deploys tab)."
        exit 0
    fi
    git push forge "$sha:refs/heads/main"
    {{python}} -m fileapi.ci_watch --url "$base" "{{repo}}" "$sha"

# Deploy to FastAPI Cloud without pulling first
deploy-only: login
    #!/usr/bin/env bash
    set -euo pipefail
    if [ ! -f .fastapicloud/cloud.json ]; then
        echo "This folder isn't linked to a FastAPI Cloud app yet. Pick the existing fileapi app below."
        echo "(Never deployed before? Press Ctrl-C and run 'just launch' instead.)"
        just link
    fi
    # Ask FastAPI Cloud (as JSON) whether DATABASE_URL is set. Only a clear "not set" provisions it;
    # if the check itself fails we stop rather than risk replacing a working database.
    if ! env_json=$({{fastapi}} cloud env list --json); then
        echo "Couldn't list the FastAPI Cloud env vars (see the error above), so not deploying." >&2
        exit 1
    fi
    has_db=$(printf '%s' "$env_json" | {{python}} -c '
    import json, sys
    out = json.load(sys.stdin)
    names = {v.get("name") for v in out.get("data", {}).get("variables", [])}
    print("yes" if "DATABASE_URL" in names else "no")
    ')
    if [ "$has_db" = "no" ]; then
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

# Generate FILEAPI_SECRETS_KEY (encrypts repo secrets) into .env and set it on FastAPI Cloud. Keeps an existing one.
secrets-key: login
    #!/usr/bin/env bash
    set -euo pipefail
    touch .env
    key=$(grep '^FILEAPI_SECRETS_KEY=' .env | cut -d= -f2- || true)
    if [ -z "$key" ]; then
        key=$({{python}} -c 'import base64, secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())')
        echo "FILEAPI_SECRETS_KEY=$key" >> .env
        chmod 600 .env
        echo "Generated FILEAPI_SECRETS_KEY in .env (back it up: without it, stored secrets can't be decrypted)."
    fi
    {{fastapi}} cloud env set FILEAPI_SECRETS_KEY "$key"
    echo "FILEAPI_SECRETS_KEY set on FastAPI Cloud."

# Connect a forge repo to Depot: browser sign-in to Depot and fileapi, find/create the Depot project
# (default: named after the repo), make a project token, store DEPOT_TOKEN + DEPOT_PROJECT_ID as repo secrets
depot-token repo project="" url=fileapi_url:
    {{python}} -m fileapi.depot_setup --url "{{url}}" "{{repo}}" "{{project}}"

# Your Depot user token as a user secret (DEPOT_TOKEN) that every repo you own gets; with a repo, also sets
# that repo's DEPOT_PROJECT_ID (finding or creating the Depot project)
depot-user-token repo="" project="" url=fileapi_url:
    {{python}} -m fileapi.depot_setup --user --url "{{url}}" "{{repo}}" "{{project}}"

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

# Copy GitHub's branches and tags to the forge as <handle>/fileapi. You rarely need it: the forge pulls GitHub's
# main by itself every minute ([upstream] in .forge/jobs.toml) and deploys it. This catches up other branches and
# tags: fast-forwards only, nothing on the forge is overwritten or deleted. Signs in through git-credential-fileapi (`just git-setup`).
# If one push of a branch fails, it falls back to one commit at a time (a safety net; the server streams big pushes).
forge-mirror url=fileapi_url repo="nandi.uk/fileapi":
    #!/usr/bin/env bash
    set -euo pipefail
    base="{{url}}"; base="${base%/}"; base="${base%/mcp}"
    forge="$base/git/{{repo}}.git"
    git remote get-url forge >/dev/null 2>&1 || git remote add forge "$forge"
    [ "$(git remote get-url forge)" = "$forge" ] || git remote set-url forge "$forge"
    git fetch --quiet origin '+refs/heads/*:refs/remotes/origin/*' --tags --prune
    [ "$(git rev-parse --is-shallow-repository)" = "true" ] && git fetch --quiet --unshallow origin
    # Step each branch toward its GitHub tip, oldest commit first, so no single push is large.
    have=$(git ls-remote forge 'refs/heads/*' | awk '{print $1}')
    for ref in $(git for-each-ref --format='%(refname:strip=3)' refs/remotes/origin | grep -vx HEAD); do
        tip=$(git rev-parse "refs/remotes/origin/$ref")
        remote=$(git ls-remote forge "refs/heads/$ref" | awk '{print $1}')
        [ "$remote" = "$tip" ] && continue
        if [ -n "$remote" ] && ! git merge-base --is-ancestor "$remote" "$tip" 2>/dev/null; then
            echo "  $ref: skipped, the forge has commits GitHub doesn't" >&2; continue
        fi
        if git push --quiet forge "$tip:refs/heads/$ref" 2>/dev/null; then echo "  $ref"; continue; fi
        exclude=$(for h in $have; do git cat-file -e "$h" 2>/dev/null && echo "^$h"; done)
        for c in $(git rev-list --reverse --first-parent "$tip" $exclude); do
            git push --quiet forge "$c:refs/heads/$ref"
        done
        echo "  $ref (step by step)"
    done
    git push --quiet forge --tags
    echo "Forge mirror up to date: $base/forge/#{{repo}}"

# Install the git credential helper (+ fileapi-release, and git-lfs from pixi if there isn't one) to ~/.local/bin and use it for this server
git-setup url=fileapi_url:
    #!/usr/bin/env bash
    set -euo pipefail
    command -v python3 >/dev/null || { echo "git-credential-fileapi needs python3 on PATH." >&2; exit 1; }
    mkdir -p ~/.local/bin
    install -m 755 fileapi/git_credential.py ~/.local/bin/git-credential-fileapi
    ln -sf git-credential-fileapi ~/.local/bin/fileapi-release
    # git runs git-lfs from PATH (outside pixi too), so put pixi's copy (a static binary) where git finds it.
    # A git-lfs from the system package manager wins; one we installed before gets updated.
    pixi_lfs=$({{run}} sh -c 'command -v git-lfs')
    current=$(command -v git-lfs || true)
    if [ -z "$current" ] || [ "$current" = "$HOME/.local/bin/git-lfs" ]; then
        install -m 755 "$pixi_lfs" ~/.local/bin/git-lfs
        echo "Installed $(~/.local/bin/git-lfs version | cut -d' ' -f1) to ~/.local/bin/git-lfs (from pixi)."
    else
        echo "Using git-lfs at $current."
    fi
    case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Add ~/.local/bin to PATH so git can find git-credential-fileapi." >&2;; esac
    ~/.local/bin/git-credential-fileapi setup "{{url}}"
