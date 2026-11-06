"""Releases for forge repos: a tag, a title, notes and uploaded binary assets (builds, archives, installers).

A release is named by its tag (e.g. v1.2.0). The tag doesn't have to exist in git: if it does, the release
points at the commit it tags, otherwise at the commit of `ref` (default branch) when it was made. `latest`
in a URL means the newest published release (not a draft or prerelease).

Storage follows the repo, like Git LFS (fileapi.lfs), so deleting a repo deletes its releases and renaming
one keeps them:

- Postgres: one row per repo in fileapi_releases (a JSON document with every release and asset's metadata,
  updated under a row lock) and the asset bytes in fileapi_release_chunks, both tied to the repo row with
  ON DELETE CASCADE.
- Disk: `<repo>.git/releases/releases.json` and `<repo>.git/releases/blobs/<blob id>`.

Uploads can come in pieces, so a host's request-size limit (or a flaky connection) doesn't cap how big an
asset can be: `offset=0` starts (or replaces) an asset, later pieces send `offset=<bytes so far>`, and
`final=false` on every piece but the last keeps it "uploading". The last piece checks the sha256 (if the
client sent one) and publishes it. A single request with the whole file is the common case.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from .storage import Conflict, NotFound, StorageError

TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,99}$")
RESERVED_TAGS = {"latest"}
CHUNK = 4 * 1024 * 1024
MAX_RELEASES = 500
MAX_ASSETS = 100
MAX_NOTES = 100_000


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def check_tag(tag: str) -> str:
    tag = (tag or "").strip()
    if not TAG_RE.match(tag):
        raise StorageError(f"Invalid release tag {tag!r}: letters, digits and . _ + - (up to 100), e.g. v1.2.0")
    if tag.lower() in RESERVED_TAGS:
        raise StorageError(f"{tag!r} is reserved (it means the newest release in URLs)")
    return tag


def check_asset_name(name: str) -> str:
    name = (name or "").strip()
    if (not name or len(name) > 200 or name.startswith(".") or "/" in name or "\\" in name
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        raise StorageError(f"Invalid asset name {name!r}: a file name without slashes, not starting with a dot")
    return name


# --------------------------------------------------------------------------- stores


PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS fileapi_releases (
    repo_id bigint PRIMARY KEY REFERENCES fileapi_git_repos(id) ON DELETE CASCADE,
    doc     jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS fileapi_release_chunks (
    repo_id bigint NOT NULL REFERENCES fileapi_git_repos(id) ON DELETE CASCADE,
    blob    text NOT NULL,
    off     bigint NOT NULL,
    data    bytea NOT NULL,
    PRIMARY KEY (repo_id, blob, off)
);
"""


