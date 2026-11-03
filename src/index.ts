import express from "express";
import { timingSafeEqual } from "node:crypto";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { Storage } from "./storage.js";
import { buildServer } from "./server.js";

const PORT = Number(process.env.PORT ?? 3000);
const HOST = process.env.HOST ?? "0.0.0.0";
const ROOT = process.env.FILEAPI_ROOT ?? "./data";
const TOKEN = process.env.FILEAPI_TOKEN ?? "";

if (!TOKEN && process.env.FILEAPI_ALLOW_NO_AUTH !== "1") {
  console.error("FILEAPI_TOKEN is not set. Set it, or set FILEAPI_ALLOW_NO_AUTH=1 for local testing.");
  process.exit(1);
}

const storage = new Storage(ROOT);
await storage.init();

function authorized(header: string | undefined): boolean {
  if (!TOKEN) return true;
  const m = /^Bearer\s+(.+)$/i.exec(header ?? "");
  if (!m) return false;
  const a = Buffer.from(m[1]);
  const b = Buffer.from(TOKEN);
  return a.length === b.length && timingSafeEqual(a, b);
}

const app = express();
app.use(express.json({ limit: process.env.FILEAPI_MAX_BODY ?? "10mb" }));

app.get("/health", (_req, res) => {
  res.json({ ok: true });
});

app.use("/mcp", (req, res, next) => {
  if (!authorized(req.headers.authorization)) {
    res.status(401).set("WWW-Authenticate", "Bearer").json({
      jsonrpc: "2.0",
      error: { code: -32001, message: "Unauthorized" },
      id: null,
    });
    return;
  }
  next();
});

// Stateless Streamable HTTP: a fresh server + transport per request.
app.post("/mcp", async (req, res) => {
  const server = buildServer(storage);
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined, enableJsonResponse: true });
  res.on("close", () => {
    transport.close();
    server.close();
  });
  try {
    await server.connect(transport);
    await transport.handleRequest(req, res, req.body);
  } catch (e) {
    console.error(e);
    if (!res.headersSent) {
      res.status(500).json({ jsonrpc: "2.0", error: { code: -32603, message: "Internal error" }, id: null });
    }
  }
});

const methodNotAllowed = (_req: express.Request, res: express.Response) => {
  res.status(405).set("Allow", "POST").json({
    jsonrpc: "2.0",
    error: { code: -32000, message: "Method not allowed" },
    id: null,
  });
};
app.get("/mcp", methodNotAllowed);
app.delete("/mcp", methodNotAllowed);

app.listen(PORT, HOST, () => {
  console.log(`fileapi MCP server on http://${HOST}:${PORT}/mcp (root: ${storage.root})`);
});
