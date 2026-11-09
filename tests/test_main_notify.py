"""Which main dispatched a task, and `tp-agent list --main`: the data the Claude main's notifier wakes on."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_tools import fake_tmux  # noqa: E402
from session_env import outside_session  # noqa: E402
MODEL = "openai/gpt-6.1-sol"


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
        fake_tmux(fake, "case \"$1\" in\n show-options) printf '%s\\n' \"$TP_STATE_DIR\" ;;\n new-window) printf '%%fake\\\\n' ;;\n *) exit 0 ;;\nesac\n")
        self.env = outside_session()
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
        answer = self.cli("list", *args, **kwargs)
        return {row["id"]: row for row in (answer["rows"] if isinstance(answer, dict) else answer)}

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

    def test_list_main_reports_the_rows_of_this_session_it_left_out(self):
        """The reproduction: two settled tasks in one state dir, dispatched under sess-1 and sess-2. The sess-1 row is listed; the
        other is not dropped without a trace but named, with its status and main, in other_main."""
        self.task(self.here, "coder-one", main="sess-1")
        self.task(self.here, "coder-two", main="sess-2")
        self.task(self.here, "coder-none")
        self.task(self.there, "coder-elsewhere", main="sess-2")  # another session's directory: not "this session"
        answer = self.cli("list", "--main", "sess-1")
        self.assertEqual({row["id"] for row in answer["rows"]}, {"coder-one", "coder-none"})
        self.assertEqual(answer["other_main"], {"count": 1, "rows": [{"id": "coder-two", "status": "paused", "main": "sess-2"}]})
        quiet = self.cli("list", "--main", "sess-2")
        self.assertEqual([row["id"] for row in quiet["other_main"]["rows"]], ["coder-one"])
        self.assertEqual(self.cli("list", "--main", "nobody")["other_main"]["count"], 2)
        self.assertIsInstance(self.cli("list"), list)  # plain list is unchanged

    def test_list_main_does_not_follow_a_linked_session_directory(self):
        outside = self.root / "outside"
        (outside / "tasks").mkdir(parents=True)
        self.task(outside, "coder-outside", main="sess-1")
        (self.project / ".tmux-pi" / "sessions" / "cccc").symlink_to(outside)
        self.assertNotIn("coder-outside", self.ids("--main", "sess-1"))


class MainLauncherIdentityTests(unittest.TestCase):
    """bin/tp-main gives each main a TP_MAIN_ID that every process it starts inherits, and keeps it in the session's state
    directory so a relaunch of the same main (--resume, /clear, a second tp-main in the pane) has the same one."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # tp-main finds its repo from its own location: a copy beside a stub tp-claude, and no real Claude.
        (self.root / "bin").mkdir()
        (self.root / "agent").mkdir()
        shutil.copy(ROOT / "bin/tp-main", self.root / "bin/tp-main")
        for name in ("orchestrator.md", "orchestrator-claude.md"):
            (self.root / "agent" / name).write_text("core\n")
        stub = self.root / "bin/tp-claude"
        stub.write_text('#!/usr/bin/env bash\nprintf \'%s\\n\' "${TP_MAIN_ID-unset}"\n')
        stub.chmod(0o755)
        self.state = self.root / "state"
        self.state.mkdir()

    def launch(self, state="default", **env):
        merged = {**outside_session(), "TP_MAIN_HARNESS": "claude", **env}
        if state is not None:
            merged["TP_STATE_DIR"] = str(self.state if state == "default" else state)
        run = subprocess.run([str(self.root / "bin/tp-main")], env=merged, text=True, capture_output=True, timeout=25, cwd=str(self.root))
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def test_the_launcher_gives_the_main_an_id_that_a_child_dispatch_records(self):
        fake = self.root / "fakebin"
        fake.mkdir()
        fake_tmux(fake, "case \"$1\" in\n show-options) printf '%s\\n' \"$TP_STATE_DIR\" ;;\n new-window) printf '%%fake\\n' ;;\n *) exit 0 ;;\nesac\n")
        project = self.root / "project"
        sessions = project / ".tmux-pi" / "sessions" / "aaaa"
        (sessions / "tasks").mkdir(parents=True)
        # The stub plays the harness's Bash tool: it runs tp-agent dispatch with the environment it was started with.
        stub = self.root / "bin/tp-claude"
        stub.write_text(f'#!/usr/bin/env bash\nexec "{ROOT}/bin/tp-agent" dispatch --role coder --task "Make a focused change" --worker pi '
                        f'--model {MODEL} --thinking medium --workdir "{project}" --deliverable Report\n')
        out = subprocess.run([str(self.root / "bin/tp-main")], text=True, capture_output=True, timeout=25, cwd=str(self.root), env={
            **outside_session(), "TP_MAIN_HARNESS": "claude", "TP_STATE_DIR": str(sessions), "TP_SESSION": "fixture",
            "TP_WORK_DIR": str(self.root / "work-store"), "PATH": str(fake) + os.pathsep + os.environ["PATH"]})
        self.assertEqual(out.returncode, 0, out.stderr)
        task = json.loads(out.stdout)["id"]
        recorded = json.loads((sessions / "tasks" / task / "dispatch.json").read_text())["main"]
        self.assertEqual(recorded, (sessions / "main-id").read_text().strip())
        self.assertRegex(recorded, r"^main-[0-9a-f]{16}$")

    def test_two_launches_in_one_session_keep_one_id_and_another_session_has_its_own(self):
        first = self.launch()
        self.assertRegex(first, r"^main-[0-9a-f]{16}$")
        self.assertEqual(self.launch(), first)  # /clear, --resume, a second tp-main in the pane: same state dir, same main
        other = self.root / "other"
        other.mkdir()
        self.assertNotEqual(self.launch(state=other), first)

    def test_a_launcher_id_already_set_is_kept_and_a_bad_one_or_file_is_replaced(self):
        self.assertEqual(self.launch(TP_MAIN_ID="main-given"), "main-given")
        self.assertFalse((self.state / "main-id").exists())
        bad = self.launch(TP_MAIN_ID="has space")
        self.assertRegex(bad, r"^main-[0-9a-f]{16}$")
        (self.state / "main-id").write_text("not valid!\n")
        fixed = self.launch()
        self.assertRegex(fixed, r"^main-[0-9a-f]{16}$")
        self.assertEqual(self.launch(), fixed)

    def test_with_no_state_directory_the_main_still_gets_an_id(self):
        self.assertRegex(self.launch(state=None), r"^main-[0-9a-f]{16}$")


class LegacyEntryWarningTests(unittest.TestCase):
    def test_tp_warns_on_stderr_when_tp_main_is_set(self):
        env = {**outside_session(), "TP_MAIN": "bogus"}
        run = subprocess.run([str(ROOT / "bin/tp")], env=env, text=True, capture_output=True, timeout=25, cwd="/")
        self.assertIn("tp: warning: TP_MAIN is the legacy entry point", run.stderr)
        self.assertEqual(run.stdout, "")

    def test_tp_is_silent_about_it_when_tp_main_is_unset(self):
        env = {key: value for key, value in os.environ.items() if key != "TP_MAIN"}
        run = subprocess.run([str(ROOT / "bin/tp"), "--bogus"], env=env, text=True, capture_output=True, timeout=25, cwd="/")
        self.assertNotIn("legacy entry point", run.stderr)


if __name__ == "__main__":
    unittest.main()
