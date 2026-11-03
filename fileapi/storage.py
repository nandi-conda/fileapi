"""Filesystem-backed storage. Every path is relative to the root and may not escape it."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path


class StorageError(Exception):
    """An expected failure (missing file, bad path, ...). Safe to show to clients."""


class NotFound(StorageError):
    pass


class Conflict(StorageError):
    pass


@dataclass
class Entry:
    path: str
    type: str  # "file" | "dir"
    size: int
    modified: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Edit:
    old_text: str
    new_text: str


class Storage:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root).resolve()

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # -- path safety -------------------------------------------------------
    def resolve(self, p: str | None) -> Path:
        clean = (p or "").replace("\\", "/").lstrip("/")
        if any(seg == ".." for seg in clean.split("/")):
            raise StorageError(f"Path may not contain '..': {p}")
        target = (self.root / clean).resolve()  # follows symlinks
        if target != self.root and self.root not in target.parents:
            raise StorageError(f"Path escapes storage root: {p}")
        return target

    def _rel(self, abs_path: Path) -> str:
        return abs_path.relative_to(self.root).as_posix()

    @staticmethod
    def _entry(rel: str, path: Path) -> Entry:
        st = path.stat()
        is_dir = path.is_dir()
        return Entry(
            path=rel + ("/" if is_dir else ""),
            type="dir" if is_dir else "file",
            size=0 if is_dir else st.st_size,
            modified=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        )

    # -- operations --------------------------------------------------------
    def list(self, directory: str = "", recursive: bool = False) -> list[Entry]:
        base = self.resolve(directory)
        if not base.exists():
            raise NotFound(f"No such directory: {directory or '/'}")
        if not base.is_dir():
            raise StorageError(f"Not a directory: {directory}")
        out: list[Entry] = []

        def walk(d: Path) -> None:
            for child in sorted(d.iterdir(), key=lambda c: c.name):
                if child.is_symlink() or child.name.endswith(".fileapi-tmp"):
                    continue
                out.append(self._entry(self._rel(child), child))
                if recursive and child.is_dir():
                    walk(child)

        walk(base)
        return out

    def stat(self, p: str) -> Entry:
        path = self.resolve(p)
        if not path.exists():
            raise NotFound(f"No such file: {p}")
        return self._entry(self._rel(path), path)

    def read(self, p: str) -> str:
        path = self.resolve(p)
        if path.is_dir():
            raise StorageError(f"Is a directory: {p}")
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise NotFound(f"No such file: {p}") from None
        except UnicodeDecodeError:
            raise StorageError(f"Not a UTF-8 text file: {p}") from None

    def write(self, p: str, content: str, overwrite: bool) -> tuple[bool, int]:
        """Write a file atomically. Returns (created, bytes)."""
        path = self.resolve(p)
        if path == self.root:
            raise StorageError("A file path is required")
        if path.is_dir():
            raise StorageError(f"Is a directory: {p}")
        existed = path.exists()
        if existed and not overwrite:
            raise Conflict(f"File already exists: {p} (use write_file or edit_file to change it)")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".fileapi-tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return (not existed, len(data))

    def edit(self, p: str, edits: list[Edit]) -> int:
        """Apply exact find/replace edits, all-or-nothing. Each old_text must match exactly once."""
        content = self.read(p)
        for i, e in enumerate(edits, 1):
            if not e.old_text:
                raise StorageError(f"Edit {i}: old_text must not be empty")
            count = content.count(e.old_text)
            if count == 0:
                raise StorageError(f"Edit {i}: old_text not found in {p}")
            if count > 1:
                raise StorageError(f"Edit {i}: old_text matches {count} times in {p}; include more context")
            content = content.replace(e.old_text, e.new_text, 1)
        self.write(p, content, overwrite=True)
        return len(edits)

    def append(self, p: str, content: str) -> int:
        path = self.resolve(p)
        if path == self.root or path.is_dir():
            raise StorageError(f"Not a file: {p}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
        return path.stat().st_size

    def delete(self, p: str) -> None:
        path = self.resolve(p)
        if path == self.root:
            raise StorageError("Refusing to delete the storage root")
        if not path.exists():
            raise NotFound(f"No such file: {p}")
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                raise Conflict(f"Directory not empty: {p}") from None
        else:
            path.unlink()

    def move(self, src: str, dst: str, overwrite: bool = False) -> None:
        a, b = self.resolve(src), self.resolve(dst)
        if self.root in (a, b):
            raise StorageError("Cannot move the storage root")
        if not a.exists():
            raise NotFound(f"No such file: {src}")
        if b.exists() and not overwrite:
            raise Conflict(f"Destination exists: {dst}")
        b.parent.mkdir(parents=True, exist_ok=True)
        os.replace(a, b)
