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
 *
 * Settings (environment):
 *   TP_SENSITIVE_READ_BIN         classifier used to decide which cited paths are withheld (default bin/tp-sensitive-read).
 *   TP_SENSITIVE_READ_TIMEOUT_MS  milliseconds the classifier gets per path (default 5000). On timeout or any
 *                                 error the path is treated as sensitive: withheld and masked, never hashed.
 *
 * Substantial work is dispatched through orchestration.ts and tp-agent.
 */

import { execFile, execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { closeSync, lstatSync, mkdtempSync, openSync, readFileSync, readSync, realpathSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
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

const MAX_CITED = 50; // paths hashed and listed in one manifest; the rest are counted in `omitted`
const MAX_CLASSIFIED = 200; // cited names run through the sensitive classifier; one past this is masked, never named
const MAX_BACK_WORDS = 8; // words an unquoted citation may reach back over to find a path with spaces
const WITHHELD_MARK = "[withheld path]";
const NOT_SAVED = "full reply not saved";

/** Cut `text` to at most `max` characters, at a line boundary when the cut has one; `midLine` says it did not. */
export function cutAtLine(text: string, max: number): { text: string; midLine: boolean } {
	if (text.length <= max) return { text, midLine: false };
	const head = text.slice(0, max);
	const nl = head.lastIndexOf("\n");
	return nl > 0 ? { text: head.slice(0, nl), midLine: false } : { text: head, midLine: true };
}

export type Span = { start: number; end: number };
export type Cite = { path: string; lines: string[]; spans: Span[]; spaced: boolean };

const LINES = String.raw`(?::(\d+)(?:[-–:](\d+))?|#L(\d+)(?:-L?(\d+))?)?`;
const PATH_RE = new RegExp(String.raw`(?<=^|[\s` + "`" + String.raw`'"(\[<])(\/?(?:[\w@.+-]+\/)+[\w@.+-]+|[\w@.+-]*\.[A-Za-z0-9]{1,8})` + LINES, "gm");
// A quoted name with a space in it: "src/my file.py:8", `my dir/a.py`.
const QUOTED_RE = new RegExp("([\"`'])([^\"`'\\n]* [^\"`'\\n]*)\\1" + LINES, "g");

/**
 * The file paths a reply cites, with the line ranges it gives for each, in first-seen order, and where each
 * citation sits in the reply. A path with spaces is taken when the reply quotes it, or (given `exists`) when
 * it names a file that is there; a quoted one is returned with `spaced` set whether or not it is there.
 */
export function citedPaths(reply: string, exists?: (path: string) => boolean): Cite[] {
	const found = new Map<string, Cite>();
	const events: { at: number; run: () => void }[] = [];
	const add = (path: string, first: string | undefined, last: string | undefined, span: Span, spaced: boolean) => {
		const cite = found.get(path) ?? { path, lines: [], spans: [], spaced };
		const range = first ? (last && last !== first ? `${first}-${last}` : first) : undefined;
		if (range && !cite.lines.includes(range)) cite.lines.push(range);
		cite.spans.push(span);
		found.set(path, cite);
	};
	const quoted: Span[] = [];
	for (const m of reply.matchAll(QUOTED_RE)) {
		// The :line, :line:col or :line-line may sit inside the quotes; take it off before the name is judged.
		const tail = m[2].trim().match(/^(.*?)(?::(\d+)(?:(?:[-–](\d+))|(?::\d+))?)?$/)!;
		const inner = tail[1];
		const looksLikePath = inner.includes("/") || /\.[A-Za-z0-9]{1,8}$/.test(inner);
		if (!looksLikePath || !/^[\w@.+\-/ ()]+$/.test(inner)) continue;
		const span = { start: m.index! + 1, end: m.index! + 1 + m[2].length };
		quoted.push({ start: m.index!, end: m.index! + m[0].length });
		events.push({ at: m.index!, run: () => add(inner, tail[2] ?? m[3] ?? m[5], tail[3] ?? m[4] ?? m[6], span, true) });
	}
	const plain: { span: Span; run: () => void }[] = [];
	for (const m of reply.matchAll(PATH_RE)) {
		if (quoted.some((q) => m.index! >= q.start && m.index! < q.end)) continue;
		let path = m[1].replace(/[.,;:]+$/, "");
		if (!path || path === "." || path === "..") continue;
		let start = m.index!;
		const end = m.index! + m[0].length;
		if (exists) {
			// Reach back over earlier words on the line; the longest stretch that names a file here is the citation.
			const lineStart = reply.lastIndexOf("\n", start - 1) + 1;
			const words = [...reply.slice(lineStart, start).matchAll(/\S+/g)].slice(-MAX_BACK_WORDS);
			for (let k = words.length; k >= 1; k--) {
				const from = lineStart + words[words.length - k].index!;
				const raw = reply.slice(from, start + path.length);
				const text = raw.replace(/^[`'"(\[<]+/, "");
				if (exists(text)) { start = from + raw.length - text.length; path = text; break; }
			}
		}
		const span = { start, end };
		plain.push({ span, run: () => add(path, m[2] ?? m[4], m[3] ?? m[5], span, false) });
	}
	// "src/my file.py" is one citation: drop the pieces ("src/my") that the longer match swallowed.
	for (const p of plain) {
		if (!plain.some((o) => o !== p && o.span.start <= p.span.start && o.span.end >= p.span.end && o.span.end - o.span.start > p.span.end - p.span.start)) {
			events.push({ at: p.span.start, run: p.run });
		}
	}
	for (const e of events.sort((x, y) => x.at - y.at)) e.run();
	return [...found.values()];
}

export type Manifest = {
	paths: { path: string; lines?: string[]; content_hash: string }[];
	withheld: number;
	omitted: number;
	unresolved: number;
	truncated: true;
	bytes_total: number;
	artifact?: string; // absent when the full reply could not be saved
	error?: string; // set exactly then
};

/** SHA-256 of a file's current content, read in chunks so its size does not matter. */
function hashFile(file: string): string {
	const hash = createHash("sha256");
	const buf = Buffer.allocUnsafe(1 << 20);
	const fd = openSync(file, "r");
	try {
		for (let n; (n = readSync(fd, buf, 0, buf.length, null)) > 0;) hash.update(buf.subarray(0, n));
	} finally { closeSync(fd); }
	return "sha256:" + hash.digest("hex");
}

/**
 * Build the manifest for an over-cap reply. Every cited name is classified, including one that is not on disk.
 * One the sensitive classifier flags, or cannot classify, is counted in `withheld` and never read, hashed or named;
 * `withheldSpans` says where it sits in the reply so the excerpt can mask it. Hashes come from the working tree now.
 * Nothing is dropped silently: past the cap, `omitted` counts the readable files left out and `unresolved` the
 * citations that could not be read or found.
 */
export function buildManifest(reply: string, cwd: string, artifact: string | undefined, sensitive: (path: string) => boolean):
	{ manifest: Manifest; withheldSpans: Span[] } {
	const resolveIn = (p: string) => (isAbsolute(p) ? p : resolve(cwd, p));
	const isThere = (p: string) => { try { lstatSync(resolveIn(p)); return true; } catch { return false; } };
	const paths: Manifest["paths"] = [];
	const withheldSpans: Span[] = [];
	let withheld = 0, omitted = 0, unresolved = 0, classified = 0;
	for (const cite of citedPaths(reply, isThere)) {
		const abs = resolveIn(cite.path);
		const exists = isThere(cite.path);
		let flagged = ++classified > MAX_CLASSIFIED || sensitive(abs);
		let real = abs;
		if (!flagged && exists) {
			try { real = realpathSync(abs); } catch { unresolved++; continue; }
			flagged = real !== abs && sensitive(real);
		}
		if (flagged) { withheld++; withheldSpans.push(...cite.spans); continue; }
		if (!exists) {
			// A bare name that is not here is prose ("e.g.", "v1.2"); a quoted or directory-qualified one is a citation we could not find.
			if (cite.spaced || cite.path.includes("/")) unresolved++;
			continue;
		}
		let content_hash: string;
		try {
			if (!lstatSync(real).isFile()) continue;
			if (paths.length >= MAX_CITED) { omitted++; continue; }
			content_hash = hashFile(real);
		} catch { unresolved++; continue; }
		paths.push({ path: cite.path, ...(cite.lines.length ? { lines: cite.lines } : {}), content_hash });
	}
	return { manifest: { paths, withheld, omitted, unresolved, truncated: true, bytes_total: Buffer.byteLength(reply), ...(artifact ? { artifact } : { error: NOT_SAVED }) }, withheldSpans };
}

/** Replace each span of `text` (offsets into the full reply) with a fixed placeholder; spans past its end are ignored. */
export function maskSpans(text: string, spans: Span[]): string {
	let out = text;
	for (const { start, end } of [...spans].sort((a, b) => b.start - a.start)) {
		if (start >= out.length) continue;
		out = out.slice(0, start) + WITHHELD_MARK + out.slice(Math.min(end, out.length));
	}
	return out;
}

const GUIDANCE =
    "\n\nFor a small read or search, ask_local can return paths, line numbers and snippets without growing your context. " +
    "Dispatch substantial investigation, edits, testing, review, or design with dispatch_role and collect with task_collect.";

/**
 * Write an over-cap reply to disk, mode 0600: under the session state dir's evidence/ folder, else in a private
 * per-call folder under the OS temp dir. Returns the path, or undefined when neither place takes it.
 */
async function saveReply(reply: string, state: string | undefined): Promise<string | undefined> {
	const name = `ask-local-${Date.now()}-${randomUUID().slice(0, 8)}.md`;
	if (state) {
		try {
			// Loaded here so a reply under the cap needs nothing beyond this file.
			const { ensureDir, writeFileAtomic } = await import("./lib/state-write.ts");
			const evidence = join(state, "evidence");
			ensureDir(state, evidence);
			const file = join(evidence, name);
			writeFileAtomic(state, file, reply, 0o600);
			return file;
		} catch { /* fall through to the temp dir */ }
	}
	try {
		const file = join(mkdtempSync(join(tmpdir(), "tp-ask-local-")), name); // mkdtemp makes the folder 0700
		writeFileSync(file, reply, { mode: 0o600, flag: "wx" });
		return file;
	} catch { return undefined; }
}

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

	// Same classifier as the worker gate; TP_SENSITIVE_READ_BIN lets tests stub it, TP_SENSITIVE_READ_TIMEOUT_MS sets its time limit. Fails closed.
	const isSensitive = (cwd: string) => (path: string): boolean => {
		try {
			const answer = JSON.parse(execFileSync(process.env.TP_SENSITIVE_READ_BIN || join(root, "bin/tp-sensitive-read"), [], {
				input: JSON.stringify({ tool_name: "read", tool_input: { path }, cwd }), encoding: "utf8", timeout: Number(process.env.TP_SENSITIVE_READ_TIMEOUT_MS) || 5000,
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
				// Over the cap: one path. Save the full reply, then mask what the guard refuses, then return.
				const artifact = await saveReply(out, stateDir());
				const cut = cutAtLine(out, MAX_REPLY);
				const { manifest, withheldSpans } = buildManifest(out, ctx.cwd, artifact, isSensitive(ctx.cwd));
				// A path the guard refuses is not advertised in the excerpt; the saved reply keeps it on disk.
				const how = cut.midLine ? "truncated, cut mid-line" : "truncated at a line boundary";
				const label = artifact ? `${how}; the full reply is saved at ${artifact}` : `${how}; ${NOT_SAVED}, ask a narrower question`;
				const text = `${maskSpans(cut.text, withheldSpans)}\n[${label}]\nevidence manifest: ${JSON.stringify(manifest)}`;
				return { content: [{ type: "text" as const, text }], details: { model: assistant.model, manifest } };
			} catch (error) {
				throw new Error(`local assistant unavailable (${(error as Error).message}); do this yourself with read/grep/find`);
			}
		},
	});

}
