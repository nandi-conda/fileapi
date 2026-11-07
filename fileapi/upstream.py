"""Pull mirrors: a forge repo follows branches of another git host and runs its jobs when they move.

A repo opts in from its default branch's .forge/jobs.toml:

    [upstream]
    url = "https://github.com/owner/repo.git"   # public, or ${SECRET} in the URL for a private one
    branches = ["main"]                          # default: the repo's default branch
    interval = 60                                # seconds between checks (30 to 3600)

Every interval the server asks the upstream for its refs (one small request). When a listed branch moved, it
fetches just the new objects and moves the forge branch, fast-forward only and compare-and-swap, so a
replica racing it or a push to the forge always wins cleanly. The move is an ordinary push to the forge's
listeners: deploy jobs run exactly as if someone had pushed. A branch the forge has moved past (or that
diverged) is left alone and logged. POST /api/forge/repos/<o>/<r>/upstream/sync checks right away (a
webhook can call it; it only ever pulls from the configured URL).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import tomllib

from dulwich.objects import Blob, Commit

from .deploy import JOBS_FILE, _branch_matches, _VAR_RE
from .storage import StorageError

log = logging.getLogger("fileapi.upstream")

DEFAULT_INTERVAL = 60
MIN_INTERVAL = 30
MAX_INTERVAL = 3600
# How often the background loop re-reads which repos have an [upstream] from the database. Between those
# reads it only talks to the upstream hosts, so a quiet forge leaves Postgres alone and a serverless database
# (Neon) can suspend instead of being woken every few seconds. Pushes, renames, deletes and poke() refresh it
# early.
REFRESH = int(os.environ.get("FILEAPI_UPSTREAM_REFRESH", 3600))


def parse_upstream(text: str) -> dict | None:
    """The [upstream] table of a jobs.toml, validated; None when there isn't one."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StorageError(f"{JOBS_FILE}: {e}") from None
    u = doc.get("upstream")
    if u is None:
        return None
    if not isinstance(u, dict):
        raise StorageError(f"{JOBS_FILE}: upstream must be a table ([upstream])")
    unknown = set(u) - {"url", "branches", "interval"}
    if unknown:
        raise StorageError(f"{JOBS_FILE}: unknown upstream setting(s): {', '.join(sorted(unknown))}")
    url = u.get("url")
    if not isinstance(url, str) or not url:
        raise StorageError(f"{JOBS_FILE}: upstream.url is required")
    branches = u.get("branches", [])
    if isinstance(branches, str):
        branches = [branches]
    if not isinstance(branches, list) or not all(isinstance(b, str) and b for b in branches):
        raise StorageError(f"{JOBS_FILE}: upstream.branches must be a list of branch names or globs")
    interval = u.get("interval", DEFAULT_INTERVAL)
    if not isinstance(interval, int) or not MIN_INTERVAL <= interval <= MAX_INTERVAL:
        raise StorageError(f"{JOBS_FILE}: upstream.interval is whole seconds, {MIN_INTERVAL} to {MAX_INTERVAL}")
    return {"url": url, "branches": branches, "interval": interval}


