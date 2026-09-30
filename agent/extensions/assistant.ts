/**
 * assistant — the local model as the main model's legwork assistant, plus guardrails.
 *
 * Active only in the main agent, unless config/local.json sets "mode": "strict"
 * (then dispatcher.ts's contract handshake runs instead).
 *
 *   ask_local    main hands a read/search job to the local model (config/local.json
 *                "assistant", default LM Studio Qwen). It runs fresh and read-only
 *                (read, grep, find, ls) in the working directory and returns a digest:
 *                paths, line numbers and verbatim snippets. Main keeps the thinking and
 *                the knowledge; the local model's context never accumulates, so its
 *                window never compacts mid-task.
 *   guardrails   block destructive shell commands and edits outside the workdir.
 *
 * Long-running or parallel work is spawn_subagent (subagents.ts); no contract step.
 */

import { execFile } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, resolve, sep } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const DESTRUCTIVE = [
	/\brm\s+(?:-\w*\s+)*-\w*[rf]\w*\s+(?:-\w+\s+)*(?:\/|~|\$HOME|\*|\.\.?)(?:\s|$)/,
	/\bgit\s+push\b[^\n]*(?:--force\b|--force-with-lease\b|\s-f\b)/,
	/\bgit\s+reset\s+--hard\b/,
	/\bgit\s+clean\b[^\n]*\s-\w*[fd]/,
	/\bsudo\b/,
	/\b(?:mkfs|dd\s+[^\n]*of=\/dev\/|diskutil\s+erase)/,
	/>\s*\/dev\/(?:sd|disk|nvme)/,
	/\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b/,
];

function readJson(file: string): any {
	try {
		return JSON.parse(readFileSync(file, "utf8"));
	} catch {
		return {};
	}
}

const ASSISTANT_PROMPT =
	"You are a read-only research assistant for a stronger model that will do the thinking. " +
	"Do the legwork with your tools (read, grep, find, ls). Reply with exact file paths, line numbers and " +
	"verbatim relevant snippets. When asked to read files, quote the relevant text with line numbers " +
	"rather than paraphrasing it; summarize only the remainder. Say plainly when you did not find something. " +
	"No opinions, no padding.";
const MAX_REPLY = 24_000;

const GUIDANCE =
	"\n\nFor finding files, searching code, or reading several files, prefer the ask_local tool: a local " +
	"assistant model does the legwork and returns exact paths, line numbers and snippets, which keeps your " +
	"context small. Read a file yourself when you need to edit it or need its full text. " +
		"For independent, self-contained work (a separate fix, a long investigation, a parallel task) use " +
		"spawn_subagent instead of doing it inline, then collect_result.";

function run(cmd: string, args: string[], cwd: string, signal?: AbortSignal): Promise<string> {
	return new Promise((res, rej) => {
		const child = execFile(cmd, args, { cwd, timeout: 240_000, maxBuffer: 8_000_000, signal }, (error, stdout, stderr) =>
			error ? rej(new Error(String(stderr || error.message).trim().slice(-500))) : res(String(stdout)));
		child.stdin?.end();
	});
}

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const root = dirname(process.env.PI_CODING_AGENT_DIR);
	const local = readJson(join(root, "config/local.json"));
	if (local.mode === "strict") return;
	const localProviders: string[] = Array.isArray(local.local_providers) ? local.local_providers : ["slotstream", "lmstudio"];

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
				"--thinking", assistant.thinking, "--tools", "read,grep,find,ls", "--append-system-prompt", ASSISTANT_PROMPT, params.task];
			try {
				const out = (await run(join(root, "bin/tp-pi"), args, ctx.cwd, signal)).trim();
				const reply = out.length > MAX_REPLY ? out.slice(0, MAX_REPLY) + "\n[truncated; ask a narrower question]" : out;
				return { content: [{ type: "text" as const, text: reply || "(the local assistant returned nothing)" }], details: { model: assistant.model } };
			} catch (error) {
				throw new Error(`local assistant unavailable (${(error as Error).message}); do this yourself with read/grep/find`);
			}
		},
	});

	pi.on("tool_call", async (event, ctx) => {
		const input = (event.input ?? {}) as Record<string, unknown>;
		if (event.toolName === "bash") {
			const command = String(input.command ?? "");
			if (DESTRUCTIVE.some((re) => re.test(command))) {
				return { block: true, reason: "tmux-pi blocked a destructive command. If you really need it, ask the user to run it themselves." };
			}
		}
		if (event.toolName === "edit" || event.toolName === "write") {
			const raw = String(input.path ?? input.file_path ?? "");
			if (!raw) return undefined;
			const base = realpathSync(ctx.cwd);
			const abs = isAbsolute(raw) ? raw : resolve(base, raw);
			const real = existsSync(abs) ? realpathSync(abs) : abs;
			if (real !== base && !real.startsWith(base + sep)) {
				return { block: true, reason: `tmux-pi blocked an edit outside the working directory (${base}).` };
			}
		}
		return undefined;
	});
}
