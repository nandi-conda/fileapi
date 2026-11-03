import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { spawn, ChildProcess } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const PORT = 3999 + Math.floor(Math.random() * 1000);
const TOKEN = "test-token";
const URL_ = new URL(`http://127.0.0.1:${PORT}/mcp`);
let proc: ChildProcess;
let root: string;
let client: Client;

const text = (r: any) => r.content[0].text as string;

before(async () => {
  root = mkdtempSync(path.join(tmpdir(), "fileapi-"));
  proc = spawn(process.execPath, ["--import", "tsx", "src/index.ts"], {
    env: { ...process.env, PORT: String(PORT), HOST: "127.0.0.1", FILEAPI_ROOT: root, FILEAPI_TOKEN: TOKEN },
    stdio: ["ignore", "pipe", "inherit"],
  });
  await new Promise<void>((res) => proc.stdout!.on("data", (d) => String(d).includes("MCP server") && res()));
  client = new Client({ name: "test", version: "0" });
  await client.connect(
    new StreamableHTTPClientTransport(URL_, { requestInit: { headers: { Authorization: `Bearer ${TOKEN}` } } }),
  );
});

after(async () => {
  await client?.close();
  proc?.kill();
  rmSync(root, { recursive: true, force: true });
});

test("rejects missing or wrong token", async () => {
  const r1 = await fetch(URL_, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal(r1.status, 401);
  const r2 = await fetch(URL_, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer nope" },
    body: "{}",
  });
  assert.equal(r2.status, 401);
});

test("lists tools", async () => {
  const { tools } = await client.listTools();
  assert.deepEqual(
    tools.map((t) => t.name).sort(),
    ["append_file", "create_file", "delete_file", "edit_file", "list_files", "move_file", "read_file", "write_file"],
  );
});

test("create, read, list, edit, move, delete", async () => {
  const call = (name: string, args: any) => client.callTool({ name, arguments: args });

  assert.match(text(await call("create_file", { path: "notes/a.md", content: "hello world\n" })), /Created/);
  const dup = await call("create_file", { path: "notes/a.md", content: "x" });
  assert.equal(dup.isError, true);

  assert.equal(text(await call("read_file", { path: "notes/a.md" })), "hello world\n");

  const list = text(await call("list_files", { recursive: true }));
  assert.match(list, /^notes\/$/m);
  assert.match(list, /^notes\/a\.md\t12B/m);

  await call("edit_file", { path: "notes/a.md", edits: [{ old_text: "world", new_text: "there" }] });
  assert.equal(text(await call("read_file", { path: "notes/a.md" })), "hello there\n");

  const bad = await call("edit_file", { path: "notes/a.md", edits: [{ old_text: "missing", new_text: "" }] });
  assert.equal(bad.isError, true);

  await call("append_file", { path: "notes/a.md", content: "more\n" });
  assert.equal(text(await call("read_file", { path: "notes/a.md" })), "hello there\nmore\n");

  assert.match(text(await call("write_file", { path: "notes/a.md", content: "new" })), /Overwrote/);

  await call("move_file", { from: "notes/a.md", to: "archive/b.md" });
  assert.equal(text(await call("read_file", { path: "archive/b.md" })), "new");

  await call("delete_file", { path: "archive/b.md" });
  assert.equal((await call("read_file", { path: "archive/b.md" })).isError, true);
});

test("blocks path traversal", async () => {
  for (const p of ["../etc/passwd", "a/../../x", "/../x"]) {
    const r = await client.callTool({ name: "read_file", arguments: { path: p } });
    assert.equal(r.isError, true, p);
  }
});
