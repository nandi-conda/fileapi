"""Blob storage outside the database: Backblaze B2 (native API, over httpx), or memory for tests.

Postgres keeps metadata, refs and small objects; the bulky bytes (large git blobs, Git LFS objects, release
assets, big stored files) go to a bucket. Each stored blob gets a fresh random name (`<kind>/<32 hex>`), kept
in its database row, so nobody can guess a name even if the bucket is made public. The app reads blobs with
the account's key and serves them itself: the bucket should be private.

Configuration: env vars, else the forge owner's user secrets, else the deploy's build config (fileapi/B2_CONFIG,
written by CI from .forge/jobs.toml; it holds only the key ID and bucket):

- B2_KEY_ID: the application key's ID (not secret).
- B2_APPLICATION_KEY (or B2_TOKEN): the application key itself. Keep it a user secret.
  B2_TOKEN may also be "<keyID>:<applicationKey>".
- B2_BUCKET: bucket name, default "fileapi".
- FILEAPI_BLOBS=memory keeps blobs in process memory (tests only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import secrets
import threading
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

from .storage import StorageError

log = logging.getLogger("fileapi.blobs")

AUTH_URL = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"
DEFAULT_BUCKET = "fileapi"
# Git blobs at least this big (compressed) and stored files at least this big go to the bucket; smaller ones
# stay in Postgres, where reading many of them (a clone, a tree listing) is one query instead of many requests.
MIN_OBJECT = int(os.environ.get("FILEAPI_BLOB_MIN_BYTES", 32 * 1024))


class BlobError(StorageError):
    pass


def new_key(kind: str) -> str:
    return f"{kind}/{secrets.token_hex(16)}"


class MemoryBlobs:
    """In-process blobs, for tests."""

    name = "memory"

    def __init__(self):
        self.data: dict[str, tuple[bytes, float]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: bytes) -> None:
        with self._lock:
            self.data[key] = (bytes(data), time.time())

    def get(self, key: str) -> bytes:
        try:
            return self.data[key][0]
        except KeyError:
            raise BlobError(f"Blob not found: {key}") from None

    def size(self, key: str) -> int | None:
        hit = self.data.get(key)
        return None if hit is None else len(hit[0])

    def delete(self, keys: Iterable[str]) -> None:
        with self._lock:
            for k in keys:
                self.data.pop(k, None)

    def list(self, prefix: str = "") -> Iterator[tuple[str, int, float]]:
        for k, (v, ts) in sorted(self.data.items()):
            if k.startswith(prefix):
                yield k, len(v), ts


class B2Blobs:
    """Backblaze B2 through its native API. Thread-safe; upload URLs are pooled (B2 wants one per uploader)."""

    name = "b2"

    def __init__(self, key_id: str, app_key: str, bucket: str = DEFAULT_BUCKET, client: httpx.Client | None = None):
        self.key_id, self.app_key, self.bucket = key_id, app_key, bucket
        self.http = client or httpx.Client(timeout=httpx.Timeout(120, connect=15))
        self._lock = threading.Lock()
        self._auth: dict | None = None
        self._bucket_id: str | None = None
        self._uploads: queue.SimpleQueue = queue.SimpleQueue()

    # -- account ------------------------------------------------------------------
    def _authorize(self, stale: dict | None = None) -> dict:
        with self._lock:
            if self._auth is not None and self._auth is not stale:
                return self._auth
            r = self.http.get(AUTH_URL, auth=(self.key_id, self.app_key))
            if r.status_code != 200:
                raise BlobError(f"B2 rejected the key (B2_KEY_ID / B2_APPLICATION_KEY): {r.status_code} {r.text[:200]}")
            auth = r.json()
            allowed = auth.get("allowed") or {}
            if allowed.get("bucketName") and allowed["bucketName"] != self.bucket:
                raise BlobError(f"This B2 key only allows bucket {allowed['bucketName']!r}, not {self.bucket!r}")
            if allowed.get("bucketId"):
                self._bucket_id = allowed["bucketId"]
            self._auth = auth
            self._uploads = queue.SimpleQueue()  # upload URLs belong to the old token's session
        if self._bucket_id is None:
            out = self._api("b2_list_buckets", {"accountId": auth["accountId"], "bucketName": self.bucket})
            buckets = out.get("buckets") or []
            if not buckets:
                raise BlobError(f"No B2 bucket named {self.bucket!r} for this key")
            self._bucket_id = buckets[0]["bucketId"]
        return auth

    def _api(self, op: str, body: dict) -> dict:
        auth = self._auth or self._authorize()
        for attempt in range(4):
            try:
                r = self.http.post(f"{auth['apiUrl']}/b2api/v2/{op}", json=body,
                                   headers={"Authorization": auth["authorizationToken"]})
            except httpx.TransportError as e:
                if attempt == 3:
                    raise BlobError(f"B2 {op}: {e}") from e
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code == 401:
                auth = self._authorize(stale=auth)
                continue
            if r.status_code in (429, 500, 503) and attempt < 3:
                time.sleep(float(r.headers.get("Retry-After") or 0.5 * 2 ** attempt))
                continue
            if r.status_code != 200:
                raise BlobError(f"B2 {op}: {r.status_code} {r.text[:300]}")
            return r.json()
        raise BlobError(f"B2 {op}: gave up")

    @property
    def bucket_id(self) -> str:
        if self._bucket_id is None:
            self._authorize()
        return self._bucket_id  # type: ignore[return-value]

    def check(self) -> dict:
        """Sign in and find the bucket (raises BlobError with a readable reason)."""
        auth = self._authorize()
        return {"bucket": self.bucket, "bucket_id": self.bucket_id, "account": auth.get("accountId")}

    # -- blobs --------------------------------------------------------------------
    def put(self, key: str, data: bytes) -> None:
        sha1 = hashlib.sha1(data).hexdigest()
        for attempt in range(5):
            try:
                up = self._uploads.get_nowait()
            except queue.Empty:
                up = self._api("b2_get_upload_url", {"bucketId": self.bucket_id})
            try:
                r = self.http.post(up["uploadUrl"], content=data, headers={
                    "Authorization": up["authorizationToken"], "X-Bz-File-Name": key,
                    "Content-Type": "application/octet-stream", "X-Bz-Content-Sha1": sha1,
                    "Content-Length": str(len(data))})
            except httpx.TransportError as e:
                if attempt == 4:
                    raise BlobError(f"B2 upload of {key}: {e}") from e
                time.sleep(0.5 * 2 ** attempt)
                continue  # this upload URL may be bad: drop it
            if r.status_code == 200:
                self._uploads.put(up)
                return
            if r.status_code in (401, 408, 429, 500, 503) and attempt < 4:
                if r.status_code == 401:
                    self._authorize(stale=self._auth)
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise BlobError(f"B2 upload of {key}: {r.status_code} {r.text[:300]}")
        raise BlobError(f"B2 upload of {key}: gave up")

    def _download(self, method: str, key: str) -> httpx.Response:
        auth = self._auth or self._authorize()
        for attempt in range(4):
            try:
                r = self.http.request(method, f"{auth['downloadUrl']}/file/{self.bucket}/{key}",
                                      headers={"Authorization": auth["authorizationToken"]})
            except httpx.TransportError as e:
                if attempt == 3:
                    raise BlobError(f"B2 download of {key}: {e}") from e
                time.sleep(0.5 * 2 ** attempt)
                continue
            if r.status_code == 401:
                auth = self._authorize(stale=auth)
                continue
            if r.status_code in (429, 500, 503) and attempt < 3:
                time.sleep(0.5 * 2 ** attempt)
                continue
            return r
        raise BlobError(f"B2 download of {key}: gave up")

    def get(self, key: str) -> bytes:
        r = self._download("GET", key)
        if r.status_code != 200:
            raise BlobError(f"B2 download of {key}: {r.status_code} {r.text[:200]}")
        return r.content

    def size(self, key: str) -> int | None:
        r = self._download("HEAD", key)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise BlobError(f"B2 HEAD {key}: {r.status_code}")
        return int(r.headers.get("Content-Length", -1))

    def delete(self, keys: Iterable[str]) -> None:
        for key in keys:
            out = self._api("b2_list_file_versions",
                            {"bucketId": self.bucket_id, "startFileName": key, "prefix": key, "maxFileCount": 100})
            for f in out.get("files", []):
                if f["fileName"] == key:
                    self._api("b2_delete_file_version", {"fileName": key, "fileId": f["fileId"]})

    def list(self, prefix: str = "") -> Iterator[tuple[str, int, float]]:
        start = None
        while True:
            body = {"bucketId": self.bucket_id, "prefix": prefix, "maxFileCount": 1000}
            if start:
                body["startFileName"] = start
            out = self._api("b2_list_file_names", body)
            for f in out.get("files", []):
                if f.get("action", "upload") == "upload":
                    yield f["fileName"], int(f.get("contentLength", 0)), f.get("uploadTimestamp", 0) / 1000
            start = out.get("nextFileName")
            if not start:
                return


def delete_quietly(blobs, keys: Iterable[str | None]) -> None:
    """Delete blobs whose rows are gone; a failure only leaves an orphan for `fileapi blobs sweep`."""
    keys = [k for k in keys if k]
    if not keys or blobs is None:
        return
    try:
        blobs.delete(keys)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not delete %d blob(s) from %s (sweep will): %s", len(keys), blobs.name, e)


BUILD_CONFIG = Path(__file__).with_name("B2_CONFIG")


def build_config() -> dict:
    """{"B2_KEY_ID", "B2_BUCKET"} written into the deploy by CI (ci/deploy.py, from .forge/jobs.toml)."""
    try:
        doc = json.loads(BUILD_CONFIG.read_text())
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in doc.items() if k in ("B2_KEY_ID", "B2_BUCKET") and v}


def from_config(env=os.environ, secrets_: dict | None = None, build: dict | None = None):
    """The configured blob store, or None (everything stays in Postgres). Values come from env vars, then the
    owner's user secrets, then the deploy's build config (key ID and bucket only: never the secret key)."""
    if (env.get("FILEAPI_BLOBS") or "").strip().lower() == "memory":
        return MemoryBlobs()
    src = {**(build if build is not None else build_config()), **(secrets_ or {}),
           **{k: v for k, v in env.items() if k.startswith("B2_") and v}}
    key_id, app_key = (src.get("B2_KEY_ID") or "").strip(), (src.get("B2_APPLICATION_KEY") or "").strip()
    token = (src.get("B2_TOKEN") or "").strip()
    if not app_key and token:
        if ":" in token:  # "<keyID>:<applicationKey>"
            key_id, _, app_key = token.partition(":")
        else:  # just the application key; the key ID comes from elsewhere
            app_key = token
    if not (key_id and app_key):
        if token or key_id or app_key:
            log.warning("B2 is only half configured: it needs a key ID (B2_KEY_ID) and its application key "
                        "(B2_APPLICATION_KEY or B2_TOKEN)")
        return None
    return B2Blobs(key_id, app_key, (src.get("B2_BUCKET") or DEFAULT_BUCKET).strip())


class BlobConfig:
    """Which blob store is in use, if any. Resolved lazily (and re-checked every minute until found), so B2 keys
    saved as user secrets after startup are picked up without a redeploy. Blobs are only written once the
    database has the columns that record them (see prepare_pg)."""

    RECHECK = 60.0

    def __init__(self, resolve=None, store=None):
        self._resolve = resolve or (lambda: from_config())
        self._store = store
        self._checked = 0.0 if store is None else float("inf")
        self._lock = threading.Lock()
        self.pg_ready = True  # set by prepare_pg on the Postgres backend
        self.error: str | None = None

    @property
    def store(self):
        """The configured store (for reads), whether or not new writes may use it."""
        if self._store is None and time.monotonic() - self._checked > self.RECHECK:
            with self._lock:
                if self._store is None and time.monotonic() - self._checked > self.RECHECK:
                    self._checked = time.monotonic()
                    try:
                        self._store = self._resolve()
                        self.error = None
                    except Exception as e:  # noqa: BLE001 - e.g. secrets unreadable; keep serving from Postgres
                        self.error = str(e)
                        log.warning("Blob storage config: %s", e)
        return self._store

    @property
    def active(self):
        """The store new blobs go to, or None (they stay in Postgres)."""
        return self.store if self.pg_ready else None

    def need(self):
        s = self.store
        if s is None:
            raise BlobError("This blob is stored in B2, but B2 isn't configured: save B2_KEY_ID and "
                            "B2_APPLICATION_KEY as user secrets (or server env vars)")
        return s


def fetch(cfg: "BlobConfig | None", key: str) -> bytes:
    if cfg is None:
        raise BlobError(f"Blob {key} is in B2, but B2 isn't configured")
    return cfg.need().get(key)


# Columns that let a row point at a blob instead of holding its bytes. Added at startup, tolerantly: a database
# at its size limit can refuse even these, and then everything keeps working from Postgres alone.
PG_COLUMNS = {
    "fileapi_git_objects": "data",
    "fileapi_lfs_chunks": "data",
    "fileapi_release_chunks": "data",
    "fileapi_files": None,  # content stays NOT NULL (''), the size column already exists
}


def prepare_pg(pool, cfg: BlobConfig) -> bool:
    with pool.connection() as c:
        for table, data_col in PG_COLUMNS.items():
            stmts = [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS b2key text"]
            if data_col:
                stmts += [f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS size bigint",
                          f"ALTER TABLE {table} ALTER COLUMN {data_col} DROP NOT NULL"]
            for sql in stmts:
                try:
                    c.execute(sql)
                except Exception as e:  # noqa: BLE001
                    log.warning("Blob storage schema change failed (%s): %s", sql, e)
        have = {(t, col) for t, col in c.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_name = ANY(%s) "
            "AND column_name IN ('b2key', 'size')", (list(PG_COLUMNS),)).fetchall()}
    cfg.pg_ready = all((t, "b2key") in have and (not d or (t, "size") in have) for t, d in PG_COLUMNS.items())
    if not cfg.pg_ready:
        log.warning("Blob storage is off until the database accepts the new columns (is it full?)")
    return cfg.pg_ready
