"""Repo secrets and deploy jobs for the forge.

Secrets
    Each repo has named secrets (env-var style names, e.g. FLY_API_TOKEN). They are encrypted with AES-256-GCM
    under FILEAPI_SECRETS_KEY (32 random bytes, base64) before they reach the KV store, and they are
    write-only: the API and MCP tools list names and dates, never values. Values only ever leave the server
    as environment variables of a deploy job (and they are masked in its output).

Deploy jobs
    A repo opts in by committing `.forge/jobs.toml`. After every push (git, push_bundle or commit_files) the
    server reads that file from the pushed commit and runs the jobs whose `branches` match, in a background
    thread on the replica that took the push. Two kinds of job:

        [[job]]                       # a shell script, run with sh -e in a checkout of the pushed commit
        name = "deploy"
        branches = ["main"]           # default: the repo's default branch; ["*"] = every branch
        run = "python deploy.py"      # secrets and FORGE_* variables are in the environment

        [[job]]                       # an HTTP request (deploy hooks): no shell or curl needed on the host
        name = "render"
        post = "${RENDER_DEPLOY_HOOK}"
        headers = { Authorization = "Bearer ${SOME_TOKEN}" }
        body = '{"sha": "${FORGE_SHA}"}'

        [[job]]                       # a container image build on Depot (depot.dev): remote BuildKit, no Docker here
        name = "image"
        [job.depot]
        project = "abc123xyz"         # Depot project ID; default: the DEPOT_PROJECT_ID secret (not masked)
        platforms = ["linux/amd64", "linux/arm64"]
        tags = ["ghcr.io/me/app:${FORGE_SHA}"]
        push = true                   # push the tags; default: save to the Depot registry instead
        login = { server = "ghcr.io", username = "me", password = "${GHCR_TOKEN}" }

    A depot job needs the DEPOT_TOKEN secret (a Depot project or organization token). The server downloads the
    Depot CLI the first time it's needed; shell jobs whose script mentions `depot` get it on PATH too.

    `${NAME}` in post/headers/body (and the depot table) is replaced by that secret or FORGE_* value. Each run (status, output
    with secrets masked, timing) is kept in the KV store, the last RUNS_KEPT per repo.

Only the admin can push (or set secrets), so a job's code is as trusted as the server's own. Jobs still get
a scrubbed environment: none of the server's own variables (DATABASE_URL, FILEAPI_SECRETS_KEY...) except PATH.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import secrets as pysecrets
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dulwich.object_store import iter_tree_contents
from dulwich.objects import Blob, Commit

from .storage import NotFound, StorageError

KEY_ENV = "FILEAPI_SECRETS_KEY"
SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
MAX_SECRET_BYTES = 64 * 1024
MAX_SECRETS = 100
JOBS_FILE = ".forge/jobs.toml"
RUNS_KEPT = 30
MAX_OUTPUT = 64 * 1024
DEFAULT_TIMEOUT = 600
MAX_TIMEOUT = 3600
DEPOT_CLI_VERSION = "2.103.9"  # FILEAPI_DEPOT_CLI_VERSION overrides ("latest" works too)
UNMASKED = {"DEPOT_PROJECT_ID"}  # secrets for convenience, not secret: masking them would break build links
DEPOT_URL_RE = re.compile(r"https://depot\.dev/orgs/[\w-]+/projects/[\w-]+/builds/[\w-]+")


class SecretsUnavailable(StorageError):
    """FILEAPI_SECRETS_KEY isn't set (or is malformed), so secrets can't be stored or read."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_key(prefix: str, repo: str) -> str:
    # KV keys are file names on disk, so the "<owner>/<name>" goes in as hex.
    return prefix + repo.encode().hex()


