"""Git LFS storage for forge repos (the server side of the Git LFS batch API).

git-lfs finds the server from the remote URL: `https://<host>/git/<owner>/<repo>.git/info/lfs`. Objects
are content-addressed by sha256 and kept per repo, so deleting a repo deletes its LFS objects and renaming
one keeps them:

- Postgres: rows in fileapi_lfs_objects (oid, size) and fileapi_lfs_chunks (4 MiB pieces), tied to the
  repo row with ON DELETE CASCADE. Chunks keep each row small and let downloads stream.
- Disk: `<repo>.git/lfs/objects/<aa>/<bb>/<oid>`, the same layout git-lfs uses locally.

Uploads are spooled to a temp file and hashed first; an object is only stored once its sha256 and size
match what the client claimed.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .blobs import delete_quietly, fetch, new_key
from .storage import NotFound, StorageError

OID_RE = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 4 * 1024 * 1024
POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
POINTER_RE = re.compile(rb"^version https://git-lfs\.github\.com/spec/v1\n(?:.*\n)*?oid sha256:([0-9a-f]{64})\nsize (\d+)\n")


def check_oid(oid: str) -> str:
    if not OID_RE.match(oid or ""):
        raise StorageError(f"Invalid LFS object id {oid!r}: expected 64 lowercase hex digits (sha256)")
    return oid


def parse_pointer(data: bytes) -> tuple[str, int] | None:
    """(oid, size) if `data` is a Git LFS pointer file, else None."""
    if len(data) > 1024 or not data.startswith(POINTER_PREFIX):
        return None
    m = POINTER_RE.match(data)
    return (m.group(1).decode(), int(m.group(2))) if m else None


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS fileapi_lfs_objects (
    repo_id bigint NOT NULL REFERENCES fileapi_git_repos(id) ON DELETE CASCADE,
    oid     text NOT NULL,
    size    bigint NOT NULL,
    created timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (repo_id, oid)
);
CREATE TABLE IF NOT EXISTS fileapi_lfs_chunks (
    repo_id bigint NOT NULL,
    oid     text NOT NULL,
    seq     integer NOT NULL,
    data    bytea NOT NULL,
    PRIMARY KEY (repo_id, oid, seq),
    FOREIGN KEY (repo_id, oid) REFERENCES fileapi_lfs_objects(repo_id, oid) ON DELETE CASCADE
);
"""


class PgLfsStore:
    def __init__(self, pool, blobs=None):
        self.pool = pool
        self.blobs = blobs  # fileapi.blobs.BlobConfig: chunks go to B2 when it's set up

    def init(self) -> None:
        with self.pool.connection() as c:
            c.execute(PG_SCHEMA)

    def _repo_id(self, c, project: str, name: str) -> int:
        row = c.execute("SELECT id FROM fileapi_git_repos WHERE project = %s AND name = %s", (project, name)).fetchone()
        if row is None:
            raise NotFound(f"No such repo: {project}/{name}")
        return row[0]

    def size(self, project: str, name: str, oid: str) -> int | None:
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            row = c.execute("SELECT size FROM fileapi_lfs_objects WHERE repo_id = %s AND oid = %s", (rid, oid)).fetchone()
        return None if row is None else int(row[0])

    def put(self, project: str, name: str, oid: str, size: int, f: BinaryIO) -> None:
        with self.pool.connection() as c, c.transaction():
            rid = self._repo_id(c, project, name)
            cur = c.execute("INSERT INTO fileapi_lfs_objects (repo_id, oid, size) VALUES (%s, %s, %s) "
                            "ON CONFLICT DO NOTHING", (rid, oid, size))
            if cur.rowcount == 0:
                return  # already stored (content-addressed, so it is the same bytes)
            store = self.blobs.active if self.blobs else None
            seq, uploaded = 0, []
            try:
                while chunk := f.read(CHUNK):
                    if store is not None:
                        key = new_key("lfs")
                        store.put(key, chunk)
                        uploaded.append(key)
                        c.execute("INSERT INTO fileapi_lfs_chunks (repo_id, oid, seq, data, b2key, size) "
                                  "VALUES (%s, %s, %s, NULL, %s, %s)", (rid, oid, seq, key, len(chunk)))
                    else:
                        c.execute("INSERT INTO fileapi_lfs_chunks (repo_id, oid, seq, data) VALUES (%s, %s, %s, %s)",
                                  (rid, oid, seq, chunk))
                    seq += 1
            except BaseException:
                delete_quietly(store, uploaded)
                raise

    def read(self, project: str, name: str, oid: str) -> Iterator[bytes]:
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            n = c.execute("SELECT count(*) FROM fileapi_lfs_chunks WHERE repo_id = %s AND oid = %s", (rid, oid)).fetchone()[0]
        for seq in range(n):  # one chunk per query, so a large object never sits in memory whole
            with self.pool.connection() as c:
                row = c.execute("SELECT data" + (", b2key" if self.blobs and self.blobs.pg_ready else ", NULL")
                                + " FROM fileapi_lfs_chunks WHERE repo_id = %s AND oid = %s AND seq = %s",
                                (rid, oid, seq)).fetchone()
            if row is None:
                return
            yield bytes(row[0]) if row[0] is not None else fetch(self.blobs, row[1])


class DiskLfsStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def init(self) -> None:
        pass

    def _repo(self, project: str, name: str) -> Path:
        path = self.root / project / f"{name}.git"
        if not path.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        return path

    def path(self, project: str, name: str, oid: str) -> Path:
        return self._repo(project, name) / "lfs" / "objects" / oid[:2] / oid[2:4] / oid

    def size(self, project: str, name: str, oid: str) -> int | None:
        p = self.path(project, name, oid)
        return p.stat().st_size if p.is_file() else None

    def put(self, project: str, name: str, oid: str, size: int, f: BinaryIO) -> None:
        dest = self.path(project, name, oid)
        if dest.is_file():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{oid}.{os.getpid()}.tmp")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(f, out, CHUNK)
        os.replace(tmp, dest)

    def read(self, project: str, name: str, oid: str) -> Iterator[bytes]:
        with open(self.path(project, name, oid), "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk


class Spool:
    """Collects an upload in a temp file while hashing it."""

    def __init__(self, limit: int):
        self.limit = limit
        self.file = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, prefix="lfs-")
        self.sha = hashlib.sha256()
        self.size = 0

    def write(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.limit:
            raise StorageError(f"LFS object larger than FILEAPI_LFS_MAX_OBJECT ({self.limit} bytes)")
        self.sha.update(chunk)
        self.file.write(chunk)

    def close(self) -> None:
        self.file.close()
