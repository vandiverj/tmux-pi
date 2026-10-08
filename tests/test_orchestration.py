"""Shared task interface and sensitive-read boundary without launching models."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai-codex/gpt-6-sol"
WAITING = '{"state":"waiting"}'
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

    def raw(self, *args, env=None, timeout=25):
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env or self.env, text=True,
                              capture_output=True, timeout=timeout)

    def cli(self, *args):
        run = self.raw(*args)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def task_dir(self, item):
        return self.state / "tasks" / item["id"]

    def packet(self, item):
        return (self.task_dir(item) / "packet.md").read_text()

    def settle(self, item, result=None):
        task = self.task_dir(item)
        (task / "state.json").write_text(WAITING)
        if result is not None:
            (task / "result.md").write_text(result)
        return task

    def retry(self, item, reason="Again"):
        return self.cli("retry", item["id"], "--reason", reason, "--worker", "pi", "--model", MODEL)

    def retry_packet(self, item):
        return self.packet(self.retry(item))

    def dispatch(self, role="coder", task="Make a focused change", **extra):
        args = ["dispatch", "--role", role, "--task", task, "--worker", "pi",
                "--model", MODEL, "--thinking", "medium",
                "--workdir", str(self.project), "--deliverable", "Report paths and checks"]
        for key, value in extra.items():
            args += ["--" + key.replace("_", "-"), value]
        return self.cli(*args)

    def test_role_packet_record_collect_and_index(self):
        item = self.dispatch()
        self.assertEqual(item["role"], "coder")
        self.assertEqual(item["status"], "starting")
        task = self.task_dir(item)
        packet = self.packet(item)
        self.assertIn("# Coder", packet)
        self.assertIn("RESULT_STATUS", packet)
        self.assertEqual(json.loads((task / "meta.json").read_text())["deliverable"], "Report paths and checks")
        self.settle(item, "RESULT_STATUS: complete\nVerified one path.\n")
        collected = self.cli("collect", item["id"])
        self.assertEqual(collected["status"], "completed")
        self.assertEqual(collected["result_status"], "complete")
        self.assertIn("Verified one path", collected["result"])
        index = self.cli("index")
        self.assertEqual(index["recent"][-1]["id"], item["id"])
        self.assertEqual(index["recent"][-1]["status"], "completed")

    def test_reply_without_result_status_is_paused_and_not_collected(self):
        item = self.dispatch()
        task = self.settle(item, "Started a background sleep; waiting for it.\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        self.assertEqual(self.cli("index")["recent"][-1]["status"], "paused")
        index = self.cli("index")
        self.assertEqual(index["pending"], [])
        self.assertEqual([r["id"] for r in index["unstatused"]], [item["id"]])
        self.assertEqual(index["paused"], [])
        run = self.raw("collect", item["id"])
        self.assertEqual(run.returncode, 0, run.stderr)
        notice = f"{item['id']} replied without a result status (paused); nothing was collected."
        out = json.loads(run.stdout)  # stdout stays pure JSON
        self.assertEqual((out["status"], out["notice"]), ("paused", notice))
        self.assertEqual(next(iter(out)), "notice")
        self.assertIn(notice, run.stderr)
        self.assertFalse((task / "collected.json").exists())
        # A later reply that carries a status settles it.
        (task / "result.md").write_text("RESULT_STATUS: complete\nDone.\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        # A clean exit with an unstatused reply is paused too; with a status it settles.
        (task / "exit").write_text("0\n")
        (task / "result.md").write_text("no status here\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        (task / "result.md").write_text("RESULT_STATUS: blocked\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")
        # A dependent task does not start from a paused predecessor.
        (task / "result.md").write_text("no status here\n")
        run = self.raw("dispatch", "--role", "coder", "--task", "next", "--worker", "pi", "--model", MODEL,
                       "--thinking", "medium", "--workdir", str(self.project), "--after", item["id"])
        self.assertNotEqual(json.loads(run.stdout).get("status"), "starting")

    def test_paused_with_a_result_status_is_not_called_unstatused(self):
        item = self.dispatch()
        task = self.settle(item, "RESULT_STATUS: blocked\nOver budget.\n")
        state = json.loads((task / "state.json").read_text())
        (task / "state.json").write_text(json.dumps(dict(state, state="paused", updated="2020-01-01T00:00:00Z")))
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        # A paused predecessor never starts a dependent task, with or without a result status.
        dependent = self.raw("dispatch", "--role", "coder", "--task", "next", "--worker", "pi", "--model", MODEL,
                             "--thinking", "medium", "--workdir", str(self.project), "--after", item["id"])
        self.assertEqual(dependent.returncode, 0, dependent.stderr)
        self.assertNotEqual(json.loads(dependent.stdout).get("status"), "starting")
        run = self.raw("collect", item["id"])
        out = json.loads(run.stdout)
        self.assertEqual((out["status"], out["result_status"]), ("paused", "blocked"))
        self.assertEqual(out["notice"], f"{item['id']} is paused; nothing was collected.")
        self.assertNotIn("without a result status", run.stderr + run.stdout)
        index = self.cli("index")
        self.assertEqual(index["unstatused"], [])
        self.assertEqual([r["id"] for r in index["paused"]], [item["id"]])
        # still listed when 14 newer tasks push it out of the recent window (rows sort by second-resolution time)
        for _ in range(14):
            self.dispatch()
        index = self.cli("index")
        self.assertNotIn(item["id"], [r["id"] for r in index["recent"]])
        self.assertEqual([r["id"] for r in index["paused"]], [item["id"]])

    def test_raw_settled_state_without_a_result_status_is_paused(self):
        item = self.dispatch()
        task = self.settle(item, "Still going.\n")
        state = json.loads((task / "state.json").read_text())
        for raw in ("settled", "waiting"):
            (task / "state.json").write_text(json.dumps(dict(state, state=raw)))
            self.assertEqual(self.cli("inspect", item["id"])["status"], "paused", raw)
            self.assertNotIn("completed", self.raw("collect", item["id"]).stdout.split('"status"')[1][:20])
            self.assertFalse((task / "collected.json").exists())
        (task / "result.md").unlink()
        (task / "state.json").write_text(json.dumps(dict(state, state="settled")))
        self.assertEqual(self.cli("inspect", item["id"])["status"], "paused")
        (task / "result.md").write_text("RESULT_STATUS: complete\n")
        self.assertEqual(self.cli("inspect", item["id"])["status"], "settled")

    def test_dispatch_without_optional_deliverable_starts_worker(self):
        item = self.cli("dispatch", "--role", "coder", "--task", "Check the parser", "--worker", "pi",
                        "--model", MODEL, "--thinking", "low", "--workdir", str(self.project))
        self.assertTrue((self.task_dir(item) / "meta.json").exists())

    def test_dependency_queues_then_starts_with_result_pointer(self):
        first = self.dispatch(role="researcher", task="Find the relevant interface")
        second = self.dispatch(role="coder", task="Use the interface", after=first["id"])
        self.assertEqual(second["status"], "queued")
        self.assertIn("waiting for", second["reason"])
        self.settle(first, "RESULT_STATUS: complete\nInterface found.\n")
        started = self.cli("tick")
        self.assertEqual(started["started"], [second["id"]])
        packet = self.packet(second)
        self.assertIn(f"Dependency {first['id']} result:", packet)
        self.assertFalse((self.state / "queue" / f"{second['id']}.json").exists())

    def test_total_slots_queue_without_blocking_direct_answers(self):
        active = [self.dispatch(task=f"Task {i}") for i in range(4)]
        self.assertTrue(all(item["status"] == "starting" for item in active))
        fifth = self.dispatch(task="Task 5")
        self.assertEqual(fifth["status"], "queued")
        self.assertIn("global worker slots", fifth["reason"])
        self.settle(active[0], "RESULT_STATUS: complete")
        self.assertEqual(self.cli("tick")["started"], [fifth["id"]])

    def test_sensitive_context_is_omitted_without_stopping_work(self):
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        item = self.dispatch(context=".env")
        packet = self.packet(item)
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertIn("Sensitive context .env was omitted", packet)
        self.assertEqual(item["status"], "starting")

    def test_worker_request_is_visible_and_message_answers_it(self):
        item = self.dispatch()
        task = self.task_dir(item)
        asked = self.raw("request", "--kind", "tool", "--what", "browser", "--reason", "Need rendered evidence",
                         env={**self.env, "TP_TASK_DIR": str(task)}, timeout=10)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertEqual(self.cli("inspect", item["id"])["status"], "needs_input")
        self.assertEqual(self.cli("index")["active"][0]["capability_request"]["what"], "browser")
        self.cli("message", item["id"], "--text", "Use the local HTML renderer.")
        self.assertEqual(json.loads((task / "capability-request.json").read_text())["status"], "answered")

    def test_retry_links_prior_attempt_and_records_reason(self):
        first = self.dispatch()
        self.settle(first, "RESULT_STATUS: incomplete\nCould not find the parser.\n")
        second = self.retry(first, "Look in the package directory")
        self.assertEqual(second["parent"], first["id"])
        packet = self.packet(second)
        self.assertIn("Look in the package directory", packet)
        self.assertIn(first["id"], packet)
        self.assertEqual(self.cli("inspect", first["id"])["result_status"], "incomplete")

    def test_retry_preserves_context_and_notes_without_stale_grants(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        dep = self.dispatch(role="researcher", task="Dependency")
        first = self.dispatch(task="Use spec", after=dep["id"], context="spec.txt", note="Keep API stable")
        self.assertEqual(first["status"], "queued")
        self.settle(dep, "RESULT_STATUS: complete\n")
        self.cli("tick")
        sensitive = self.dispatch(task="Env", context=".env")
        self.settle(first, "RESULT_STATUS: incomplete\n")
        second = self.retry(first, "Try again")
        packet = self.packet(second)
        self.assertIn("SPEC_MARKER", packet)
        self.assertIn("Keep API stable", packet)
        self.assertIn("Prior packet:", packet)
        self.assertNotIn("Dependency %s result" % dep["id"], packet)
        # a retry of a retry does not accumulate retry notes
        self.settle(second)
        packet3 = self.retry_packet(second)
        self.assertEqual(packet3.count("Retry of "), 1)
        self.assertIn("SPEC_MARKER", packet3)
        # sensitive context stays omitted on retry
        self.settle(sensitive)
        retry = self.retry(sensitive)
        packet4 = self.packet(retry)
        self.assertNotIn("PRIVATE_TOKEN=example", packet4)
        # the omitted input is recorded as omitted, never as admitted, and is not handed back to spawn
        kept = json.loads((self.task_dir(sensitive) / "dispatch.json").read_text())
        self.assertEqual((kept["context"], kept["omitted_context"]), ([], [".env"]))
        self.assertNotIn("Sensitive context .env was omitted", packet4)
        self.assertIn("1 context input(s) omitted as sensitive in the prior attempt were not replayed", packet4)
        again = json.loads((self.task_dir(retry) / "dispatch.json").read_text())
        self.assertEqual((again["context"], again["omitted_context"]), ([], []))

    def settled(self, **extra):
        item = self.dispatch(**extra)
        return item, self.settle(item)

    def test_retry_does_not_replay_raw_sensitive_spec_from_older_record(self):
        (self.project / ".env").write_text("PRIVATE_TOKEN=example\n")
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context=".env")
        # a record written before admission was tracked lists the raw operator specs
        (task / "dispatch.json").write_text(json.dumps({"context": [".env", "spec.txt"], "notes": []}))
        packet = self.retry_packet(item)
        self.assertNotIn("PRIVATE_TOKEN=example", packet)
        self.assertNotIn("Sensitive context .env was omitted", packet)
        self.assertIn("SPEC_MARKER", packet)
        self.assertIn("1 context input(s) omitted as sensitive", packet)

    def test_retry_without_dispatch_record_points_at_prior_packet(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable")
        (task / "dispatch.json").unlink()
        packet = self.retry_packet(item)
        self.assertIn("has no dispatch record, so its selected context and notes were not replayed", packet)
        self.assertIn(f"Prior packet: {(task / 'packet.md').resolve()}", packet)
        self.assertNotIn("SPEC_MARKER", packet)

    def test_retry_with_malformed_dispatch_record_replays_nothing(self):
        (self.project / "spec.txt").write_text("SPEC_MARKER\n")
        item, task = self.settled(context="spec.txt", note="Keep API stable")
        for raw in ("[]", "null", "{not json", '{"context":null,"notes":[]}', '{"context":[],"notes":null}',
                    '{"context":["spec.txt",7],"notes":[]}', '{"context":"spec.txt","notes":[]}',
                    '{"context":["spec.txt"],"omitted_context":{},"notes":[]}'):
            with self.subTest(raw=raw):
                (task / "dispatch.json").write_text(raw)
                packet = self.retry_packet(item)
                self.assertIn("prior dispatch record is malformed", packet)
                self.assertNotIn("SPEC_MARKER", packet)
                self.assertNotIn("Keep API stable", packet)
                for path in (self.state / "tasks").iterdir():
                    (path / "state.json").write_text(WAITING)

    def test_retry_replays_more_than_twenty_inputs(self):
        args = ["dispatch", "--role", "coder", "--task", "Many inputs", "--worker", "pi",
                "--model", MODEL, "--workdir", str(self.project)]
        for i in range(21):
            (self.project / f"ctx{i}.txt").write_text(f"CTX_MARKER_{i:02d}\n")
            args += ["--context", f"ctx{i}.txt", "--note", f"NOTE_MARKER_{i:02d}"]
        item = self.cli(*args)
        self.settle(item)
        packet = self.retry_packet(item)
        for i in range(21):
            self.assertIn(f"CTX_MARKER_{i:02d}", packet)
            self.assertIn(f"NOTE_MARKER_{i:02d}", packet)

    def test_incompatible_harness_model_is_rejected(self):
        run = self.raw("dispatch", "--role", "coder", "--task", "Check config", "--worker", "claude",
                       "--model", MODEL, timeout=10)
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
                                      env={**os.environ, "TP_TASK_DIR": folder, "TP_REDACTION": "shadow"}, check=True)
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
        env = {**os.environ, "TP_REDACTION": "shadow"}  # in "on" mode tp-redact gates instead
        env.pop("TP_TASK_DIR", None)
        run = subprocess.run([str(ROOT / "claude/plugins/tmux-pi/hooks/sensitive-read.sh")],
                             input=json.dumps(event), text=True, capture_output=True,
                             env=env, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "ask")


if __name__ == "__main__":
    unittest.main()
