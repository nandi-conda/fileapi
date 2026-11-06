"""Print the forge CI runs for a pushed commit, with links, and follow them to the end (`just deploy`).

    python -m fileapi.ci_watch --url https://fileapi.fastapicloud.dev nandi.uk/fileapi <sha>

Signs in like git does (git-credential-fileapi's saved token, or the browser). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

try:
    from . import git_credential
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import git_credential  # type: ignore[no-redef]

DONE = {"success", "failed", "error", "timeout", "cancelled", "skipped", "lost"}


def get(base: str, token: str, path: str) -> dict:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--no-follow", action="store_true", help="print the links and stop")
    ap.add_argument("repo")
    ap.add_argument("sha")
    args = ap.parse_args(argv)
    base = args.url.rstrip("/").removesuffix("/mcp")
    entry = git_credential.token_for(base)
    if not entry:
        print(f"Couldn't sign in to {base}", file=sys.stderr)
        return 1
    token = entry["access_token"]
    shown: dict[str, tuple] = {}
    deadline = time.time() + 30  # jobs start right after the push; give them a moment
    while True:
        runs = [r for r in get(base, token, f"/api/forge/repos/{args.repo}/runs?limit=30")["runs"]
                if r.get("sha") == args.sha and r.get("trigger") == "push"]
        for r in reversed(runs):
            state = (r["status"], (r.get("depot") or {}).get("url"))
            if shown.get(r["id"]) == state:
                continue
            if r["id"] not in shown:
                print(f"{r['job']}: {base}/forge/#{args.repo}/-/deploys/{r['id']}", flush=True)
            if state[1] and (r["id"] not in shown or shown[r["id"]][1] != state[1]):
                print(f"  {r['job']} on Depot: {state[1]}", flush=True)
            print(f"  {r['job']}: {r['status']}" + (f" ({r['error']})" if r.get("error") else ""), flush=True)
            shown[r["id"]] = state
        if not runs:
            if time.time() > deadline:
                print(f"No CI runs started for {args.sha[:12]} (no .forge/jobs.toml job for this branch?).")
                return 0
        elif args.no_follow or all(r["status"] in DONE for r in runs):
            return 0 if all(r["status"] in ("success", "skipped") for r in runs) or args.no_follow else 1
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
