"""The status pane's window (lib/tp_window.py) and the usage summary it keeps (lib/tp_usage.py): which tasks are loaded, that
a refresh does not read every record, that memory is bounded, and that the summary says what the old jq program said."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tp_rows  # noqa: E402
import tp_window  # noqa: E402

SEP = "\x1f"
PAUSE = {"action": "pause", "at": 1000, "tools": 3}
WORKING = {"state": "working", "model": "m", "tokens": {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}
SETTLED = {"state": "waiting", "model": "m", "tokens": {"input": 5, "cacheRead": 0, "cacheWrite": 0, "output": 6}}


class WindowFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.tasks = self.base / "state" / "tasks"
        self.tasks.mkdir(parents=True)
        self.cache = self.base / "cache.json"
        self.clock = time.time() - 100000
        self.panes = ""

    def task(self, name, state=None, result=None, events=None, files=None, alive=False):
        """A task directory; each one is a minute newer than the one before. alive: its pane is in the pane list."""
        d = self.tasks / name
        d.mkdir()
        pane = "%%%d" % (len(list(self.tasks.iterdir())) + 100)
        (d / "meta.json").write_text(json.dumps({"id": name, "worker": "pi", "pane": pane, "model": "", "thinking": ""}))
        (d / "state.json").write_text(json.dumps(state or SETTLED))
        (d / "task.md").write_text("task of %s\n" % name)
        if result is not None:
            (d / "result.md").write_text(result)
        if events is not None:
            (d / "checkpoints.json").write_text(json.dumps({"events": events}))
        for fname, value in (files or {}).items():
            (d / fname).write_text(json.dumps(value))
        if alive:
            self.panes += "%s 0\n" % pane
        self.clock += 60
        os.utime(d / "meta.json", (self.clock, self.clock))
        return d

    def finished(self, name):
        return self.task(name, state=SETTLED, result="Done.\nRESULT_STATUS: complete\n")

    def run_window(self, keep, start="newest", cache=True):
        """(header fields, ids of the R lines, the R lines, the U lines) from the helper run as a program."""
        run = subprocess.run([sys.executable, str(ROOT / "lib/tp_window.py"), str(self.tasks), self.panes,
                              str(self.cache) if cache else "-", str(keep), str(start)],
                             text=True, capture_output=True, check=True)
        return self.parse(run.stdout)

    def counting(self):
        """Count the record reads (evaluate), the rows built (row_line) and the cache rewrites (save_cache) the helper does
        when called in this process."""
        counts = {"evaluate": 0, "row_line": 0, "save_cache": 0}
        real_eval, real_row, real_save = tp_window.evaluate, tp_window.tp_rows.row_line, tp_window.save_cache

        def evaluate(*args):
            counts["evaluate"] += 1
            return real_eval(*args)

        def row_line(*args):
            counts["row_line"] += 1
            return real_row(*args)

        def save_cache(*args):
            counts["save_cache"] += 1
            return real_save(*args)

        tp_window.evaluate, tp_window.tp_rows.row_line, tp_window.save_cache = evaluate, row_line, save_cache
        self.addCleanup(setattr, tp_window, "evaluate", real_eval)
        self.addCleanup(setattr, tp_window.tp_rows, "row_line", real_row)
        self.addCleanup(setattr, tp_window, "save_cache", real_save)
        return counts

    def refresh(self, counts, keep, start="newest"):
        """One call of the helper in this process; what it read, built and wrote this time. self.out is what it printed."""
        before = dict(counts)
        real = sys.stdout
        sys.stdout = type("Out", (), {"buffer": io.BytesIO()})()
        try:
            tp_window.main(str(self.tasks), self.panes, str(self.cache), keep, start)
            self.out = sys.stdout.buffer.getvalue().decode()
        finally:
            sys.stdout = real
        return {key: counts[key] - before[key] for key in counts}

    def changed(self):
        """The ids on the C lines of the last refresh."""
        return [line[1:] for line in self.out.split("\n") if line.startswith("C")]

    def shown(self):
        """The ids on the R lines of the last refresh."""
        return [line[1:].split(SEP)[0] for line in self.out.split("\n") if line.startswith("R")]

    @staticmethod
    def parse(text):
        header, rows, usage = None, [], []
        for line in text.split("\n"):
            if line.startswith("H" + SEP):
                header = line.split(SEP)[1:]
            elif line.startswith("R"):
                rows.append(line[1:])
            elif line.startswith("U"):
                usage.append(line[1:])
        return header, [r.split(SEP)[0] for r in rows], rows, usage


class ActiveTasksAlwaysShownTest(WindowFixture):
    def build(self):
        """Four old tasks that are not finished (starting, working, needs_input, paused), then 30 finished ones, with one
        old finished task among the first."""
        self.task("old-starting", state={"state": "starting", "model": "m", "tokens": {}}, alive=True)
        self.task("old-working", state=WORKING, alive=True)
        self.task("old-asking", state=WORKING, alive=True, files={"permission-request.json": {"status": "pending", "target": "db"}})
        self.task("old-paused", state=WORKING, result="Done, I think.\n", events=[PAUSE], alive=True)
        self.finished("old-finished")
        for n in range(30):
            self.finished("done%02d" % n)

    def test_active_paused_and_needs_input_tasks_are_in_the_window_whatever_their_age(self):
        self.build()
        for start in ("newest", 0, 12):
            with self.subTest(start=start):
                header, ids, _, _ = self.run_window(5, start)
                for name in ("old-starting", "old-working", "old-asking", "old-paused"):
                    self.assertIn(name, ids)
                self.assertNotIn("old-finished" if start == "newest" else "done29", ids)
                self.assertEqual(header[0], "35")      # the whole list is counted, not just what is loaded

    def test_the_window_is_the_newest_tasks_plus_the_active_ones_in_list_order(self):
        self.build()
        header, ids, _, _ = self.run_window(5)
        self.assertEqual(ids, ["old-starting", "old-working", "old-asking", "old-paused",
                               "done25", "done26", "done27", "done28", "done29"])
        self.assertEqual(header[1:3], ["30", "done29"])
        self.assertEqual(header[3], "0,1,2,3,30,31,32,33,34")     # list positions, so the pane can place the rows

    def test_a_settled_task_is_not_active_however_new_the_window_wants_it_to_be(self):
        self.build()
        _, ids, _, _ = self.run_window(5, 0)
        self.assertIn("old-finished", ids)                  # inside the window by position
        _, ids, _, _ = self.run_window(2, "newest")
        self.assertNotIn("old-finished", ids)               # outside it, and finished
        self.assertEqual(len(ids), 6)                       # 4 active + the 2 newest


class OlderTasksLookLikeRecentOnesTest(WindowFixture):
    def test_a_task_reached_by_paging_has_the_row_a_recent_one_has(self):
        for n in range(12):
            self.task("t%02d" % n, state=dict(SETTLED, model="p/model"), result="Reply %d\nRESULT_STATUS: complete\n" % n)
        full = subprocess.run([sys.executable, str(ROOT / "lib/tp_rows.py"), str(self.tasks), self.panes],
                              text=True, capture_output=True, check=True).stdout.splitlines()
        by_id = {line.split(SEP)[0]: line for line in full}
        for start in (0, 4, "newest"):
            _, ids, rows, _ = self.run_window(4, start)
            for ident, row in zip(ids, rows):
                self.assertEqual(row, by_id[ident], "row of %s from window start %s" % (ident, start))
        _, ids, _, _ = self.run_window(4, 0)
        self.assertEqual(ids, ["t00", "t01", "t02", "t03"])

    def test_the_detail_lines_come_from_the_same_reader_for_an_old_task_and_a_new_one(self):
        """lib/tp_detail.py reads the task's own files, not the window: it gives an old task what it gives a new one."""
        for n in range(8):
            self.task("t%d" % n, state=SETTLED, result="Reply %d\nmore\nRESULT_STATUS: complete\n" % n,
                      files={"permission-request.json": {"status": "pending", "target": "tgt%d" % n}})
        header, ids, rows, _ = self.run_window(2, 0)
        self.assertEqual(ids, ["t0", "t1"])
        detail = subprocess.run([sys.executable, str(ROOT / "lib/tp_detail.py"), str(self.tasks), ""], input="\n".join(rows) + "\n",
                                text=True, capture_output=True, check=True).stdout.splitlines()
        _, ids_new, rows_new, _ = self.run_window(2, "newest")
        detail_new = subprocess.run([sys.executable, str(ROOT / "lib/tp_detail.py"), str(self.tasks), ""], input="\n".join(rows_new) + "\n",
                                    text=True, capture_output=True, check=True).stdout.splitlines()
        shape = lambda line: [bool(part) for part in line.split(SEP)]
        self.assertEqual([shape(l) for l in detail], [shape(l) for l in detail_new])
        self.assertIn("permission: tgt0", detail[0])
        self.assertIn("permission: tgt7", detail_new[1])
        self.assertTrue(detail[0].split(SEP)[3].startswith("task of t0"))
        self.assertTrue(detail[0].split(SEP)[4].startswith("Reply 0"))


