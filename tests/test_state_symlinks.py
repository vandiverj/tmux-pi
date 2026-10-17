"""State writers never write through a planted symlink (lib/safe_write.py).

A symlink left where a task file, a session file, a queue entry, a lane record or a log belongs would send a plain open(path,
"w") to any file the user can write. For a representative writer of each kind, plant a link at the target pointing at a file
outside the state directory and check that the outside file is unchanged and that the write replaced the link or refused clearly.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(HERE))
import capabilities  # noqa: E402
import context_broker  # noqa: E402
import contract  # noqa: E402
import lanes  # noqa: E402
import preflight  # noqa: E402
import review_rounds  # noqa: E402
import safe_write  # noqa: E402
import task_record  # noqa: E402
import tp_window  # noqa: E402
import work  # noqa: E402
import work_checkpoint  # noqa: E402
import worker_process  # noqa: E402
from safe_write import UnsafePath  # noqa: E402
from test_lane_records import RepoFixture  # noqa: E402

OUTSIDE = "outside: the user's own file, not tmux-pi state\n"
MODEL = "openai-codex/gpt-6-sol"


def load_script(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_") + "_under_test", str(ROOT / "bin" / name))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def load_tp_agent():
    return load_script("tp-agent")


class Planted(unittest.TestCase):
    """A state directory with a task in it, and a file outside it that a planted link points at."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.state = self.base / "state"
        self.task = self.state / "tasks" / "w1"
        self.task.mkdir(parents=True)
        self.outside_dir = self.base / "outside"
        self.outside_dir.mkdir()
        self.outside = self.outside_dir / "victim.txt"
        self.outside.write_text(OUTSIDE)

    def plant(self, path):
        """A symlink at path to the outside file; the link is what a tool would find where it writes."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(self.outside)
        return path

    def assertUntouched(self):
        self.assertEqual(self.outside.read_text(), OUTSIDE)
        self.assertEqual(sorted(p.name for p in self.outside_dir.iterdir()), ["victim.txt"])

    def assertReplaced(self, path, text=None):
        self.assertFalse(path.is_symlink(), f"{path} is still a link")
        self.assertTrue(path.is_file())
        if text is not None:
            self.assertEqual(path.read_text(), text)
        self.assertUntouched()

    def assertRefused(self, path, run, *words):
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("symlink", run.stderr)
        for word in words:
            self.assertIn(word, run.stderr)
        self.assertTrue(path.is_symlink(), "a refusal leaves the link as it found it")
        self.assertUntouched()


class SafeWriteTests(Planted):
    def test_a_link_to_a_file_is_replaced_not_written_through(self):
        link = self.plant(self.task / "meta.json")
        safe_write.write_atomic(link, "new\n")
        self.assertReplaced(link, "new\n")

    def test_a_dangling_link_does_not_create_its_target(self):
        link = self.task / "meta.json"
        link.symlink_to(self.outside_dir / "not-yet")
        safe_write.write_atomic(link, "new\n")
        self.assertReplaced(link, "new\n")
        self.assertFalse((self.outside_dir / "not-yet").exists())

    def test_a_link_to_a_directory_is_replaced_and_the_directory_untouched(self):
        link = self.task / "packet.md"
        link.symlink_to(self.outside_dir)
        safe_write.write_atomic(link, "new\n")
        self.assertReplaced(link, "new\n")

    def test_no_temp_file_is_left_behind(self):
        safe_write.write_atomic(self.task / "a.json", "x")
        self.assertEqual([p.name for p in self.task.iterdir()], ["a.json"])
        with self.assertRaises(IsADirectoryError):  # the path is a real directory: refused, and the temp file removed
            (self.task / "d").mkdir()
            safe_write.write_atomic(self.task / "d", "x")
        self.assertEqual(sorted(p.name for p in self.task.iterdir()), ["a.json", "d"])

    def test_modes_are_what_a_plain_write_gave(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        fresh = self.task / "fresh"
        safe_write.write_atomic(fresh, "x")
        self.assertEqual(stat.S_IMODE(fresh.stat().st_mode), 0o644)
        kept = self.task / "kept"
        kept.write_text("old")
        kept.chmod(0o640)
        safe_write.write_atomic(kept, "new")
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o640)
        private = self.task / "private"
        safe_write.write_atomic(private, "x", mode=0o600)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        script = self.task / "run.sh"
        safe_write.write_atomic(script, "x", create=0o777)
        self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o755)

    def test_content_is_written_exactly(self):
        raw = bytes(range(256))
        safe_write.write_atomic(self.task / "raw", raw)
        self.assertEqual((self.task / "raw").read_bytes(), raw)
        safe_write.write_atomic(self.task / "text", "café\n")
        self.assertEqual((self.task / "text").read_bytes(), "café\n".encode())

    def test_an_append_through_a_link_is_refused(self):
        for target in (self.outside, self.outside_dir / "not-yet"):
            with self.subTest(target=target.name):
                link = self.task / "log"
                link.unlink(missing_ok=True)
                link.symlink_to(target)
                with self.assertRaises(UnsafePath) as caught:
                    safe_write.append_text(link, "line\n")
                self.assertIn("symlink", str(caught.exception))
                self.assertUntouched()
        (self.task / "log").unlink()
        safe_write.append_text(self.task / "log", "a\n")
        safe_write.append_text(self.task / "log", "b\n")
        self.assertEqual((self.task / "log").read_text(), "a\nb\n")

    def test_an_owned_directory_that_is_a_link_is_refused(self):
        link = self.state / "queue"
        link.symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            safe_write.ensure_dir(link)
        with self.assertRaises(UnsafePath):
            safe_write.refuse_link(link)
        self.assertUntouched()
        self.assertEqual(safe_write.ensure_dir(self.state / "new" / "deeper"), self.state / "new" / "deeper")


class TaskFileTests(Planted):
    """Task files written by Python: the link is replaced and the outside file is as it was."""

    def writers(self):
        agent = load_tp_agent()
        manifest = {"selected": [], "protections": []}
        return {
            "escalation.json": lambda p: contract.write_escalations(p.parent, [{"id": 1}]),
            "capability-manifest.json": lambda p: capabilities.write_capability_manifest(p, manifest),
            "context-manifest.json": lambda p: context_broker.write_manifest(p, manifest),
            "round.md": lambda p: review_rounds.write(p, "round\n"),
            "review-changed-files.txt": lambda p: review_rounds.write_list_file(p, "a.py\n"),
            "dispatch.json": lambda p: agent.atomic_json(p, {"notes": []}),
            "exit": lambda p: worker_process._ended(p.parent, "working", "%1", 1, "term"),
        }

    def test_every_task_file_writer_replaces_a_planted_link(self):
        for name, write in self.writers().items():
            with self.subTest(file=name):
                path = self.plant(self.task / name)
                write(path)
                self.assertReplaced(path)
        reaped = self.task / "reaped.json"  # worker_process wrote two files; the second is a regular one too
        self.assertTrue(reaped.is_file() and not reaped.is_symlink())

    def test_the_permission_request_of_a_worker_replaces_a_planted_link(self):
        import redact_hook
        path = self.plant(self.task / "permission-request.json")
        old = os.environ.get("TP_TASK_DIR")
        os.environ["TP_TASK_DIR"] = str(self.task)
        self.addCleanup(lambda: os.environ.pop("TP_TASK_DIR", None) if old is None else os.environ.__setitem__("TP_TASK_DIR", old))
        redact_hook.save_request("/x", "why", "pending")
        self.assertReplaced(path)
        self.assertEqual(json.loads(path.read_text())["status"], "pending")

    def test_the_status_cache_replaces_a_planted_link(self):
        path = self.plant(self.base / "window.json")
        tp_window.save_cache(str(path), {})
        self.assertReplaced(path)

    def test_a_new_task_record_is_refused_when_tasks_is_a_link(self):
        other_state = self.base / "other-state"
        other_state.mkdir()
        (other_state / "tasks").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            preflight.create_preflight_record(str(other_state), "do it", {"task": "do it"})
        self.assertUntouched()

    def test_a_task_directory_that_is_a_link_is_refused_by_tp_agent(self):
        agent = load_tp_agent()
        (self.outside_dir / "meta.json").write_text("{}")
        (self.state / "tasks" / "linked").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            agent.task_dir(self.state, "linked")
        self.assertEqual(sorted(p.name for p in self.outside_dir.iterdir()), ["meta.json", "victim.txt"])


class SessionFileTests(Planted):
    def test_the_session_record_writer_refuses_a_planted_link(self):
        link = self.plant(self.state / "session-autonomy.json")
        with self.assertRaises(work.WorkError) as caught:
            work.atomic_write(link, "{}\n")
        self.assertIn("symlink", str(caught.exception))
        self.assertTrue(link.is_symlink())
        self.assertUntouched()

    def test_a_queue_entry_replaces_a_planted_link(self):
        agent = load_tp_agent()
        entry = self.plant(self.state / "queue" / "q1.json")
        agent.atomic_json(entry, {"id": "q1"})
        self.assertReplaced(entry)
        self.assertEqual(json.loads(entry.read_text()), {"id": "q1"})
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)

    def test_a_queue_directory_that_is_a_link_is_refused(self):
        agent = load_tp_agent()
        (self.state / "queue").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            agent.atomic_json(self.state / "queue" / "q1.json", {"id": "q1"})
        self.assertUntouched()

    def test_a_recovery_directory_that_is_a_link_is_refused(self):
        (self.state / work_checkpoint.RECOVERY_DIR).symlink_to(self.outside_dir)
        with self.assertRaises(work.WorkError) as caught:
            work_checkpoint.arm(str(self.state))
        self.assertIn("symlink", str(caught.exception))
        self.assertUntouched()

    def test_a_work_store_directory_that_is_a_link_is_refused(self):
        store = self.base / "store"
        store.mkdir()
        (store / "workspaces").symlink_to(self.outside_dir)
        old = os.environ.get("TP_WORK_DIR")
        os.environ["TP_WORK_DIR"] = str(store)
        self.addCleanup(lambda: os.environ.pop("TP_WORK_DIR", None) if old is None else os.environ.__setitem__("TP_WORK_DIR", old))
        with self.assertRaises(work.WorkError):
            work.ensure_store()
        self.assertUntouched()


class LaneRecordTests(RepoFixture):
    def test_a_lane_record_that_is_a_link_is_refused_and_not_written_through(self):
        self.write()
        path = Path(self.common) / "tmux-pi/lanes/alpha/lane.json"
        outside = path.parent.parent.parent.parent.parent / "victim.txt"  # beside the repository's .git: not lane state
        outside.write_text(OUTSIDE)
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.write()
        self.assertTrue(path.is_symlink())
        self.assertEqual(outside.read_text(), OUTSIDE)


class AppendLogTests(Planted):
    def test_the_cleanup_log_is_not_appended_through_a_link(self):
        log = self.plant(self.state / "cleanup.log")
        with self.assertRaises(UnsafePath):
            task_record.end_session_retention(self.state)
        self.assertTrue(log.is_symlink())
        self.assertUntouched()

    def test_the_cleanup_log_still_appends_as_before(self):
        task_record.end_session_retention(self.state)
        task_record.end_session_retention(self.state)
        lines = (self.state / "cleanup.log").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["event"], "session-end-retention")

    def test_the_message_log_and_lock_of_tp_agent_are_not_followed(self):
        agent = load_tp_agent()
        link = self.plant(self.state / ".tp-agent.lock")
        with self.assertRaises(UnsafePath):
            agent.locked(self.state)
        self.assertTrue(link.is_symlink())
        self.assertUntouched()


class LinkedAncestorTests(Planted):
    """A link in a directory ABOVE the file (tasks/, tasks/<id>/, archive/, a store directory) is refused too: replacing the
    leaf does not help when the directory it is in already leads outside."""

    def link_tasks(self):
        """state/tasks becomes a link to the outside directory, which holds a task that looks like ours."""
        shutil.rmtree(self.state / "tasks")
        (self.outside_dir / "w1").mkdir(exist_ok=True)
        (self.outside_dir / "w1" / "meta.json").write_text("{}")
        (self.state / "tasks").symlink_to(self.outside_dir)
        self.snapshot()

    def snapshot(self):
        self.before = sorted(str(p.relative_to(self.outside_dir)) for p in self.outside_dir.rglob("*"))

    def assertOutsideAsFound(self):
        self.assertEqual(self.outside.read_text(), OUTSIDE)
        self.assertEqual(sorted(str(p.relative_to(self.outside_dir)) for p in self.outside_dir.rglob("*")), self.before)

    def use_work_dir(self, path):
        old = os.environ.get("TP_WORK_DIR")
        os.environ["TP_WORK_DIR"] = str(path)
        self.addCleanup(lambda: os.environ.pop("TP_WORK_DIR", None) if old is None else os.environ.__setitem__("TP_WORK_DIR", old))

    def test_the_helpers_check_every_component_below_the_root(self):
        deep = self.state / "a" / "b"
        deep.mkdir(parents=True)
        safe_write.check_below(self.state, deep / "file")  # all real: fine
        (self.state / "a").rename(self.state / "moved")
        (self.state / "a").symlink_to(self.state / "moved")
        for call in (lambda: safe_write.check_below(self.state, deep / "file"),
                     lambda: safe_write.ensure_dir(deep / "c", root=self.state),
                     lambda: safe_write.write_atomic(deep / "file", "x", root=self.state),
                     lambda: safe_write.append_text(deep / "log", "x", root=self.state)):
            with self.assertRaises(UnsafePath):
                call()
        self.assertEqual(list((self.state / "moved" / "b").iterdir()), [])
        safe_write.write_atomic(self.state / "moved" / "b" / "ok", "x", root=self.state)  # the same place, reached honestly
        link_root = self.base / "root-link"  # the root itself may be spelled with a link: it is where the caller stands
        link_root.symlink_to(self.state)
        safe_write.write_atomic(link_root / "moved" / "b" / "ok2", "x", root=link_root)

    def test_task_root_names_the_state_directory_at_a_fixed_depth_only(self):
        self.assertEqual(safe_write.task_root(self.state / "tasks" / "w1"), str(self.state))
        self.assertEqual(safe_write.task_root(self.state / "archive" / "w1"), str(self.state))
        for path in (self.state, self.state / "tasks", self.state / "tasks" / "w1" / "sub", self.state / "queue" / "q1"):
            with self.subTest(path=str(path)):
                self.assertIsNone(safe_write.task_root(path))
        self.link_tasks()
        root = safe_write.task_root(self.state / "tasks" / "w1")
        for call in (lambda: safe_write.ensure_dir(self.state / "tasks" / "new", root=root),
                     lambda: safe_write.write_atomic(self.state / "tasks" / "w1" / "meta.json", "x", root=root),
                     lambda: safe_write.append_text(self.state / "tasks" / "w1" / "log", "x", root=root)):
            with self.assertRaises(UnsafePath):
                call()
        self.assertOutsideAsFound()

    def test_no_root_is_guessed_from_a_tasks_directory_above_the_state(self):
        """base/tasks -> base/real, and the state directory reached through it: that link is above the state, not in it."""
        (self.base / "real" / "project" / "state").mkdir(parents=True)
        (self.base / "tasks").symlink_to(self.base / "real")
        state = self.base / "tasks" / "project" / "state"
        self.use_work_dir(self.base / "store")
        work.ensure_store()
        work.atomic_write(state / "session-autonomy.json", "{}\n")
        work.atomic_write(state / "session-work.json", "{}\n", root=state)
        safe_write.write_atomic(state / "session-index.json", "{}\n")
        safe_write.write_atomic(state / "session-objective.md", "x\n", root=state)
        safe_write.ensure_dir(state / "queue", root=state)
        safe_write.append_text(state / "cleanup.log", "x\n", root=state)
        work_checkpoint.arm(str(state))
        run = subprocess.run(["python3", str(ROOT / "lib/safe_write.py"), "write", str(state / "tasks-file"), "--root", str(state)],
                             input="y\n", text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(sorted(p.name for p in (self.base / "real" / "project" / "state").iterdir()),
                         sorted(["session-autonomy.json", "session-work.json", "session-index.json", "session-objective.md",
                                 "queue", "cleanup.log", "tasks-file", work_checkpoint.RECOVERY_DIR]))

    def test_a_root_spelled_through_a_link_still_sees_a_linked_tasks_directory(self):
        self.link_tasks()
        link_root = self.base / "root-link"
        link_root.symlink_to(self.state)
        with self.assertRaises(UnsafePath):  # the target is spelled without the root's link: matched by identity, not resolved
            safe_write.check_below(link_root, self.state / "tasks" / "w1" / "meta.json")
        with self.assertRaises(ValueError):
            safe_write.check_below(self.state, self.outside_dir / "w1")
        self.assertOutsideAsFound()

    def test_archiving_refuses_a_task_reached_through_a_linked_tasks_directory(self):
        self.link_tasks()
        with self.assertRaises(UnsafePath):
            task_record.archive_task(self.state, self.state / "tasks" / "w1")
        run = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.state), "archive-one", str(self.state / "tasks" / "w1")],
                             text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("symlink", run.stderr)
        self.assertFalse((self.state / "archive").exists() and any((self.state / "archive").iterdir()))
        retention = task_record.end_session_retention(self.state)  # /end skips what is not ours, and archives nothing from it
        self.assertEqual(retention["archived"], [])
        self.assertOutsideAsFound()

    def test_archiving_refuses_a_linked_task_directory(self):
        (self.outside_dir / "w2").mkdir()
        (self.outside_dir / "w2" / "meta.json").write_text("{}")
        (self.state / "tasks" / "w2").symlink_to(self.outside_dir / "w2")
        self.snapshot()
        with self.assertRaises(UnsafePath):
            task_record.archive_task(self.state, self.state / "tasks" / "w2")
        run = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.state), "archive-one", str(self.state / "tasks" / "w2")],
                             text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertEqual(task_record.end_session_retention(self.state)["archived"], [])
        self.assertTrue((self.state / "tasks" / "w2").is_symlink())
        self.assertOutsideAsFound()

    def test_archiving_a_real_task_still_works(self):
        (self.task / "meta.json").write_text(json.dumps({"id": "w1"}))
        dest = task_record.archive_task(self.state, self.task)
        self.assertEqual(dest.parent, self.state / "archive")
        self.assertFalse(self.task.exists())

    def test_cleanup_deletion_never_counts_a_linked_directory_as_a_task(self):
        tool = load_script("tp-task")
        self.assertTrue(tool._safe_task_path(self.state, self.task))
        (self.state / "tasks" / "w2").symlink_to(self.outside_dir)
        self.assertFalse(tool._safe_task_path(self.state, self.state / "tasks" / "w2"))
        self.link_tasks()
        self.assertFalse(tool._safe_task_path(self.state, self.state / "tasks" / "w1"))
        self.assertFalse(tool._safe_task_path(self.state, self.outside_dir / "w1"))
        self.assertOutsideAsFound()

    def test_a_worker_request_refuses_a_linked_tasks_directory(self):
        self.link_tasks()
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.update(TP_STATE_DIR=str(self.state), TP_TASK_DIR=str(self.state / "tasks" / "w1"))
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "request", "--kind", "tool", "--what", "x", "--reason", "y"],
                             env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertOutsideAsFound()

    def test_task_writers_refuse_when_tasks_is_a_link(self):
        self.link_tasks()
        agent = load_tp_agent()
        task = self.state / "tasks" / "w1"
        for name, call in {
            "tp-agent atomic_json": lambda: agent.atomic_json(task / "dispatch.json", {"a": 1}, self.state),
            "tp-agent atomic_text": lambda: agent.atomic_text(task / "x.md", "x", self.state),
            "tp-agent task_dir": lambda: agent.task_dir(self.state, "w1"),
            "contract": lambda: contract.write_escalations(task, [{"id": 1}]),
            "manifest": lambda: context_broker.write_manifest(task / "m.json", {}),
            "worker_process": lambda: worker_process._ended(task, "working", "%1", 1, "term"),
            "task_record": lambda: task_record.write_record(task),
            "preflight": lambda: preflight.create_preflight_record(str(self.state), "do it", {"task": "do it"}),
        }.items():
            with self.subTest(writer=name), self.assertRaises(UnsafePath):
                call()
        self.assertOutsideAsFound()

    def test_tp_contract_does_not_enter_a_linked_tasks_or_task_directory(self):
        tool = load_script("tp-contract")
        for linked_task_only in (False, True):
            with self.subTest(linked_task_only=linked_task_only):
                if linked_task_only:
                    (self.state / "tasks").unlink()
                    (self.state / "tasks").mkdir()
                    (self.state / "tasks" / "w1").symlink_to(self.outside_dir / "w1")
                    self.snapshot()
                else:
                    self.link_tasks()
                self.assertEqual(list(tool.tasks(self.state)), [])
                self.assertEqual(list(tool.tasks(self.state, ["w1"])), [])
                with self.assertRaises(UnsafePath):
                    tool.find(self.state, "w1")
                with self.assertRaises(UnsafePath):
                    tool.save_events(self.state / "tasks" / "w1", [])
                self.assertOutsideAsFound()

    def test_tp_contract_collect_refuses_a_linked_task_and_writes_nothing_outside(self):
        self.link_tasks()
        run = subprocess.run([str(ROOT / "bin/tp-contract"), "collect", "w1"], text=True, capture_output=True,
                             env={**os.environ, "TP_STATE_DIR": str(self.state)})
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertOutsideAsFound()

    def test_iter_task_dirs_skips_linked_directories(self):
        (self.task / "meta.json").write_text("{}")
        (self.outside_dir / "meta.json").write_text("{}")
        (self.state / "tasks" / "linked").symlink_to(self.outside_dir)
        self.assertEqual([d.name for d, _ in task_record.iter_task_dirs(self.state)], ["w1"])
        self.link_tasks()
        self.assertEqual(list(task_record.iter_task_dirs(self.state)), [])
        (self.state / "archive").symlink_to(self.outside_dir)
        self.assertEqual(list(task_record.iter_task_dirs(self.state)), [])
        with self.assertRaises(UnsafePath):
            task_record.write_record(self.state / "tasks" / "w1")
        self.assertOutsideAsFound()

    def test_a_linked_archive_directory_is_refused_by_retention(self):
        (self.state / "archive").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            task_record.end_session_retention(self.state)
        self.assertUntouched()

    def test_session_files_refuse_a_linked_queue_investigations_or_handoffs(self):
        agent = load_tp_agent()
        (self.state / "queue").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            agent.atomic_json(self.state / "queue" / "q1.json", {"id": "q1"}, self.state)
        (self.state / "investigations").symlink_to(self.outside_dir)
        with self.assertRaises(UnsafePath):
            safe_write.ensure_dir(self.state / "investigations", root=self.state)
        with self.assertRaises(UnsafePath):
            safe_write.write_atomic(self.state / "investigations" / "i1" / "diff.patch", "x", root=self.state)
        (self.state / "handoffs").symlink_to(self.outside_dir)
        tool = load_script("tp-task")
        with self.assertRaises(UnsafePath):
            tool._write_json(self.state / "handoffs" / "h1" / "scaffold.json", {}, self.state)
        self.assertUntouched()

    def test_the_work_store_refuses_a_linked_archive_directory(self):
        store = self.base / "store"
        store.mkdir()
        (store / "archive").symlink_to(self.outside_dir)
        self.use_work_dir(store)
        with self.assertRaises(work.WorkError):
            work.ensure_store()
        self.assertUntouched()  # no workspaces/ or notes/ made in the directory the link leads to

    def test_the_work_store_refuses_a_directory_swapped_for_a_link(self):
        store = self.base / "store"
        self.use_work_dir(store)
        work.ensure_store()
        (store / "workspaces").rmdir()
        (store / "workspaces").symlink_to(self.outside_dir)
        with self.assertRaises(work.WorkError):
            work.atomic_write(store / "workspaces" / "w.md", "x\n")
        with self.assertRaises(work.WorkError):
            work.ensure_store()
        self.assertUntouched()

    def test_the_checkpoint_directories_of_the_store_refuse_a_link(self):
        store = self.base / "store"
        store.mkdir()
        (store / "checkpoints").symlink_to(self.outside_dir)
        self.use_work_dir(store)
        with self.assertRaises(work.WorkError):
            work_checkpoint._private(store / "checkpoints" / "ws")
        self.assertUntouched()

    def test_a_linked_store_root_is_accepted_and_every_component_below_it_checked(self):
        """The store root is the user's choice (TP_WORK_DIR may be a synced folder reached through a link): it is accepted
        as it resolves. What is below it is the store's own and a link there is refused."""
        real = self.base / "synced-store"
        real.mkdir()
        alias = self.base / "store-alias"
        alias.symlink_to(real, target_is_directory=True)
        self.use_work_dir(alias)
        self.assertEqual(work.ensure_store(), real)
        for sub in ("workspaces", "notes", "archive", "archive/workspaces", "archive/notes"):
            self.assertTrue((real / sub).is_dir() and not (real / sub).is_symlink(), sub)
        work.atomic_write(real / "notes" / "n.md", "note\n")
        self.assertEqual((real / "notes" / "n.md").read_text(), "note\n")
        for sub in ("notes", "archive/workspaces"):
            shutil.rmtree(real / sub)
            (real / sub).symlink_to(self.outside_dir)
            with self.assertRaises(work.WorkError):
                work.ensure_store()
            with self.assertRaises(work.WorkError):
                work.atomic_write(real / sub / "x.md", "x\n")
            with self.assertRaises(work.WorkError):  # the same path spelled through the user's alias
                work.atomic_write(alias / sub / "x.md", "x\n")
            (real / sub).unlink()
            (real / sub).mkdir()
        (real / "checkpoints").symlink_to(self.outside_dir)
        with self.assertRaises(work.WorkError):
            work_checkpoint._private(real / "checkpoints" / "ws")
        self.assertUntouched()

    def test_the_recovery_directory_is_refused_when_it_is_a_link(self):
        (self.state / work_checkpoint.RECOVERY_DIR).symlink_to(self.outside_dir)
        with self.assertRaises(work.WorkError):
            work_checkpoint.arm(str(self.state))
        self.assertUntouched()


