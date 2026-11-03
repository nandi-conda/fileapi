"""Trust-on-first-use access token.

The server starts unclaimed. The first MCP client calls the `claim_server` tool, which mints a random
token, stores only its SHA-256 hash, and returns the token once. From then on every request must
present that token. To reset, delete the stored record (or set FILEAPI_TOKEN to pin a token yourself).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

KEY = "access_token"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class KV(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...
    def create(self, key: str, value: dict) -> bool:
        """Insert only if absent; True if this call created it."""
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
        tmp = self._path(key).with_suffix(".tmp")
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


class TokenStore:
    ROTATE_GRACE = 60  # seconds the old token keeps working after rotate_token

    def __init__(self, kv: KV, preset: str | None = None):
        self.kv = kv
        self.preset = preset or None
        self._lock = threading.Lock()

    def claimed(self) -> bool:
        return bool(self.preset) or self.kv.get(KEY) is not None

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        if self.preset and hmac.compare_digest(token.encode(), self.preset.encode()):
            return True
        record = self.kv.get(KEY)
        if record is None:
            return False
        h = _hash(token)
        if hmac.compare_digest(h, record["sha256"]):
            return True
        prev = record.get("previous")
        return bool(prev) and prev["until"] > time.time() and hmac.compare_digest(h, prev["sha256"])

    def claim(self) -> str | None:
        """Mint and persist the token if unclaimed. Returns it, or None if already claimed."""
        if self.preset:
            return None
        token = "fa_" + secrets.token_urlsafe(32)
        # Atomic insert-if-absent, so two racing clients (or replicas) cannot both claim.
        if not self.kv.create(KEY, {"sha256": _hash(token), "claimed_at": _now()}):
            return None
        return token

    def rotate(self) -> str:
        """Replace the token; the old one keeps working for ROTATE_GRACE seconds so in-flight clients finish."""
        with self._lock:
            token = "fa_" + secrets.token_urlsafe(32)
            old = self.kv.get(KEY) or {}
            self.kv.put(
                KEY,
                {
                    "sha256": _hash(token),
                    "claimed_at": old.get("claimed_at", _now()),
                    "previous": {"sha256": old["sha256"], "until": time.time() + self.ROTATE_GRACE} if old else None,
                },
            )
            return token
