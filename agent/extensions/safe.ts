/**
 * safe — redacted reads and searches for files outside the working tree, without a permission prompt.
 *
 * The pi twin of claude/mcp/safe.mjs (minus `reveal`: a real value stays the user's decision). safe_read and safe_grep
 * pipe what they find through bin/tp-redact before it leaves this process, in every redaction mode, so values come back
 * as placeholders from the same session store as the tool_result handler in sensitive.ts. If the redactor does not
 * succeed nothing from the file is returned. The trust-store guard in sensitive.ts judges both calls first.
 */
import { execFile } from "node:child_process";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { userInfo } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { redactionSession } from "./lib/redaction.ts";

const MAX_FILE = 4_000_000;
const MAX_CHARS = 200_000;
const ph = (name: string) => `<REDACTED:${name}>`;
// The tool descriptions state the read/search rule of agent/contract/worker.md in the same words as claude/mcp/safe.mjs;
// only READ and SHELL name this harness's tools (tests/test_pi_safe_tools.py compares the two templates).
const READ = "the read tool";
const SHELL = "bash";
const SAFE_READ_HELP =
	`Read a file with secrets replaced by ${ph("kind_n")} placeholders. Use this or ${READ} on an exact path; use this one for ` +
	"files outside the working tree and files that may hold secrets (.env*, keys, credentials such as ~/.aws/credentials): it " +
	`needs no permission prompt. Refuses a directory, a binary file or a file over ${MAX_FILE} bytes; output is clipped at ` +
	`${MAX_CHARS} characters. Placeholders never reveal the value.`;
const SAFE_GREP_HELP =
	`Search file contents (regular expression) with secrets replaced by ${ph("kind_n")} placeholders. Use this to search, in the ` +
	`working tree or outside it: it needs no permission prompt. Use grep or rg through ${SHELL} only on exact paths in the ` +
	"working tree that are known not to be sensitive (no .env files, keys or credentials, nothing under agent/, config/ or claude/). Searches " +
	"hidden files such as .env* (never .git; files ignored by .gitignore only with include_ignored); returns up to max_lines " +
	"lines of path:line:text, each cut at 400 characters.";

type Ran = { code: number; stdout: string };

