#!/usr/bin/env node
// safe MCP server — redacted reads for files the PreToolUse hook will not let Read/Grep open, and `reveal`.
// safe_read and safe_grep run their output through bin/tp-redact before it leaves this process, so values
// come back as redaction placeholders shared with the hooks (same TP_REDACTION_LAUNCH store).
// reveal returns one real value. Claude Code prompts for it on every call (requiresUserInteraction), and the
// PreToolUse hook adds the placeholder, its file:line and the reason to that prompt; the value is never shown there.
import { execFile } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const REDACT = join(ROOT, "bin", "tp-redact");
const MAX_FILE = 4_000_000;
const MAX_CHARS = 200_000;
const PLACEHOLDER = /^<REDACTED:[a-z0-9_]+>$/;
const ph = (name) => `<REDACTED:${name}>`;

const run = (cmd, args, input) =>
  new Promise((done) => {
    const child = execFile(cmd, args, { timeout: 120000, maxBuffer: 64 * 1024 * 1024, cwd: process.cwd() }, (err, stdout, stderr) =>
      done({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) }));
    if (input !== undefined) child.stdin?.end(input);
  });
// Fail closed: if the redactor does not succeed, nothing from the file is returned.
const redact = async (text, args) => {
  const r = await run(REDACT, ["--stdin", ...args], text);
  if (r.code !== 0) throw new Error("redaction failed; output withheld");
  return r.stdout;
};
const where = (p) => {
  const expanded = p === "~" ? homedir() : p.startsWith("~/") ? join(homedir(), p.slice(2)) : p;
  return isAbsolute(expanded) ? expanded : resolve(process.cwd(), expanded);
};
const clip = (text) => (text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS)}\n…(truncated)` : text);
const str = (description) => ({ type: "string", description });

const TOOLS = {
  safe_read: {
    description:
      `Read a file with secrets replaced by ${ph("kind_n")} placeholders. Use this for credential files and env files that the ` +
      "normal Read tool refuses (production/staging env files, ~/.aws/credentials, keys). Placeholders can be used in commands " +
      "and in Write content or Edit new_string for the same file (never in Edit old_string); they never reveal the value.",
    schema: {
      type: "object", required: ["path"],
      properties: { path: str("File to read"), offset: { type: "number", description: "First line, 1-based (default 1)" }, limit: { type: "number", description: "Max lines (default 2000)" } },
    },
    async run(p) {
      const file = where(String(p.path ?? ""));
      const info = statSync(file);
      if (!info.isFile()) throw new Error(`${file} is not a file`);
      if (info.size > MAX_FILE) throw new Error(`${file} is larger than ${MAX_FILE} bytes`);
      const raw = readFileSync(file);
      if (raw.includes(0)) throw new Error(`${file} looks binary`);
      const lines = (await redact(raw.toString("utf8"), ["--source", file])).split("\n");
      const first = Math.max(1, Math.floor(Number(p.offset) || 1));
      const count = Math.max(1, Math.min(10000, Math.floor(Number(p.limit) || 2000)));
      const shown = lines.slice(first - 1, first - 1 + count).map((l, i) => `${String(first + i).padStart(6)}\t${l}`);
      return clip(shown.join("\n") || "(empty)");
    },
  },
  safe_grep: {
    description:
      `Search file contents (regular expression) with secrets replaced by ${ph("kind_n")} placeholders. Includes hidden files such ` +
      "as .env*, so it can search what the normal Grep refuses. Returns path:line:text.",
    schema: {
      type: "object", required: ["pattern"],
      properties: {
        pattern: str("Regular expression"), path: str("File or directory (default: working directory)"),
        glob: str("Only files matching this glob, e.g. '*.env*'"), ignore_case: { type: "boolean" },
        include_ignored: { type: "boolean", description: "Also search files ignored by .gitignore (default false)" },
        max_lines: { type: "number", description: "Max result lines (default 200)" },
      },
    },
    async run(p) {
      const pattern = String(p.pattern ?? "");
      if (!pattern) throw new Error("pattern is required");
      const target = where(String(p.path ?? "."));
      const have = await run("rg", ["--version"]);
      const args = have.code === 0
        ? ["--line-number", "--no-heading", "--color", "never", "--with-filename", "--hidden", "-g", "!.git",
           ...(p.include_ignored ? ["--no-ignore"] : []), ...(p.ignore_case ? ["-i"] : []), ...(p.glob ? ["-g", String(p.glob)] : []),
           "-e", pattern, "--", target]
        : ["-rnIE", ...(p.ignore_case ? ["-i"] : []), ...(p.glob ? [`--include=${p.glob}`] : []), "-e", pattern, "--", target];
      const found = await run(have.code === 0 ? "rg" : "grep", args);
      if (found.code > 1) throw new Error("search failed");
      const redacted = await redact(found.stdout, ["--grep", "--cwd", process.cwd()]);
      const max = Math.max(1, Math.min(2000, Math.floor(Number(p.max_lines) || 200)));
      const lines = redacted.split("\n").filter(Boolean);
      const body = lines.slice(0, max).map((l) => (l.length > 400 ? `${l.slice(0, 400)}…` : l)).join("\n");
      return clip((body || "(no matches)") + (lines.length > max ? `\n…(${lines.length - max} more lines)` : ""));
    },
  },
  reveal: {
    description:
      `Ask the user to let you see the real value behind one ${ph("...")} placeholder. The user is prompted every time and sees ` +
      "the placeholder, where it came from and your reason, never the value. Only use this when the literal value itself is needed in " +
      "your reasoning; to USE a value (in a command or an edit) just write the placeholder. A revealed value leaves this machine.",
    schema: { type: "object", required: ["placeholder", "reason"], properties: { placeholder: str(`e.g. ${ph("clerk_secret_1")}`), reason: str("Why the literal value is needed") } },
    meta: { "anthropic/requiresUserInteraction": true },
    async run(p) {
      const placeholder = String(p.placeholder ?? "");
      if (!PLACEHOLDER.test(placeholder)) throw new Error(`placeholder must look like ${ph("kind_1")}`);
      if (!String(p.reason ?? "").trim()) throw new Error("reason is required");
      const r = await run(REDACT, ["--reveal", placeholder]);
      if (r.code !== 0) throw new Error(`cannot recover ${placeholder}; its source may have changed. Read the file again.`);
      return `${placeholder} = ${r.stdout}`;
    },
  },
};

const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
async function handle(msg) {
  const { id, method, params } = msg;
  if (id === undefined) return; // notifications
  if (method === "initialize")
    return send({ id, result: { protocolVersion: params?.protocolVersion ?? "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "tmux-pi-safe", version: "0.1.0" } } });
  if (method === "ping") return send({ id, result: {} });
  if (method === "tools/list")
    return send({ id, result: { tools: Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.description, inputSchema: t.schema, ...(t.meta ? { _meta: t.meta } : {}) })) } });
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
createInterface({ input: process.stdin }).on("line", (l) => { try { handle(JSON.parse(l)); } catch {} });
