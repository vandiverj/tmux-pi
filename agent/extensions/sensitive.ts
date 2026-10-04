/** Ask the human before a worker reads a credential-bearing path. */
import { execFileSync } from "node:child_process";
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
	pi.on("tool_call", async (event, ctx) => {
		let decision: any;
		try {
			decision = JSON.parse(execFileSync(classifier, [], {
				input: JSON.stringify({ tool_name: event.toolName, tool_input: event.input ?? {} }),
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