def parse_key(raw: str | None) -> bytes | None:
    """32 bytes from base64 (standard or URL-safe, padding optional) or 64 hex characters."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"[0-9a-fA-F]{64}", raw):
        return bytes.fromhex(raw)
    try:
        key = base64.b64decode(raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4), validate=True)
    except (binascii.Error, ValueError):
        key = b""
    if len(key) != 32:
        raise RuntimeError(f"{KEY_ENV} must be 32 random bytes as base64 (e.g. `openssl rand -base64 32`)")
    return key


def check_secret_name(name: str) -> str:
    name = (name or "").strip()
    if not SECRET_NAME_RE.match(name):
        raise StorageError("Secret names are letters, digits and underscores, not starting with a digit "
                           "(they become environment variables), e.g. FLY_API_TOKEN")
    if name.upper().startswith("FORGE_"):
        raise StorageError("Names starting with FORGE_ are reserved for the job's own variables")
    return name


class RepoSecrets:
    """Encrypted, write-only secrets per repo, one KV record per repo."""

    def __init__(self, kv, key: bytes | None):
        self.kv = kv
        self._aes = AESGCM(key) if key else None
        self._lock = threading.Lock()  # read-modify-write of a repo's record within this process

    @property
    def enabled(self) -> bool:
        return self._aes is not None

    def _need(self) -> AESGCM:
        if self._aes is None:
            raise SecretsUnavailable(f"Secrets are turned off: set {KEY_ENV} on the server (32 random bytes, base64)")
        return self._aes

    def _record(self, repo: str) -> dict:
        return (self.kv.get(_repo_key("forge_secrets_", repo)) or {}).get("secrets", {})

    def _save(self, repo: str, secrets: dict) -> None:
        if secrets:
            self.kv.put(_repo_key("forge_secrets_", repo), {"secrets": secrets})
        else:
            self.kv.pop(_repo_key("forge_secrets_", repo))

    def list(self, repo: str) -> list[dict]:
        return [{"name": n, "updated": s.get("updated", "")} for n, s in sorted(self._record(repo).items())]

    def set(self, repo: str, name: str, value: str) -> dict:
        aes = self._need()
        name = check_secret_name(name)
        data = value.encode()
        if len(data) > MAX_SECRET_BYTES:
            raise StorageError(f"Secret values are limited to {MAX_SECRET_BYTES // 1024} KB")
        nonce = pysecrets.token_bytes(12)
        # The name is authenticated data, so a ciphertext can't be passed off as another secret.
        box = base64.b64encode(nonce + aes.encrypt(nonce, data, name.encode())).decode()
        with self._lock:
            current = self._record(repo)
            if name not in current and len(current) >= MAX_SECRETS:
                raise StorageError(f"A repo can have at most {MAX_SECRETS} secrets")
            created = name not in current
            current[name] = {"v": box, "updated": _now()}
            self._save(repo, current)
        return {"name": name, "updated": current[name]["updated"], "created": created}

    def delete(self, repo: str, name: str) -> None:
        with self._lock:
            current = self._record(repo)
            if current.pop(name, None) is None:
                raise NotFound(f"No such secret: {name}")
            self._save(repo, current)

    def values(self, repo: str) -> dict[str, str]:
        """Decrypted values, for a job's environment. Unreadable ones (key changed) are skipped."""
        if self._aes is None:
            return {}
        out = {}
        for name, s in self._record(repo).items():
            try:
                raw = base64.b64decode(s["v"])
                out[name] = self._aes.decrypt(raw[:12], raw[12:], name.encode()).decode()
            except (InvalidTag, KeyError, ValueError, binascii.Error):
                continue
        return out

    def move(self, old: str, new: str) -> None:
        with self._lock:
            rec = self.kv.pop(_repo_key("forge_secrets_", old))
            if rec:
                self.kv.put(_repo_key("forge_secrets_", new), rec)

    def drop(self, repo: str) -> None:
        self.kv.pop(_repo_key("forge_secrets_", repo))


# --------------------------------------------------------------------------- jobs


