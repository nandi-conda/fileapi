"""A minimal git forge: bare repos grouped by owner, served over git smart HTTP and browsable over MCP.

Repos are addressed as "<owner>/<name>" and cloned from /git/<owner>/<name>.git. The owner is the signed-in
account's atproto handle (e.g. "nandi.uk"), so callers normally give only the repo name. Older names
(repos from before owners, or renamed ones) are kept as aliases that resolve to the current name, so old
clone URLs keep working. Two backends, chosen the same way as file storage:

- Postgres (DATABASE_URL set): objects and refs are rows, so repos survive deploys on hosts without a
  persistent disk (FastAPI Cloud). Objects are content-addressed and immutable, so a process-wide cache is
  always safe; refs are updated with compare-and-swap so concurrent pushes from several replicas are safe.
- Disk: ordinary bare git repos under FILEAPI_GIT_ROOT, readable by the real git CLI.

Git itself is pure Python (dulwich), so the host needs no git binary.
"""

from __future__ import annotations

import gzip
import io
import re
import shutil
import threading
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from dulwich.errors import NotGitRepository
from dulwich.lru_cache import LRUSizeCache
from dulwich.object_store import MemoryObjectStore
from dulwich.objects import Blob, Commit, Tree, ZERO_SHA, ShaFile
from dulwich.pack import PackData, PackInflater
from dulwich.protocol import ReceivableProtocol
from dulwich.refs import SYMREF, RefsContainer
from dulwich.repo import BaseRepo, MemoryRepo, Repo
from dulwich.server import Backend, ReceivePackHandler, UploadPackHandler

from .storage import Conflict, NotFound, StorageError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SERVICES = {"git-upload-pack": UploadPackHandler, "git-receive-pack": ReceivePackHandler}


def check_name(kind: str, value: str) -> str:
    value = (value or "").strip()
    if value.endswith(".git"):
        value = value[:-4]
    if not NAME_RE.match(value) or ".." in value:
        raise StorageError(f"Invalid {kind} name {value!r}: use letters, digits, '.', '_' or '-' (max 64)")
    return value


def split_repo(repo: str) -> tuple[str, str]:
    project, sep, name = (repo or "").strip("/").partition("/")
    if not sep:
        raise StorageError(f"Repo must be '<owner>/<name>', got {repo!r}")
    return check_name("owner", project), check_name("repo", name)


def owner_name(handle: str | None, did: str | None = None) -> str:
    """The repo namespace for an account: its handle, or (no handle) its DID made URL-safe."""
    raw = (handle or "").strip().lower().lstrip("@") or re.sub(r"[^A-Za-z0-9._-]+", "-", did or "").strip("-.")
    return check_name("owner", raw[:64] or "admin")


def qualify(repo: str, owner: str) -> str:
    """'<name>' -> '<owner>/<name>'; an explicit '<owner>/<name>' is kept as given."""
    repo = (repo or "").strip().strip("/")
    return repo if "/" in repo else f"{owner}/{repo}"


@dataclass
class RepoInfo:
    project: str
    name: str
    description: str
    created: str
    public: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.project}/{self.name}"


