/**
 * dispatcher — Laya-directed routing for the main agent (plan M8, phase 1).
 *
 * Active only in the main agent: inside tmux and not in a tp-spawn worker. Every
 * user message (not a /command or !shell line) goes through `tp-route --dispatch`,
 * which returns a validated route, intent, review level, execution scope and
 * placement. Leading +here, +delegate and +review markers override placement or
 * review and are removed before the model sees the message.
 *
 *   placement main, pi route      switch the main model and thinking in place
 *   placement main, Claude route  stay on the home model (Claude runs as a
 *                                 foreground worker in phase 3)
 *   placement subagent            return to the home model and tell it to delegate
 *                                 with the routed worker, model and thinking
 *   Laya unavailable              change nothing; status "AUTO off: Laya unavailable"
 *
 * The home model is the default in agent/settings.json. A routed model whose
 * context window cannot hold the conversation is skipped for the home model.
 * `/auto off` turns routing off for this session; `/auto on` turns it back on.
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
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

function route(bin: string, text: string): Promise<Decision> {
	return new Promise((resolve, reject) => {
		const child = execFile(bin, ["--dispatch"], { timeout: 30_000 }, (error, stdout, stderr) => {
			if (error) return reject(new Error(String(stderr || error.message).trim()));
			try {
				resolve(JSON.parse(String(stdout)));
			} catch (parseError) {
				reject(parseError);
			}
		});
		child.stdin?.end(text);
	});
}

export default function (pi: ExtensionAPI) {
	if (process.env.TP_TASK_DIR || !process.env.TMUX || !process.env.PI_CODING_AGENT_DIR) return;
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	const root = dirname(agentDir);
	const tpRoute = join(root, "bin", "tp-route");
	let enabled = true;
	let note: string | undefined;

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
		const text = event.text.trim();
		if (!enabled || event.source === "extension" || !text || text.startsWith("/") || text.startsWith("!")) {
			return { action: "continue" };
		}
		let decision: Decision;
		try {
			decision = await route(tpRoute, text);
		} catch {
			ctx.ui.setStatus("auto", "AUTO off: Laya unavailable");
			return { action: "continue" };
		}
		const target = `${decision.model}/${decision.thinking}`;
		const tags = `${decision.route} · review ${decision.review}${decision.overrides.length ? ` · ${decision.overrides.join(", ")} set by marker` : ""}`;
		if (decision.placement === "main" && decision.worker === "pi") {
			const used = await switchTo(ctx, decision.model, decision.thinking);
			ctx.ui.setStatus("auto", `AUTO main · ${used} · ${tags}`);
		} else {
			const fallback = home();
			if (fallback) await switchTo(ctx, `${fallback.provider}/${fallback.id}`, fallback.thinking);
			if (decision.placement === "main") {
				ctx.ui.setStatus("auto", `AUTO main · home model (Claude route ${target} not yet in place) · ${tags}`);
			} else {
				note =
					`[tmux-pi AUTO] Laya places this request in a subagent: intent ${decision.intent}, scope ${decision.scope}, ` +
					`review ${decision.review}. Delegate it with spawn_subagent using worker ${decision.worker}, model ` +
					`${decision.model}, thinking ${decision.thinking}. Put the user's request and any constraints from this ` +
					"conversation in the task, and tell the subagent to ask if the intent is unclear in a way where a wrong " +
					"guess would change files or waste significant work, and otherwise to state its assumptions and proceed." +
					(decision.review === "required" ? " When it finishes, run review_subagent on it." : "");
				ctx.ui.setStatus("auto", `AUTO subagent · ${target} · ${tags}`);
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
