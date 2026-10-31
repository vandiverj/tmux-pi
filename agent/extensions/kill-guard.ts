/**
 * kill-guard — refuse pattern-based kills (pkill, killall, kill $(pgrep ...), xargs kill) in worker bash calls.
 *
 * The pi twin of claude/plugins/tmux-pi/hooks/kill-guard.sh. The parser is lib/kill_guard.py; both run it, so the two
 * guards cannot drift. It applies to worker sessions only (lib/kill_guard.py decides, from the launch environment).
 * Fails closed: if the checker cannot run or answers something unreadable, the bash call is blocked.
 */
import { execFileSync } from "node:child_process";
import { dirname, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
	const agentDir = process.env.PI_CODING_AGENT_DIR;
	if (!agentDir) return;
	const checker = join(dirname(agentDir), "lib", "kill_guard.py");
	pi.on("tool_call", (event) => {
		if (String(event.toolName).toLowerCase() !== "bash") return;
		try {
			const answer = JSON.parse(execFileSync("python3", [checker, "--pi"], {
				input: JSON.stringify({ tool_name: event.toolName, tool_input: event.input ?? {} }),
				encoding: "utf8", timeout: 5000,
			}));
			if (answer.deny === false) return;
			return { block: true, reason: typeof answer.reason === "string" ? answer.reason : "Kill guard denied this command." };
		} catch {
			return { block: true, reason: "Kill guard unavailable; this bash command cannot be verified." };
		}
	});
}