# --------------------------------------------------------------------------- Postgres backend

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS fileapi_git_repos (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, name)
);
ALTER TABLE fileapi_git_repos ADD COLUMN IF NOT EXISTS public boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS fileapi_git_objects (
    repo_id bigint NOT NULL REFERENCES fileapi_git_repos(id) ON DELETE CASCADE,
    sha     bytea NOT NULL,
    type    smallint NOT NULL,
    data    bytea NOT NULL,
    PRIMARY KEY (repo_id, sha)
);
CREATE TABLE IF NOT EXISTS fileapi_git_aliases (
    old text PRIMARY KEY,
    new text NOT NULL
);
CREATE TABLE IF NOT EXISTS fileapi_git_refs (
    repo_id bigint NOT NULL REFERENCES fileapi_git_repos(id) ON DELETE CASCADE,
    name    bytea NOT NULL,
    value   bytea NOT NULL,
    PRIMARY KEY (repo_id, name)
);
"""

# Objects never change once written, so caching them across requests (and repos) is always correct.
_OBJ_CACHE: LRUSizeCache = LRUSizeCache(64 * 1024 * 1024, compute_size=lambda v: len(v[1]))
_CACHE_LOCK = threading.Lock()


class PgObjectStore(MemoryObjectStore):
    def __init__(self, pool, repo_id: int):
        super().__init__()
        self.pool, self.repo_id = pool, repo_id

    def _hex(self, sha) -> bytes:
        return self._to_hexsha(sha)

    def contains_loose(self, sha) -> bool:
        key = (self.repo_id, self._hex(sha))
        with _CACHE_LOCK:
            if key in _OBJ_CACHE:
                return True
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT 1 FROM fileapi_git_objects WHERE repo_id = %s AND sha = %s",
                (self.repo_id, bytes.fromhex(key[1].decode())),
            ).fetchone()
        return row is not None

    def __iter__(self) -> Iterator[bytes]:
        with self.pool.connection() as c:
            rows = c.execute("SELECT sha FROM fileapi_git_objects WHERE repo_id = %s", (self.repo_id,)).fetchall()
        return iter([bytes(r[0]).hex().encode() for r in rows])

    def get_raw(self, name) -> tuple[int, bytes]:
        key = (self.repo_id, self._hex(name))
        with _CACHE_LOCK:
            hit = _OBJ_CACHE.get(key)
        if hit is not None:
            return hit
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT type, data FROM fileapi_git_objects WHERE repo_id = %s AND sha = %s",
                (self.repo_id, bytes.fromhex(key[1].decode())),
            ).fetchone()
        if row is None:
            raise KeyError(key[1])
        value = (row[0], zlib.decompress(row[1]))
        with _CACHE_LOCK:
            _OBJ_CACHE[key] = value
        return value

    def __getitem__(self, name) -> ShaFile:
        type_num, raw = self.get_raw(name)
        return ShaFile.from_raw_string(type_num, raw, sha=self._hex(name))

    def add_object(self, obj: ShaFile) -> None:
        self.add_objects([(obj, None)])

    def add_objects(self, objects, progress=None) -> None:
        rows = [(self.repo_id, obj.sha().digest(), obj.type_num, zlib.compress(obj.as_raw_string()))
                for obj, _ in objects]
        if not rows:
            return
        with self.pool.connection() as c, c.cursor() as cur:
            cur.executemany(
                "INSERT INTO fileapi_git_objects (repo_id, sha, type, data) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                rows,
            )

    def add_pack(self):
        from tempfile import SpooledTemporaryFile

        f = SpooledTemporaryFile(max_size=16 * 1024 * 1024, prefix="incoming-")

        def commit() -> None:
            size = f.tell()
            try:
                if size > 0:
                    f.seek(0)
                    p = PackData.from_file(f, self.object_format, size)
                    try:
                        p.check()
                        batch = []
                        for obj in PackInflater.for_pack_data(p, self.get_raw):
                            batch.append((obj, None))
                            if len(batch) >= 500:
                                self.add_objects(batch)
                                batch = []
                        self.add_objects(batch)
                    finally:
                        p.close()
            finally:
                f.close()

        return f, commit, f.close


class PgRefsContainer(RefsContainer):
    """Refs as rows. Updates are compare-and-swap in SQL, so concurrent pushes cannot clobber each other."""

    def __init__(self, pool, repo_id: int):
        super().__init__()
        self.pool, self.repo_id = pool, repo_id

    def _q(self, sql: str, args: tuple = ()):
        with self.pool.connection() as c:
            return c.execute(sql, (self.repo_id, *args))

    def allkeys(self) -> set[bytes]:
        return {bytes(r[0]) for r in self._q("SELECT name FROM fileapi_git_refs WHERE repo_id = %s").fetchall()}

    def read_loose_ref(self, name: bytes) -> bytes | None:
        row = self._q("SELECT value FROM fileapi_git_refs WHERE repo_id = %s AND name = %s", (name,)).fetchone()
        return bytes(row[0]) if row else None

    def get_packed_refs(self) -> dict:
        return {}

    def get_peeled(self, name: bytes):
        return None

    def set_symbolic_ref(self, name, other, committer=None, timestamp=None, timezone=None, message=None) -> None:
        self._q(
            "INSERT INTO fileapi_git_refs (repo_id, name, value) VALUES (%s, %s, %s) "
            "ON CONFLICT (repo_id, name) DO UPDATE SET value = EXCLUDED.value",
            (name, SYMREF + other),
        )

    def set_if_equals(self, name, old_ref, new_ref, committer=None, timestamp=None, timezone=None, message=None) -> bool:
        self._check_ref_value(new_ref)
        self._check_refname(name)
        # Follow symrefs (e.g. HEAD -> refs/heads/main) like git does.
        try:
            realnames, _ = self.follow(name)
            name = realnames[-1]
        except Exception:
            pass
        if old_ref is None:
            self._q(
                "INSERT INTO fileapi_git_refs (repo_id, name, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (repo_id, name) DO UPDATE SET value = EXCLUDED.value",
                (name, new_ref),
            )
            return True
        if old_ref == ZERO_SHA:
            return self.add_if_new(name, new_ref)
        with self.pool.connection() as c:
            cur = c.execute(
                "UPDATE fileapi_git_refs SET value = %s WHERE repo_id = %s AND name = %s AND value = %s",
                (new_ref, self.repo_id, name, old_ref),
            )
            return cur.rowcount == 1

    def add_if_new(self, name, ref, committer=None, timestamp=None, timezone=None, message=None) -> bool:
        self._check_ref_value(ref)
        self._check_refname(name)
        cur = self._q(
            "INSERT INTO fileapi_git_refs (repo_id, name, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (name, ref),
        )
        return cur.rowcount == 1

    def remove_if_equals(self, name, old_ref, committer=None, timestamp=None, timezone=None, message=None) -> bool:
        if old_ref is None or old_ref == ZERO_SHA:
            self._q("DELETE FROM fileapi_git_refs WHERE repo_id = %s AND name = %s", (name,))
            return True
        cur = self._q(
            "DELETE FROM fileapi_git_refs WHERE repo_id = %s AND name = %s AND value = %s", (name, old_ref)
        )
        return cur.rowcount == 1


class PgRepo(MemoryRepo):
    def __init__(self, pool, repo_id: int, description: str = ""):
        super().__init__()
        self.object_store = PgObjectStore(pool, repo_id)
        self.refs = PgRefsContainer(pool, repo_id)
        self._description = description.encode()


class PgForgeStore:
    backend = "postgres"

    def __init__(self, pool):
        self.pool = pool

    def init(self) -> None:
        with self.pool.connection() as c:
            c.execute(PG_SCHEMA)

    @staticmethod
    def _info(row) -> RepoInfo:
        return RepoInfo(row[0], row[1], row[2], row[3].isoformat(), bool(row[4]))

    def create(self, project: str, name: str, description: str, default_branch: str) -> RepoInfo:
        with self.pool.connection() as c:
            row = c.execute(
                "INSERT INTO fileapi_git_repos (project, name, description) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING id, project, name, description, created, public",
                (project, name, description),
            ).fetchone()
        if row is None:
            raise Conflict(f"Repo already exists: {project}/{name}")
        PgRefsContainer(self.pool, row[0]).set_symbolic_ref(b"HEAD", b"refs/heads/" + default_branch.encode())
        return self._info(row[1:])

    def list(self, project: str | None) -> list[RepoInfo]:
        sql = "SELECT project, name, description, created, public FROM fileapi_git_repos"
        args: tuple = ()
        if project is not None:
            sql, args = sql + " WHERE project = %s", (project,)
        with self.pool.connection() as c:
            return [self._info(r) for r in c.execute(sql + " ORDER BY project, name", args).fetchall()]

    def open(self, project: str, name: str) -> BaseRepo:
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT id, description FROM fileapi_git_repos WHERE project = %s AND name = %s", (project, name)
            ).fetchone()
        if row is None:
            raise NotFound(f"No such repo: {project}/{name}")
        return PgRepo(self.pool, row[0], row[1])

    def set_public(self, project: str, name: str, public: bool) -> None:
        with self.pool.connection() as c:
            cur = c.execute("UPDATE fileapi_git_repos SET public = %s WHERE project = %s AND name = %s",
                            (public, project, name))
        if cur.rowcount == 0:
            raise NotFound(f"No such repo: {project}/{name}")

    def set_description(self, project: str, name: str, description: str) -> None:
        with self.pool.connection() as c:
            cur = c.execute("UPDATE fileapi_git_repos SET description = %s WHERE project = %s AND name = %s",
                            (description, project, name))
        if cur.rowcount == 0:
            raise NotFound(f"No such repo: {project}/{name}")

    def rename(self, project: str, name: str, new_project: str, new_name: str) -> None:
        with self.pool.connection() as c:
            if c.execute("SELECT 1 FROM fileapi_git_repos WHERE project = %s AND name = %s",
                         (new_project, new_name)).fetchone():
                raise Conflict(f"Repo already exists: {new_project}/{new_name}")
            cur = c.execute("UPDATE fileapi_git_repos SET project = %s, name = %s WHERE project = %s AND name = %s",
                            (new_project, new_name, project, name))
        if cur.rowcount == 0:
            raise NotFound(f"No such repo: {project}/{name}")

    def is_public(self, project: str, name: str) -> bool:
        with self.pool.connection() as c:
            row = c.execute("SELECT public FROM fileapi_git_repos WHERE project = %s AND name = %s",
                            (project, name)).fetchone()
        return bool(row and row[0])

    # Old "<owner>/<name>" -> current one, so URLs survive renames and the move to owner namespaces.
    def get_alias(self, full: str) -> str | None:
        with self.pool.connection() as c:
            row = c.execute("SELECT new FROM fileapi_git_aliases WHERE old = %s", (full,)).fetchone()
        return row[0] if row else None

    def set_alias(self, old: str, new: str) -> None:
        with self.pool.connection() as c:
            c.execute("UPDATE fileapi_git_aliases SET new = %s WHERE new = %s", (new, old))
            c.execute("DELETE FROM fileapi_git_aliases WHERE old = %s", (new,))
            c.execute("INSERT INTO fileapi_git_aliases (old, new) VALUES (%s, %s) "
                      "ON CONFLICT (old) DO UPDATE SET new = EXCLUDED.new", (old, new))

    def drop_aliases(self, full: str) -> None:
        """Forget aliases named `full` (a real repo now has that name) or pointing at it (it was deleted)."""
        with self.pool.connection() as c:
            c.execute("DELETE FROM fileapi_git_aliases WHERE old = %s OR new = %s", (full, full))

    def aliases(self) -> dict[str, str]:
        with self.pool.connection() as c:
            return dict(c.execute("SELECT old, new FROM fileapi_git_aliases ORDER BY old").fetchall())

    def delete(self, project: str, name: str) -> None:
        with self.pool.connection() as c:
            cur = c.execute("DELETE FROM fileapi_git_repos WHERE project = %s AND name = %s", (project, name))
        if cur.rowcount == 0:
            raise NotFound(f"No such repo: {project}/{name}")


# --------------------------------------------------------------------------- Disk backend


class DiskForgeStore:
    backend = "disk"

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, project: str, name: str) -> Path:
        return self.root / project / f"{name}.git"

    def _info(self, project: str, name: str) -> RepoInfo:
        path = self._path(project, name)
        desc_file = path / "description"
        desc = desc_file.read_text().strip() if desc_file.exists() else ""
        if desc.startswith("Unnamed repository"):
            desc = ""
        created = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(path.stat().st_ctime))
        return RepoInfo(project, name, desc, created, (path / "git-daemon-export-ok").exists())

    def create(self, project: str, name: str, description: str, default_branch: str) -> RepoInfo:
        path = self._path(project, name)
        if path.exists():
            raise Conflict(f"Repo already exists: {project}/{name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        repo = Repo.init_bare(str(path), mkdir=True)
        repo.refs.set_symbolic_ref(b"HEAD", b"refs/heads/" + default_branch.encode())
        (path / "description").write_text(description + "\n")
        repo.close()
        return self._info(project, name)

    def list(self, project: str | None) -> list[RepoInfo]:
        projects = [project] if project is not None else sorted(p.name for p in self.root.iterdir() if p.is_dir())
        out = []
        for p in projects:
            d = self.root / p
            if d.is_dir():
                out += [self._info(p, r.name[:-4]) for r in sorted(d.iterdir()) if r.name.endswith(".git")]
        return out

    def open(self, project: str, name: str) -> BaseRepo:
        path = self._path(project, name)
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        return Repo(str(path))

    # Public repos carry git's own marker file, as with git-daemon / git http-backend.
    def set_public(self, project: str, name: str, public: bool) -> None:
        path = self._path(project, name)
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        marker = path / "git-daemon-export-ok"
        marker.touch() if public else marker.unlink(missing_ok=True)

    def set_description(self, project: str, name: str, description: str) -> None:
        path = self._path(project, name)
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        (path / "description").write_text(description + "\n")

    def rename(self, project: str, name: str, new_project: str, new_name: str) -> None:
        path, new = self._path(project, name), self._path(new_project, new_name)
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        if new.exists():
            raise Conflict(f"Repo already exists: {new_project}/{new_name}")
        new.parent.mkdir(parents=True, exist_ok=True)
        path.rename(new)
        if not any(path.parent.iterdir()):
            path.parent.rmdir()

    def is_public(self, project: str, name: str) -> bool:
        return (self._path(project, name) / "git-daemon-export-ok").exists()

    # Aliases (old name -> current name) live in one JSON file next to the owner directories.
    def _alias_file(self) -> Path:
        return self.root / ".aliases.json"

    def aliases(self) -> dict[str, str]:
        import json

        try:
            return json.loads(self._alias_file().read_text())
        except FileNotFoundError:
            return {}

    def _save_aliases(self, data: dict[str, str]) -> None:
        import json
        import os

        tmp = self._alias_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(data.items())), indent=1))
        os.replace(tmp, self._alias_file())

    def get_alias(self, full: str) -> str | None:
        return self.aliases().get(full)

    def set_alias(self, old: str, new: str) -> None:
        data = {k: (new if v == old else v) for k, v in self.aliases().items() if k != new}
        data[old] = new
        self._save_aliases(data)

    def drop_aliases(self, full: str) -> None:
        data = self.aliases()
        kept = {k: v for k, v in data.items() if full not in (k, v)}
        if kept != data:
            self._save_aliases(kept)

    def delete(self, project: str, name: str) -> None:
        path = self._path(project, name)
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        shutil.rmtree(path)


# --------------------------------------------------------------------------- Forge


class _Backend(Backend):
    def __init__(self, forge: "Forge"):
        self.forge = forge

    def open_repository(self, path: str):
        try:
            return self.forge.open(path)
        except StorageError as e:
            raise NotGitRepository(str(e)) from None


def _ident(name: str | None, email: str | None) -> bytes:
    return f"{name or 'fileapi'} <{email or 'fileapi@localhost'}>".encode()


class Forge:
    def __init__(self, store):
        self.store = store
        self._backend = _Backend(self)

    # -- names ----------------------------------------------------------------
    def canonical(self, repo: str) -> str:
        """The current '<owner>/<name>' for a repo, following an alias left by a rename or migration."""
        full = "/".join(split_repo(repo))
        return self.store.get_alias(full) or full

    def _parts(self, repo: str) -> tuple[str, str]:
        return split_repo(self.canonical(repo))

    def migrate_to_owner(self, owner: str) -> list[tuple[str, str]]:
        """Move every repo outside `owner` into it, keeping the old name as an alias. Idempotent.

        A name already taken in `owner` becomes '<old owner>-<name>'.
        """
        owner = check_name("owner", owner)
        moved = []
        taken = {i.name for i in self.store.list(owner)}
        for info in self.store.list(None):
            if info.project == owner:
                continue
            name = info.name if info.name not in taken else check_name("repo", f"{info.project}-{info.name}"[:64])
            try:
                self.store.rename(info.project, info.name, owner, name)
            except (Conflict, NotFound):  # another replica got there first, or a clash: leave it
                continue
            self.store.set_alias(info.full_name, f"{owner}/{name}")
            taken.add(name)
            moved.append((info.full_name, f"{owner}/{name}"))
        return moved

    # -- repo management ----------------------------------------------------
    def create(self, project: str, name: str, description: str = "", default_branch: str = "main") -> RepoInfo:
        project, name = check_name("owner", project), check_name("repo", name)
        check_name("branch", default_branch)
        info = self.store.create(project, name, description.strip(), default_branch)
        self.store.drop_aliases(info.full_name)  # a real repo now owns this name
        return info

    def list(self, project: str | None = None) -> list[RepoInfo]:
        return self.store.list(check_name("owner", project) if project else None)

    def info(self, repo: str) -> RepoInfo:
        owner, name = self._parts(repo)
        found = next((i for i in self.store.list(owner) if i.name == name), None)
        if found is None:
            raise NotFound(f"No such repo: {owner}/{name}")
        return found

    def open(self, repo: str) -> BaseRepo:
        return self.store.open(*self._parts(repo))

    def delete(self, repo: str) -> None:
        owner, name = self._parts(repo)
        self.store.delete(owner, name)
        self.store.drop_aliases(f"{owner}/{name}")

    def set_public(self, repo: str, public: bool) -> None:
        self.store.set_public(*self._parts(repo), public)

    def is_public(self, repo: str) -> bool:
        return self.store.is_public(*self._parts(repo))

    def set_description(self, repo: str, description: str) -> None:
        self.store.set_description(*self._parts(repo), description.strip())

    def rename(self, repo: str, new_repo: str) -> str:
        old, new = self._parts(repo), split_repo(new_repo)
        if old != new:
            self.store.rename(*old, *new)
            self.store.set_alias("/".join(old), "/".join(new))  # old clone URLs keep working
        return "/".join(new)

    def set_default_branch(self, repo: str, branch: str) -> None:
        r = self.open(repo)
        try:
            heads = [k for k in r.refs.allkeys() if k.startswith(b"refs/heads/")]
            if b"refs/heads/" + branch.encode() not in heads:
                if heads:
                    raise NotFound(f"No such branch: {branch}")
                check_name("branch", branch)  # empty repo: any sane name, for the first push
            r.refs.set_symbolic_ref(b"HEAD", b"refs/heads/" + branch.encode())
        finally:
            r.close()

    # -- smart HTTP -----------------------------------------------------------
    def advertise(self, repo: str, service: str) -> bytes:
        self.open(repo).close()  # 404 early for unknown repos
        out = io.BytesIO()
        proto = ReceivableProtocol(io.BytesIO().read, out.write)
        handler = SERVICES[service](self._backend, [repo], proto, stateless_rpc=True, advertise_refs=True)
        proto.write_pkt_line(b"# service=" + service.encode() + b"\n")
        proto.write_pkt_line(None)
        handler.handle()
        return out.getvalue()

    def rpc(self, repo: str, service: str, body: bytes, content_encoding: str | None = None) -> bytes:
        self.open(repo).close()
        if content_encoding == "gzip":
            body = gzip.decompress(body)
        out = io.BytesIO()
        proto = ReceivableProtocol(io.BytesIO(body).read, out.write)
        SERVICES[service](self._backend, [repo], proto, stateless_rpc=True).handle()
        return out.getvalue()

    # -- browsing -------------------------------------------------------------
    @staticmethod
    def _resolve(r: BaseRepo, ref: str) -> bytes:
        ref = ref or "HEAD"
        candidates = [ref, f"refs/heads/{ref}", f"refs/tags/{ref}"]
        for c in candidates:
            try:
                return r.refs[c.encode()]
            except KeyError:
                continue
        if re.fullmatch(r"[0-9a-f]{4,40}", ref):
            if len(ref) == 40 and ref.encode() in r.object_store:
                return ref.encode()
            matches = [s for s in r.object_store if s.startswith(ref.encode())] if len(ref) < 40 else []
            if len(matches) == 1:
                return matches[0]
        if ref == "HEAD":
            raise NotFound("Repo is empty: nothing has been pushed yet")
        raise NotFound(f"Unknown ref: {ref}")

    @staticmethod
    def _peel_commit(r: BaseRepo, sha: bytes) -> Commit:
        obj = r[sha]
        while not isinstance(obj, Commit):
            if obj.type_name == b"tag":
                obj = r[obj.object[1]]
            else:
                raise StorageError("Ref does not point at a commit")
        return obj

    def branches(self, repo: str) -> dict:
        r = self.open(repo)
        try:
            refs = r.refs.as_dict()
            head = r.refs.read_ref(b"HEAD") or b""
            return {
                "default": head[len(SYMREF + b"refs/heads/"):].decode() if head.startswith(SYMREF) else None,
                "branches": {k[11:].decode(): v.decode() for k, v in refs.items() if k.startswith(b"refs/heads/")},
                "tags": {k[10:].decode(): v.decode() for k, v in refs.items() if k.startswith(b"refs/tags/")},
            }
        finally:
            r.close()

    def browse(self, repo: str, path: str = "", ref: str = "HEAD") -> dict:
        """A directory listing or a file's content at a ref."""
        r = self.open(repo)
        try:
            commit = self._peel_commit(r, self._resolve(r, ref))
            obj = r[commit.tree]
            parts = [p for p in path.strip("/").split("/") if p]
            for i, part in enumerate(parts):
                if not isinstance(obj, Tree):
                    raise NotFound(f"Not a directory: {'/'.join(parts[:i])}")
                try:
                    _, sha = obj[part.encode()]
                except KeyError:
                    raise NotFound(f"No such path at {ref}: {path}") from None
                obj = r[sha]
            base = {"commit": commit.id.decode(), "path": "/".join(parts)}
            if isinstance(obj, Tree):
                entries = []
                for item in obj.iteritems():
                    is_dir = item.mode & 0o170000 == 0o040000
                    size = None if is_dir else len(r[item.sha].as_raw_string()) if item.mode & 0o170000 != 0o160000 else None
                    entries.append({"name": item.path.decode(errors="replace") + ("/" if is_dir else ""), "size": size})
                entries.sort(key=lambda e: (not e["name"].endswith("/"), e["name"]))
                return {**base, "type": "dir", "entries": entries}
            data = obj.as_raw_string()
            try:
                return {**base, "type": "file", "size": len(data), "content": data.decode()}
            except UnicodeDecodeError:
                return {**base, "type": "file", "size": len(data), "content": None, "binary": True}
        finally:
            r.close()

    def _file_at(self, r: BaseRepo, ref: str, path: str) -> tuple[Commit, bytes]:
        commit = self._peel_commit(r, self._resolve(r, ref))
        obj = r[commit.tree]
        parts = [p for p in path.strip("/").split("/") if p]
        sha = None
        for part in parts:
            if not isinstance(obj, Tree):
                raise NotFound(f"No such file at {ref}: {path}")
            try:
                _, sha = obj[part.encode()]
            except KeyError:
                raise NotFound(f"No such file at {ref}: {path}") from None
            obj = r[sha]
        if sha is None or isinstance(obj, Tree):
            raise NotFound(f"Not a file: {path}")
        return commit, sha

    def symbols(self, repo: str, path: str, ref: str = "HEAD") -> dict:
        """The definitions in one file (its outline), for the web UI's symbol list."""
        from . import codenav
        r = self.open(repo)
        try:
            commit, sha = self._file_at(r, ref, path)
            return {"commit": commit.id.decode(), **codenav.outline(r, commit, "/".join(p for p in path.split("/") if p), sha)}
        finally:
            r.close()

    def symbol_at(self, repo: str, path: str, line: int, col: int, ref: str = "HEAD", references: bool = True) -> dict:
        """Definitions (and references) of the identifier at line/col of a file, searched across the commit."""
        from . import codenav
        r = self.open(repo)
        try:
            commit, sha = self._file_at(r, ref, path)
            clean = "/".join(p for p in path.split("/") if p)
            return {"commit": commit.id.decode(), **codenav.lookup(r, commit, clean, sha, line, col, references)}
        finally:
            r.close()

    def log(self, repo: str, ref: str = "HEAD", path: str | None = None, limit: int = 20) -> list[dict]:
        r = self.open(repo)
        try:
            sha = self._peel_commit(r, self._resolve(r, ref)).id
            walker = r.get_walker(include=[sha], max_entries=max(1, min(limit, 200)),
                                  paths=[path.strip("/").encode()] if path else None)
            out = []
            for entry in walker:
                c = entry.commit
                out.append({
                    "sha": c.id.decode(),
                    "author": c.author.decode(errors="replace"),
                    "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(c.author_time)),
                    "message": c.message.decode(errors="replace").strip(),
                    "parents": [p.decode() for p in c.parents],
                })
            return out
        finally:
            r.close()

    def commit_files(self, repo: str, branch: str, files: dict[str, str | None], message: str,
                     author_name: str | None = None, author_email: str | None = None,
                     expected_head: str | None = None) -> dict:
        """Write files (None deletes) as one commit on a branch, creating the branch if needed."""
        check_name("branch", branch.replace("/", "-"))
        if not files:
            raise StorageError("At least one file is required")
        r = self.open(repo)
        try:
            ref = b"refs/heads/" + branch.encode()
            try:
                old = r.refs[ref]
            except KeyError:
                old = None
            if expected_head and (old or b"").decode() != expected_head:
                raise Conflict(f"Branch {branch} moved: now at {(old or b'(none)').decode()}")
            base_tree = self._peel_commit(r, old).tree if old else None
            changes: dict[tuple[bytes, ...], bytes | None] = {}
            for p, content in files.items():
                parts = tuple(x.encode() for x in p.strip("/").split("/") if x)
                if not parts or any(x in (b".", b"..", b".git") for x in parts):
                    raise StorageError(f"Invalid path: {p!r}")
                changes[parts] = None if content is None else content.encode()
            new_objects: list = []
            tree_id = _apply_tree(r, base_tree, changes, new_objects)
            if tree_id is None:
                raise StorageError("Commit would leave the repo empty")
            if old and tree_id == base_tree:
                raise StorageError("Nothing to commit: files are unchanged")
            c = Commit()
            c.tree = tree_id
            c.parents = [old] if old else []
            c.author = _ident(author_name, author_email)
            c.committer = _ident("fileapi", "fileapi@localhost")
            c.author_time = c.commit_time = int(time.time())
            c.author_timezone = c.commit_timezone = 0
            c.encoding = b"UTF-8"
            c.message = message.encode() + (b"" if message.endswith("\n") else b"\n")
            new_objects.append(c)
            r.object_store.add_objects([(o, None) for o in new_objects])
            ok = r.refs.set_if_equals(ref, old, c.id) if old else r.refs.add_if_new(ref, c.id)
            if not ok:
                raise Conflict(f"Branch {branch} moved during the commit; retry")
            return {"commit": c.id.decode(), "branch": branch, "parent": old.decode() if old else None}
        finally:
            r.close()


