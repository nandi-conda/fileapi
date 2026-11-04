"""Small key/value store for server state: OAuth clients, codes and tokens (fileapi.auth) and the forge
secret. FileKV keeps one JSON file per key on disk; storage_pg.PostgresKV keeps them in Postgres."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Protocol


class KV(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...
    def create(self, key: str, value: dict) -> bool:
        """Insert only if absent; True if this call created it."""
        ...
    def pop(self, key: str) -> dict | None:
        """Delete and return the record, atomically: of two racing callers only one gets it."""
        ...


class FileKV:
    """One JSON file per key in a local directory."""

    def __init__(self, directory: str | os.PathLike):
        self.dir = Path(directory)

    def _path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def get(self, key: str) -> dict | None:
        try:
            return json.loads(self._path(key).read_text())
        except FileNotFoundError:
            return None

    def put(self, key: str, value: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{key}.{secrets.token_hex(4)}.tmp"
        tmp.write_text(json.dumps(value))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path(key))

    def create(self, key: str, value: dict) -> bool:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path(key), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
        return True

    def pop(self, key: str) -> dict | None:
        claimed = self.dir / f"{key}.{secrets.token_hex(4)}.popped"
        try:
            os.rename(self._path(key), claimed)  # atomic: only one caller wins the rename
        except FileNotFoundError:
            return None
        try:
            return json.loads(claimed.read_text())
        finally:
            claimed.unlink(missing_ok=True)
