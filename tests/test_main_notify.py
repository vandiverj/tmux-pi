"""Which main dispatched a task, and `tp-agent list --main`: the data the Claude main's notifier wakes on."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import fcntl
import json
import os
from pathlib import Path
import select
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

    def test_a_worker_with_its_mains_id_records_it_on_what_it_dispatches(self):
        item = self.dispatch(TP_TASK_DIR=str(self.here / "tasks" / "x"), CLAUDE_CODE_SESSION_ID="sess-1", TP_MAIN_ID="main-a")
        self.assertEqual(self.ids()[item["id"]]["main"], "main-a")

    def test_a_dispatched_worker_exports_the_dispatching_mains_id_only_when_there_is_a_valid_one(self):
        """tp-spawn writes it into the worker's run.sh (the worker window does not inherit the caller's environment); with none, or a
        bad one, run.sh has no TP_MAIN_ID line. A Claude session id alone is the main the task was recorded under, so it follows too."""
        def run_sh(item):
            return (self.here / "tasks" / item["id"] / "run.sh").read_text()
        self.assertIn("export TP_MAIN_ID=main-a\n", run_sh(self.dispatch(TP_MAIN_ID="main-a")))
        self.assertNotIn("TP_MAIN_ID", run_sh(self.dispatch()))
        self.assertNotIn("TP_MAIN_ID", run_sh(self.dispatch(TP_MAIN_ID="has space")))
        self.assertIn("export TP_MAIN_ID=sess-1\n", run_sh(self.dispatch(CLAUDE_CODE_SESSION_ID="sess-1")))

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

    def env(self, state="default", **env):
        merged = {**outside_session(), "TP_MAIN_HARNESS": "claude", **env}
        if state is not None:
            merged["TP_STATE_DIR"] = str(self.state if state == "default" else state)
        return merged

    def launch(self, state="default", **env):
        run = subprocess.run([str(self.root / "bin/tp-main")], env=self.env(state, **env), text=True, capture_output=True, timeout=25, cwd=str(self.root))
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout.strip()

    def fresh_state(self, name, content=None):
        state = self.root / name
        state.mkdir()
        if content is not None:
            (state / "main-id").write_text(content)
        return state

    def together(self, envs):
        """One tp-main per env, all started at the same moment: each waits on one pipe until every process exists."""
        release, hold = os.pipe()
        procs = [subprocess.Popen(["bash", "-c", 'read -r _ <&"$1"; shift; exec "$@"', "barrier", str(release), str(self.root / "bin/tp-main")],
                                  env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(self.root), pass_fds=(release,))
                 for env in envs]
        self.addCleanup(lambda: [p.kill() for p in procs if p.poll() is None])
        os.close(release)
        os.close(hold)  # EOF on the pipe is the starting gun
        results = [p.communicate(timeout=25) + (p.returncode,) for p in procs]
        for out, err, code in results:
            self.assertEqual((code, err), (0, ""), out)
        return [out.strip() for out, _, _ in results]

    def spawn(self, state, pass_fds, **env):
        proc = subprocess.Popen([str(self.root / "bin/tp-main")], env=self.env(state, **env), text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=str(self.root), pass_fds=pass_fds)
        self.addCleanup(lambda: proc.kill() if proc.poll() is None else None)
        return proc

    @staticmethod
    def byte(fd):
        """The next byte on a pipe, b'' at EOF (the writer ended without sending one), None if nothing comes in 20 s."""
        ready, _, _ = select.select([fd], [], [], 20)
        return os.read(fd, 1) if ready else None

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

    def test_an_id_given_at_launch_is_kept_and_recorded_for_a_later_launch_without_it(self):
        self.assertEqual(self.launch(TP_MAIN_ID="main-given"), "main-given")
        self.assertEqual((self.state / "main-id").read_text().strip(), "main-given")
        self.assertEqual(self.launch(), "main-given")  # no TP_MAIN_ID this time: the same state dir, the same main
        (self.state / "main-id").write_text("main-old\n")
        self.assertEqual(self.launch(TP_MAIN_ID="main-new"), "main-new")  # a given id replaces what was there
        self.assertEqual(self.launch(), "main-new")

    def test_a_bad_id_given_at_launch_is_not_exported_or_recorded(self):
        bad = self.launch(TP_MAIN_ID="has space")
        self.assertRegex(bad, r"^main-[0-9a-f]{16}$")
        self.assertEqual((self.state / "main-id").read_text().strip(), bad)

    def test_an_empty_or_bad_main_id_file_is_replaced_with_a_fresh_valid_id_that_a_second_launch_reads_back(self):
        for content in ("", "\n", "not valid!\n", "two\nlines\n"):
            with self.subTest(content=content):
                (self.state / "main-id").write_text(content)
                fresh = self.launch()
                self.assertRegex(fresh, r"^main-[0-9a-f]{16}$")  # never an empty TP_MAIN_ID
                self.assertEqual((self.state / "main-id").read_text().strip(), fresh)
                self.assertEqual(self.launch(), fresh)

    def test_an_id_that_cannot_be_read_back_is_still_exported_and_said_on_stderr(self):
        (self.state / "main-id").mkdir()  # a directory where the file goes: it cannot be removed as a file or read
        run = subprocess.run([str(self.root / "bin/tp-main")], text=True, capture_output=True, timeout=25, cwd=str(self.root), env={
            **outside_session(), "TP_MAIN_HARNESS": "claude", "TP_STATE_DIR": str(self.state)})
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertRegex(run.stdout.strip(), r"^main-[0-9a-f]{16}$")
        self.assertIn("could not read the main id back", run.stderr)

    def test_with_no_state_directory_the_main_still_gets_an_id(self):
        self.assertRegex(self.launch(state=None), r"^main-[0-9a-f]{16}$")

    # A missing, an empty and a bad file are the three states that make a launch write; each must hold under overlapping launches.
    FILE_STATES = (("missing", None), ("empty", ""), ("bad", "not valid!\n"))

    def test_launches_started_together_all_export_the_one_id_the_file_holds(self):
        for round_number in range(3):
            for name, content in self.FILE_STATES:
                with self.subTest(file=name, round=round_number):
                    state = self.fresh_state(f"together-{name}-{round_number}", content)
                    ids = self.together([self.env(state)] * 8)
                    self.assertEqual(len(set(ids)), 1, ids)
                    self.assertRegex(ids[0], r"^main-[0-9a-f]{16}$")
                    self.assertEqual((state / "main-id").read_text().strip(), ids[0])
                    self.assertEqual(self.launch(state=state), ids[0])  # and a relaunch keeps it

    def test_a_launch_paused_after_reading_the_file_is_not_overtaken_by_another(self):
        """The review's reproduction, without a sleep: A holds the lock and has read the file; B starts and is shown to be
        waiting for the lock; only then A goes on. Without the lock B installs its own id while A is paused, and A's differs."""
        for name, content in self.FILE_STATES:
            with self.subTest(file=name):
                state = self.fresh_state(f"paused-{name}", content)
                reached, reached_w = os.pipe()
                go_r, go = os.pipe()
                waiting, waiting_w = os.pipe()
                a = self.spawn(state, (reached_w, go_r), MAIN_ID_TEST_PAUSE=f"{reached_w}:{go_r}")
                os.close(reached_w)
                os.close(go_r)
                self.assertEqual(self.byte(reached), b"x", "A did not stop between its read and its repair")
                b = self.spawn(state, (waiting_w,), MAIN_ID_TEST_WAITING=str(waiting_w))
                os.close(waiting_w)
                self.assertEqual(self.byte(waiting), b"x", "B did not find the lock taken")
                os.write(go, b"x")
                (a_out, a_err), (b_out, b_err) = a.communicate(timeout=25), b.communicate(timeout=25)
                for fd in (reached, go, waiting):
                    os.close(fd)
                self.assertEqual((a.returncode, b.returncode, a_err, b_err), (0, 0, "", ""))
                self.assertEqual(a_out, b_out)
                self.assertEqual((state / "main-id").read_text().strip(), a_out.strip())
                self.assertEqual(self.launch(state=state), a_out.strip())

    def test_launches_started_together_with_different_given_ids_each_export_their_own_and_the_last_writer_is_kept(self):
        state = self.fresh_state("given")
        ids = self.together([self.env(state, TP_MAIN_ID=f"main-given-{n}") for n in range(4)])
        self.assertEqual(ids, [f"main-given-{n}" for n in range(4)])
        self.assertIn((state / "main-id").read_text().strip(), ids)
        self.assertEqual(self.launch(state=state), (state / "main-id").read_text().strip())

    def test_a_lock_that_stays_taken_never_blocks_the_launch_and_never_removes_a_valid_file(self):
        """The lock is held by this test for the whole run: the launch waits MAIN_ID_LOCK_WAIT, then goes on without writing."""
        for name, content, given, expect in (
                ("valid file", "main-kept\n", None, "main-kept"), ("valid file, given id", "main-kept\n", "main-given", "main-given"),
                ("missing file", None, None, None), ("bad file", "not valid!\n", None, None)):
            with self.subTest(case=name):
                state = self.fresh_state("locked-" + name.replace(",", "").replace(" ", "-"), content)
                with open(state / ".main-id.lock", "a") as held:
                    fcntl.flock(held, fcntl.LOCK_EX)
                    env = self.env(state, MAIN_ID_LOCK_WAIT="0.2", **({"TP_MAIN_ID": given} if given else {}))
                    run = subprocess.run([str(self.root / "bin/tp-main")], env=env, text=True, capture_output=True, timeout=25, cwd=str(self.root))
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertIn("could not lock", run.stderr)
                if given and content:
                    self.assertIn("main-given", run.stderr)
                    self.assertIn("recorded id stays main-kept", run.stderr)
                    # a following launch without the override exports the recorded id
                    held_off = self.launch(state=state)
                    self.assertEqual(held_off, "main-kept")
                # the recorded id, else the given one, else a fresh one for this launch: never empty
                self.assertRegex(run.stdout.strip(), f"^{expect}$" if expect else r"^main-[0-9a-f]{16}$")
                if content is None:
                    self.assertFalse((state / "main-id").exists())  # not created
                else:
                    self.assertEqual((state / "main-id").read_text(), content)  # not removed, not rewritten


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
