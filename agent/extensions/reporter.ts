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

import { readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { ensureDir, refuseLinks, taskStateRoot, writeFileAtomic } from "./lib/state-write.ts";

type Tokens = { input: number; output: number; cacheRead: number; cacheWrite: number };

type State = {
	id: string;
	harness: "pi";
	state: "starting" | "working" | "tool" | "waiting" | "error";
	tool: string;
	model: string;
	turns: number;
	tools: number;
	toolErrors: number;
	tokens: Tokens;
	// Usage by the model that produced each assistant message, so a model switch keeps history.
	byModel: Record<string, Tokens & { cost: number }>;
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
	// The trusted root for every write: the state dir that holds the task dir (state/tasks/<id>), so a link at
	// tasks/ or at the task dir is refused, or the session state dir that holds main/.
	let root = taskDir ? taskStateRoot(taskDir) : undefined;
	// A symlink at main/ is refused: the reporter stays off rather than write through it.
	const mainDir = (stateDir: string): string | undefined => {
		const main = join(stateDir, "main");
		try { ensureDir(stateDir, main); } catch { return undefined; }
		return main;
	};
	if (!dir && process.env.TP_STATE_DIR) {
		root = process.env.TP_STATE_DIR;
		dir = mainDir(root);
	}
	if (!dir && process.env.TMUX && process.env.PI_CODING_AGENT_DIR) {
		try {
			const { execFileSync } = require("node:child_process");
			const session = execFileSync("tmux", ["display-message", "-p", ...(process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : []), "#S"], { encoding: "utf8" }).trim();
			const stateDir = execFileSync("tmux", ["show-options", "-qv", "-t", `=${session}:`, "@tp_state_dir"], { encoding: "utf8" }).trim();
			if (stateDir) { root = stateDir; dir = mainDir(stateDir); }
		} catch {}
	}
	if (!dir || !root) return;

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
		byModel: {},
		cost: 0,
		error: "",
		updated: "",
	};
	let lastReply = "";

	// Write via rename so the status pane never reads a half-written file.
	const atomic = (name: string, body: string) => {
		writeFileAtomic(root, join(dir, name), body, 0o666);
	};
	const save = () => {
		s.updated = new Date().toISOString();
		try {
			atomic("state.json", `${JSON.stringify(s)}\n`);
		} catch {
			// The task dir may have been removed; reporting must never break the worker.
		}
	};

	// A restarted or resumed worker reuses its task dir; carry over its totals instead of resetting.
	const num = (v: any) => (typeof v === "number" && Number.isFinite(v) ? v : 0);
	const restore = () => {
		try {
			const prev = JSON.parse(readFileSync(join(dir, "state.json"), "utf8"));
			if (prev?.harness !== "pi" || prev.id !== s.id) return;
			s.turns = num(prev.turns);
			s.tools = num(prev.tools);
			s.cost = num(prev.cost);
			for (const k of Object.keys(s.tokens) as (keyof Tokens)[]) s.tokens[k] = num(prev.tokens?.[k]);
			for (const [name, u] of Object.entries<any>(prev.byModel ?? {})) {
				s.byModel[name] = { input: num(u?.input), output: num(u?.output), cacheRead: num(u?.cacheRead), cacheWrite: num(u?.cacheWrite), cost: num(u?.cost) };
			}
		} catch {}
	};

	pi.on("session_start", (_event, ctx) => {
		restore();
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
		// A link at tasks/ or the task dir would make this delete a file outside the state dir.
		try { refuseLinks(root, dir); unlinkSync(join(dir, "result.md")); } catch {}
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
			const d = { input: num(u.input), output: num(u.output), cacheRead: num(u.cacheRead), cacheWrite: num(u.cacheWrite), cost: num(u.cost?.total) };
			s.tokens.input += d.input;
			s.tokens.output += d.output;
			s.tokens.cacheRead += d.cacheRead;
			s.tokens.cacheWrite += d.cacheWrite;
			s.cost += d.cost;
			// The message names the model that produced it; the selected model is only a fallback.
			const name = m.model ? `${m.provider ? `${m.provider}/` : ""}${m.model}` : s.model;
			if (name) {
				const b = (s.byModel[name] ??= { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, cost: 0 });
				b.input += d.input;
				b.output += d.output;
				b.cacheRead += d.cacheRead;
				b.cacheWrite += d.cacheWrite;
				b.cost += d.cost;
			}
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
