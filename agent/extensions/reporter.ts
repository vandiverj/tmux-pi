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
 *
 * Turn numbers (HB4a round 5), the same fields and rule as claude/plugins/tmux-pi/hooks/report.sh: promptTurn counts
 * agent_start; completedTurn is the highest promptTurn whose own agent_settled wrote a reply with a valid RESULT_STATUS line
 * (lib/result_status.py). It only goes up: the next agent_start withdraws result.md without lowering it, and a restart
 * carries both over. A pause names the promptTurn it interrupts and is void once completedTurn reaches it
 * (lib/contract.py pause_void). An agent_settled answers the turn its reply started from (replyTurn, HB4b rule 2): the
 * agent_start that opened the reply, not one that arrived while it ran. HB4c: such a prompt is owed (owedTurn); a later
 * settle with no open reply but a new reply (an assistant message since the last settle) is the owed turn's own settle and
 * completes it; the next agent_start closes an owed turn. Any other settle with no open reply completes nothing.
 *
 * A worker that settles waiting with a reply that has no RESULT_STATUS line waits for an answer: it opens (or finds) an
 * `input` request under $TP_TASK_DIR/requests/ through lib/hub_records.py (pi-state), and the next start resolves it
 * (left-state). That bookkeeping never breaks the worker.
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { ensureDir, refuseLinks, taskStateRoot, writeFileAtomic } from "./lib/state-write.ts";

type Tokens = { input: number; output: number; cacheRead: number; cacheWrite: number };

type State = {
	id: string;
	harness: "pi";
	state: "starting" | "working" | "tool" | "waiting" | "error";
	tool: string;
	model: string;
	// The name pi runs the selected model under when this machine aliases its provider (TP_PROVIDER_ALIASES); absent otherwise.
	launched_model?: string;
	turns: number;
	tools: number;
	toolErrors: number;
	tokens: Tokens;
	// Usage by the model that produced each assistant message, so a model switch keeps history.
	byModel: Record<string, Tokens & { cost: number }>;
	cost: number;
	error: string;
	promptTurn: number;
	completedTurn: number;
	replyTurn: number;
	// HB4c: the oldest prompt that arrived while an earlier reply ran, whose own settle may still come (0: none).
	owedTurn: number;
	updated: string;
};

// The RESULT_STATUS line as lib/result_status.py reads it (RESULT_STATUS_LEAD + RESULT_STATUS_WORD); tests/test_refusal_remedies.py
// holds this copy to the same answers.
const RESULT_STATUS_LEAD = "^[ \\t]{0,3}(?:(?:#{1,6}|[-+*]|\\d+[.)])[ \\t]+)*[*_`~]*RESULT_STATUS[ \\t]*[*_`~]*[ \\t]*:";
const STATUSED = new RegExp(`${RESULT_STATUS_LEAD}[\\s*_\`~]*(complete|empty|blocked|incomplete)(?![\\p{L}\\p{N}])`, "imu");

// lib/hub_records.py, the one writer of request records (TP_HUB_RECORDS_PY overrides it for a copy of this file in a test).
const hubRecords = (): string => process.env.TP_HUB_RECORDS_PY || fileURLToPath(new URL("../../lib/hub_records.py", import.meta.url));

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

