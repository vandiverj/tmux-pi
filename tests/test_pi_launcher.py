"""bin/tp-pi argv per launch kind, against a stub pi on PATH; no models or live sessions."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_env import outside_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = {"read", "bash", "edit", "write", "grep", "find", "ls", "codemode"}
EXPECTED = ("read,bash,edit,write,grep,find,ls,codemode,dispatch_role,task_index,task_collect,"
            "tmux_pi_find_skill,ask_local,tool_output,safe_read,safe_grep")
ASK_LOCAL = r'''
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const [tmp, pkg, ai, source, host] = process.argv.slice(2);
const { linkPiHost } = await import(pathToFileURL(host).href);
linkPiHost(tmp, pkg);
fs.writeFileSync(path.join(tmp, 'package.json'), '{"type":"module"}\n');
fs.copyFileSync(source, path.join(tmp, 'assistant.ts'));
const mod = await import(pathToFileURL(path.join(tmp, 'assistant.ts')).href);
const tools = {};
mod.default({ on() {}, registerTool(tool) { tools[tool.name] = tool; } });
const result = await tools.ask_local.execute('id', { task: 'find the parser' }, undefined, undefined, { cwd: tmp });
console.log(result.content[0].text);
'''


def pi_package():
    explicit = os.environ.get("PI_CODING_AGENT_PACKAGE")
    if explicit:
        return Path(explicit)
    pi = shutil.which("pi")
    if not pi:
        return None
    pi = Path(pi).resolve()
    # Either the package's own bin, or the installer layout <agent>/bin/pi beside <agent>/install/releases/<version>.
    releases = sorted(pi.parents[1].glob("install/releases/*/node_modules/@earendil-works/pi-coding-agent"))
    return next((path for path in (pi.parents[2], *reversed(releases)) if (path / "dist/index.js").exists()), None)


class PiLauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.fake = self.base / "bin"
        self.fake.mkdir()
        self.record = self.base / "argv.json"
        pi = self.fake / "pi"
        pi.write_text('#!/usr/bin/env python3\nimport json, os, sys\n'
                      'mode = os.environ.get("TP_MODE")\n'
                      'with open(os.environ["TEST_PI_ARGV"], "w") as f: json.dump({"argv": sys.argv[1:], "mode": mode}, f)\n'
                      'print("stub reply")\n')
        pi.chmod(0o755)
        self.env = {**outside_session(), "PATH": str(self.fake) + os.pathsep + os.environ["PATH"],
                    "TEST_PI_ARGV": str(self.record), "TP_PI_NODE": ""}
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE"):
            self.env.pop(key, None)

    def launched(self, *args, **extra):
        run = subprocess.run([str(ROOT / "bin/tp-pi"), *args], env={**self.env, **extra},
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(self.record.read_text())["argv"]

    def value(self, argv, flag):
        self.assertEqual(argv.count(flag), 1, argv)
        return argv[argv.index(flag) + 1]

    def extensions(self, argv):
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--extension"]

    def test_orchestrator_main_gets_codemode_no_mcp_and_every_main_tool(self):
        argv = self.launched("--extra", "value", TP_MODE="orchestrator")
        for flag in ("--no-skills", "--no-context-files", "--no-extensions", "--no-mcp"):
            self.assertEqual(argv.count(flag), 1, argv)
        self.assertIn("builtin:codemode", self.extensions(argv))
        self.assertNotIn("builtin:mcp", argv)
        self.assertEqual(self.value(argv, "--tools"), EXPECTED)
        self.assertEqual(len(EXPECTED.split(",")), 16)
        self.assertEqual(argv[-2:], ["--extra", "value"])

    def test_worker_plain_and_legacy_launches_get_no_additions(self):
        task = str(self.base / "task")
        for extra in ({"TP_TASK_DIR": task, "TP_MODE": "orchestrator"}, {"TP_TASK_DIR": task}, {},
                      {"TP_MODE": "legacy"}):
            argv = self.launched("--extra", **extra)
            for absent in ("builtin:codemode", "--no-mcp", "--tools"):
                self.assertNotIn(absent, argv, extra)
            self.assertEqual(argv[:3], ["--no-skills", "--no-context-files", "--no-extensions"], extra)
            self.assertTrue(all(Path(path).is_file() for path in self.extensions(argv)), extra)

    def test_strict_launch_adds_only_the_kill_guard(self):
        guard = ["--extension", str(ROOT / "agent/extensions/kill-guard.ts")]
        self.assertEqual(self.launched("--extra", TP_MODE="strict"), ["--no-skills", *guard, "--extra"])
        self.assertEqual(self.launched(TP_MODE="strict", TP_TASK_DIR=str(self.base / "task")), ["--no-skills", *guard])

    def test_caller_tools_flag_is_the_only_one_outside_the_orchestrator_main(self):
        argv = self.launched("--tools", "read,grep,find,ls")
        self.assertEqual(self.value(argv, "--tools"), "read,grep,find,ls")

    def test_every_tool_registered_by_a_main_extension_is_listed(self):
        argv = self.launched(TP_MODE="orchestrator")
        files = [Path(path) for path in self.extensions(argv) if not path.startswith("builtin:")]
        self.assertEqual({path.name for path in files}, {"reporter.ts", "sensitive.ts", "orchestration.ts",
                                                         "assistant.ts", "catalogue.ts", "offload.ts", "safe.ts",
                                                         "kill-guard.ts"})
        registered = set()
        for path in files:
            source = path.read_text()
            names = re.findall(r'registerTool\(\s*\{\s*name:\s*["\']([^"\']+)["\']', source)
            # A registration this pattern cannot read must fail here rather than pass unseen.
            self.assertEqual(len(names), source.count("registerTool("), path.name)
            registered.update(names)
        listed = self.value(argv, "--tools").split(",")
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(registered - set(listed), set(), "main tools missing from MAIN_TOOLS in bin/tp-pi")
        self.assertEqual(set(listed), registered | BUILTIN)

    def test_long_argv_never_reaches_python_and_the_model_alias_still_applies(self):
        # Endpoint security SIGKILLs a python3 whose argv passes ~1 KB: a stub python3 fails the launch step on a long argv.
        real = shutil.which("python3")
        stub = self.fake / "python3"
        stub.write_text('#!/bin/sh\n[ "${1##*/}" = pi ] || [ "$(printf %s "$*" | wc -c)" -lt 600 ] || { echo "python3 argv too long" >&2; exit 137; }\n'
                        'exec "' + real + '" "$@"\n')
        stub.chmod(0o755)
        local = self.base / "local.json"
        local.write_text(json.dumps({"provider_aliases": {"openai": "openai-codex"}}))
        long = "x y\n'\"$z " * 400
        args = ["--model", "openai/gpt-5", "--append-system-prompt", long, "--", "--model=openai/keep", long, ""]
        argv = self.launched(*args, TP_LOCAL_CONFIG=str(local))
        self.assertEqual(argv[-len(args):], ["--model", "openai-codex/gpt-5", *args[2:]])
        (self.base / "bad.json").write_text("{")
        bad = subprocess.run([str(ROOT / "bin/tp-pi"), "--append-system-prompt", long], capture_output=True, text=True,
                             env={**self.env, "TP_LOCAL_CONFIG": str(self.base / "bad.json")}, timeout=15)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("tp-pi:", bad.stderr)
        self.assertNotIn("launch step failed", bad.stderr)
        stub.write_text("#!/bin/sh\nkill -9 $$\n")
        dead = subprocess.run([str(ROOT / "bin/tp-pi")], env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(dead.returncode, 1)
        self.assertIn("tp-pi: launch step failed (status 137)", dead.stderr)

    def test_ask_local_child_is_not_an_orchestrator_main(self):
        package = pi_package()
        # pi-ai is nested under the package or installed beside it, depending on the install layout.
        ai = next((path for path in ((package / "node_modules/@earendil-works/pi-ai"), package.parent / "pi-ai")
                   if path.exists()), None) if package else None
        if not ai:
            self.skipTest("installed Pi package not found")
        # A scratch root whose bin/tp-pi is the real launcher: config/local.json is absent, so defaults apply.
        root = self.base / "root"
        (root / "bin").mkdir(parents=True)
        (root / "bin/tp-pi").symlink_to(ROOT / "bin/tp-pi")
        work = self.base / "node"
        work.mkdir()
        script = self.base / "ask_local.mjs"
        script.write_text(ASK_LOCAL)
        env = {**self.env, "TMUX": "fixture", "TP_MODE": "orchestrator", "PI_CODING_AGENT_DIR": str(root / "agent")}
        run = subprocess.run(["node", "--experimental-strip-types", str(script), str(work), str(package), str(ai),
                              str(ROOT / "agent/extensions/assistant.ts"), str(ROOT / "tests/fixtures/pi_host.mjs")],
                             env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(run.stdout.strip(), "stub reply")
        child = json.loads(self.record.read_text())
        self.assertIsNone(child["mode"])
        self.assertEqual(self.value(child["argv"], "--tools"), "read,grep,find,ls,safe_read,safe_grep")
        for absent in ("builtin:codemode", "--no-mcp"):
            self.assertNotIn(absent, child["argv"])


if __name__ == "__main__":
    unittest.main()
