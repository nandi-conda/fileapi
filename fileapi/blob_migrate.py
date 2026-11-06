"""Moving bytes that are already in Postgres out to the blob store (B2), in two steps:

1. copy: upload each big row's bytes to B2 and record the key in the row. The bytes stay in Postgres, so this
   is safe to run (and re-run) at any time; reads keep using the Postgres copy while it exists. Only git blobs
   some branch or tag can reach are copied: unreachable ones are gc_repo's job, not worth uploading.
2. free: for rows whose copy is in B2 (checked: it exists with the right size), drop the Postgres bytes, then
   VACUUM so the space can be reused. This is the step that deletes data from the database.

sweep deletes blobs in the bucket that no row points at any more (failed uploads, deletes that didn't reach B2).
"""

from __future__ import annotations

import logging
import threading
import time

from . import blobs as _blobs
from .blobs import delete_quietly, new_key

log = logging.getLogger("fileapi.blobs")

# (table, primary key columns, bytes column, B2 key prefix)
TABLES = [
    ("fileapi_git_objects", ("repo_id", "sha"), "data", "git"),
    ("fileapi_lfs_chunks", ("repo_id", "oid", "seq"), "data", "lfs"),
    ("fileapi_release_chunks", ("repo_id", "blob", "off"), "data", "release"),
    ("fileapi_files", ("path",), "content", "file"),
]