def _apply_tree(r: BaseRepo, tree_id: bytes | None, changes: dict, new_objects: list) -> bytes | None:
    tree = r[tree_id].copy() if tree_id else Tree()
    here = {k[0]: v for k, v in changes.items() if len(k) == 1}
    nested: dict[bytes, dict] = {}
    for k, v in changes.items():
        if len(k) > 1:
            nested.setdefault(k[0], {})[k[1:]] = v
    for name, content in here.items():
        if name in nested:
            raise StorageError(f"Path is both a file and a directory: {name.decode()}")
        if content is None:
            if name not in tree:
                raise NotFound(f"Cannot delete missing file: {name.decode()}")
            del tree[name]
        else:
            blob = Blob.from_string(content)
            new_objects.append(blob)
            tree.add(name, 0o100644, blob.id)
    for name, sub in nested.items():
        sub_id = None
        if name in tree:
            mode, sub_id = tree[name]
            if mode & 0o170000 != 0o040000:
                raise StorageError(f"Not a directory: {name.decode()}")
        new_sub = _apply_tree(r, sub_id, sub, new_objects)
        if new_sub is None:
            if name in tree:
                del tree[name]
        else:
            tree.add(name, 0o040000, new_sub)
    if not tree.items():
        return None
    new_objects.append(tree)
    return tree.id


