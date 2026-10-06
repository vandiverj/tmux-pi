/**
 * offload — replace large tool results the main model has already used with a short receipt.
 *
 * Main only. At `turn_end`, successful text results that sit before the assistant
 * message that just finished (so that response was generated with the full text in
 * context) get a `context_edit` replacement. The raw session entry is never touched,
 * the result stays paired with its tool call, and `tool_output` rereads a bounded
 * slice from the raw entry. Results of the turn that just ran, failed results,
 * non-text results, and results whose receipt would not be meaningfully shorter stay
 * verbatim. Native compaction is not hooked; it sees the same projection the model does.
 */
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export const MIN_BYTES = 4096; // smaller results stay verbatim
export const MIN_SAVED_BYTES = 1024; // a replacement must save at least this much
const SLICE_DEFAULT = 8000; // characters returned by tool_output
const SLICE_MAX = 20000;

type Draft = { type: "context_edit"; targetId: string; replacement: { content: string } };
type Limits = { minBytes: number; minSaved: number };

const bytesOf = (text: string) => Buffer.byteLength(text, "utf8");

// One bounded line: no control characters, no newlines.
function clean(value: unknown, max: number): string {
	const text = String(value ?? "").replace(/[\u0000-\u001f\u007f-\u009f\u2028\u2029]+/g, " ").trim();
	return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// Where the output came from, without echoing arguments that can carry secrets:
// a path, or only the program name of a shell command. The paired tool call keeps
// the full arguments in context.
function source(args: any): string {
	const path = args?.path ?? args?.file_path;
	if (typeof path === "string" && path) return ` path=${JSON.stringify(clean(path, 160))}`;
	if (typeof args?.command === "string") {
		const first = args.command.trim().split(/\s+/)[0] ?? "";
		const program = /^[A-Za-z0-9_.\/-]{1,60}$/.test(first) ? first.split("/").pop() : "";
		return ` command=${program || "shell"}`;
	}
	return "";
}

function textBlocks(message: any): string[] | undefined {
	const content = message?.content;
	if (!Array.isArray(content) || !content.length) return undefined;
	if (content.some((part: any) => part?.type !== "text" || typeof part.text !== "string")) return undefined;
	return content.map((part: any) => part.text);
}

/** Deterministic receipt: same inputs, same bytes, so an edited prefix stays cacheable. */
export function receipt(entryId: string, toolName: string, args: unknown, bytes: number, lines: number): string {
	const id = clean(entryId, 64);
	return `[tool output offloaded after use] tool=${clean(toolName, 60)} status=ok bytes=${bytes} lines=${lines} entry=${id}${source(args)}\n` +
		`The full output is kept in the session. Call tool_output(entry="${id}") to reread a bounded slice; the arguments are in the paired tool call.`;
}

/** Replace only when strictly shorter and the saving is worth a context edit. */
export function worthReplacing(originalBytes: number, replacementBytes: number, minSaved = MIN_SAVED_BYTES): boolean {
	return replacementBytes < originalBytes && originalBytes - replacementBytes >= minSaved;
}

/**
 * Drafts for results the model has already responded to: everything in the projection
 * before `beforeId`, the assistant entry that just finished.
 */
export function plan(
	projected: { sourceEntry: any; messages: unknown[] }[],
	edited: Set<string>,
	beforeId: string,
	limits: Limits = { minBytes: MIN_BYTES, minSaved: MIN_SAVED_BYTES },
): Draft[] {
	const cut = projected.findIndex((item) => item.sourceEntry?.id === beforeId);
	const calls = new Map<string, unknown>();
	const drafts: Draft[] = [];
	for (const item of projected.slice(0, Math.max(cut, 0))) {
		const entry = item.sourceEntry;
		const message = entry?.type === "message" ? entry.message : undefined;
		if (message?.role === "assistant" && Array.isArray(message.content)) {
			for (const part of message.content) if (part?.type === "toolCall") calls.set(part.id, part.arguments);
			continue;
		}
		// Failures keep their diagnostics; an entry edited or omitted earlier is left alone.
		if (message?.role !== "toolResult" || message.toolName === "tool_output" || message.isError || edited.has(entry.id) || item.messages.length !== 1) continue;
		const blocks = textBlocks(message);
		if (!blocks) continue;
		const bytes = blocks.reduce((sum, text) => sum + bytesOf(text), 0);
		if (bytes < limits.minBytes) continue;
		const lines = blocks.join("\n").split("\n").length;
		const content = receipt(entry.id, message.toolName, calls.get(message.toolCallId), bytes, lines);
		if (!worthReplacing(bytes, bytesOf(content), limits.minSaved)) continue;
		drafts.push({ type: "context_edit", targetId: entry.id, replacement: { content } });
	}
	return drafts;
}

// Same classifier as the worker gate; TP_SENSITIVE_READ_BIN lets tests stub it.
function sensitiveTarget(toolName: unknown, input: unknown): string | undefined {
	const bin = process.env.TP_SENSITIVE_READ_BIN || fileURLToPath(new URL("../../bin/tp-sensitive-read", import.meta.url));
	try {
		const decision = JSON.parse(execFileSync(bin, [], {
			input: JSON.stringify({ tool_name: toolName, tool_input: input ?? {} }), encoding: "utf8", timeout: 5000,
		}));
		return decision.sensitive ? String(decision.target ?? "a sensitive path") : undefined;
	} catch {
		return "an unclassifiable source"; // fail closed
	}
}

// In redaction "on" mode sensitive.ts redacts every tool result, tool_output rereads included, so no
// confirm is needed. Any other answer, or none, keeps the confirm. TP_REDACT_BIN lets tests stub it.
function redactionMode(): string {
	const bin = process.env.TP_REDACT_BIN || fileURLToPath(new URL("../../bin/tp-redact", import.meta.url));
	try { return execFileSync(bin, ["--mode"], { encoding: "utf8", timeout: 5000 }).trim(); }
	catch { return "unknown"; }
}

export default function (pi: ExtensionAPI) {
	// Workers and legacy strict sessions keep their full context.
	if (process.env.TP_TASK_DIR || process.env.TP_MODE === "strict") return;
	let redactedAtSource: boolean | undefined; // read once per session, like sensitive.ts

	pi.on("turn_end", (event, ctx) => {
		// Nested helpers (ask_local) restrict --tools; never offload where retrieval is unavailable.
		if (!pi.getActiveTools().includes("tool_output")) return;
		const done: any = event.message;
		// A failed or aborted response is not evidence that the model used earlier results.
		if (done?.role !== "assistant" || !["stop", "toolUse", "length"].includes(done.stopReason)) return;
		const edited = new Set<string>();
		for (const entry of ctx.sessionManager.getBranch()) if (entry.type === "context_edit") edited.add(entry.targetId);
		for (const draft of event.entries) if (draft.type === "context_edit") edited.add(draft.targetId);
		const drafts = plan(ctx.sessionManager.buildSessionProjection().entries, edited, event.messageEntryId);
		if (drafts.length) return { entries: [...event.entries, ...drafts] };
	});

	pi.registerTool({
		name: "tool_output",
		label: "Stored tool output",
		description: "Reread a bounded slice of an earlier tool result on this conversation branch, including one replaced by an offload receipt. Use the entry id from the receipt; page with offset.",
		parameters: Type.Object({
			entry: Type.String({ description: "Session entry id from the receipt" }),
			offset: Type.Optional(Type.Integer({ minimum: 0, description: "Character offset, default 0" })),
			limit: Type.Optional(Type.Integer({ minimum: 1, maximum: SLICE_MAX, description: `Characters to return, default ${SLICE_DEFAULT}` })),
		}),
		async execute(_id, p, _signal, _onUpdate, ctx) {
			const id = clean(p.entry, 64);
			const entry: any = ctx.sessionManager.getBranch().find((item) => item.id === p.entry);
			const blocks = entry?.type === "message" && entry.message.role === "toolResult" ? textBlocks(entry.message) : undefined;
			if (!blocks) throw new Error(`No text tool result with entry id ${id} on this branch.`);
			// Replaying a credential-bearing read needs the same per-call authorization as the read.
			let call: any;
			for (const item of ctx.sessionManager.getBranch() as any[]) {
				if (item.type !== "message" || item.message.role !== "assistant" || !Array.isArray(item.message.content)) continue;
				call = item.message.content.find((part: any) => part?.type === "toolCall" && part.id === entry.message.toolCallId) ?? call;
			}
			redactedAtSource ??= redactionMode() === "on";
			const target = redactedAtSource ? undefined
				: call ? sensitiveTarget(call.name, call.arguments) : "an unpaired tool call"; // fail closed
			if (target) {
				if (!ctx.hasUI) throw new Error(`Rereading ${target} needs user authorization in the active tmux pane.`);
				const allowed = await ctx.ui.confirm("Reread sensitive tool output?", `Entry ${id} came from a read that may expose ${target}. Approve only if needed.`);
				if (!allowed) throw new Error(`User denied rereading ${target}.`);
			}
			const text = blocks.join("\n");
			const isHigh = (i: number) => (text.charCodeAt(i) & 0xfc00) === 0xd800;
			const isLow = (i: number) => (text.charCodeAt(i) & 0xfc00) === 0xdc00;
			let start = Math.min(Math.max(0, Math.trunc(p.offset ?? 0)), text.length);
			if (start > 0 && start < text.length && isLow(start)) start -= 1; // keep surrogate pairs whole
			const size = Math.min(Math.max(1, Math.trunc(p.limit ?? SLICE_DEFAULT)), SLICE_MAX);
			let end = Math.min(text.length, start + size);
			if (end < text.length && isHigh(end - 1)) end = end - 1 > start ? end - 1 : end + 1;
			const more = end < text.length ? `\n[more: tool_output(entry="${id}", offset=${end})]` : "";
			return {
				content: [{ type: "text" as const, text: `[entry ${id}: characters ${start}-${end} of ${text.length}]\n${text.slice(start, end)}${more}` }],
				details: { entry: id, start, end, total: text.length },
			};
		},
	});
}