export default function (pi: ExtensionAPI) {
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!agentDir) return;
	const redactor = join(dirname(agentDir), "bin", "tp-redact");
	const session = redactionSession();

	const run = (cmd: string, args: string[], cwd: string, signal?: AbortSignal, input?: string): Promise<Ran> =>
		new Promise((done) => {
			const child = execFile(cmd, args, { timeout: 120000, maxBuffer: 64 * 1024 * 1024, cwd, encoding: "utf8", signal }, (err, stdout) =>
				done({ code: err ? (typeof err.code === "number" ? err.code : err.killed ? 2 : 1) : 0, stdout: String(stdout) }));
			child.stdin?.on("error", () => {}); // the child may exit before it reads
			if (input !== undefined) child.stdin?.end(input);
		});
	// Fail closed: if the redactor does not succeed, nothing from the file is returned.
	const redact = async (text: string, args: string[], cwd: string, signal?: AbortSignal): Promise<string> => {
		const r = await run(redactor, ["--stdin", "--session", session, ...args], cwd, signal, text);
		if (r.code !== 0) throw new Error("redaction failed; output withheld");
		return r.stdout;
	};
	// `~` is the account's home from the password database, never $HOME: the home the trust-store guard expands it to
	// (lib/trust.py home_dir), so the guard judges the file the tool opens.
	const where = (p: string, cwd: string) => {
		const home = userInfo().homedir;
		const expanded = p === "~" ? home : p.startsWith("~/") ? join(home, p.slice(2)) : p;
		return isAbsolute(expanded) ? expanded : resolve(cwd, expanded);
	};
	const clip = (text: string) => (text.length > MAX_CHARS ? `${text.slice(0, MAX_CHARS)}\n…(truncated)` : text);
	const reply = (text: string) => ({ content: [{ type: "text" as const, text }], details: undefined });
	// The operand is resolved once, before the read or search; that real path is what is read or searched and what
	// tp-redact judges (`--real`, never resolved again). If the name leads elsewhere once the output is ready, the file
	// changed during the call and nothing is returned.
	const unchanged = (name: string, real: string) => {
		let now: string | undefined;
		try { now = realpathSync(name); } catch { now = undefined; }
		if (now !== real) throw new Error(`${name} changed during the call; output withheld`);
	};
	// The directory the trust-store guard judged the call in.
	const base = (ctx: { cwd?: unknown } | undefined) => (typeof ctx?.cwd === "string" && ctx.cwd ? ctx.cwd : process.cwd());

	pi.registerTool({
		name: "safe_read",
		label: "Safe read",
		description: SAFE_READ_HELP,
		parameters: Type.Object({
			path: Type.String({ description: "File to read" }),
			offset: Type.Optional(Type.Number({ description: "First line, 1-based (default 1)" })),
			limit: Type.Optional(Type.Number({ description: "Max lines (default 2000)" })),
		}),
		async execute(_id, p, signal, _onUpdate, ctx) {
			const cwd = base(ctx);
			const file = where(String(p.path ?? ""), cwd);
			statSync(file); // a missing file fails here, under the name the call gave
			let real: string;
			try { real = realpathSync(file); } catch { throw new Error(`${file} changed during the call; output withheld`); }
			const info = statSync(real);
			if (!info.isFile()) throw new Error(`${file} is not a file`);
			if (info.size > MAX_FILE) throw new Error(`${file} is larger than ${MAX_FILE} bytes`);
			const raw = readFileSync(real);
			if (raw.includes(0)) throw new Error(`${file} looks binary`);
			const lines = (await redact(raw.toString("utf8"), ["--source", file, "--real", real], cwd, signal)).split("\n");
			unchanged(file, real);
			const first = Math.max(1, Math.floor(Number(p.offset) || 1));
			const count = Math.max(1, Math.min(10000, Math.floor(Number(p.limit) || 2000)));
			const shown = lines.slice(first - 1, first - 1 + count).map((l, i) => `${String(first + i).padStart(6)}\t${l}`);
			return reply(clip(shown.join("\n") || "(empty)"));
		},
	});

	pi.registerTool({
		name: "safe_grep",
		label: "Safe grep",
		description: SAFE_GREP_HELP,
		parameters: Type.Object({
			pattern: Type.String({ description: "Regular expression" }),
			path: Type.Optional(Type.String({ description: "File or directory (default: working directory)" })),
			glob: Type.Optional(Type.String({ description: "Only files matching this glob, e.g. '*.env*'" })),
			ignore_case: Type.Optional(Type.Boolean()),
			include_ignored: Type.Optional(Type.Boolean({ description: "Also search files ignored by .gitignore (default false)" })),
			max_lines: Type.Optional(Type.Number({ description: "Max result lines (default 200)" })),
		}),
		async execute(_id, p, signal, _onUpdate, ctx) {
			const pattern = String(p.pattern ?? "");
			if (!pattern) throw new Error("pattern is required");
			const cwd = base(ctx);
			const target = where(String(p.path ?? "."), cwd);
			let real: string;
			try { real = realpathSync(target); } catch { throw new Error("search failed"); }
			const have = await run("rg", ["--version"], cwd, signal);
			// No ripgrep config (it could turn on --follow) and no symlink walking: the guard judges the search root, so a
			// link inside it that leads into the trust store must stay unfollowed. `--null` ends each path with a NUL, so tp-redact
			// takes every line's source from the path the search printed, never from the display text (F-GREP). The search
			// runs on the resolved root; tp-redact shows its lines under the name the call gave.
			const args = have.code === 0
				? ["--no-config", "--no-follow", "--null", "--line-number", "--no-heading", "--color", "never", "--with-filename", "--hidden", "-g", "!.git",
					...(p.include_ignored ? ["--no-ignore"] : []), ...(p.ignore_case ? ["-i"] : []), ...(p.glob ? ["-g", String(p.glob)] : []),
					"-e", pattern, "--", real]
				: ["--null", "-rnIE", ...(p.ignore_case ? ["-i"] : []), ...(p.glob ? [`--include=${p.glob}`] : []), "-e", pattern, "--", real];
			const found = await run(have.code === 0 ? "rg" : "grep", args, cwd, signal);
			if (found.code > 1) throw new Error("search failed");
			const redacted = await redact(found.stdout, ["--grep", "--null", "--source", target, "--real", real, "--cwd", cwd], cwd, signal);
			unchanged(target, real);
			const max = Math.max(1, Math.min(2000, Math.floor(Number(p.max_lines) || 200)));
			const lines = redacted.split("\n").filter(Boolean);
			const body = lines.slice(0, max).map((l) => (l.length > 400 ? `${l.slice(0, 400)}…` : l)).join("\n");
			return reply(clip((body || "(no matches)") + (lines.length > max ? `\n…(${lines.length - max} more lines)` : "")));
		},
	});
}
