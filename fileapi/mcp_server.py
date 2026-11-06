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
    releases=None,
    migrator=None,
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
        if releases is not None and passes is not None:
            _register_release_tools(mcp, forge, releases, passes, guard, public_url, owner)
    if migrator is not None:
        _register_blob_tools(mcp, migrator, guard)

    return mcp


def _register_blob_tools(mcp: MCPServer, migrator, guard) -> None:
    import json

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def blob_storage(
        ctx: Context,
        action: Annotated[str, Field(description=(
            "status (default): where bytes live and the last run. copy: upload big rows still only in Postgres "
            "to B2 (keeps the Postgres copy; safe). free: drop the Postgres copy of rows verified in B2, then "
            "VACUUM (deletes data from the database). sweep: list blobs in B2 no row points at; with apply=true "
            "delete them"))] = "status",
        apply: bool = False,
    ) -> str:
        """Move bulky bytes (big git blobs, LFS objects, release assets, big files) from Postgres to Backblaze B2
        and report on it. copy/free/sweep run in the background: call status to follow them."""
        def go():
            from .storage import StorageError

            cfg = migrator.cfg
            if action == "status":
                store = cfg.store
                out = {"blob_store": store.name if store else None, "writes_to_blob_store": cfg.active is not None,
                       "config_error": cfg.error, "last_run": migrator.status}
                if cfg.pg_ready:
                    out["rows"] = migrator.counts()
                return json.dumps(out, indent=1, default=str)
            if action not in ("copy", "free", "sweep"):
                raise StorageError("action must be status, copy, free or sweep")
            if cfg.active is None:
                raise StorageError("B2 isn't set up: needs B2_KEY_ID (CI config) and the B2_TOKEN or "
                                   "B2_APPLICATION_KEY user secret, and room in the database for the blob columns")
            kw = {"apply": apply} if action == "sweep" else {}
            if not migrator.run_background(action, **kw):
                raise StorageError(f"A {migrator.status.get('step')} run is still going: call status")
            return f"Started {action} in the background. Call blob_storage(action='status') to follow it."

        return guard(ctx, go)


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

    def _mb(n: int) -> str:
        return f"{n / 1048576:.1f} MB"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def storage_usage(ctx: Context) -> str:
        """How much of the database each table and each repo's git objects use (Postgres backend)."""
        u = guard(ctx, forge.usage)
        lines = [f"Database: {_mb(u['database_bytes'])}", "", "Tables (incl. indexes and TOAST):"]
        lines += [f"  {t['table']}: {_mb(t['bytes'])} ({t['live_rows']} rows, {t['dead_rows']} dead)"
                  for t in u["tables"] if t["bytes"] >= 65536]
        lines += ["", "Git objects per repo (compressed bytes stored):"]
        lines += [f"  {r['repo']}: {_mb(r['stored_bytes'])} in {r['objects']} objects" for r in u["repos"]]
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def gc_repo(
        ctx: Context,
        repo: RepoArg,
        apply: Annotated[bool, Field(description="false (default): only report what would go. true: delete it")] = False,
    ) -> str:
        """Find git objects no branch or tag can reach any more (left by force-pushes, deleted branches and
        failed pushes) and, with apply=true, delete them. Objects from the last hour are always kept."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        r = guard(ctx, lambda: forge.gc(full, apply=apply))
        head = (f"{r['repo']}: {r['objects']} objects, {_mb(r['stored_bytes'])}. Unreachable: {r['unreachable']} "
                f"({_mb(r['unreachable_bytes'])}), {r['collectable']} old enough to collect "
                f"({_mb(r['collectable_bytes'])}).")
        if apply:
            return head + f"\nDeleted {r['deleted']} objects ({_mb(r['deleted_bytes'])})."
        return head + "\nNothing deleted: call again with apply=true to delete them."

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
        """Names of a repo's secrets (its own and the user secrets it inherits) and when each was set.
        Values are write-only and never shown."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        if not secrets.enabled:
            return "Secrets are turned off on this server: FILEAPI_SECRETS_KEY is not set."
        rows = secrets.list(full)
        inherited = secrets.inherited(full)
        lines = [f"{r['name']}  (set {r['updated']})" for r in rows] or [
            f"{full} has no secrets of its own. Add one with set_repo_secret."]
        if inherited:
            lines.append("\nFrom your user secrets (all your repos get these):")
            lines += [f"{r['name']}  (set {r['updated']})" + ("  overridden by the repo's own" if r["overridden"] else "")
                      for r in inherited]
        return "\n".join(lines)

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

    def me(ctx: Context) -> str:
        return _owner(ctx, default_owner)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_user_secrets(ctx: Context) -> str:
        """Names of your user secrets, which deploy jobs of every repo you own get (a repo secret of the
        same name overrides one). Values are write-only and never shown."""
        if not secrets.enabled:
            return "Secrets are turned off on this server: FILEAPI_SECRETS_KEY is not set."
        owner = guard(ctx, lambda: me(ctx))
        rows = secrets.list_user(owner)
        if not rows:
            return f"{owner} has no user secrets. Add one with set_user_secret."
        return "\n".join(f"{r['name']}  (set {r['updated']})" for r in rows)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def set_user_secret(
        ctx: Context,
        name: Annotated[str, Field(description="Env-var style name, e.g. DEPOT_TOKEN")],
        value: Annotated[str, Field(description="The secret value. It is encrypted and can't be read back")],
    ) -> str:
        """Create or replace an encrypted user secret: deploy jobs of all your repos get it as an env var,
        unless the repo has its own secret of the same name."""
        owner = guard(ctx, lambda: me(ctx))
        out = guard(ctx, lambda: secrets.set_user(owner, name, value))
        return f"{'Added' if out['created'] else 'Replaced'} user secret {out['name']} for all of {owner}'s repos."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_user_secret(ctx: Context, name: str) -> str:
        """Delete a user secret (repos that have their own secret of that name keep it)."""
        owner = guard(ctx, lambda: me(ctx))
        guard(ctx, lambda: secrets.delete_user(owner, name.strip()))
        return f"Deleted user secret {name.strip()}."

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


