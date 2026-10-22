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

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
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
				const reply = out.length > MAX_REPLY ? out.slice(0, MAX_REPLY) + "\n[truncated; ask a narrower question]" : out;
				return { content: [{ type: "text" as const, text: reply || "(the local assistant returned nothing)" }], details: { model: assistant.model } };
			} catch (error) {
				throw new Error(`local assistant unavailable (${(error as Error).message}); do this yourself with read/grep/find`);
			}
		},
	});

}
