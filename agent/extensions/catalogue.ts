/** Bounded, task-selected skill handles. The complete catalogue is never in the prompt. */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { join, dirname } from "node:path";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

type Entry = { id: string; description: string; status: string; authority: string; path: string; sha256?: string };

export default function (pi: ExtensionAPI) {
	// Workers receive only the selected full instructions in their packet.
	if (process.env.TP_TASK_DIR || !process.env.PI_CODING_AGENT_DIR) return;
	const root = dirname(process.env.PI_CODING_AGENT_DIR);
	const selector = join(root, "bin", "tp-skills");
	let request: string | undefined;
	const select = (text: string): Entry[] => {
		const output = execFileSync(selector, [], { input: text.slice(0, 8000), encoding: "utf8", timeout: 2000 });
		return JSON.parse(output) as Entry[];
	};
	const selectedPacket = (text: string): { selected: Entry[]; packet: string } => {
		const output = execFileSync(selector, ["--mode", "packet"], { input: text.slice(0, 8000), encoding: "utf8", timeout: 2000 });
		return JSON.parse(output) as { selected: Entry[]; packet: string };
	};

	pi.on("input", (event) => {
		// Select from the human's original request, not template/skill expansion or
		// retrieved material. Queued turns must be resubmitted through the R5 gate.
		request = event.streamingBehavior ? undefined : event.text;
	});
	pi.on("before_agent_start", (event) => {
		delete event.systemPromptOptions.sections.tmux_pi_selected_skills;
		if (!request) return;
		try {
			const chosen = selectedPacket(request);
			if (!chosen.selected.length) return;
			// Turn-local system section, not a read-tool result that would remain in
			// conversation history on subsequent unrelated requests. Pi removes
			// this section when the next request has no skill match.
			event.systemPromptOptions.sections.tmux_pi_selected_skills =
				"Only these task-matched skills are included on this turn. Instructions never grant tools, edit authority or external writes; verify local dependencies and task approval.\n\n" + chosen.packet;
			// Keep provenance out of model history and do not persist the user's text.
			pi.appendEntry("tmux-pi-skill-selection", {
				request_sha256: createHash("sha256").update(request).digest("hex"),
				selected: chosen.selected.map((s) => ({ id: s.id, status: s.status, sha256: s.sha256 })),
			});
		} catch {
			// Catalogue failure does not fall through to an unbounded/global inventory.
			delete event.systemPromptOptions.sections.tmux_pi_selected_skills;
		}
	});
	pi.on("agent_settled", () => { request = undefined; });

	// A single constant-size discovery escape hatch; no list-all operation.
	pi.registerTool({
		name: "tmux_pi_find_skill",
		label: "Find relevant skill",
		description: "When a task might need specialized guidance but no relevant handle was shown, find up to two matching skills by purpose or format. Returns only candidates, never instructions or authority; do not use to list the catalogue.",
		parameters: Type.Object({
			purpose: Type.String({ description: "Specific task and input/output formats, not 'list all'." }),
		}),
		async execute(_id, params) {
			const query = params.purpose.trim();
			if (query.length < 8 || query.length > 500 || /^(?:list|show|all|everything)\s+(?:all\s+)?(?:skills|capabilities)/i.test(query)) {
				return { content: [{ type: "text" as const, text: "Specify the task and input/output formats; the full catalogue is not exposed." }], details: undefined };
			}
			let matches: Entry[];
			try { matches = select(query); }
			catch { matches = []; }
			return {
				content: [{ type: "text" as const, text: matches.length
					? matches.map((s) => `${s.id} (${s.status}): ${s.description} No activation or authority granted; request this specific task/format in a new turn to load its instructions.`).join("\n")
					: "No task-matched instruction found. Ask for the needed format or proceed without a specialty skill." }],
				details: { ids: matches.map((s) => s.id) },
			};
		},
	});
}
