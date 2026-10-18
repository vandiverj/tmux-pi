"""Which main dispatched a task, and `tp-agent list --main`: the data the Claude main's notifier wakes on."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
MODEL = "openai/gpt-6-sol"


class MainNotifyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        sessions = self.project / ".tmux-pi" / "sessions"
        self.here = sessions / "aaaa"
        self.there = sessions / "bbbb"
        for state in (self.here, self.there):
            (state / "tasks").mkdir(parents=True)
        fake = self.root / "bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text("#!/bin/sh\ncase \"$1\" in\n show-options) printf '%s\\n' \"$TP_STATE_DIR\" ;;\n new-window) printf '%%fake\\\\n' ;;\n *) exit 0 ;;\nesac\n")
        tmux.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items() if key not in ("TMUX", "TP_TASK_DIR", "TP_MAIN_ID", "CLAUDE_CODE_SESSION_ID")}
        self.env.update(TP_STATE_DIR=str(self.here), TP_SESSION="fixture", TP_WORK_DIR=str(self.root / "work-store"),
                        PATH=str(fake) + os.pathsep + os.environ["PATH"])

    def cli(self, *args, state=None, **env):
        merged = {**self.env, **env, **({"TP_STATE_DIR": str(state)} if state else {})}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=merged, text=True, capture_output=True, timeout=25)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def dispatch(self, state=None, **env):
        return self.cli("dispatch", "--role", "coder", "--task", "Make a focused change", "--worker", "pi", "--model", MODEL,
                        "--thinking", "medium", "--workdir", str(self.project), "--deliverable", "Report", state=state, **env)

    def task(self, state, ident, main=None):
        """A task record by hand: only what list reads."""
        path = state / "tasks" / ident
        path.mkdir()
        (path / "meta.json").write_text(json.dumps({"id": ident, "role": "coder", "worker": "pi", "created": "2026-10-18T00:00:00Z"}))
        (path / "state.json").write_text('{"state":"waiting"}')
        (path / "dispatch.json").write_text(json.dumps({"context": [], "notes": [], **({"main": main} if main else {})}))
        return path

    def ids(self, *args, **kwargs):
        return {row["id"]: row for row in self.cli("list", *args, **kwargs)}

    def test_dispatch_records_the_claude_session_as_the_main(self):
        item = self.dispatch(CLAUDE_CODE_SESSION_ID="sess-1")
        self.assertEqual(json.loads((self.here / "tasks" / item["id"] / "dispatch.json").read_text())["main"], "sess-1")
        self.assertEqual(self.ids()[item["id"]]["main"], "sess-1")

    def test_tp_main_id_wins_and_a_bad_id_is_not_recorded(self):
        first = self.dispatch(CLAUDE_CODE_SESSION_ID="sess-1", TP_MAIN_ID="main-a")
        self.assertEqual(self.ids()[first["id"]]["main"], "main-a")
        second = self.dispatch(CLAUDE_CODE_SESSION_ID="has space")
        self.assertNotIn("main", self.ids()[second["id"]])

    def test_a_dispatch_with_no_identity_records_none(self):
        item = self.dispatch()
        self.assertNotIn("main", self.ids()[item["id"]])

    def test_a_worker_env_records_no_main(self):
        item = self.dispatch(TP_TASK_DIR=str(self.here / "tasks" / "x"), CLAUDE_CODE_SESSION_ID="sess-1")
        self.assertNotIn("main", self.ids()[item["id"]])

    def test_a_queued_task_keeps_its_main(self):
        queued = self.here / "queue"
        queued.mkdir()
        (queued / "coder-q1.json").write_text(json.dumps({
            "id": "coder-q1", "created": "2026-10-18T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.project),
            "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "",
            "after": [], "context": [], "notes": [], "tests": "affected", "main": "sess-1"}))
        self.assertEqual(self.ids()["coder-q1"].get("main"), "sess-1")

    def test_list_main_follows_the_main_into_another_state_directory(self):
        self.task(self.there, "coder-there", main="sess-1")
        row = self.ids("--main", "sess-1")["coder-there"]
        self.assertEqual(Path(row["state_dir"]).resolve(), self.there.resolve())
        self.assertEqual(row["status"], "paused")  # waiting with no result: a notable status
        self.assertNotIn("coder-there", self.ids())  # plain list stays one session

    def test_list_main_leaves_out_another_mains_tasks_in_both_directories(self):
        self.task(self.here, "coder-mine", main="sess-1")
        self.task(self.here, "coder-old")  # dispatched before main ids existed: still this session's
        self.task(self.here, "coder-theirs", main="sess-2")
        self.task(self.there, "coder-elsewhere-theirs", main="sess-2")
        self.task(self.there, "coder-elsewhere-unrecorded")
        found = self.ids("--main", "sess-1")
        self.assertEqual(set(found), {"coder-mine", "coder-old"})
        self.assertNotIn("state_dir", found["coder-mine"])

    def test_list_main_does_not_follow_a_linked_session_directory(self):
        outside = self.root / "outside"
        (outside / "tasks").mkdir(parents=True)
        self.task(outside, "coder-outside", main="sess-1")
        (self.project / ".tmux-pi" / "sessions" / "cccc").symlink_to(outside)
        self.assertNotIn("coder-outside", self.ids("--main", "sess-1"))


class LegacyEntryWarningTests(unittest.TestCase):
    def test_tp_warns_on_stderr_when_tp_main_is_set(self):
        env = {**os.environ, "TP_MAIN": "bogus"}
        run = subprocess.run([str(ROOT / "bin/tp")], env=env, text=True, capture_output=True, timeout=25, cwd="/")
        self.assertIn("tp: warning: TP_MAIN is the legacy entry point", run.stderr)
        self.assertEqual(run.stdout, "")

    def test_tp_is_silent_about_it_when_tp_main_is_unset(self):
        env = {key: value for key, value in os.environ.items() if key != "TP_MAIN"}
        run = subprocess.run([str(ROOT / "bin/tp"), "--bogus"], env=env, text=True, capture_output=True, timeout=25, cwd="/")
        self.assertNotIn("legacy entry point", run.stderr)


if __name__ == "__main__":
    unittest.main()