class LaneAncestorTests(RepoFixture):
    def test_a_lane_record_refuses_a_linked_tmux_pi_or_lanes_directory(self):
        outside = Path(self.common).parent.parent / "elsewhere"
        outside.mkdir()
        (outside / "victim.txt").write_text(OUTSIDE)
        for parts in (["tmux-pi"], ["tmux-pi", "lanes"]):
            with self.subTest(linked="/".join(parts)):
                (Path(self.common) / "tmux-pi").mkdir(exist_ok=True)
                folder = Path(self.common).joinpath(*parts)
                shutil.rmtree(folder, ignore_errors=True)
                folder.symlink_to(outside)
                with self.assertRaises(ValueError):
                    self.write()
                self.assertTrue(folder.is_symlink())
                self.assertEqual(sorted(p.name for p in outside.iterdir()), ["victim.txt"])
                folder.unlink()


class ModeTests(Planted):
    """Every converted writer gives its file the mode it had before: a write into the file kept an existing file's mode, a
    new file renamed over it got the umask's each time, and a private file 0600."""

    def setUp(self):
        super().setUp()
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)

    def mode(self, path):
        return stat.S_IMODE(Path(path).stat().st_mode)

    def test_private_writers_stay_0600(self):
        agent = load_tp_agent()
        tool = load_script("tp-task")
        agent.atomic_json(self.task / "dispatch.json", {})
        task_record._write_atomic(self.task / "task.md", "x\n")
        tool._write_json_atomic(self.task / "review-required.json", {})
        work.atomic_write(self.base / "doc.md", "x\n")
        for path in (self.task / "dispatch.json", self.task / "task.md", self.task / "review-required.json", self.base / "doc.md"):
            with self.subTest(file=path.name):
                self.assertEqual(self.mode(path), 0o600)

    def test_plain_writers_keep_the_umask_mode_and_an_existing_mode(self):
        review_rounds.write_list_file(self.task / "review-changed-files.txt", "a\n")
        self.assertEqual(self.mode(self.task / "review-changed-files.txt"), 0o644)
        for path, write in ((self.task / "round.md", lambda p: review_rounds.write(p, "r")),
                            (self.task / "context.json", lambda p: context_broker.write_manifest(p, {})),
                            (self.task / "cap.json", lambda p: capabilities.write_capability_manifest(p, {}))):
            with self.subTest(file=path.name):
                write(path)
                self.assertEqual(self.mode(path), 0o644)
                path.chmod(0o640)
                write(path)
                self.assertEqual(self.mode(path), 0o640)

    def test_writers_that_renamed_a_new_file_into_place_give_the_umask_mode_each_time(self):
        """The status cache and the fixed `.tmp` + rename writers: 0666 less the umask (0644 here), whatever was there."""
        for path, write in ((self.base / "cache.json", lambda p: tp_window.save_cache(str(p), {})),
                            (self.task / "escalation.json", lambda p: contract.write_escalations(p.parent, [])),
                            (self.task / "ledger", lambda p: contract._write_ledger(p, [])),
                            (self.task / "process.json", lambda p: safe_write.write_atomic(p, "x", keep_mode=False))):
            with self.subTest(file=path.name):
                write(path)
                self.assertEqual(self.mode(path), 0o644)
                path.chmod(0o600)
                write(path)
                self.assertEqual(self.mode(path), 0o644)

    def test_an_explicit_mode_wins_over_an_existing_file(self):
        script = self.task / "run.sh"
        script.write_text("old")
        script.chmod(0o644)

        def cli(*args):
            run = subprocess.run(["python3", str(ROOT / "lib/safe_write.py"), "write", str(script), *args],
                                 input="x", text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            return self.mode(script)

        self.assertEqual(cli("--executable"), 0o755)  # what chmod +x after a write gave
        self.assertEqual(cli("--mode", "600"), 0o600)
        self.assertEqual(cli("--fresh"), 0o644)
        safe_write.write_atomic(script, "x", mode=0o640)
        self.assertEqual(self.mode(script), 0o640)
        safe_write.write_atomic(script, "y")  # no mode given: the existing file's is kept
        self.assertEqual(self.mode(script), 0o640)


class ShellWriterTests(Planted):
    """The shell scripts' way in (lib/tp.sh) and the real tp-spawn, with a fake tmux."""

    def setUp(self):
        super().setUp()
        fake = self.base / "fakebin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%fake\\n' ;;
 *) exit 0 ;;
