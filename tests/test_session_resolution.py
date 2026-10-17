"""Session resolution without TMUX: TP_STATE_DIR selects the session, never a decoy legacy 'tmux-pi'."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai/gpt-6-sol"
REAL = "tmux-pi-0123456789ab-cdef01234567"

# Two sessions: the real workspace and a legacy 'tmux-pi' with its own state dir, main pane and cwd.
TMUX = '''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
 has-session)
   case "$3" in "=tmux-pi"|"=$FAKE_REAL") exit 0 ;; *) exit 1 ;; esac ;;
 list-sessions) printf 'tmux-pi\\n%s\\n' "$FAKE_REAL" ;;
 show-options)
   case "$4$5" in
     "=tmux-pi:@tp_state_dir") printf '%s\\n' "$FAKE_DECOY_STATE" ;;
     "=tmux-pi:@tp_main_pane") printf '%%decoy\\n' ;;
     "=$FAKE_REAL:@tp_state_dir") printf '%s\\n' "$FAKE_REAL_STATE" ;;
     "=$FAKE_REAL:@tp_main_pane") printf '%%main\\n' ;;
   esac ;;
 display-message)
   case "$4$5" in
     '%main#{pane_current_path}') printf '%s\\n' "$FAKE_REAL_CWD" ;;
     '%decoy#{pane_current_path}') printf '%s\\n' "$FAKE_DECOY_CWD" ;;
     '%main#S') printf '%s\\n' "$FAKE_REAL" ;;
     '%decoy#S') printf 'tmux-pi\\n' ;;
     *) exit 1 ;;
   esac ;;
 new-window) printf '%%fake\\n' ;;
esac
'''


class SessionResolutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.project = self.base / "project"
        self.state = self.project / ".tmux-pi" / "sessions" / "run"
        self.decoy_project = self.base / "legacy"
        self.decoy_state = self.decoy_project / ".tmux-pi"
        for path in (self.state / "tasks", self.decoy_state / "tasks"):
            path.mkdir(parents=True)
        # Same relative name in both projects; the decoy copy would trip tp-spawn's size limit.
        (self.project / "notes.md").write_text("real project notes")
        (self.decoy_project / "notes.md").write_text("x" * 70000)
        fake = self.base / "fake-bin"
        fake.mkdir()
        (fake / "tmux").write_text(TMUX)
        (fake / "tmux").chmod(0o755)
        self.log = self.base / "tmux.log"
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_WORKDIR": str(self.project),
                    "TP_MAIN_HARNESS": "claude", "FAKE_LOG": str(self.log), "FAKE_REAL": REAL,
                    "FAKE_REAL_STATE": str(self.state), "FAKE_DECOY_STATE": str(self.decoy_state),
                    "FAKE_REAL_CWD": str(self.project), "FAKE_DECOY_CWD": str(self.decoy_project),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        for name in ("TMUX", "TMUX_PANE", "TP_SESSION"):
            self.env.pop(name, None)

    def cli(self, script, *args, **updates):
        env = {**self.env, **updates}
        for name in [key for key, value in env.items() if value is None]:
            del env[name]
        return subprocess.run([str(ROOT / "bin" / script), *args], env=env, text=True, cwd=self.decoy_project,
                              capture_output=True, timeout=25)

    def tasks(self, state):
        return sorted(p.name for p in (state / "tasks").iterdir())

    def windows(self):
        if not self.log.exists():
            return []
        return [line for line in self.log.read_text().splitlines() if line.startswith("new-window")]

    def assert_nothing_spawned(self):
        self.assertEqual(self.tasks(self.state), [])
        self.assertEqual(self.tasks(self.decoy_state), [])
        self.assertEqual(self.windows(), [])

    def test_spawn_uses_state_dir_session_not_decoy(self):
        run = self.cli("tp-spawn", "--worker", "claude", "--context", "notes.md", "Read the notes")
        self.assertEqual(run.returncode, 0, run.stderr)
        ident = run.stdout.strip()
        self.assertEqual(self.tasks(self.state), [ident])
        self.assertEqual(self.tasks(self.decoy_state), [])
        task = self.state / "tasks" / ident
        self.assertEqual(json.loads((task / "meta.json").read_text())["workdir"], str(self.project))
        self.assertIn("real project notes", (task / "packet.md").read_text())
        window = self.windows()
        self.assertEqual(len(window), 1)
        self.assertIn(f"-t ={REAL}:", window[0])
        self.assertIn(f"-c {self.project}", window[0])

    def test_dispatch_then_status_agree_on_task(self):
        run = self.cli("tp-agent", "dispatch", "--role", "researcher", "--task", "Read the notes", "--worker", "pi",
                       "--model", MODEL, "--thinking", "medium", "--context", "notes.md")
        self.assertEqual(run.returncode, 0, run.stderr)
        item = json.loads(run.stdout)
        self.assertEqual(self.tasks(self.state), [item["id"]])
        self.assertEqual(self.tasks(self.decoy_state), [])
        self.assertIn(f"-t ={REAL}:", self.windows()[0])
        status = self.cli("tp-agent", "inspect", item["id"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn("unknown task", status.stderr)

    def test_explicit_session_with_other_state_dir_fails_before_spawn(self):
        for script, args in (("tp-spawn", ("--worker", "claude", "Task")),
                             ("tp-agent", ("dispatch", "--role", "coder", "--task", "Task", "--worker", "pi",
                                           "--model", MODEL, "--thinking", "medium"))):
            with self.subTest(script=script):
                run = self.cli(script, *args, TP_SESSION="tmux-pi")
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("not TP_STATE_DIR", run.stderr)
                self.assertIn(str(self.decoy_state), run.stderr)
                self.assert_nothing_spawned()

    def test_tmux_pane_in_other_session_fails_before_spawn(self):
        run = self.cli("tp-spawn", "--worker", "claude", "Task", TMUX="/tmp/fake,1,0", TMUX_PANE="%decoy")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not TP_STATE_DIR", run.stderr)
        self.assert_nothing_spawned()

    def test_state_dir_without_session_never_falls_back(self):
        orphan = self.base / "orphan"
        (orphan / "tasks").mkdir(parents=True)
        run = self.cli("tp-spawn", "--worker", "claude", "Task", TP_STATE_DIR=str(orphan))
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("no tmux-pi session uses TP_STATE_DIR", run.stderr)
        self.assert_nothing_spawned()
        self.assertEqual(self.tasks(orphan), [])

    def test_two_sessions_sharing_one_state_dir_need_an_explicit_choice(self):
        shared = {"FAKE_DECOY_STATE": str(self.state)}
        run = self.cli("tp-spawn", "--worker", "claude", "Task", **shared)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(f"2 tmux-pi sessions use TP_STATE_DIR '{self.state}'; set TP_SESSION to choose one", run.stderr)
        self.assert_nothing_spawned()
        for name in (REAL, "tmux-pi"):
            with self.subTest(session=name):
                run = self.cli("tp-spawn", "--worker", "claude", "Task", TP_SESSION=name, **shared)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn(f"-t ={name}:", self.windows()[-1])
        self.assertEqual(len(self.tasks(self.state)), 2)

    def test_symlinked_state_dir_variable_selects_the_same_session(self):
        link = self.base / "state-link"
        link.symlink_to(self.state, target_is_directory=True)
        for updates in ({}, {"TP_SESSION": REAL}, {"TMUX": "/tmp/fake,1,0", "TMUX_PANE": "%main"}):
            with self.subTest(updates=updates):
                run = self.cli("tp-spawn", "--worker", "claude", "Task", TP_STATE_DIR=str(link), **updates)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn(run.stdout.strip(), self.tasks(self.state))
                self.assertIn(f"-t ={REAL}:", self.windows()[-1])
        self.assertEqual(self.tasks(self.decoy_state), [])
        # The link does not widen the rule: another session is still refused.
        run = self.cli("tp-spawn", "--worker", "claude", "Task", TP_STATE_DIR=str(link), TP_SESSION="tmux-pi")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("not TP_STATE_DIR", run.stderr)
        self.assertEqual(len(self.tasks(self.state)), 3)

    def test_matching_tmux_pane_and_explicit_session_resolve_same_session(self):
        for updates in ({"TMUX": "/tmp/fake,1,0", "TMUX_PANE": "%main"}, {"TP_SESSION": REAL}):
            with self.subTest(updates=updates):
                run = self.cli("tp-spawn", "--worker", "claude", "Task", **updates)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn(f"-t ={REAL}:", self.windows()[-1])
        self.assertEqual(self.tasks(self.decoy_state), [])

    def test_other_session_commands_follow_state_dir(self):
        ident = self.cli("tp-spawn", "--worker", "claude", "--name", "probe", "Task").stdout.strip()
        listed = self.cli("tp-list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(ident, listed.stdout)
        for script, args in (("tp-list", ()), ("tp-view", (ident,)), ("tp-kill", (ident,)), ("tp-clean", ()),
                             ("tp-refresh-status", ()), ("tp-status", ()), ("tp-reset", ()), ("tp-exit", ()),
                             ("tp-review", (ident,))):
            with self.subTest(script=script):
                self.log.unlink(missing_ok=True)
                run = self.cli(script, *args, TP_SESSION="tmux-pi")
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("not TP_STATE_DIR", run.stderr)
                # Only read-only lookups reached tmux before the refusal.
                verbs = {line.split()[0] for line in self.log.read_text().splitlines()}
                self.assertLessEqual(verbs, {"has-session", "show-options"})

    def test_legacy_default_without_state_dir_is_unchanged(self):
        run = self.cli("tp-spawn", "--worker", "claude", "Task", TP_STATE_DIR=None)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.tasks(self.decoy_state), [run.stdout.strip()])
        self.assertIn("-t =tmux-pi:", self.windows()[0])


if __name__ == "__main__":
    unittest.main()
