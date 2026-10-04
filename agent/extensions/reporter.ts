/**
 * reporter — writes a tmux-pi subagent's live state for the status pane.
 *
 * Active only in workers started by tp-spawn, which set TP_TASK_DIR. The main
 * agent loads it too but does nothing.
 *
 *   $TP_TASK_DIR/state.json  state, current tool, turns, tokens, cost, model
 *   $TP_TASK_DIR/result.md   last assistant reply, written each time the agent settles
 *
 * States: starting -> working <-> tool -> waiting (settled, ready for input).
 * tools counts tool calls; toolErrors counts failed tool calls in a row (reset by a
 * success), which tp-contract reads to spot a worker that cannot reach its sources.
 * tp-spawn's run.sh records the process exit separately.
 */

import { mkdirSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type State = {
	id: string;
	harness: "pi";
	state: "starting" | "working" | "tool" | "waiting" | "error";
	tool: string;
	model: string;
	turns: number;
	tools: number;
	toolErrors: number;
	tokens: { input: number; output: number; cacheRead: number; cacheWrite: number };
	cost: number;
	error: string;
	updated: string;
};

// A short, single-line hint of what a tool is doing (path, command, pattern).
function toolLabel(name: string, args: any): string {
	const hint = args?.path ?? args?.file_path ?? args?.command ?? args?.pattern ?? "";
	const text = `${name} ${String(hint)}`.replace(/\s+/g, " ").trim();
	return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

function textOf(message: any): string {
	const content = message?.content;
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((part: any) => part?.type === "text")
		.map((part: any) => part.text)
		.join("\n");
}

export default function (pi: ExtensionAPI) {
	const taskDir = process.env.TP_TASK_DIR;
	// Main runs have no TP_TASK_DIR; tp creates @tp_state_dir for the session.
	let dir = taskDir;
	if (!dir && process.env.TP_STATE_DIR) {
		dir = join(process.env.TP_STATE_DIR, "main");
		mkdirSync(dir, { recursive: true });
	}
	if (!dir && process.env.TMUX && process.env.PI_CODING_AGENT_DIR) {
		try {
			const { execFileSync } = require("node:child_process");
			const session = execFileSync("tmux", ["display-message", "-p", ...(process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : []), "#S"], { encoding: "utf8" }).trim();
			const stateDir = execFileSync("tmux", ["show-options", "-qv", "-t", `=${session}:`, "@tp_state_dir"], { encoding: "utf8" }).trim();
			if (stateDir) { dir = join(stateDir, "main"); mkdirSync(dir, { recursive: true }); }
		} catch {}
	}
	if (!dir) return;

	const s: State = {
		id: process.env.TP_TASK_ID ?? "main",
		harness: "pi",
		state: "starting",
		tool: "",
		model: "",
		turns: 0,
		tools: 0,
		toolErrors: 0,
		tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
		cost: 0,
		error: "",
		updated: "",
	};
	let lastReply = "";

	// Write via rename so the status pane never reads a half-written file.
	const atomic = (name: string, body: string) => {
		const file = join(dir, name);
		writeFileSync(`${file}.tmp`, body);
		renameSync(`${file}.tmp`, file);
	};
	const save = () => {
		s.updated = new Date().toISOString();
		try {
			atomic("state.json", `${JSON.stringify(s)}\n`);
		} catch {
			// The task dir may have been removed; reporting must never break the worker.
		}
	};

	pi.on("session_start", (_event, ctx) => {
		s.model = ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "";
		save();
	});
	pi.on("model_select", (event) => {
		s.model = `${event.model.provider}/${event.model.id}`;
		save();
	});
	pi.on("agent_start", () => {
		s.state = "working";
		s.error = "";
		lastReply = "";
		try { unlinkSync(join(dir, "result.md")); } catch {}
		save();
	});
	pi.on("tool_execution_start", (event) => {
		s.state = "tool";
		s.tool = toolLabel(event.toolName, event.args);
		s.tools += 1;
		save();
	});
	pi.on("tool_execution_end", (event) => {
		s.state = "working";
		s.tool = "";
		s.toolErrors = event.isError ? s.toolErrors + 1 : 0;
		save();
	});
	pi.on("message_end", (event) => {
		const m: any = event.message;
		if (m?.role !== "assistant") return;
		s.turns += 1;
		const u = m.usage;
		if (u) {
			s.tokens.input += u.input ?? 0;
			s.tokens.output += u.output ?? 0;
			s.tokens.cacheRead += u.cacheRead ?? 0;
			s.tokens.cacheWrite += u.cacheWrite ?? 0;
			s.cost += u.cost?.total ?? 0;
		}
		if (m.stopReason === "error" || m.errorMessage) s.error = String(m.errorMessage ?? "error");
		const text = textOf(m).trim();
		if (text) lastReply = text;
		save();
	});
	pi.on("agent_settled", () => {
		s.state = s.error ? "error" : "waiting";
		s.tool = "";
		save();
		if (lastReply) {
			try {
				atomic("result.md", `${lastReply}\n`);
			} catch {}
		}
	});
}
