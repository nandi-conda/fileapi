"""Trust-on-first-use access token.

The server starts unclaimed. The first MCP client calls the `claim_server` tool, which mints a random
token, stores only its SHA-256 hash, and returns the token once. From then on every request must
present that token. Delete the state file to reset (or set FILEAPI_TOKEN to pin a token yourself).
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


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, state_dir: str | os.PathLike, preset: str | None = None):
        self.path = Path(state_dir) / "token.json"
        self.preset = preset or None
        self._lock = threading.Lock()

    def claimed(self) -> bool:
        return bool(self.preset) or self.path.exists()

    ROTATE_GRACE = 60  # seconds the old token keeps working after rotate_token

    def _record(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return None

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        if self.preset and hmac.compare_digest(token.encode(), self.preset.encode()):
            return True
        record = self._record()
        if record is None:
            return False
        h = _hash(token)
        if hmac.compare_digest(h, record["sha256"]):
            return True
        prev = record.get("previous")
        return bool(prev) and prev["until"] > time.time() and hmac.compare_digest(h, prev["sha256"])

    def claim(self) -> str | None:
        """Mint and persist the token if unclaimed. Returns it, or None if already claimed."""
        with self._lock:
            if self.claimed():
                return None
            token = "fa_" + secrets.token_urlsafe(32)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = {"sha256": _hash(token), "claimed_at": datetime.now(timezone.utc).isoformat()}
            # O_EXCL so two racing processes cannot both claim.
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return None
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            return token

    def rotate(self) -> str:
        """Replace the token; the old one keeps working for ROTATE_GRACE seconds so in-flight clients finish."""
        with self._lock:
            token = "fa_" + secrets.token_urlsafe(32)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            old = self._record() or {}
            record = {
                "sha256": _hash(token),
                "claimed_at": old.get("claimed_at", datetime.now(timezone.utc).isoformat()),
                "previous": {"sha256": old["sha256"], "until": time.time() + self.ROTATE_GRACE} if old else None,
            }
            tmp.write_text(json.dumps(record))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
            return token