# --------------------------------------------------------------------------- bundles (push/pull over MCP)


def _bundle_pack_offset(data: bytes) -> int:
    """Byte offset where a bundle's pack starts: right after the blank line that ends the header."""
    end = data.find(b"\n\n")
    if end < 0 or data[end + 2:end + 6] != b"PACK":
        raise StorageError("Not a git bundle (expected a header, a blank line, then a pack)")
    return end + 2


def push_bundle(forge: Forge, repo: str, data: bytes, force: bool = False) -> list[dict]:
    """Apply a `git bundle create` file: store its objects, then move each branch/tag it carries.

    Each ref update is fast-forward-only unless force, and compare-and-swap so a concurrent push wins cleanly.
    """
    from dulwich.bundle import read_bundle
    from dulwich.graph import can_fast_forward

    try:
        with read_bundle(io.BytesIO(data)) as b:
            refs = {k: v for k, v in b.references.items() if k.startswith((b"refs/heads/", b"refs/tags/"))}
            prereqs = [sha for sha, _ in b.prerequisites]
    except StorageError:
        raise
    except Exception as e:
        raise StorageError(f"Not a valid git bundle: {e}") from None
    if not refs:
        raise StorageError("The bundle has no branches or tags. Create it with e.g. `git bundle create f.bundle main`")
    r = forge.open(repo)
    try:
        missing = [p.decode() for p in prereqs if p not in r.object_store]
        if missing:
            raise Conflict(
                f"The server does not have commit(s) {', '.join(m[:12] for m in missing)} that this bundle builds on. "
                "Fetch first (fetch_bundle), or bundle the full history."
            )
        pack = io.BytesIO(data[_bundle_pack_offset(data):])
        r.object_store.add_thin_pack(pack.read, None)
        results = []
        for ref, new in sorted(refs.items()):
            name = ref.decode()
            if new not in r.object_store:
                results.append({"ref": name, "status": "rejected", "reason": "object missing from bundle"})
                continue
            try:
                old = r.refs[ref]
            except KeyError:
                old = None
            if old == new:
                results.append({"ref": name, "status": "up to date", "sha": new.decode()})
                continue
            forced = False
            if old is not None:
                if ref.startswith(b"refs/tags/") and not force:
                    results.append({"ref": name, "status": "rejected", "reason": "tag exists (use force)"})
                    continue
                if not force:
                    try:
                        ff = can_fast_forward(r, old, new)
                    except KeyError:
                        ff = False
                    if not ff:
                        results.append({"ref": name, "status": "rejected",
                                        "reason": "non-fast-forward: fetch and merge/rebase first, or use force"})
                        continue
                else:
                    forced = True
            ok = r.refs.set_if_equals(ref, old, new) if old is not None else r.refs.add_if_new(ref, new)
            if not ok:
                results.append({"ref": name, "status": "rejected", "reason": "changed during the push; retry"})
                continue
            results.append({"ref": name, "status": "forced" if forced else ("created" if old is None else "updated"),
                            "old": old.decode() if old else None, "sha": new.decode()})
        return results
    finally:
        r.close()


