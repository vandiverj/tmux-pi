/**
 * subagents — gives the main tmux-pi agent tools to run subagents.
 *
 *   spawn_subagent    route the task (bin/tp-route) unless a model is given, then tp-spawn
 *   list_subagents    tp-list
 *   send_to_subagent  type a follow-up message into a subagent's session
 *   collect_result    read (optionally wait for) a subagent's state and last reply
 *   kill_subagent     tp-kill
 *   review_subagent   tp-review (cross-provider review of completed work)
 *
 * Active only in the main agent: inside tmux and not in a tp-spawn worker
 * (workers have TP_TASK_DIR). Everything goes through the bin/tp-* commands and
 * the task dir files, so the tools see exactly what the status pane shows.
 */

import { execFile } from "node:child_process";
import { existsSync, readdirSync, readFileSync, realpathSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { StringEnum, Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const THINKING = ["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const;

type Run = { code: number; stdout: string; stderr: string };

function run(cmd: string, args: string[], input?: string): Promise<Run> {
	return new Promise((resolve) => {
		const child = execFile(cmd, args, { timeout: 120_000 }, (error, stdout, stderr) => {
			const code = error ? (typeof (error as any).code === "number" ? (error as any).code : 1) : 0;
			resolve({ code, stdout: String(stdout), stderr: String(stderr) });
		});
		if (input !== undefined) child.stdin?.end(input);
	});
}

// Throw the command's own error message so the model sees why it failed.
async function must(cmd: string, args: string[], input?: string): Promise<string> {
	const r = await run(cmd, args, input);
	if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || `${cmd} exited with ${r.code}`);
	return r.stdout.trim();
}

const text = (t: string, details?: unknown) => ({ content: [{ type: "text" as const, text: t }], details });

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

const sleep = (ms: number, signal?: AbortSignal) =>
	new Promise<void>((resolve) => {
		const t = setTimeout(resolve, ms);
		signal?.addEventListener("abort", () => {
			clearTimeout(t);
			resolve();
		});
	});

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const root = dirname(process.env.PI_CODING_AGENT_DIR);
	const bin = (name: string) => join(root, "bin", name);

	// When each subagent was last given input; collect_result waits for a report newer than this.
	const lastInput = new Map<string, number>();

	const stateDir = () => must("tmux", ["display-message", "-p", "#{@tp_state_dir}"]);
	const taskDir = async (id: string) => {
		if (!/^[\w.-]+$/.test(id)) throw new Error(`bad subagent id: ${id}`);
		const dir = join(await stateDir(), "tasks", id);
		if (!existsSync(join(dir, "meta.json"))) throw new Error(`no subagent '${id}' (see list_subagents)`);
		return dir;
	};

	// running | done | failed:<code> | killed — from the pane, like tp_rows.
	const paneState = async (dir: string) => {
		const pane = readJson(join(dir, "meta.json")).pane;
		const panes = await must("tmux", ["list-panes", "-a", "-F", "#{pane_id} #{pane_dead}"]);
		const line = panes.split("\n").find((l) => l.split(" ")[0] === pane);
		if (!line) return "killed";
		if (line.endsWith(" 0")) return "running";
		const code = readText(join(dir, "exit")) || "?";
		return code === "0" ? "done" : `failed:${code}`;
	};

	pi.registerTool({
		name: "spawn_subagent",
		label: "Spawn subagent",
		description:
			"Start a subagent in its own tmux-pi session to work on a task, and return its id. " +
			"The subagent gets a context packet and nothing else: no AGENTS.md, CLAUDE.md or this conversation. " +
			"The packet is the task, then the files you list in `files` (whole, or just the lines that matter), " +
			"then `notes`. Pick only what the task needs: the files it will change, the interfaces it must " +
			"use, and facts from this conversation it cannot rediscover. Say what to report back. It can " +
			"still read other files itself. Leave model and thinking unset to let the local router " +
			"pick them; set them only when the user asked for a specific model. The subagent runs in the " +
			"background; use collect_result to wait for its reply. Normally it uses the main pane's current " +
			"directory; an explicit workdir requires a fresh human confirmation and does not grant Claude folder trust.",
		parameters: Type.Object({
			task: Type.String({ description: "Complete, self-contained instructions for the subagent" }),
			worker: Type.Optional(StringEnum(["pi", "claude"] as const, { description: "Harness; implied by the router or the model" })),
			model: Type.Optional(Type.String({ description: "pi: provider/id (e.g. openai-codex/gpt-6-sol); claude: alias (sonnet, opus, haiku)" })),
			thinking: Type.Optional(StringEnum(THINKING, { description: "Thinking level" })),
			name: Type.Optional(Type.String({ description: "Short id prefix, e.g. 'tests' or 'review'" })),
			workdir: Type.Optional(Type.String({ description: "Absolute project directory explicitly approved by the human; otherwise use the main pane cwd. Requires interactive confirmation." })),
			files: Type.Optional(
				Type.Array(
					Type.Object({
						path: Type.String({ description: "File path, relative to the working directory" }),
						lines: Type.Optional(Type.String({ description: "Line range to include, e.g. '40-120'; omit for the whole file" })),
					}),
					{ description: "Files or snippets to include in the packet" },
				),
			),
			notes: Type.Optional(Type.Array(Type.String(), { description: "Short facts, decisions or constraints to include in the packet" })),
			view: Type.Optional(Type.Boolean({ description: "Show it in the viewer slot right away" })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			// A model's assertion of user approval is not approval. Confirm the
			// exact requested path with the human; no headless auto-accept.
			let approvedWorkdir: string | undefined;
			if (params.workdir) {
				if (!params.workdir.startsWith("/") || !ctx.hasUI) {
					throw new Error("workdir override requires explicit human approval of an absolute project path");
				}
				try {
					approvedWorkdir = realpathSync(params.workdir);
					if (!statSync(approvedWorkdir).isDirectory()) throw new Error("not a directory");
				} catch {
					throw new Error(`workdir must be an existing directory: ${params.workdir}`);
				}
				if (!(await ctx.ui.confirm("Override worker project directory?",
					`Run this worker (and its reviewer) in:\n${approvedWorkdir}\nThis does not grant Claude folder trust. Only approve a project you selected.`))) {
					throw new Error("workdir override was not approved by the human");
				}
			}
			// Runaway guard: cap simultaneous workers (config/local.json "max_workers", default 6).
			const cap = Number(readJson(join(root, "config/local.json")).max_workers) || 6;
			const tasksDir = join(await stateDir(), "tasks");
			const active = existsSync(tasksDir) ? readdirSync(tasksDir).filter((d) =>
				["starting", "working", "needs_input"].includes(readJson(join(tasksDir, d, "state.json")).state)).length : 0;
			if (active >= cap) throw new Error(`${active} subagents already running (limit ${cap}); collect or kill one first`);
			let { worker, model, thinking } = params;
			let routed = "";
			if (!model) {
				const decision = JSON.parse(await must(bin("tp-route"), [], params.task));
				worker = decision.worker;
				model = decision.model;
				thinking = thinking ?? decision.thinking;
				routed = ` (routed: ${worker} ${model} ${thinking})`;
			}
			if (!worker) worker = model && !model.includes("/") ? "claude" : "pi";
			const args = ["--worker", worker!];
			if (approvedWorkdir) args.push("--workdir", approvedWorkdir);
			if (model) args.push("--model", model);
			if (thinking) args.push("--thinking", thinking);
			if (params.name) args.push("--name", params.name.replace(/[^\w.-]+/g, "-"));
			for (const f of params.files ?? []) {
				if (f.lines && !/^\d+-\d+$/.test(f.lines)) throw new Error(`bad line range for ${f.path}: ${f.lines} (use start-end)`);
				args.push("--context", f.lines ? `${f.path}:${f.lines}` : f.path);
			}
			for (const n of params.notes ?? []) args.push("--note", n);
			if (params.view) args.push("--view");
			const id = (await must(bin("tp-spawn"), args, params.task)).split("\n")[0];
			lastInput.set(id, Date.now());
			const packet = readText(join(await stateDir(), "tasks", id, "packet.md"));
			return text(`started ${id}${routed}; packet ${packet.length} chars`, { id, worker, model, thinking });
		},
	});

	pi.registerTool({
		name: "list_subagents",
		label: "List subagents",
		description: "List subagents with state (working, tool, waiting, done, failed, killed), tokens and model.",
		parameters: Type.Object({}),
		async execute() {
			return text(await must(bin("tp-list"), []));
		},
	});

	pi.registerTool({
		name: "send_to_subagent",
		label: "Message subagent",
		description:
			"Send a follow-up message to a subagent that is waiting for input, as if typed into its session. " +
			"Use collect_result afterwards to wait for its answer.",
		parameters: Type.Object({
			id: Type.String({ description: "Subagent id" }),
			message: Type.String({ description: "Message to send" }),
		}),
		async execute(_id, params) {
			const dir = await taskDir(params.id);
			const ps = await paneState(dir);
			if (ps !== "running") throw new Error(`subagent '${params.id}' is ${ps}; it cannot take input`);
			const pane = readJson(join(dir, "meta.json")).pane;
			// Type literally, then Enter separately so the TUI treats the text as a paste, not keys.
			await must("tmux", ["send-keys", "-t", pane, "-l", params.message.replace(/\r?\n/g, " ")]);
			await sleep(300);
			await must("tmux", ["send-keys", "-t", pane, "Enter"]);
			lastInput.set(params.id, Date.now());
			return text(`sent to ${params.id}`);
		},
	});

	pi.registerTool({
		name: "collect_result",
		label: "Collect result",
		description:
			"Get a subagent's state and its last reply. With wait_seconds, wait until it has finished " +
			"its current work (waiting for input, errored or exited) or the time runs out.",
		parameters: Type.Object({
			id: Type.String({ description: "Subagent id" }),
			wait_seconds: Type.Optional(Type.Number({ description: "Maximum seconds to wait (0-1800, default 0)" })),
		}),
		async execute(_id, params, signal, onUpdate) {
			const dir = await taskDir(params.id);
			const deadline = Date.now() + Math.min(Math.max(params.wait_seconds ?? 0, 0), 1800) * 1000;
			const since = lastInput.get(params.id) ?? 0;
			let state: any = {};
			let ps = "running";
			for (;;) {
				// A local-model worker that kept failing hands off to a stronger model (extensions/escalate.ts).
				const escalatedTo = readText(join(dir, "escalated"));
				if (escalatedTo) {
					const why = readJson(join(dir, "meta.json")).escalated_to ?? {};
					return text(
						`${params.id} escalated to ${escalatedTo} (${why.model ?? "a stronger model"}) after ${why.after_errors ?? "repeated"} ` +
							`failed tool calls in a row. Use collect_result on ${escalatedTo}; its attempt log is ${join(dir, "escalation.md")}.`,
						{ id: params.id, status: "escalated", escalatedTo },
					);
				}
				state = readJson(join(dir, "state.json"));
				ps = await paneState(dir);
				const fresh = Date.parse(state.updated ?? "") >= since - 1000;
				const settled = ps !== "running" || (fresh && ["waiting", "error"].includes(state.state));
				if (settled || Date.now() >= deadline || signal?.aborted) break;
				onUpdate?.(text(`${params.id}: ${state.state ?? "starting"}${state.tool ? ` — ${state.tool}` : ""}`));
				await sleep(2000, signal);
			}
			const raw = ps === "running" ? (state.state ?? "starting") : ps;
			// starting, working, needs_input, paused, settled, completed, …; reading a settled result here
			// hands it off, so it becomes completed (bin/tp-contract collect).
			const status = (await run(bin("tp-contract"), ["collect", params.id])).stdout.trim() || raw;
			const t = state.tokens;
			const lines = [
				`id: ${params.id}`,
				`status: ${status}${state.tool ? ` (${state.tool})` : ""}`,
				state.model ? `model: ${state.model}` : "",
				t ? `tokens: in ${t.input + t.cacheRead + t.cacheWrite}, out ${t.output}; turns ${state.turns}` : "",
				state.error ? `error: ${state.error}` : "",
				"",
			].filter((l, i, a) => l || i === a.length - 1);
			// A worker stuck before its reporter runs (e.g. Claude's folder-trust prompt) or
			// at a permission prompt needs a human; show its screen so the model can say so.
			if (ps === "running" && ["starting", "permission"].includes(raw)) {
				const pane = readJson(join(dir, "meta.json")).pane;
				const screen = (await run("tmux", ["capture-pane", "-p", "-t", pane])).stdout
					.split("\n").filter((l) => l.trim()).slice(-15).join("\n");
				lines.push(`${params.id} may be waiting for the user (they can view it with enter in the status pane). Its screen:`, screen, "");
			}
			const reply = readText(join(dir, "result.md"));
			const body = reply.length > 20_000 ? `${reply.slice(0, 20_000)}\n…(truncated; full reply in ${join(dir, "result.md")})` : reply;
			return text(`${lines.join("\n")}\n${body || "(no reply yet)"}`, { id: params.id, status });
		},
	});

	pi.registerTool({
		name: "review_subagent",
		label: "Review subagent result",
		description:
			"Start an independent reviewer on the other provider (Pi versus Claude) for a successfully completed subagent. " +
			"The reviewer receives the original task, context packet, and result, checks the working tree, and reports APPROVED or REQUEST_CHANGES. " +
			"It never edits files. Use collect_result on the returned id. " +
			"The local review policy decides first: routine research and summaries are not reviewed. " +
			"Set force only when the user asked for a review.",
		parameters: Type.Object({
			id: Type.String({ description: "Successfully completed subagent id to review" }),
			force: Type.Optional(Type.Boolean({ description: "Review even when the policy says none" })),
		}),
		async execute(_id, params) {
			const r = await run(bin("tp-review"), params.force ? ["--force", params.id] : [params.id]);
			// Exit 3: the policy said no review is needed; not an error.
			if (r.code === 3) return text(r.stderr.trim(), { id: params.id, review: "none" });
			if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim() || `tp-review exited with ${r.code}`);
			const reviewId = r.stdout.trim();
			lastInput.set(reviewId, Date.now());
			return text(`started cross-provider review ${reviewId} for ${params.id}`, { id: reviewId, reviewOf: params.id });
		},
	});

	pi.registerCommand("end", {
		description: "Kill all tmux-pi agents and close the tmux workspace",
		handler: async (_args, ctx) => {
			const answer = await ctx.ui.confirm(
				"Exit tmux-pi workspace",
				"This stops the main agent and every subagent, archives eligible worker task records, retains protected records, then closes this tmux workspace.",
			);
			if (!answer) return;
			await must(bin("tp-exit"), []);
		},
	});

	pi.registerCommand("reset", {
		description: "Kill all subagents and start a fresh main-agent session",
		handler: async (_args, ctx) => {
			const answer = await ctx.ui.confirm(
				"Reset tmux-pi workspace",
				"This stops every subagent and starts a new main-agent session. Task files are kept.",
			);
			if (!answer) return;
			const output = await must(bin("tp-reset"), []);
			lastInput.clear();
			const result = await ctx.newSession({
				withSession: async (freshCtx) => {
					freshCtx.ui.notify(output || "Workspace reset", "info");
				},
			});
			if (result.cancelled) ctx.ui.notify("Reset cancelled before creating a new session", "warning");
		},
	});

	pi.registerTool({
		name: "kill_subagent",
		label: "Kill subagent",
		description: "Stop a subagent and close its session. Its task files and last reply are kept.",
		parameters: Type.Object({ id: Type.String({ description: "Subagent id" }) }),
		async execute(_id, params) {
			await must(bin("tp-kill"), [params.id]);
			lastInput.delete(params.id);
			return text(`killed ${params.id}`);
		},
	});
}
