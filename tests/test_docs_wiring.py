"""TP_DOCS_TOOLS wiring: bin/tp-pi and bin/tp-claude argv stay byte-identical when it is unset, gain the docs tools when it is 1;
the Claude MCP server and the pi extension carry the same two tools. Stub pi/claude binaries; no model, no network."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "localdocs"))
from project import FIXTURES, ROOT, ProjectCase  # noqa: E402

GOLDEN = json.loads((FIXTURES / "golden_argv.json").read_text())
NODE = shutil.which("node")
TOOLS = ["api_signature", "docs_search"]
CASES = {  # the same launch kinds golden_argv.json was captured for, on the launchers before this change
    "plain": {}, "worker": {"TP_TASK_DIR": "{TASK}"}, "main": {"TP_MODE": "orchestrator"},
    "worker_orchestrator": {"TP_MODE": "orchestrator", "TP_TASK_DIR": "{TASK}"}, "strict": {"TP_MODE": "strict"}, "legacy": {"TP_MODE": "legacy"},
}


class LauncherCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "pi").write_text('#!/usr/bin/env python3\nimport json, os, sys\njson.dump(sys.argv[1:], open(os.environ["REC"], "w"))\n')
        (fake / "claude").write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$REC"\n')
        for stub in fake.iterdir():
            stub.chmod(0o755)
        self.rec = self.tmp / "rec"
        self.env = {**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "REC": str(self.rec), "TP_PI_NODE": "",
                    "TP_CLAUDE_CONFIG_DIR": str(self.tmp / "cfg")}
        for name in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE", "TP_DOCS_TOOLS"):
            self.env.pop(name, None)

    def launch(self, which, case, **extra):
        env = {**self.env}
        for name, value in {**CASES[case], **extra}.items():
            env[name] = value.replace("{TASK}", str(self.tmp / "task"))
        self.rec.unlink(missing_ok=True)
        command = [str(ROOT / "bin/tp-pi")] if which == "pi" else [shutil.which("bash"), str(ROOT / "bin/tp-claude")]
        subprocess.run([*command, "--extra", "value"], env=env, check=True, capture_output=True, timeout=60)
        if which == "pi":
            argv = json.loads(self.rec.read_text())
        else:
            argv = self.rec.read_text().split("\0")[:-1]
            argv = ["<inline settings>" if (i and argv[i - 1] == "--settings" and a.startswith("{")) else a for i, a in enumerate(argv)]
        return [a.replace(str(ROOT), "{ROOT}") for a in argv]


def with_docs_pi(golden):
    """What tp-pi should launch with TP_DOCS_TOOLS=1: docs.ts after the last file extension, and the tool names on --tools."""
    argv = list(golden)
    if "--no-context-files" not in argv:  # strict mode hands pi over with only --no-skills and the kill guard
        return argv
    last = max(i for i, a in enumerate(argv) if a == "--extension" and argv[i + 1].endswith(".ts"))
    argv[last + 2:last + 2] = ["--extension", "{ROOT}/agent/extensions/docs.ts"]
    if "--tools" in argv:
        argv[argv.index("--tools") + 1] += "," + ",".join(TOOLS)
    return argv


def with_docs_claude(golden):
    argv = list(golden)
    at = argv.index("{ROOT}/claude/mcp.json") + 1
    argv[at:at] = ["--mcp-config", "{ROOT}/claude/mcp.docs.json"]
    return argv


class GoldenArgvTests(LauncherCase):
    def test_pi_argv_is_byte_identical_when_the_variable_is_unset(self):
        for case in CASES:
            self.assertEqual(self.launch("pi", case), GOLDEN["pi"][case], case)

    def test_claude_argv_is_byte_identical_when_the_variable_is_unset(self):
        for case in CASES:
            self.assertEqual(self.launch("claude", case), GOLDEN["claude"][case], case)

    def test_only_the_exact_value_1_turns_the_tools_on(self):
        for value in ("", "0", "true", "yes", "01", " 1", "1 ", "TRUE"):
            for case in ("worker", "main"):
                self.assertEqual(self.launch("pi", case, TP_DOCS_TOOLS=value), GOLDEN["pi"][case], (value, case))
                self.assertEqual(self.launch("claude", case, TP_DOCS_TOOLS=value), GOLDEN["claude"][case], (value, case))

    def test_pi_argv_includes_the_docs_extension_and_tools_when_set(self):
        for case in CASES:
            got = self.launch("pi", case, TP_DOCS_TOOLS="1")
            self.assertEqual(got, with_docs_pi(GOLDEN["pi"][case]), case)
            if case != "strict":
                self.assertIn("{ROOT}/agent/extensions/docs.ts", got, case)
                self.assertEqual(got.count("{ROOT}/agent/extensions/docs.ts"), 1)

    def test_pi_main_tool_list_gains_exactly_the_two_tools_and_stays_unique(self):
        argv = self.launch("pi", "main", TP_DOCS_TOOLS="1")
        tools = argv[argv.index("--tools") + 1].split(",")
        before = GOLDEN["pi"]["main"][GOLDEN["pi"]["main"].index("--tools") + 1].split(",")
        self.assertEqual(tools, before + TOOLS)
        self.assertEqual(len(tools), len(set(tools)))

    def test_a_worker_gets_the_extension_but_no_tools_flag(self):
        argv = self.launch("pi", "worker", TP_DOCS_TOOLS="1")
        self.assertIn("{ROOT}/agent/extensions/docs.ts", argv)
        self.assertNotIn("--tools", argv)

    def test_claude_argv_includes_the_second_mcp_config_when_set(self):
        for case in CASES:
            got = self.launch("claude", case, TP_DOCS_TOOLS="1")
            self.assertEqual(got, with_docs_claude(GOLDEN["claude"][case]), case)
            self.assertEqual(got.count("--strict-mcp-config"), 1, case)  # the strict flag still governs every config
            self.assertEqual(got.count("--mcp-config"), 2)

    def test_the_main_tool_list_in_pi_covers_every_tool_the_docs_extension_registers(self):
        source = (ROOT / "agent/extensions/docs.ts").read_text()
        names = re.findall(r'registerTool\(\s*\{\s*name:\s*["\']([^"\']+)["\']', source)
        self.assertEqual(len(names), source.count("registerTool("))
        self.assertEqual(names, TOOLS)
        listed = self.launch("pi", "main", TP_DOCS_TOOLS="1")
        self.assertEqual(set(names) - set(listed[listed.index("--tools") + 1].split(",")), set())

    def test_docs_server_is_in_its_own_config_and_not_in_the_default_one(self):
        docs = json.loads((ROOT / "claude/mcp.docs.json").read_text())
        self.assertEqual(list(docs["mcpServers"]), ["tmux-pi-docs"])
        self.assertEqual(docs["mcpServers"]["tmux-pi-docs"]["args"], ["${TP_ROOT}/claude/mcp/docs.mjs"])
        self.assertNotIn("tmux-pi-docs", json.loads((ROOT / "claude/mcp.json").read_text())["mcpServers"])

    def test_the_shared_claude_permissions_allow_exactly_the_two_docs_tools(self):
        allow = json.loads((ROOT / "claude/settings.json").read_text())["permissions"]["allow"]
        self.assertEqual([a for a in allow if "tmux-pi-docs" in a], [f"mcp__tmux-pi-docs__{t}" for t in TOOLS])


    def test_the_readme_names_what_the_shared_allowlist_pre_allows(self):
        readme = (ROOT / "README.md").read_text()
        line = next(l for l in readme.splitlines() if "are pre-allowed" in l or "is pre-allowed" in l)
        for tool in TOOLS:
            self.assertIn(f"`{tool}`", line)
        self.assertIn("tmux-pi-docs", line)
        self.assertIn("tmux-pi-safe", line)
        self.assertNotIn("Only the legacy subagent MCP tools are pre-allowed", readme)


@unittest.skipUnless(NODE, "node is not installed")
class McpServerTests(ProjectCase):
    SERVER = ROOT / "claude" / "mcp" / "docs.mjs"

    def rpc(self, *calls, task=True, listing=False):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
        if listing:
            requests.append({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for n, (name, arguments) in enumerate(calls, 3):
            requests.append({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        env = {k: v for k, v in os.environ.items() if k != "TP_TASK_DIR"}
        if task:
            env["TP_TASK_DIR"] = str(self.task)
        run = subprocess.run([NODE, str(self.SERVER)], input="".join(json.dumps(r) + "\n" for r in requests), text=True,
                             capture_output=True, timeout=60, cwd=self.project, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        replies = {m["id"]: m for m in map(json.loads, run.stdout.splitlines())}
        return replies

    def answers(self, replies, count):
        return [(replies[n]["result"]["content"][0]["text"], bool(replies[n]["result"].get("isError"))) for n in range(3, count + 3)]

    def log_lines(self):
        path = self.task / "docs-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_it_lists_exactly_the_two_read_only_tools(self):
        replies = self.rpc(listing=True)
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        self.assertEqual(sorted(tools), sorted(TOOLS))
        self.assertEqual(tools["api_signature"]["inputSchema"]["required"], ["package", "symbol"])
        self.assertEqual(tools["docs_search"]["inputSchema"]["required"], ["lib", "query"])
        for tool in tools.values():
            self.assertNotIn("_meta", tool)  # nothing here needs user consent: it only reads allowlisted files

    def test_api_signature_answers_from_the_fixture_package_and_logs_the_call(self):
        replies = self.rpc(("api_signature", {"package": "plainpkg", "symbol": "ConnectOptions"}),
                           ("api_signature", {"package": "esmpkg", "symbol": "parseDuration"}))
        (first, e1), (second, e2) = self.answers(replies, 2)
        self.assertFalse(e1 or e2)
        self.assertIn("plainpkg@1.2.3", first)
        self.assertIn("retries: number", first)
        self.assertIn("esmpkg@2.0.0 — dist/index.d.mts", second)
        lines = self.log_lines()
        self.assertEqual([(l["tool"], l["result"]) for l in lines], [("api_signature", "hit")] * 2)
        self.assertTrue(first.startswith("[provenance] source=node_modules:"), first)
        self.assertEqual([l["provenance"]["version"] for l in lines], ["1.2.3", "2.0.0"])

    def test_requests_are_run_one_at_a_time_in_the_order_they_arrive(self):
        calls = [("api_signature", {"package": "plainpkg", "symbol": "connect"}), ("api_signature", {"package": "plainpkg", "symbol": "nope"})] * 3
        self.rpc(*calls)
        self.assertEqual([l["result"] for l in self.log_lines()], ["hit", "miss"] * 3)

    def test_docs_search_answers_from_a_built_index_and_a_missing_index_is_a_plain_answer(self):
        (self.base / "docs").mkdir()
        (self.base / "docs" / "README.md").write_text("# plainpkg\n\n## Retries\n\nThe `retries` option controls repeated attempts.\n")
        built = subprocess.run([sys.executable, str(ROOT / "bin/tp-docs"), "--project", str(self.project), "build", "plainpkg", "--from",
                                str(self.base / "docs")], capture_output=True, text=True)
        self.assertEqual(built.returncode, 0, built.stderr)
        replies = self.rpc(("docs_search", {"lib": "plainpkg", "query": "repeated attempts", "limit": 3}),
                           ("docs_search", {"lib": "esmpkg", "query": "anything"}))
        (hit, e1), (miss, e2) = self.answers(replies, 2)
        self.assertIn("README.md:", hit)
        self.assertFalse(e1)
        self.assertIn("no docs index for esmpkg@2.0.0", miss)
        self.assertFalse(e2)
        self.assertEqual([l["result"] for l in self.log_lines()], ["hit", "miss"])

    def test_symbol_not_found_and_no_pinned_version_are_answers_and_are_logged(self):
        replies = self.rpc(("api_signature", {"package": "plainpkg", "symbol": "nope"}),
                           ("api_signature", {"package": "left-pad", "symbol": "pad"}))
        (a, _), (b, _) = self.answers(replies, 2)
        self.assertIn("nope not found", a)
        self.assertIn("no pinned version", b)
        self.assertEqual([l["result"] for l in self.log_lines()], ["miss", "miss"])

    def test_refused_requests_come_back_as_answers_and_leave_a_log_line(self):
        replies = self.rpc(("api_signature", {"package": "../../etc/passwd", "symbol": "x"}))
        (text, error), = self.answers(replies, 1)
        self.assertIn("refused", text)
        self.assertEqual([l["result"] for l in self.log_lines()], ["rejected"])

    def test_unset_task_dir_fails_closed_the_tool_errors_and_returns_no_declaration(self):
        replies = self.rpc(("api_signature", {"package": "plainpkg", "symbol": "connect"}),
                           ("docs_search", {"lib": "plainpkg", "query": "x"}), task=False)
        for text, error in self.answers(replies, 2):
            self.assertTrue(error)
            self.assertIn("TP_TASK_DIR", text)
            self.assertNotIn("export declare", text)
        self.assertEqual(self.log_lines(), [])

    def test_unknown_tool_and_method_are_errors(self):
        replies = self.rpc(("safe_read", {"path": "x"}))
        self.assertTrue(replies[3]["result"]["isError"])
        extra = subprocess.run([NODE, str(self.SERVER)], input=json.dumps({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}) + "\n",
                               text=True, capture_output=True, timeout=30, cwd=self.project)
        self.assertEqual(json.loads(extra.stdout)["error"]["code"], -32601)

    def test_the_server_source_opens_no_sockets_and_reads_no_files_itself(self):
        source = self.SERVER.read_text()
        for forbidden in ("node:net", "node:http", "node:https", "node:dgram", "node:tls", "node:dns", "fetch(", "WebSocket", "readFile", "node:fs"):
            self.assertNotIn(forbidden, source, forbidden)


class ExtensionContractTests(unittest.TestCase):
    """agent/extensions/docs.ts is the pi twin of docs.mjs; the runtime for it needs the installed pi package, so its shape is pinned here."""
    SOURCE = (ROOT / "agent/extensions/docs.ts").read_text()

    def test_registers_the_same_two_tools_with_the_same_descriptions_as_the_mcp_server(self):
        mjs = (ROOT / "claude/mcp/docs.mjs").read_text()
        for tool in TOOLS:
            self.assertIn(f'name: "{tool}"', self.SOURCE)
        descriptions = re.findall(r'"((?:Exact TypeScript|Search the local)[^"]*)"(?:\s*\+\s*"([^"]*)")*', self.SOURCE)
        self.assertEqual(len(descriptions), 2)
        # every line of each description in the twin appears verbatim in the server
        for chunk in re.findall(r'^\s*"([^"]{20,})"(?: \+)?,?$', self.SOURCE, re.M):
            if "npm package name" in chunk or chunk.startswith(("What to", "Max results", "Exported name")):
                continue
            self.assertIn(chunk, mjs)

    def test_it_runs_tp_docs_in_json_mode_and_never_touches_the_network_or_files(self):
        self.assertIn('"--json"', self.SOURCE)
        self.assertIn('"bin", "tp-docs"', self.SOURCE)
        for forbidden in ("node:net", "node:http", "node:https", "node:fs", "fetch(", "readFile", "WebSocket"):
            self.assertNotIn(forbidden, self.SOURCE, forbidden)

    def test_a_failed_call_throws_so_a_missing_log_returns_no_answer(self):
        self.assertRegex(self.SOURCE, r"if \(r\.code !== 0\) throw new Error\(")

    def test_the_extension_is_listed_by_the_pi_launcher_only_behind_the_variable(self):
        script = (ROOT / "bin/tp-pi").read_text()
        self.assertEqual(script.count("docs.ts"), 1)
        self.assertRegex(script, r'if \[\[ "\$\{TP_DOCS_TOOLS:-\}" == 1 \]\]; then\n\s+extensions\+=\("\$ROOT/agent/extensions/docs\.ts"\)')


class ExtensionRuntimeTests(ProjectCase):
    """agent/extensions/docs.ts loaded as pi loads an extension (the installed pi packages, real bin/tp-docs), no model."""

    def test_both_tools_answer_against_fixtures_with_logging_and_fail_closed_without_a_task_dir(self):
        package = os.environ.get("PI_CODING_AGENT_PACKAGE")
        if not package:
            pi = shutil.which("pi")
            package = str(Path(pi).resolve().parents[2]) if pi else ""
        if not package or not (Path(package) / "dist/index.js").exists() or not NODE:
            self.skipTest("installed pi package or node not found")
        sys.path.insert(0, str(ROOT / "lib"))
        from localdocs import index  # noqa: E402
        from project import install_deno_remote  # noqa: E402
        shutil.copytree(FIXTURES / "docs", self.base / "docs")
        index.build_for(self.project, "plainpkg", source=self.base / "docs")
        jsr = self.base / "jsr-project"
        jsr.mkdir()
        deno = self.base / "deno"
        install_deno_remote(deno)
        (jsr / "deno.lock").write_text(json.dumps({"version": "4", "specifiers": {"jsr:@fx/strs@1": "1.4.0"}, "jsr": {"@fx/strs@1.4.0": {}}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("TP_")}
        env.update(PI_CODING_AGENT_PACKAGE=package, DOCS_ROOT=str(ROOT), DOCS_PROJECT=str(self.project), DOCS_JSR_PROJECT=str(jsr),
                   DOCS_TASK_DIR=str(self.task), DENO_DIR=str(deno))
        run = subprocess.run([NODE, "--experimental-strip-types", str(FIXTURES / "docs_runtime.mjs")], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=240)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("docs extension runtime ok", run.stdout)


if __name__ == "__main__":
    unittest.main()
