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
import { existsSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Target = { model: string; thinking: string };
type Policy = { models: string[]; after_consecutive_errors: number; to: Target };
type Call = { name: string; args: unknown; error: boolean; output: string };

function readJson(file: string): any {
	try {
		return JSON.parse(readFileSync(file, "utf8"));
	} catch {
		return undefined;
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

export default function (pi: ExtensionAPI) {
	const dir = process.env.TP_TASK_DIR;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!dir || !agentDir) return;
	const root = dirname(agentDir);
	const policy: Policy | undefined = readJson(process.env.TP_LOCAL_CONFIG ?? join(root, "config", "local.json"))?.escalation;
	if (!policy?.models?.length || !policy.to?.model) return;
	const roster: { worker: string; model: string }[] = readJson(join(root, "config", "router.json"))?.models ?? [];
	const target = roster.find((entry) => entry.model === policy.to.model);
	if (!target) return;

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

		const instruction =
			`A smaller local model could not finish this task: it failed ${streak} tool calls in a row. ` +
			"The original task and context come first in this packet; escalation.md shows what it tried and why each call failed. " +
			"Complete the original task yourself. Do not repeat its failing calls without fixing what made them fail.";
		const context = ["packet.md", "escalation.md"].filter((name) => existsSync(join(dir, name)));
		const newId = execFileSync(
			join(root, "bin", "tp-spawn"),
			[
				"--worker", target!.worker,
				"--model", policy.to.model,
				"--thinking", policy.to.thinking,
				"--name", `esc-${id}`,
				...context.flatMap((name) => ["--context", join(dir, name)]),
				"--note", `Escalated from ${id} (${model}).`,
				"--", instruction,
			],
			{ encoding: "utf8", timeout: 60_000 },
		).trim();

		writeFileSync(join(dir, "escalated"), `${newId}\n`);
		const meta = readJson(join(dir, "meta.json")) ?? {};
		meta.escalated_to = { id: newId, model: policy.to.model, after_errors: streak };
		writeFileSync(join(dir, "meta.json.tmp"), `${JSON.stringify(meta)}\n`);
		renameSync(join(dir, "meta.json.tmp"), join(dir, "meta.json"));
		ctx.abort();
		ctx.shutdown();
	}
}
