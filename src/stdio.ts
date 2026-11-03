import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { Storage } from "./storage.js";
import { buildServer } from "./server.js";

const storage = new Storage(process.env.FILEAPI_ROOT ?? "./data");
await storage.init();
await buildServer(storage).connect(new StdioServerTransport());
