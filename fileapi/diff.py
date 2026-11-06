"""A commit's changes as structured unified-diff hunks, for the forge web UI's commit view.

Compared with the first parent (or nothing, for a root commit), with renames detected. Big commits are
cut down so the response stays small: files over MAX_FILE bytes, and every file once MAX_LINES diff lines
have been sent, are listed with their status only (``"hunks": null`` and a ``"skipped"`` reason).
"""

from __future__ import annotations

import difflib
import time

from dulwich.diff_tree import CHANGE_ADD, CHANGE_COPY, CHANGE_DELETE, CHANGE_RENAME, RenameDetector, tree_changes
from dulwich.objects import Commit, TreeEntry
from dulwich.repo import BaseRepo

from .lfs import parse_pointer

CONTEXT = 3
MAX_FILE = 1024 * 1024       # bytes per side
MAX_LINES = 20_000           # diff lines in the whole response
MAX_FILES = 1000             # files listed at all
MAX_RENAME_FILES = 400       # rename detection compares contents: skip it on huge commits

_NONE = TreeEntry(None, None, None)
_STATUS = {CHANGE_ADD: "added", CHANGE_DELETE: "deleted", CHANGE_RENAME: "renamed", CHANGE_COPY: "copied"}


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:8000]


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


def hunks(old: list[str], new: list[str], context: int = CONTEXT) -> list[dict]:
    """Unified-diff hunks: {old_start, old_lines, new_start, new_lines, lines: [" x", "-y", "+z", ...]}."""
    out = []
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for group in matcher.get_grouped_opcodes(context):
        i1, i2, j1, j2 = group[0][1], group[-1][2], group[0][3], group[-1][4]
        lines: list[str] = []
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                lines += [" " + s for s in old[a1:a2]]
                continue
            if tag in ("replace", "delete"):
                lines += ["-" + s for s in old[a1:a2]]
            if tag in ("replace", "insert"):
                lines += ["+" + s for s in new[b1:b2]]
        # Like git: a hunk's start is 0 when its side is empty, otherwise 1-based.
        out.append({"old_start": i1 + 1 if i2 > i1 else i1, "old_lines": i2 - i1,
                    "new_start": j1 + 1 if j2 > j1 else j1, "new_lines": j2 - j1, "lines": lines})
    return out


def commit_diff(r: BaseRepo, commit: Commit) -> dict:
    parent = r[commit.parents[0]] if commit.parents else None
    store = r.object_store
    old_tree = parent.tree if parent is not None else None
    changes = list(tree_changes(store, old_tree, commit.tree))
    if len(changes) <= MAX_RENAME_FILES:
        changes = list(tree_changes(store, old_tree, commit.tree, rename_detector=RenameDetector(store)))
    # Newer dulwich gives None for the missing side of an add or delete; older gives an empty entry.
    changes = [(c.type, c.old or _NONE, c.new or _NONE) for c in changes]
    changes.sort(key=lambda c: c[2].path or c[1].path or b"")

    files, budget, adds, dels = [], MAX_LINES, 0, 0
    for kind, old, new in changes[:MAX_FILES]:
        old_path = old.path.decode(errors="replace") if old.path else None
        new_path = new.path.decode(errors="replace") if new.path else None
        f: dict = {
            "path": new_path or old_path,
            "old_path": old_path if old_path != new_path else None,
            "status": _STATUS.get(kind, "modified"),
            "additions": 0, "deletions": 0, "binary": False, "hunks": None,
        }
        if old.mode and new.mode and old.mode != new.mode:
            f["old_mode"], f["new_mode"] = f"{old.mode:o}", f"{new.mode:o}"
        files.append(f)
        # Submodules (gitlinks) have no blob here.
        if (old.mode or 0) & 0o170000 == 0o160000 or (new.mode or 0) & 0o170000 == 0o160000:
            f["skipped"] = "submodule"
            continue
        old_data = store[old.sha].as_raw_string() if old.sha else b""
        new_data = store[new.sha].as_raw_string() if new.sha else b""
        if parse_pointer(old_data) or parse_pointer(new_data):
            f["lfs"] = True
        if _is_binary(old_data) or _is_binary(new_data):
            f["binary"] = True
            f["skipped"] = "binary"
            continue
        if max(len(old_data), len(new_data)) > MAX_FILE:
            f["skipped"] = "too large"
            continue
        if budget <= 0:
            f["skipped"] = "diff too big"
            continue
        hs = hunks(_lines(old_data), _lines(new_data))
        n = sum(len(h["lines"]) for h in hs)
        f["additions"] = sum(1 for h in hs for s in h["lines"] if s[:1] == "+")
        f["deletions"] = sum(1 for h in hs for s in h["lines"] if s[:1] == "-")
        adds, dels = adds + f["additions"], dels + f["deletions"]
        if n > budget and files[:-1]:  # the first file always gets shown, however big
            f["skipped"] = "diff too big"
            budget = 0
            continue
        f["hunks"] = hs
        budget -= n

    return {
        "sha": commit.id.decode(),
        "parents": [p.decode() for p in commit.parents],
        "author": commit.author.decode(errors="replace"),
        "committer": commit.committer.decode(errors="replace"),
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(commit.author_time)),
        "message": commit.message.decode(errors="replace").strip(),
        "stats": {"files": len(changes), "additions": adds, "deletions": dels},
        "files": files,
        "truncated": len(changes) > MAX_FILES or any(f.get("skipped") == "diff too big" for f in files),
    }
