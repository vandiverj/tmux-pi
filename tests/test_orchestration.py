"""Shared task interface and sensitive-read boundary without launching models."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from sensitive_read import classify, hook_response


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        fake = self.root / "bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%fake\\n' ;;
 send-keys) exit 0 ;;
 *) exit 0 ;;
esac
''')
        tmux.chmod(0o755)
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture",
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)

    def cli(self, *args):
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True,
                             capture_output=True, timeout=25)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def dispatch(self, role="coder", task="Make a focused change", **extra):
        args = ["dispatch", "--role", role, "--task", task, "--worker", "pi",
                "--model", "openai-codex/gpt-6-sol", "--thinking", "medium",
                "--workdir", str(self.project), "--deliverable", "Report paths and checks"]
        for key, value in extra.items():
            args += ["--" + key.replace("_", "-"), value]
        return self.cli(*args)

    def test_role_packet_record_collect_and_index(self):
        item = self.dispatch()
        self.assertEqual(item["role"], "coder")
        self.assertEqual(item["status"], "starting")
        task = self.state / "tasks" / item["id"]
        packet = (task / "packet.md").read_text()
        self.assertIn("# Coder", packet)
        self.assertIn("RESULT_STATUS", packet)
        self.assertEqual(json.loads((task / "meta.json").read_text())["deliverable"], "Report paths and checks")
        (task / "state.json").write_text('{"state":"waiting"}')
        (task / "result.md").write_text("RESULT_STATUS: complete\nVerified one path.\n")
        collected = self.cli("collect", item["id"])
        self.assertEqual(collected["status"], "completed")
        self.assertEqual(collected["result_status"], "complete")
        self.assertIn("Verified one path", collected["result"])
        index = self.cli("index")
        self.assertEqual(index["recent"][-1]["id"], item["id"])
        self.assertEqual(index["recent"][-1]["status"], "completed")

    def test_dispatch_without_optional_deliverable_starts_worker(self):
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "dispatch", "--role", "coder",
                              "--task", "Check the parser", "--worker", "pi",
                              "--model", "openai-codex/gpt-6-sol", "--thinking", "low",
                              "--workdir", str(self.project)], env=self.env, text=True,
                             capture_output=True, timeout=25)
        self.assertEqual(run.returncode, 0, run.stderr)
        task = self.state / "tasks" / json.loads(run.stdout)["id"]
        self.assertTrue((task / "meta.json").exists())

    def test_dependency_queues_then_starts_with_result_pointer(self):
        first = self.dispatch(role="researcher", task="Find the relevant interface")
        second = self.dispatch(role="coder", task="Use the interface", after=first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertIn("waiting for", second["reason"])
        first_task = self.state / "tasks" / first["id"]
        (first_task / "state.json").write_text('{"state":"waiting"}')
        (first_task / "result.md").write_text("RESULT_STATUS: complete\nInterface found.\n")
        started = self.cli("tick")
        self.assertEqual(started["started"], [second["id"]])
        packet = (self.state / "tasks" / second["id"] / "packet.md").read_text()
        self.assertIn(f"Dependency {first['id']} result:", packet)
        self.assertFalse((self.state / "queue" / f"{second['id']}.json").exists())

    def test_total_slots_queue_without_blocking_direct_answers(self):
        active = [self.dispatch(task=f"Task {i}") for i in range(4)]
        self.assertTrue(all(item["status"] == "starting" for item in active))
        fifth = self.dispatch(task="Task 5")
        self.assertEqual(fifth["status"], "queued")
        self.assertIn("global worker slots", fifth["reason"])
        task = self.state / "tasks" / active[0]["id"]
        (task / "state.json").write_text('{"state":"waiting"}')
        (task / "result.md").write_text("RESULT_STATUS: complete")
        self.assertEqual(self.cli("tick")["started"], [fifth["id"]])

    def test_sensitive_context_is_omitted_without_stopping_work(self):
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        item = self.dispatch(context=".env")
        packet = (self.state / "tasks" / item["id"] / "packet.md").read_text()
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertIn("Sensitive context .env was omitted", packet)
        self.assertEqual(item["status"], "starting")

    def test_worker_request_is_visible_and_message_answers_it(self):
        item = self.dispatch()
        task = self.state / "tasks" / item["id"]
        asked = subprocess.run([str(ROOT / "bin/tp-agent"), "request", "--kind", "tool",
                                "--what", "browser", "--reason", "Need rendered evidence"],
                               env={**self.env, "TP_TASK_DIR": str(task)}, text=True,
                               capture_output=True, timeout=10)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertEqual(self.cli("inspect", item["id"])["status"], "needs_input")
        self.assertEqual(self.cli("index")["active"][0]["capability_request"]["what"], "browser")
        self.cli("message", item["id"], "--text", "Use the local HTML renderer.")
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "answered")

    def test_retry_links_prior_attempt_and_records_reason(self):
        first = self.dispatch()
        task = self.state / "tasks" / first["id"]
        (task / "state.json").write_text('{"state":"waiting"}')
        (task / "result.md").write_text("RESULT_STATUS: incomplete\nCould not find the parser.\n")
        second = self.cli("retry", first["id"], "--reason", "Look in the package directory",
                          "--worker", "pi", "--model", "openai-codex/gpt-6-sol")
        self.assertEqual(second["parent"], first["id"])
        packet = (self.state / "tasks" / second["id"] / "packet.md").read_text()
        self.assertIn("Look in the package directory", packet)
        self.assertIn(first["id"], packet)
        self.assertEqual(self.cli("inspect", first["id"])["result_status"], "incomplete")

    def test_incompatible_harness_model_is_rejected(self):
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "dispatch", "--role", "coder",
                              "--task", "Check config", "--worker", "claude",
                              "--model", "openai-codex/gpt-6-sol"], env=self.env,
                             text=True, capture_output=True, timeout=10)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("belongs to pi", run.stderr)


class SensitiveReadTests(unittest.TestCase):
    def test_known_files_and_common_shell_routes_prompt(self):
        cases = [
            ("Read", {"file_path": ".env"}),
            ("Read", {"file_path": "config/appsettings.Production.json"}),
            ("Grep", {"path": "/tmp/project/.aws/credentials"}),
            ("Read", {"file_path": "/tmp/home/.config/gcloud/application_default_credentials.json"}),
            ("Read", {"file_path": "/tmp/home/.config/gh/hosts.yml"}),
            ("Bash", {"command": "cat .env"}),
            ("Bash", {"command": "rg TOKEN .env.local"}),
            ("Bash", {"command": "rg --hidden password ."}),
            ("Bash", {"command": "python3 -c 'from pathlib import Path; print(Path(\".env\").read_text())'"}),
            ("Bash", {"command": "find . -type f -exec cat {} \\;"}),
            ("Bash", {"command": "find . -type f -print0 | xargs -0 cat"}),
        ]
        for tool, params in cases:
            with self.subTest(tool=tool, params=params):
                self.assertTrue(classify(tool, params)["sensitive"])
                response = hook_response({"tool_name": tool, "tool_input": params})
                self.assertEqual(response["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertFalse(classify("Bash", {"command": "rg handler src"})["sensitive"])
        self.assertFalse(classify("Read", {"file_path": "src/handler.py"})["sensitive"])

    def test_claude_hook_keeps_request_pending_until_matching_tool_settles(self):
        with tempfile.TemporaryDirectory() as folder:
            def hook(name, path):
                event = {"hook_event_name": name, "tool_name": "Read", "tool_input": {"file_path": path}}
                return subprocess.run([str(ROOT / "bin/tp-sensitive-read"), "--claude-hook"],
                                      input=json.dumps(event), text=True, capture_output=True,
                                      env={**os.environ, "TP_TASK_DIR": folder}, check=True)
            asked = hook("PreToolUse", ".env")
            self.assertEqual(json.loads(asked.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")
            request = Path(folder) / "permission-request.json"
            hook("PostToolUse", "README.md")
            self.assertEqual(json.loads(request.read_text())["status"], "pending")
            hook("PermissionDenied", ".env")
            self.assertEqual(json.loads(request.read_text())["status"], "denied")

    def test_claude_main_hook_asks_without_worker_task_directory(self):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Read",
                 "tool_input": {"file_path": ".env"}}
        env = dict(os.environ)
        env.pop("TP_TASK_DIR", None)
        run = subprocess.run([str(ROOT / "claude/plugins/tmux-pi/hooks/sensitive-read.sh")],
                             input=json.dumps(event), text=True, capture_output=True,
                             env=env, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
