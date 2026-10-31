"""Kill guard (lib/kill_guard.py): pattern-based kills are refused in worker sessions.

The table in tests/fixtures/kill_guard/cases.json lists every command that must be refused and every one that must pass.
Entry points are checked too: the Claude hook (a PreToolUse payload through kill-guard.sh), the pi mode, the worker/Main
switch, and the registration in hooks.json.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import kill_guard  # noqa: E402

CASES = json.loads((ROOT / "tests/fixtures/kill_guard/cases.json").read_text())
HOOK = ROOT / "claude/plugins/tmux-pi/hooks/kill-guard.sh"
CLI = ROOT / "lib/kill_guard.py"
WORKER = {"TP_TASK_DIR": "/tmp/tp-kill-guard-test-task"}


def run(argv, payload, env_extra, raw=None):
    env = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_TASK_ID", "TP_MODE")}
    env.update(env_extra)
    return subprocess.run(argv, input=raw if raw is not None else json.dumps(payload), capture_output=True, text=True,
                          env=env, timeout=30)


def event(command, tool="Bash"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "cwd": str(ROOT)}


class Table(unittest.TestCase):
    def test_refuse_table(self):
        for command in CASES["refuse"]:
            with self.subTest(command=command):
                reason = kill_guard.check(command)
                self.assertTrue(reason, "should be refused")
                self.assertIn("What to do instead", reason)
                self.assertIn("$!", reason)
                self.assertIn("Main", reason)

    def test_allow_table(self):
        for command in CASES["allow"]:
            with self.subTest(command=command):
                self.assertIsNone(kill_guard.check(command))

    def test_not_text_fails_closed(self):
        for value in (None, 5, ["pkill x"], {"a": 1}, "a\0b"):
            with self.subTest(value=value):
                self.assertTrue(kill_guard.check(value))

    def test_deep_nesting_fails_closed(self):
        self.assertTrue(kill_guard.check("echo " + "$(echo " * 40 + "hi" + ")" * 40))
        self.assertTrue(kill_guard.check("bash -c " + "'" * 1))


class Hook(unittest.TestCase):
    def test_worker_kill_is_denied_end_to_end(self):
        r = run([str(HOOK)], event("pkill -f 'run tests'"), WORKER)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("What to do instead", out["permissionDecisionReason"])

    def test_worker_allowed_command_prints_nothing(self):
        for command in ("kill 123", "echo \"pkill\"", "grep -n pkill file"):
            with self.subTest(command=command):
                r = run([str(HOOK)], event(command), WORKER)
                self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_other_tools_and_events_are_not_judged(self):
        self.assertEqual(run([str(HOOK)], event("pkill x", tool="Read"), WORKER).stdout, "")
        self.assertEqual(run([str(HOOK)], {**event("pkill x"), "hook_event_name": "PostToolUse"}, WORKER).stdout, "")

    def test_main_session_is_not_guarded_unless_asked(self):
        self.assertEqual(run([str(HOOK)], event("pkill x"), {}).stdout, "")
        r = run([sys.executable, str(CLI), "--claude-hook", "--all"], event("pkill x"), {})
        self.assertIn("deny", r.stdout)

    def test_strict_mode_and_task_id_count_as_worker(self):
        for env in ({"TP_MODE": "strict"}, {"TP_TASK_ID": "abc"}):
            with self.subTest(env=env):
                self.assertIn("deny", run([str(HOOK)], event("pkill x"), env).stdout)

    def test_unreadable_worker_event_fails_closed(self):
        for raw in ("not json", "[]", ""):
            with self.subTest(raw=raw):
                r = run([str(HOOK)], None, WORKER, raw=raw)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(r.stdout, "")

    def test_missing_or_odd_command_is_denied(self):
        for tool_input in (None, {}, {"command": 5}, "pkill x"):
            with self.subTest(tool_input=tool_input):
                r = run([str(HOOK)], {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input}, WORKER)
                self.assertIn("deny", r.stdout)

    def test_unparseable_command_is_denied(self):
        self.assertIn("cannot be parsed", run([str(HOOK)], event("echo \"x"), WORKER).stdout)


class PiMode(unittest.TestCase):
    def pi(self, command, tool="bash", env=WORKER):
        r = run([sys.executable, str(CLI), "--pi"], {"tool_name": tool, "tool_input": {"command": command}}, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_pi_denies_and_allows(self):
        denied = self.pi("pgrep foo | xargs kill")
        self.assertTrue(denied["deny"])
        self.assertIn("What to do instead", denied["reason"])
        self.assertEqual(self.pi("kill 123"), {"deny": False, "reason": None})
        self.assertEqual(self.pi("pkill x", tool="read"), {"deny": False, "reason": None})
        self.assertEqual(self.pi("pkill x", env={}), {"deny": False, "reason": None})


class Registration(unittest.TestCase):
    def test_hooks_json_registers_bash_only_after_trust_guard(self):
        pre = json.loads((ROOT / "claude/plugins/tmux-pi/hooks/hooks.json").read_text())["hooks"]["PreToolUse"]
        groups = [g for g in pre if any("kill-guard.sh" in h["command"] for h in g["hooks"])]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["matcher"], "Bash")
        self.assertGreater(pre.index(groups[0]), 0)
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_pi_extension_runs_the_same_module(self):
        text = (ROOT / "agent/extensions/kill-guard.ts").read_text()
        self.assertIn("kill_guard.py", text)
        self.assertIn("--pi", text)


if __name__ == "__main__":
    unittest.main()
