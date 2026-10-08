/** Ask the human before a worker reads a credential-bearing path. */
import { execFileSync, spawnSync } from "node:child_process";
import { renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const taskDir = process.env.TP_TASK_DIR;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!agentDir) return;
	const classifier = join(dirname(agentDir), "bin", "tp-sensitive-read");
	const request = taskDir ? join(taskDir, "permission-request.json") : "";
	const save = (value: object) => {
		if (!request) return;
		writeFileSync(request + ".tmp", JSON.stringify(value) + "\n");
		renameSync(request + ".tmp", request);
	};
	// Redact secrets in every tool result before the model or the session sees it. Only in "on" mode; fail closed.
	const redactor = join(dirname(agentDir), "bin", "tp-redact");
	const session = process.env.TP_TASK_ID || process.env.TP_REDACTION_SESSION || `pi-${process.ppid}`;
	let active: boolean | undefined;
	const redactionOn = () => {
		if (active !== undefined) return active;
		try { active = execFileSync(redactor, ["--mode"], { encoding: "utf8", timeout: 5000 }).trim() === "on"; }
		catch { active = true; } // cannot tell: assume on, so results are redacted or withheld
		return active;
	};
	const withheld = (why: string) => `[output withheld: redaction ${why}]`;
	const redactText = (text: string, toolName: string, input: Record<string, unknown>): string => {
		const args = ["--stdin", "--session", session];
		const path = typeof input?.path === "string" ? input.path : typeof input?.file_path === "string" ? input.file_path : "";
		if (toolName === "grep") args.push("--grep");
		else if (path) args.push("--source", path);
		const run = spawnSync(redactor, args, { input: text, encoding: "utf8", timeout: 60000, maxBuffer: 256 * 1024 * 1024 });
		return run.status === 0 && typeof run.stdout === "string" ? run.stdout : withheld("failed");
	};
	pi.on("tool_result", (event) => {
		if (!redactionOn()) return;
		const content = event.content.map((block) =>
			block.type === "text" ? { ...block, text: redactText(block.text, event.toolName, event.input) } : block);
		const result: any = { content };
		if (event.structuredContent !== undefined) result.structuredContent = null; // cannot be redacted structurally; `content` carries the text
		return result;
	});
	pi.on("tool_call", async (event, ctx) => {
		let decision: any;
		// The session's directory, as the Claude hook sends it. Left out when absent or odd, so the classifier assumes nothing and prompts.
		const cwd = typeof ctx.cwd === "string" && ctx.cwd ? ctx.cwd : undefined;
		try {
			decision = JSON.parse(execFileSync(classifier, [], {
				input: JSON.stringify({ tool_name: event.toolName, tool_input: event.input ?? {}, ...(cwd ? { cwd } : {}) }),
				encoding: "utf8", timeout: 5000,
			}));
		} catch {
			return { block: true, reason: "Sensitive-read classifier unavailable; the worker cannot verify this read." };
		}
		if (!decision.sensitive) return;
		const base = { target: decision.target, reason: decision.reason, updated: new Date().toISOString() };
		save({ ...base, status: "pending" });
		if (!ctx.hasUI) {
			return { block: true, reason: `Reading ${decision.target} needs user authorization in the active tmux pane.` };
		}
		const allowed = await ctx.ui.confirm(
			"Authorize sensitive read?",
			`This tool call may expose ${decision.target}. Approve only if the requested information is needed for this task.`,
		);
		save({ ...base, status: allowed ? "authorized" : "denied", updated: new Date().toISOString() });
		if (!allowed) return { block: true, reason: `User denied access to ${decision.target}.` };
	});
}