class RefreshDoesNotReadEveryRecordTest(WindowFixture):
    def test_a_warm_refresh_reads_only_the_active_tasks_and_builds_only_the_window(self):
        for n in range(40):
            self.finished("done%02d" % n)
        self.task("live", state=WORKING, alive=True)
        counts = self.counting()
        self.assertEqual(self.refresh(counts, 5)["evaluate"], 41)      # no cache yet: every record once
        warm = self.refresh(counts, 5)
        self.assertEqual(warm["evaluate"], 1)                          # the live task only
        self.assertEqual(warm["row_line"], 5)                          # the window: the 4 newest finished, and the live one
        (self.tasks / "done00" / "result.md").write_text("changed\nRESULT_STATUS: complete\n")
        self.assertEqual(self.refresh(counts, 5)["evaluate"], 2)      # the live task and the one that changed

    def test_a_refresh_reads_no_more_records_for_500_finished_tasks_than_for_20(self):
        reads = {}
        counts = self.counting()
        for count in (20, 500):
            shutil.rmtree(self.tasks)
            self.tasks.mkdir()
            if self.cache.exists():
                self.cache.unlink()
            for n in range(count):
                self.finished("done%04d" % n)
            self.task("live", state=WORKING, alive=True)
            self.refresh(counts, 10)
            reads[count] = self.refresh(counts, 10)
        self.assertEqual(reads[20], reads[500])
        self.assertEqual(reads[500], {"evaluate": 1, "row_line": 10, "save_cache": 0})   # and the cache is not rewritten