esac
''')
        tmux.chmod(0o755)
        self.project = self.base / "project"
        self.project.mkdir()
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(self.base / "work-store"),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)
        self.env.pop("TP_MAIN", None)

    def sh(self, script, *args):
        return subprocess.run(["bash", "-c", f'ROOT="{ROOT}"; source "$ROOT/lib/tp.sh"; set -euo pipefail; {script}', "sh", *args],
                              env=self.env, text=True, capture_output=True)

    def spawn(self, *extra):
        return subprocess.run([str(ROOT / "bin/tp-spawn"), "--worker", "pi", "--model", MODEL, "--workdir", str(self.project),
                               *extra, "--", "Review it"], env=self.env, text=True, capture_output=True, timeout=60)

    def test_tp_write_replaces_a_planted_link_and_keeps_the_content(self):
        link = self.plant(self.task / "request.md")
        run = self.sh('printf "%s\\n" "one two" | tp_write "$1"', str(link))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertReplaced(link, "one two\n")

    def test_tp_write_keeps_the_mode_of_the_file_it_replaces(self):
        path = self.task / "meta.json"
        path.write_text("{}")
        path.chmod(0o640)
        self.assertEqual(self.sh('echo new | tp_write "$1"', str(path)).returncode, 0)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(path.read_text(), "new\n")

    def test_tp_append_and_tp_owned_dir_refuse_links(self):
        log = self.plant(self.task / "messages.jsonl")
        self.assertRefused(log, self.sh('echo x | tp_append "$1"', str(log)))
        queue = self.state / "queue"
        queue.symlink_to(self.outside_dir)
        self.assertRefused(queue, self.sh('tp_owned_dir "$1"', str(queue)))
        self.assertRefused(queue, self.sh('tp_not_link "$1"', str(queue)))

    def test_a_pipeline_that_fails_still_fails_the_script(self):
        run = self.sh('echo "not json" | jq . | tp_write "$1"; echo unreachable', str(self.task / "x.json"))
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("unreachable", run.stdout)

    def test_tp_spawn_writes_the_task_files_as_before(self):
        run = self.spawn()
        self.assertEqual(run.returncode, 0, run.stderr)
        task = self.state / "tasks" / run.stdout.strip()
        self.assertEqual((task / "request.md").read_text(), "Review it\n")
        for name in ("packet.md", "meta.json", "skill-selection.json", "run.sh"):
            self.assertTrue((task / name).is_file() and not (task / name).is_symlink(), name)
        self.assertEqual(json.loads((task / "meta.json").read_text())["id"], task.name)
        self.assertTrue(os.access(task / "run.sh", os.X_OK))
        self.assertTrue((task / "run.sh").read_text().startswith("#!/usr/bin/env bash\n"))

    def test_tp_spawn_refuses_a_task_directory_that_is_a_dangling_link(self):
        link = self.state / "tasks" / "fixed"
        link.symlink_to(self.outside_dir / "not-yet")
        run = self.spawn("--task-id", "fixed")
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertTrue(link.is_symlink())
        self.assertFalse((self.outside_dir / "not-yet").exists())
        self.assertUntouched()

    def test_the_exit_file_a_worker_writes_replaces_a_planted_link(self):
        run = self.spawn()
        self.assertEqual(run.returncode, 0, run.stderr)
        task = self.state / "tasks" / run.stdout.strip()
        line = next(l for l in (task / "run.sh").read_text().splitlines() if "safe_write.py" in l)
        link = self.plant(task / "exit")
        done = subprocess.run(["bash", "-c", "status=7; " + line], env=self.env, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertReplaced(link, "7\n")


class LauncherSessionDirTests(Planted):
    """bin/tp makes <workdir>/.tmux-pi/sessions/<run>/{state,tasks}. The workdir is the root it checks from: a repository can
    commit .tmux-pi or .tmux-pi/sessions as a link, and the session state must not land where that link leads."""

    def setUp(self):
        super().setUp()
        self.project = self.base / "project"
        self.project.mkdir()
        fake = self.base / "fakebin"
        fake.mkdir()
        self.log = self.base / "tmux.log"
        tmux = fake / "tmux"
        tmux.write_text('#!/bin/sh\necho "$1" >> "$TEST_TMUX_LOG"\n'
                        'case "$1" in has-session) exit 1;; new-session) echo %main;; esac\nexit 0\n')
        tmux.chmod(0o755)
        for name in ("pi", "claude"):
            (fake / name).write_text("#!/bin/sh\nexit 0\n")
            (fake / name).chmod(0o755)
        self.env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"], "TEST_TMUX_LOG": str(self.log),
                    "TP_NO_ATTACH": "1"}
        for key in ("TP_SESSION", "TMUX", "TP_MAIN", "TP_MAIN_HARNESS", "TP_STATE_DIR", "TP_WORKDIR"):
            self.env.pop(key, None)
        self.before = sorted(str(p.relative_to(self.outside_dir)) for p in self.outside_dir.rglob("*"))

    def launch(self):
        return subprocess.run([str(ROOT / "bin" / "tp"), str(self.project)], env=self.env, text=True, capture_output=True,
                              timeout=30)

    def assertOutsideAsFound(self):
        self.assertEqual(self.outside.read_text(), OUTSIDE)
        self.assertEqual(sorted(str(p.relative_to(self.outside_dir)) for p in self.outside_dir.rglob("*")), self.before)

    def assertRefused(self, run):
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn("symlink", run.stderr)
        self.assertNotIn("new-session", self.log.read_text() if self.log.exists() else "")
        self.assertOutsideAsFound()

    def test_a_real_session_directory_is_made_as_before(self):
        run = self.launch()
        self.assertEqual(run.returncode, 0, run.stderr)
        sessions = list((self.project / ".tmux-pi" / "sessions").iterdir())
        self.assertEqual(len(sessions), 1)
        self.assertTrue((sessions[0] / "state").is_dir() and (sessions[0] / "tasks").is_dir())

    def test_a_linked_tmux_pi_directory_is_refused(self):
        (self.project / ".tmux-pi").symlink_to(self.outside_dir, target_is_directory=True)
        self.assertRefused(self.launch())

    def test_a_linked_sessions_directory_is_refused(self):
        (self.project / ".tmux-pi").mkdir()
        (self.project / ".tmux-pi" / "sessions").symlink_to(self.outside_dir, target_is_directory=True)
        self.assertRefused(self.launch())

    def test_a_linked_session_state_or_tasks_directory_is_refused(self):
        """The run id is fresh each launch, so the session dir, state and tasks are planted for a fixed id and the command
        bin/tp runs (mkdir --root <workdir>) is checked against each."""
        session = self.project / ".tmux-pi" / "sessions" / "fixed"
        line = next(l for l in (ROOT / "bin" / "tp").read_text().splitlines() if 'safe_write.py" mkdir' in l)
        self.assertIn('--root "$WORKDIR" "$STATE_DIR/state" "$STATE_DIR/tasks"', line)
        for planted in (session, session / "state", session / "tasks"):
            shutil.rmtree(self.project / ".tmux-pi", ignore_errors=True)
            planted.parent.mkdir(parents=True)
            planted.symlink_to(self.outside_dir, target_is_directory=True)
            run = subprocess.run([sys.executable, str(ROOT / "lib" / "safe_write.py"), "mkdir", "--root", str(self.project),
                                  str(session / "state"), str(session / "tasks")], text=True, capture_output=True)
            self.assertNotEqual(run.returncode, 0, planted)
            self.assertIn("symlink", run.stderr)
            self.assertOutsideAsFound()


if __name__ == "__main__":
    unittest.main()