def _as_list(value, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return value
    raise StorageError(f"{what} must be a string or a list of strings")


def parse_jobs(text: str) -> list[dict]:
    """Validate .forge/jobs.toml into a list of {name, branches, run|post, headers, body, timeout}."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StorageError(f"{JOBS_FILE}: {e}") from None
    raw = doc.get("job", [])
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        raise StorageError(f"{JOBS_FILE}: use [[job]] tables")
    jobs, names = [], set()
    for i, j in enumerate(raw, 1):
        if not isinstance(j, dict):
            raise StorageError(f"{JOBS_FILE}: job {i} must be a table")
        name = str(j.get("name") or f"job{i}")
        if name in names:
            raise StorageError(f"{JOBS_FILE}: two jobs are named {name!r}")
        names.add(name)
        run, post, depot = j.get("run"), j.get("post"), j.get("depot")
        if sum(x is not None for x in (run, post, depot)) != 1:
            raise StorageError(f"{JOBS_FILE}: job {name!r} needs exactly one of run (a shell script), post (a URL) "
                               "or depot (a container build)")
        if run is not None and not isinstance(run, str):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: run must be a string")
        if post is not None and not isinstance(post, str):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: post must be a URL string")
        headers = j.get("headers") or {}
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: headers must be a table of strings")
        body = j.get("body")
        if body is not None and not isinstance(body, str):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: body must be a string")
        timeout = j.get("timeout", DEFAULT_TIMEOUT)
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT:
            raise StorageError(f"{JOBS_FILE}: job {name!r}: timeout is whole seconds, 1 to {MAX_TIMEOUT}")
        if depot is not None:
            depot = _parse_depot(name, depot)
        jobs.append({"name": name, "branches": _as_list(j.get("branches"), "branches"), "run": run, "post": post,
                     "depot": depot, "headers": headers, "body": body, "timeout": timeout})
    return jobs


_DEPOT_KEYS = {"project", "file", "context", "platforms", "tags", "push", "save", "build_args", "target", "login",
               "args"}


def _parse_depot(name: str, d) -> dict:
    """The [job.depot] table: what to build. `depot = true` builds ./Dockerfile with every default."""
    if d is True:
        d = {}
    if not isinstance(d, dict):
        raise StorageError(f"{JOBS_FILE}: job {name!r}: depot must be a table, e.g. depot = {{ tags = [...] }}")
    unknown = set(d) - _DEPOT_KEYS
    if unknown:
        raise StorageError(f"{JOBS_FILE}: job {name!r}: unknown depot setting(s): {', '.join(sorted(unknown))} "
                           f"(known: {', '.join(sorted(_DEPOT_KEYS))})")

    def text(key, default=None):
        v = d.get(key, default)
        if v is not None and not isinstance(v, str):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: depot.{key} must be a string")
        return v

    def flag(key):
        v = d.get(key)
        if v is not None and not isinstance(v, bool):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: depot.{key} must be true or false")
        return v

    build_args = d.get("build_args") or {}
    if not isinstance(build_args, dict) or not all(isinstance(v, str) for v in build_args.values()):
        raise StorageError(f"{JOBS_FILE}: job {name!r}: depot.build_args must be a table of strings")
    login = d.get("login")
    if login is not None:
        if (not isinstance(login, dict) or set(login) - {"server", "username", "password"}
                or not all(isinstance(login.get(k), str) and login.get(k) for k in ("server", "username", "password"))):
            raise StorageError(f"{JOBS_FILE}: job {name!r}: depot.login needs server, username and password strings")
    out = {"project": text("project"), "file": text("file"), "context": text("context", "."),
           "platforms": _as_list(d.get("platforms"), f"depot.platforms of job {name!r}"),
           "tags": _as_list(d.get("tags"), f"depot.tags of job {name!r}"),
           "push": bool(flag("push")), "save": flag("save"), "build_args": build_args, "target": text("target"),
           "login": login, "args": _as_list(d.get("args"), f"depot.args of job {name!r}")}
    if out["push"] and not out["tags"]:
        raise StorageError(f"{JOBS_FILE}: job {name!r}: depot.push needs tags to push to")
    for p in (out["context"], out["file"] or ""):
        if p.startswith("/") or ".." in Path(p).parts:
            raise StorageError(f"{JOBS_FILE}: job {name!r}: depot paths are relative to the repo root")
    return out


def _branch_matches(patterns: list[str], branch: str, default: str) -> bool:
    import fnmatch

    return any(fnmatch.fnmatchcase(branch, p) for p in (patterns or [default]))


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(text: str, env: dict[str, str]) -> str:
    def sub(m):
        if m.group(1) not in env:
            raise StorageError(f"${{{m.group(1)}}} is not a secret of this repo (or a FORGE_ variable)")
        return env[m.group(1)]

    return _VAR_RE.sub(sub, text)


def _kind(job: dict) -> str:
    return "run" if job["run"] is not None else "post" if job["post"] is not None else "depot"


class _Output:
    """A job's combined output: capped, with secret values masked."""

    def __init__(self, secrets: dict[str, str]):
        self.parts: list[str] = []
        self.size = 0
        self.truncated = False
        # Longest first, so a secret containing another is masked whole. Very short values would mask noise.
        self.masks = sorted({v for v in secrets.values() if len(v) >= 4}, key=len, reverse=True)

    def mask(self, text: str) -> str:
        for v in self.masks:
            text = text.replace(v, "***")
        return text

    def write(self, text: str) -> None:
        if self.truncated:
            return
        if self.size + len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT - self.size]
            self.truncated = True
        self.parts.append(text)
        self.size += len(text)

    def value(self) -> str:
        out = self.mask("".join(self.parts))
        return out + ("\n[output truncated]\n" if self.truncated else "")


