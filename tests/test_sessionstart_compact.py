"""The SessionStart(compact) command hook (claude/plugins/tmux-pi/hooks/sessionstart-compact.sh): after a compaction it arms recovery
and prints the task index as hook JSON (additionalContext). Orchestrator main only; fail-open. The real delivery needs a live
Claude session (see the final report); everything here runs the script itself against temporary stores and state directories."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import index_text  # noqa: E402
import work_checkpoint as cp  # noqa: E402

PLUGIN = ROOT / "claude" / "plugins" / "tmux-pi"
HOOK = PLUGIN / "hooks" / "sessionstart-compact.sh"
LOG = "hook-sessionstart-compact.log"
COMPACT = '{"hook_event_name":"SessionStart","source":"compact"}'


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.state = self.tmp / "project" / ".tmux-pi" / "run-one"
        (self.state / "tasks").mkdir(parents=True)
        (self.tmp / "work-config.json").write_text("{}")
        self.env = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_MODE", "TP_MAIN_HARNESS", "TMUX", "TP_STATE_DIR")}
        self.env.update(TP_WORK_DIR=str(self.tmp / "work"), TP_WORK_CONFIG=str(self.tmp / "work-config.json"), TP_STATE_DIR=str(self.state),
                        TP_ROOT=str(ROOT), TP_MODE="orchestrator")

    def tp(self, *args):
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def run_hook(self, stdin=COMPACT, hook=HOOK, **env):
        full = dict(self.env)
        for key, value in env.items():
            if value is None:
                full.pop(key, None)
            else:
                full[key] = value
        return subprocess.run([str(hook)], input=stdin, env=full, text=True, capture_output=True, timeout=60)

    def pending(self):
        return cp.marker(self.state)["pending"] if (self.state / cp.RECOVERY_DIR).exists() else False


class Delivers(Base):
    def test_an_orchestrator_main_gets_hook_json_with_the_index(self):
        (self.state / "session-objective.md").write_text("ship the loader\n")
        self.tp("work", "new", "Loader", "--id", "loader")
        self.tp("work", "attach", "loader")
        run = self.run_hook()
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        answer = json.loads(run.stdout)
        self.assertEqual(list(answer), ["hookSpecificOutput"])
        self.assertEqual(answer["hookSpecificOutput"]["hookEventName"], "SessionStart")
        text = answer["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(set(answer["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        self.assertTrue(text.startswith(index_text.HEADER + "\nobjective: ship the loader\n"))
        self.assertIn("- loader ", text)
        # The marker was set before the index was read, so the text says so (the first pending event is the attach).
        self.assertIn("recovery pending (workspace attached at ", text)
        # The text is what `tp-agent index --text` prints now, and what the shared renderer makes of the JSON.
        self.assertEqual(text + "\n", self.tp("index", "--no-write", "--text"))
        self.assertEqual(text, index_text.render(json.loads(self.tp("index", "--no-write"))))
        # The script prints tp-agent's finished hook output unchanged.
        self.assertEqual(run.stdout, self.tp("index", "--no-write", "--hook-json"))

    def test_controls_and_lone_surrogates_reach_the_main_as_the_renderers_give_them(self):
        (self.state / "session-objective.md").write_bytes(b"a\x00b\x1bc\n")
        task = self.state / "tasks" / "coder-1"
        task.mkdir()
        (task / "meta.json").write_text('{"id": "coder-1", "role": "co\\ud800der", "harness": "pi", "task": "t", "created": "2026-10-01T00:00:00Z"}')
        run = self.run_hook()
        self.assertEqual((run.returncode, run.stderr), (0, ""))
        text = json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("\nobjective: abc\n", text)
        self.assertIn("coder-1 co\ufffdder", text)
        self.assertEqual(text, index_text.render(json.loads(self.tp("index", "--no-write"))))

    def test_the_text_carries_rows_of_the_session(self):
        task = self.state / "tasks" / "coder-1"
        task.mkdir()
        (task / "meta.json").write_text(json.dumps({"id": "coder-1", "role": "coder", "harness": "pi", "task": "t", "created": "2026-10-01T00:00:00Z"}))
        text = json.loads(self.run_hook().stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("coder-1", text)

    def test_it_arms_recovery_pending(self):
        self.assertFalse(self.pending())
        run = self.run_hook()
        self.assertEqual(run.returncode, 0)
        marker = cp.marker(self.state)
        self.assertEqual((marker["pending"], marker["reason"], marker["events_pending"]), (True, "session-start:compact", 1))
        # Autonomy reads the same marker.
        self.tp("work", "new", "Loader", "--id", "loader")
        self.assertTrue(json.loads(self.tp("work", "compacted", "--reason", "x"))["pending"])

    def test_the_gate_is_the_modules(self):
        for env in ({"TP_TASK_DIR": str(self.tmp)}, {"TP_TASK_DIR": ""}, {"TP_MODE": "legacy"}, {"TP_MODE": None}, {"TP_ROOT": None}, {"TP_MAIN_HARNESS": "pi"},
                    {"TP_MAIN_HARNESS": ""}):  # set but empty is not claude, as autonomy.env_problem reads it
            with self.subTest(env=env):
                run = self.run_hook(**env)
                self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
                self.assertFalse(self.pending())
        # TP_MAIN_HARNESS=claude (what autonomy.env_problem demands) and unset (tp-claude does not export it) both deliver.
        self.assertIn("hookSpecificOutput", self.run_hook(TP_MAIN_HARNESS="claude").stdout)
        self.assertIn("hookSpecificOutput", self.run_hook().stdout)

    def test_only_a_compaction_delivers(self):
        for source in ("startup", "resume", "clear"):
            with self.subTest(source=source):
                run = self.run_hook(json.dumps({"hook_event_name": "SessionStart", "source": source}))
                self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertFalse(self.pending())
        for stdin in ("", "not json"):  # the matcher already selected compact: no source named is not a reason to stay silent
            self.assertIn("hookSpecificOutput", self.run_hook(stdin).stdout)


class FailsOpen(Base):
    def fake_root(self, agent_script):
        """A copy of the hook where the plugin expects it, over a tp-agent that is `agent_script`, with the real lib/safe_write.py."""
        root = self.tmp / "fake"
        hook = root / "claude" / "plugins" / "tmux-pi" / "hooks" / HOOK.name
        hook.parent.mkdir(parents=True)
        shutil.copy(HOOK, hook)
        (root / "bin").mkdir()
        (root / "lib").mkdir()
        shutil.copy(ROOT / "lib" / "safe_write.py", root / "lib" / "safe_write.py")
        agent = root / "bin" / "tp-agent"
        agent.write_text("#!/usr/bin/env bash\n" + agent_script)
        agent.chmod(agent.stat().st_mode | stat.S_IXUSR)
        return hook

    def log(self):
        path = self.state / LOG
        return path.read_text() if path.exists() else ""

    def test_a_failing_tp_agent_prints_nothing_and_leaves_a_note(self):
        hook = self.fake_root('echo "boom: store unreadable" >&2\nexit 1\n')
        run = self.run_hook(hook=hook)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        lines = self.log().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("work compacted failed", lines[0])
        self.assertIn("index --hook-json failed (exit 1), nothing injected: boom: store unreadable", lines[1])
        self.assertRegex(lines[1], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ sessionstart-compact: ")
        self.assertEqual(stat.S_IMODE((self.state / LOG).stat().st_mode), 0o600)

    def test_an_empty_index_is_not_injected(self):
        hook = self.fake_root("exit 0\n")
        run = self.run_hook(hook=hook)
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertIn("index --hook-json failed (exit 0), nothing injected", self.log())

    def test_a_failed_marker_does_not_stop_the_index(self):
        hook = self.fake_root('[[ "$1" == work ]] && { echo "no state" >&2; exit 1; }\necho \'{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "the index text"}}\'\n')
        run = self.run_hook(hook=hook)
        self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["additionalContext"], "the index text")
        self.assertIn("work compacted failed (the index is still read): no state", self.log())

    def test_the_output_is_printed_byte_for_byte_never_through_a_shell_variable(self):
        # A shell variable would drop the NUL and the trailing newlines; the file the script prints keeps them.
        hook = self.fake_root("[[ \"$1\" == index ]] && printf 'x\\000y\\n\\n\\n'\nexit 0\n")
        run = subprocess.run([str(hook)], input=COMPACT.encode(), env=self.env, capture_output=True, timeout=60)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, b"x\x00y\n\n\n", b""))

    def test_the_hook_asks_for_the_finished_hook_output(self):
        hook = self.fake_root('[[ "$1" == index ]] && echo "$*" >&2 && exit 3\nexit 0\n')
        self.run_hook(hook=hook)
        self.assertIn("nothing injected: index --no-write --hook-json", self.log())

    def test_the_note_never_follows_a_symlink(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("keep")
        (self.state / LOG).symlink_to(victim)
        run = self.run_hook(hook=self.fake_root("exit 1\n"))
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertEqual(victim.read_text(), "keep")

    def test_no_state_directory_is_still_a_quiet_exit(self):
        run = self.run_hook(hook=self.fake_root("exit 1\n"), TP_STATE_DIR=str(self.tmp / "gone"))
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))

    def test_the_real_script_with_an_unreadable_work_store_still_exits_zero(self):
        (self.tmp / "work").write_text("a file where the store should be")
        run = self.run_hook()
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stderr, "")


class Registered(unittest.TestCase):
    def test_hooks_json_runs_it_for_a_compaction_only_with_a_short_timeout(self):
        hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"]
        mine = [g for g in hooks if any("sessionstart-compact.sh" in h["command"] for h in g["hooks"])]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["matcher"], "compact")
        self.assertEqual(mine[0]["hooks"], [{"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/sessionstart-compact.sh\"", "timeout": 15}])
        self.assertTrue(os.access(HOOK, os.X_OK))
        # report.sh still sees every SessionStart.
        self.assertTrue(any("report.sh" in h["command"] and "matcher" not in g for g in hooks for h in g["hooks"]))

    def test_the_module_no_longer_injects_after_a_compaction(self):
        source = (PLUGIN / "hooks" / "register.tsx").read_text()
        self.assertNotIn("on('classic.SessionStart'", source)
        self.assertNotIn("session-start:compact", source.replace("sessionstart-compact.sh", ""))


if __name__ == "__main__":
    unittest.main()