class NothingIsDeletedTest(WindowFixture):
    def test_running_the_helper_leaves_every_task_file_as_it_was(self):
        for n in range(8):
            self.finished("done%d" % n)
        self.task("live", state=WORKING, alive=True, files={"permission-request.json": {"status": "pending"}})

        def snapshot():
            return {str(p.relative_to(self.tasks)): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in sorted(self.tasks.rglob("*"))}
        before = snapshot()
        for start in ("newest", 0, 3):
            self.run_window(2, start)
            self.run_window(2, start, cache=False)
        self.assertEqual(snapshot(), before)           # nothing removed, nothing rewritten, not even a timestamp
        self.assertTrue(self.cache.exists())           # the cache is the one thing it writes, and it is outside the tasks directory


class MemoryIsBoundedTest(WindowFixture):
    def test_the_window_holds_the_keep_newest_plus_the_active_whatever_the_number_of_tasks(self):
        for n in range(500):
            self.finished("done%04d" % n)
        self.task("live-a", state=WORKING, alive=True)
        self.task("live-b", state=WORKING, alive=True)
        for keep in (1, 7, 60):
            header, ids, rows, _ = self.run_window(keep)
            self.assertEqual(header[0], "502")
            self.assertEqual(len(rows), len(ids))
            # the keep newest, which include the two live ones when keep >= 2, plus the live ones outside them
            self.assertEqual(len(ids), max(keep, 2))
        _, ids, _, _ = self.run_window(60, 100)
        self.assertEqual(len(ids), 62)

    def test_the_pane_refuses_a_window_that_is_not_a_positive_number(self):
        for keep, start in (("0", "newest"), ("-3", "newest"), ("many", "newest"), ("5", "soon"), ("5", "")):
            with self.subTest(keep=keep, start=start):
                run = subprocess.run([sys.executable, str(ROOT / "lib/tp_window.py"), str(self.tasks), "", "-", keep, start],
                                     text=True, capture_output=True)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn("KEEP must be a positive number", run.stderr)
                self.assertEqual(run.stdout, "")

    def test_a_cache_that_cannot_be_read_is_rebuilt_not_trusted(self):
        for n in range(6):
            self.finished("d%d" % n)
        first = self.run_window(3)
        for junk in ("{not json", json.dumps({"v": 1, "e": {"d0": {"skip": True}}}), json.dumps([1, 2]), json.dumps({"v": 2, "e": 5})):
            self.cache.write_text(junk)
            self.assertEqual(self.run_window(3), first)
        self.assertEqual(json.loads(self.cache.read_text())["v"], tp_window.VERSION)

    def test_a_cache_never_hides_a_task_whose_record_changed(self):
        self.task("t", state=WORKING, alive=True)
        self.finished("u")
        self.run_window(5)
        (self.tasks / "t" / "state.json").write_text(json.dumps(dict(WORKING, model="changed-model")))
        _, ids, rows, _ = self.run_window(5)
        self.assertIn("changed-model", rows[ids.index("t")])
        # a task that stops being listed disappears from the cache and the window
        shutil.rmtree(self.tasks / "u")
        header, ids, _, _ = self.run_window(5)
        self.assertEqual((header[0], ids), ("1", ["t"]))


