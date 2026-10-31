/**
 * ctx — the project context tools for pi workers: ctx_brief, ctx_symbol, ctx_relations, ctx_knowledge, ctx_note.
 *
 * The pi twin of claude/mcp/ctx.mjs, and loaded only when TP_CTX_TOOLS=1 (without it this extension registers nothing). Every
 * tool runs `bin/tp-ctx --json <tool> --input <json>`, which checks the arguments and every path, answers in at most 6 KB, and
 * logs the call to $TP_TASK_DIR/ctx-calls.jsonl. If tp-ctx cannot log it answers nothing and the tool call fails. This extension
 * opens no sockets and reads no files; the field table below is a copy of `tp-ctx ctx_schema`, which tests/test_ctx_tools_wiring.py
 * compares with it (and with the MCP server's copy).
 */
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { StringEnum, Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

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

type Field = { type: string; description: string; required?: boolean; min?: number; max?: number; item_max?: number; enum?: string[]; pattern?: string };
type Ran = { code: number; stdout: string; stderr: string };

const field = (f: Field) => {
	const description = f.description;
	if (f.enum) return StringEnum(f.enum as [string, ...string[]], { description });
	if (f.type === "integer") return Type.Integer({ minimum: f.min, maximum: f.max, description });
	if (f.type === "boolean") return Type.Boolean({ description });
	if (f.type === "array") return Type.Array(Type.String({ minLength: 1, maxLength: f.item_max }), { minItems: f.min || undefined, maxItems: f.max, description });
	return Type.String({ minLength: f.min || undefined, maxLength: f.max, description });
};

export default function (pi: ExtensionAPI) {
	if (process.env.TP_CTX_TOOLS !== "1") return;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!agentDir) return;
	const tpCtx = join(dirname(agentDir), "bin", "tp-ctx");

	const run = (args: string[], cwd: string, signal?: AbortSignal): Promise<Ran> =>
		new Promise((done) => {
			execFile(tpCtx, args, { timeout: 90000, maxBuffer: 1024 * 1024, cwd, encoding: "utf8", env: process.env, signal }, (err, stdout, stderr) =>
				done({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) }));
		});
	// `tp-ctx --json` prints {status,text,truncated,left_out}. A refusal or a bad argument is an answer (exit 1 or 2, with text);
	// no stdout at all (exit 3: it could not log, or no project) means no answer, and the tool call fails.
	const ask = async (tool: string, input: unknown, ctx: { cwd?: unknown } | undefined, signal?: AbortSignal) => {
		const cwd = typeof ctx?.cwd === "string" && ctx.cwd ? ctx.cwd : process.cwd();
		const r = await run(["--json", tool, "--input", JSON.stringify(input ?? {})], cwd, signal);
		let reply: { text?: unknown } | null = null;
		try {
			reply = JSON.parse(r.stdout);
		} catch {
			reply = null;
		}
		if (!reply || typeof reply.text !== "string") throw new Error(r.stderr.trim() || `tp-ctx exited ${r.code}`);
		return { content: [{ type: "text" as const, text: reply.text }], details: undefined };
	};

	for (const [name, tool] of Object.entries(SCHEMA.tools)) {
		const fields = tool.fields as Record<string, Field>;
		const props: Record<string, any> = {};
		for (const [key, f] of Object.entries(fields)) props[key] = f.required ? field(f) : Type.Optional(field(f));
		pi.registerTool({
			name,
			label: name.replace("ctx_", "Context "),
			description: tool.description,
			parameters: Type.Object(props),
			async execute(_id, p, signal, _onUpdate, ctx) {
				return ask(name, p, ctx, signal);
			},
		});
	}
}
