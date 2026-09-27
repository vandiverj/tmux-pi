/**
 * dispatcher — Laya-directed routing for the main agent (plan M8, phases 1 and 2).
 *
 * Active only in the main agent: inside tmux and not in a tp-spawn worker. Every
 * user message typed while main is idle (not a /command or !shell line) is routed:
 *
 *   follow-up          when delegated tasks are still running, `tp-route --follow-up`
 *                      decides whether the message belongs to one of them; if so it is
 *                      typed into that worker. When the judge is unsure, the user picks.
 *   placement main     pi route: switch the main model and thinking in place. Claude
 *                      route: pi cannot switch to Claude, so a foreground Claude worker
 *                      answers it in the viewer slot and its reply is posted into main.
 *   placement subagent spawn the routed worker directly with the packet core (the
 *                      verbatim request, the conversation since the last delegation,
 *                      ask-if-unclear and report-back instructions); main takes no turn.
 *                      Models in local packet_files also get up to 3 files a Claude
 *                      model picked (`tp-route --pick-files`).
 *   review required    when the author settles, a cross-provider reviewer starts; one
 *                      REQUEST_CHANGES round goes back to the author automatically.
 *   Laya unavailable   change nothing; status "AUTO off: Laya unavailable".
 *
 * Leading +here, +delegate and +review markers override placement or review; +followup
 * [id] sends the message to a running task without asking the judge. Results and worker
 * questions are posted into main's conversation as tmux-pi-auto messages, so main sees
 * them on its next turn. `/auto on|off` turns routing on or off.
 *
 * `+research [+review] <question>` starts a research worker (config/research.json) under a
 * contract: a report file, checkpoints and a budget, enforced by `tp-contract enforce`
 * (reminders, then a pause) for every contract task in the workspace. Research rows go
 * into the run ledger (`tp-ledger`) when the worker settles. Past the configured number
 * of research or active workers, the user is asked before another starts.
 *
 * Independently of routing, a Claude worker stopped at a choice prompt (folder trust, a
 * tool permission) is shown to the user in main as a dialog; the pick is keyed into it.
 */

