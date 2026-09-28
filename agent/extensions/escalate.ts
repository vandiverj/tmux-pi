/**
 * escalate — hand a struggling local-model worker's task to a stronger model.
 *
 * Active only in tp-spawn workers (TP_TASK_DIR) whose model is listed in the
 * "escalation" section of config/local.json. Each failed tool call adds to a
 * streak; a successful one resets it. When the streak passes
 * after_consecutive_errors, this worker:
 *
 *   1. writes $TP_TASK_DIR/escalation.md: every tool call it made (arguments,
 *      error or result) and its last reply
 *   2. spawns the escalation target with tp-spawn, whose packet is the original
 *      packet plus escalation.md
 *   3. records the new id in $TP_TASK_DIR/escalated and meta.json, then exits
 *
 * The main agent's collect_result follows the escalated file to the new worker.
 */

import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Policy = { models: string[]; after_consecutive_errors: number };
type Call = { name: string; args: unknown; error: boolean; output: string };

function readJson(file: string): any {
	try {
		return JSON.parse(readFileSync(file, "utf8"));
	} catch {
		return undefined;
	}
}

// Replace metadata in the same directory: readers never see a truncated JSON file.
function writeJsonAtomic(file: string, value: unknown): void {
	const temp = join(dirname(file), `.meta-${randomUUID()}.tmp`);
	try {
		writeFileSync(temp, `${JSON.stringify(value)}\n`, { flag: "wx", mode: 0o600 });
		renameSync(temp, file);
	} catch (error) {
		try { unlinkSync(temp); } catch { /* write may have failed before creating it */ }
		throw error;
	}
}

function textOf(value: any): string {
	const content = value?.content ?? value;
	if (typeof content === "string") return content;
	if (!Array.isArray(content)) return value === undefined ? "" : JSON.stringify(value);
	return content
		.filter((part: any) => part?.type === "text")
		.map((part: any) => part.text)
		.join("\n");
}

const clip = (text: string, max: number) => (text.length > max ? `${text.slice(0, max)}\n… (${text.length - max} more characters)` : text);

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

function readText(file: string): string {
	try {
		return readFileSync(file, "utf8").trim();
	} catch {
		return "";
	}
}

function requestText(dir: string): string {
	const request = readText(join(dir, "request.md"));
	if (request) return request;
	const task = readText(join(dir, "task.md"));
	return task.includes("tmux-pi-task-record:v1") ? extractFencedRequest(task) : task;
}