class EveryLifecycleFileCountsTest(WindowFixture):
    """Finding 1: the cache must notice a change in any file the lifecycle is worked out from, or an old task that needs
    input would sit outside the window unseen."""

    def pad(self):
        """Three newer finished tasks, so with keep=2 an older task is outside the window."""
        for n in range(3):
            self.finished("new%d" % n)

    def test_the_fingerprint_and_the_lifecycle_reader_take_the_list_from_the_same_place(self):
        import contract
        import lifecycle_files
        self.assertIs(tp_window.LIFECYCLE_FILES, lifecycle_files.LIFECYCLE_FILES)
        self.assertIs(contract.LIFECYCLE_FILES, lifecycle_files.LIFECYCLE_FILES)
        for name in ("pending-capability.json", "permission-request.json", "capability-request.json", "completed", "exit",
                     "escalated", "stopped.json", "collected.json", "state.json", "checkpoints.json", "result.md"):
            self.assertIn(name, lifecycle_files.LIFECYCLE_FILES)

    def test_a_file_the_lifecycle_reader_reads_is_a_file_the_fingerprint_covers(self):
        """task_lifecycle is run with every name it builds under the task directory recorded. Whatever it opens must be
        in the fingerprint's list; with every file present it opens exactly the list (so the list holds nothing dead)."""
        import contract
        names = []

        class Recording(type(Path())):
            def __truediv__(self, other):
                names.append(str(other))
                return super().__truediv__(other)

        real = contract.Path
        contract.Path = Recording
        self.addCleanup(setattr, contract, "Path", real)
        d = self.task("full", state=WORKING, result="Done.\nRESULT_STATUS: complete\n", events=[PAUSE], alive=True,
                      files={"pending-capability.json": {}, "permission-request.json": {"status": "answered"},
                             "capability-request.json": {"status": "answered"}, "completed": 1, "exit": 0,
                             "escalated": {}, "stopped.json": {}, "collected.json": {}})
        contract.task_lifecycle(d, "alive")
        self.assertEqual(set(names), set(tp_window.LIFECYCLE_FILES))
        names.clear()
        contract.task_lifecycle(self.tasks / "nowhere", "gone")     # a directory with nothing in it: still only the list
        self.assertTrue(set(names) <= set(tp_window.LIFECYCLE_FILES))

    def test_the_lifecycle_reader_refuses_a_file_that_is_not_in_the_list(self):
        """The guard that makes the test above hold for new code: a read of an uncovered name raises, it does not pass."""
        import contract
        shorter = tuple(n for n in tp_window.LIFECYCLE_FILES if n != "collected.json")
        real = contract.LIFECYCLE_FILES
        contract.LIFECYCLE_FILES = shorter
        self.addCleanup(setattr, contract, "LIFECYCLE_FILES", real)
        d = self.finished("t")
        with self.assertRaises(ValueError) as caught:
            contract.task_lifecycle(d, "alive")
        self.assertIn("collected.json", str(caught.exception))

    def test_a_change_to_any_listed_file_makes_the_helper_read_that_task_again(self):
        counts = self.counting()
        old = self.task("old", state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True)
        self.pad()
        self.refresh(counts, 2)
        self.assertEqual(self.refresh(counts, 2)["evaluate"], 0)       # nothing changed: nothing read
        for name in tp_window.LIFECYCLE_FILES:
            with self.subTest(file=name):
                existed = (old / name).exists()
                (old / name).write_text((old / name).read_text() + " " if existed else "{}")
                self.assertGreaterEqual(self.refresh(counts, 2)["evaluate"], 1, "%s written" % name)
                if not existed:
                    (old / name).unlink()
                    self.assertGreaterEqual(self.refresh(counts, 2)["evaluate"], 1, "%s removed" % name)
                self.refresh(counts, 2)

    def test_the_pane_state_is_part_of_the_fingerprint_too(self):
        counts = self.counting()
        self.task("old", state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True)
        self.pad()
        self.refresh(counts, 2)
        self.assertEqual(self.refresh(counts, 2)["evaluate"], 0)
        self.panes = ""                                                # its pane is gone
        self.assertEqual(self.refresh(counts, 2)["evaluate"], 1)

    def move(self, label, build, change, keep=2):
        """An old task that is not active is outside the window; after `change` it is active and is inside."""
        counts = self.counting()
        old = self.task("old", **build)
        self.pad()
        self.refresh(counts, keep)
        self.assertNotIn("old", self.shown(), "%s: the old task was not outside the window to begin with" % label)
        self.assertEqual(self.refresh(counts, keep)["evaluate"], 0)
        change(old)
        read = self.refresh(counts, keep)
        self.assertIn("old", self.shown(), "%s: the old task did not enter the window" % label)
        self.assertGreaterEqual(read["evaluate"], 1)

    def test_an_old_task_enters_the_window_when_a_capability_is_pending(self):
        self.move("pending-capability.json", dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                  lambda d: (d / "pending-capability.json").write_text("{}"))

    def test_an_old_task_enters_the_window_when_a_pause_is_recorded(self):
        self.move("checkpoints.json", dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                  lambda d: (d / "checkpoints.json").write_text(json.dumps({"events": [PAUSE]})))

    def test_an_old_task_enters_the_window_when_its_reply_loses_the_result_status(self):
        self.move("result.md", dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                  lambda d: (d / "result.md").write_text("I stopped to ask: which one?\nNo status line.\n"))

    def test_an_old_task_enters_the_window_when_the_worker_works_again(self):
        self.move("state.json", dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                  lambda d: (d / "state.json").write_text(json.dumps(WORKING)))

    def test_an_old_task_enters_the_window_when_a_request_file_cannot_be_read(self):
        for name in ("permission-request.json", "capability-request.json"):
            for shape in ("null\n{\"status\": \"pending\"}\n", "{}{}", "{\"status\": \"pen", ""):
                with self.subTest(file=name, shape=shape):
                    self.setUp()
                    self.move(name, dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                              lambda d: (d / name).write_text(shape))

    def test_an_old_task_enters_the_window_when_a_request_entry_exists_but_leads_nowhere(self):
        """A dangling link or a directory is an entry that cannot be read: it differs from no entry, so the cache re-reads."""
        def dangling(d, name):
            os.symlink("missing-request.json", d / name)

        def directory(d, name):
            (d / name).mkdir()
        for name in ("permission-request.json", "capability-request.json"):
            for kind, make in (("dangling", dangling), ("dir", directory)):
                with self.subTest(file=name, entry=kind):
                    self.setUp()
                    self.move(name, dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True),
                              lambda d: make(d, name))

    def test_a_dangling_link_is_not_the_same_stamp_as_no_entry(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "permission-request.json")
        absent = tp_window.stamp(path)
        os.symlink("missing-request.json", path)
        self.assertNotEqual(tp_window.stamp(path), absent)
        self.assertEqual(absent, "-")
        os.unlink(path)
        self.assertEqual(tp_window.stamp(path), absent)

    def test_a_request_entry_that_exists_but_cannot_be_read_is_unreadable_and_a_missing_one_is_absent(self):
        import request_file
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.assertEqual(request_file.read_request(d / "none.json"), (request_file.ABSENT, None))
        os.symlink("missing-request.json", d / "dangling.json")
        os.symlink("loop.json", d / "loop.json")
        (d / "dir.json").mkdir()
        for name in ("dangling.json", "loop.json", "dir.json"):
            self.assertEqual(request_file.read_request(d / name), (request_file.UNREADABLE, None), name)
        (d / "target.json").write_text('{"status":"pending"}')
        os.symlink("target.json", d / "fine.json")
        self.assertEqual(request_file.read_request(d / "fine.json"), (request_file.OBJECT, {"status": "pending"}))
        # a path through a file is no entry
        self.assertEqual(request_file.read_request(d / "target.json" / "inner.json"), (request_file.ABSENT, None))

    def test_an_old_task_enters_the_window_when_a_stop_marker_is_removed_and_a_pause_shows(self):
        for marker in ("stopped.json", "escalated"):
            with self.subTest(marker=marker):
                self.setUp()
                self.move(marker, dict(state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True,
                                       events=[PAUSE], files={marker: {}}),
                          lambda d: (d / marker).unlink())

    def test_an_old_task_enters_the_window_when_its_exit_file_goes_and_it_is_still_working(self):
        self.move("exit", dict(state=WORKING, alive=True, files={"exit": 1}), lambda d: (d / "exit").unlink())

    def test_a_task_that_was_outside_the_window_and_active_stays_in_it_every_refresh(self):
        counts = self.counting()
        self.task("old", state=WORKING, alive=True, files={"pending-capability.json": {}})
        self.pad()
        for _ in range(3):
            self.refresh(counts, 2)
            self.assertIn("old", self.shown())