class Upstream:
    """Keeps repos with an [upstream] in step with it. One per server; every replica may run one (the ref
    updates are compare-and-swap, so two replicas pulling the same commit move the branch once)."""

    def __init__(self, forge, secrets=None, *, tick: float = 15.0):
        self.forge = forge
        self.secrets = secrets  # deploy.RepoSecrets: ${NAME} in upstream.url
        self.tick = tick
        self._configs: dict[str, tuple[str, dict | None]] = {}  # repo -> (default head sha, config)
        self._due: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._watch: list[tuple[str, dict]] | None = None  # (repo, config) for repos with an [upstream]
        self._watch_at = 0.0
        self._seen: dict[str, dict] = {}  # repo -> upstream refs last pulled in full (no DB work while unchanged)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- config --------------------------------------------------------------------
    def config(self, repo: str) -> dict | None:
        r = self.forge.open(repo)
        try:
            default = self.forge.branches(repo).get("default") or "main"
            try:
                head = r.refs[f"refs/heads/{default}".encode()]
            except KeyError:
                return None
            cached = self._configs.get(repo)
            if cached and cached[0] == head:
                return cached[1]
            cfg = None
            try:
                commit = r[head]
                tree = r[commit.tree] if isinstance(commit, Commit) else None
                _, d_sha = tree[b".forge"]
                _, f_sha = r[d_sha][b"jobs.toml"]
                blob = r[f_sha]
                if isinstance(blob, Blob):
                    cfg = parse_upstream(blob.data.decode("utf-8", "replace"))
            except (KeyError, TypeError):
                cfg = None
            except StorageError as e:
                log.warning("%s: %s", repo, e)
                cfg = None
            if cfg is not None:
                cfg = {**cfg, "default": default}
            self._configs[repo] = (head, cfg)
            return cfg
        finally:
            r.close()

    def _url(self, repo: str, cfg: dict) -> str:
        url = cfg["url"]
        names = _VAR_RE.findall(url)
        if not names:
            return url
        try:
            env = self.secrets.values(repo) if self.secrets is not None else {}
        except StorageError:
            env = {}
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise StorageError(f"upstream.url needs the secret(s) {', '.join(missing)}")
        return _VAR_RE.sub(lambda m: env[m.group(1)], url)

    # -- syncing -----------------------------------------------------------------------
    def sync(self, repo: str) -> list[dict]:
        """Pull the upstream's listed branches into the forge once. Returns what happened to each."""
        repo = self.forge.canonical(repo)
        cfg = self.config(repo)
        if cfg is None:
            raise StorageError(f"{repo} has no [upstream] in {JOBS_FILE} on its default branch")
        return self._sync(repo, cfg)

    def _sync(self, repo: str, cfg: dict, *, quiet: bool = False) -> list[dict]:
        """quiet: skip the database when the upstream's refs are the ones last pulled (the background loop)."""
        from dulwich.client import get_transport_and_path
        from dulwich.graph import can_fast_forward

        lock = self._locks.setdefault(repo, threading.Lock())
        if not lock.acquire(blocking=False):
            return [{"status": "busy"}]
        try:
            client, path = get_transport_and_path(self._url(repo, cfg))
            remote = client.get_refs(path)
            remote = getattr(remote, "refs", remote)  # dulwich: LsRemoteResult or a plain dict
            patterns = cfg["branches"] or [cfg["default"]]
            wanted = {ref: sha for ref, sha in remote.items()
                      if ref.startswith(b"refs/heads/") and not ref.endswith(b"^{}")
                      and _branch_matches(patterns, ref[len(b"refs/heads/"):].decode(), cfg["default"])}
            if quiet and self._seen.get(repo) == wanted:
                return [{"ref": ref.decode(), "status": "up to date", "sha": sha.decode()}
                        for ref, sha in sorted(wanted.items())]
            r = self.forge.open(repo)
            results, moved = [], []
            try:
                local = {ref: r.refs.as_dict().get(ref) for ref in wanted}
                need = [sha for ref, sha in wanted.items() if local[ref] != sha and sha not in r.object_store]
                if need:
                    client.fetch(path, r, determine_wants=lambda refs, depth=None: list(dict.fromkeys(need)))
                for ref, new in sorted(wanted.items()):
                    name, old = ref.decode(), local[ref]
                    if old == new:
                        results.append({"ref": name, "status": "up to date", "sha": new.decode()})
                        continue
                    if new not in r.object_store:
                        results.append({"ref": name, "status": "error", "reason": "fetch didn't bring the commit"})
                        continue
                    if old is not None:
                        try:
                            ff = can_fast_forward(r, old, new)
                            behind = not ff and can_fast_forward(r, new, old)
                        except KeyError:
                            ff = behind = False
                        if not ff:
                            reason = "the forge is ahead of the upstream" if behind else "diverged from the upstream"
                            results.append({"ref": name, "status": "kept", "reason": reason})
                            continue
                    ok = r.refs.set_if_equals(ref, old, new) if old is not None else r.refs.add_if_new(ref, new)
                    if not ok:
                        results.append({"ref": name, "status": "kept", "reason": "moved during the pull"})
                        continue
                    results.append({"ref": name, "status": "created" if old is None else "updated",
                                    "old": old.decode() if old else None, "sha": new.decode()})
                    moved.append((name, old.decode() if old else None, new.decode()))
            finally:
                r.close()
            for x in results:
                if x["status"] in ("created", "updated"):
                    log.info("%s: %s %s -> %s from upstream", repo, x["ref"], (x["old"] or "-")[:12], x["sha"][:12])
                elif x["status"] in ("kept", "error"):
                    log.info("%s: %s not pulled: %s", repo, x["ref"], x["reason"])
            if moved:
                self.forge._emit("on_push", repo, moved)
            if all(x["status"] != "error" for x in results):
                self._seen[repo] = wanted
            else:
                self._seen.pop(repo, None)
            return results
        finally:
            lock.release()

    def poke(self, repo: str | None = None) -> None:
        """Check (one repo, or all) on the next tick instead of waiting out the interval."""
        if repo:
            repo = self.forge.canonical(repo)
            self._due[repo] = 0
            self._seen.pop(repo, None)
        else:
            self._due.clear()
            self._seen.clear()
        self._watch = None  # re-read which repos follow an upstream
        self._wake.set()

    # Forge listener: a push can add, change or drop a repo's [upstream].
    def on_push(self, repo: str, updates) -> None:
        self._watch = None

    def on_rename(self, old: str, new: str) -> None:
        self._watch = None

    def on_delete(self, repo: str) -> None:
        self._watch = None

    def _watched(self, now: float) -> list[tuple[str, dict]]:
        if self._watch is None or now - self._watch_at >= REFRESH:
            watch = []
            for info in self.forge.list():
                repo = info.full_name
                try:
                    cfg = self.config(repo)
                except Exception:  # noqa: BLE001
                    log.exception("%s: reading [upstream] failed", repo)
                    continue
                if cfg is not None:
                    watch.append((repo, cfg))
            self._watch, self._watch_at = watch, now
        return self._watch

    def run_once(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        due = [(repo, cfg) for repo, cfg in self._watched(now) if self._due.get(repo, 0) <= now]
        for repo, cfg in due:
            self._due[repo] = now + cfg["interval"]
            try:
                self._sync(repo, cfg, quiet=True)
            except Exception as e:  # noqa: BLE001 - a host being down must not stop the others
                log.warning("%s: upstream pull failed: %s", repo, e)

    # -- background thread ----------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="forge-upstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                self.run_once()
                failures = 0
            except Exception as e:  # noqa: BLE001
                # Usually the database being unreachable: log it briefly and back off (up to ~15 min) instead of
                # a full traceback every tick.
                failures += 1
                log.warning("upstream pass failed (%d in a row): %s: %s", failures, type(e).__name__, e)
                if self._wake.wait(min(self.tick * 2 ** failures, 900)):
                    self._wake.clear()
                continue
            self._wake.wait(self.tick)
            self._wake.clear()


def enabled_from_env() -> bool:
    return os.environ.get("FILEAPI_UPSTREAM", "on").strip().lower() not in ("0", "off", "false", "no")
