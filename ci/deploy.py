"""Deploy this checkout to FastAPI Cloud from CI (ci/Dockerfile.deploy, built on Depot). Standard library only.

1. Finds the app's ID from its name (FASTAPI_CLOUD_APP), unless FASTAPI_CLOUD_APP_ID is set.
2. Checks the app's environment on FastAPI Cloud: no DATABASE_URL means a deploy would start on a wiped disk, so
   it stops (run `just cloud-db` once); no FILEAPI_SECRETS_KEY only warns (forge secrets are off without it).
3. Checks the committed forge UI build (fileapi/static/forge) is there: CI doesn't rebuild it (`just ui` does).
4. Writes the commit to fileapi/BUILD (the server reports it in /health), runs `fastapi deploy`, and waits
   until FILEAPI_URL/health reports that commit.

Needs FASTAPI_CLOUD_TOKEN: a deploy token for the app (`fastapi cloud tokens create`), and the app ID in
FASTAPI_CLOUD_APP_ID (deploy tokens can't look an app up by name).
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("FASTAPI_CLOUD_API", "https://api.fastapicloud.com/api/v1")
TOKEN = os.environ.get("FASTAPI_CLOUD_TOKEN", "")
APP = os.environ.get("FASTAPI_CLOUD_APP", "fileapi")
CLI = os.environ.get("FASTAPI_CLI", "fastapi")
SITE = os.environ.get("FILEAPI_URL", "").rstrip("/")


def say(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"::error::{msg}", flush=True)
    sys.exit(1)


def api(path: str, params: str = ""):
    req = urllib.request.Request(f"{API}{path}{'?' + params if params else ''}",
                                 headers={"Authorization": f"Bearer {TOKEN}", "User-Agent": "fileapi-ci"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def items(data) -> list:
    return data.get("data", []) if isinstance(data, dict) else data or []


def app_id() -> str:
    if os.environ.get("FASTAPI_CLOUD_APP_ID"):
        return os.environ["FASTAPI_CLOUD_APP_ID"]
    try:
        for team in items(api("/teams/", "limit=100")):
            for app in items(api("/apps/", f"team_id={team['id']}&limit=100")):
                if APP in (app.get("name"), app.get("slug")):
                    say(f"FastAPI Cloud app {APP!r}: {app['id']}")
                    return app["id"]
    except urllib.error.HTTPError as e:
        fail(f"Couldn't list FastAPI Cloud apps to find {APP!r} (HTTP {e.code}). If the deploy token can't list "
             "apps, add the app's ID as the FASTAPI_CLOUD_APP_ID secret (app_id in .fastapicloud/cloud.json).")
    fail(f"No FastAPI Cloud app named {APP!r} is visible to this token.")


def check_env(app: str) -> None:
    try:
        names = {v.get("name") for v in items(api(f"/apps/{app}/environment-variables/"))}
    except urllib.error.HTTPError as e:
        say(f"::warning::Couldn't read the app's env vars (HTTP {e.code}), so DATABASE_URL wasn't checked. "
            "That's still safe: the app refuses to start without it, and FastAPI Cloud keeps the old version.")
        return
    if "DATABASE_URL" not in names:
        fail("DATABASE_URL isn't set on FastAPI Cloud, so this deploy would start on a wiped disk. "
             "Run `just cloud-db` once, then push again.")
    say("DATABASE_URL is set on FastAPI Cloud.")
    if "FILEAPI_SECRETS_KEY" not in names:
        say("::warning::FILEAPI_SECRETS_KEY isn't set on FastAPI Cloud: forge secrets (and so this CI) stop "
            "working. Run `just secrets-key`.")
    else:
        say("FILEAPI_SECRETS_KEY is set on FastAPI Cloud.")


def main() -> None:
    if not TOKEN:
        fail("FASTAPI_CLOUD_TOKEN is empty: save a FastAPI Cloud deploy token as the forge secret FASTAPI_TOKEN.")
    if os.environ.get("FORGE_RUN"):
        say(f"Started by {os.environ['FORGE_RUN']}")
    sha = os.environ.get("DEPLOY_SHA") or os.environ.get("FORGE_SHA") or subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    say(f"Deploying {sha} to FastAPI Cloud app {APP!r}")
    app = app_id()
    check_env(app)
    ui = Path("fileapi/static/forge")
    missing = [f for f in ("index.html", "forgeui.js", "forgeui.wasm.gz", "qtloader.js") if not (ui / f).is_file()]
    if missing:
        fail(f"The forge UI build is missing {', '.join(missing)} in {ui}: run `just ui` and commit the result.")
    Path("fileapi/BUILD").write_text(sha + "\n")
    # Blob storage (fileapi/blobs.py): the B2 key ID and bucket from .forge/jobs.toml, baked into the build. The
    # application key itself is the B2_TOKEN user secret, which the server reads from the forge.
    b2 = {k: os.environ[k] for k in ("B2_KEY_ID", "B2_BUCKET") if os.environ.get(k)}
    if b2:
        Path("fileapi/B2_CONFIG").write_text(json.dumps(b2) + "\n")
        say(f"Blob storage: B2 bucket {b2.get('B2_BUCKET', 'fileapi')!r}, key ID {b2.get('B2_KEY_ID', '(not set)')}")
    run_id = os.environ.get("FORGE_RUN", "").rstrip("/").rsplit("/", 1)[-1]
    if len(run_id) == 16:  # the forge run: the new server marks it done if its job dies with the old replica
        Path("fileapi/BUILD_RUN").write_text(run_id + "\n")
    import shutil

    cli = [CLI] if shutil.which(CLI) else [sys.executable, "-m", "fastapi"]
    code = subprocess.run([*cli, "deploy", "--app-id", app]).returncode
    if code != 0:
        fail(f"fastapi deploy failed (exit {code}); FastAPI Cloud keeps serving the previous version.")
    if not SITE:
        return
    say(f"Waiting for {SITE}/health to report {sha[:12]}...")
    deadline = time.time() + 300  # 5 minutes
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{SITE}/health", timeout=15) as resp:
                health = json.loads(resp.read())
            if health.get("build") == sha:
                say(f"Live: {SITE} runs {sha[:12]} (storage: {health.get('storage')}, blobs: "
                    f"{health.get('blobs', 'postgres')}, copy to B2: {health.get('blob_copy', 'n/a')}).")
                return
        except (OSError, ValueError):
            pass
        time.sleep(5)
    # fastapi deploy already waited for FastAPI Cloud's own verification, so this only warns: during a rolling
    # deploy (or from inside the old replica) /health can keep answering from the previous version for a while.
    say(f"::warning::FastAPI Cloud says the deploy succeeded, but {SITE}/health didn't report {sha[:12]} within "
        "5 minutes (it may still be answering from the previous replica).")


if __name__ == "__main__":
    main()