export default function (pi: ExtensionAPI) {
	const dir = process.env.TP_TASK_DIR;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!dir || !agentDir) return;
	const root = dirname(agentDir);
	const policy: Policy | undefined = readJson(process.env.TP_LOCAL_CONFIG ?? join(root, "config", "local.json"))?.escalation;
	if (!policy?.models?.length) return;

	let model = "";
	let streak = 0;
	let escalated = false;
	let lastReply = "";
	const calls: Call[] = [];
	const pending = new Map<string, { name: string; args: unknown }>();
	const active = () => policy.models.includes(model);

	pi.on("model_select", (event) => {
		model = `${event.model.provider}/${event.model.id}`;
	});

	pi.on("message_end", (event) => {
		const message: any = event.message;
		if (message?.role === "assistant") lastReply = textOf(message) || lastReply;
	});

	pi.on("tool_execution_start", (event) => {
		pending.set(event.toolCallId, { name: event.toolName, args: event.args });
	});

	pi.on("tool_execution_end", (event, ctx) => {
		if (ctx.model) model = `${ctx.model.provider}/${ctx.model.id}`;
		const start = pending.get(event.toolCallId);
		pending.delete(event.toolCallId);
		calls.push({ name: event.toolName, args: start?.args, error: event.isError, output: textOf(event.result) });
		streak = event.isError ? streak + 1 : 0;
		const meta = readJson(join(dir, "meta.json")) ?? {};
		if (meta.escalated_from || existsSync(join(dir, "escalated")) || existsSync(join(dir, "escalation.json"))) return;
		if (!active() || escalated || streak <= policy.after_consecutive_errors) return;
		escalated = true;
		try {
			escalate(ctx);
		} catch (error) {
			// Leave the worker running if the handoff fails; the error is in escalation.md.
			writeFileSync(join(dir, "escalation-error.txt"), `${String(error)}\n`);
		}
	});

	function escalate(ctx: { abort(): void; shutdown(): void }) {
		const id = process.env.TP_TASK_ID ?? "";
		const log = calls
			.map((call, index) =>
				[
					`### ${index + 1}. ${call.name} — ${call.error ? "ERROR" : "ok"}`,
					"",
					"Arguments:",
					"```json",
					JSON.stringify(call.args ?? {}, null, 2),
					"```",
					call.error ? "Error:" : "Result:",
					"```",
					clip(call.output, call.error ? 2000 : 600),
					"```",
				].join("\n"),
			)
			.join("\n\n");
		const notes = [
			`# Escalation from ${id}`,
			"",
			`The worker on \`${model}\` made ${streak} failed tool calls in a row (the limit is ${policy.after_consecutive_errors}) and was stopped.`,
			"Its original task and context packet are included above this file. Below is every tool call it made, in order.",
			"",
			"## Tool calls",
			"",
			log || "(none)",
			"",
			"## Its last reply",
			"",
			lastReply ? clip(lastReply, 4000) : "(none)",
			"",
		].join("\n");
		writeFileSync(join(dir, "escalation.md"), notes);

		const evidence = calls.slice(-streak).map((c) => `${c.name}: ${clip(c.output, 300)}`).join("\n");
		const plan = JSON.parse(
			execFileSync(
				join(root, "bin", "tp-contract"),
				["escalate", id, "--trigger", "tool_errors", "--reason", `${streak} failed tool calls in a row`, "--evidence", evidence],
				{ encoding: "utf8", timeout: 30_000 },
			),
		);
		const target = plan.target;
		const sourceMeta = readJson(join(dir, "meta.json"));
		if (!sourceMeta || typeof sourceMeta !== "object" || Array.isArray(sourceMeta)) throw new Error("source task metadata unavailable for escalation");
		const context = ["packet.md", "escalation.md"].filter((name) => existsSync(join(dir, name)));
		// tp-spawn stores the merged preflight in meta.json, not preflight.json.
		// Materialize that authoritative restriction before the target's contract is
		// built; copying metadata after spawn cannot undo a newly granted edit right.
		if (sourceMeta.preflight && typeof sourceMeta.preflight === "object" && !Array.isArray(sourceMeta.preflight)) {
			writeJsonAtomic(join(dir, "preflight.json"), sourceMeta.preflight);
		}
		// tp-spawn owns the copies and builds a fresh target contract/budget. Never
		// reuse the source contract's artifact path or elapsed-time checkpoints.
		const lineage = ([
			["preflight.json", "--preflight"],
			["main-contract.json", "--main-contract"],
			["policy-decision.json", "--policy-decision"],
			["context-manifest.json", "--context-manifest"],
		] as const).flatMap(([name, flag]) => existsSync(join(dir, name)) ? [flag, join(dir, name)] : []);
		const decision = { ...(sourceMeta.decision && typeof sourceMeta.decision === "object" ? sourceMeta.decision : {}),
			worker: target.worker, model: target.model, thinking: target.thinking, route: "escalated" };
		let newId = "";
		try {
			newId = execFileSync(
				join(root, "bin", "tp-spawn"),
				[
					"--worker", target.worker,
					"--model", target.model,
					"--thinking", target.thinking,
					"--name", `esc-${id}`,
					...(sourceMeta.contract ? ["--kind", sourceMeta.contract.kind === "research" ? "research" : "task"] : []),
					...(sourceMeta.contract || decision.review === "required" ? ["--decision-json", JSON.stringify(decision)] : []),
					...lineage,
					...context.flatMap((name) => ["--context", join(dir, name)]),
					"--note", `Escalated from ${id} (${model}). Original task is preserved; escalation.md shows failed tool evidence.`,
					"--", requestText(dir),
				],
				{ encoding: "utf8", timeout: 60_000 },
			).trim().split("\n")[0];
			const targetMetaPath = join(dirname(dir), newId, "meta.json");
			const targetMeta = readJson(targetMetaPath);
			if (!targetMeta || typeof targetMeta !== "object" || Array.isArray(targetMeta) || targetMeta.id !== newId) {
				throw new Error(`target task metadata unavailable for ${newId}`);
			}
			targetMeta.escalated_from = { id, trigger: "tool_errors", reason: `${streak} failed tool calls in a row` };
			// These fields describe the inherited authority, not the new worker's identity.
			// File-backed fields are copied by tp-spawn above; preserve metadata-only
			// sources too, without overwriting the target's new contract or pane.
			for (const key of ["decision", "blocked_by", "preflight", "main_contract", "policy_decision", "context_manifest", "handshake_id"] as const) {
				if (sourceMeta[key] !== undefined && targetMeta[key] === undefined) targetMeta[key] = sourceMeta[key];
			}
			if (sourceMeta.decision?.review === "required") {
				targetMeta.decision = { ...targetMeta.decision, review: "required" };
			}
			writeJsonAtomic(targetMetaPath, targetMeta);
			execFileSync(
				join(root, "bin", "tp-contract"),
				["escalated", id, "--to", newId, "--outcome", "started", "--record-json", JSON.stringify(plan.record)],
				{ encoding: "utf8", timeout: 30_000 },
			);
		} catch (error) {
			if (newId) execFileSync(join(root, "bin", "tp-kill"), [newId], { encoding: "utf8", timeout: 30_000 });
			execFileSync(
				join(root, "bin", "tp-contract"),
				["escalated", id, "--outcome", "failed", "--record-json", JSON.stringify(plan.record), "--message", String(error)],
				{ encoding: "utf8", timeout: 30_000 },
			);
			throw error;
		}
		const sourceMetaPath = join(dir, "meta.json");
		const meta = readJson(sourceMetaPath);
		if (!meta || typeof meta !== "object" || Array.isArray(meta)) throw new Error("source task metadata unavailable after escalation");
		meta.escalated_to = { id: newId, model: target.model, after_errors: streak };
		writeJsonAtomic(sourceMetaPath, meta);
		ctx.abort();
		ctx.shutdown();
	}
}
