#!/usr/bin/env node
// ctx MCP server — the project context tools: ctx_brief, ctx_symbol, ctx_relations, ctx_knowledge, ctx_note.
// Added to Claude only when TP_CTX_TOOLS=1 (bin/tp-claude passes claude/mcp.ctx.json); started without it this process exits
// at once and offers nothing. It opens no sockets and reads no files itself: every tool runs `bin/tp-ctx --json <tool> --input
// <json>`, which checks the arguments and every path, answers in at most 6 KB, and logs the call to $TP_TASK_DIR/ctx-calls.jsonl.
// If tp-ctx cannot log it answers nothing and the tool call fails. The field table below is a copy of `tp-ctx ctx_schema`;
// tests/test_ctx_tools_wiring.py compares it with that and with agent/extensions/ctx.ts, so the two surfaces cannot drift.
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createInterface } from "node:readline";

if (process.env.TP_CTX_TOOLS !== "1") {
  process.stderr.write("ctx MCP server: TP_CTX_TOOLS is not 1; nothing is offered\n");
  process.exit(1);
}

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const TP_CTX = join(ROOT, "bin", "tp-ctx");

// <schema>
const SCHEMA = {
  "cap": 6144,
  "tools": {
    "ctx_brief": {
      "description": "Brief on 1-5 files before you change them: size, tests, exports, who imports them, shared contracts, the project's notes about them and the libraries they use. A fixed shape; read it instead of opening each file to find out how it fits.",
      "fields": {
        "paths": {
          "description": "1 to 5 project files to brief (relative to the project root)",
          "item_max": 300,
          "max": 5,
          "min": 1,
          "required": true,
          "type": "array"
        }
      }
    },
    "ctx_knowledge": {
      "description": "Search the project's approved notes (conventions, framework rules, decisions, lessons) by words, files or tags.",
      "fields": {
        "include_candidates": {
          "description": "also return unapproved candidate notes, marked as such",
          "type": "boolean"
        },
        "limit": {
          "description": "most notes to return (default 5)",
          "max": 10,
          "min": 1,
          "type": "integer"
        },
        "paths": {
          "description": "return notes whose scope covers these files",
          "item_max": 300,
          "max": 5,
          "min": 0,
          "type": "array"
        },
        "query": {
          "description": "words to search the notes for",
          "max": 200,
          "min": 0,
          "type": "string"
        },
        "tags": {
          "description": "return notes with one of these tags",
          "item_max": 40,
          "max": 5,
          "min": 0,
          "type": "array"
        }
      }
    },
    "ctx_note": {
      "description": "Offer a candidate note about the project for a person to review. Needs file:line evidence. At most 5 per task; it is never approved by you.",
      "fields": {
        "evidence": {
          "description": "file:line places that show it is true",
          "item_max": 300,
          "max": 10,
          "min": 1,
          "required": true,
          "type": "array"
        },
        "kind": {
          "description": "what sort of note",
          "enum": [
            "convention",
            "framework",
            "relation",
            "decision",
            "lesson"
          ],
          "required": true,
          "type": "string"
        },
        "scope": {
          "description": "globs of the files the note applies to",
          "item_max": 200,
          "max": 10,
          "min": 1,
          "required": true,
          "type": "array"
        },
        "tags": {
          "description": "tags",
          "item_max": 40,
          "max": 5,
          "min": 0,
          "type": "array"
        },
        "text": {
          "description": "the note, one rule or fact with the reason",
          "max": 800,
          "min": 1,
          "required": true,
          "type": "string"
        },
        "title": {
          "description": "a one-line title",
          "max": 100,
          "min": 1,
          "type": "string"
        }
      }
    },
    "ctx_relations": {
      "description": "Who imports a file (importers), what it imports (imports), or the shared contracts it uses and who else uses them (contracts).",
      "fields": {
        "direction": {
          "description": "importers: who imports it; imports: what it imports; contracts: shared types it uses and who else uses them",
          "enum": [
            "importers",
            "imports",
            "contracts"
          ],
          "required": true,
          "type": "string"
        },
        "limit": {
          "description": "most edges to show (default 10)",
          "max": 25,
          "min": 1,
          "type": "integer"
        },
        "target": {
          "description": "a project file path, or the name of a symbol that is defined once",
          "max": 300,
          "min": 1,
          "required": true,
          "type": "string"
        }
      }
    },
    "ctx_symbol": {
      "description": "Where a symbol is defined, with its signature, doc and a few references.",
      "fields": {
        "name": {
          "description": "the symbol, a bare identifier",
          "max": 100,
          "min": 1,
          "pattern": "bare identifier",
          "required": true,
          "type": "string"
        },
        "package": {
          "description": "limit the search for the definition to one package",
          "enum": [
            "web",
            "api",
            "mc",
            "lib"
          ],
          "type": "string"
        },
        "path": {
          "description": "the file that defines it, when known (relative to the project root)",
          "max": 300,
          "min": 1,
          "type": "string"
        }
      }
    }
  }
};
// </schema>