TagArg = Annotated[str, Field(description="Release tag, e.g. v1.2.0 (need not exist in git)")]


def _human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def _register_release_tools(mcp: MCPServer, forge: Forge, releases, passes, guard, public_url: str | None,
                            default_owner: str) -> None:
    import base64
    import io
    from urllib.parse import quote

    def full_name(ctx: Context, repo: str) -> str:
        full = forge.canonical(qualify(repo, _owner(ctx, default_owner)))
        forge.open(full).close()
        return full

    def api(ctx: Context, full: str) -> str:
        return f"{_base_url(ctx, public_url)}/api/forge/repos/{full}/releases"

    def describe(ctx: Context, full: str, rel: dict) -> str:
        flags = [f for f in ("draft", "prerelease") if rel.get(f)]
        target = (rel.get("target") or "")[:12] or "no commit"
        lines = [f"{rel['tag']}  {rel['name']}" + (f"  [{', '.join(flags)}]" if flags else "")
                 + f"  @{target}{'' if rel.get('tagged') else ' (no git tag)'}  created {rel['created']}"]
        private = not forge.is_public(full)
        for a in rel["assets"]:
            url = f"{api(ctx, full)}/{quote(rel['tag'])}/assets/{quote(a['name'])}"
            if private:
                url += "?dl=" + passes.sign_link(f"{full}/{rel['tag']}/{a['name']}")
            state = "" if a["state"] == "ready" else f"  ({a['state']})"
            lines.append(f"  {a['name']}  {_human(a['size'])}{state}  {url}")
        return "\n".join(lines)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_releases(ctx: Context, repo: RepoArg,
                      tag: Annotated[str, Field(description="Show only this release, with its notes")] = "") -> str:
        """A repo's releases, newest first, with their assets and download URLs (private repos: signed links
        that work for an hour)."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        if tag.strip():
            rel = guard(ctx, lambda: releases.get(full, tag.strip()))
            return describe(ctx, full, rel) + "\n\n" + (rel["notes"] or "(no notes)")
        rels = guard(ctx, lambda: releases.list(full))
        if not rels:
            return f"{full} has no releases. Make one with create_release."
        return "\n\n".join(describe(ctx, full, r) for r in rels)

    @mcp.tool(structured_output=False)
    def create_release(
        ctx: Context,
        repo: RepoArg,
        tag: TagArg,
        name: Annotated[str, Field(description="Title; default: the tag")] = "",
        notes: Annotated[str, Field(description="Release notes (Markdown)")] = "",
        ref: Annotated[str, Field(description="Commit to point at when the tag isn't in git; default: the default branch")] = "",
        prerelease: bool = False,
        draft: Annotated[bool, Field(description="Hidden from anyone not signed in until published")] = False,
    ) -> str:
        """Create a release. Then add files with upload_release_asset (small) or release_upload_url (big)."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        rel = guard(ctx, lambda: releases.create(full, tag, name, notes, ref.strip() or None, prerelease, draft))
        return "Created release:\n" + describe(ctx, full, rel)

    @mcp.tool(structured_output=False)
    def update_release(
        ctx: Context,
        repo: RepoArg,
        tag: TagArg,
        new_tag: Annotated[str, Field(description="Rename the release to this tag")] = "",
        name: Annotated[str | None, Field(description="New title")] = None,
        notes: Annotated[str | None, Field(description="New notes (replaces them)")] = None,
        prerelease: bool | None = None,
        draft: Annotated[bool | None, Field(description="false publishes a draft")] = None,
    ) -> str:
        """Change a release's tag, title, notes or flags. Leave a field out to keep it."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        rel = guard(ctx, lambda: releases.update(full, tag, name=name, notes=notes, prerelease=prerelease,
                                                 draft=draft, new_tag=new_tag.strip() or None))
        return "Updated:\n" + describe(ctx, full, rel)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_release(ctx: Context, repo: RepoArg, tag: TagArg) -> str:
        """Delete a release and all its assets (the git tag, if any, stays)."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        guard(ctx, lambda: releases.delete(full, tag))
        return f"Deleted release {tag} of {full}."

    @mcp.tool(structured_output=False)
    def upload_release_asset(
        ctx: Context,
        repo: RepoArg,
        tag: TagArg,
        name: Annotated[str, Field(description="File name of the asset, e.g. app-linux-x86_64.tar.gz")],
        content_base64: Annotated[str, Field(description="The file's bytes, base64. Up to about 7 MB; for "
                                                         "bigger files use release_upload_url")] = "",
        text: Annotated[str, Field(description="Or the file as UTF-8 text (checksums, notes)")] = "",
        create: Annotated[bool, Field(description="Create the release if it doesn't exist")] = False,
    ) -> str:
        """Add (or replace) a small file on a release."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        if bool(content_base64) == bool(text):
            raise ToolError("Give exactly one of content_base64 or text")
        try:
            data = base64.b64decode(content_base64, validate=True) if content_base64 else text.encode()
        except ValueError:
            raise ToolError("content_base64 is not valid base64") from None

        def go():
            if create:
                try:
                    releases.get(full, tag)
                except StorageError:
                    releases.create(full, tag)
            return releases.upload(full, tag, name, io.BytesIO(data), len(data))

        a = guard(ctx, go)
        return f"Uploaded {a['name']} ({_human(a['size'])}, sha256 {a['sha256']}) to {full} {tag}."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True), structured_output=False)
    def delete_release_asset(ctx: Context, repo: RepoArg, tag: TagArg, name: str) -> str:
        """Remove one file from a release."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        guard(ctx, lambda: releases.delete_asset(full, tag, name))
        return f"Deleted {name} from {full} {tag}."

    @mcp.tool(structured_output=False)
    def release_upload_url(
        ctx: Context,
        repo: RepoArg,
        tag: TagArg,
        minutes: Annotated[int, Field(description="How long the credential works (1 to 1440)")] = 60,
    ) -> str:
        """A short-lived credential and curl commands to upload big release assets straight to the server
        (from a shell, outside MCP). Works for this one repo only."""
        full = guard(ctx, lambda: full_name(ctx, repo))
        cred, expires = passes.issue(full, minutes)
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))
        url = f"{api(ctx, full)}/{quote(tag)}/assets"
        return (f"Credential for {full} until {when} (Authorization: Bearer header):\n{cred}\n\n"
                f"Upload a file (creates the release if needed):\n"
                f'  curl -fsS -H "Authorization: Bearer {cred}" -T dist/app.tar.gz "{url}/app.tar.gz?create=true"\n'
                f"Or with the fileapi release client (splits big files into pieces):\n"
                f"  FORGE_TOKEN={cred} FORGE_URL={_base_url(ctx, public_url)} FORGE_REPO={full} "
                f"git-credential-fileapi release upload --create {tag} dist/*\n"
                f"Assets up to {_human(releases.max_asset)}.")
