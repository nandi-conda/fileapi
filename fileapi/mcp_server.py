"""MCP tools over the shared storage. Sign-in happens before a request gets here (fileapi.auth)."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .forge import Forge, RepoPasses, fetch_bundle as forge_fetch_bundle, push_bundle as forge_push_bundle, split_repo
from .storage import Edit, Storage, StorageError

PathArg = Annotated[str, Field(description="Path relative to the storage root, e.g. 'notes/todo.md'")]


class EditSpec(BaseModel):
    old_text: str = Field(description="Exact text to find (must appear exactly once)")
    new_text: str = Field(description="Replacement text")


def _authed(ctx: Context) -> bool:
    request = ctx.request_context.request
    if request is None:  # stdio: local process, trusted
        return True
    return bool(getattr(request.state, "authed", False))


def _scope(ctx: Context) -> str | None:
    request = ctx.request_context.request
    if request is None:  # stdio: local process, owner
        return ""
    return getattr(request.state, "scope", None)


def _base_url(ctx: Context, public_url: str | None) -> str:
    if public_url:
        return public_url.rstrip("/")
    request = ctx.request_context.request
    if request is None:
        return "https://<your-host>"
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",")[0].strip()
    return f"{proto}://{host}"


def build_mcp(
    storage: Storage,
    public_url: str | None = None,
    forge: Forge | None = None,
    passes: RepoPasses | None = None,
) -> MCPServer:
    mcp = MCPServer(
        name="fileapi",
        version="1.4.0",
        instructions=(
            "A central file store. Paths are relative to the store root; directories are created automatically. "
            "It also hosts git repos, named '<project>/<repo>': manage and browse them with the *_repo tools, "
            "or clone/push them with git over HTTPS (repo_remote_url gives a short-lived authenticated URL)."
        ),
    )

    def guard(ctx: Context, fn):
        if not _authed(ctx):
            raise ToolError("Not authorized: sign in again from your MCP client.")
        try:
            return fn()
        except StorageError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def whoami(ctx: Context) -> str:
        """Show which atproto account this connection is signed in as."""
        request = ctx.request_context.request
        if request is None:
            return "local stdio process (no sign-in needed)"
        return guard(ctx, lambda: f"signed in as {getattr(request.state, 'did', '?')} (admin)")

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

    if forge is not None:
        _register_forge_tools(mcp, forge, guard, public_url, passes)

    return mcp


RepoArg = Annotated[str, Field(description="Repo as '<project>/<name>', e.g. 'website/blog'")]
RefArg = Annotated[str, Field(description="Branch, tag or commit sha; HEAD is the default branch")]


def _register_forge_tools(mcp: MCPServer, forge: Forge, guard, public_url: str | None, passes) -> None:
    def scoped(ctx: Context, project: str, fn):
        """guard() plus the per-project check: a project token only reaches its own project."""

        def run():
            scope = _scope(ctx)
            if scope and project != scope:
                raise StorageError(f"This token can only access project {scope!r}")
            return fn()

        return guard(ctx, run)

    def project_of(repo: str) -> str:
        try:
            return split_repo(repo)[0]
        except StorageError:
            return ""

    def clone_help(ctx: Context, full: str) -> str:
        url = f"{_base_url(ctx, public_url)}/git/{full}.git"
        return (
            f"Clone URL: {url}\n"
            "Authenticate with the access token as the password (any username), e.g.\n"
            f"  git clone {url.replace('://', '://x:<token>@', 1)}\n"
            "or keep it out of the URL with a header:\n"
            f"  git -c http.extraHeader=\"Authorization: Bearer <token>\" clone {url}"
        )

    @mcp.tool(structured_output=False)
    def create_repo(
        ctx: Context,
        project: Annotated[str, Field(description="Project the repo belongs to, e.g. 'website'")],
        name: Annotated[str, Field(description="Repo name, e.g. 'blog'")],
        description: str = "",
        default_branch: str = "main",
    ) -> str:
        """Create an empty git repo. Push to it with git, or add files with commit_files."""
        info = scoped(ctx, project, lambda: forge.create(project, name, description, default_branch))
        return f"Created {info.full_name}\n" + clone_help(ctx, info.full_name)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_repos(ctx: Context, project: str = "") -> str:
        """List git repos, optionally only one project's. Shows each repo's clone URL."""
        scope = _scope(ctx)
        repos = scoped(ctx, project or scope or "", lambda: forge.list(project or scope or None))
        if not repos:
            return "(no repos)"
        base = _base_url(ctx, public_url)
        return "\n".join(
            f"{r.full_name}\t{'public' if r.public else 'private'}\t{base}/git/{r.full_name}.git"
            + (f"\t{r.description}" if r.description else "")
            for r in repos
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_branches(ctx: Context, repo: RepoArg) -> str:
        """List a repo's branches and tags with the commit each points at."""
        info = scoped(ctx, project_of(repo), lambda: forge.branches(repo))
        lines = [f"default branch: {info['default']}"]
        lines += [f"branch {k}\t{v}" for k, v in sorted(info["branches"].items())]
        lines += [f"tag {k}\t{v}" for k, v in sorted(info["tags"].items())]
        return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n(empty repo: nothing pushed yet)"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def browse_repo(ctx: Context, repo: RepoArg, path: str = "", ref: RefArg = "HEAD") -> str:
        """Show a directory listing, or a file's content, at a branch, tag or commit."""
        res = scoped(ctx, project_of(repo), lambda: forge.browse(repo, path, ref))
        head = f"{repo}:{res['path'] or '/'} @ {res['commit'][:12]}"
        if res["type"] == "dir":
            rows = [e["name"] if e["size"] is None else f"{e['name']}\t{e['size']}B" for e in res["entries"]]
            return head + "\n" + ("\n".join(rows) or "(empty)")
        if res.get("binary"):
            return head + f"\n(binary file, {res['size']} bytes)"
        return res["content"]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def repo_log(ctx: Context, repo: RepoArg, ref: RefArg = "HEAD", path: str = "", limit: int = 20) -> str:
        """Show recent commits on a branch (newest first), optionally only those touching a path."""
        commits = scoped(ctx, project_of(repo), lambda: forge.log(repo, ref, path or None, limit))
        return "\n\n".join(
            f"{c['sha'][:12]}  {c['date']}  {c['author']}\n    " + c["message"].replace("\n", "\n    ")
            for c in commits
        ) or "(no commits)"

    @mcp.tool(structured_output=False)
    def commit_files(
        ctx: Context,
        repo: RepoArg,
        message: str,
        files: Annotated[
            dict[str, str | None],
            Field(description="Map of path to new file content; null deletes the file"),
        ],
        branch: str = "main",
        author_name: str = "",
        author_email: str = "",
        expected_head: Annotated[
            str, Field(description="Optional: fail unless the branch is still at this commit sha")
        ] = "",
    ) -> str:
        """Commit file changes to a branch in one commit (creates the branch if it does not exist).
        Lets clients without git write to a repo."""
        res = scoped(
            ctx,
            project_of(repo),
            lambda: forge.commit_files(repo, branch, files, message, author_name or None, author_email or None,
                                       expected_head or None),
        )
        return f"Committed {res['commit'][:12]} to {repo}@{res['branch']}"

    @mcp.tool(structured_output=False)
    def push_bundle(
        ctx: Context,
        repo: RepoArg,
        bundle_base64: Annotated[str, Field(description="Base64 of a file made by `git bundle create`")],
        force: bool = False,
    ) -> str:
        """Push local commits without git credentials: run `git bundle create /tmp/p.bundle <branch>` (add
        `^origin/<branch>` or `^<sha the server has>` to send only new commits), base64 it, and pass it here.
        Branches move fast-forward only unless force. Best for small pushes: the bundle travels inside the
        tool call. For large pushes use repo_remote_url and plain `git push`."""
        import base64
        import binascii

        try:
            data = base64.b64decode("".join(bundle_base64.split()), validate=True)
        except (binascii.Error, ValueError):
            raise ToolError("bundle_base64 is not valid base64") from None
        results = scoped(ctx, project_of(repo), lambda: forge_push_bundle(forge, repo, data, force))
        lines = []
        for r in results:
            if r["status"] == "rejected":
                lines.append(f"! {r['ref']}: rejected ({r['reason']})")
            elif r["status"] == "up to date":
                lines.append(f"= {r['ref']}: up to date")
            else:
                old = (r["old"] or "")[:12]
                lines.append(f"{'+' if r['status'] == 'forced' else '*'} {r['ref']}: {r['status']} "
                             f"{old + '..' if old else ''}{r['sha'][:12]}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def fetch_bundle(
        ctx: Context,
        repo: RepoArg,
        refs: Annotated[list[str], Field(description="Branches/tags to include; empty means all")] = [],
        have: Annotated[
            list[str], Field(description="Commit shas you already have (e.g. `git rev-parse origin/main`), "
                                         "so only newer history is sent")
        ] = [],
    ) -> str:
        """Pull or clone without git credentials. Returns a base64 git bundle: decode it to a file, then
        `git clone f.bundle dir` or `git fetch f.bundle 'refs/heads/*:refs/remotes/fileapi/*'`.
        Best for small repos: the bundle travels inside the tool result. For large ones use repo_remote_url."""
        import base64

        data, tips = scoped(ctx, project_of(repo), lambda: forge_fetch_bundle(forge, repo, refs or None, have or None))
        tip_lines = "\n".join(f"{k}\t{v}" for k, v in tips.items())
        if not data:
            return f"Already up to date.\n{tip_lines}"
        limit = 8 * 1024 * 1024
        if len(data) > limit:
            raise ToolError(f"Bundle is {len(data)} bytes, over {limit}; pass `have`, or use repo_remote_url and git")
        return (f"{tip_lines}\n\nBundle ({len(data)} bytes), base64 between the markers:\n"
                f"-----BEGIN BUNDLE-----\n{base64.b64encode(data).decode()}\n-----END BUNDLE-----")

    if passes is not None:

        @mcp.tool(structured_output=False)
        def repo_remote_url(
            ctx: Context,
            repo: RepoArg,
            minutes: Annotated[int, Field(description="How long the URL works (1 to 1440)")] = 60,
        ) -> str:
            """Get a git remote URL with a short-lived credential for this one repo, so you can run plain
            `git push` / `git pull` / `git clone` without the user typing the access token. Use it as a
            one-off URL (`git push <url> main`) rather than saving it as a remote: it expires."""
            full = scoped(ctx, project_of(repo), lambda: "/".join(split_repo(repo)))
            scoped(ctx, project_of(repo), lambda: forge.open(full).close())
            cred, expires = passes.issue(full, minutes)
            url = f"{_base_url(ctx, public_url)}/git/{full}.git".replace("://", f"://x:{cred}@", 1)
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))
            return (f"{url}\n\nWorks for {full} only, until {when}. Examples:\n"
                    f"  git push {url} main\n  git pull {url} main\n  git clone {url}\n"
                    "After a clone, run `git remote set-url origin <plain URL>` so the credential is not kept.")

    @mcp.tool(structured_output=False)
    def set_repo_visibility(
        ctx: Context,
        repo: RepoArg,
        visibility: Annotated[Literal["public", "private"], Field(description="public: anyone can clone and fetch "
                                                                           "without a token. private: token needed")],
    ) -> str:
        """Make a repo public (anonymous clone and fetch over HTTPS) or private again. Pushing always needs a token."""
        public = visibility == "public"
        scoped(ctx, project_of(repo), lambda: forge.set_public(repo, public))
        url = f"{_base_url(ctx, public_url)}/git/{'/'.join(split_repo(repo))}.git"
        if public:
            return f"{repo} is now public. Anyone can run: git clone {url}\nPushing still needs the access token."
        return f"{repo} is now private. Cloning needs the access token."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_repo(ctx: Context, repo: RepoArg) -> str:
        """Permanently delete a repo and all its history."""
        scoped(ctx, project_of(repo), lambda: forge.delete(repo))
        return f"Deleted {repo}"