// A local model may inline its reasoning as a leading <think>...</think> block in the reply text. Drop that block (and
// the whitespace after it); a <think> later in the text is part of the answer and stays.
export function stripLeadingThink(text: string): string {
	return text.replace(/^\s*<think>[\s\S]*?<\/think>\s*/, "");
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
		promptTurn: 0,
		completedTurn: 0,
		replyTurn: 0,
		owedTurn: 0,
		updated: "",
	};
	let lastReply = "";
	let replyOpen = false; // an agent_start opened a reply that no agent_settled has ended yet
	let freshReply = false; // an assistant message ended since the last agent_settled: a settle with it carries a new reply

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
	const turnNumber = (v: any) => (Number.isInteger(v) && v >= 0 ? v : 0);
	const restore = () => {
		try {
			const prev = JSON.parse(readFileSync(join(dir, "state.json"), "utf8"));
			if (prev?.harness !== "pi" || prev.id !== s.id) return;
			s.turns = num(prev.turns);
			s.tools = num(prev.tools);
			s.cost = num(prev.cost);
			s.promptTurn = turnNumber(prev.promptTurn);
			s.completedTurn = turnNumber(prev.completedTurn);
			for (const k of Object.keys(s.tokens) as (keyof Tokens)[]) s.tokens[k] = num(prev.tokens?.[k]);
			for (const [name, u] of Object.entries<any>(prev.byModel ?? {})) {
				s.byModel[name] = { input: num(u?.input), output: num(u?.output), cacheRead: num(u?.cacheRead), cacheWrite: num(u?.cacheWrite), cost: num(u?.cost) };
			}
		} catch {}
	};

	// The task's request records (lib/hub_records.py pi-state): only a worker's task dir has them, never main's. Nothing to resolve
	// when the task has no requests/ yet, so a start costs no process then.
	const requests = (state: string, textFile?: string) => {
		if (!taskDir || dir !== taskDir) return;
		if (state !== "waiting" && !existsSync(join(dir, "requests"))) return;
		try {
			execFileSync(existsSync("/usr/bin/python3") ? "/usr/bin/python3" : "python3",
				[hubRecords(), "pi-state", "--task-dir", dir, "--state", state, ...(textFile ? ["--text-file", textFile] : [])],
				{ stdio: "ignore", timeout: 5000 });
		} catch {}
	};

	// A machine-local provider alias (config/local.json provider_aliases) changes only the provider name pi is launched with.
	// bin/tp-pi, which every pi start passes, exports the map it applied as TP_PROVIDER_ALIASES ({"shared": "launched"}). Every
	// name this reporter records (state model, byModel keys) is the shared id: a "<launched>/<id>" name is recorded as
	// "<shared>/<id>" for any id, with or without a task (main too), and the launched name of the selected model is kept in
	// launched_model. A name whose provider is not an alias target, or a missing or malformed variable, is left as it is.
	const sharedOf = new Map<string, string>();
	try {
		const aliases = JSON.parse(process.env.TP_PROVIDER_ALIASES || "{}");
		if (aliases && typeof aliases === "object" && !Array.isArray(aliases)) {
			for (const [from, to] of Object.entries(aliases)) {
				if (typeof to === "string" && to && from && !from.includes("/") && !to.includes("/")) sharedOf.set(to, from);
			}
		}
	} catch {}
	// [shared name, launched name or "" when the name is not aliased]
	const resolve = (name: string): [string, string] => {
		const slash = name.indexOf("/");
		const from = slash > 0 ? sharedOf.get(name.slice(0, slash)) : undefined;
		return from ? [`${from}${name.slice(slash)}`, name] : [name, ""];
	};
	const select = (name: string) => {
		const [shared, launched] = resolve(name);
		s.model = shared;
		if (launched) s.launched_model = launched;
		else delete s.launched_model;
	};

	pi.on("session_start", (_event, ctx) => {
		restore();
		select(ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : "");
		save();
	});
	pi.on("model_select", (event) => {
		select(`${event.model.provider}/${event.model.id}`);
		save();
	});
	pi.on("agent_start", () => {
		s.promptTurn += 1;
		if (!replyOpen) s.replyTurn = s.promptTurn;
		s.owedTurn = 0; // HB4c rule 1: a new prompt closes an owed turn; with no open reply it starts the reply that answers it
		replyOpen = true;
		s.state = "working";
		s.error = "";
		lastReply = "";
		// A link at tasks/ or the task dir would make this delete a file outside the state dir.
		try { refuseLinks(root, dir); unlinkSync(join(dir, "result.md")); } catch {}
		save();
		requests("working");
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
			const name = m.model ? resolve(`${m.provider ? `${m.provider}/` : ""}${m.model}`)[0] : s.model;
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
		const text = stripLeadingThink(textOf(m)).trim();
		if (text) {
			lastReply = text;
			freshReply = true;
		}
		save();
	});
	pi.on("agent_settled", () => {
		s.state = s.error ? "error" : "waiting";
		s.tool = "";
		// result.md first, then state.json (as report.sh does): the state that says the turn completed (completedTurn) is
		// written only after its reply is on disk, in one write with the settled state.
		let replied = false;
		if (lastReply) {
			try {
				atomic("result.md", `${lastReply}\n`);
				replied = true;
			} catch {}
		}
		// The turn this settle answers: the open reply's (HB4b rule 2), else the owed turn's when its reply is new (HB4c rule 3:
		// an assistant message ended since the last settle), else none (a duplicate or late settle completes nothing).
		const answers = replyOpen ? s.replyTurn : s.owedTurn > 0 && freshReply ? s.owedTurn : 0;
		if (answers && replied && STATUSED.test(lastReply)) s.completedTurn = Math.max(s.completedTurn, answers);
		if (answers) s.owedTurn = answers < s.promptTurn ? answers + 1 : 0; // HB4c rule 2: a prompt that arrived meanwhile is owed
		replyOpen = false;
		freshReply = false;
		save();
		requests(s.state, replied ? join(dir, "result.md") : undefined);
	});
}
