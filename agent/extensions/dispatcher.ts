/**
 * dispatcher — R5 main Orient/Frame handshake and deterministic dispatch policy.
 * Input persists a non-authorizing scaffold; the main contract is persisted before
 * policy evaluation, then agent_end dispatches only authorized worker work.
 *
 * Active only in the main agent: inside tmux and not in a tp-spawn worker. Every
 * prompt reaching input is routed from raw text; later skill/template expansion
 * invalidates that route and closes dispatch/edit authority:
 *
 *   follow-up          when delegated tasks are still running, `tp-route --follow-up`
 *                      decides whether the message belongs to one of them; if so it is
 *                      typed into that worker. When the judge is unsure, the user picks.
 *   placement main     Pi remains in main on its existing model (no in-place switch).
 *                      Claude needs the same contract gate before a foreground worker.
 *   placement subagent main frames/commits a contract; policy authorizes dispatch at
 *                      agent_end. The worker gets the verbatim request, conversation,
 *                      ask-if-unclear, review requirement and report-back instructions.
 *                      Models in local packet_files also get up to 3 files a Claude
 *                      model picked (`tp-route --pick-files`).
 *   review required    when the author settles, a cross-provider reviewer starts; one
 *                      REQUEST_CHANGES round goes back to the author automatically.
 *   Laya unavailable   do not authorize dispatch or edits; main may answer read-only.
 *
 * Leading +here, +delegate and +review markers override placement or review; +followup
 * [id] sends the message to a running task without asking the judge. Results and worker
 * questions are posted into main's conversation as tmux-pi-auto messages, so main sees
 * them on its next turn. `/auto on|off` turns routing on or off.
 *
 * `+research [+review] [+after <id>[,<id>]] <question>` starts a research worker
 * (config/research.json) under a contract: a report file, checkpoints and a budget,
 * enforced by `tp-contract enforce` (reminders, then a pause) for every contract task in
 * the workspace. Independent research runs in parallel up to the frontier and local caps;
 * research that builds on a running research task (named with +after, or claimed by the
 * follow-up judge) or that finds no free slot waits in a queue (.tmux-pi/research-queue.json)
 * and starts with the earlier reports in its notes. A contract task that was stopped or
 * paused and has settled is "halted": main shows its reply and offers to continue it once
 * with more budget, narrow or escalate the question (research), or discard it.
 *
 * Independently of routing, a Claude worker stopped at a choice prompt (folder trust, a
 * tool permission) is shown to the user in main as a dialog; the pick is keyed into it.
 */

import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, lstatSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { appendFileSafe, ensureDir, refuseLinks, writeFileAtomic } from "./lib/state-write.ts";

type Decision = {
	worker: "pi" | "claude";
	model: string;
	thinking: string;
	route: string;
	intent: string;
	review: "none" | "optional" | "required";
	scope: string;
	placement: "main" | "subagent";
	overrides: string[];
	task: string;
};

type Preflight = {
	mode: string;
	artifact_tier: number;
	durable: boolean;
	stop: boolean;
	no_edits: boolean;
	promotion_notice?: string;
	task: string;
};

type AutomationPolicy = {
	action: "auto_dispatch" | "recommend_manual";
	mode: "qualified" | "recommendation_manual" | "explicit_human_request" | "approved_goal";
	qualified_cell?: string;
	candidate_cell?: string;
	notice: string;
	review?: { required?: boolean; reasons?: string[]; risk?: string };
	confirmation_required?: boolean;
	confirmation_reason?: string;
	post_contract?: boolean;
};

type MainContract = {
	handshake_id: string;
	request_sha256: string;
	action: "direct" | "delegate" | "ask_alignment" | "stop";
	mode: "direct" | "delegated" | "plan" | "prototype" | "investigate";
	task_summary: string;
	frame: string;
	assumptions: string[];
	material_ambiguity?: { question: string; why_material: string };
	no_edits: boolean;
	scope: "brief" | "sustained" | "multi_step";
	risk_notes: string[];
	proof_path: string[];
	done_when: string[];
};

type Handshake = {
	id: string;
	requestHash: string;
	original: string;
	effective: string;
	decision?: Decision;
	preflight?: any;
	mode?: string;
	research?: { review: boolean; after: string[]; worker: string; model: string; thinking: string };
	handoff?: string;
	created: number;
	requiresContract: boolean;
	committed?: boolean;
	allowMainEdits?: boolean;
	reviewRequired?: boolean;
	ownReviewRequested?: boolean;
	blockedBy?: string[];
	unverifiedExpansion?: boolean;
	policyDecision?: any;
	mainContract?: MainContract;
	goal?: { id: string; ticket: string; spec_sha256: string; workdir: string; scope: string; allowed_paths: string[]; done: string; objective: string };
};

type PendingAlignment = {
	handshake_id: string;
	request_sha256: string;
	question: string;
	why_material?: string;
	original: string;
	effective: string;
	handoff?: string;
	asked_at: string;
};

type DispatchPlan = {
	handshake: Handshake;
	decision: Decision;
	foreground: boolean;
	preflight: any;
	automation: AutomationPolicy;
	mainContract: MainContract;
	policyDecision: any;
};

// A task the dispatcher delegated. `id` follows escalation to the worker now doing it.
type Delegated = {
	id: string;
	request: string;
	review: Decision["review"];
	rounds: number; // reviews run since the user last spoke to it
	reviewer?: string;
	since: number; // when it was last given input; a report must be newer
	watching?: boolean;
	foreground?: boolean; // a main-placed Claude route, shown in the viewer
	report?: string; // a research contract's report file
};

const ASK_IF_UNCLEAR =
	"If the intent is unclear in a way where a wrong guess would change files or waste significant work, " +
	"ask before acting and wait for the answer; otherwise state your assumption and proceed.";
const REPORT_BACK =
	"Finish with a short report: what you did, the files you changed, how you checked it, and anything you assumed or left open.";
const RESEARCH_REPORT =
	"Finish with a short reply: the answer in a few sentences and how confident you are; tmux-pi adds the report's path.";
const MAX_REVIEW_ROUNDS = 2;

// A reviewer's verdict (same rules as lib/review_verdict.py): the whole first line is APPROVED, APPROVED_WITH_NOTES or
// REQUEST_CHANGES, or "REQUEST_CHANGES: reason" (the one longer form). Anything else is refused. APPROVED_WITH_NOTES approves
// and must carry its notes. Only the RESULT_STATUS line can contradict an approval; the notes may quote earlier rounds.
const VERDICT_LINE = /^(APPROVED_WITH_NOTES|APPROVED|REQUEST_CHANGES)(?::[ \t]*(\S.*))?$/;
// The RESULT_STATUS line as lib/result_status.py reads it (its RESULT_STATUS_LEAD): plain or with a heading, list, bold, italic or
// backtick marker around it. tests/test_refusal_remedies.py runs both readers over the same lines.
const RESULT_STATUS_LEAD = "^[ \\t]{0,3}(?:(?:#{1,6}|[-+*]|\\d+[.)])[ \\t]+)*[*_`~]*RESULT_STATUS[ \\t]*[*_`~]*[ \\t]*:";
const parseVerdict = (text: string): { verdict?: "APPROVED" | "APPROVED_WITH_NOTES" | "REQUEST_CHANGES"; notes: string; error?: string } => {
	const body = text.trim().replace(/^\ufeff/, "").trim();
	const [first, ...others] = body.split("\n");
	const match = VERDICT_LINE.exec(first.trimEnd());
	if (!match || (match[2] && match[1] !== "REQUEST_CHANGES")) return { notes: "", error: "the whole first line must be APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES (or 'REQUEST_CHANGES: reason') without formatting" };
	const verdict = match[1] as "APPROVED" | "APPROVED_WITH_NOTES" | "REQUEST_CHANGES";
	const rest = others.join("\n");
	const status = new RegExp(`${RESULT_STATUS_LEAD}(.*)$`, "im").exec(rest);
	if (verdict !== "REQUEST_CHANGES" && status && /\bREQUEST_CHANGES\b/.test(status[1])) return { notes: "", error: `${verdict} but its RESULT_STATUS line says REQUEST_CHANGES` };
	const notes = [match[2] ?? "", rest.replace(new RegExp(`${RESULT_STATUS_LEAD}.*$`, "gim"), "").trim()].filter(Boolean).join("\n");
	if (verdict === "APPROVED_WITH_NOTES" && !notes) return { notes: "", error: "APPROVED_WITH_NOTES has no notes" };
	return { verdict, notes };
};
const MAX_FOLLOW_UP_TASKS = 3;
const REPLY_LIMIT = 4000;

// A +research request waiting for its dependencies or a free research slot.
type Queued = {
	question: string;
	review: boolean;
	worker: string;
	model: string;
	thinking: string;
	after: string[]; // research tasks whose reports it builds on
	notes: string[];
	conversation: string;
	replaces?: string; // a halted task this narrows or escalates
	how?: "narrow" | "escalate";
	rounds?: number;
	escalationRecord?: any;
	preflight?: any;
	mainContract?: MainContract;
	policyDecision?: any;
	decision?: Decision;
	queued: number;
};

type Run = { code: number; stdout: string; stderr: string };

function run(cmd: string, args: string[], input?: string, timeout = 30_000): Promise<Run> {
	return new Promise((resolve) => {
		const child = execFile(cmd, args, { timeout }, (error, stdout, stderr) => {
			const code = error ? (typeof (error as any).code === "number" ? (error as any).code : 1) : 0;
			resolve({ code, stdout: String(stdout), stderr: String(stderr) });
		});
		if (input !== undefined) child.stdin?.end(input);
	});
}

async function must(cmd: string, args: string[], input?: string, timeout?: number): Promise<string> {
	const r = await run(cmd, args, input, timeout);
	if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || `${cmd} exited with ${r.code}`);
	return r.stdout.trim();
}

// Physical path when it exists, like tp_canon in lib/tp.sh.
const canon = (p: string) => { try { return realpathSync(p); } catch { return resolve(p); } };

function readJson(file: string): any {
	try {
		return JSON.parse(readFileSync(file, "utf8"));
	} catch {
		return {};
	}
}

function readText(file: string): string {
	try {
		return readFileSync(file, "utf8").trim();
	} catch {
		return "";
	}
}

const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const nowIso = () => new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
const handshakeId = () => `r5-${Date.now().toString(36)}-${randomUUID().slice(0, 8)}`;

