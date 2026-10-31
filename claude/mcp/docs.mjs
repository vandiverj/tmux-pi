#!/usr/bin/env node
// docs MCP server — offline, read-only library docs matched to the project's lockfile: `api_signature` and `docs_search`.
// Added to Claude only when TP_DOCS_TOOLS=1 (bin/tp-claude passes claude/mcp.docs.json). This process opens no sockets and
// reads no files itself: both tools run `bin/tp-docs --json`, which answers from allowlisted local files, caps the answer at
// 8 KB, and logs every call to $TP_TASK_DIR/docs-calls.jsonl. If it cannot log (no TP_TASK_DIR) it answers nothing, and the
// tool call fails.
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const TP_DOCS = join(ROOT, "bin", "tp-docs");
const str = (description) => ({ type: "string", description });

const run = (args) =>
  new Promise((done) =>
    execFile(TP_DOCS, args, { timeout: 60000, maxBuffer: 1024 * 1024, cwd: process.cwd(), env: process.env }, (err, stdout, stderr) =>
      done({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) })));

// `tp-docs --json` prints {status,text,version,provenance}; a non-zero exit means it could not answer (for instance, could not log).
const ask = async (args) => {
  const r = await run(["--json", ...args]);
  if (r.code !== 0) throw new Error(r.stderr.trim() || `tp-docs exited ${r.code}`);
  const reply = JSON.parse(r.stdout);
  return reply.text;
};

const TOOLS = {
  api_signature: {
    description:
      "Exact TypeScript declaration (with its doc comment) of a symbol from an installed npm package, read offline from the " +
      "package's own .d.ts files at the version the project's lockfile pins. Use it instead of guessing an API from memory. " +
      "`symbol` may be dotted (Namespace.member, Class.method). No lockfile entry means no answer.",
    schema: {
      type: "object", required: ["package", "symbol"],
      properties: { package: str("npm package name, e.g. zod or @scope/name (or npm:name)"), symbol: str("Exported name, e.g. z.object or Client.connect") },
    },
    run: (p) => ask(["signature", String(p.package ?? ""), String(p.symbol ?? "")]),
  },
  docs_search: {
    description:
      "Search the local docs index of a library (built from its docs at the lockfile-pinned version). Returns the best matching " +
      "sections with file:line. Offline; no result when the library has no index for the pinned version.",
    schema: {
      type: "object", required: ["lib", "query"],
      properties: { lib: str("npm package name the docs belong to"), query: str("What to look for"), limit: { type: "number", description: "Max results, 1-10 (default 5)" } },
    },
    run: (p) => ask(["search", String(p.lib ?? ""), String(p.query ?? ""), ...(p.limit ? ["--limit", String(Math.floor(Number(p.limit)))] : [])]),
  },
};

const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
async function handle(msg) {
  const { id, method, params } = msg;
  if (id === undefined) return; // notifications
  if (method === "initialize")
    return send({ id, result: { protocolVersion: params?.protocolVersion ?? "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "tmux-pi-docs", version: "0.1.0" } } });
  if (method === "ping") return send({ id, result: {} });
  if (method === "tools/list")
    return send({ id, result: { tools: Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.description, inputSchema: t.schema })) } });
  if (method === "tools/call") {
    const tool = TOOLS[params?.name];
    if (!tool) return send({ id, result: { isError: true, content: [{ type: "text", text: `unknown tool ${params?.name}` }] } });
    try {
      return send({ id, result: { content: [{ type: "text", text: await tool.run(params.arguments ?? {}) }] } });
    } catch (e) {
      return send({ id, result: { isError: true, content: [{ type: "text", text: String(e.message ?? e) }] } });
    }
  }
  send({ id, error: { code: -32601, message: `method not found: ${method}` } });
}
// One request at a time, in arrival order: calls (and the lines they log) happen in the order the client sent them.
let queue = Promise.resolve();
createInterface({ input: process.stdin }).on("line", (l) => { queue = queue.then(() => handle(JSON.parse(l))).catch(() => {}); });