def fetch_bundle(forge: Forge, repo: str, refs: list[str] | None = None, have: list[str] | None = None) -> tuple[bytes, dict]:
    """A bundle with the given branches/tags (default: all), leaving out history the client already has."""
    from dulwich.bundle import create_bundle_from_repo, write_bundle

    r = forge.open(repo)
    try:
        all_refs = {k: v for k, v in r.refs.as_dict().items() if k.startswith((b"refs/heads/", b"refs/tags/"))}
        if refs:
            wanted = []
            for name in refs:
                full = next((c for c in (name.encode(), b"refs/heads/" + name.encode(), b"refs/tags/" + name.encode())
                             if c in all_refs), None)
                if full is None:
                    raise NotFound(f"Unknown branch or tag: {name}")
                wanted.append(full)
        else:
            wanted = sorted(all_refs)
        if not wanted:
            raise NotFound("Repo is empty: nothing has been pushed yet")
        # Only commits the server knows can be prerequisites; skip ones that are already the tips (nothing to send).
        prereqs = []
        for sha in have or []:
            s = sha.strip().lower().encode()
            if len(s) == 40 and s in r.object_store and isinstance(r[s], Commit):
                prereqs.append(s)
        tips = {all_refs[w] for w in wanted}
        if prereqs and tips <= set(prereqs):
            return b"", {w.decode(): all_refs[w].decode() for w in wanted}
        head = r.refs.read_ref(b"HEAD") or b""
        # Carry HEAD when the default branch is included, so `git clone f.bundle` checks it out.
        extra = [b"HEAD"] if head.startswith(SYMREF) and head[len(SYMREF):] in wanted else []
        bundle = create_bundle_from_repo(r, refs=extra + wanted, prerequisites=prereqs, version=2)
        out = io.BytesIO()
        try:
            write_bundle(out, bundle)
        finally:
            bundle.close()
        return out.getvalue(), {w.decode(): all_refs[w].decode() for w in wanted}
    finally:
        r.close()