import { execFile } from "node:child_process";
import { appendFileSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

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
const MAX_FOLLOW_UP_TASKS = 3;
const REPLY_LIMIT = 4000;

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

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	const root = dirname(agentDir);
	const bin = (name: string) => join(root, "bin", name);
	let enabled = true;
	let note: string | undefined;
	let lastCtx: any;
	const delegated = new Map<string, Delegated>();

	const stateDir = () => must("tmux", ["display-message", "-p", "#{@tp_state_dir}"]);
	const taskDir = async (id: string) => join(await stateDir(), "tasks", id);

	const paneAlive = async (dir: string) => {
		const pane = readJson(join(dir, "meta.json")).pane;
		const panes = await run("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"]);
		return panes.stdout.split("\n").some((l) => l === `${pane} 0`);
	};

	const home = () => {
		try {
			const settings = JSON.parse(readFileSync(join(agentDir, "settings.json"), "utf8"));
			return { provider: settings.defaultProvider, id: settings.defaultModel, thinking: settings.defaultThinkingLevel };
		} catch {
			return undefined;
		}
	};

	// Switch the main model, falling back to the home model when the routed one cannot hold the conversation.
	const switchTo = async (ctx: any, spec: string, thinking: string): Promise<string> => {
		const slash = spec.indexOf("/");
		const model = ctx.modelRegistry.find(spec.slice(0, slash), spec.slice(slash + 1));
		const used = ctx.getContextUsage()?.tokens ?? 0;
		if (model && used < model.contextWindow * 0.8 && (await pi.setModel(model))) {
			pi.setThinkingLevel(thinking as any);
			return spec;
		}
		const fallback = home();
		const homeModel = fallback && ctx.modelRegistry.find(fallback.provider, fallback.id);
		if (homeModel && (await pi.setModel(homeModel))) pi.setThinkingLevel(fallback!.thinking);
		return fallback ? `${fallback.provider}/${fallback.id} (${spec} unavailable or too small)` : "current model";
	};

	// Post into main's conversation without starting a turn (pi holds it until a running turn ends);
	// main sees it on its next turn.
	const post = (content: string, details: Record<string, unknown>) =>
		pi.sendMessage({ customType: "tmux-pi-auto", content, display: true, details }, { triggerTurn: false });

	const showRunning = () => {
		if (!lastCtx) return;
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
	const settle = async (id: string, since: () => number): Promise<{ id: string; ok: boolean; escalatedTo?: string }> => {
		const started = Date.now();
		let warned = false;
		for (;;) {
			const dir = await taskDir(id);
			const escalatedTo = readText(join(dir, "escalated"));
			if (escalatedTo) return { id, ok: false, escalatedTo };
			const state = readJson(join(dir, "state.json"));
			if (!(await paneAlive(dir))) return { id, ok: readText(join(dir, "exit")) === "0" };
			if (Date.parse(state.updated ?? "") >= since() - 1000 && ["waiting", "error"].includes(state.state)) {
				return { id, ok: state.state === "waiting" };
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
	const summary = (reply: string, path: string) =>
		reply ? `${clip(reply, REPLY_LIMIT)}${reply.length > REPLY_LIMIT ? `\n(full reply: ${path})` : ""}` : "(no reply)";

	// Follow a delegated task through escalation, questions and review until it needs the user.
	const watch = async (task: Delegated) => {
		for (;;) {
			const done = await settle(task.id, () => task.since);
			if (done.escalatedTo) {
				post(`${task.id} escalated to ${done.escalatedTo} after repeated tool errors.`, { kind: "escalated", id: task.id, to: done.escalatedTo });
				delegated.delete(task.id);
				task.id = done.escalatedTo;
				delegated.set(task.id, task);
				showRunning();
				continue;
			}
			const dir = await taskDir(task.id);
			const raw = await replyOf(task.id);
			const reply = raw + (task.report && !raw.includes(task.report) ? `\n\nReport: ${task.report}` : "");
			if (!done.ok) {
				post(`${task.id} stopped with an error.\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "failed", id: task.id });
				break;
			}
			// A worker that asked a question waits for the user; its answer is routed back as a follow-up.
			if (raw.trimEnd().endsWith("?")) {
				post(`${task.id} asks:\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "question", id: task.id });
				break;
			}
			if (task.review !== "required" || task.rounds >= MAX_REVIEW_ROUNDS) {
				const note = task.rounds >= MAX_REVIEW_ROUNDS ? ` (after ${task.rounds} review rounds; last review: ${task.reviewer})` : "";
				const verb = task.foreground ? "answered" : "finished";
				post(`${task.id} ${verb}${note}.\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "result", id: task.id });
				break;
			}
			const started = Date.now();
			const r = await run(bin("tp-review"), ["--force", task.id]);
			if (r.code !== 0) {
				post(`${task.id} finished, but its required review could not start: ${(r.stderr || r.stdout).trim()}\n\n${summary(reply, join(dir, "result.md"))}`, { kind: "result", id: task.id });
				break;
			}
			task.reviewer = r.stdout.trim();
			task.rounds += 1;
			showRunning();
			lastCtx?.ui.setStatus("auto", `AUTO review · ${task.reviewer} checking ${task.id}`);
			const reviewed = await settle(task.reviewer, () => started);
			const reviewDir = await taskDir(task.reviewer);
			const verdict = await replyOf(task.reviewer);
			await run(bin("tp-kill"), [task.reviewer]);
			const reviewer = task.reviewer;
			task.reviewer = undefined;
			showRunning();
			const approved = reviewed.ok && /\bAPPROVED\b/.test(verdict) && !/\bREQUEST_CHANGES\b/.test(verdict);
			if (approved || !reviewed.ok) {
				const head = approved ? `${task.id} finished and ${reviewer} approved it.` : `${task.id} finished; review ${reviewer} failed.`;
				post(`${head}\n\n${summary(reply, join(dir, "result.md"))}\n\nReview:\n${summary(verdict, join(reviewDir, "result.md"))}`, { kind: "result", id: task.id, review: reviewer });
				break;
			}
			if (task.rounds >= MAX_REVIEW_ROUNDS) {
				post(`${task.id}: ${reviewer} still requests changes after ${task.rounds} rounds.\n\n${summary(verdict, join(reviewDir, "result.md"))}`, { kind: "result", id: task.id, review: reviewer });
				break;
			}
			post(`${reviewer} requested changes to ${task.id}; sent back to the author.`, { kind: "review", id: task.id, review: reviewer });
			task.since = Date.now();
			await typeInto(task.id, `A reviewer on another provider requested changes. Their findings are in ${join(reviewDir, "result.md")}. Address each finding, or explain why it does not apply, then report again.`);
		}
		lastCtx?.ui.setStatus("auto", "AUTO on");
	};

	const follow = (task: Delegated) => {
		if (task.watching) return;
		task.watching = true;
		void watch(task)
			.catch((error) => post(`Stopped following ${task.id}: ${(error as Error).message}`, { kind: "failed", id: task.id }))
			.finally(async () => {
				task.watching = false;
				if (task.report) await run(bin("tp-ledger"), ["record", task.id]);
			});
	};

	const delegate = async (ctx: any, decision: Decision, foreground = false): Promise<string> => {
		const tmp = mkdtempSync(join(tmpdir(), "tp-dispatch-"));
		try {
			const args = ["--worker", decision.worker, "--model", decision.model, "--thinking", decision.thinking];
			args.push(...(foreground ? ["--name", "fg", "--view"] : ["--name", "auto"]));
			const conversation = conversationSinceDelegation(ctx);
			if (conversation) {
				writeFileSync(join(tmp, "conversation.md"), conversation);
				args.push("--conversation", join(tmp, "conversation.md"));
			}
			args.push("--note", ASK_IF_UNCLEAR);
			if (!foreground) args.push("--note", REPORT_BACK);
			if (decision.review === "required") args.push("--note", "A reviewer on another provider will check this work when you report.");
			// Models that explore poorly (local packet_files) get files a Claude model picked for the task.
			// If picking fails the worker starts with the core packet and finds files itself.
			if (!foreground) {
				const picked = await run(bin("tp-route"), ["--pick-files", dirname(await stateDir()), "--model", decision.model], decision.task, 150_000);
				if (picked.code === 0) for (const spec of picked.stdout.split("\n").filter(Boolean)) args.push("--context", spec);
			}
			return (await must(bin("tp-spawn"), args, decision.task, 60_000)).split("\n")[0];
		} finally {
			rmSync(tmp, { recursive: true, force: true });
		}
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
			await relay(id, meta.pane, prompt);
			return;
		}
	};

	// Contract checkpoints (M9): tp-contract types reminders into the worker and pauses it;
	// budget stops and pauses are posted into main, where the user decides what happens next.
	const enforceContracts = async () => {
		const r = await run(bin("tp-contract"), ["enforce"]);
		for (const line of r.stdout.split("\n").filter(Boolean)) {
			const a = JSON.parse(line);
			const where = `Its report: ${a.artifact} (${a.status ? `Status: ${a.status}` : "no status line"}).`;
			if (a.action === "pause") {
				post(
					`Paused ${a.id}: ${a.reason}. ${where} Continue, narrow the question or escalate with +followup ${a.id} <message>, or kill it from the status pane.`,
					{ kind: "paused", id: a.id, reason: a.reason },
				);
				await run(bin("tp-ledger"), ["record", a.id]);
			} else if (a.action === "stop") {
				post(`${a.id}: ${a.reason}; it was asked to save its report and ask how to go on. ${where}`, { kind: "budget", id: a.id });
			} else {
				lastCtx?.ui.notify(`${a.id}: ${a.reason}; reminded it`, "info");
			}
		}
	};

	// +research: one bounded question to a research worker under a contract.
	const research = async (ctx: any, question: string, review: boolean): Promise<boolean> => {
		const config = JSON.parse(await must(bin("tp-contract"), ["research"]));
		const tasks = join(await stateDir(), "tasks");
		const live = new Set((await run("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"])).stdout.split("\n"));
		let researching = 0;
		let active = 0;
		for (const id of readdirSync(tasks, { withFileTypes: true }).filter((e) => e.isDirectory()).map((e) => e.name)) {
			const meta = readJson(join(tasks, id, "meta.json"));
			if (!live.has(`${meta.pane} 0`)) continue;
			if (!["waiting", "error"].includes(readJson(join(tasks, id, "state.json")).state)) active += 1;
			if (meta.contract?.kind === "research") researching += 1;
		}
		const over = [
			researching >= config.max_research ? `${researching} research workers are running (limit ${config.max_research})` : "",
			active >= config.max_active ? `${active} workers are active (limit ${config.max_active})` : "",
		].filter(Boolean);
		if (over.length && !(await ctx.ui.confirm("Start another research worker?", `${over.join("; ")}. Start it anyway?`))) return false;
		const tmp = mkdtempSync(join(tmpdir(), "tp-research-"));
		try {
			const args = ["--worker", config.worker, "--model", config.model, "--thinking", config.thinking, "--name", "research", "--kind", "research"];
			const conversation = conversationSinceDelegation(ctx);
			if (conversation) {
				writeFileSync(join(tmp, "conversation.md"), conversation);
				args.push("--conversation", join(tmp, "conversation.md"));
			}
			args.push("--note", ASK_IF_UNCLEAR, "--note", RESEARCH_REPORT);
			if (review) args.push("--note", "A reviewer on another provider will check the report's sources when you finish.");
			const id = (await must(bin("tp-spawn"), args, question, 60_000)).split("\n")[0];
			const contract = readJson(join(tasks, id, "meta.json")).contract ?? {};
			const task: Delegated = { id, request: question, review: review ? "required" : "none", rounds: 0, since: Date.now(), report: contract.artifact };
			delegated.set(id, task);
			const minutes = Math.round((contract.budget?.seconds ?? 0) / 60);
			post(
				`Research started: ${id} (${config.worker} ${config.model}/${config.thinking}, up to ${minutes} min${review ? ", reviewed" : ""}). Report: ${contract.artifact}\n\n${question}`,
				{ kind: "delegated", id, research: true, report: contract.artifact },
			);
			ctx.ui.setStatus("auto", `AUTO research · ${id}`);
			showRunning();
			follow(task);
			return true;
		} finally {
			rmSync(tmp, { recursive: true, force: true });
		}
	};

	let scanning = false;
	pi.on("session_start", (_event, ctx) => {
		lastCtx = ctx;
		if (scanning) return;
		scanning = true;
		void (async () => {
			for (;;) {
				await scanPrompts().catch(() => undefined);
				await sleep(3000);
			}
		})();
		void (async () => {
			for (;;) {
				await enforceContracts().catch(() => undefined);
				await sleep(15_000);
			}
		})();
	});

	// +followup [id] <message>: the task named, the only running one, or the user's pick.
	const forcedTarget = async (ctx: any, rest: string): Promise<{ task?: Delegated; text: string }> => {
		const [word, ...more] = rest.split(/\s+/);
		if (word && !delegated.has(word) && readText(join(await taskDir(word), "meta.json")) && (await paneAlive(await taskDir(word)))) {
			// A subagent main started by hand: follow it from here on.
			const task: Delegated = { id: word, request: readText(join(await taskDir(word), "task.md")), review: "none", rounds: 0, since: 0 };
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

	pi.registerCommand("auto", {
		description: "Turn Laya-directed routing on or off for this session: /auto on | /auto off",
		handler: async (args, ctx) => {
			const value = String(args ?? "").trim().toLowerCase();
			if (value !== "on" && value !== "off") {
				ctx.ui.notify(`AUTO is ${enabled ? "on" : "off"}; use /auto on or /auto off`, "info");
				return;
			}
			enabled = value === "on";
			ctx.ui.setStatus("auto", enabled ? "AUTO on" : "AUTO off (manual)");
		},
	});

	pi.on("input", async (event, ctx) => {
		lastCtx = ctx;
		const text = event.text.trim();
		// Messages typed while main is working steer that work; they are not new requests.
		if (!enabled || event.source === "extension" || event.streamingBehavior || !text || text.startsWith("/") || text.startsWith("!")) {
			return { action: "continue" };
		}

		const sendTo = async (target: Delegated, message: string) => {
			target.since = Date.now();
			target.rounds = 0;
			await typeInto(target.id, message);
			// Reviewers read these with the original task (bin/tp-review).
			appendFileSync(join(await taskDir(target.id), "followups.md"), `- ${message.replace(/\r?\n/g, " ")}\n`);
			post(`Sent to ${target.id}: ${message}`, { kind: "follow-up", id: target.id });
			ctx.ui.setStatus("auto", `AUTO follow-up · ${target.id}`);
			follow(target);
		};

		const forced = /^\+followup(\s+|$)/i.exec(text);
		if (forced) {
			const { task, text: message } = await forcedTarget(ctx, text.slice(forced[0].length));
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

		const researchMarker = /^\+research(\s+|$)/i.exec(text);
		if (researchMarker) {
			let question = text.slice(researchMarker[0].length).trim();
			const reviewed = /^\+review(\s+|$)/i.exec(question);
			if (reviewed) question = question.slice(reviewed[0].length).trim();
			if (!question) {
				ctx.ui.notify("+research needs a question", "warning");
				ctx.ui.setEditorText(text);
				return { action: "handled" };
			}
			try {
				if (!(await research(ctx, question, !!reviewed))) ctx.ui.setEditorText(text);
			} catch (error) {
				ctx.ui.notify(`Could not start research: ${(error as Error).message}`, "warning");
				ctx.ui.setEditorText(text);
			}
			return { action: "handled" };
		}

		const target = await followUpTarget(ctx, text);
		if (target) {
			try {
				await sendTo(target, text);
				return { action: "handled" };
			} catch (error) {
				ctx.ui.notify(`Could not send to ${target.id}: ${(error as Error).message}`, "warning");
			}
		}

		let decision: Decision;
		try {
			const r = await must(bin("tp-route"), ["--dispatch"], text);
			decision = JSON.parse(r);
		} catch {
			ctx.ui.setStatus("auto", "AUTO off: Laya unavailable");
			return { action: "continue" };
		}
		const routed = `${decision.model}/${decision.thinking}`;
		const tags = `${decision.route} · review ${decision.review}${decision.overrides.length ? ` · ${decision.overrides.join(", ")} set by marker` : ""}`;
		if (decision.placement === "main" && decision.worker === "pi") {
			const used = await switchTo(ctx, decision.model, decision.thinking);
			ctx.ui.setStatus("auto", `AUTO main · ${used} · ${tags}`);
		} else if (decision.placement === "main") {
			try {
				const id = await delegate(ctx, decision, true);
				const task: Delegated = { id, request: decision.task, review: decision.review, rounds: 0, since: Date.now(), foreground: true };
				delegated.set(id, task);
				post(`Answering in the viewer: ${id} (claude ${routed}):\n\n${decision.task}`, { kind: "delegated", id, foreground: true, ...decision });
				ctx.ui.setStatus("auto", `AUTO main · foreground ${id} · ${routed} · ${tags}`);
				showRunning();
				follow(task);
				return { action: "handled" };
			} catch (error) {
				// Could not start the worker: answer on the home model instead.
				const fallback = home();
				if (fallback) await switchTo(ctx, `${fallback.provider}/${fallback.id}`, fallback.thinking);
				ctx.ui.setStatus("auto", `AUTO main · home model (foreground ${routed} failed: ${(error as Error).message}) · ${tags}`);
			}
		} else {
			try {
				const id = await delegate(ctx, decision);
				const task: Delegated = { id, request: decision.task, review: decision.review, rounds: 0, since: Date.now() };
				delegated.set(id, task);
				post(
					`Delegated to ${id} (${decision.worker} ${routed}; ${decision.intent}, ${decision.scope}, review ${decision.review}):\n\n${decision.task}`,
					{ kind: "delegated", id, ...decision },
				);
				ctx.ui.setStatus("auto", `AUTO subagent · ${id} · ${routed} · ${tags}`);
				showRunning();
				follow(task);
				return { action: "handled" };
			} catch (error) {
				// Could not spawn: let main delegate it by hand, as in phase 1.
				note =
					`[tmux-pi AUTO] Automatic delegation failed (${(error as Error).message}). Delegate this request with ` +
					`spawn_subagent using worker ${decision.worker}, model ${decision.model}, thinking ${decision.thinking}, ` +
					`and tell the subagent: ${ASK_IF_UNCLEAR}` + (decision.review === "required" ? " When it finishes, run review_subagent on it." : "");
				ctx.ui.setStatus("auto", `AUTO subagent (manual) · ${routed} · ${tags}`);
			}
		}
		return decision.task !== text ? { action: "transform", text: decision.task } : { action: "continue" };
	});

	pi.on("before_agent_start", () => {
		if (!note) return;
		const content = note;
		note = undefined;
		return { message: { customType: "tmux-pi-auto", content, display: true } };
	});
}
