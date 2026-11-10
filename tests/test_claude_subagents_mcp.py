"""Exercise claude/mcp/subagents.mjs over stdio with a synthetic env; no tmux, models or credentials."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "claude" / "mcp" / "subagents.mjs"
NODE = shutil.which("node")
# Never a live socket: tools/list must not touch tmux, and no test here calls an active tool.
FAKE_TMUX = "/nonexistent/tmux-pi-test.sock,1,0"
EXPECTED = {"spawn_subagent", "list_subagents", "send_to_subagent", "collect_result",
            "review_subagent", "find_skill", "get_skill", "kill_subagent"}


@unittest.skipUnless(NODE, "node is not installed")
class ClaudeSubagentsMcpExposureTests(unittest.TestCase):
    def rpc(self, *requests, **env):
        """Run the server with only PATH plus `env`, and return its replies keyed by id."""
        lines = "".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests)
        run = subprocess.run([NODE, str(SERVER)], input=lines, text=True, capture_output=True,
                             timeout=15, cwd=ROOT, env={"PATH": os.environ["PATH"], **env})
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {m["id"]: m for m in map(json.loads, run.stdout.splitlines())}
        self.assertEqual(set(replies), {r["id"] for r in requests if "id" in r}, run.stdout)
        return replies

    def tools(self, **env):
        replies = self.rpc({"id": 1, "method": "initialize", "params": {}},
                           {"method": "notifications/initialized"},
                           {"id": 2, "method": "tools/list"}, **env)
        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "tmux-pi-subagents")
        return replies[2]["result"]["tools"]

    def test_legacy_claude_main_in_tmux_exposes_the_tools(self):
        tools = self.tools(TMUX=FAKE_TMUX, TP_MAIN="claude")
        self.assertEqual({t["name"] for t in tools}, EXPECTED)
        for tool in tools:
            self.assertTrue(tool["description"], tool["name"])
            self.assertEqual(tool["inputSchema"]["type"], "object", tool["name"])

    def test_tool_exposure_by_environment(self):
        # get_skill (read-only) is in both mains; the worker-management tools only in the legacy main; a worker and a
        # process outside tmux get nothing. The orchestrator main is the env bin/tp-main sets (TP_MODE=orchestrator).
        legacy, both_mains = EXPECTED, {"get_skill"}
        cases = (
            ("orchestrator main", dict(TMUX=FAKE_TMUX, TP_MODE="orchestrator"), both_mains),
            ("orchestrator main with TP_MAIN", dict(TMUX=FAKE_TMUX, TP_MAIN="claude", TP_MODE="orchestrator"), both_mains),
            ("legacy main", dict(TMUX=FAKE_TMUX, TP_MAIN="claude"), legacy),
            ("non-orchestrator mode value", dict(TMUX=FAKE_TMUX, TP_MODE="worker"), legacy),
            ("task worker", dict(TMUX=FAKE_TMUX, TP_MAIN="claude", TP_TASK_DIR="/nonexistent/task"), set()),
            ("orchestrator-mode task worker", dict(TMUX=FAKE_TMUX, TP_MODE="orchestrator", TP_TASK_DIR="/nonexistent/task"), set()),
            ("outside tmux", dict(TP_MAIN="claude"), set()),
            ("orchestrator mode outside tmux", dict(TP_MODE="orchestrator"), set()),
            ("empty TMUX", dict(TP_MAIN="claude", TMUX=""), set()),
        )
        for label, env, expected in cases:
            with self.subTest(label):
                names = {t["name"] for t in self.tools(**env)}
                self.assertEqual(names, expected)

    def test_every_plugin_command_that_loads_a_skill_gets_its_body(self):
        # a Claude slash command (claude/plugins/tmux-pi/commands/<name>.md) says "call the `get_skill` tool with id `X`";
        # the command only works if the server exposes get_skill in the main it runs in and the enum and catalogue know X
        import re
        wanted = {}
        for path in sorted((ROOT / "claude/plugins/tmux-pi/commands").glob("*.md")):
            text = path.read_text()
            if "get_skill" in text:
                found = re.search(r"`get_skill` tool with id `([a-z0-9-]+)`", text)
                self.assertTrue(found, f"{path.name} names get_skill without an id")
                wanted[path.stem] = found.group(1)
        self.assertEqual(set(wanted), {"counterpoint", "make-qa", "product-fit", "threat-model",
                                       "product-design", "prepare-cases"})
        calls = [{"id": i, "method": "tools/call", "params": {"name": "get_skill", "arguments": {"id": ident}}}
                 for i, ident in enumerate(wanted.values(), start=10)]
        catalogue = {s["id"]: s for s in json.loads((ROOT / "config/skill-catalog.json").read_text())["skills"]}
        mains = (("orchestrator main", dict(TMUX=FAKE_TMUX, TP_MODE="orchestrator")),
                 ("legacy main", dict(TMUX=FAKE_TMUX, TP_MAIN="claude")))
        for label, env in mains:
            replies = self.rpc({"id": 1, "method": "initialize", "params": {}}, *calls, **env)
            for call in calls:
                ident = call["params"]["arguments"]["id"]
                with self.subTest(main=label, skill=ident):
                    result = replies[call["id"]]["result"]
                    self.assertFalse(result.get("isError"), result["content"][0]["text"])
                    self.assertIn((ROOT / catalogue[ident]["path"]).read_text(), result["content"][0]["text"])

    def test_hidden_tools_cannot_be_called(self):
        # Each call names a tool hidden in that environment; none may run (list_subagents would start bin/tp-list).
        cases = (("get_skill", {"id": "make-qa"}, {"TMUX": FAKE_TMUX, "TP_TASK_DIR": "/nonexistent/task"}),
                 ("get_skill", {"id": "make-qa"}, {"TMUX": FAKE_TMUX, "TP_MODE": "orchestrator", "TP_TASK_DIR": "/nonexistent/task"}),
                 ("get_skill", {"id": "make-qa"}, {}),
                 ("get_skill", {"id": "make-qa"}, {"TP_MODE": "orchestrator"}),
                 ("list_subagents", {}, {"TMUX": FAKE_TMUX, "TP_MODE": "orchestrator"}),
                 ("kill_subagent", {"id": "x"}, {"TMUX": FAKE_TMUX, "TP_MODE": "orchestrator"}),
                 ("find_skill", {"purpose": "write acceptance tests"}, {"TMUX": FAKE_TMUX, "TP_MODE": "orchestrator"}),
                 ("constructor", {}, {"TMUX": FAKE_TMUX, "TP_MAIN": "claude"}),
                 ("__proto__", {}, {"TMUX": FAKE_TMUX, "TP_MODE": "orchestrator"}))
        for name, args, env in cases:
            with self.subTest(tool=name, env=env):
                call = {"id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
                result = self.rpc(call, **env)[1]["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(result["content"][0]["text"], f"unknown tool {name}")


if __name__ == "__main__":
    unittest.main()
