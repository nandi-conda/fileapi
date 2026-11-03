import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import { Storage, StorageError } from "./storage.js";

type ToolResult = { content: { type: "text"; text: string }[]; isError?: boolean };

const ok = (text: string): ToolResult => ({ content: [{ type: "text", text }] });

async function run(fn: () => Promise<string>): Promise<ToolResult> {
  try {
    return ok(await fn());
  } catch (e) {
    const msg = e instanceof StorageError ? e.message : `Internal error: ${(e as Error).message}`;
    return { content: [{ type: "text", text: msg }], isError: true };
  }
}

const pathArg = z.string().describe("Path relative to the storage root, e.g. 'notes/todo.md'");

export function buildServer(storage: Storage): McpServer {
  const server = new McpServer({ name: "fileapi", version: "1.0.0" });

  server.registerTool(
    "list_files",
    {
      description: "List files and directories in the central store. Directories end with '/'.",
      inputSchema: {
        path: z.string().optional().describe("Directory to list; defaults to the root"),
        recursive: z.boolean().optional().describe("Include all nested entries"),
      },
      annotations: { readOnlyHint: true },
    },
    ({ path, recursive }) =>
      run(async () => {
        const entries = await storage.list(path ?? "", recursive ?? false);
        if (entries.length === 0) return "(empty)";
        return entries
          .map((e) => (e.type === "dir" ? e.path : `${e.path}\t${e.size}B\t${e.modified}`))
          .join("\n");
      }),
  );

  server.registerTool(
    "read_file",
    {
      description: "Read a text file from the central store.",
      inputSchema: { path: pathArg },
      annotations: { readOnlyHint: true },
    },
    ({ path }) => run(() => storage.read(path)),
  );

  server.registerTool(
    "create_file",
    {
      description: "Create a new file. Fails if the file already exists. Parent directories are created automatically.",
      inputSchema: { path: pathArg, content: z.string().describe("Full file contents") },
    },
    ({ path, content }) =>
      run(async () => {
        const r = await storage.write(path, content, { overwrite: false });
        return `Created ${path} (${r.bytes} bytes)`;
      }),
  );

  server.registerTool(
    "write_file",
    {
      description: "Create or fully overwrite a file with the given contents.",
      inputSchema: { path: pathArg, content: z.string().describe("Full file contents") },
      annotations: { destructiveHint: true },
    },
    ({ path, content }) =>
      run(async () => {
        const r = await storage.write(path, content, { overwrite: true });
        return `${r.created ? "Created" : "Overwrote"} ${path} (${r.bytes} bytes)`;
      }),
  );

  server.registerTool(
    "edit_file",
    {
      description:
        "Edit an existing file by exact find-and-replace. Each old_text must appear exactly once. Edits apply in order and all-or-nothing.",
      inputSchema: {
        path: pathArg,
        edits: z
          .array(
            z.object({
              old_text: z.string().describe("Exact text to find (must be unique in the file)"),
              new_text: z.string().describe("Replacement text"),
            }),
          )
          .min(1),
      },
    },
    ({ path, edits }) =>
      run(async () => {
        const r = await storage.edit(path, edits);
        return `Applied ${r.replacements} edit(s) to ${path}`;
      }),
  );

  server.registerTool(
    "append_file",
    {
      description: "Append text to the end of a file, creating it if missing.",
      inputSchema: { path: pathArg, content: z.string() },
    },
    ({ path, content }) =>
      run(async () => {
        const r = await storage.append(path, content);
        return `Appended to ${path} (now ${r.bytes} bytes)`;
      }),
  );

  server.registerTool(
    "move_file",
    {
      description: "Move or rename a file or directory.",
      inputSchema: {
        from: pathArg,
        to: pathArg,
        overwrite: z.boolean().optional().describe("Replace the destination if it exists"),
      },
      annotations: { destructiveHint: true },
    },
    ({ from, to, overwrite }) =>
      run(async () => {
        await storage.move(from, to, overwrite ?? false);
        return `Moved ${from} -> ${to}`;
      }),
  );

  server.registerTool(
    "delete_file",
    {
      description: "Delete a file or an empty directory.",
      inputSchema: { path: pathArg },
      annotations: { destructiveHint: true },
    },
    ({ path }) =>
      run(async () => {
        await storage.remove(path);
        return `Deleted ${path}`;
      }),
  );

  return server;
}