class PgReleaseStore:
    def __init__(self, pool):
        self.pool = pool

    def init(self) -> None:
        with self.pool.connection() as c:
            c.execute(PG_SCHEMA)

    def _repo_id(self, c, project: str, name: str) -> int:
        row = c.execute("SELECT id FROM fileapi_git_repos WHERE project = %s AND name = %s", (project, name)).fetchone()
        if row is None:
            raise NotFound(f"No such repo: {project}/{name}")
        return row[0]

    def load(self, project: str, name: str) -> dict:
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            row = c.execute("SELECT doc FROM fileapi_releases WHERE repo_id = %s", (rid,)).fetchone()
        return row[0] if row else {"releases": []}

    def update(self, project: str, name: str, fn: Callable[[dict], object]):
        """Run fn on the repo's releases document under a row lock and save what it leaves."""
        from psycopg.types.json import Jsonb

        with self.pool.connection() as c, c.transaction():
            rid = self._repo_id(c, project, name)
            c.execute("INSERT INTO fileapi_releases (repo_id, doc) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (rid, Jsonb({"releases": []})))
            doc = c.execute("SELECT doc FROM fileapi_releases WHERE repo_id = %s FOR UPDATE", (rid,)).fetchone()[0]
            out = fn(doc)
            c.execute("UPDATE fileapi_releases SET doc = %s WHERE repo_id = %s", (Jsonb(doc), rid))
            return out

    def blob_size(self, project: str, name: str, blob: str) -> int:
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            row = c.execute("SELECT coalesce(max(off + length(data)), 0) FROM fileapi_release_chunks "
                            "WHERE repo_id = %s AND blob = %s", (rid, blob)).fetchone()
        return int(row[0])

    def blob_append(self, project: str, name: str, blob: str, offset: int, f: BinaryIO) -> int:
        with self.pool.connection() as c, c.transaction():
            rid = self._repo_id(c, project, name)
            pos = offset
            while chunk := f.read(CHUNK):
                c.execute("INSERT INTO fileapi_release_chunks (repo_id, blob, off, data) VALUES (%s, %s, %s, %s)",
                          (rid, blob, pos, chunk))
                pos += len(chunk)
        return pos

    def blob_read(self, project: str, name: str, blob: str) -> Iterator[bytes]:
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            offs = [r[0] for r in c.execute("SELECT off FROM fileapi_release_chunks WHERE repo_id = %s AND blob = %s "
                                            "ORDER BY off", (rid, blob)).fetchall()]
        for off in offs:  # one chunk per query, so a large asset never sits in memory whole
            with self.pool.connection() as c:
                row = c.execute("SELECT data FROM fileapi_release_chunks WHERE repo_id = %s AND blob = %s AND off = %s",
                                (rid, blob, off)).fetchone()
            if row is None:
                return
            yield bytes(row[0])

    def blob_delete(self, project: str, name: str, blobs: list[str]) -> None:
        if not blobs:
            return
        with self.pool.connection() as c:
            rid = self._repo_id(c, project, name)
            c.execute("DELETE FROM fileapi_release_chunks WHERE repo_id = %s AND blob = ANY(%s)", (rid, blobs))


class DiskReleaseStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def init(self) -> None:
        pass

    def _dir(self, project: str, name: str) -> Path:
        repo = self.root / project / f"{name}.git"
        if not repo.is_dir():
            raise NotFound(f"No such repo: {project}/{name}")
        return repo / "releases"

    def load(self, project: str, name: str) -> dict:
        try:
            return json.loads((self._dir(project, name) / "releases.json").read_text())
        except FileNotFoundError:
            return {"releases": []}

    @contextmanager
    def _locked(self, d: Path):
        d.mkdir(parents=True, exist_ok=True)
        with open(d / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def update(self, project: str, name: str, fn: Callable[[dict], object]):
        d = self._dir(project, name)
        with self._locked(d):
            doc = self.load(project, name)
            out = fn(doc)
            tmp = d / f".releases.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(doc, indent=1))
            os.replace(tmp, d / "releases.json")
            return out

    def _blob(self, project: str, name: str, blob: str) -> Path:
        return self._dir(project, name) / "blobs" / blob

    def blob_size(self, project: str, name: str, blob: str) -> int:
        p = self._blob(project, name, blob)
        return p.stat().st_size if p.is_file() else 0

    def blob_append(self, project: str, name: str, blob: str, offset: int, f: BinaryIO) -> int:
        p = self._blob(project, name, blob)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "ab") as out:
            shutil.copyfileobj(f, out, CHUNK)
            return out.tell()

    def blob_read(self, project: str, name: str, blob: str) -> Iterator[bytes]:
        with open(self._blob(project, name, blob), "rb") as f:
            while chunk := f.read(CHUNK):
                yield chunk

    def blob_delete(self, project: str, name: str, blobs: list[str]) -> None:
        for b in blobs:
            self._blob(project, name, b).unlink(missing_ok=True)


# --------------------------------------------------------------------------- releases


def _find(doc: dict, tag: str) -> dict:
    for rel in doc["releases"]:
        if rel["tag"] == tag:
            return rel
    raise NotFound(f"No release {tag}")


def _asset(rel: dict, name: str) -> dict:
    for a in rel["assets"]:
        if a["name"] == name:
            return a
    raise NotFound(f"No asset {name} in release {rel['tag']}")


def _public(rel: dict) -> dict:
    out = {k: v for k, v in rel.items() if k != "assets"}
    out["assets"] = [{k: v for k, v in a.items() if k != "blob"} for a in rel["assets"]]
    return out


class Releases:
    def __init__(self, forge, store, max_asset: int):
        self.forge = forge
        self.store = store
        self.max_asset = max_asset

    def _parts(self, repo: str) -> tuple[str, str]:
        p, _, n = repo.partition("/")
        return p, n

    # -- reading --------------------------------------------------------------
    def list(self, repo: str, drafts: bool = True) -> list[dict]:
        rels = self.store.load(*self._parts(repo))["releases"]
        rels = [r for r in rels if drafts or not r.get("draft")]
        return [_public(r) for r in reversed(rels)]  # kept in the order they were made: newest last

    def resolve_tag(self, repo: str, tag: str, drafts: bool = True) -> str:
        """`latest` -> the newest published release's tag; other tags pass through."""
        if tag.lower() != "latest":
            return tag
        for rel in self.list(repo, drafts=False):
            if not rel.get("prerelease"):
                return rel["tag"]
        raise NotFound("No published release yet")

    def get(self, repo: str, tag: str, drafts: bool = True) -> dict:
        tag = self.resolve_tag(repo, tag, drafts)
        rel = _find(self.store.load(*self._parts(repo)), tag)
        if rel.get("draft") and not drafts:
            raise NotFound(f"No release {tag}")
        return _public(rel)

    # -- releases -------------------------------------------------------------
    def _target(self, repo: str, tag: str, ref: str | None) -> tuple[str | None, bool]:
        """(commit sha, whether the git tag exists). An empty repo has no commit to point at."""
        r = self.forge.open(repo)
        try:
            try:
                sha = r.refs[f"refs/tags/{tag}".encode()]
                return self.forge._peel_commit(r, sha).id.decode(), True
            except KeyError:
                pass
            try:
                sha = self.forge._resolve(r, ref or "HEAD")
            except NotFound:
                if ref:
                    raise
                return None, False
            return self.forge._peel_commit(r, sha).id.decode(), False
        finally:
            r.close()

    def create(self, repo: str, tag: str, name: str = "", notes: str = "", ref: str | None = None,
               prerelease: bool = False, draft: bool = False) -> dict:
        tag = check_tag(tag)
        if len(notes) > MAX_NOTES:
            raise StorageError(f"Release notes are limited to {MAX_NOTES} characters")
        target, tagged = self._target(repo, tag, (ref or "").strip() or None)
        now = _now()
        rel = {"tag": tag, "name": (name or "").strip() or tag, "notes": notes or "", "target": target,
               "tagged": tagged, "prerelease": bool(prerelease), "draft": bool(draft), "created": now,
               "updated": now, "assets": []}

        def add(doc):
            if any(r["tag"] == tag for r in doc["releases"]):
                raise Conflict(f"Release {tag} already exists")
            if len(doc["releases"]) >= MAX_RELEASES:
                raise StorageError(f"A repo can have at most {MAX_RELEASES} releases; delete old ones first")
            doc["releases"].append(rel)

        self.store.update(*self._parts(repo), add)
        return _public(rel)

    def update(self, repo: str, tag: str, *, name: str | None = None, notes: str | None = None,
               prerelease: bool | None = None, draft: bool | None = None, new_tag: str | None = None) -> dict:
        if notes is not None and len(notes) > MAX_NOTES:
            raise StorageError(f"Release notes are limited to {MAX_NOTES} characters")
        new_tag = check_tag(new_tag) if new_tag and new_tag != tag else None
        target = self._target(repo, new_tag, None) if new_tag else None

        def change(doc):
            rel = _find(doc, tag)
            if new_tag:
                if any(r["tag"] == new_tag for r in doc["releases"]):
                    raise Conflict(f"Release {new_tag} already exists")
                rel["tag"] = new_tag
                if target and target[1]:  # the new name is a git tag: point at what it tags
                    rel["target"], rel["tagged"] = target
            if name is not None:
                rel["name"] = name.strip() or rel["tag"]
            if notes is not None:
                rel["notes"] = notes
            if prerelease is not None:
                rel["prerelease"] = bool(prerelease)
            if draft is not None:
                rel["draft"] = bool(draft)
            rel["updated"] = _now()
            return _public(rel)

        return self.store.update(*self._parts(repo), change)

    def delete(self, repo: str, tag: str) -> None:
        def drop(doc):
            rel = _find(doc, tag)
            doc["releases"].remove(rel)
            return [a["blob"] for a in rel["assets"]]

        self.store.blob_delete(*self._parts(repo), self.store.update(*self._parts(repo), drop))

    # -- assets ----------------------------------------------------------------
    def upload(self, repo: str, tag: str, name: str, f: BinaryIO, size: int, *, offset: int = 0,
               final: bool = True, content_type: str | None = None, sha256: str | None = None) -> dict:
        """Store one piece of an asset (the whole file when offset=0 and final). See the module docs."""
        name = check_asset_name(name)
        parts = self._parts(repo)
        if offset < 0:
            raise StorageError("offset can't be negative")
        if offset + size > self.max_asset:
            raise StorageError(f"Asset larger than FILEAPI_RELEASE_MAX_ASSET ({self.max_asset} bytes)")
        if sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise StorageError("sha256 must be 64 lowercase hex digits")
        ctype = (content_type or "").split(";")[0].strip()
        if not ctype or ctype in ("application/x-www-form-urlencoded",):
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"

        if offset == 0:  # a new asset, or a fresh copy of one: its old bytes go once the new ones are in
            blob = secrets.token_hex(12)

            def start(doc):
                rel = _find(doc, tag)
                old = next((a for a in rel["assets"] if a["name"] == name), None)
                if old is None and len(rel["assets"]) >= MAX_ASSETS:
                    raise StorageError(f"A release can have at most {MAX_ASSETS} assets")
                now = _now()
                entry = {"name": name, "size": 0, "sha256": None, "content_type": ctype, "state": "uploading",
                         "blob": blob, "created": old["created"] if old else now, "updated": now, "downloads": 0}
                if old is None:
                    rel["assets"].append(entry)
                else:
                    rel["assets"][rel["assets"].index(old)] = entry
                rel["updated"] = now
                return old["blob"] if old else None

            stale = self.store.update(*parts, start)
            if stale:
                self.store.blob_delete(*parts, [stale])
        else:
            a = _asset(_find(self.store.load(*parts), tag), name)
            if a["state"] != "uploading":
                raise Conflict(f"{name} is already complete; upload again from offset 0 to replace it")
            blob = a["blob"]
        have = self.store.blob_size(*parts, blob)
        if have != offset:
            raise Conflict(f"{name} has {have} bytes so far; send the next piece with offset={have}")
        end = self.store.blob_append(*parts, blob, offset, f)

        digest = None
        if final:
            h = hashlib.sha256()
            for chunk in self.store.blob_read(*parts, blob):
                h.update(chunk)
            digest = h.hexdigest()
            if sha256 and sha256 != digest:
                self._drop_blob(repo, tag, name, blob)
                raise StorageError(f"sha256 mismatch for {name}: got {digest}; upload it again")

        def finish(doc):
            a = _asset(_find(doc, tag), name)
            if a["blob"] != blob:
                raise Conflict(f"{name} was replaced by another upload meanwhile")
            a["size"], a["updated"] = end, _now()
            if final:
                a["state"], a["sha256"] = "ready", digest
            return {k: v for k, v in a.items() if k != "blob"}

        return self.store.update(*parts, finish)

    def _drop_blob(self, repo: str, tag: str, name: str, blob: str) -> None:
        def drop(doc):
            rel = _find(doc, tag)
            rel["assets"] = [a for a in rel["assets"] if not (a["name"] == name and a["blob"] == blob)]

        self.store.update(*self._parts(repo), drop)
        self.store.blob_delete(*self._parts(repo), [blob])

    def open_asset(self, repo: str, tag: str, name: str, drafts: bool = True) -> tuple[dict, Iterator[bytes]]:
        tag = self.resolve_tag(repo, tag, drafts)
        parts = self._parts(repo)
        rel = _find(self.store.load(*parts), tag)
        if rel.get("draft") and not drafts:
            raise NotFound(f"No release {tag}")
        a = _asset(rel, name)
        if a["state"] != "ready":
            raise NotFound(f"{name} is still uploading")

        def count(doc):
            try:
                _asset(_find(doc, tag), name)["downloads"] += 1
            except NotFound:
                pass

        try:
            self.store.update(*parts, count)
        except Exception:  # noqa: BLE001 - a download counter is never worth failing a download
            pass
        return {k: v for k, v in a.items() if k != "blob"} | {"tag": tag}, self.store.blob_read(*parts, a["blob"])

    def delete_asset(self, repo: str, tag: str, name: str) -> None:
        def drop(doc):
            rel = _find(doc, tag)
            a = _asset(rel, name)
            rel["assets"].remove(a)
            rel["updated"] = _now()
            return a["blob"]

        self.store.blob_delete(*self._parts(repo), [self.store.update(*self._parts(repo), drop)])