const property = (f) => {
  const out = { description: f.description };
  if (f.type === "array") {
    out.type = "array";
    out.items = { type: "string", minLength: 1, maxLength: f.item_max };
    if (f.min) out.minItems = f.min;
    out.maxItems = f.max;
  } else if (f.type === "integer") {
    Object.assign(out, { type: "integer", minimum: f.min, maximum: f.max });
  } else if (f.type === "boolean") {
    out.type = "boolean";
  } else {
    out.type = "string";
    if (f.enum) out.enum = f.enum;
    else {
      if (f.min) out.minLength = f.min;
      out.maxLength = f.max;
    }
  }
  return out;
};
const TOOLS = Object.fromEntries(
  Object.entries(SCHEMA.tools).map(([name, tool]) => [
    name,
    {
      description: tool.description,
      schema: {
        type: "object",
        required: Object.entries(tool.fields).filter(([, f]) => f.required).map(([key]) => key),
        properties: Object.fromEntries(Object.entries(tool.fields).map(([key, f]) => [key, property(f)])),
      },
    },
  ]),
);

const run = (args) =>
  new Promise((done) =>
    execFile(TP_CTX, args, { timeout: 90000, maxBuffer: 1024 * 1024, cwd: process.cwd(), env: process.env }, (err, stdout, stderr) =>
      done({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) })));

// `tp-ctx --json` prints {status,text,truncated,left_out}. A refusal or a bad argument is an answer (exit 1 or 2, with text);
// no stdout at all (exit 3: it could not log, or no project) means no answer, and the tool call fails.
const ask = async (tool, input) => {
  const r = await run(["--json", tool, "--input", JSON.stringify(input ?? {})]);
  let reply = null;
  try {
    reply = JSON.parse(r.stdout);
  } catch {
    reply = null;
  }
  if (!reply || typeof reply.text !== "string") throw new Error(r.stderr.trim() || `tp-ctx exited ${r.code}`);
  return reply.text;
};

const send = (m) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...m }) + "\n");
async function handle(msg) {
  const { id, method, params } = msg;
  if (id === undefined) return; // notifications
  if (method === "initialize")
    return send({ id, result: { protocolVersion: params?.protocolVersion ?? "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "tmux-pi-ctx", version: "0.1.0" } } });
  if (method === "ping") return send({ id, result: {} });
  if (method === "tools/list")
    return send({ id, result: { tools: Object.entries(TOOLS).map(([name, t]) => ({ name, description: t.description, inputSchema: t.schema })) } });
  if (method === "tools/call") {
    const tool = TOOLS[params?.name];
    if (!tool) return send({ id, result: { isError: true, content: [{ type: "text", text: `unknown tool ${params?.name}` }] } });
    try {
      return send({ id, result: { content: [{ type: "text", text: await ask(params.name, params.arguments ?? {}) }] } });
    } catch (e) {
      return send({ id, result: { isError: true, content: [{ type: "text", text: String(e.message ?? e) }] } });
    }
  }
  send({ id, error: { code: -32601, message: `method not found: ${method}` } });
}
// One request at a time, in arrival order: calls (and the lines they log) happen in the order the client sent them.
let queue = Promise.resolve();
createInterface({ input: process.stdin }).on("line", (l) => {
  queue = queue.then(() => handle(JSON.parse(l))).catch(() => {});
});
