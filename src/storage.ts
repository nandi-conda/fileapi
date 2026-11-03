import { promises as fs } from "node:fs";
import path from "node:path";

export class StorageError extends Error {}

export interface Entry {
  path: string;
  type: "file" | "dir";
  size: number;
  modified: string;
}

export interface Edit {
  old_text: string;
  new_text: string;
}

/** Filesystem-backed store. Every path is relative to `root` and may not escape it. */
export class Storage {
  readonly root: string;

  constructor(root: string) {
    this.root = path.resolve(root);
  }

  async init(): Promise<void> {
    await fs.mkdir(this.root, { recursive: true });
  }

  /** Resolve a user path to an absolute path inside root, rejecting traversal. */
  resolve(p: string): string {
    const clean = (p ?? "").replace(/\\/g, "/").replace(/^\/+/, "");
    if (clean.split("/").some((seg) => seg === "..")) {
      throw new StorageError(`Path may not contain '..': ${p}`);
    }
    const abs = path.resolve(this.root, clean);
    if (abs !== this.root && !abs.startsWith(this.root + path.sep)) {
      throw new StorageError(`Path escapes storage root: ${p}`);
    }
    return abs;
  }

  private rel(abs: string): string {
    return path.relative(this.root, abs).split(path.sep).join("/");
  }

  private async assertNoSymlinkEscape(abs: string): Promise<void> {
    // Resolve the deepest existing ancestor and make sure it is still inside root.
    let cur = abs;
    while (true) {
      try {
        const real = await fs.realpath(cur);
        const realRoot = await fs.realpath(this.root);
        if (real !== realRoot && !real.startsWith(realRoot + path.sep)) {
          throw new StorageError("Path resolves outside storage root");
        }
        return;
      } catch (e: any) {
        if (e instanceof StorageError) throw e;
        if (e.code !== "ENOENT") throw e;
        const parent = path.dirname(cur);
        if (parent === cur) return;
        cur = parent;
      }
    }
  }

  private async target(p: string): Promise<string> {
    const abs = this.resolve(p);
    await this.assertNoSymlinkEscape(abs);
    return abs;
  }

  async list(dir = "", recursive = false): Promise<Entry[]> {
    const abs = await this.target(dir);
    const out: Entry[] = [];
    const walk = async (d: string) => {
      let names: import("node:fs").Dirent[];
      try {
        names = await fs.readdir(d, { withFileTypes: true });
      } catch (e: any) {
        if (e.code === "ENOENT") throw new StorageError(`No such directory: ${dir || "/"}`);
        if (e.code === "ENOTDIR") throw new StorageError(`Not a directory: ${dir}`);
        throw e;
      }
      names.sort((a, b) => a.name.localeCompare(b.name));
      for (const n of names) {
        const full = path.join(d, n.name);
        if (n.isSymbolicLink()) continue;
        const st = await fs.stat(full);
        const isDir = st.isDirectory();
        out.push({
          path: this.rel(full) + (isDir ? "/" : ""),
          type: isDir ? "dir" : "file",
          size: isDir ? 0 : st.size,
          modified: st.mtime.toISOString(),
        });
        if (isDir && recursive) await walk(full);
      }
    };
    await walk(abs);
    return out;
  }

  async read(p: string): Promise<string> {
    const abs = await this.target(p);
    try {
      return await fs.readFile(abs, "utf8");
    } catch (e: any) {
      if (e.code === "ENOENT") throw new StorageError(`No such file: ${p}`);
      if (e.code === "EISDIR") throw new StorageError(`Is a directory: ${p}`);
      throw e;
    }
  }

  async write(p: string, content: string, opts: { overwrite: boolean }): Promise<{ created: boolean; bytes: number }> {
    const abs = await this.target(p);
    if (abs === this.root) throw new StorageError("A file path is required");
    await fs.mkdir(path.dirname(abs), { recursive: true });
    let existed = false;
    try {
      const st = await fs.stat(abs);
      if (st.isDirectory()) throw new StorageError(`Is a directory: ${p}`);
      existed = true;
    } catch (e: any) {
      if (e instanceof StorageError) throw e;
      if (e.code !== "ENOENT") throw e;
    }
    if (existed && !opts.overwrite) {
      throw new StorageError(`File already exists: ${p} (use write_file or edit_file to change it)`);
    }
    // Atomic write: temp file then rename.
    const tmp = `${abs}.${process.pid}.${Date.now()}.tmp`;
    await fs.writeFile(tmp, content, "utf8");
    await fs.rename(tmp, abs);
    return { created: !existed, bytes: Buffer.byteLength(content, "utf8") };
  }

  async edit(p: string, edits: Edit[]): Promise<{ content: string; replacements: number }> {
    let content = await this.read(p);
    edits.forEach((e, i) => {
      if (e.old_text === "") throw new StorageError(`Edit ${i + 1}: old_text must not be empty`);
      const first = content.indexOf(e.old_text);
      if (first === -1) throw new StorageError(`Edit ${i + 1}: old_text not found in ${p}`);
      if (content.indexOf(e.old_text, first + 1) !== -1) {
        throw new StorageError(`Edit ${i + 1}: old_text matches more than once in ${p}; include more context`);
      }
      content = content.slice(0, first) + e.new_text + content.slice(first + e.old_text.length);
    });
    await this.write(p, content, { overwrite: true });
    return { content, replacements: edits.length };
  }

  async append(p: string, content: string): Promise<{ bytes: number }> {
    const abs = await this.target(p);
    await fs.mkdir(path.dirname(abs), { recursive: true });
    await fs.appendFile(abs, content, "utf8");
    return { bytes: (await fs.stat(abs)).size };
  }

  async remove(p: string): Promise<void> {
    const abs = await this.target(p);
    if (abs === this.root) throw new StorageError("Refusing to delete the storage root");
    try {
      const st = await fs.stat(abs);
      if (st.isDirectory()) {
        await fs.rmdir(abs); // only empty directories
      } else {
        await fs.unlink(abs);
      }
    } catch (e: any) {
      if (e.code === "ENOENT") throw new StorageError(`No such file: ${p}`);
      if (e.code === "ENOTEMPTY") throw new StorageError(`Directory not empty: ${p}`);
      throw e;
    }
  }

  async move(from: string, to: string, overwrite = false): Promise<void> {
    const src = await this.target(from);
    const dst = await this.target(to);
    if (src === this.root || dst === this.root) throw new StorageError("Cannot move the storage root");
    try {
      await fs.stat(src);
    } catch {
      throw new StorageError(`No such file: ${from}`);
    }
    if (!overwrite) {
      try {
        await fs.stat(dst);
        throw new StorageError(`Destination exists: ${to}`);
      } catch (e: any) {
        if (e instanceof StorageError) throw e;
      }
    }
    await fs.mkdir(path.dirname(dst), { recursive: true });
    await fs.rename(src, dst);
  }
}
