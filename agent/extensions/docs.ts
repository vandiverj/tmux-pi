/**
 * docs — offline, read-only library docs for pi workers: `api_signature` and `docs_search`.
 *
 * The pi twin of claude/mcp/docs.mjs, loaded by bin/tp-pi only when TP_DOCS_TOOLS=1. Both tools run `bin/tp-docs --json`,
 * which answers from allowlisted local files (.d.ts/.d.mts/.d.cts/.md under the pinned package or the index), caps the
 * answer at 8 KB and logs every call to $TP_TASK_DIR/docs-calls.jsonl. This extension opens no sockets and reads no
 * files; if tp-docs cannot log (no TP_TASK_DIR) it answers nothing and the tool call fails.
 */
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Ran = { code: number; stdout: string; stderr: string };

export default function (pi: ExtensionAPI) {
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!agentDir) return;
	const tpDocs = join(dirname(agentDir), "bin", "tp-docs");

	const run = (args: string[], cwd: string, signal?: AbortSignal): Promise<Ran> =>
		new Promise((done) => {
			execFile(tpDocs, args, { timeout: 60000, maxBuffer: 1024 * 1024, cwd, encoding: "utf8", env: process.env, signal }, (err, stdout, stderr) =>
				done({ code: err ? (typeof err.code === "number" ? err.code : 1) : 0, stdout: String(stdout), stderr: String(stderr) }));
		});
	// `tp-docs --json` prints {status,text,version,provenance}; a non-zero exit means it could not answer (for instance, could not log).
	const ask = async (args: string[], ctx: { cwd?: unknown } | undefined, signal?: AbortSignal) => {
		const cwd = typeof ctx?.cwd === "string" && ctx.cwd ? ctx.cwd : process.cwd();
		const r = await run(["--json", ...args], cwd, signal);
		if (r.code !== 0) throw new Error(r.stderr.trim() || `tp-docs exited ${r.code}`);
		return { content: [{ type: "text" as const, text: String(JSON.parse(r.stdout).text) }], details: undefined };
	};

	pi.registerTool({
		name: "api_signature",
		label: "API signature",
		description:
			"Exact TypeScript declaration (with its doc comment) of a symbol from an installed npm package, read offline from the " +
			"package's own .d.ts files at the version the project's lockfile pins. Use it instead of guessing an API from memory. " +
			"`symbol` may be dotted (Namespace.member, Class.method). No lockfile entry means no answer.",
		parameters: Type.Object({
			package: Type.String({ description: "npm package name, e.g. zod or @scope/name (or npm:name)" }),
			symbol: Type.String({ description: "Exported name, e.g. z.object or Client.connect" }),
		}),
		async execute(_id, p, signal, _onUpdate, ctx) {
			return ask(["signature", String(p.package ?? ""), String(p.symbol ?? "")], ctx, signal);
		},
	});

	pi.registerTool({
		name: "docs_search",
		label: "Docs search",
		description:
			"Search the local docs index of a library (built from its docs at the lockfile-pinned version). Returns the best matching " +
			"sections with file:line. Offline; no result when the library has no index for the pinned version.",
		parameters: Type.Object({
			lib: Type.String({ description: "npm package name the docs belong to" }),
			query: Type.String({ description: "What to look for" }),
			limit: Type.Optional(Type.Number({ description: "Max results, 1-10 (default 5)" })),
		}),
		async execute(_id, p, signal, _onUpdate, ctx) {
			const limit = p.limit ? ["--limit", String(Math.floor(Number(p.limit)))] : [];
			return ask(["search", String(p.lib ?? ""), String(p.query ?? ""), ...limit], ctx, signal);
		},
	});
}