class Migrator:
    def __init__(self, pool, cfg, forge_store=None):
        self.pool, self.cfg, self.forge_store = pool, cfg, forge_store
        self.status: dict = {"state": "idle"}
        self._lock = threading.Lock()

    def _store(self):
        store = self.cfg.active
        if store is None:
            raise RuntimeError("B2 isn't set up (or the database lacks the blob columns): nothing to do")
        return store

    def _candidates(self, c, table: str, pk: tuple, col: str) -> list[tuple]:
        """Primary keys of rows whose bytes are still only in Postgres and big enough to move."""
        where = f"b2key IS NULL AND {col} IS NOT NULL"
        if table == "fileapi_git_objects":
            where += f" AND type = 3 AND length({col}) >= {_blobs.MIN_OBJECT}"
        elif table == "fileapi_files":
            where += f" AND size >= {_blobs.MIN_OBJECT}"
        return [tuple(r) for r in c.execute(f"SELECT {', '.join(pk)} FROM {table} WHERE {where}").fetchall()]

    def _reachable(self, c) -> dict[int, set[bytes]] | None:
        if self.forge_store is None or not hasattr(self.forge_store, "_reachable"):
            return None
        out = {}
        for (rid,) in c.execute("SELECT id FROM fileapi_git_repos").fetchall():
            objects, seen = self.forge_store._reachable(c, rid)
            # Keep young objects too: a push in flight hasn't pointed a ref at them yet.
            out[rid] = {bytes.fromhex(s.decode()) for s in objects if s in seen or not objects[s][2]}
        return out

    def copy(self, reachable_only: bool = True) -> dict:
        store = self._store()
        report = {}
        with self.pool.connection() as c:
            keep = self._reachable(c) if reachable_only else None
        for table, pk, col, kind in TABLES:
            if table == "fileapi_git_objects" and not _blobs.GIT_TO_BLOBS:
                continue  # git objects stay in Postgres (see blobs.GIT_TO_BLOBS)
            with self.pool.connection() as c:
                rows = self._candidates(c, table, pk, col)
            skipped = 0
            if table == "fileapi_git_objects" and keep is not None:
                before = len(rows)
                rows = [r for r in rows if bytes(r[1]) in keep.get(r[0], ())]
                skipped = before - len(rows)
            done = nbytes = 0
            cond = " AND ".join(f"{k} = %s" for k in pk)
            for i, row in enumerate(rows):
                self.status.update(table=table, row=i, rows=len(rows))
                with self.pool.connection() as c:
                    got = c.execute(f"SELECT {col} FROM {table} WHERE {cond} AND b2key IS NULL", row).fetchone()
                if got is None or got[0] is None:
                    continue
                data = got[0].encode("utf-8") if isinstance(got[0], str) else bytes(got[0])
                key = new_key(kind)
                store.put(key, data)
                size_set = "" if table == "fileapi_files" else ", size = %s"
                args = (key,) + (() if table == "fileapi_files" else (len(data),)) + row
                with self.pool.connection() as c:
                    cur = c.execute(f"UPDATE {table} SET b2key = %s{size_set} WHERE {cond} AND b2key IS NULL", args)
                if cur.rowcount != 1:
                    delete_quietly(store, [key])
                    continue
                done += 1
                nbytes += len(data)
            report[table] = {"copied": done, "bytes": nbytes, "skipped_unreachable": skipped}
            log.info("blob copy %s: %s", table, report[table])
        return report

    def free(self, vacuum: bool = True) -> dict:
        """Drop the Postgres copy of every row whose bytes are verified in B2. Deletes data from the database."""
        store = self._store()
        report = {}
        for table, pk, col, _ in TABLES:
            if table == "fileapi_git_objects" and not _blobs.GIT_TO_BLOBS:
                continue  # git objects stay in Postgres
            cond = " AND ".join(f"{k} = %s" for k in pk)
            with self.pool.connection() as c:
                rows = c.execute(
                    f"SELECT {', '.join(pk)}, b2key, " + (f"octet_length({col})" if col == "content" else f"length({col})")
                    + f" FROM {table} WHERE b2key IS NOT NULL AND {col} IS NOT NULL"
                    + (" AND content <> ''" if col == "content" else "")).fetchall()
            freed = nbytes = missing = 0
            for i, r in enumerate(rows):
                self.status.update(table=table, row=i, rows=len(rows))
                *key_cols, b2key, size = r
                if store.size(b2key) != size:
                    missing += 1  # not (fully) in B2: keep the Postgres copy
                    continue
                with self.pool.connection() as c:
                    c.execute(f"UPDATE {table} SET {col} = " + ("''" if col == "content" else "NULL")
                              + f" WHERE {cond} AND b2key = %s", (*key_cols, b2key))
                freed += 1
                nbytes += size
            report[table] = {"freed": freed, "bytes": nbytes, "not_in_b2": missing}
            log.info("blob free %s: %s", table, report[table])
        if vacuum:
            report["vacuum"] = self.vacuum()
        return report

    def vacuum(self) -> dict:
        out = {}
        with self.pool.connection() as c:
            before = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            for table, *_ in TABLES:
                try:
                    c.execute(f"VACUUM {table}")
                    out[table] = "vacuumed"
                except Exception as e:  # noqa: BLE001
                    out[table] = f"VACUUM failed: {e}"
            after = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        out["database_bytes_before"], out["database_bytes_after"] = before, after
        return out

    def sweep(self, apply: bool = False, grace: int = 3600) -> dict:
        """Find (apply=True: delete) blobs in the bucket no row points at, older than `grace` seconds."""
        store = self._store()
        known: set[str] = set()
        with self.pool.connection() as c:
            for table, *_ in TABLES:
                known |= {k for (k,) in c.execute(f"SELECT b2key FROM {table} WHERE b2key IS NOT NULL")}
        cutoff = time.time() - grace
        orphans = [(k, size) for k, size, ts in store.list() if k not in known and ts < cutoff
                   and k.split("/", 1)[0] in {t[3] for t in TABLES}]
        if apply:
            store.delete(k for k, _ in orphans)
        return {"orphans": len(orphans), "bytes": sum(s for _, s in orphans), "deleted": len(orphans) if apply else 0}

    def counts(self) -> dict:
        """Per table: rows/bytes only in Postgres, in both, only in B2."""
        out = {}
        with self.pool.connection() as c:
            for table, _, col, _ in TABLES:
                length = f"octet_length({col})" if col == "content" else f"length({col})"
                empty = f"({col} IS NULL" + (f" OR {col} = '')" if col == "content" else ")")
                size = "size"
                row = c.execute(
                    f"SELECT count(*) FILTER (WHERE b2key IS NULL), coalesce(sum({length}) FILTER (WHERE b2key IS NULL), 0), "
                    f"count(*) FILTER (WHERE b2key IS NOT NULL AND NOT {empty}), "
                    f"coalesce(sum({length}) FILTER (WHERE b2key IS NOT NULL AND NOT {empty}), 0), "
                    f"count(*) FILTER (WHERE b2key IS NOT NULL AND {empty}), "
                    f"coalesce(sum({size}) FILTER (WHERE b2key IS NOT NULL AND {empty}), 0) FROM {table}").fetchone()
                out[table] = {"postgres_only": [row[0], int(row[1])], "both": [row[2], int(row[3])],
                              "b2_only": [row[4], int(row[5])]}
        return out

    def restore_git(self) -> dict:
        """Bring git objects that an earlier version moved to B2 back into Postgres (they're read in bulk by
        clones and pushes). The B2 copy stays until a sweep, so this is safe to stop and re-run."""
        if _blobs.GIT_TO_BLOBS:
            return {"skipped": "FILEAPI_BLOB_GIT is on"}
        store = self.cfg.store
        if store is None:
            raise RuntimeError("B2 isn't configured, so objects stored there can't be read")
        with self.pool.connection() as c:
            rows = c.execute("SELECT repo_id, sha, b2key FROM fileapi_git_objects "
                             "WHERE data IS NULL AND b2key IS NOT NULL").fetchall()
        done = nbytes = 0
        for i, (rid, sha, key) in enumerate(rows):
            self.status.update(table="fileapi_git_objects", row=i, rows=len(rows))
            data = store.get(key)
            with self.pool.connection() as c:
                c.execute("UPDATE fileapi_git_objects SET data = %s WHERE repo_id = %s AND sha = %s AND data IS NULL",
                          (data, rid, sha))
            done += 1
            nbytes += len(data)
        return {"restored": done, "bytes": nbytes, "left": len(rows) - done}

    def _retry_restore(self, delay: float = 1800, tries: int = 48) -> None:
        def again():
            try:
                log.info("git restore retry: %s", self.restore_git())
            except Exception as e:  # noqa: BLE001
                log.warning("Restoring git objects from B2 failed again: %s", e)
                if tries > 1:
                    self._retry_restore(delay, tries - 1)

        t = threading.Timer(delay, again)
        t.daemon = True
        t.start()

    def startup(self, kv=None, token: str = "") -> dict:
        """What runs after each start: put git objects back in Postgres, copy the rest, free once if CI asked."""
        try:
            out = {"restore_git": self.restore_git()}
        except Exception as e:  # noqa: BLE001 - e.g. B2's daily download cap: retry later, carry on now
            log.warning("Restoring git objects from B2 failed (retrying in 30 min): %s", e)
            out = {"restore_git": {"error": str(e)}}
            self._retry_restore()
        if token and kv is not None:
            out.update(self.copy_then_free(kv, token))
        else:
            out["copy"] = self.copy()
        return out

    def copy_then_free(self, kv=None, token: str = "") -> dict:
        """Copy, then free (one-shot, asked for by CI: B2_FREE in .forge/jobs.toml). The result goes in KV."""
        try:
            out = {"copy": self.copy()}
            out["free"] = self.free()
        except Exception as e:
            if kv is not None and token:
                kv.put(f"blob_free_{token}", {"state": "failed", "error": str(e)})
            raise
        if kv is not None and token:
            freed = sum(v.get("bytes", 0) for k, v in out["free"].items() if k != "vacuum")
            kv.put(f"blob_free_{token}", {"state": "done", "freed_bytes": freed, "result": out,
                                          "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return out

    def run_background(self, what: str = "copy", **kw) -> bool:
        """Run copy/free/sweep in a thread (one at a time). False if one is already running."""
        if not self._lock.acquire(blocking=False):
            return False

        def go():
            try:
                self.status = {"state": "running", "step": what, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                result = getattr(self, what)(**kw)
                self.status = {"state": "done", "step": what, "result": result,
                               "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            except Exception as e:  # noqa: BLE001
                log.exception("blob %s failed", what)
                self.status = {"state": "failed", "step": what, "error": str(e)}
            finally:
                self._lock.release()

        threading.Thread(target=go, name=f"blobs-{what}", daemon=True).start()
        return True
