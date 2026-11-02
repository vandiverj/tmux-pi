"""CX4 wiring: the pi extension and the MCP server carry the same five tools, load only when TP_CTX_TOOLS=1, open no socket and read no
files themselves, and give the same text as `tp-ctx` for the same input. Stub navigators (tests/fixtures/ctx_surface/fake_navs.py);
no model, no network. The pi extension runs under node against the installed pi package and is skipped when there is none."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "ctx_surface"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lib"))
from test_ctx_surface import clean_env, copy_project  # noqa: E402
from test_docs_wiring import CASES, GOLDEN, LauncherCase  # noqa: E402  (the stub pi/claude launcher harness and its golden argv)

NODE = shutil.which("node")
TOOLS = ["ctx_brief", "ctx_symbol", "ctx_relations", "ctx_knowledge", "ctx_note"]
ORDERS = "apps/launchpad/api/src/orders.ts"
SCHEMA_BLOCK = re.compile(r"// <schema>\nconst SCHEMA = (\{.*\});\n// </schema>", re.S)


def pi_package():
    explicit = os.environ.get("PI_CODING_AGENT_PACKAGE")
    if explicit:
        return Path(explicit)
    pi = shutil.which("pi")
    return Path(pi).resolve().parents[2] if pi else None


class Workspace(unittest.TestCase):
    """A project with the fixture sources, a task directory, and an environment that points tp-ctx at the fake navigators."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = copy_project(self.tmp)
        self.task = self.tmp / "task"
        self.task.mkdir()

    def env(self, task=True, tools=True, **extra):
        env = clean_env({"TP_CTX_TODAY": "2026-10-31", "TP_CTX_NAV_MODULES": "fake_navs", "PYTHONPATH": str(FIX), **extra})
        if task:
            env["TP_TASK_DIR"] = str(self.task)
        if tools:
            env["TP_CTX_TOOLS"] = "1"
        return env

    def tp_ctx(self, tool, args, **kw):
        done = subprocess.run([str(ROOT / "bin" / "tp-ctx"), "--json", tool, "--input", json.dumps(args)], cwd=self.project, env=self.env(**kw),
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        return json.loads(done.stdout)["text"] if done.stdout else None

    def log(self):
        path = self.task / "ctx-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def expected_fields(tool, schema):
    """The facts about each argument both wrappers must state, from the engine's schema."""
    out = {}
    for name, f in schema["tools"][tool]["fields"].items():
        entry = {"required": bool(f.get("required"))}
        for key in ("type", "enum", "description"):
            if key in f:
                entry[key] = f[key]
        out[name] = entry
    return out


def stated_fields(schema_object):
    """The same facts read back from a JSON schema object as a wrapper emitted it."""
    out = {}
    required = set(schema_object.get("required", []))
    for name, prop in schema_object["properties"].items():
        entry = {"required": name in required, "type": prop["type"], "description": prop["description"]}
        if "enum" in prop:
            entry["enum"] = prop["enum"]
        out[name] = entry
    return out


class SchemaConsistency(Workspace):
    def test_both_wrappers_embed_exactly_what_tp_ctx_prints_as_its_schema(self):
        printed = json.loads(self.tp_ctx("ctx_schema", {}))
        self.assertEqual(sorted(printed["tools"]), sorted(TOOLS))
        for name in ("agent/extensions/ctx.ts", "claude/mcp/ctx.mjs"):
            block = SCHEMA_BLOCK.search((ROOT / name).read_text())
            self.assertIsNotNone(block, name)
            self.assertEqual(json.loads(block.group(1)), printed, name)

    def test_the_limits_the_schema_states_are_the_limits_the_engine_enforces(self):
        printed = json.loads(self.tp_ctx("ctx_schema", {}))
        from ctxengine import surface
        self.assertEqual(printed, surface.schema())
        for tool, spec in surface.SPECS.items():
            for name, field in spec.items():
                if "max" in field and field["type"] == "integer":
                    _, over = surface.validate(tool, {**{k: "x" for k, f in spec.items() if f.get("required")}, name: field["max"] + 1})
                    self.assertTrue(any(f"{name} must be a whole number from {field['min']} to {field['max']}" in p for p in over), (tool, name))
                    _, within = surface.validate(tool, {name: field["max"]})
                    self.assertFalse(any(p.startswith(f"{name} ") for p in within), (tool, name))

    def test_the_config_names_one_server_and_the_default_config_does_not(self):
        config = json.loads((ROOT / "claude" / "mcp.ctx.json").read_text())
        self.assertEqual(list(config["mcpServers"]), ["tmux-pi-ctx"])
        self.assertEqual(config["mcpServers"]["tmux-pi-ctx"]["args"], ["${TP_ROOT}/claude/mcp/ctx.mjs"])
        self.assertNotIn("tmux-pi-ctx", json.loads((ROOT / "claude" / "mcp.json").read_text())["mcpServers"])

    def test_neither_wrapper_opens_a_socket_or_reads_a_file(self):
        for name in ("agent/extensions/ctx.ts", "claude/mcp/ctx.mjs"):
            source = (ROOT / name).read_text()
            for forbidden in ("node:net", "node:http", "node:https", "node:dgram", "node:tls", "node:dns", "fetch(", "WebSocket", "readFile",
                              "node:fs", "createReadStream", "readFileSync"):
                self.assertNotIn(forbidden, source, (name, forbidden))

    def test_both_wrappers_check_the_switch_before_they_register_or_serve_anything(self):
        ts = (ROOT / "agent/extensions/ctx.ts").read_text()
        self.assertLess(ts.index('process.env.TP_CTX_TOOLS !== "1"'), ts.index("registerTool("))
        mjs = (ROOT / "claude/mcp/ctx.mjs").read_text()
        self.assertLess(mjs.index('process.env.TP_CTX_TOOLS !== "1"'), mjs.index("createInterface("))


class Launchers(LauncherCase):
    """ctx-enable: with TP_CTX_TOOLS=1 a task's tp-pi loads ctx.ts and a task's tp-claude adds mcp.ctx.json; everything else is the golden argv."""
    PI_EXT = "{ROOT}/agent/extensions/ctx.ts"
    CLAUDE_CFG = "{ROOT}/claude/mcp.ctx.json"

    def setUp(self):
        super().setUp()
        self.env.pop("TP_CTX_TOOLS", None)

    def with_ctx_pi(self, golden):
        argv = list(golden)
        last = max(i for i, a in enumerate(argv) if a == "--extension" and argv[i + 1].endswith(".ts"))
        argv[last + 2:last + 2] = ["--extension", self.PI_EXT]
        return argv

    def test_pi_loads_the_extension_for_a_task_only_when_the_switch_is_1(self):
        for case in CASES:
            with self.subTest(case=case):
                self.assertEqual(self.launch("pi", case), GOLDEN["pi"][case])
                got = self.launch("pi", case, TP_CTX_TOOLS="1")
                if "TP_TASK_DIR" in CASES[case] and case != "strict":
                    self.assertEqual(got, self.with_ctx_pi(GOLDEN["pi"][case]))
                    self.assertNotIn("--tools", got)  # a worker has no --tools list to extend
                else:
                    self.assertEqual(got, GOLDEN["pi"][case])

    def test_claude_adds_the_ctx_config_for_a_task_only_when_the_switch_is_1(self):
        for case in CASES:
            with self.subTest(case=case):
                self.assertEqual(self.launch("claude", case), GOLDEN["claude"][case])
                got = self.launch("claude", case, TP_CTX_TOOLS="1")
                if "TP_TASK_DIR" in CASES[case]:
                    want = list(GOLDEN["claude"][case])
                    at = want.index("{ROOT}/claude/mcp.json") + 1
                    want[at:at] = ["--mcp-config", self.CLAUDE_CFG]
                    self.assertEqual(got, want)
                    self.assertEqual(got.count("--strict-mcp-config"), 1)
                else:
                    self.assertEqual(got, GOLDEN["claude"][case])

    def test_only_the_exact_value_1_turns_it_on(self):
        for value in ("", "0", "true", "01", " 1", "1 "):
            self.assertNotIn(self.PI_EXT, self.launch("pi", "worker", TP_CTX_TOOLS=value), value)
            self.assertNotIn(self.CLAUDE_CFG, self.launch("claude", "worker", TP_CTX_TOOLS=value), value)

    def test_the_ctx_and_docs_switches_add_both(self):
        pi = self.launch("pi", "worker", TP_CTX_TOOLS="1", TP_DOCS_TOOLS="1")
        self.assertIn(self.PI_EXT, pi)
        self.assertIn("{ROOT}/agent/extensions/docs.ts", pi)
        claude = self.launch("claude", "worker", TP_CTX_TOOLS="1", TP_DOCS_TOOLS="1")
        self.assertEqual(claude.count("--mcp-config"), 3)

    def test_the_shared_claude_permissions_allow_exactly_the_five_ctx_tools(self):
        allow = json.loads((ROOT / "claude/settings.json").read_text())["permissions"]["allow"]
        self.assertEqual([a for a in allow if "tmux-pi-ctx" in a], [f"mcp__tmux-pi-ctx__{t}" for t in TOOLS])


@unittest.skipUnless(NODE, "node is not installed")
class McpServer(Workspace):
    SERVER = ROOT / "claude" / "mcp" / "ctx.mjs"

    def rpc(self, calls=(), listing=False, **env):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        if listing:
            requests.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for number, (name, arguments) in enumerate(calls, 3):
            requests.append({"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        run = subprocess.run([NODE, str(self.SERVER)], input="".join(json.dumps(r) + "\n" for r in requests), text=True, capture_output=True,
                             timeout=120, cwd=self.project, env=self.env(**env))
        return run, {m["id"]: m for m in map(json.loads, run.stdout.splitlines())}

    def test_without_the_switch_the_server_exits_and_offers_nothing(self):
        for value in (None, "", "0", "true", "yes", "01", " 1", "TRUE"):
            env = self.env(tools=False)
            if value is not None:
                env["TP_CTX_TOOLS"] = value
            run = subprocess.run([NODE, str(self.SERVER)], input=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n", text=True,
                                 capture_output=True, timeout=30, cwd=self.project, env=env)
            self.assertNotEqual(run.returncode, 0, value)
            self.assertEqual(run.stdout, "", value)
            self.assertIn("TP_CTX_TOOLS", run.stderr)
        self.assertEqual(self.log(), [])

    def test_it_lists_the_five_tools_with_the_engines_arguments(self):
        run, replies = self.rpc(listing=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        listed = {t["name"]: t for t in replies[2]["result"]["tools"]}
        self.assertEqual(sorted(listed), sorted(TOOLS))
        schema = json.loads(self.tp_ctx("ctx_schema", {}))
        for name, tool in listed.items():
            self.assertEqual(tool["description"], schema["tools"][name]["description"])
            self.assertEqual(stated_fields(tool["inputSchema"]), expected_fields(name, schema), name)
            self.assertNotIn("_meta", tool)

    def test_a_call_gives_the_same_text_as_tp_ctx_and_logs_one_line_each(self):
        calls = [("ctx_knowledge", {}), ("ctx_relations", {"target": ORDERS, "direction": "importers", "limit": 3}),
                 ("ctx_brief", {"paths": [ORDERS]}), ("ctx_symbol", {"name": "createOrder", "path": ORDERS})]
        run, replies = self.rpc(calls)
        self.assertEqual(run.returncode, 0, run.stderr)
        texts = [replies[n]["result"]["content"][0]["text"] for n in range(3, 3 + len(calls))]
        self.assertFalse(any(replies[n]["result"].get("isError") for n in range(3, 3 + len(calls))))
        self.assertEqual([entry["tool"] for entry in self.log()], [c[0] for c in calls])
        for (tool, args), text in zip(calls, texts):
            self.assertEqual(self.tp_ctx(tool, args), text, tool)
        self.assertIn("apps/launchpad/api/src/routes/r01.ts:3 -> " + ORDERS, texts[1])

    def test_a_note_answers_with_its_id_and_status_as_structured_content(self):
        """Review 1 F6: the candidate's id is data in the reply, not only prose."""
        note = {"kind": "lesson", "scope": ["apps/**"], "text": "Parse before use.", "evidence": [f"{ORDERS}:6"]}
        run, replies = self.rpc([("ctx_note", note), ("ctx_knowledge", {})], TP_TASK_ID="coder-x")
        written = replies[3]["result"]
        self.assertEqual(written["structuredContent"]["status"], "candidate")
        self.assertRegex(written["structuredContent"]["id"], r"^kn-\d{4}-\d\d-\d\d-")
        self.assertIn(written["structuredContent"]["id"], written["content"][0]["text"])
        self.assertNotIn("structuredContent", replies[4]["result"])

    def test_a_bad_argument_is_an_answer_with_a_next_step_not_an_error(self):
        run, replies = self.rpc([("ctx_relations", {"target": ORDERS, "direction": "sideways"})])
        result = replies[3]["result"]
        self.assertFalse(result.get("isError"))
        self.assertIn("direction must be one of", result["content"][0]["text"])
        self.assertIn("Next: ", result["content"][0]["text"])
        self.assertEqual([entry["status"] for entry in self.log()], ["invalid"])

    def test_a_refused_path_is_an_answer_and_is_logged_as_refused(self):
        run, replies = self.rpc([("ctx_brief", {"paths": [".env"]})])
        result = replies[3]["result"]
        self.assertFalse(result.get("isError"))
        self.assertIn("refused", result["content"][0]["text"])
        self.assertNotIn("do-not-read", result["content"][0]["text"])
        self.assertEqual([entry["status"] for entry in self.log()], ["refused"])

    def test_with_no_task_directory_every_tool_fails_and_returns_no_text_from_the_engine(self):
        run, replies = self.rpc([("ctx_knowledge", {}), ("ctx_brief", {"paths": [ORDERS]})], task=False)
        for number in (3, 4):
            result = replies[number]["result"]
            self.assertTrue(result["isError"])
            self.assertIn("TP_TASK_DIR", result["content"][0]["text"])
            self.assertNotIn("== ", result["content"][0]["text"])
        self.assertEqual(self.log(), [])

    def test_an_unknown_tool_and_an_unknown_method_are_refused(self):
        run, replies = self.rpc([("safe_read", {"path": "x"})])
        self.assertTrue(replies[3]["result"]["isError"])
        extra = subprocess.run([NODE, str(self.SERVER)], input=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}) + "\n", text=True,
                               capture_output=True, timeout=30, cwd=self.project, env=self.env())
        self.assertEqual(json.loads(extra.stdout)["error"]["code"], -32601)

    def test_requests_run_one_at_a_time_in_the_order_they_arrive(self):
        names = [f"Rule number {n}." for n in range(3)]
        calls = [("ctx_note", {"kind": "lesson", "scope": ["apps/**"], "text": text, "evidence": [f"{ORDERS}:6"]}) for text in names]
        run, replies = self.rpc(calls, TP_TASK_ID="coder-x")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual([entry["args"]["text"] for entry in self.log()], names)


@unittest.skipUnless(NODE and pi_package() and (pi_package() / "dist/index.js").exists(), "node or the installed pi package is not available")
class PiExtension(Workspace):
    def run_extension(self, calls, task=True, **extra):
        agent = self.tmp / "agent"
        (self.tmp / "bin").mkdir(exist_ok=True)
        agent.mkdir(exist_ok=True)
        link = self.tmp / "bin" / "tp-ctx"
        if not link.exists():
            link.symlink_to(ROOT / "bin" / "tp-ctx")
        spec = {"pkg": str(pi_package()), "root": str(ROOT), "project": str(self.project), "calls": [{"tool": t, "input": a} for t, a in calls]}
        env = self.env(task=task, tools=False, PI_CODING_AGENT_DIR=str(agent), CTX_RUNTIME_SPEC=json.dumps(spec), **extra)
        run = subprocess.run(["node", "--experimental-strip-types", str(FIX / "ctx_runtime.mjs")], cwd=ROOT, env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads(run.stdout.strip().splitlines()[-1])

    def test_every_tmux_pi_extension_loads_against_the_installed_pi(self):
        # An import the installed pi no longer provides (pi 1.5.0 dropped Type from pi-ai) fails here, not in a user's session.
        run = subprocess.run(["node", "--experimental-strip-types", str(FIX.parent / "load_extensions.mjs"), str(pi_package()), str(ROOT)],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        out = json.loads(run.stdout.strip().splitlines()[-1])
        self.assertEqual(out["failed"], {})
        self.assertEqual(out["loaded"], sorted(p.name for p in (ROOT / "agent/extensions").glob("*.ts")))

    def test_it_registers_nothing_without_the_switch_and_the_five_tools_with_it(self):
        out = self.run_extension([])
        self.assertEqual(out["off"], 0)
        self.assertEqual(sorted(t["name"] for t in out["registered"]), sorted(TOOLS))

    def test_the_registered_arguments_are_the_engines_arguments(self):
        schema = json.loads(self.tp_ctx("ctx_schema", {}))
        for tool in self.run_extension([])["registered"]:
            self.assertEqual(tool["description"], schema["tools"][tool["name"]]["description"])
            self.assertEqual(stated_fields(tool["parameters"]), expected_fields(tool["name"], schema), tool["name"])
        limit = next(t for t in self.run_extension([])["registered"] if t["name"] == "ctx_relations")["parameters"]["properties"]["limit"]
        self.assertEqual((limit["minimum"], limit["maximum"]), (1, 25))

    def test_pi_and_the_mcp_server_return_the_same_text_for_the_same_input(self):
        calls = [("ctx_knowledge", {}), ("ctx_relations", {"target": ORDERS, "direction": "importers", "limit": 3}),
                 ("ctx_brief", {"paths": [ORDERS]}), ("ctx_relations", {"target": ORDERS, "direction": "sideways"}),
                 ("ctx_brief", {"paths": [".env"]})]
        answers = self.run_extension(calls)["answers"]
        self.assertTrue(all(a.get("type") == "text" for a in answers), answers)
        for (tool, args), answer in zip(calls, answers):
            self.assertEqual(answer["text"], self.tp_ctx(tool, args), tool)
        self.assertIn("Next: ", answers[3]["text"])
        self.assertEqual([entry["tool"] for entry in self.log()][:5], [c[0] for c in calls])

    def test_a_note_returns_its_id_and_status_as_the_tool_details(self):
        """Review 1 F6, pi side."""
        note = {"kind": "lesson", "scope": ["apps/**"], "text": "Parse before use.", "evidence": [f"{ORDERS}:6"]}
        answers = self.run_extension([("ctx_note", note), ("ctx_knowledge", {})], TP_TASK_ID="coder-x")["answers"]
        self.assertEqual(answers[0]["details"]["status"], "candidate")
        self.assertRegex(answers[0]["details"]["id"], r"^kn-\d{4}-\d\d-\d\d-")
        self.assertIsNone(answers[1]["details"])

    def test_with_no_task_directory_the_tool_call_fails(self):
        out = self.run_extension([("ctx_knowledge", {})], task=False)
        self.assertIn("TP_TASK_DIR", out["answers"][0]["error"])
        self.assertEqual(self.log(), [])


if __name__ == "__main__":
    unittest.main()
