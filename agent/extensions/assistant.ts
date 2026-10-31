/**
 * assistant — the local model as an optional short read/search assistant for the orchestrator.
 *
 * Active only in the main agent outside explicitly launched legacy strict mode.
 *
 *   ask_local    main hands a read/search job to the local model (config/local.json
 *                "assistant", default LM Studio Qwen). It runs fresh and read-only
 *                (read, grep, find, ls) in the working directory and returns a digest:
 *                paths, line numbers and verbatim snippets. Main keeps the thinking and
 *                the knowledge; the local model's context never accumulates, so its
 *                window never compacts mid-task.
 * Substantial work is dispatched through orchestration.ts and tp-agent.
 */

import { execFile, execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function readJson(file: string): any {
	try {
		return JSON.parse(readFileSync(file, "utf8"));
	} catch {
		return {};
	}
}

const ASSISTANT_PROMPT =
	"You are a read-only research assistant for a stronger model that will do the thinking. " +
	"Do the legwork with your tools (read, grep, find, ls). Read or search files outside the working directory " +
	"with safe_read and safe_grep, which redact credentials without a prompt. Reply with exact file paths, line numbers and " +
	"verbatim relevant snippets. When asked to read files, quote the relevant text with line numbers " +
	"rather than paraphrasing it; summarize only the remainder. Say plainly when you did not find something. " +
	"No opinions, no padding.";
const MAX_REPLY = 24_000;

const MAX_CITED = 50; // paths examined and listed in one manifest
const MAX_HASH_BYTES = 64 * 1024 * 1024;

/** Cut `text` to at most `max` characters, at a line boundary when the cut has one. */
export function cutAtLine(text: string, max: number): string {
	if (text.length <= max) return text;
	const head = text.slice(0, max);
	const nl = head.lastIndexOf("\n");
	return nl > 0 ? head.slice(0, nl) : head;
}

export type Cited = { path: string; lines: string[] };

const PATH_RE = /(?<=^|[\s`'"(\[<])(\/?(?:[\w@.+-]+\/)+[\w@.+-]+|[\w@.+-]*\.[A-Za-z0-9]{1,8})(?::(\d+)(?:[-–:](\d+))?|#L(\d+)(?:-L?(\d+))?)?/gm;

/** The file paths a reply cites, with the line ranges it gives for each, in first-seen order. */
export function citedPaths(reply: string): Cited[] {
	const found = new Map<string, Set<string>>();
	for (const m of reply.matchAll(PATH_RE)) {
		const path = m[1].replace(/[.,;:]+$/, "");
		if (!path || path === "." || path === "..") continue;
		const first = m[2] ?? m[4], last = m[3] ?? m[5];
		const lines = found.get(path) ?? new Set<string>();
		if (first) lines.add(last && last !== first ? `${first}-${last}` : first);
		found.set(path, lines);
	}
	return [...found].map(([path, lines]) => ({ path, lines: [...lines] }));
}

export type Manifest = {
	paths: { path: string; lines?: string[]; content_hash: string | null }[];
	withheld: number;
	truncated: true;
	bytes_total: number;
	artifact: string;
};

/**
 * Build the manifest for an over-cap reply. A cited path the sensitive classifier flags, or cannot classify,
 * is counted in `withheld` and never read, hashed or named. Hashes come from the working tree now.
 */
export function buildManifest(reply: string, cwd: string, artifact: string, sensitive: (path: string) => boolean): Manifest {
	const paths: Manifest["paths"] = [];
	let withheld = 0;
	for (const cite of citedPaths(reply).slice(0, MAX_CITED)) {
		const abs = isAbsolute(cite.path) ? cite.path : resolve(cwd, cite.path);
		let exists = true;
		try { lstatSync(abs); } catch { exists = false; }
		// A name with no directory part that is not here is prose ("e.g.", "v1.2"), not a citation.
		if (!exists && !cite.path.includes("/")) continue;
		if (sensitive(abs)) { withheld++; continue; }
		if (!exists) continue;
		let real: string;
		try { real = realpathSync(abs); } catch { continue; }
		if (real !== abs && sensitive(real)) { withheld++; continue; }
		let content_hash: string | null = null;
		try {
			const stat = lstatSync(real);
			if (!stat.isFile()) continue;
			if (stat.size <= MAX_HASH_BYTES) content_hash = "sha256:" + createHash("sha256").update(readFileSync(real)).digest("hex");
		} catch { continue; }
		paths.push({ path: cite.path, ...(cite.lines.length ? { lines: cite.lines } : {}), content_hash });
	}
	return { paths, withheld, truncated: true, bytes_total: Buffer.byteLength(reply), artifact };
}

const GUIDANCE =
    "\n\nFor a small read or search, ask_local can return paths, line numbers and snippets without growing your context. " +
    "Dispatch substantial investigation, edits, testing, review, or design with dispatch_role and collect with task_collect.";

function run(cmd: string, args: string[], cwd: string, signal?: AbortSignal): Promise<string> {
	// The child is not the orchestrator main: without TP_MODE, bin/tp-pi adds neither codemode nor a second --tools.
	const env = { ...process.env };
	delete env.TP_MODE;
	return new Promise((res, rej) => {
		const child = execFile(cmd, args, { cwd, env, timeout: 240_000, maxBuffer: 8_000_000, signal }, (error, stdout, stderr) =>
			error ? rej(new Error(String(stderr || error.message).trim().slice(-500))) : res(String(stdout)));
		child.stdin?.end();
	});
}

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const root = dirname(process.env.PI_CODING_AGENT_DIR);
	const local = readJson(join(root, "config/local.json"));
	if (process.env.TP_MODE === "strict") return;

	// Same classifier as the worker gate; TP_SENSITIVE_READ_BIN lets tests stub it. Fails closed.
	const isSensitive = (cwd: string) => (path: string): boolean => {
		try {
			const answer = JSON.parse(execFileSync(process.env.TP_SENSITIVE_READ_BIN || join(root, "bin/tp-sensitive-read"), [], {
				input: JSON.stringify({ tool_name: "read", tool_input: { path }, cwd }), encoding: "utf8", timeout: 5000,
			}));
			return answer.sensitive !== false;
		} catch { return true; }
	};
	// The session state dir, as reporter.ts finds it: TP_STATE_DIR, else the tmux session's @tp_state_dir.
	const stateDir = (): string | undefined => {
		if (process.env.TP_STATE_DIR) return process.env.TP_STATE_DIR;
		try {
			const session = execFileSync("tmux", ["display-message", "-p", ...(process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : []), "#S"], { encoding: "utf8" }).trim();
			return execFileSync("tmux", ["show-options", "-qv", "-t", `=${session}:`, "@tp_state_dir"], { encoding: "utf8" }).trim() || undefined;
		} catch { return undefined; }
	};

	const assistant = { model: "lmstudio/qwen/qwen3.6-35b-a3b", thinking: "off", ...(local.assistant ?? {}) };

	pi.on("before_agent_start", async (event) => ({ systemPrompt: event.systemPrompt + GUIDANCE }));

	pi.registerTool({
		name: "ask_local",
		label: "Ask local assistant",
		description:
			"Have the local assistant model do read-only legwork and report back: find files, search the code, " +
			"read one or more files and quote the relevant parts. It starts fresh each call and knows nothing " +
			"of this conversation, so make the task self-contained (what to find, which paths, what to quote). " +
			"It returns exact paths, line numbers and verbatim snippets. It cannot edit or run commands.",
		parameters: Type.Object({
			task: Type.String({ description: "Self-contained job: what to find or read, where to look, what to quote back" }),
		}),
		async execute(_id, params, signal, _onUpdate, ctx) {
			const args = ["--no-session", "--no-extensions", "--no-context-files", "-p", "--model", assistant.model,
				"--thinking", assistant.thinking, "--tools", "read,grep,find,ls,safe_read,safe_grep", "--append-system-prompt", ASSISTANT_PROMPT, params.task];
			try {
				const out = (await run(join(root, "bin/tp-pi"), args, ctx.cwd, signal)).trim();
				if (out.length <= MAX_REPLY) {
					return { content: [{ type: "text" as const, text: out || "(the local assistant returned nothing)" }], details: { model: assistant.model } };
				}
				// Over the cap: keep the whole reply on disk and return an excerpt plus a manifest of what it cites.
				const state = stateDir();
				let artifact: string | undefined;
				if (state) {
					try {
						// Loaded here so a reply under the cap needs nothing beyond this file.
						const { ensureDir, writeFileAtomic } = await import("./lib/state-write.ts");
						const evidence = join(state, "evidence");
						ensureDir(state, evidence);
						artifact = join(evidence, `ask-local-${Date.now()}-${randomUUID().slice(0, 8)}.md`);
						writeFileAtomic(state, artifact, out, 0o600);
					} catch { artifact = undefined; }
				}
				const head = cutAtLine(out, MAX_REPLY);
				if (!artifact) {
					return { content: [{ type: "text" as const, text: head + "\n[truncated; ask a narrower question]" }], details: { model: assistant.model } };
				}
				const manifest = buildManifest(out, ctx.cwd, artifact, isSensitive(ctx.cwd));
				const text = `${head}\n[truncated at a line boundary; the full reply is saved at ${artifact}]\n` +
					`evidence manifest: ${JSON.stringify(manifest)}`;
				return { content: [{ type: "text" as const, text }], details: { model: assistant.model, manifest } };
			} catch (error) {
				throw new Error(`local assistant unavailable (${(error as Error).message}); do this yourself with read/grep/find`);
			}
		},
	});

}
