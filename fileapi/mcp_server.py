"""MCP tools over the shared storage."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .storage import Edit, Storage, StorageError

PathArg = Annotated[str, Field(description="Path relative to the storage root, e.g. 'notes/todo.md'")]


class EditSpec(BaseModel):
    old_text: str = Field(description="Exact text to find (must appear exactly once)")
    new_text: str = Field(description="Replacement text")


def _guard(fn):
    try:
        return fn()
    except StorageError as e:
        raise ToolError(str(e)) from None


def build_mcp(storage: Storage) -> MCPServer:
    mcp = MCPServer(
        name="fileapi",
        version="1.0.0",
        instructions="A central file store. Paths are relative to the store root; directories are created automatically.",
    )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_files(path: str = "", recursive: bool = False) -> str:
        """List files and directories in the central store. Directories end with '/'."""
        entries = _guard(lambda: storage.list(path, recursive))
        if not entries:
            return "(empty)"
        return "\n".join(e.path if e.type == "dir" else f"{e.path}\t{e.size}B\t{e.modified}" for e in entries)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def read_file(path: PathArg) -> str:
        """Read a text file from the central store."""
        return _guard(lambda: storage.read(path))

    @mcp.tool(structured_output=False)
    def create_file(path: PathArg, content: str) -> str:
        """Create a new file. Fails if it already exists. Parent directories are created automatically."""
        _, n = _guard(lambda: storage.write(path, content, overwrite=False))
        return f"Created {path} ({n} bytes)"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def write_file(path: PathArg, content: str) -> str:
        """Create or fully overwrite a file with the given contents."""
        created, n = _guard(lambda: storage.write(path, content, overwrite=True))
        return f"{'Created' if created else 'Overwrote'} {path} ({n} bytes)"

    @mcp.tool(structured_output=False)
    def edit_file(path: PathArg, edits: Annotated[list[EditSpec], Field(min_length=1)]) -> str:
        """Edit an existing file by exact find-and-replace. Each old_text must appear exactly once.
        Edits apply in order and are all-or-nothing."""
        n = _guard(lambda: storage.edit(path, [Edit(e.old_text, e.new_text) for e in edits]))
        return f"Applied {n} edit(s) to {path}"

    @mcp.tool(structured_output=False)
    def append_file(path: PathArg, content: str) -> str:
        """Append text to the end of a file, creating it if missing."""
        size = _guard(lambda: storage.append(path, content))
        return f"Appended to {path} (now {size} bytes)"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def move_file(source: PathArg, destination: PathArg, overwrite: bool = False) -> str:
        """Move or rename a file or directory."""
        _guard(lambda: storage.move(source, destination, overwrite))
        return f"Moved {source} -> {destination}"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_file(path: PathArg) -> str:
        """Delete a file or an empty directory."""
        _guard(lambda: storage.delete(path))
        return f"Deleted {path}"

    return mcp
