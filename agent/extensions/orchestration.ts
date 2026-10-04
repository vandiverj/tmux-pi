/** Thin Pi binding for the shared tp-agent command. Main only. */
import { execFile } from "node:child_process";
import { dirname, join } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const REFRESH_TIMEOUT_MS = 15_000;

function run(command: string, args: string[], timeout = 180_000): Promise<string> {
	return new Promise((resolve, reject) => {
		execFile(command, args, { timeout, maxBuffer: 2_000_000 }, (error, stdout, stderr) => {
			if (error) reject(new Error((stderr || (error.killed ? `timed out after ${timeout / 1000}s` : error.message)).trim()));
			else resolve(stdout);
		});
	});
}

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const command = join(dirname(process.env.PI_CODING_AGENT_DIR), "bin", "tp-agent");
	const refresh = join(dirname(process.env.PI_CODING_AGENT_DIR), "bin", "tp-refresh-status");
	// Deterministic: runs the script directly and only notifies; no model turn or message.
	pi.registerCommand("refresh-status", {
		description: "Reload this session's status pane and clear its scrollback (no model turn)",
		handler: async (_args, ctx) => {
			try {
				const out = (await run(refresh, [], REFRESH_TIMEOUT_MS)).trim();
				ctx.ui.notify(out || "Status pane refreshed", "info");
			} catch (error) {
				ctx.ui.notify(`Status refresh failed: ${(error as Error).message}`, "error");
			}
		},
	});
	pi.registerTool({
		name: "dispatch_role",
		label: "Dispatch specialist",
		description: "Start or queue a specialist worker through the shared tmux-pi task interface. Choose a role from the work needed. The classifier selects model and effort unless you specify both a model and worker.",
		parameters: Type.Object({
			role: Type.String({ description: "coder, researcher, code-reviewer, tester, vulnerability-reviewer, data-flow-reviewer, architect, or infrastructure" }),
			task: Type.String({ description: "Focused, self-contained assignment" }),
			deliverable: Type.Optional(Type.String()),
			worker: Type.Optional(Type.String()),
			model: Type.Optional(Type.String()),
			thinking: Type.Optional(Type.String()),
			context: Type.Optional(Type.Array(Type.String({ description: "path or path:start-end" }))),
			notes: Type.Optional(Type.Array(Type.String())),
			after: Type.Optional(Type.Array(Type.String({ description: "Task id that must settle first" }))),
		}),
		async execute(_id, p) {
			const args = ["dispatch", "--role", p.role, "--task", p.task];
			if (p.deliverable) args.push("--deliverable", p.deliverable);
			if (p.worker) args.push("--worker", p.worker);
			if (p.model) args.push("--model", p.model);
			if (p.thinking) args.push("--thinking", p.thinking);
			for (const spec of p.context ?? []) args.push("--context", spec);
			for (const note of p.notes ?? []) args.push("--note", note);
			for (const id of p.after ?? []) args.push("--after", id);
			const value = JSON.parse(await run(command, args));
			return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: value };
		},
	});
	pi.registerTool({
		name: "task_index",
		label: "Task index",
		description: "Rebuild and read the short session index after compaction or to see active and pending specialist work.",
		parameters: Type.Object({}),
		async execute() {
			const value = JSON.parse(await run(command, ["index"]));
			return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: value };
		},
	});
	pi.registerTool({
		name: "task_collect",
		label: "Collect task",
		description: "Read a settled worker's concise result and mark it handed off. Active tasks remain active.",
		parameters: Type.Object({ id: Type.String() }),
		async execute(_id, p) {
			const value = JSON.parse(await run(command, ["collect", p.id]));
			return { content: [{ type: "text" as const, text: JSON.stringify(value) }], details: value };
		},
	});
}
