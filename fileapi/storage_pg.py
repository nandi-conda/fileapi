"""Postgres backend for hosts without a persistent disk (e.g. FastAPI Cloud with Neon or Supabase).

Files are rows keyed by their normalized path. Directories are implicit: a directory exists while
some file lives under it, so there are no empty directories.
"""

from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .blobs import delete_quietly, fetch
from .storage import Conflict, Edit, Entry, NotFound, StorageError, normalize

SCHEMA = """
CREATE TABLE IF NOT EXISTS fileapi_files (
    path     text PRIMARY KEY,
    content  text NOT NULL,
    size     integer NOT NULL,
    modified timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fileapi_kv (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
"""


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


class PostgresStorage:
    def __init__(self, dsn: str, pool: ConnectionPool | None = None, blobs=None):
        self.blobs = blobs  # fileapi.blobs.BlobConfig: big files go to B2 when it's set up
        # Hosted Postgres (Neon) drops idle connections when the compute suspends, so a pooled connection
        # can be dead by the time a request borrows it, which used to surface as a 500 on the first request
        # after a quiet spell. check= pings each connection on checkout and replaces a dead one; max_idle
        # retires connections before the server-side idle timeout; TCP keepalives catch silent drops.
        self.pool = pool or ConnectionPool(
            dsn, min_size=1, max_size=5, open=True, check=ConnectionPool.check_connection, max_idle=120,
            kwargs={"autocommit": True, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
                    "keepalives_count": 3})
        self.root = "postgres"

    def init(self) -> None:
        with self.pool.connection() as c:
            c.execute(SCHEMA)

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _iso(ts: datetime) -> str:
        return ts.astimezone(timezone.utc).isoformat()

    def _ready(self) -> bool:
        return bool(self.blobs and self.blobs.pg_ready)

    def _key_col(self) -> str:
        return "b2key" if self._ready() else "NULL::text"

    def _file_row(self, path: str):
        with self.pool.connection() as c:
            return c.execute(f"SELECT content, size, modified, {self._key_col()} FROM fileapi_files WHERE path = %s",
                             (path,)).fetchone()

    def _content(self, content: str, key: str | None) -> str:
        return fetch(self.blobs, key).decode("utf-8") if key else content

    def _offload(self, content: str) -> tuple[str, str | None]:
        """(what goes in the content column, B2 key or None): big files go to B2 when it's set up."""
        from .blobs import MIN_OBJECT, new_key

        store = self.blobs.active if self.blobs else None
        data = content.encode("utf-8")
        if store is None or len(data) < MIN_OBJECT:
            return content, None
        key = new_key("file")
        store.put(key, data)
        return "", key

    def _drop(self, keys) -> None:
        delete_quietly(self.blobs.store if self.blobs else None, keys)

    def _has_children(self, prefix: str) -> bool:
        if not prefix:
            return True
        with self.pool.connection() as c:
            return (
                c.execute(
                    "SELECT 1 FROM fileapi_files WHERE path LIKE %s LIMIT 1", (_like_prefix(prefix + "/"),)
                ).fetchone()
                is not None
            )

    def is_dir(self, p: str) -> bool:
        return self._has_children(normalize(p))

    def _require_file_path(self, p: str) -> str:
        path = normalize(p)
        if not path:
            raise StorageError("A file path is required")
        if self._has_children(path):
            raise StorageError(f"Is a directory: {p}")
        # A file may not sit where a parent directory needs to be (e.g. writing a/b when a is a file).
        parts = path.split("/")
        parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if parents:
            with self.pool.connection() as c:
                hit = c.execute("SELECT path FROM fileapi_files WHERE path = ANY(%s) LIMIT 1", (parents,)).fetchone()
            if hit:
                raise StorageError(f"Not a directory: {hit[0]}")
        return path

    # -- operations ----------------------------------------------------------
    def list(self, directory: str = "", recursive: bool = False) -> list[Entry]:
        base = normalize(directory)
        if base and self._file_row(base):
            raise StorageError(f"Not a directory: {directory}")
        prefix = base + "/" if base else ""
        with self.pool.connection() as c:
            rows = c.execute(
                "SELECT path, size, modified FROM fileapi_files WHERE path LIKE %s ORDER BY path",
                (_like_prefix(prefix),),
            ).fetchall()
        if base and not rows:
            raise NotFound(f"No such directory: {directory}")
        files: dict[str, Entry] = {}
        dirs: dict[str, datetime] = {}
        for path, size, modified in rows:
            rest = path[len(prefix):].split("/")
            # every intermediate directory under base
            depth = len(rest) - 1 if recursive else min(1, len(rest) - 1)
            for i in range(1, depth + 1):
                d = prefix + "/".join(rest[:i])
                dirs[d] = max(dirs.get(d, modified), modified)
            if recursive or len(rest) == 1:
                files[path] = Entry(path=path, type="file", size=size, modified=self._iso(modified))
        out = list(files.values()) + [
            Entry(path=d + "/", type="dir", size=0, modified=self._iso(m)) for d, m in dirs.items()
        ]
        return sorted(out, key=lambda e: e.path.rstrip("/").split("/"))

    def stat(self, p: str) -> Entry:
        path = normalize(p)
        row = self._file_row(path)
        if row:
            return Entry(path=path, type="file", size=row[1], modified=self._iso(row[2]))
        if self._has_children(path):
            return Entry(path=path + "/", type="dir", size=0, modified="")
        raise NotFound(f"No such file: {p}")

    def read(self, p: str) -> str:
        path = normalize(p)
        row = self._file_row(path)
        if row is None:
            if self._has_children(path):
                raise StorageError(f"Is a directory: {p}")
            raise NotFound(f"No such file: {p}")
        return self._content(row[0], row[3])

    def write(self, p: str, content: str, overwrite: bool) -> tuple[bool, int]:
        path = self._require_file_path(p)
        size = len(content.encode("utf-8"))
        stored, key = self._offload(content)
        if not self._ready():
            with self.pool.connection() as c:
                if overwrite:
                    row = c.execute(
                        """INSERT INTO fileapi_files (path, content, size) VALUES (%s, %s, %s)
                           ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, size = EXCLUDED.size,
                           modified = now() RETURNING (xmax = 0)""",
                        (path, content, size),
                    ).fetchone()
                    return (bool(row[0]), size)
                row = c.execute(
                    "INSERT INTO fileapi_files (path, content, size) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING 1",
                    (path, content, size),
                ).fetchone()
            if row is None:
                raise Conflict(f"File already exists: {p} (use write_file or edit_file to change it)")
            return (True, size)
        with self.pool.connection() as c, c.transaction():
            old = c.execute("SELECT b2key FROM fileapi_files WHERE path = %s FOR UPDATE", (path,)).fetchone()
            if old is not None and not overwrite:
                self._drop([key])
                raise Conflict(f"File already exists: {p} (use write_file or edit_file to change it)")
            c.execute(
                """INSERT INTO fileapi_files (path, content, size, b2key) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (path) DO UPDATE SET content = EXCLUDED.content, size = EXCLUDED.size,
                   b2key = EXCLUDED.b2key, modified = now()""",
                (path, stored, size, key),
            )
        if old is not None:
            self._drop([old[0]])
        return (old is None, size)

    def edit(self, p: str, edits: list[Edit]) -> int:
        path = normalize(p)
        with self.pool.connection() as c, c.transaction():
            row = c.execute(f"SELECT content, {self._key_col()} FROM fileapi_files WHERE path = %s FOR UPDATE",
                            (path,)).fetchone()
            if row is None:
                raise NotFound(f"No such file: {p}")
            content = self._content(row[0], row[1])
            for i, e in enumerate(edits, 1):
                if not e.old_text:
                    raise StorageError(f"Edit {i}: old_text must not be empty")
                count = content.count(e.old_text)
                if count == 0:
                    raise StorageError(f"Edit {i}: old_text not found in {p}")
                if count > 1:
                    raise StorageError(f"Edit {i}: old_text matches {count} times in {p}; include more context")
                content = content.replace(e.old_text, e.new_text, 1)
            if self._ready():
                stored, key = self._offload(content)
                c.execute("UPDATE fileapi_files SET content = %s, size = %s, b2key = %s, modified = now() "
                          "WHERE path = %s", (stored, len(content.encode("utf-8")), key, path))
            else:
                c.execute(
                    "UPDATE fileapi_files SET content = %s, size = %s, modified = now() WHERE path = %s",
                    (content, len(content.encode("utf-8")), path),
                )
        if self._ready() and row[1]:
            self._drop([row[1]])
        return len(edits)

    def append(self, p: str, content: str) -> int:
        path = self._require_file_path(p)
        size = len(content.encode("utf-8"))
        if self._ready():
            # A file in B2 (or one this append makes big) is rewritten whole; small ones append in SQL.
            from .blobs import MIN_OBJECT

            with self.pool.connection() as c, c.transaction():
                row = c.execute("SELECT content, size, b2key FROM fileapi_files WHERE path = %s FOR UPDATE",
                                (path,)).fetchone()
                if row is not None and (row[2] or (self.blobs.active and row[1] + size >= MIN_OBJECT)):
                    whole = self._content(row[0], row[2]) + content
                    stored, key = self._offload(whole)
                    c.execute("UPDATE fileapi_files SET content = %s, size = %s, b2key = %s, modified = now() "
                              "WHERE path = %s", (stored, row[1] + size, key, path))
                    old_key, total = row[2], row[1] + size
                else:
                    old_key = total = None
            if total is not None:
                self._drop([old_key])
                return total
            if row is None and self.blobs.active and size >= MIN_OBJECT:
                try:
                    return self.write(p, content, overwrite=False)[1]
                except Conflict:
                    return self.append(p, content)  # someone created it meanwhile
        with self.pool.connection() as c:
            row = c.execute(
                """INSERT INTO fileapi_files (path, content, size) VALUES (%s, %s, %s)
                   ON CONFLICT (path) DO UPDATE SET content = fileapi_files.content || EXCLUDED.content,
                   size = fileapi_files.size + EXCLUDED.size, modified = now() RETURNING size""",
                (path, content, size),
            ).fetchone()
        return row[0]

    def delete(self, p: str) -> None:
        path = normalize(p)
        if not path:
            raise StorageError("Refusing to delete the storage root")
        with self.pool.connection() as c:
            row = c.execute(f"DELETE FROM fileapi_files WHERE path = %s RETURNING {self._key_col()}", (path,)).fetchone()
            if row:
                self._drop([row[0]])
                return
        if self._has_children(path):
            raise Conflict(f"Directory not empty: {p}")
        raise NotFound(f"No such file: {p}")

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        a, b = normalize(src), normalize(dst)
        if not a or not b:
            raise StorageError("Cannot move the storage root")
        if b == a or b.startswith(a + "/"):
            raise StorageError("Cannot move a path into itself")
        with self.pool.connection() as c, c.transaction():
            if c.execute("SELECT 1 FROM fileapi_files WHERE path = %s", (a,)).fetchone():
                if c.execute("SELECT 1 FROM fileapi_files WHERE path = %s", (b,)).fetchone():
                    if not overwrite:
                        raise Conflict(f"Destination exists: {dst}")
                    gone = c.execute(f"DELETE FROM fileapi_files WHERE path = %s RETURNING {self._key_col()}",
                                     (b,)).fetchone()
                    if gone:
                        self._drop([gone[0]])
                elif self._has_children(b):
                    raise Conflict(f"Destination exists: {dst}")
                c.execute("UPDATE fileapi_files SET path = %s, modified = now() WHERE path = %s", (b, a))
                return
            moved = c.execute(
                "SELECT path FROM fileapi_files WHERE path LIKE %s", (_like_prefix(a + "/"),)
            ).fetchall()
            if not moved:
                raise NotFound(f"No such file: {src}")
            targets = [b + p[len(a):] for (p,) in moved]
            clash = c.execute("SELECT path FROM fileapi_files WHERE path = ANY(%s) LIMIT 1", (targets,)).fetchone()
            if clash or self._has_children(b) or c.execute(
                "SELECT 1 FROM fileapi_files WHERE path = %s", (b,)
            ).fetchone():
                raise Conflict(f"Destination exists: {dst}")
            c.execute(
                "UPDATE fileapi_files SET path = %s || substr(path, %s), modified = now() WHERE path LIKE %s",
                (b, len(a) + 1, _like_prefix(a + "/")),
            )


class PostgresKV:
    """Key/value records for server state (OAuth records, forge secret)."""

    def __init__(self, storage: PostgresStorage):
        self.pool = storage.pool

    def get(self, key: str) -> dict | None:
        with self.pool.connection() as c:
            row = c.execute("SELECT value FROM fileapi_kv WHERE key = %s", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: dict) -> None:
        with self.pool.connection() as c:
            c.execute(
                "INSERT INTO fileapi_kv VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, Jsonb(value)),
            )

    def create(self, key: str, value: dict) -> bool:
        with self.pool.connection() as c:
            row = c.execute(
                "INSERT INTO fileapi_kv VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING 1", (key, Jsonb(value))
            ).fetchone()
        return row is not None

    def pop(self, key: str) -> dict | None:
        with self.pool.connection() as c:
            row = c.execute("DELETE FROM fileapi_kv WHERE key = %s RETURNING value", (key,)).fetchone()
        return row[0] if row else None


__all__ = ["PostgresStorage", "PostgresKV"]
