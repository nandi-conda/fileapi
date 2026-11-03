"""MCP tools over the shared storage, including the first-connect token flow."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .storage import Edit, Storage, StorageError
from .tokens import TokenStore

PathArg = Annotated[str, Field(description="Path relative to the storage root, e.g. 'notes/todo.md'")]


class EditSpec(BaseModel):
    old_text: str = Field(description="Exact text to find (must appear exactly once)")
    new_text: str = Field(description="Replacement text")


def _authed(ctx: Context) -> bool:
    request = ctx.request_context.request
    if request is None:  # stdio: local process, trusted
        return True
    return bool(getattr(request.state, "authed", False))


def connect_instructions(token: str, public_url: str | None) -> str:
    base = (public_url or "https://<your-host>").rstrip("/")
    return (
        f"Access token (shown only once, save it now):\n\n    {token}\n\n"
        "Reconnect with it using either:\n"
        f"  - header   Authorization: Bearer {token}\n"
        f"      e.g. claude mcp add --transport http fileapi {base}/mcp --header \"Authorization: Bearer {token}\"\n"
        f"  - URL      {base}/mcp?token={token}\n"
        "      (for clients that cannot set headers, e.g. a claude.ai custom connector)\n\n"
        "Until you reconnect with the token, this connection can no longer use the file tools."
    )


def build_mcp(storage: Storage, tokens: TokenStore | None = None, public_url: str | None = None) -> MCPServer:
    mcp = MCPServer(
        name="fileapi",
        version="1.2.0",
        instructions=(
            "A central file store. Paths are relative to the store root; directories are created automatically. "
            "If tools say the server is unclaimed, call claim_server once to get an access token."
        ),
    )

    def guard(ctx: Context, fn):
        if not _authed(ctx):
            if tokens is not None and not tokens.claimed():
                raise ToolError("This server is not claimed yet. Call claim_server to create its access token.")
            raise ToolError("Not authorized: reconnect with this server's access token.")
        try:
            return fn()
        except StorageError as e:
            raise ToolError(str(e)) from None

    if tokens is not None:

        @mcp.tool(structured_output=False)
        def claim_server(ctx: Context) -> str:
            """First-time setup: create this server's access token. Only works once, for the first client.
            Show the returned token to the user so they can save it and reconnect with it."""
            token = tokens.claim()
            if token is None:
                raise ToolError("This server is already claimed. Connect with its access token.")
            return connect_instructions(token, public_url)

        @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
        def rotate_token(ctx: Context) -> str:
            """Replace the access token with a new one. The old token stops working after 60 seconds, so every client must reconnect with the new token."""
            if not _authed(ctx):
                raise ToolError("Not authorized.")
            if tokens.preset:
                raise ToolError("The token is pinned by FILEAPI_TOKEN; change that setting instead.")
            return connect_instructions(tokens.rotate(), public_url)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_files(ctx: Context, path: str = "", recursive: bool = False) -> str:
        """List files and directories in the central store. Directories end with '/'."""
        entries = guard(ctx, lambda: storage.list(path, recursive))
        if not entries:
            return "(empty)"
        return "\n".join(e.path if e.type == "dir" else f"{e.path}\t{e.size}B\t{e.modified}" for e in entries)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def read_file(ctx: Context, path: PathArg) -> str:
        """Read a text file from the central store."""
        return guard(ctx, lambda: storage.read(path))

    @mcp.tool(structured_output=False)
    def create_file(ctx: Context, path: PathArg, content: str) -> str:
        """Create a new file. Fails if it already exists. Parent directories are created automatically."""
        _, n = guard(ctx, lambda: storage.write(path, content, overwrite=False))
        return f"Created {path} ({n} bytes)"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def write_file(ctx: Context, path: PathArg, content: str) -> str:
        """Create or fully overwrite a file with the given contents."""
        created, n = guard(ctx, lambda: storage.write(path, content, overwrite=True))
        return f"{'Created' if created else 'Overwrote'} {path} ({n} bytes)"

    @mcp.tool(structured_output=False)
    def edit_file(ctx: Context, path: PathArg, edits: Annotated[list[EditSpec], Field(min_length=1)]) -> str:
        """Edit an existing file by exact find-and-replace. Each old_text must appear exactly once.
        Edits apply in order and are all-or-nothing."""
        n = guard(ctx, lambda: storage.edit(path, [Edit(e.old_text, e.new_text) for e in edits]))
        return f"Applied {n} edit(s) to {path}"

    @mcp.tool(structured_output=False)
    def append_file(ctx: Context, path: PathArg, content: str) -> str:
        """Append text to the end of a file, creating it if missing."""
        size = guard(ctx, lambda: storage.append(path, content))
        return f"Appended to {path} (now {size} bytes)"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def move_file(ctx: Context, source: PathArg, destination: PathArg, overwrite: bool = False) -> str:
        """Move or rename a file or directory."""
        guard(ctx, lambda: storage.move(source, destination, overwrite))
        return f"Moved {source} -> {destination}"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_file(ctx: Context, path: PathArg) -> str:
        """Delete a file or an empty directory."""
        guard(ctx, lambda: storage.delete(path))
        return f"Deleted {path}"

    return mcp