function extractFencedRequest(record: string): string {
	const lines = record.split("\n");
	let inSummary = false;
	let fence = "";
	const body: string[] = [];
	for (const line of lines) {
		if (line.startsWith("## ")) {
			inSummary = line.trim().toLowerCase() === "## request summary";
			if (body.length) break;
			continue;
		}
		if (!inSummary) continue;
		if (!fence) {
			const match = /^(`{3,})(?:\w+)?\s*$/.exec(line.trim());
			if (match) fence = match[1];
			continue;
		}
		if (line.trim() === fence) break;
		body.push(line);
	}
	return body.join("\n").trim();
}

function requestText(dir: string): string {
	const request = readText(join(dir, "request.md"));
	if (request) return request;
	const task = readText(join(dir, "task.md"));
	return task.includes("tmux-pi-task-record:v1") ? extractFencedRequest(task) : task;
}

const clip = (text: string, limit: number) => (text.length > limit ? `${text.slice(0, limit)}\n…(truncated)` : text);
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

// The text of a session message, without tool calls or thinking.
function messageText(content: unknown): string {
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return "";
	return content
		.filter((part: any) => part?.type === "text")
		.map((part: any) => part.text)
		.join("\n");
}

// A choice prompt on a worker's screen: Claude Code's folder-trust and permission prompts.
// Options sit in a column with ❯ on the selected one, above an "Enter to confirm" or
// "Esc to cancel" footer; long options wrap onto further-indented lines.
export type Prompt = { text: string; options: string[]; cursor: number };

export function parsePrompt(screen: string): Prompt | undefined {
	const lines = screen.split("\n");
	const footer = lines.findLastIndex((l) => /Enter to confirm|Esc to cancel/.test(l));
	const cur = footer < 0 ? -1 : lines.slice(0, footer).findLastIndex((l) => /^\s*❯ \S/.test(l));
	if (cur < 0) return undefined;
	const col = lines[cur].indexOf("❯");
	const blank = (l: string) => !l.trim();
	const isOption = (l: string) => !l.slice(0, col).trim() && /^[❯ ] \S/.test(l.slice(col, col + 3));
	const isMore = (l: string) => !l.slice(0, col + 3).trim() && !blank(l);
	let first = cur;
	for (let i = cur - 1; i >= 0 && (isOption(lines[i]) || blank(lines[i])); i--) if (isOption(lines[i])) first = i;
	const options: string[] = [];
	let cursor = 0;
	for (let i = first; i < footer; i++) {
		const l = lines[i];
		if (isOption(l)) {
			if (i === cur) cursor = options.length;
			options.push(l.slice(col + 2).trim());
		} else if (isMore(l) && options.length) {
			const prev = options[options.length - 1];
			const next = l.trim();
			// Claude wraps by hand; keep a space only between two words.
			options[options.length - 1] = prev + (/[A-Za-z,]$/.test(prev) && /^[A-Za-z]/.test(next) ? " " : "") + next;
		} else if (!blank(l)) break;
	}
	if (options.length < 2) return undefined;
	const text: string[] = [];
	for (let i = first - 1; i >= 0 && text.length < 8 && !/^\s*─{10,}/.test(lines[i]); i--) {
		if (!blank(lines[i])) text.unshift(lines[i].trim());
	}
	return { text: text.join("\n"), options, cursor };
}

const stringArraySchema = { type: "array", items: { type: "string" } };
const ContractParams = {
	type: "object",
	additionalProperties: false,
	required: ["handshake_id", "request_sha256", "action", "mode", "task_summary", "frame", "assumptions", "no_edits", "scope", "risk_notes", "proof_path", "done_when"],
	properties: {
		handshake_id: { type: "string", description: "Dispatcher-provided handshake id" },
		request_sha256: { type: "string", description: "Dispatcher-provided SHA-256 of the original/effective request" },
		action: { type: "string", enum: ["direct", "delegate", "ask_alignment", "stop"] },
		mode: { type: "string", enum: ["direct", "delegated", "plan", "prototype", "investigate"] },
		task_summary: { type: "string", description: "One compact sentence describing the task" },
		frame: { type: "string", description: "Compact Orient/Frame: goal, boundary, and key risk/ambiguity" },
		assumptions: stringArraySchema,
		material_ambiguity: { type: "object", additionalProperties: false, required: ["question", "why_material"], properties: { question: { type: "string" }, why_material: { type: "string" } } },
		no_edits: { type: "boolean" },
		scope: { type: "string", enum: ["brief", "sustained", "multi_step"] },
		risk_notes: stringArraySchema,
		proof_path: stringArraySchema,
		done_when: stringArraySchema,
	},
} as any;

const spawnToolNames = new Set(["spawn_subagent", "review_subagent", "subagent", "send_to_subagent", "kill_subagent"]);
// Bash is unrestricted code execution: this pattern is a best-effort block, NOT a sandbox.
const spawnCommandPattern = /(?:^|[\s;&|"'(/])(?:tp-spawn|tp-review|tp-pi|tp-claude|tp-task\b[^\n]*\b(?:capability|review)-resolve)\b|\btmux\s+(?:neww|new-window|splitw|split-window|new-session|new|send-keys)\b|(?:^|[\s;&|"'(/])(?:claude|pi)\s+(?:-p|--print|--prompt)\b|\bsubprocess\.(?:run|Popen)\b/i;
// Shell is a programming language, not a read-only API. Before edit authority,
// allow only exact argument-free builtins; no flags, paths, aliases, or expansions.
// Even this is cooperative model policy, not a process/OS sandbox.
// Narrow exception: plain git inspection with simple flags/paths only. No shell
// metacharacters, and no flags that write files or run helper programs.
const readOnlyGit = /^git (?:status|diff|log|branch --show-current)(?: [A-Za-z0-9_.\/=:@,+-]+){0,8}$/;
const unsafeGitFlag = /(?:^| )(?:--output|-O|--ext-diff|--textconv|--open-files-in-pager|--exec|--no-index)(?:[= ]|$)/;
const readOnlyBash = (command: string) => {
	const c = command.trim();
	return /^(?:pwd|true)$/.test(c) || (readOnlyGit.test(c) && !unsafeGitFlag.test(c));
};

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	const root = dirname(agentDir);
	// Legacy contract flow is only entered through an explicit legacy launch.
	// The normal tp-main path sets TP_MODE=orchestrator, even if local.json is old.
	if (process.env.TP_MODE !== "strict") return;
	const bin = (name: string) => join(root, "bin", name);
	// Why a review is incomplete (no Coverage section, or its result cannot be read), or "" when it is complete.
	const reviewIncomplete = async (resultFile: string): Promise<string> => {
		const r = await run("python3", [join(root, "lib", "review_verdict.py"), resultFile]);
		if (r.code !== 0) return `review_verdict refused: ${(r.stderr || r.stdout).trim()}`;
		try {
			const out = JSON.parse(r.stdout);
			return out.complete === true ? "" : String(out.incomplete_reason || "incomplete");
		} catch {
			return "review_verdict output unreadable";
		}
	};
	let enabled = true;
	let active = true;
	let lastCtx: any;
	let activeHandshake: Handshake | undefined;
	let pendingAlignment: PendingAlignment | undefined;
	let pendingDispatch: DispatchPlan | undefined;
	let pendingStopRecord: { id: string; mode: string } | undefined;
	let lastAgentMessages: any[] = [];
	const delegated = new Map<string, Delegated>();

	// Session options are read from the session that owns this agent's pane. Without -t,
	// tmux falls back to the most recently used session whenever it cannot place the
	// caller, which can be another workspace.
	const sessionOpt = (name: string) =>
		must("tmux", ["display-message", "-p", ...(process.env.TMUX_PANE ? ["-t", process.env.TMUX_PANE] : []), `#{${name}}`]);
	// TP_STATE_DIR is authoritative, as in lib/tp.sh tp_session(): a pane whose session keeps
	// its state elsewhere is refused, so the state dir and the main pane never come from
	// two workspaces.
	// The last state dir this pane accepted: the trusted root under which every state write below is checked for symlinks.
	let stateRoot: string | undefined;
	const stateDir = async () => {
		const have = await sessionOpt("@tp_state_dir");
		if (!have) throw new Error("tmux-pi state dir unavailable for this pane; no workspace guess");
		const want = process.env.TP_STATE_DIR;
		if (!want) return (stateRoot = have);
		if (canon(have) !== canon(want)) {
			const session = await sessionOpt("session_name");
			throw new Error(`session '${session}' keeps its state in '${have}', not TP_STATE_DIR '${want}'; refusing to act on another workspace`);
		}
		return (stateRoot = want);
	};
	// The state dir that `path` lies under (as given or resolved); a write outside it is refused.
	const rootOf = (path: string): string => {
		for (const root of stateRoot ? [stateRoot, canon(stateRoot)] : []) {
			const rel = relative(resolve(root), resolve(path));
			if (rel !== ".." && !rel.startsWith("../") && !rel.startsWith("/")) return root;
		}
		throw new Error(`refusing to write ${path}: not under the tmux-pi state dir`);
	};
	const mainWorkdir = async () => {
		const pane = (await sessionOpt("@tp_main_pane")).trim();
		if (!/^%\d+$/.test(pane)) throw new Error("registered main pane unavailable; no workspace guess");
		const cwd = (await must("tmux", ["display-message", "-p", "-t", pane, "#{pane_current_path}"])).trim();
		if (!cwd.startsWith("/") || !cwd) throw new Error("registered main-pane cwd unavailable");
		return cwd;
	};
	const taskDir = async (id: string) => join(await stateDir(), "tasks", id);

	const paneAlive = async (dir: string) => {
		const pane = readJson(join(dir, "meta.json")).pane;
		const panes = await run("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"]);
		return panes.stdout.split("\n").some((l) => l === `${pane} 0`);
	};

	// Post into main's conversation without starting a turn (pi holds it until a running turn ends);
	// main sees it on its next turn.
	const post = (content: string, details: Record<string, unknown>) => {
		if (!active) return;
		try {
			pi.sendMessage({ customType: "tmux-pi-auto", content, display: true, details }, { triggerTurn: false });
		} catch (error) {
			// Reload replaces Pi's extension runtime. Its former watcher may settle
			// before session_shutdown runs; never let a stale ctx crash the new main.
			if (!/extension ctx is stale/i.test(String(error))) throw error;
			active = false;
			lastCtx = undefined;
		}
	};

	const showRunning = () => {
		if (!active || !lastCtx) return;
		const n = delegated.size;
		lastCtx.ui.setStatus("auto-tasks", n ? `AUTO tasks: ${[...delegated.values()].map((t) => t.reviewer ?? t.id).join(", ")}` : undefined);
	};

	// What was said since the last delegation: the one thing a worker cannot rediscover.
	const conversationSinceDelegation = (ctx: any): string => {
		const entries: any[] = ctx.sessionManager.getBranch();
		let start = 0;
		entries.forEach((e, i) => {
			if (e.type === "custom_message" && e.customType === "tmux-pi-auto" && e.details?.kind === "delegated") start = i + 1;
		});
		const lines: string[] = [];
		for (const e of entries.slice(start)) {
			if (e.type === "message" && (e.message?.role === "user" || e.message?.role === "assistant")) {
				const t = messageText(e.message.content).trim();
				if (t) lines.push(`${e.message.role === "user" ? "User" : "Main agent"}: ${t}`);
			} else if (e.type === "custom_message" && e.customType === "tmux-pi-auto") {
				const t = messageText(e.content).trim();
				if (t) lines.push(`[tmux-pi] ${t}`);
			}
		}
		const text = lines.join("\n\n");
		return text.length > 20_000 ? `…${text.slice(-20_000)}` : text;
	};

	const typeInto = async (id: string, message: string) => {
		const pane = readJson(join(await taskDir(id), "meta.json")).pane;
		// Type literally, then Enter separately so the TUI treats the text as a paste, not keys.
		await must("tmux", ["send-keys", "-t", pane, "-l", message.replace(/\r?\n/g, " ")]);
		await sleep(300);
		await must("tmux", ["send-keys", "-t", pane, "Enter"]);
	};

	// Wait until a task settles: waiting or error after `since`, its pane exits, or it escalates.
	// A worker that never starts reporting is usually at a prompt the relay did not recognize; say so once.
	const settle = async (id: string, since: () => number): Promise<{ id: string; ok: boolean; error?: boolean; cancelled?: boolean; escalatedTo?: string }> => {
		const started = Date.now();
		let warned = false;
		for (;;) {
			if (!active) throw new Error("dispatcher session ended while waiting for worker");
			const dir = await taskDir(id);
			if (!active) throw new Error("dispatcher session ended while waiting for worker");
			const escalatedTo = readText(join(dir, "escalated"));
			if (escalatedTo) return { id, ok: false, escalatedTo };
			const state = readJson(join(dir, "state.json"));
			if (!(await paneAlive(dir))) {
				const exit = readText(join(dir, "exit"));
				return { id, ok: exit === "0", error: Boolean(exit && exit !== "0"), cancelled: !exit };
			}
			if (Date.parse(state.updated ?? "") >= since() - 1000 && ["waiting", "error"].includes(state.state)) {
				return { id, ok: state.state === "waiting", error: state.state === "error" };
			}
			// A prompt already relayed to the user (see scanPrompts) needs no notice.
			if (!warned && !asked.has(id) && Date.now() - started > 45_000 && ["starting", "permission", undefined].includes(state.state)) {
				warned = true;
				post(`${id} has not started working; it may be waiting at a prompt. View it from the status pane.`, { kind: "stuck", id });
			}
			await sleep(3000);
		}
	};

	const replyOf = async (id: string) => readText(join(await taskDir(id), "result.md"));
	const markEscalatedFrom = async (id: string, source: string, record?: any) => {
		const file = join(await taskDir(id), "meta.json");
		const meta = readJson(file);
		meta.escalated_from = { id: source, trigger: record?.trigger, reason: record?.reason };
		writeJson(file, meta);
	};
	// Persist review authority before publishing a spawned task. Never downgrade a human gate.
	const reviewMarker = (dir: string) => readJson(join(dir, "review-required.json"));
	const pendingDelegatedReview = (dir: string, id: string, source?: string) => {
		const current = reviewMarker(dir);
		if (current.status === "needs_human") return;
		const previous = source ? reviewMarker(source) : {};
		writeJson(join(dir, "review-required.json"), { required: true,
			status: previous.status === "needs_human" ? "needs_human" : "pending_review", task_id: id,
			reasons: previous.status === "needs_human" ? previous.reasons : ["Required opposite-provider review pending"],
			...(current.status === "resolved" ? { previous_resolution: current.resolution } : {}),
			...(source ? { escalated_from: previous.task_id } : {}), round_nonce: randomUUID() });
	};
	// Reconstruct review policy after /reload; the marker also covers legacy tasks.
	const persistedReview = (dir: string): Decision["review"] => {
		const marker = reviewMarker(dir);
		return readJson(join(dir, "meta.json")).decision?.review === "required" ||
			(marker.required === true && marker.status !== "resolved") ? "required" : "none";
	};
	const rearmDelegatedReview = (dir: string, id: string) => {
		const marker = reviewMarker(dir);
		if (marker.status === "needs_human") return; // Human blockers are sticky.
		if (existsSync(join(dir, "review-required.json")) &&
			(lstatSync(join(dir, "review-required.json")).isSymbolicLink() || marker.required !== true ||
				marker.task_id !== id || !["approved", "pending_review", "resolved"].includes(marker.status) ||
				(marker.status === "resolved" && marker.resolution?.actor !== "human_cli")))
			throw new Error(`Cannot re-arm malformed review marker for ${id}; human inspection required`);
		// An approved round cannot authorize new work. Publish the gate before typing.
		pendingDelegatedReview(dir, id);
		// An earlier collection is not the completion of this new round.
		refuseLinks(rootOf(dir), dir);
		rmSync(join(dir, "completed"), { force: true });
		// The record (task.md) must not keep saying "completed"; tp-contract status rewrites it (run never rejects).
		void run(bin("tp-contract"), ["status", id]);
	};
	const linkDelegatedReview = (source: string, target: string) => {
		const marker = reviewMarker(source);
		if (marker.status === "pending_review") writeJson(join(source, "review-required.json"), { ...marker, review_target: target });
	};
	const requireDelegatedReview = (dir: string, id: string, reason: string, reviewer?: string) => {
		const previous = reviewMarker(dir);
		writeJson(join(dir, "review-required.json"), { required: true, status: "needs_human", task_id: id,
			reasons: [...(Array.isArray(previous.reasons) ? previous.reasons : []), reason], reviewer,
			...(previous.previous_resolution ? { previous_resolution: previous.previous_resolution } : {}),
			...(previous.round_nonce ? { round_nonce: previous.round_nonce } : {}) });
	};
	const authorHash = (dir: string) => existsSync(join(dir, "result.md")) && !lstatSync(join(dir, "result.md")).isSymbolicLink()
		? createHash("sha256").update(readFileSync(join(dir, "result.md"))).digest("hex") : undefined;
	const approveDelegatedReview = (dir: string, id: string, reviewer: string, reviewDir: string, verdict: string, nonce: string, snapshot: string) => {
		const marker = reviewMarker(dir);
		const author = readJson(join(dir, "meta.json"));
		const review = readJson(join(reviewDir, "meta.json"));
		if (marker.status !== "pending_review" || marker.task_id !== id || marker.round_nonce !== nonce ||
			authorHash(dir) !== snapshot ||
			(author.worker !== "pi" && author.worker !== "claude") ||
			review.worker !== (author.worker === "pi" ? "claude" : "pi") ||
			readText(join(reviewDir, "review-of")) !== id ||
			readText(join(reviewDir, "result.md")) !== verdict) return false;
		writeJson(join(dir, "review-required.json"), { ...marker, status: "approved",
			approval: { reviewer_id: reviewer, evidence_sha256: sha256(verdict), author_sha256: snapshot, round_nonce: nonce, approved_at: nowIso() } });
		return true;
	};
	// A result handed to main is completed through the canonical task lifecycle.
	const complete = async (id: string, dir: string) => {
		// The canonical collect CLI owns completion and rejects pending review/capability.
		const result = await run(bin("tp-contract"), ["collect", id]);
		if (result.code !== 0) throw new Error(`Cannot collect ${id}: ${(result.stderr || result.stdout).trim()}`);
	};
	// Stopped or paused by its contract since the last "continue": main's halted dialog takes over.
	const contractHalted = (dir: string) => {
		const events: any[] = readJson(join(dir, "checkpoints.json")).events ?? [];
		const last = events.map((e) => e.action).lastIndexOf("extend");
		return events.slice(last + 1).some((e) => e.action === "stop" || e.action === "pause");
	};
	const summary = (reply: string, path: string) =>
		reply ? `${clip(reply, REPLY_LIMIT)}${reply.length > REPLY_LIMIT ? `\n(full reply: ${path})` : ""}` : "(no reply)";

	// Follow a delegated task through escalation, questions and review until it needs the user.
	const watch = async (task: Delegated) => {
		for (;;) {
			if (!active) return;
			const done = await settle(task.id, () => task.since);
			if (!active) return;
			if (done.escalatedTo) {
				if (task.review === "required") {
					if (!existsSync(join(await taskDir(done.escalatedTo), "review-required.json")))
						pendingDelegatedReview(await taskDir(done.escalatedTo), done.escalatedTo, await taskDir(task.id));
					linkDelegatedReview(await taskDir(task.id), done.escalatedTo);
				}
				post(`${task.id} escalated to ${done.escalatedTo} after repeated tool errors.`, { kind: "escalated", id: task.id, to: done.escalatedTo });
				delegated.delete(task.id);
				task.id = done.escalatedTo;
				delegated.set(task.id, task);
				showRunning();
				continue;
			}
			const dir = await taskDir(task.id);
			if (contractHalted(dir)) break;
			const raw = await replyOf(task.id);
			const reply = raw + (task.report && !raw.includes(task.report) ? `\n\nReport: ${task.report}` : "");
			if (!done.ok) {
				if (done.error && (await autoEscalateSettled(task, "model_error", "worker stopped with an explicit error state", summary(reply, join(dir, "result.md"))))) continue;
				if (task.review === "required") requireDelegatedReview(dir, task.id, done.cancelled ? "Required-review author cancelled with possible partial edits" : "Required-review author failed with possible partial edits");
				post(`${task.id} ${done.cancelled ? "was cancelled" : "stopped with an error"}.\n\n${summary(reply, join(dir, "result.md"))}`, { kind: done.cancelled ? "cancelled" : "failed", id: task.id });
				break;
			}
			const capRequest = capabilityRequestText(raw);
			if (capRequest) {
				const capOutcome = await processCapabilityRequest(task, capRequest);
				if (capOutcome === "granted") continue;
				// Stay attached until the CLI records a resolution AND removes the gate.
				const pending = join(dir, "pending-capability.json");
				let resolvedAt = 0;
				while (true) {
					if (!(await paneAlive(dir))) {
						post(`${task.id} stopped while awaiting capability resolution.`, { kind: "failed", id: task.id });
						return;
					}
					const events: any[] = readJson(join(dir, "checkpoints.json")).events ?? [];
					const lastRequest = events.findLastIndex((e) => e.action === "capability-request" && e.request === capRequest);
					const resolution = events.slice(lastRequest + 1).find((e) => e.action === "capability-resolve" && e.request === capRequest);
					if (!existsSync(pending) && lastRequest >= 0 && resolution && Number.isFinite(Number(resolution.at))) {
						resolvedAt = Number(resolution.at) * 1000;
						break;
					}
					await sleep(500);
				}
				// The pre-resolution waiting state/result is not a completion.
				// Wait for a new worker report, or a genuine pane exit, before settle.
				while ((await paneAlive(dir)) && (await replyOf(task.id)) === raw) await sleep(500);
				task.since = resolvedAt + 1000;
				continue;
			}
			// A worker that asked a question waits for the user; its answer is routed back as a follow-up.
			if (raw.trimEnd().endsWith("?")) {
				post(`${task.id} asks:\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "question", id: task.id });
				break;
			}
			const signal = structuredSignal(raw);
			if (signal && (await autoEscalateSettled(task, signal.trigger, signal.reason, signal.evidence))) continue;
			if (task.review !== "required") {
				const note = "";
				const verb = task.foreground ? "answered" : "finished";
				post(`${task.id} ${verb}${note}.\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "result", id: task.id });
				await complete(task.id, dir);
				break;
			}
			// Capture BEFORE launching the reviewer; a pane edit during the review is not its evidence.
			const markerAtStart = reviewMarker(dir);
			if (markerAtStart.status === "pending_review" && !markerAtStart.round_nonce) {
				writeJson(join(dir, "review-required.json"), { ...markerAtStart, round_nonce: randomUUID() });
			}
			const nonce = reviewMarker(dir).round_nonce;
			const snapshot = authorHash(dir);
			if (typeof nonce !== "string" || !snapshot) {
				requireDelegatedReview(dir, task.id, "Required review has no valid author snapshot or round nonce");
				post(`${task.id} needs human review resolution: author snapshot unavailable.`, { kind: "review-needs-human", id: task.id });
				break;
			}
			const started = Date.now();
			const r = await run(bin("tp-review"), ["--force", task.id]);
			if (!active) return;
			if (r.code !== 0) {
				requireDelegatedReview(dir, task.id, `Required opposite-provider review unavailable: ${(r.stderr || r.stdout).trim()}`);
				post(`${task.id} finished, but its required opposite-provider review could not start: ${(r.stderr || r.stdout).trim()}\n\nHuman action required: start an opposite-provider report-only review manually or decide to accept the risk. tmux-pi will not silently substitute a same-provider reviewer or mark this review skipped.\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "review-needs-human", id: task.id, reason: (r.stderr || r.stdout).trim() });
				lastCtx?.ui.setStatus("auto", `AUTO review needs human · ${task.id}`);
				break;
			}
			task.reviewer = r.stdout.trim();
			const goal = readJson(join(dir, "meta.json"))?.goal;
			if (goal?.id && goal?.workdir) {
				const linked = await run(bin("tp-goal"), ["--project", goal.workdir, "link-reviewer", goal.id, "--author-id", task.id, "--reviewer-id", task.reviewer]);
				if (linked.code !== 0) {
					await run(bin("tp-kill"), [task.reviewer]);
					requireDelegatedReview(dir, task.id, `Goal reviewer linkage failed: ${(linked.stderr || linked.stdout).trim()}`);
					post(`${task.id} goal reviewer linkage failed; human reconciliation required.`, { kind: "review-needs-human", id: task.id });
					break;
				}
			}
			task.rounds += 1;
			showRunning();
			lastCtx?.ui.setStatus("auto", `AUTO review · ${task.reviewer} checking ${task.id}`);
			const reviewed = await settle(task.reviewer, () => started);
			if (!active) return; // The durable pending marker survives; the old watcher owns no verdict.
			const reviewDir = await taskDir(task.reviewer);
			const verdict = await replyOf(task.reviewer);
			await run(bin("tp-kill"), [task.reviewer]);
			const reviewer = task.reviewer;
			task.reviewer = undefined;
			showRunning();
			const parsed = parseVerdict(verdict);
			// A review without a Coverage section is incomplete whatever its first line says; lib/review_verdict.py decides, and
			// a refusal or unreadable answer from it counts as incomplete too.
			const incomplete = parsed.verdict ? await reviewIncomplete(join(reviewDir, "result.md")) : undefined;
				const verdictApproved = reviewed.ok && !incomplete && (parsed.verdict === "APPROVED" || parsed.verdict === "APPROVED_WITH_NOTES");
			const changed = reviewMarker(dir).round_nonce !== nonce || authorHash(dir) !== snapshot;
			if (changed && reviewed.ok && reviewMarker(dir).status !== "needs_human") {
				// The old verdict (including REQUEST_CHANGES) describes an older result. Never collect it.
				if (task.rounds >= MAX_REVIEW_ROUNDS) {
					requireDelegatedReview(dir, task.id, "Author result or review round changed during final review", reviewer);
					post(`${task.id} changed during its final review; human review required.`, { kind: "review-needs-human", id: task.id });
					break;
				}
				if (reviewMarker(dir).status !== "pending_review" || reviewMarker(dir).round_nonce === nonce)
					rearmDelegatedReview(dir, task.id);
				post(`${task.id} changed during ${reviewer}'s review; reviewing the newer result instead.`, { kind: "review", id: task.id });
				if (reviewMarker(dir).round_nonce === nonce) {
					requireDelegatedReview(dir, task.id, "Could not re-arm changed review round", reviewer);
					break;
				}
				// A direct pane edit may not update state.json; the new result is already on disk.
				if (authorHash(dir) !== snapshot && task.since <= started) task.since = 0;
				continue;
			}
			const approved = verdictApproved && approveDelegatedReview(dir, task.id, reviewer, reviewDir, verdict, nonce, snapshot);
			if (!approved && !changed && verdictApproved && reviewMarker(dir).status === "resolved" && reviewMarker(dir).resolution?.actor === "human_cli") {
				await complete(task.id, dir);
				post(`${task.id} completed after recorded human review resolution; ${reviewer} also approved it.`, { kind: "result", id: task.id, review: reviewer });
				break;
			}
			if (approved) {
				await complete(task.id, dir);
				const withNotes = parsed.verdict === "APPROVED_WITH_NOTES";
					post(`${task.id} finished and ${reviewer} approved it${withNotes ? " with notes" : ""}.\n\n${summary(reply, join(dir, "result.md"))}\n\n${withNotes ? `Notes (${reviewer}'s verdict is APPROVED_WITH_NOTES; handle them or say why not):\n${clip(parsed.notes, REPLY_LIMIT)}\n\nFull review:\n` : "Review:\n"}${summary(verdict, join(reviewDir, "result.md"))}`, { kind: "result", id: task.id, review: reviewer, ...(withNotes ? { notes: parsed.notes } : {}) });
				break;
			}
			if (!reviewed.ok || verdictApproved || parsed.verdict !== "REQUEST_CHANGES") {
				const reason = !reviewed.ok ? "Required review failed before approval" : incomplete ?
					`Review incomplete (${incomplete}); its stated verdict ${parsed.verdict} does not count as an approval` : verdictApproved ?
					"Approval cannot clear an existing human gate or lacks independent evidence" :
					`Reviewer verdict unparseable: ${parsed.error ?? "whole first line must be APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES without formatting"}`;
				requireDelegatedReview(dir, task.id, reason, reviewer);
				post(`${task.id} still needs human review resolution; ${reviewer} did not clear the durable gate.\n\n${summary(verdict, join(reviewDir, "result.md"))}`, { kind: "review-needs-human", id: task.id, review: reviewer });
				break;
			}
			if (task.rounds >= MAX_REVIEW_ROUNDS) {
				requireDelegatedReview(dir, task.id, "Required review still requests changes after final round", reviewer);
				post(`${task.id}: ${reviewer} still requests changes after ${task.rounds} rounds.\n\nHuman action required: inspect the canonical task state, author result, and review before continuing.\n\n${summary(verdict, join(reviewDir, "result.md"))}`, { kind: "review-needs-human", id: task.id, review: reviewer });
				break;
			}
			post(`${reviewer} requested changes to ${task.id}; sent back to the author.`, { kind: "review", id: task.id, review: reviewer });
			task.since = Date.now();
			await typeInto(task.id, `A reviewer on another provider requested changes. Their findings are in ${join(reviewDir, "result.md")}. Address each finding, or explain why it does not apply, then report again.`);
		}
		if (active) lastCtx?.ui.setStatus("auto", "AUTO on");
	};

	const follow = (task: Delegated) => {
		if (!active || task.watching) return;
		task.watching = true;
		void watch(task)
			.catch((error) => { if (active) post(`Stopped following ${task.id}: ${(error as Error).message}`, { kind: "failed", id: task.id }); })
			.finally(() => {
				task.watching = false;
			});
	};

	const selectPreflight = async (request: string, decision?: Decision, mode?: string, record = false) => {
		const args = ["preflight", "--json"];
		if (mode) args.push("--mode", mode);
		if (record) args.push("--record", "--name", mode ?? "contract");
		if (decision) args.push("--decision-json", JSON.stringify(decision));
		return JSON.parse(await must(bin("tp-task"), args, request));
	};

	const selectAutomation = async (request: string, decision: Decision, preflight?: any, mainContract?: MainContract): Promise<AutomationPolicy> => {
		const args = ["automation", "--json", "--decision-json", JSON.stringify(decision)];
		if (preflight?.preflight) args.push("--preflight-json", JSON.stringify(preflight.preflight));
		if (mainContract) args.push("--main-contract-json", JSON.stringify(mainContract));
		return JSON.parse(await must(bin("tp-task"), args, request));
	};

	const ownReviewFor = async (hand: Handshake, automation: AutomationPolicy, enrichedPreflight: any, contract: MainContract) => {
		if (hand.ownReviewRequested) return true;
		if (!hand.blockedBy?.length) return !!automation.review?.required;
		if (!hand.decision) return false;
		try {
			// A prior blocker raised the route's review flag. Remove only that
			// inherited flag to distinguish fresh policy risk from inherited review.
			const isolated = await selectAutomation(hand.effective, { ...hand.decision, review: "none" }, { preflight: enrichedPreflight }, contract);
			return !!isolated.review?.required;
		} catch { return false; } // Inherited blocker still closes edit authority.
	};

	const selectContext = async (request: string, decision: Decision, preflight?: any, workdir?: string) => {
		const args = ["context", "--json", "--workdir", workdir ?? await mainWorkdir(), "--decision-json", JSON.stringify(decision)];
		if (preflight?.preflight) args.push("--preflight-json", JSON.stringify(preflight.preflight));
		return JSON.parse(await must(bin("tp-task"), args, request));
	};

	const writeJson = (file: string, value: any) => {
		const root = rootOf(file);
		ensureDir(root, dirname(file));
		writeFileAtomic(root, file, `${JSON.stringify(value, null, 2)}\n`, 0o666);
	};

	const handoffDir = async (id: string) => join(await stateDir(), "handoffs", id);
	const handoffPath = async (hand: Handshake, name: string) => join(await handoffDir(hand.id), name);
	// Python is the sole durable validator. One bounded CLI scan per input; no stale
	// cache may grant authority after a result or marker changes. Fail closed on error.
	const unresolvedReviews = async (): Promise<string[]> => {
		const watching = [...delegated].filter(([, task]) => task.watching).map(([id]) => id);
		const inventory = JSON.parse(await must(bin("tp-task"), ["review-list", "--json", "--watching", ...watching]));
		return inventory.blockers.filter((row: any) => !row.watched)
			.map((row: any) => row.kind === "task" ? `task:${row.id}` : row.id);
	};

	const needsPersistedContract = (decision?: Decision, pf?: Preflight, mode?: string) => {
		const overrides = new Set(decision?.overrides ?? []);
		return Boolean(
			mode ||
			pf?.stop ||
			pf?.durable ||
			pf?.mode === "delegated" ||
			decision?.placement === "subagent" ||
			decision?.review === "required" ||
			overrides.has("placement") ||
			overrides.has("review") ||
			decision?.intent === "implementation" ||
			decision?.intent === "configuration_or_operations",
		);
	};

	const persistScaffold = async (hand: Handshake) => {
		const dir = await handoffDir(hand.id);
		ensureDir(rootOf(dir), dir);
		const scaffold = {
			schema: "tmux-pi-r5-scaffold:v1",
			handshake_id: hand.id,
			request_sha256: hand.requestHash,
			created: nowIso(),
			original_request: hand.original,
			effective_request: hand.effective,
			decision: hand.decision ?? {},
			preflight: hand.preflight?.preflight ?? {},
			mode: hand.mode,
			research: hand.research,
			blocked_by: hand.blockedBy ?? [],
			...(hand.goal ? { goal: { id: hand.goal.id, ticket: hand.goal.ticket, spec_sha256: hand.goal.spec_sha256 } } : {}),
			unverified_expansion: !!hand.unverifiedExpansion,
			note: "Deterministic scaffold only; not dispatch authority. Main Orient/Frame contract is required before substantive spawn/edit-authorized handoff.",
		};
		writeJson(join(dir, "scaffold.json"), scaffold);
		hand.handoff = dir;
	};

	const hiddenHandshakeInstruction = (hand: Handshake) => {
		const pf = hand.preflight?.preflight as Preflight | undefined;
		const decision = hand.decision;
		const route = decision ? `${decision.worker} ${decision.model}/${decision.thinking}; placement ${decision.placement}; review ${decision.review}; route ${decision.route}; intent ${decision.intent}; scope ${decision.scope}` : "router unavailable; no automatic dispatch authority";
		const explicitDelegate = /^\+delegate(?:\s|$)/i.test(hand.original);
		const contractNeed = explicitDelegate && decision
			? "The original user explicitly requested +delegate and a route is available. Before answering the task, you MUST call tmux_pi_commit_contract with action=delegate as your final action in this turn. If material ambiguity requires alignment, commit action=ask_alignment and ask the user instead; do not answer the task directly. Orient/frame the request in the contract; the contract is not authorization. Policy may refuse dispatch or require separate human confirmation for high risk. No worker starts without persisted contract and policy approval."
			: hand.requiresContract ? "This request requires an Orient/Frame contract before delegated/review/stop/edit-authorized or durable work; do not treat the scaffold as authority." : "If this is ordinary low-risk direct work, answer compactly with no visible framing preamble and do not persist a contract.";
		return `[tmux-pi R5 hidden Orient/Frame handshake]\nHandshake: ${hand.id}\nRequest SHA-256: ${hand.requestHash}\nDeterministic scaffold (not authority): ${route}${pf ? `; preflight mode ${pf.mode}, Tier ${pf.artifact_tier}, no_edits=${pf.no_edits}, stop=${pf.stop}` : ""}.\n${contractNeed}\n\nMain obligations:\n- Orient/frame proportionately. For ordinary low-risk direct answers, be compact and do not show a framing preamble.\n- If ambiguity would materially change files, behavior, security/data exposure, scope, architecture, acceptance evidence, or cause significant rework, call tmux_pi_commit_contract with action=ask_alignment, then ask the user.\n- If delegation, required review, stop mode (+plan/+prototype/+investigate), foreground Claude worker, or edit-authorized work is appropriate, call tmux_pi_commit_contract first. The contract is descriptive only; policy decides authority.\n- For +plan/+prototype/+investigate, make no edits and stop at the requested artifact after committing a stop contract.\n- Do not use tp-spawn/tp-review/tmux worker creation, spawn_subagent/review_subagent, or bash-based manual subagent spawning during this handshake. This does not authorize the main model to bypass the dispatch gate.\n- For delegation, call tmux_pi_commit_contract as your final action in the turn; the tool will terminate the turn after persistence and deterministic policy pass so the dispatcher can dispatch.\n\nCurrent request for the main agent:\n${hand.effective}`;
	};

	const stopInstruction = (request: string, preflight: Preflight, record?: any) => {
		const where = record ? `\nRecord: ${record.record}` : "\nRecord: none (Tier 0 compact investigation)";
		const title = preflight.mode === "prototype" ? "Prototype stop-mode" : preflight.mode === "plan" ? "Plan stop-mode" : "Investigate stop-mode";
		return `[tmux-pi ${title} contract]${where}\nMode: ${preflight.mode}; Tier ${preflight.artifact_tier}; no edits.\n${preflight.promotion_notice ?? ""}\n\nDo only this stop-mode work in chat. Do not edit files, run implementation, or spawn an implementation worker unless the user asks after this result. If ambiguity could materially change scope, security/data exposure, architecture, or acceptance evidence, ask first; otherwise state assumptions.\n\nUser request:\n${request}`;
	};

	const delegate = async (ctx: any, decision: Decision, foreground = false, preflight?: any, goal?: Handshake["goal"]): Promise<string> => {
		const tmp = mkdtempSync(join(tmpdir(), "tp-dispatch-"));
		try {
			const args = ["--worker", decision.worker, "--model", decision.model, "--thinking", decision.thinking];
			if (goal) args.push("--workdir", goal.workdir);
			args.push(...(foreground ? ["--name", "fg", "--view"] : ["--name", "auto"]));
			if (!foreground) args.push("--kind", "task");
			args.push("--decision-json", JSON.stringify(decision));
			const conversation = conversationSinceDelegation(ctx);
			if (conversation) {
				writeFileSync(join(tmp, "conversation.md"), conversation);
				args.push("--conversation", join(tmp, "conversation.md"));
			}
			args.push("--note", ASK_IF_UNCLEAR);
			if (goal) args.push("--note", `Explicit bounded goal ${goal.id}: original objective ${goal.objective}; done ${goal.done}; approved scope ${goal.scope}; only paths ${goal.allowed_paths.join(", ") || "no edits"}. No push/deploy/release/destruction/external writes/new dependencies/MCPs/trust approval. Main's R5 per-attempt contract and independent review remain binding.`);
			args.push("--note", 'Only if you encounter an observable model/tool failure that blocks work, report one line: TMUX_PI_ESCALATION_SIGNAL: {"trigger":"model_error","reason":"specific failure (8-300 chars)","evidence":"specific observed output (3-2000 chars)"}. Never emit this for cancellation or as a request for an upgrade.');
			if (!foreground) args.push("--note", REPORT_BACK);
			if (decision.review === "required") args.push("--note", "A reviewer on another provider will check this work when you report.");
			if (preflight?.preflight) {
				writeFileSync(join(tmp, "preflight.json"), JSON.stringify(preflight.preflight));
				args.push("--preflight", join(tmp, "preflight.json"), "--note", preflight.note);
			}
			if (preflight?.mainContract) {
				writeFileSync(join(tmp, "main-contract.json"), JSON.stringify(preflight.mainContract));
				args.push("--main-contract", join(tmp, "main-contract.json"));
			}
			if (preflight?.policyDecision) {
				writeFileSync(join(tmp, "policy-decision.json"), JSON.stringify(preflight.policyDecision));
				args.push("--policy-decision", join(tmp, "policy-decision.json"));
			}
			const brokerContexts = new Set<string>();
			try {
				const broker = await selectContext(decision.task, decision, preflight, goal?.workdir);
				if (broker?.manifest) {
					writeFileSync(join(tmp, "context-manifest.json"), JSON.stringify(broker.manifest));
					args.push("--context-manifest", join(tmp, "context-manifest.json"));
					if (broker.note) args.push("--note", broker.note);
					if (broker.capability_note) args.push("--note", broker.capability_note);
					for (const spec of broker.contexts ?? []) {
						brokerContexts.add(spec);
						args.push("--context", spec);
					}
				}
			} catch (error) {
				args.push("--note", `Context broker unavailable; proceed from the task contract and request missing approved context if needed (${(error as Error).message}).`);
			}
			// Models that explore poorly (local packet_files) get files a Claude model picked for the task.
			// If picking fails the worker starts with the core packet and finds files itself.
			if (!foreground) {
				const picked = await run(bin("tp-route"), ["--pick-files", goal?.workdir ?? await mainWorkdir(), "--model", decision.model], decision.task, 150_000);
				if (picked.code === 0) for (const spec of picked.stdout.split("\n").filter(Boolean)) if (!brokerContexts.has(spec)) args.push("--context", spec);
			}
			return (await must(bin("tp-spawn"), args, decision.task, 60_000)).split("\n")[0];
		} finally {
			rmSync(tmp, { recursive: true, force: true });
		}
	};

	const performDispatch = async (ctx: any, plan: DispatchPlan) => {
		if (!enabled) {
			post(`R5 contract ${plan.handshake.id} is persisted, but /auto is off; automatic dispatch is disabled. Ask the user for an explicit manual next step.`, { kind: "r5-policy", handshake_id: plan.handshake.id, blocked: "auto_off" });
			ctx.ui.setStatus("auto", "AUTO off (contract persisted; dispatch disabled)");
			return;
		}
		const routed = `${plan.decision.model}/${plan.decision.thinking}`;
		const packet = { ...plan.preflight, mainContract: plan.mainContract, policyDecision: plan.policyDecision };
		if (plan.handshake.research) {
			await enqueue({
				question: plan.decision.task,
				review: plan.decision.review === "required" || !!plan.automation.review?.required || plan.handshake.research.review,
				worker: plan.handshake.research.worker,
				model: plan.handshake.research.model,
				thinking: plan.handshake.research.thinking,
				after: plan.handshake.research.after,
				notes: [`R5 main Orient/Frame contract ${plan.handshake.id} persisted before research dispatch.`],
				conversation: conversationSinceDelegation(ctx),
				preflight: packet,
				mainContract: plan.mainContract,
				policyDecision: plan.policyDecision,
				decision: { ...plan.decision, review: plan.decision.review === "required" || plan.automation.review?.required ? "required" : plan.decision.review },
				queued: Date.now(),
			});
			return;
		}
		const review: Decision["review"] = plan.handshake.goal || plan.decision.review === "required" || plan.automation.review?.required ? "required" : plan.decision.review;
		const authorDecision = { ...plan.decision, review };
		const id = await delegate(ctx, authorDecision, plan.foreground, packet, plan.handshake.goal);
		if (plan.handshake.goal) {
			const goal = plan.handshake.goal;
			const metaFile = join(await taskDir(id), "meta.json");
			writeJson(metaFile, { ...readJson(metaFile), goal: { id: goal.id, ticket: goal.ticket, spec_sha256: goal.spec_sha256, workdir: goal.workdir } });
			const linked = await run(bin("tp-goal"), ["--project", goal.workdir, "link-task", goal.id, "--ticket", goal.ticket, "--task-id", id]);
			if (linked.code !== 0) {
				await run(bin("tp-kill"), [id]);
				throw new Error(`Goal author linkage failed; worker stopped and review remains binding: ${(linked.stderr || linked.stdout).trim()}`);
			}
		}
		if (review === "required") pendingDelegatedReview(await taskDir(id), id);
		const task: Delegated = { id, request: plan.decision.task, review, rounds: 0, since: Date.now(), foreground: plan.foreground };
		delegated.set(id, task);
		const pf = plan.preflight?.preflight as Preflight | undefined;
		post(
			`${plan.automation.notice}\nR5 main contract persisted (${plan.handshake.id}) before ${plan.foreground ? "foreground" : "subagent"} dispatch. ${plan.foreground ? "Answering in the viewer" : "Delegated"}: ${id} (${plan.decision.worker} ${routed}; ${plan.decision.intent}, ${plan.decision.scope}, review ${review}${pf ? `; Tier ${pf.artifact_tier}` : ""}).\n\n${plan.decision.task}`,
			{ kind: "delegated", id, foreground: plan.foreground, automation: plan.automation, preflight: pf, main_contract: plan.mainContract, policy_decision: plan.policyDecision, ...plan.decision },
		);
		ctx.ui.setStatus("auto", `AUTO ${plan.foreground ? "main" : "subagent"} · ${id} · ${routed} · R5`);
		showRunning();
		follow(task);
	};

	// Running delegated tasks that could take a follow-up, most recent first.
	const followUpCandidates = async () => {
		const live: Delegated[] = [];
		for (const task of [...delegated.values()].reverse()) {
			if (await paneAlive(await taskDir(task.id))) live.push(task);
			else if (!task.reviewer) delegated.delete(task.id);
		}
		showRunning();
		return live.slice(0, MAX_FOLLOW_UP_TASKS);
	};

	// The task this message is for, or undefined for a new request.
	const followUpTarget = async (ctx: any, text: string): Promise<Delegated | undefined> => {
		const live = await followUpCandidates();
		if (!live.length) return undefined;
		const tasks = await Promise.all(live.map(async (t) => ({ id: t.id, task: t.request, reply: await replyOf(t.id) })));
		let ask = live.map((t) => t.id);
		const r = await run(bin("tp-route"), ["--follow-up"], JSON.stringify({ message: text, tasks }));
		if (r.code === 0) {
			const out = JSON.parse(r.stdout);
			if (out.target) return live.find((t) => t.id === out.target);
			ask = out.ask;
		}
		if (!ask.length) return undefined;
		const NEW = "A new request";
		const choice = await ctx.ui.select(
			"Is this message for a running task?",
			[...ask.map((id: string) => `${id}: ${clip(delegated.get(id)?.request ?? "", 60).replace(/\n.*/s, "")}`), NEW],
		);
		if (!choice || choice === NEW) return undefined;
		return delegated.get(choice.slice(0, choice.indexOf(":")));
	};

	// Claude workers stopped at a choice prompt: ask the user in main and key the pick in.
	// Each prompt is asked once; one dialog at a time. The dialog closes itself if the prompt
	// is answered in the worker's own pane.
	const asked = new Map<string, string>();
	// One dialog at a time: prompt relays and halted-task decisions share main's UI.
	let dialogs: Promise<unknown> = Promise.resolve();
	const exclusive = <T>(f: () => Promise<T>): Promise<T> => {
		const p = dialogs.then(f);
		dialogs = p.catch(() => undefined);
		return p;
	};
	const capture = async (pane: string) => (await run("tmux", ["capture-pane", "-p", "-t", pane])).stdout;
	const signature = (p: Prompt) => `${p.text}\n${p.options.join("\n")}`;

	const answer = async (pane: string, prompt: Prompt, index: number) => {
		const key = index > prompt.cursor ? "Down" : "Up";
		for (let i = 0; i < Math.abs(index - prompt.cursor); i++) {
			await must("tmux", ["send-keys", "-t", pane, key]);
			await sleep(100);
		}
		await must("tmux", ["send-keys", "-t", pane, "Enter"]);
	};

	const relay = async (id: string, pane: string, prompt: Prompt) => {
		const ui = lastCtx.ui;
		const closed = new AbortController();
		void (async () => {
			while (!closed.signal.aborted) {
				await sleep(2000);
				const now = parsePrompt(await capture(pane));
				if (!now || signature(now) !== signature(prompt)) closed.abort();
			}
		})();
		const VIEW = "Show it in the viewer";
		const LATER = "Leave it for now";
		const choice = await ui.select(`${id} is waiting at a prompt:\n\n${prompt.text}`, [...prompt.options, VIEW, LATER], {
			signal: closed.signal,
		});
		const answered = closed.signal.aborted;
		closed.abort();
		if (answered || !choice || choice === LATER) return;
		if (choice === VIEW) {
			await run(bin("tp-view"), [id]);
			return;
		}
		await answer(pane, prompt, prompt.options.indexOf(choice));
		const question = prompt.text.split("\n").filter((l) => l.includes("?")).pop() ?? "prompt";
		post(`Answered ${id}'s prompt (${question}): ${choice}`, { kind: "prompt", id, choice });
	};

	const scanPrompts = async () => {
		const tasks = join(await stateDir(), "tasks");
		const live = new Set((await run("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"])).stdout.split("\n"));
		let ids: string[] = [];
		try {
			ids = readdirSync(tasks);
		} catch {
			return;
		}
		for (const id of ids) {
			const meta = readJson(join(tasks, id, "meta.json"));
			const state = readJson(join(tasks, id, "state.json")).state;
			if (meta.worker !== "claude" || !live.has(`${meta.pane} 0`) || !["starting", "permission", undefined].includes(state)) {
				asked.delete(id);
				continue;
			}
			const prompt = parsePrompt(await capture(meta.pane));
			if (!prompt) {
				asked.delete(id);
				continue;
			}
			if (asked.get(id) === signature(prompt)) continue;
			asked.set(id, signature(prompt));
			await exclusive(() => relay(id, meta.pane, prompt));
			return;
		}
	};

	// Contract checkpoints (M9): tp-contract types reminders into the worker and pauses it;
	// budget stops are posted into main, and a halted task gets a dialog: continue, narrow,
	// escalate or discard.
	const enforceContracts = async () => {
		const r = await run(bin("tp-contract"), ["enforce"]);
		for (const line of r.stdout.split("\n").filter(Boolean)) {
			const a = JSON.parse(line);
			if (a.action === "halted") void exclusive(() => decide(a)).catch((error) => post(`${a.id}: ${(error as Error).message}`, { kind: "failed", id: a.id }));
			else if (a.action === "stop") {
				post(`${a.id}: ${a.reason}; it was asked to save its report (${a.artifact}) and stop.`, { kind: "budget", id: a.id });
			} else lastCtx?.ui.notify(`${a.id}: ${a.reason}; ${a.action === "pause" ? "paused it" : "reminded it"}`, "info");
		}
		await pumpQueue();
	};

	const capabilityRequestText = (reply: string): string | undefined => {
		const match = /(^|\n)\s*CAPABILITY_REQUEST\s*:\s*(.+?)(?=\n\s*CAPABILITY_REQUEST\s*:|$)/is.exec(reply);
		return match?.[2]?.trim();
	};

	const markCapabilityNeedsInput = async (task: Delegated, request: string, event: any, reason: string) => {
		const dir = await taskDir(task.id);
		const pending = { created: new Date().toISOString(), request, event, reason, review_required: task.review === "required" };
		writeJson(join(dir, "pending-capability.json"), pending);
		const checkpoints = readJson(join(dir, "checkpoints.json"));
		const events = Array.isArray(checkpoints.events) ? checkpoints.events : [];
		events.push({ action: "capability-request", at: Date.now() / 1000, outcome: "human_required", request, reason, review_required: task.review === "required" });
		writeJson(join(dir, "checkpoints.json"), { events });
		await run(bin("tp-contract"), ["status"]);
	};

	const processCapabilityRequest = async (task: Delegated, request: string): Promise<"granted" | "pending"> => {
		const dir = await taskDir(task.id);
		const manifest = join(dir, "context-manifest.json");
		if (!existsSync(manifest)) {
			await markCapabilityNeedsInput(task, request, {}, "no Slice 5 manifest exists for automatic grant");
			post(`${task.id} requested a capability, but no Slice 5 manifest exists for automatic grant. Human approval needed. Completion, review and escalation are blocked until resolved.\n\n${request}`, { kind: "capability", id: task.id, request, decision: "human_required" });
			return "pending";
		}
		const r = await run(bin("tp-task"), ["capability-request", "--manifest", manifest, "--json", "--", request]);
		if (r.code !== 0) {
			await markCapabilityNeedsInput(task, request, {}, `policy processing failed: ${(r.stderr || r.stdout).trim()}`);
			post(`${task.id} requested a capability, but policy processing failed: ${(r.stderr || r.stdout).trim()}\n\nHuman approval needed. Completion, review and escalation are blocked until resolved.\n\n${request}`, { kind: "capability", id: task.id, request, decision: "human_required" });
			return "pending";
		}
		const out = JSON.parse(r.stdout);
		const event = out.event ?? {};
		if (event.decision === "granted") {
			post(`${task.id}: capability request granted by Slice 5 policy (${event.category ?? event.id}).`, { kind: "capability", id: task.id, request, event });
			task.since = Date.now();
			await typeInto(task.id, `Capability request granted by deterministic Slice 5 policy. Use only the disclosed bounded authority below; do not expand beyond it without asking.\n\n${out.note ?? ""}`);
			return "granted";
		}
		if (event.decision === "unavailable") {
			const evidence = JSON.stringify({ capability_request: request, event });
			if (await autoEscalateSettled(task, "missing_capability", event.reason ?? "required capability unavailable after policy evaluation", evidence)) return "granted";
		}
		await markCapabilityNeedsInput(task, request, event, event.reason ?? "policy requires human approval");
		post(`${task.id} requested a capability requiring human approval (${event.reason ?? "policy"}). Completion, review and escalation are blocked until resolved.\n\n${request}`, { kind: "capability", id: task.id, request, event });
		lastCtx?.ui.setStatus("auto", `AUTO needs input · ${task.id}`);
		return "pending";
	};

	const structuredSignal = (reply: string): { trigger: "model_error" | "missing_capability"; reason: string; evidence: string } | undefined => {
		const match = /(^|\n)\s*TMUX_PI_ESCALATION_SIGNAL\s*:\s*(\{[^\n]+\})\s*(?=\n|$)/.exec(reply);
		if (!match) return undefined;
		try {
			const signal = JSON.parse(match[2]);
			const trigger = signal.trigger;
			const reason = String(signal.reason ?? "").trim();
			const evidence = String(signal.evidence ?? "").trim();
			if (trigger !== "model_error" && trigger !== "missing_capability") return undefined;
			if (reason.length < 8 || reason.length > 300 || evidence.length < 3 || evidence.length > 2000) return undefined;
			return { trigger, reason, evidence };
		} catch {
			return undefined;
		}
	};

	// A stronger model is not a new grant: carry recorded lineage into the target.
	const escalationLineage = (dir: string): string[] => {
		const args: string[] = [];
		const preflightPath = join(dir, "preflight.json");
		const metaPreflight = readJson(join(dir, "meta.json")).preflight;
		if (!existsSync(preflightPath) && metaPreflight?.schema) writeJson(preflightPath, metaPreflight);
		for (const [file, flag] of [["preflight.json", "--preflight"], ["main-contract.json", "--main-contract"], ["policy-decision.json", "--policy-decision"], ["context-manifest.json", "--context-manifest"]]) {
			if (existsSync(join(dir, file))) args.push(flag, join(dir, file));
		}
		return args;
	};
	const autoEscalateSettled = async (task: Delegated, trigger: "model_error" | "missing_capability", reason: string, evidence: string): Promise<boolean> => {
		const dir = await taskDir(task.id);
		const planned = await run(bin("tp-contract"), ["escalate", task.id, "--trigger", trigger, "--reason", reason, "--evidence", evidence]);
		if (planned.code !== 0) return false;
		const plan = JSON.parse(planned.stdout);
		const target = plan.target;
		const contexts = [join(dir, "packet.md")];
		if (existsSync(join(dir, "result.md"))) contexts.push(join(dir, "result.md"));
		try {
			const sourceId = task.id;
			const id = (await must(
				bin("tp-spawn"),
				[
					"--worker", target.worker,
					"--model", target.model,
					"--thinking", target.thinking,
					"--name", `esc-${sourceId}`,
					"--kind", "task",
					"--decision-json", JSON.stringify({ ...target, route: "escalated", scope: "sustained", review: task.review }),
					...escalationLineage(dir),
					...contexts.flatMap((c) => ["--context", c]),
					"--note", `Automatic one-step escalation from ${sourceId}: ${reason}. Evidence is in the source task files. This is the only automatic escalation; if blocked, save partial state and ask.`,
				],
				task.request,
				60_000,
			)).split("\n")[0];
			try {
				await markEscalatedFrom(id, sourceId, plan.record);
				if (task.review === "required") pendingDelegatedReview(await taskDir(id), id, dir);
				await must(bin("tp-contract"), ["escalated", sourceId, "--to", id, "--outcome", "started", "--record-json", JSON.stringify(plan.record)]);
				if (task.review === "required") linkDelegatedReview(dir, id);
			} catch (error) {
				await run(bin("tp-kill"), [id]);
				throw error;
			}
			await run(bin("tp-kill"), [sourceId]);
			delegated.delete(sourceId);
			task.id = id;
			task.since = Date.now();
			delegated.set(id, task);
			post(`${plan.record.source.id}: automatically escalated to ${id} (${target.worker} ${target.model}/${target.thinking}) for ${reason}.`, { kind: "escalated", id: plan.record.source.id, to: id, target, record: plan.record });
			showRunning();
			return true;
		} catch (error) {
			await run(bin("tp-contract"), ["escalated", task.id, "--outcome", "failed", "--record-json", JSON.stringify(plan.record), "--message", (error as Error).message]);
			post(`${task.id}: automatic escalation could not start; source task is left recoverable. ${(error as Error).message}`, { kind: "escalation-needs-human", id: task.id, target, record: plan.record });
			return false;
		}
	};

	const autoEscalateHalted = async (a: any, dir: string, request: string): Promise<boolean> => {
		const evidence = [`artifact=${a.artifact}`, `status=${a.status ?? "none"}`, `reason=${a.reason}`].join("; ");
		const trigger = ["budget", "checkpoint", "tool_stall", "tool_errors"].includes(a.trigger) ? a.trigger : "checkpoint";
		const planned = await run(bin("tp-contract"), ["escalate", a.id, "--trigger", trigger, "--reason", a.reason, "--evidence", evidence]);
		if (planned.code !== 0) return false;
		const plan = JSON.parse(planned.stdout);
		const target = plan.target;
		const task: Delegated = delegated.get(a.id) ?? { id: a.id, request, review: persistedReview(dir), rounds: 0, since: 0, report: a.kind === "research" ? a.artifact : undefined };
		const note = `Automatic one-step escalation from ${a.id}: ${a.reason}. Partial artifact: ${a.artifact}. Read the original packet and partial artifact first; do not repeat unproductive searches/tool calls. This is the only automatic escalation; if you hit a checkpoint or budget, save partial state and ask.`;
		if (a.kind === "research") {
			try {
				await enqueue({
					question: request,
					review: task.review === "required",
					worker: target.worker,
					model: target.model,
					thinking: target.thinking,
					after: [],
					notes: [note],
					conversation: "",
					replaces: a.id,
					how: "escalate",
					rounds: task.rounds,
					escalationRecord: plan.record,
					queued: Date.now(),
				});
			} catch (error) {
				await run(bin("tp-contract"), ["escalated", a.id, "--outcome", "failed", "--record-json", JSON.stringify(plan.record), "--message", (error as Error).message]);
				post(`${a.id}: automatic escalation could not start; source task is left recoverable. ${(error as Error).message}`, { kind: "escalation-needs-human", id: a.id, target, record: plan.record });
				return false;
			}
			post(`${a.id}: automatic escalation planned (${plan.record.source.model}/${plan.record.source.thinking} → ${target.model}/${target.thinking}) for ${a.reason}.`, { kind: "escalated", id: a.id, target, record: plan.record });
			return true;
		}
		try {
			const contexts = [join(dir, "packet.md")];
			if (a.artifact && existsSync(a.artifact)) contexts.push(a.artifact);
			const id = (await must(
				bin("tp-spawn"),
				[
					"--worker", target.worker,
					"--model", target.model,
					"--thinking", target.thinking,
					"--name", `esc-${a.id}`,
					"--kind", "task",
					"--decision-json", JSON.stringify({ ...target, route: "escalated", scope: "sustained", review: task.review }),
					...escalationLineage(dir),
					...contexts.flatMap((c) => ["--context", c]),
					"--note", note,
				],
				request,
				60_000,
			)).split("\n")[0];
			try {
				await markEscalatedFrom(id, a.id, plan.record);
				if (task.review === "required") pendingDelegatedReview(await taskDir(id), id, dir);
				await must(bin("tp-contract"), ["escalated", a.id, "--to", id, "--outcome", "started", "--record-json", JSON.stringify(plan.record)]);
				if (task.review === "required") linkDelegatedReview(dir, id);
			} catch (error) {
				await run(bin("tp-kill"), [id]);
				throw error;
			}
			await run(bin("tp-kill"), [a.id]);
			delegated.delete(a.id);
			const next: Delegated = { ...task, id, since: Date.now() };
			delegated.set(id, next);
			post(`${a.id}: automatically escalated to ${id} (${target.worker} ${target.model}/${target.thinking}) for ${a.reason}.`, { kind: "escalated", id: a.id, to: id, target, record: plan.record });
			showRunning();
			follow(next);
			return true;
		} catch (error) {
			await run(bin("tp-contract"), ["escalated", a.id, "--outcome", "failed", "--record-json", JSON.stringify(plan.record), "--message", (error as Error).message]);
			post(`${a.id}: automatic escalation could not start; source task is left recoverable. ${(error as Error).message}`, { kind: "escalation-needs-human", id: a.id, target, record: plan.record });
			return false;
		}
	};

	// A halted contract task: first try the one automatic qualified escalation, then ask the user.
	const decide = async (a: any) => {
		const dir = await taskDir(a.id);
		const request = requestText(dir);
		const reply = await replyOf(a.id);
		if (await autoEscalateHalted(a, dir, request)) return;
		post(`${a.id} ${a.reason}. Report: ${a.artifact}\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "halted", id: a.id, reason: a.reason });
		const config = a.kind === "research" ? JSON.parse(await must(bin("tp-contract"), ["research"])) : undefined;
		const CONTINUE = "Continue with one more budget";
		const NARROW = "Narrow the question (new research task)";
		const ESCALATE = config ? `Escalate to ${config.escalate.model} (new research task)` : "";
		const DISCARD = "Discard it";
		const LATER = "Decide later";
		const options = [a.extended ? "" : CONTINUE, config ? NARROW : "", ESCALATE, DISCARD, LATER].filter(Boolean);
		const choice = await lastCtx.ui.select(`${a.id} ${a.reason}. What next?`, options);
		if (!choice || choice === LATER) {
			lastCtx.ui.notify(`${a.id} left as is; +followup ${a.id} <message> or the status pane can still reach it`, "info");
			return;
		}
		const task: Delegated = delegated.get(a.id) ?? { id: a.id, request, review: persistedReview(dir), rounds: 0, since: 0, report: config ? a.artifact : undefined };
		if (choice === CONTINUE) {
			const extended = JSON.parse(await must(bin("tp-contract"), ["extend", a.id]));
			if (task.review === "required") rearmDelegatedReview(dir, a.id);
			task.since = Date.now();
			task.rounds = 0;
			delegated.set(a.id, task);
			await typeInto(a.id, extended.message);
			post(`Continued ${a.id} with one more budget.`, { kind: "follow-up", id: a.id });
			showRunning();
			follow(task);
			return;
		}
		if (choice === DISCARD) {
			await run(bin("tp-contract"), ["event", a.id, "discard"]);
			await run(bin("tp-kill"), [a.id]);
			await run(bin("tp-ledger"), ["record", a.id]);
			delegated.delete(a.id);
			showRunning();
			post(`Discarded ${a.id}. Its report stays at ${a.artifact}.`, { kind: "discarded", id: a.id });
			return;
		}
		let question = request;
		if (choice === NARROW) {
			const narrowed = (await lastCtx.ui.input(`A narrower question to replace ${a.id}`, request))?.trim();
			if (!narrowed) return;
			question = narrowed;
		}
		const route = choice === ESCALATE ? config.escalate : config;
		await run(bin("tp-kill"), [a.id]);
		delegated.delete(a.id);
		showRunning();
		// Human narrow/escalate is a deliberate exception to the model-initiated R5 gate;
		// the user's dialog choice is recorded in the source event, not a fresh main contract.
		await run(bin("tp-contract"), ["event", a.id, choice === ESCALATE ? "human-escalate" : "human-narrow"]);
		await enqueue({
			question,
			review: task.review === "required",
			worker: route.worker,
			model: route.model,
			thinking: route.thinking,
			after: [],
			notes: [`An earlier attempt at this research (${a.id}) stopped before finishing; its partial report is ${a.artifact}. Read it first, build on it, and do not repeat its searches.`],
			conversation: "",
			replaces: a.id,
			how: choice === ESCALATE ? "escalate" : "narrow",
			queued: Date.now(),
		});
	};

	// The research queue: requests wait there for their dependencies and a free slot
	// (tp-contract slot), and start in order when both are there.
	const queueFile = async () => join(await stateDir(), "research-queue.json");
	const loadQueue = async (): Promise<Queued[]> => {
		const q = readJson(await queueFile());
		return Array.isArray(q) ? q : [];
	};
	const saveQueue = async (q: Queued[]) => {
		const file = await queueFile();
		writeFileAtomic(rootOf(file), file, `${JSON.stringify(q, null, 1)}\n`);
	};
	let queueLock: Promise<unknown> = Promise.resolve();
	const serial = <T>(f: () => Promise<T>): Promise<T> => {
		const p = queueLock.then(f);
		queueLock = p.catch(() => undefined);
		return p;
	};
	const slot = async (q: Queued) =>
		JSON.parse(await must(bin("tp-contract"), ["slot", "--model", q.model, ...(q.after.length ? ["--after", q.after.join(",")] : [])]));

	const startResearch = async (q: Queued) => {
		const tasks = join(await stateDir(), "tasks");
		const tmp = mkdtempSync(join(tmpdir(), "tp-research-"));
		try {
			const args = ["--worker", q.worker, "--model", q.model, "--thinking", q.thinking, "--name", "research", "--kind", "research"];
			if (q.conversation) {
				writeFileSync(join(tmp, "conversation.md"), q.conversation);
				args.push("--conversation", join(tmp, "conversation.md"));
			}
			const reports = q.after.map((id) => `${id}: ${readJson(join(tasks, id, "meta.json")).contract?.artifact ?? "(no report)"}`);
			if (reports.length) args.push("--note", `This question builds on earlier research. Read these reports first and do not repeat their searches: ${reports.join("; ")}.`);
			for (const n of q.notes) args.push("--note", n);
			args.push("--note", ASK_IF_UNCLEAR, "--note", RESEARCH_REPORT);
			if (q.review) args.push("--note", "A reviewer on another provider will check the report's sources when you finish.");
			if (q.preflight?.preflight) {
				writeFileSync(join(tmp, "preflight.json"), JSON.stringify(q.preflight.preflight));
				args.push("--preflight", join(tmp, "preflight.json"), "--note", q.preflight.note);
			}
			if (q.mainContract) {
				writeFileSync(join(tmp, "main-contract.json"), JSON.stringify(q.mainContract));
				args.push("--main-contract", join(tmp, "main-contract.json"));
			}
			if (q.policyDecision) {
				writeFileSync(join(tmp, "policy-decision.json"), JSON.stringify(q.policyDecision));
				args.push("--policy-decision", join(tmp, "policy-decision.json"));
			}
			if (q.decision || q.review) args.push("--decision-json", JSON.stringify({ ...(q.decision ?? { model: q.model, intent: "research" }), review: q.review ? "required" : q.decision?.review ?? "none" }));
			const id = (await must(bin("tp-spawn"), args, q.question, 60_000)).split("\n")[0];
			const contract = readJson(join(tasks, id, "meta.json")).contract ?? {};
			const task: Delegated = { id, request: q.question, review: q.review ? "required" : "none", rounds: q.rounds ?? 0, since: Date.now(), report: contract.artifact };
			if (q.review) pendingDelegatedReview(join(tasks, id), id, q.replaces ? join(tasks, q.replaces) : undefined);
			delegated.set(id, task);
			if (q.replaces) {
				if (q.how === "escalate" && q.escalationRecord) {
					try {
						await markEscalatedFrom(id, q.replaces, q.escalationRecord);
						await must(bin("tp-contract"), ["escalated", q.replaces, "--to", id, "--outcome", "started", "--record-json", JSON.stringify(q.escalationRecord)]);
						if (q.review) linkDelegatedReview(join(tasks, q.replaces), id);
					} catch (error) {
						delegated.delete(id);
						await run(bin("tp-kill"), [id]);
						throw error;
					}
					await run(bin("tp-kill"), [q.replaces]);
				} else {
					await run(bin("tp-contract"), ["event", q.replaces, q.how ?? "narrow", "--to", id]);
				}
				await run(bin("tp-ledger"), ["record", q.replaces]);
			}
			const minutes = Math.round((contract.budget?.seconds ?? 0) / 60);
			const why = [q.review ? "reviewed" : "", q.after.length ? `building on ${q.after.join(", ")}` : "", q.replaces ? `${q.how === "escalate" ? "escalating" : "narrowing"} ${q.replaces}` : ""].filter(Boolean);
			post(
				`Research started: ${id} (${q.worker} ${q.model}/${q.thinking}, up to ${minutes} min${why.length ? `; ${why.join("; ")}` : ""}). Report: ${contract.artifact}\n\n${q.question}`,
				{ kind: "delegated", id, research: true, report: contract.artifact },
			);
			lastCtx?.ui.setStatus("auto", `AUTO research · ${id}`);
			showRunning();
			follow(task);
		} finally {
			rmSync(tmp, { recursive: true, force: true });
		}
	};

	const enqueue = (q: Queued) =>
		serial(async () => {
			const free = await slot(q);
			if (free.ok) return startResearch(q);
			await saveQueue([...(await loadQueue()), q]);
			post(`Research queued (${free.reason}): ${q.question}`, { kind: "queued", research: true });
		});

	const pumpQueue = () =>
		serial(async () => {
			let queue = await loadQueue();
			for (const q of [...queue]) {
				const free = await slot(q);
				if (!free.ok) continue;
				queue = queue.filter((x) => x !== q);
				await saveQueue(queue);
				await startResearch(q).catch(async (error) => {
					if (q.replaces && q.escalationRecord) await run(bin("tp-contract"), ["escalated", q.replaces, "--outcome", "failed", "--record-json", JSON.stringify(q.escalationRecord), "--message", (error as Error).message]);
					post(`Queued research could not start: ${(error as Error).message}\n\n${q.question}${q.replaces ? `\nSource ${q.replaces} remains halted; use +followup or inspect its record to continue/narrow/discard.` : ""}`, { kind: q.replaces ? "halted" : "failed", id: q.replaces });
				});
			}
		});

	// Running research tasks this question builds on: the follow-up judge's clear claimant, if any.
	const dependsOn = async (question: string): Promise<string[]> => {
		const status = await run(bin("tp-contract"), ["status"]);
		const tasks = join(await stateDir(), "tasks");
		const running = status.stdout.split("\n").map((l) => l.split("\x1f")).filter(([id, life]) =>
			id && ["starting", "working", "needs_input"].includes(life) && readJson(join(tasks, id, "meta.json")).contract?.kind === "research");
		if (!running.length) return [];
		const candidates = await Promise.all(running.slice(-MAX_FOLLOW_UP_TASKS).map(async ([id]) => ({ id, task: requestText(join(tasks, id)), reply: await replyOf(id) })));
		const r = await run(bin("tp-route"), ["--follow-up"], JSON.stringify({ message: question, tasks: candidates }));
		if (r.code !== 0) return [];
		const target = JSON.parse(r.stdout).target;
		return target ? [target] : [];
	};

	// +research: one bounded question to a research worker under a contract.
	const research = async (ctx: any, question: string, review: boolean, after: string[]) => {
		const config = JSON.parse(await must(bin("tp-contract"), ["research"]));
		if (!after.length) after = await dependsOn(question);
		await enqueue({
			question,
			review,
			worker: config.worker,
			model: config.model,
			thinking: config.thinking,
			after,
			notes: [],
			conversation: conversationSinceDelegation(ctx),
			queued: Date.now(),
		});
	};

	let scanning = false;
	pi.on("session_shutdown", () => {
		// /reload replaces this runtime; orphaned watchers and scans must not use
		// its captured pi/ctx or collect a review after the new runtime starts.
		active = false;
		lastCtx = undefined;
	});
	pi.on("session_start", (_event, ctx) => {
		if (!active) return;
		lastCtx = ctx;
		if (scanning) return;
		scanning = true;
		void (async () => {
			while (active) {
				await scanPrompts().catch(() => undefined);
				if (active) await sleep(3000);
			}
		})();
		void (async () => {
			while (active) {
				await enforceContracts().catch(() => undefined);
				if (active) await sleep(15_000);
			}
		})();
	});

	// +followup [id] <message>: the task named, the only running one, or the user's pick.
	const forcedTarget = async (ctx: any, rest: string): Promise<{ task?: Delegated; text: string }> => {
		const [word, ...more] = rest.split(/\s+/);
		if (word && !delegated.has(word) && readText(join(await taskDir(word), "meta.json")) && (await paneAlive(await taskDir(word)))) {
			// A subagent main started by hand: follow it from here on.
			const dir = await taskDir(word);
			const task: Delegated = { id: word, request: requestText(dir), review: persistedReview(dir), rounds: 0, since: 0 };
			delegated.set(word, task);
			return { task, text: more.join(" ") };
		}
		const live = await followUpCandidates();
		const named = live.find((t) => t.id === word);
		if (named) return { task: named, text: more.join(" ") };
		if (live.length <= 1) return { task: live[0], text: rest };
		const choice = await ctx.ui.select(
			"Which running task is this for?",
			live.map((t) => `${t.id}: ${clip(t.request, 60).replace(/\n.*/s, "")}`),
		);
		return { task: choice ? delegated.get(choice.slice(0, choice.indexOf(":"))) : undefined, text: rest };
	};

	pi.registerTool({
		name: "tmux_pi_commit_contract",
		executionMode: "sequential",
		label: "Commit tmux-pi R5 contract",
		description:
			"Persist the main agent's compact Orient/Frame contract for the current tmux-pi R5 handshake before any delegation, required review, stop-mode output, or edit-authorized work. This tool cannot grant authority; deterministic policy runs after persistence.",
		promptSnippet: "Commit a tmux-pi R5 Orient/Frame contract before delegated/review/stop/edit-authorized work",
		promptGuidelines: [
			"Use tmux_pi_commit_contract before delegation, required review, +plan/+prototype/+investigate stop-mode output, or edit-authorized work.",
			"Do not include authority-grant fields such as authorized, approved, can_dispatch, or capability_grants.",
			"For ordinary low-risk direct answers, do not call the tool unless review, stop, ambiguity alignment, or edits are involved.",
		],
		parameters: ContractParams,
		async execute(_toolCallId, params: MainContract, _signal, _onUpdate, ctx) {
			const hand = activeHandshake;
			if (!hand) throw new Error("No active tmux-pi R5 handshake; cannot commit a contract or dispatch.");
			if (hand.committed) throw new Error("This R5 handshake already committed; start a new user request for a new contract.");
			if (params.handshake_id !== hand.id || params.request_sha256 !== hand.requestHash) {
				throw new Error("R5 contract nonce/hash mismatch; no dispatch or edit authorization granted.");
			}
			let decision = hand.decision;
			const preflight = hand.preflight?.preflight ?? {};
			if (hand.mode && (params.action !== "stop" || params.mode !== hand.mode || !params.no_edits)) {
				throw new Error(`+${hand.mode} requires a matching no-edit stop contract; no dispatch or edit authority granted.`);
			}
			if (params.action === "stop" && (!hand.mode || params.mode !== hand.mode)) throw new Error("Stop contract must match an explicit stop-mode request.");
			const mainArgs = [
				"main-contract",
				"--json",
				"--handshake-id",
				hand.id,
				"--request-sha256",
				hand.requestHash,
				"--record",
				hand.mode ? "never" : "auto",
				"--decision-json",
				JSON.stringify(decision ?? {}),
				"--preflight-json",
				JSON.stringify(preflight),
			];
			const persisted = await run(bin("tp-task"), mainArgs, JSON.stringify(params), 30_000);
			if (persisted.code !== 0) throw new Error(`R5 contract persistence failed: ${(persisted.stderr || persisted.stdout).trim()}`);
			const saved = JSON.parse(persisted.stdout);
			const mainContract = saved.main_contract as MainContract;
			hand.mainContract = mainContract;
			hand.committed = true;
			hand.handoff = saved.handoff;
			if (saved.handoff) writeJson(join(saved.handoff, "scaffold.json"), {
				...readJson(join(saved.handoff, "scaffold.json")), original_request: hand.original,
				effective_request: hand.effective, decision: hand.decision ?? {}, blocked_by: hand.blockedBy ?? [],
			});
			if (hand.mode && pendingStopRecord) {
				const dir = await taskDir(pendingStopRecord.id);
				writeJson(join(dir, "main-contract.json"), mainContract);
				const meta = readJson(join(dir, "meta.json"));
				meta.main_contract = mainContract;
				writeJson(join(dir, "meta.json"), meta);
			}
			let automation: AutomationPolicy;
			let policyDecision: any;
			if (!decision) {
				policyDecision = { schema: "tmux-pi-r5-policy-decision:v1", created: nowIso(), action: "recommend_manual", mode: "router_unavailable", notice: "Router/Laya unavailable; no automatic dispatch authority." };
				automation = { action: "recommend_manual", mode: "recommendation_manual", notice: policyDecision.notice };
			} else {
				const enriched = { preflight: saved.enriched_preflight, note: saved.note };
				const policy = await run(
					bin("tp-task"),
					[
						"automation",
						"--json",
						"--decision-json",
						JSON.stringify(decision),
						"--preflight-json",
						JSON.stringify(saved.enriched_preflight),
						"--main-contract-json",
						JSON.stringify(mainContract),
					],
					hand.effective,
					30_000,
				);
				if (policy.code !== 0) {
					policyDecision = { schema: "tmux-pi-r5-policy-decision:v1", created: nowIso(), action: "recommend_manual", mode: "policy_unavailable", notice: `Policy unavailable; no dispatch. ${(policy.stderr || policy.stdout).trim()}` };
					automation = { action: "recommend_manual", mode: "recommendation_manual", notice: policyDecision.notice };
				} else {
					automation = JSON.parse(policy.stdout);
					policyDecision = automation;
				}
				if (hand.goal && mainContract.action === "delegate") {
					const scopedDecision = { ...decision, task: mainContract.task_summary, placement: "subagent" as const, review: "required" as const, overrides: [] };
					const goalGate = await run(bin("tp-goal"), ["--project", hand.goal.workdir, "gate-attempt", hand.goal.id,
						"--ticket", hand.goal.ticket, "--spec-sha256", hand.goal.spec_sha256,
						"--main-json", JSON.stringify(mainContract), "--decision-json", JSON.stringify(scopedDecision),
						"--preflight-json", JSON.stringify(saved.enriched_preflight), "--policy-json", JSON.stringify(automation)]);
					if (goalGate.code === 0) {
						const authority = JSON.parse(goalGate.stdout);
						decision = scopedDecision;
						hand.decision = decision;
						automation = { ...automation, action: "auto_dispatch", mode: "approved_goal", confirmation_required: false,
							review: { ...automation.review, required: true, risk: authority.risk, reasons: ["Goal edit/review gate", ...(automation.review?.reasons ?? [])] },
							notice: authority.notice };
						policyDecision = { ...automation, goal_authority: { id: hand.goal.id, ticket: hand.goal.ticket, spec_sha256: hand.goal.spec_sha256,
							workdir: hand.goal.workdir, allowed_paths: authority.allowed_paths } };
						if (enriched.preflight?.allowed) enriched.preflight.allowed.paths = authority.allowed_paths;
					} else {
						automation = { ...automation, action: "recommend_manual", mode: "recommendation_manual", notice: `Approved goal attempt blocked: ${(goalGate.stderr || goalGate.stdout).trim()}` };
						policyDecision = automation;
					}
				}
				if (saved.handoff) writeJson(join(saved.handoff, "policy-decision.json"), policyDecision);
				if (mainContract.action === "delegate" && automation.action === "auto_dispatch") {
					if (automation.confirmation_required) {
						if (!ctx?.hasUI) {
							automation = { ...automation, action: "recommend_manual", confirmation_outcome: "blocked_no_ui", notice: `${automation.notice} No UI was available for the separate human confirmation; no dispatch.` } as AutomationPolicy;
						} else {
							const choice = await ctx.ui.select(
								`tmux-pi R5: dispatch high-risk/destructive task after persisted main contract?\n\n${hand.effective}`,
								["Confirm dispatch", "Do not dispatch"],
							);
							automation = choice === "Confirm dispatch" ? ({ ...automation, confirmation_outcome: "confirmed" } as AutomationPolicy) : ({ ...automation, action: "recommend_manual", confirmation_outcome: "declined", notice: `${automation.notice} Human confirmation declined; no dispatch.` } as AutomationPolicy);
						}
					}
					policyDecision = automation;
					if (saved.handoff) writeJson(join(saved.handoff, "policy-decision.json"), policyDecision);
					if (automation.action === "auto_dispatch") {
						const foreground = decision.placement === "main" && decision.worker === "claude";
						if (decision.placement === "main" && decision.worker === "pi") {
							const ownReview = await ownReviewFor(hand, automation, saved.enriched_preflight, mainContract);
							hand.reviewRequired = !!hand.reviewRequired || ownReview;
							hand.allowMainEdits = !mainContract.no_edits && !hand.reviewRequired && automation.review?.risk === "low" && automation.mode === "explicit_human_request" && !automation.confirmation_required;
							if (ownReview && !mainContract.no_edits) {
								if (saved.handoff) writeJson(join(saved.handoff, "review-required.json"), { required: true, status: "needs_human", reasons: automation.review?.reasons ?? [], handshake_id: hand.id });
							}
							return {
								content: [{ type: "text", text: "R5 contract persisted. Main Pi route stays in main without a worker. Required review, if any, remains a human blocker; do not accept or edit until resolved." }],
								details: { handshake_id: hand.id, action: mainContract.action, policy: automation, handoff: saved.handoff },
							};
						}
						pendingDispatch = { handshake: hand, decision, foreground, preflight: enriched, automation, mainContract, policyDecision };
						lastCtx?.ui.setStatus("auto", `AUTO R5 dispatch queued · ${decision.model}/${decision.thinking}`);
						return {
							content: [{ type: "text", text: `R5 contract persisted and deterministic policy passed. Ending this turn so tmux-pi can dispatch ${foreground ? "the foreground worker" : "the worker"}; do not spawn manually.` }],
							details: { handshake_id: hand.id, action: mainContract.action, dispatch_queued: true, policy: automation, handoff: saved.handoff },
							terminate: true,
						};
					}
				}
			}
			if (saved.handoff) writeJson(join(saved.handoff, "policy-decision.json"), policyDecision);
			if (saved.record?.dir) {
				writeJson(join(saved.record.dir, "policy-decision.json"), policyDecision);
				const metaFile = join(saved.record.dir, "meta.json");
				const meta = readJson(metaFile);
				meta.policy_decision = policyDecision;
				writeJson(metaFile, meta);
			}
			if (mainContract.action === "ask_alignment") {
				const material = mainContract.material_ambiguity ?? { question: "Please clarify the material ambiguity.", why_material: "The main contract marked this as material." };
				pendingAlignment = { handshake_id: hand.id, request_sha256: hand.requestHash, question: material.question, why_material: material.why_material, original: hand.original, effective: hand.effective, handoff: saved.handoff, asked_at: nowIso() };
				if (saved.handoff) writeJson(join(saved.handoff, "alignment.json"), { schema: "tmux-pi-r5-alignment:v1", ...pendingAlignment, answers: [] });
				lastCtx?.ui.setStatus("auto", "AUTO needs alignment · R5");
				return { content: [{ type: "text", text: `R5 alignment recorded. Ask the user exactly this material alignment question; no worker will start before an answer and a final contract: ${material.question}` }], details: { handshake_id: hand.id, action: mainContract.action, alignment: pendingAlignment, policy: policyDecision, handoff: saved.handoff } };
			}
			if (mainContract.action === "stop") {
				if (saved.record?.id && !pendingStopRecord) pendingStopRecord = { id: saved.record.id, mode: mainContract.mode };
				lastCtx?.ui.setStatus("auto", `AUTO ${mainContract.mode} stop contract · R5`);
				return { content: [{ type: "text", text: `R5 ${mainContract.mode} stop contract persisted${pendingStopRecord ? ` in ${pendingStopRecord.id}` : ""}. Now produce only the stop-mode artifact in chat. Do not edit files or spawn a worker.` }], details: { handshake_id: hand.id, action: mainContract.action, record: saved.record, policy: policyDecision, handoff: saved.handoff } };
			}
			if (mainContract.action === "direct" && hand.goal) {
				hand.allowMainEdits = false;
				return { content: [{ type: "text", text: "Goal direct-main contract recorded. Main edits remain blocked: coding attempts require a fresh delegated contract, approved goal scope and independent review. Read-only investigation may continue." }],
					details: { handshake_id: hand.id, action: mainContract.action, goal: hand.goal.id, handoff: saved.handoff } };
			}
			if (mainContract.action === "direct") {
				const ownReview = await ownReviewFor(hand, automation, saved.enriched_preflight, mainContract);
				const reviewRequired = !!hand.reviewRequired || ownReview;
				hand.reviewRequired = reviewRequired;
				if (ownReview && !mainContract.no_edits && saved.handoff) writeJson(join(saved.handoff, "review-required.json"), { required: true, status: "needs_human", reasons: automation.review?.reasons ?? [], handshake_id: hand.id });
				if (ownReview && !mainContract.no_edits && saved.record?.dir) {
					writeJson(join(saved.record.dir, "review-required.json"), { required: true, status: "needs_human", handshake_id: hand.id });
				}
				hand.allowMainEdits = !mainContract.no_edits && !!decision && decision.placement === "main" && !reviewRequired && automation.review?.risk === "low" && !automation.confirmation_required && policyDecision.mode !== "policy_unavailable";
				return { content: [{ type: "text", text: `R5 direct contract persisted and policy checked. No worker will start. ${reviewRequired ? "Required review is retained as a human blocker in the handoff; do not accept or edit this work until an independent report-only review or explicit human decision." : "Answer directly within the recorded frame."}` }], details: { handshake_id: hand.id, action: mainContract.action, policy: automation, handoff: saved.handoff } };
			}
			lastCtx?.ui.setStatus("auto", "AUTO manual/recommendation · R5");
			return { content: [{ type: "text", text: `R5 contract persisted but deterministic policy did not authorize dispatch. No worker will start. ${automation.notice}` }], details: { handshake_id: hand.id, action: mainContract.action, policy: automation, handoff: saved.handoff } };
		},
	});

	pi.registerCommand("auto", {
		description: "Turn Laya-directed routing on or off for this session: /auto on | /auto off",
		handler: async (args, ctx) => {
			const value = String(args ?? "").trim().toLowerCase();
			if (value !== "on" && value !== "off") {
				ctx.ui.notify(`AUTO is ${enabled ? "on" : "off"}; use /auto on or /auto off`, "info");
				return;
			}
			enabled = value === "on";
			if (!enabled) {
				if (activeHandshake) activeHandshake.allowMainEdits = false;
			}
			ctx.ui.setStatus("auto", enabled ? "AUTO on" : "AUTO off (manual)");
		},
	});

	const maybeAnswerPendingAlignment = async (ctx: any, text: string): Promise<{ text: string; handled?: boolean }> => {
		if (!pendingAlignment) return { text };
		const align = pendingAlignment;
		const ANSWER = "Answer pending alignment";
		const NEW = "Start a new request";
		let choice: string | undefined;
		if (ctx.hasUI) {
			choice = await ctx.ui.select(
				`A tmux-pi R5 alignment question is pending. Does this input answer it or start a new request?\n\n${align.question}`,
				[ANSWER, NEW],
			);
		}
		if (choice === ANSWER) {
			const answer = text.trim();
			if (!answer) return { text, handled: true };
			if (align.handoff) {
				const file = join(align.handoff, "alignment.json");
				const data = readJson(file);
				const answers = Array.isArray(data.answers) ? data.answers : [];
				answers.push({ at: nowIso(), answer });
				writeJson(file, { schema: "tmux-pi-r5-alignment:v1", ...align, answers });
			}
			pendingAlignment = undefined;
			const combined = `${align.effective}\n\nR5 material alignment question:\n${align.question}\n\nUser alignment answer:\n${answer}`;
			return { text: combined };
		}
		if (choice === NEW || !ctx.hasUI) {
			if (align.handoff) {
				const file = join(align.handoff, "alignment.json");
				const data = readJson(file);
				writeJson(file, { schema: "tmux-pi-r5-alignment:v1", ...align, ...(data ?? {}), superseded_by_new_request_at: nowIso() });
			}
			pendingAlignment = undefined;
			return { text };
		}
		ctx.ui.setEditorText(text);
		return { text, handled: true };
	};

	const startHandshake = async (ctx: any, original: string, effective: string, decision?: Decision, mode?: string, researchSpec?: Handshake["research"], goal?: Handshake["goal"]): Promise<{ action: "transform"; text: string } | { action: "continue" } | { action: "handled" }> => {
		let blockers: string[];
		try { blockers = await unresolvedReviews(); }
		catch (error) {
			activeHandshake = undefined;
			ctx.ui.notify(`Cannot check required-review blockers: ${(error as Error).message}`, "warning");
			return { action: "handled" };
		}
		const ownReviewRequested = decision?.review === "required";
		if (blockers.length && decision) decision = { ...decision, review: "required" };
		let preflight: any;
		try {
			preflight = await selectPreflight(effective, decision, mode);
		} catch (error) {
			preflight = undefined;
			ctx.ui.notify(`R5 scaffold unavailable; no automatic dispatch will be allowed (${(error as Error).message})`, "warning");
		}
		const pf = preflight?.preflight as Preflight | undefined;
		const hand: Handshake = {
			id: handshakeId(),
			requestHash: sha256(original),
			original,
			effective,
			decision,
			preflight,
			mode,
			research: researchSpec,
			created: Date.now(),
			requiresContract: !!goal || blockers.length > 0 || needsPersistedContract(decision, pf, mode),
			reviewRequired: blockers.length > 0,
			ownReviewRequested,
			blockedBy: blockers,
			goal,
		};
		if (hand.requiresContract) {
			try {
				await persistScaffold(hand);
			} catch (error) {
				ctx.ui.notify(`Could not persist R5 scaffold; no dispatch will be allowed (${(error as Error).message})`, "warning");
			}
		}
		if (mode) {
			try {
				// Force a durable scaffold even for a brief +investigate request.
				const stopDecision = decision ?? { scope: "sustained", review: "none" } as Decision;
				const created = await selectPreflight(effective, stopDecision, mode, true);
				if (!created.record?.id) throw new Error("stop record was not created");
				pendingStopRecord = { id: created.record.id, mode };
			} catch (error) {
				ctx.ui.notify(`Stop record unavailable; no durable output can be promised: ${(error as Error).message}`, "warning");
				return { action: "handled" };
			}
		} else pendingStopRecord = undefined;
		activeHandshake = hand;
		const routed = decision ? `${decision.model}/${decision.thinking}` : "router unavailable";
		ctx.ui.setStatus("auto", hand.requiresContract ? `AUTO R5 frame required · ${routed}` : `AUTO R5 direct · ${routed}`);
		return effective !== original ? { action: "transform", text: effective } : { action: "continue" };
	};

	pi.on("tool_call", async (event) => {
		const hand = activeHandshake;
		// /auto off disables routing, not the ban on model-initiated worker creation.
		if (spawnToolNames.has(event.toolName)) return { block: true, reason: "Use an explicit tmux-pi handoff, not a model-initiated subagent tool." };
		if (event.toolName === "bash" && spawnCommandPattern.test(String((event.input as any)?.command ?? ""))) return { block: true, reason: "Model-initiated worker commands are blocked (best effort; bash is not a sandbox)." };
		// Known read tools may inspect tasks; collect_result invokes the canonical
		// collect CLI, which independently rejects unresolved required review.
		// Unknown tools (including powershell) may mutate state.
		if ((!hand || !enabled || !hand.committed || !hand.allowMainEdits) &&
			!["read", "bash", "grep", "find", "ls", "list_subagents", "collect_result", "tmux_pi_commit_contract", "tmux_pi_find_skill"].includes(event.toolName)) {
			return { block: true, reason: "Main tool execution needs an authorized direct edit contract." };
		}
		if (!hand && event.toolName === "bash" && !readOnlyBash(String((event.input as any)?.command ?? ""))) return { block: true, reason: "No active R5 handshake: shell execution is not authorized." };
		if (!hand) return undefined;
		if (event.toolName === "bash") {
			const command = String((event.input as any)?.command ?? "");
			if ((!enabled || !hand.committed || !hand.allowMainEdits) && !readOnlyBash(command)) return { block: true, reason: "tmux-pi R5 permits only simple read-only bash commands before a low-risk direct edit contract; arbitrary shell execution is not a sandbox." };
		}
		if ((event.toolName === "edit" || event.toolName === "write") && (!enabled || !hand.committed || !hand.allowMainEdits)) {
			return { block: true, reason: "Main edits are not authorized: a committed low-risk main placement without unresolved review is required." };
		}
		return undefined;
	});

	pi.on("input", async (event, ctx) => {
		lastCtx = ctx;
		const text = event.text.trim();
		// Only a verified extension-sourced admission can skip follow-up routing.
		// A user/model-inserted marker is ordinary text, never goal authority.
		const goalHeader = /^TP_GOAL_CONTINUATION ([A-Za-z0-9_][A-Za-z0-9_.-]{0,63}) ([a-f0-9]{32}) ([a-f0-9]{64})\n([\s\S]+)$/.exec(text);
		if (goalHeader && event.source === "extension") {
			if (!enabled || event.streamingBehavior) {
				ctx.ui.notify("Goal continuation needs idle AUTO/R5; ticket is not replayed", "warning");
				return { action: "handled" };
			}
			try {
				const [, id, ticket, specSha, body] = goalHeader;
				const authority = JSON.parse(await must(bin("tp-goal"), ["--project", ctx.cwd, "gate-input", id, "--ticket", ticket, "--spec-sha256", specSha]));
				const routed = JSON.parse(await must(bin("tp-route"), ["--dispatch"], body)) as Decision;
				if (!routed || !["pi", "claude"].includes(routed.worker) || !routed.model) throw new Error("Laya model/effort unavailable");
				// Placement/review are deterministic for an approved goal, never Laya grants.
				const decision: Decision = { ...routed, task: body, placement: "subagent", review: "required", overrides: [] };
				return startHandshake(ctx, text, body, decision, undefined, undefined, authority);
			} catch (error) {
				ctx.ui.notify(`Goal admission refused; no routing or follow-up: ${(error as Error).message}`, "warning");
				return { action: "handled" };
			}
		}
		// A queued user input is not authorized by a previous request's nonce.
		if (event.streamingBehavior) {
			activeHandshake = undefined;
			pendingDispatch = undefined;
			ctx.ui.notify("Queued input needs its own R5 handshake; resend after this turn.", "warning");
			ctx.ui.setEditorText(text);
			return { action: "handled" };
		}
		if (!text) {
			return { action: "continue" };
		}
		if (!enabled && !pendingAlignment) {
			activeHandshake = undefined;
			// Even with routing off, slash/extension prompts reaching input need
			// an identity of their own; the edit gate remains closed.
			return text.startsWith("/") || event.source === "extension"
				? startHandshake(ctx, text, text) : { action: "continue" };
		}

		const sendTo = async (target: Delegated, message: string) => {
			// Follow-ups cannot discard a review blocker by changing the request wording.
			if ((await unresolvedReviews()).length) target.review = "required";
			const dir = await taskDir(target.id);
			if (target.review === "required") {
				rearmDelegatedReview(dir, target.id);
				const meta = readJson(join(dir, "meta.json"));
				meta.decision = { ...meta.decision, review: "required" };
				writeJson(join(dir, "meta.json"), meta);
			}
			target.since = Date.now();
			target.rounds = 0;
			await typeInto(target.id, message);
			// Reviewers read these with the original task (bin/tp-review).
			const followups = join(await taskDir(target.id), "followups.md");
			appendFileSafe(rootOf(followups), followups, `- ${message.replace(/\r?\n/g, " ")}\n`);
			post(`Sent to ${target.id}: ${message}`, { kind: "follow-up", id: target.id });
			ctx.ui.setStatus("auto", `AUTO follow-up · ${target.id}`);
			follow(target);
		};

		const alignmentInput = await maybeAnswerPendingAlignment(ctx, text);
		if (alignmentInput.handled) return { action: "handled" };
		const requestTextForRouting = alignmentInput.text;
		if (!enabled && requestTextForRouting === text) return { action: "continue" };

		const forced = /^\+followup(\s+|$)/i.exec(requestTextForRouting);
		if (forced) {
			const { task, text: message } = await forcedTarget(ctx, requestTextForRouting.slice(forced[0].length));
			if (!task || !message) {
				ctx.ui.notify(task ? "+followup needs a message" : "No running task to follow up; name one with +followup <id>", "warning");
				ctx.ui.setEditorText(text);
				return { action: "handled" };
			}
			try {
				await sendTo(task, message);
			} catch (error) {
				ctx.ui.notify(`Could not send to ${task.id}: ${(error as Error).message}`, "warning");
				ctx.ui.setEditorText(text);
			}
			return { action: "handled" };
		}

		const stopMarker = /^\+(plan|prototype|investigate)(\s+|$)/i.exec(requestTextForRouting);
		if (stopMarker) {
			const mode = stopMarker[1].toLowerCase();
			const request = requestTextForRouting.slice(stopMarker[0].length).trim();
			if (!request) {
				ctx.ui.notify(`+${mode} needs a request`, "warning");
				ctx.ui.setEditorText(text);
				return { action: "handled" };
			}
			return startHandshake(ctx, requestTextForRouting, request, undefined, mode);
		}

		const researchMarker = /^\+research(\s+|$)/i.exec(requestTextForRouting);
		if (researchMarker) {
			let question = requestTextForRouting.slice(researchMarker[0].length).trim();
			let reviewed = false;
			const after: string[] = [];
			for (let m; (m = /^\+(review|after\s+([\w.,-]+))(\s+|$)/i.exec(question)); question = question.slice(m[0].length).trim()) {
				if (m[2]) after.push(...m[2].split(",").filter(Boolean));
				else reviewed = true;
			}
			if (!question) {
				ctx.ui.notify("+research needs a question", "warning");
				ctx.ui.setEditorText(text);
				return { action: "handled" };
			}
			try {
				const config = JSON.parse(await must(bin("tp-contract"), ["research"]));
				const decision: Decision = {
					worker: config.worker,
					model: config.model,
					thinking: config.thinking,
					route: "research",
					intent: "research",
					review: reviewed ? "required" : "none",
					scope: "sustained",
					placement: "subagent",
					overrides: ["placement", "research"],
					task: question,
				};
				return startHandshake(ctx, requestTextForRouting, question, decision, undefined, { review: reviewed, after, worker: config.worker, model: config.model, thinking: config.thinking });
			} catch (error) {
				ctx.ui.notify(`Could not prepare research handshake: ${(error as Error).message}`, "warning");
				ctx.ui.setEditorText(text);
				return { action: "handled" };
			}
		}

		const target = await followUpTarget(ctx, requestTextForRouting);
		if (target) {
			try {
				await sendTo(target, requestTextForRouting);
				return { action: "handled" };
			} catch (error) {
				ctx.ui.notify(`Could not send to ${target.id}: ${(error as Error).message}`, "warning");
			}
		}

		let decision: Decision | undefined;
		try {
			const r = await must(bin("tp-route"), ["--dispatch"], requestTextForRouting);
			decision = JSON.parse(r);
		} catch {
			ctx.ui.setStatus("auto", "AUTO R5 frame · Laya unavailable");
			return startHandshake(ctx, requestTextForRouting, requestTextForRouting);
		}
		return startHandshake(ctx, requestTextForRouting, decision?.task ?? requestTextForRouting, decision);
	});

	pi.on("agent_end", async (event, ctx) => {
		lastAgentMessages = event.messages;
		const dispatch = pendingDispatch;
		pendingDispatch = undefined;
		if (dispatch) {
			try {
				await performDispatch(ctx ?? lastCtx, dispatch);
			} catch (error) {
				post(`R5 dispatch failed after contract persistence; no retry/escalation started. ${(error as Error).message}`, { kind: "r5-dispatch-failed", handshake_id: dispatch.handshake.id, failed: true });
			}
		}
	});

	pi.on("agent_settled", async () => {
		if (pendingStopRecord) {
			const record = pendingStopRecord;
			pendingStopRecord = undefined;
			const output = [...lastAgentMessages].reverse().map((entry: any) => entry?.message ?? entry)
				.filter((message: any) => message?.role === "assistant")
				.map((message: any) => messageText(message.content).trim()).find(Boolean) ?? "";
			if (output) {
				const r = await run(bin("tp-task"), ["capture-output", record.id, "--state", "completed"], output);
				if (r.code === 0) {
					post(`${record.mode} output captured in ${record.id}. Resume with \`tp-task resume ${record.id}\`.`, { kind: "preflight-output", id: record.id, mode: record.mode });
				} else {
					post(`${record.mode} output could not be captured durably for ${record.id}: ${(r.stderr || r.stdout).trim()}`, { kind: "preflight-output", id: record.id, mode: record.mode, failed: true });
				}
			} else post(`${record.mode} produced no final assistant output; ${record.id} remains open for recovery.`, { kind: "preflight-output", id: record.id, failed: true });
		}
		if (activeHandshake && /^\+delegate(?:\s|$)/i.test(activeHandshake.original) && !activeHandshake.committed) {
			post(`+delegate request ${activeHandshake.id} was not delegated; no contract committed. No worker started or main edit authority granted.`, { kind: "delegate-contract-omitted", handshake_id: activeHandshake.id });
		}
		if (activeHandshake?.reviewRequired) {
			const hand = activeHandshake;
			const refs = [...(hand.blockedBy ?? [])];
			if (hand.handoff && existsSync(join(hand.handoff, "review-required.json"))) refs.push(hand.id);
			if (refs.length) post(`Main task ${hand.id} still has required review. Output is not accepted as reviewed. Resolve: ${refs.join(", ")}.`, { kind: "review-needs-human", handshake_id: hand.id, blocked_by: refs });
			else if (hand.unverifiedExpansion) post(`Expanded prompt for ${hand.id} was not routed or verified. No edit or dispatch authority was granted; resend the expanded request explicitly if work is needed.`, { kind: "unverified-expansion", handshake_id: hand.id });
		}
		activeHandshake = undefined;
		lastAgentMessages = [];
	});

	pi.on("before_agent_start", async (event) => {
		if (event.messageType !== "user") return undefined;
		const hand = activeHandshake;
		if (hand && event.prompt !== hand.effective) {
			// Pi expands skills/templates after input; invalidate the old route/hash.
			hand.effective = event.prompt;
			hand.requestHash = sha256(event.prompt);
			hand.decision = undefined;
			hand.preflight = undefined;
			hand.ownReviewRequested = false;
			hand.unverifiedExpansion = true;
			hand.reviewRequired = true;
			hand.requiresContract = true;
			hand.allowMainEdits = false;
			pendingDispatch = undefined;
			try { await persistScaffold(hand); }
			catch { hand.handoff = undefined; }
		}
		if (activeHandshake) {
			return { message: { customType: "tmux-pi-r5-handshake", content: hiddenHandshakeInstruction(activeHandshake), display: false } };
		}
		return undefined;
	});
}