# --------------------------------------------------------------------------- short-lived repo credentials


class RepoPasses:
    """Short-lived git credentials for one repo, so an MCP client can run git without anyone typing the token.

    Stateless and replica-safe: a pass is "<repo>|<expiry>" signed with HMAC under a random server secret
    kept in the token KV. They cannot be revoked individually; rotating the secret (deleting the
    `forge_secret` record) kills them all.
    """

    PREFIX = "fg_"
    MAX_MINUTES = 24 * 60

    def __init__(self, kv):
        self.kv = kv
        self._secret: bytes | None = None

    def _key(self) -> bytes:
        if self._secret is None:
            import secrets

            self.kv.create("forge_secret", {"secret": secrets.token_hex(32)})
            self._secret = bytes.fromhex(self.kv.get("forge_secret")["secret"])
        return self._secret

    def _sig(self, payload: str) -> str:
        import hashlib
        import hmac

        return hmac.new(self._key(), payload.encode(), hashlib.sha256).hexdigest()[:40]

    def issue(self, repo: str, minutes: int) -> tuple[str, int]:
        import base64

        minutes = max(1, min(int(minutes), self.MAX_MINUTES))
        expires = int(time.time()) + minutes * 60
        payload = f"{repo}|{expires}"
        body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{self.PREFIX}{body}.{self._sig(payload)}", expires

    def check(self, token: str | None, repo: str) -> bool:
        import base64
        import hmac

        if not token or not token.startswith(self.PREFIX) or "." not in token:
            return False
        body, _, sig = token[len(self.PREFIX):].rpartition(".")
        try:
            payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
            pass_repo, expires = payload.rsplit("|", 1)
        except Exception:
            return False
        return (hmac.compare_digest(sig, self._sig(payload)) and pass_repo == repo
                and int(expires) > time.time())