class Jobs:
    """Runs a repo's .forge/jobs.toml after pushes and keeps a short history of runs."""

    def __init__(self, forge, kv, secrets: RepoSecrets, *, enabled: bool = True, workers: int = 2,
                 work_dir: str | None = None, tools_dir: str | None = None):
        self.forge = forge
        self.kv = kv
        self.secrets = secrets
        self.enabled = enabled
        self.work_dir = work_dir
        # Downloaded tools (the Depot CLI). Local disk is fine: it's a cache, refetched on a fresh replica.
        self.tools_dir = tools_dir or os.path.join(tempfile.gettempdir(), "fileapi-tools")
        self._tools_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="forge-job")
        self._index_lock = threading.Lock()
        self._futures: set = set()
        self.http = httpx.Client(timeout=60, follow_redirects=False)
        forge.listeners.append(self)

    # -- run records ------------------------------------------------------------
    def _save_run(self, run: dict) -> None:
        self.kv.put(f"forge_run_{run['id']}", run)

    def get_run(self, repo: str, run_id: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{16}", run_id or ""):
            raise NotFound(f"No such run: {run_id}")
        run = self.kv.get(f"forge_run_{run_id}")
        if not run or run.get("repo") != repo:
            raise NotFound(f"No such run: {run_id}")
        return self._fresh(run)

    @staticmethod
    def _fresh(run: dict) -> dict:
        # A run whose process died (deploy, scale-down) would say "running" forever.
        if run.get("status") in ("queued", "running"):
            started = run.get("started_ts") or run.get("queued_ts") or 0
            if time.time() - started > run.get("timeout", DEFAULT_TIMEOUT) + 120:
                run = {**run, "status": "lost", "error": "The server stopped before the job finished"}
        return run

    def list_runs(self, repo: str, limit: int = RUNS_KEPT) -> list[dict]:
        ids = (self.kv.get(_repo_key("forge_runs_", repo)) or {}).get("ids", [])
        out = []
        for run_id in ids[:limit]:
            run = self.kv.get(f"forge_run_{run_id}")
            if run:
                run = self._fresh(run)
                out.append({k: v for k, v in run.items() if k != "output"})
        return out

    def _add_to_index(self, repo: str, run_id: str) -> None:
        with self._index_lock:
            key = _repo_key("forge_runs_", repo)
            ids = [run_id] + (self.kv.get(key) or {}).get("ids", [])
            for old in ids[RUNS_KEPT:]:
                self.kv.pop(f"forge_run_{old}")
            self.kv.put(key, {"ids": ids[:RUNS_KEPT]})

    # -- forge events -------------------------------------------------------------
    def on_push(self, repo: str, updates: list[tuple[str, str | None, str | None]]) -> None:
        """updates: (ref, old sha, new sha) for each ref the push changed."""
        if not self.enabled:
            return
        for ref, old, new in updates:
            if not new or not ref.startswith("refs/heads/"):
                continue  # deleted branches and tags don't deploy
            try:
                self.start(repo, ref[len("refs/heads/"):], new, before=old, trigger="push")
            except StorageError:
                continue  # no jobs file, or nothing matches this branch

    def on_rename(self, old: str, new: str) -> None:
        self.secrets.move(old, new)
        with self._index_lock:
            rec = self.kv.pop(_repo_key("forge_runs_", old))
            if rec:
                self.kv.put(_repo_key("forge_runs_", new), rec)
                for run_id in rec.get("ids", []):
                    run = self.kv.get(f"forge_run_{run_id}")
                    if run:
                        self._save_run({**run, "repo": new})

    def on_delete(self, repo: str) -> None:
        self.secrets.drop(repo)
        with self._index_lock:
            rec = self.kv.pop(_repo_key("forge_runs_", repo))
            for run_id in (rec or {}).get("ids", []):
                self.kv.pop(f"forge_run_{run_id}")

    # -- starting -------------------------------------------------------------------
    def jobs_at(self, repo: str, sha: str) -> list[dict]:
        r = self.forge.open(repo)
        try:
            commit = r[sha.encode()]
            if not isinstance(commit, Commit):
                raise StorageError("Not a commit")
            tree = r[commit.tree]
            try:
                d_mode, d_sha = tree[b".forge"]
                f_mode, f_sha = r[d_sha][b"jobs.toml"]
            except KeyError:
                raise NotFound(f"No {JOBS_FILE} in this commit") from None
            blob = r[f_sha]
            if not isinstance(blob, Blob):
                raise NotFound(f"No {JOBS_FILE} in this commit")
            return parse_jobs(blob.data.decode("utf-8", "replace"))
        finally:
            r.close()

    def start(self, repo: str, branch: str, sha: str, *, before: str | None = None, trigger: str = "manual",
              job: str | None = None) -> list[dict]:
        """Queue the jobs for a commit on a branch. A named job runs whatever its branches say."""
        if not self.enabled:
            raise StorageError("Deploy jobs are turned off on this server (FILEAPI_JOBS=off)")
        jobs = self.jobs_at(repo, sha)
        default = self.forge.branches(repo).get("default") or "main"
        if job:
            jobs = [j for j in jobs if j["name"] == job]
            if not jobs:
                raise NotFound(f"No job named {job!r} in {JOBS_FILE}")
        else:
            jobs = [j for j in jobs if _branch_matches(j["branches"], branch, default)]
            if not jobs:
                raise NotFound(f"No job in {JOBS_FILE} runs for branch {branch}")
        started = []
        for j in jobs:
            run = {"id": pysecrets.token_hex(8), "repo": repo, "job": j["name"], "kind": _kind(j),
                   "branch": branch, "sha": sha, "before": before, "trigger": trigger, "status": "queued",
                   "queued": _now(), "queued_ts": time.time(), "timeout": j["timeout"]}
            self._save_run(run)
            self._add_to_index(repo, run["id"])
            fut = self._pool.submit(self._execute, run, j)
            self._futures.add(fut)
            fut.add_done_callback(self._futures.discard)
            started.append({k: v for k, v in run.items()})
        return started

    # -- running ----------------------------------------------------------------------
    def _execute(self, run: dict, job: dict) -> None:
        secrets = self.secrets.values(run["repo"])
        out = _Output({k: v for k, v in secrets.items() if k not in UNMASKED})
        run.update(status="running", started=_now(), started_ts=time.time())
        self._save_run(run)
        env = {**secrets, "FORGE_REPO": run["repo"], "FORGE_BRANCH": run["branch"],
               "FORGE_REF": f"refs/heads/{run['branch']}", "FORGE_SHA": run["sha"], "FORGE_BEFORE": run["before"] or "",
               "FORGE_JOB": run["job"], "FORGE_RUN_ID": run["id"], "FORGE_TRIGGER": run["trigger"]}
        status, error, code = "failed", None, None
        try:
            if job["run"] is not None:
                code = self._run_shell(run, job, env, out)
            elif job["depot"] is not None:
                code = self._run_depot(run, job, env, out)
            else:
                code = self._run_post(job, env, out)
            status = "success" if code == 0 else "failed"
        except subprocess.TimeoutExpired:
            status, error = "timeout", f"Stopped after {job['timeout']} s"
        except Exception as e:  # noqa: BLE001 - every failure belongs in the run record
            status, error = "error", out.mask(str(e))
        run.update(status=status, exit_code=code, finished=_now(),
                   duration=round(time.time() - run["started_ts"], 1), output=out.value())
        if error:
            run["error"] = error
        self._save_run(run)

    def _run_shell(self, run: dict, job: dict, env: dict, out: _Output) -> int:
        sh = shutil.which("bash") or "/bin/sh"
        tools = [self.depot_cli(out)] if re.search(r"\bdepot\b", job["run"]) else []
        return self._run_process(run, job, env, out, lambda src, home: [sh, "-e", "-c", job["run"]], tools)

    def _run_process(self, run: dict, job: dict, env: dict, out: _Output, argv, tools=(), after=None) -> int:
        """Run argv(src, home) in a fresh checkout of the run's commit, with the job's clean environment."""
        work = Path(tempfile.mkdtemp(prefix="forge-job-", dir=self.work_dir))
        try:
            src = work / "src"
            self._checkout(run["repo"], run["sha"], src)
            home = work / "home"
            home.mkdir()
            path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
            path = os.pathsep.join([str(Path(t).parent) for t in tools] + [path])
            full_env = {"PATH": path, "HOME": str(home), "LANG": "C.UTF-8", "CI": "true", "TMPDIR": str(work), **env}
            cmd = argv(src, home)
            proc = subprocess.Popen(cmd, cwd=src, env=full_env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            reader = threading.Thread(target=self._pump, args=(proc, out), daemon=True)
            reader.start()
            try:
                proc.wait(timeout=job["timeout"])
            except subprocess.TimeoutExpired:
                self._kill(proc)
                raise
            finally:
                reader.join(5)
            if after is not None:
                after(proc.returncode)  # while the work dir (and anything the process wrote there) still exists
            return proc.returncode
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # -- Depot --------------------------------------------------------------------------
    def depot_cli(self, out: _Output | None = None) -> str:
        """Path to the Depot CLI: FILEAPI_DEPOT_CLI, `depot` on PATH, or downloaded once into the tools dir."""
        pinned = os.environ.get("FILEAPI_DEPOT_CLI", "").strip()
        if pinned:
            return pinned
        found = shutil.which("depot")
        if found:
            return found
        version = os.environ.get("FILEAPI_DEPOT_CLI_VERSION", "").strip() or DEPOT_CLI_VERSION
        arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "arm64", "arm64": "arm64"}.get(
            os.uname().machine.lower(), os.uname().machine)
        system = os.uname().sysname  # Linux / Darwin, as dl.depot.dev spells them
        dest = Path(self.tools_dir) / f"depot-{version}-{system}-{arch}" / "depot"
        with self._tools_lock:
            if dest.is_file() and os.access(dest, os.X_OK):
                return str(dest)
            if out is not None:
                out.write(f"Downloading the Depot CLI {version} ({system}/{arch})...\n")
            url = f"https://dl.depot.dev/cli/download/{system}/{arch}/{version.removeprefix('v')}"
            if version == "latest":
                url = f"https://dl.depot.dev/cli/download/{system}/{arch}/latest"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix="depot-cli-", dir=dest.parent))
            try:
                with httpx.stream("GET", url, follow_redirects=True, timeout=300) as resp:
                    if not resp.is_success:
                        raise StorageError(f"Couldn't download the Depot CLI: {url} answered HTTP {resp.status_code}")
                    with open(tmp / "depot.tar.gz", "wb") as f:
                        for chunk in resp.iter_bytes():
                            f.write(chunk)
                import tarfile

                with tarfile.open(tmp / "depot.tar.gz") as tar:
                    member = next((m for m in tar.getmembers() if m.isfile() and m.name.rstrip("/").endswith("bin/depot")),
                                  None)
                    if member is None:
                        raise StorageError("The Depot CLI download has no bin/depot in it")
                    src = tar.extractfile(member)
                    with open(tmp / "depot", "wb") as f:
                        shutil.copyfileobj(src, f)
                (tmp / "depot").chmod(0o755)
                os.replace(tmp / "depot", dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            return str(dest)

    def _run_depot(self, run: dict, job: dict, env: dict, out: _Output) -> int:
        d = job["depot"]
        if not env.get("DEPOT_TOKEN"):
            raise StorageError("A depot job needs the DEPOT_TOKEN secret: a Depot project token (project > Settings > "
                               "Project tokens) or an organization token (organization Settings > API tokens)")
        project = _expand(d["project"], env) if d["project"] else env.get("DEPOT_PROJECT_ID", "")
        if not project:
            raise StorageError("Set depot.project in the job, or a DEPOT_PROJECT_ID secret (the ID on the "
                               "project's page at depot.dev)")
        tags = [_expand(t, env) for t in d["tags"]]
        save = d["save"] if d["save"] is not None else not d["push"]
        cmd = ["build", "--project", project, "--progress", "plain"]
        if d["file"]:
            cmd += ["--file", _expand(d["file"], env)]
        for p in d["platforms"]:
            cmd += ["--platform", _expand(p, env)]
        for t in tags:
            cmd += ["--tag", t]
        for k, v in d["build_args"].items():
            cmd += ["--build-arg", f"{k}={_expand(v, env)}"]
        if d["target"]:
            cmd += ["--target", _expand(d["target"], env)]
        if d["push"]:
            cmd.append("--push")
        if save:
            cmd.append("--save")
        cmd += [_expand(a, env) for a in d["args"]]
        cmd.append(_expand(d["context"], env))
        cli = self.depot_cli(out)
        meta: dict = {}

        def argv(src: Path, home: Path) -> list[str]:
            if d["login"]:
                login = {k: _expand(v, env) for k, v in d["login"].items()}
                auth = base64.b64encode(f"{login['username']}:{login['password']}".encode()).decode()
                (home / ".docker").mkdir()
                (home / ".docker" / "config.json").write_text(json.dumps({"auths": {login["server"]: {"auth": auth}}}))
            meta["file"] = home / "depot-metadata.json"
            out.write("$ depot " + " ".join(cmd) + "\n")
            return [cli, *cmd[:1], "--metadata-file", str(meta["file"]), *cmd[1:]]

        def read_meta(code: int) -> None:
            try:
                data = json.loads(meta["file"].read_text())
            except (KeyError, OSError, ValueError):
                data = {}
            build = data.get("depot.build") if isinstance(data.get("depot.build"), dict) else {}
            depot = {"project": project, "build_id": build.get("buildID") or "", "tags": tags, "pushed": d["push"],
                     "saved": save}
            if data.get("containerimage.digest"):
                depot["digest"] = data["containerimage.digest"]
            link = DEPOT_URL_RE.search("".join(out.parts))
            if link:
                depot["url"] = link.group(0)
            if save and depot["build_id"]:
                depot["image"] = f"registry.depot.dev/{project}:{depot['build_id']}"
            # Values that are secrets (a DEPOT_PROJECT_ID secret, say) stay masked here too, as in the output.
            run["depot"] = {k: out.mask(v) if isinstance(v, str) else [out.mask(x) for x in v] if isinstance(v, list)
                            else v for k, v in depot.items()}
            summary = [f"\nDepot build {depot['build_id'] or '(no id)'}: {'succeeded' if code == 0 else 'failed'}"]
            if depot.get("url"):
                summary.append(f"  {depot['url']}")
            if depot.get("image"):
                summary.append(f"  saved as {depot['image']}  (pull with a token from `depot pull-token`)")
            if d["push"] and code == 0:
                summary.extend(f"  pushed {t}" for t in tags)
            if depot.get("digest"):
                summary.append(f"  digest {depot['digest']}")
            out.write("\n".join(summary) + "\n")

        return self._run_process(run, job, {**env, "DEPOT_PROJECT_ID": project}, out, argv, after=read_meta)

    @staticmethod
    def _pump(proc, out: _Output) -> None:
        for chunk in iter(lambda: proc.stdout.read1(4096), b""):
            out.write(chunk.decode("utf-8", "replace"))

    @staticmethod
    def _kill(proc) -> None:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)  # the whole process group: the script and its children
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait()

    def _checkout(self, repo: str, sha: str, dest: Path) -> None:
        r = self.forge.open(repo)
        try:
            commit = r[sha.encode()]
            for entry in iter_tree_contents(r.object_store, commit.tree):
                path = dest / entry.path.decode("utf-8", "replace")
                if ".." in Path(entry.path.decode("utf-8", "replace")).parts:
                    continue
                kind = entry.mode & 0o170000
                if kind == 0o160000:  # submodule: not fetched
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                data = r[entry.sha].data
                if kind == 0o120000:
                    os.symlink(data, path)
                    continue
                path.write_bytes(data)
                if entry.mode & 0o111:
                    path.chmod(0o755)
        finally:
            r.close()

    def _run_post(self, job: dict, env: dict, out: _Output) -> int:
        url = _expand(job["post"], env).strip()
        if not url.startswith(("https://", "http://")):
            raise StorageError("post must be an http(s) URL")
        headers = {k: _expand(v, env) for k, v in job["headers"].items()}
        body = _expand(job["body"], env) if job["body"] is not None else None
        out.write(f"POST {url}\n")
        resp = self.http.post(url, headers=headers, content=body.encode() if body is not None else None,
                              timeout=job["timeout"])
        out.write(f"HTTP {resp.status_code} {resp.reason_phrase}\n\n")
        out.write(resp.text[:MAX_OUTPUT])
        return 0 if resp.is_success else 1

    def wait(self, timeout: float = 30) -> None:
        """Block until the jobs queued so far have finished (tests, and a clean shutdown)."""
        from concurrent.futures import wait

        with self._index_lock:
            pending = list(self._futures)
        wait(pending, timeout=timeout)