class RecordRefreshHintsTest(WindowFixture):
    """Finding 2 (the pane's part): a task outside the window whose files changed is named on a C line, so the pane has its
    canonical record (task.md) refreshed, which `tp-contract status` otherwise does for the window's rows only."""

    def build(self):
        counts = self.counting()
        old = self.task("old", state=SETTLED, result="Done.\nRESULT_STATUS: complete\n", alive=True)
        for n in range(3):
            self.finished("new%d" % n)
        return counts, old

    def test_the_first_run_names_nothing_and_an_unchanged_run_names_nothing(self):
        counts, _ = self.build()
        self.refresh(counts, 2)
        self.assertEqual(self.changed(), [])
        self.refresh(counts, 2)
        self.assertEqual(self.changed(), [])

    def test_an_old_task_whose_files_changed_is_named_once(self):
        counts, old = self.build()
        self.refresh(counts, 2)
        (old / "collected.json").write_text("{}")
        self.refresh(counts, 2)
        self.assertEqual(self.changed(), ["old"])
        self.assertNotIn("old", self.shown())
        self.refresh(counts, 2)
        self.assertEqual(self.changed(), [])                       # once: it is cached again

    def test_a_task_that_just_finished_outside_the_window_is_named(self):
        counts = self.counting()
        old = self.task("old", state=WORKING, alive=True)
        for n in range(3):
            self.finished("new%d" % n)
        self.refresh(counts, 2)
        self.assertIn("old", self.shown())                         # working: in the window whatever its age
        self.assertEqual(self.changed(), [])
        (old / "state.json").write_text(json.dumps(SETTLED))
        (old / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
        self.refresh(counts, 2)
        self.assertNotIn("old", self.shown())
        self.assertEqual(self.changed(), ["old"])

    def test_a_task_in_the_window_is_not_named_because_its_row_is_refreshed_with_the_window(self):
        counts, _ = self.build()
        self.refresh(counts, 2)
        (self.tasks / "new2" / "collected.json").write_text("{}")
        self.refresh(counts, 2)
        self.assertEqual(self.changed(), [])

    def test_the_cache_is_not_rewritten_on_every_refresh(self):
        counts, old = self.build()
        self.task("live", state=WORKING, alive=True)
        self.assertEqual(self.refresh(counts, 2)["save_cache"], 1)  # first run: written
        for _ in range(3):
            self.assertEqual(self.refresh(counts, 2)["save_cache"], 0)
        (old / "collected.json").write_text("{}")
        self.assertEqual(self.refresh(counts, 2)["save_cache"], 1)  # a finished task changed
        self.assertEqual(self.refresh(counts, 2)["save_cache"], 0)
        shutil.rmtree(old)
        self.assertEqual(self.refresh(counts, 2)["save_cache"], 1)  # a finished task is gone


class UsageSummaryTest(WindowFixture):
    """tp_window's U lines are the lines of the jq program bin/tp-status ran before (kept in tests/fixtures)."""

    @unittest.skipUnless(shutil.which("jq"), "the old summary was a jq program")
    def test_the_summary_is_what_the_old_jq_program_printed(self):
        z = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        cases = {
            "grouped": {"harness": "pi", "state": "working", "model": "p/beta", "cost": 0.3,
                        "tokens": {"input": 100, "output": 20, "cacheRead": 5000, "cacheWrite": 700},
                        "byModel": {"p/alpha": {"input": 60, "output": 10, "cacheRead": 4000, "cacheWrite": 700, "cost": 0.2},
                                    "p/beta": {"input": 40, "output": 10, "cacheRead": 1000, "cacheWrite": 0, "cost": 0.1}}},
            "claude": {"harness": "claude", "state": "working", "model": "opus", "cost": 0, "costKnown": False,
                       "tokens": {"input": 7, "output": 3, "cacheRead": 2000, "cacheWrite": 0},
                       "byModel": {"opus": {"input": 7, "output": 3, "cacheRead": 2000, "cacheWrite": 0}}},
            "legacy": {"harness": "pi", "state": "settled", "model": "p/beta", "cost": 1,
                       "tokens": {"input": 1000, "output": 500, "cacheRead": 9000, "cacheWrite": 0}},
            "zero": {"harness": "pi", "state": "starting", "tokens": z, "byModel": {}},
            "big": {"harness": "pi", "state": "settled", "model": "p/gamma", "cost": 12.3456789,
                    "tokens": {"input": 2500000, "output": 1234567, "cacheRead": 99999999, "cacheWrite": 1500}},
            "strings": {"harness": "pi", "state": "settled", "model": "p/delta", "tokens": {"input": "x", "output": None}},
            "partial": {"harness": "pi", "model": "p/beta", "cost": 5, "tokens": {"input": 50, "output": 5, "cacheRead": 0, "cacheWrite": 0},
                        "byModel": {"p/beta": {"input": 10, "output": 1, "cacheRead": 0, "cacheWrite": 0, "cost": 1}}},
        }
        for name, state in cases.items():
            self.task(name, state=state, result="Done.\nRESULT_STATUS: complete\n")
        self.task("nostate", state=SETTLED)
        (self.tasks / "nostate" / "state.json").unlink()
        main = self.base / "state" / "main"
        main.mkdir()
        (main / "state.json").write_text(json.dumps({"model": "p/beta", "tokens": {"input": 30, "output": 4, "cacheRead": 0, "cacheWrite": 0}}))
        _, _, _, usage = self.run_window(3)
        self.assertEqual(usage, self.old_summary())
        self.assertIn("total", "\n".join(usage))

    def old_summary(self):
        script = 'source "$1"; tasks="$2"; usage_summary_old'
        run = subprocess.run(["bash", "-c", script, "bash", str(ROOT / "tests/fixtures/usage_summary_old.sh"), str(self.tasks)],
                             text=True, capture_output=True)   # jq stops with a status on a file it refuses; the pane threw that away
        return run.stdout.splitlines()

    @unittest.skipUnless(shutil.which("jq"), "the old summary was a jq program")
    def test_a_state_json_jq_would_refuse_leaves_the_summary_out_as_before(self):
        self.finished("fine")
        (self.tasks / "fine" / "state.json").write_text("{not json")
        _, _, _, usage = self.run_window(3)
        self.assertEqual(usage, [])
        self.assertEqual(self.old_summary(), [])

    def test_no_state_json_at_all_prints_no_summary(self):
        self.finished("fine")
        (self.tasks / "fine" / "state.json").unlink()
        _, _, _, usage = self.run_window(3)
        self.assertEqual(usage, [])

    @unittest.skipUnless(shutil.which("jq"), "the old summary was a jq program")
    def test_the_summary_counts_tasks_outside_the_window(self):
        for n in range(30):
            self.finished("d%02d" % n)
        _, ids, _, small = self.run_window(2)
        _, _, _, large = self.run_window(60)
        self.assertEqual(len(ids), 2)
        self.assertEqual(small, large)
        self.assertEqual(small, self.old_summary())
        self.assertIn("records 30 (main 0, workers 30)", "\n".join(small))


if __name__ == "__main__":
    unittest.main()
