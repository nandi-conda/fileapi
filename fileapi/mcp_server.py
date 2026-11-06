"""MCP tools over the shared storage. Sign-in happens before a request gets here (fileapi.auth)."""

from __future__ import annotations

import time
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .forge import Forge, RepoPasses, fetch_bundle as forge_fetch_bundle, push_bundle as forge_push_bundle, qualify
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


def _owner(ctx: Context, default: str) -> str:
    """The signed-in account's repo namespace (its handle); stdio runs as the server's admin."""
    request = ctx.request_context.request
    if request is None:
        return default
    return getattr(request.state, "owner", None) or default


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
    owner: str = "admin",
    jobs=None,
) -> MCPServer:
    mcp = MCPServer(
        name="fileapi",
        version="1.5.0",
        instructions=(
            "A central file store. Paths are relative to the store root; directories are created automatically. "
            "It also hosts git repos owned by the signed-in account: refer to one by its name alone (e.g. 'blog'); "
            "they live at '<your handle>/<name>'. Manage and browse them with the *_repo tools, or clone/push "
            "them with git over HTTPS (repo_remote_url gives a short-lived authenticated URL)."
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
        return guard(ctx, lambda: f"signed in as {_owner(ctx, owner)} ({getattr(request.state, 'did', '?')}, admin); "
                                  f"your repos live at {_owner(ctx, owner)}/<name>")

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
        _register_forge_tools(mcp, forge, guard, public_url, passes, owner)
        if jobs is not None:
            _register_deploy_tools(mcp, forge, jobs, guard, owner)

    return mcp


RepoArg = Annotated[str, Field(description="Repo name, e.g. 'blog' (it is yours: '<your handle>/blog'). "
                                         "'<owner>/<name>' also works")]
RefArg = Annotated[str, Field(description="Branch, tag or commit sha; HEAD is the default branch")]


def _register_forge_tools(mcp: MCPServer, forge: Forge, guard, public_url: str | None, passes, default_owner: str) -> None:
    def full_name(ctx: Context, repo: str) -> str:
        """'<name>' (or an old alias) -> the repo's current '<owner>/<name>'."""
        return forge.canonical(qualify(repo, _owner(ctx, default_owner)))

    def scoped(ctx: Context, repo: str, fn):
        """guard() plus name resolution: fn gets the repo's full '<owner>/<name>'."""
        return guard(ctx, lambda: fn(full_name(ctx, repo)))

    def clone_help(ctx: Context, full: str) -> str:
        url = f"{_base_url(ctx, public_url)}/git/{full}.git"
        return (
            f"Clone URL: {url}\n"
            "To clone or push with git, call repo_remote_url for a short-lived authenticated URL."
        )

    @mcp.tool(structured_output=False)
    def create_repo(
        ctx: Context,
        name: Annotated[str, Field(description="Repo name, e.g. 'blog'. It is created under your handle")],
        description: str = "",
        default_branch: str = "main",
    ) -> str:
        """Create an empty git repo owned by you. Push to it with git, or add files with commit_files."""
        info = guard(ctx, lambda: forge.create(_owner(ctx, default_owner), name.strip().strip("/"), description,
                                               default_branch))
        return f"Created {info.full_name}\n" + clone_help(ctx, info.full_name)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_repos(ctx: Context) -> str:
        """List your git repos with each one's visibility and clone URL."""
        me = _owner(ctx, default_owner)
        repos = guard(ctx, lambda: forge.list(me))
        if not repos:
            return f"(no repos yet: create one with create_repo; it will live at {me}/<name>)"
        base = _base_url(ctx, public_url)
        return "\n".join(
            f"{r.name}\t{'public' if r.public else 'private'}\t{base}/git/{r.full_name}.git"
            + (f"\t{r.description}" if r.description else "")
            for r in repos
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_branches(ctx: Context, repo: RepoArg) -> str:
        """List a repo's branches and tags with the commit each points at."""
        info = scoped(ctx, repo, forge.branches)
        lines = [f"default branch: {info['default']}"]
        lines += [f"branch {k}\t{v}" for k, v in sorted(info["branches"].items())]
        lines += [f"tag {k}\t{v}" for k, v in sorted(info["tags"].items())]
        return "\n".join(lines) if len(lines) > 1 else lines[0] + "\n(empty repo: nothing pushed yet)"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def browse_repo(ctx: Context, repo: RepoArg, path: str = "", ref: RefArg = "HEAD") -> str:
        """Show a directory listing, or a file's content, at a branch, tag or commit."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        res = guard(ctx, lambda: forge.browse(full, path, ref))
        head = f"{full}:{res['path'] or '/'} @ {res['commit'][:12]}"
        if res["type"] == "dir":
            rows = [e["name"] if e["size"] is None else f"{e['name']}\t{e['size']}B" for e in res["entries"]]
            return head + "\n" + ("\n".join(rows) or "(empty)")
        if res.get("lfs"):
            return head + f"\n(stored with Git LFS, {res['size']} bytes, sha256 {res['lfs']['oid']})"
        if res.get("binary"):
            return head + f"\n(binary file, {res['size']} bytes)"
        return res["content"]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def repo_log(ctx: Context, repo: RepoArg, ref: RefArg = "HEAD", path: str = "", limit: int = 20) -> str:
        """Show recent commits on a branch (newest first), optionally only those touching a path."""
        commits = scoped(ctx, repo, lambda full: forge.log(full, ref, path or None, limit))
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
        full = guard(ctx, lambda: full_name(ctx, repo))
        res = guard(ctx, lambda: forge.commit_files(full, branch, files, message, author_name or None,
                                                    author_email or None, expected_head or None))
        return f"Committed {res['commit'][:12]} to {full}@{res['branch']}"

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
        results = scoped(ctx, repo, lambda full: forge_push_bundle(forge, full, data, force))
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

        data, tips = scoped(ctx, repo, lambda full: forge_fetch_bundle(forge, full, refs or None, have or None))
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
            full = guard(ctx, lambda: full_name(ctx, repo))
            guard(ctx, lambda: forge.open(full).close())
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
        full = guard(ctx, lambda: full_name(ctx, repo))
        guard(ctx, lambda: forge.set_public(full, public))
        url = f"{_base_url(ctx, public_url)}/git/{full}.git"
        if public:
            return f"{full} is now public. Anyone can run: git clone {url}\nPushing still needs a credential: use repo_remote_url."
        return f"{full} is now private. Cloning needs a credential: use repo_remote_url."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_repo(ctx: Context, repo: RepoArg) -> str:
        """Permanently delete a repo and all its history."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        guard(ctx, lambda: forge.delete(full))
        return f"Deleted {full}"


def _register_deploy_tools(mcp: MCPServer, forge: Forge, jobs, guard, default_owner: str) -> None:
    from .deploy import JOBS_FILE

    secrets = jobs.secrets

    def full_name(ctx: Context, repo: str) -> str:
        full = forge.canonical(qualify(repo, _owner(ctx, default_owner)))
        forge.open(full).close()
        return full

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_repo_secrets(ctx: Context, repo: RepoArg) -> str:
        """Names of a repo's secrets and when each was set. Values are write-only and never shown."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        if not secrets.enabled:
            return "Secrets are turned off on this server: FILEAPI_SECRETS_KEY is not set."
        rows = secrets.list(full)
        if not rows:
            return f"{full} has no secrets. Add one with set_repo_secret."
        return "\n".join(f"{r['name']}  (set {r['updated']})" for r in rows)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def set_repo_secret(
        ctx: Context,
        repo: RepoArg,
        name: Annotated[str, Field(description="Env-var style name, e.g. FLY_API_TOKEN")],
        value: Annotated[str, Field(description="The secret value. It is encrypted and can't be read back")],
    ) -> str:
        """Create or replace an encrypted repo secret. Deploy jobs (.forge/jobs.toml) get it as an env var."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        out = guard(ctx, lambda: secrets.set(full, name, value))
        return f"{'Added' if out['created'] else 'Replaced'} secret {out['name']} on {full}."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_repo_secret(ctx: Context, repo: RepoArg, name: str) -> str:
        """Delete a repo secret."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        guard(ctx, lambda: secrets.delete(full, name.strip()))
        return f"Deleted secret {name.strip()} from {full}."

    @mcp.tool(structured_output=False)
    def run_deploy(
        ctx: Context,
        repo: RepoArg,
        branch: Annotated[str, Field(description="Branch whose current commit to deploy; default: the default branch")] = "",
        job: Annotated[str, Field(description="Run only this job from .forge/jobs.toml")] = "",
    ) -> str:
        """Run a repo's deploy jobs (.forge/jobs.toml) now, as a push would. Check progress with deploy_runs."""
        full = guard(ctx, lambda: full_name(ctx, repo))

        def go():
            info = forge.branches(full)
            b = branch.strip() or info.get("default") or "main"
            if b not in info["branches"]:
                from .storage import NotFound

                raise NotFound(f"No such branch: {b}")
            return jobs.start(full, b, info["branches"][b], trigger="manual", job=job.strip() or None)

        runs = guard(ctx, go)
        return "Started:\n" + "\n".join(f"{r['id']}  {r['job']}  {r['branch']}@{r['sha'][:12]}" for r in runs)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def deploy_runs(
        ctx: Context,
        repo: RepoArg,
        run_id: Annotated[str, Field(description="Show this run's full output instead of the list")] = "",
    ) -> str:
        """Recent deploy job runs for a repo (newest first), or one run's output with secrets masked."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        if run_id.strip():
            r = guard(ctx, lambda: jobs.get_run(full, run_id.strip()))
            head = (f"{r['id']}  {r['job']}  {r['status']}  {r['branch']}@{r['sha'][:12]}  "
                    f"trigger={r['trigger']}  started={r.get('started', '-')}  duration={r.get('duration', '-')}s")
            if r.get("error"):
                head += f"\nerror: {r['error']}"
            return head + "\n\n" + (r.get("output") or "(no output)")
        runs = guard(ctx, lambda: jobs.list_runs(full))
        if not runs:
            return f"No runs yet. Commit {JOBS_FILE} to {full} and push, or call run_deploy."
        return "\n".join(f"{r['id']}  {r['status']:<8} {r['job']}  {r['branch']}@{r['sha'][:12]}  "
                         f"{r.get('started') or r.get('queued')}  {r['trigger']}" for r in runs)
