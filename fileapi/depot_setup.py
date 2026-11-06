"""Connect a forge repo to Depot without copying tokens by hand (`just depot-token <repo>`).

1. Signs in to Depot in the browser (`depot login`; the Depot CLI is installed to ~/.depot/bin if missing).
2. Finds the Depot project (by name or ID; default: the repo's name) or creates it.
3. Creates a project token for it through Depot's API (ProjectService.CreateToken), as the signed-in user.
4. Signs in to fileapi in the browser (the same sign-in and token cache as git-credential-fileapi).
5. Stores DEPOT_TOKEN and DEPOT_PROJECT_ID as the repo's secrets, so `depot = true` in .forge/jobs.toml works.

Standard library only, like git_credential.py, so it runs with any python3.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from . import git_credential
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import git_credential  # type: ignore[no-redef]

DEPOT_API = "https://api.depot.dev"
DEFAULT_URL = os.environ.get("FILEAPI_URL", git_credential.DEFAULT_URL)


def say(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ------------------------------------------------------------------ Depot CLI
def depot_cli() -> str:
    found = shutil.which("depot") or (str(Path.home() / ".depot/bin/depot")
                                      if (Path.home() / ".depot/bin/depot").is_file() else None)
    if found:
        return found
    system = platform.system()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if system not in ("Linux", "Darwin") or not arch:
        fail(f"No Depot CLI for {system}/{platform.machine()}; install it from https://depot.dev/docs/cli/installation")
    dest = Path.home() / ".depot/bin/depot"
    say(f"Installing the Depot CLI to {dest} ...")
    url = f"https://dl.depot.dev/cli/download/{system}/{arch}/latest"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "depot.tar.gz"
        with urllib.request.urlopen(url, timeout=300) as resp, open(archive, "wb") as f:
            shutil.copyfileobj(resp, f)
        with tarfile.open(archive) as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and m.name.endswith("bin/depot")), None)
            if member is None:
                fail("The Depot CLI download has no bin/depot in it")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    dest.chmod(0o755)
    return str(dest)


def depot(cli: str, *args: str, capture: bool = True) -> str:
    proc = subprocess.run([cli, *args], text=True, stdout=subprocess.PIPE if capture else None)
    if proc.returncode != 0:
        fail(f"`depot {' '.join(args)}` failed")
    return proc.stdout or ""


def list_projects(cli: str) -> list[dict]:
    raw = depot(cli, "projects", "list", "--output", "json").strip()
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        fail(f"Couldn't read `depot projects list` output: {raw[:200]}")
    if isinstance(data, dict):
        data = data.get("projects") or data.get("Projects") or []
    out = []
    for p in data:
        if not isinstance(p, dict):
            continue
        low = {k.lower().replace("_", ""): v for k, v in p.items()}
        pid, name = low.get("projectid") or low.get("id"), low.get("name")
        if pid:
            out.append({"id": str(pid), "name": str(name or "")})
    return out


def depot_api(method: str, body: dict, token: str) -> dict:
    req = urllib.request.Request(f"{DEPOT_API}/{method}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("message", "")
        except ValueError:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {detail}".strip()) from None


# ------------------------------------------------------------------ fileapi
def fileapi(base: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail", "")
        except ValueError:
            detail = ""
        fail(f"fileapi {method} {path}: HTTP {e.code} {detail}")
        raise


def resolve_repo(base: str, token: str, repo: str) -> str:
    repos = fileapi(base, token, "GET", "/api/forge/repos").get("repos", [])
    for r in repos:
        if repo in (r.get("repo"), r.get("name")):
            return r["repo"]
    fail(f"No repo {repo!r} on {base}. Yours: {', '.join(r['name'] for r in repos) or 'none'}")
    raise AssertionError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("repo", help="forge repo name (or owner/name)")
    ap.add_argument("project", nargs="?", default="", help="Depot project name or ID (default: the repo's name; "
                                                          "created if missing)")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"fileapi server (default {DEFAULT_URL})")
    args = ap.parse_args(argv)
    base = args.url.rstrip("/").removesuffix("/mcp")

    # fileapi first: a typo in the repo name shouldn't leave a stray Depot project and token behind.
    entry = git_credential.token_for(base)
    if not entry:
        fail(f"Couldn't sign in to {base}")
    full = resolve_repo(base, entry["access_token"], args.repo)
    secrets_info = fileapi(base, entry["access_token"], "GET", f"/api/forge/repos/{full}/secrets")
    if not secrets_info.get("enabled", True):
        fail(f"Secrets are off on {base}: run `just secrets-key` (sets FILEAPI_SECRETS_KEY) and deploy first")

    cli = depot_cli()
    say("Signing in to Depot (a browser window opens if you aren't signed in yet)...")
    depot(cli, "login", "--quiet", capture=False)
    user_token = depot(cli, "login", "token").strip().splitlines()[-1].strip()
    if not user_token:
        fail("`depot login token` printed no token")

    wanted = args.project or full.split("/")[-1]
    projects = list_projects(cli)
    project = next((p for p in projects if wanted in (p["id"], p["name"])), None)
    if project is None:
        say(f"Creating Depot project {wanted!r} ...")
        depot(cli, "projects", "create", wanted, capture=False)
        project = next((p for p in list_projects(cli) if p["name"] == wanted), None)
        if project is None:
            fail(f"Created {wanted!r} but can't find it in `depot projects list`")
    say(f"Depot project: {project['name']} ({project['id']})")

    try:
        made = depot_api("depot.core.v1.ProjectService/CreateToken",
                         {"projectId": project["id"], "description": f"fileapi forge {full}"}, user_token)
        token, what = made.get("secret", ""), f"project token {made.get('tokenId', '')}".strip()
    except RuntimeError as e:
        fail(f"Depot wouldn't create a project token ({e}). Create one by hand: depot.dev > project "
             f"{project['name']} > Settings > Project tokens, then set it as the repo's DEPOT_TOKEN secret.")
        raise
    if not token:
        fail("Depot's CreateToken answer had no secret in it")

    for name, value in (("DEPOT_TOKEN", token), ("DEPOT_PROJECT_ID", project["id"])):
        fileapi(base, entry["access_token"], "PUT", f"/api/forge/repos/{full}/secrets/{name}", {"value": value})
    say(f"Stored DEPOT_TOKEN ({what}) and DEPOT_PROJECT_ID on {full}.")
    say("Add a job to .forge/jobs.toml and push:\n\n  [[job]]\n  name = \"image\"\n  depot = true\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
