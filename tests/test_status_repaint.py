"""Isolated status-pane repaint checks; fake tmux, never a live session."""
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import subprocess
import tempfile
import termios
import time
import tty
import unittest

ROOT = Path(__file__).resolve().parents[1]
ESC = b"\x1b[H"


class StatusPaneFixture(unittest.TestCase):
    raw_slave = True   # False: leave the pty cooked (ECHO, ICANON on) like a real pane

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.state = self.base / "state"
        (self.state / "tasks").mkdir(parents=True)
        self.mockbin = self.base / "bin"
        self.mockbin.mkdir()
        (self.base / "cols").write_text("80\n")
        (self.base / "lines").write_text("24\n")
        self.script("tput", '#!/bin/sh\necho tput >> "$FAKE_BASE/calls"\ncase "$1" in cols|lines) cat "$FAKE_BASE/$1";; esac\n')
        # Every program the pane might start is wrapped, so a test can count what a redraw runs.
        for name in ("grep", "sed", "cat", "jq", "awk", "wc", "python3", "date", "find", "dirname", "cut", "head", "stat", "tr", "sort"):
            real = shutil.which(name)
            if real:
                self.script(name, '#!/bin/sh\necho %s >> "$FAKE_BASE/calls"\nexec %s "$@"\n' % (name, real))
        self.script("tmux", '''#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  show-options)
    case "$*" in
      *"@tp_state_dir"*) printf '%s\\n' "$FAKE_BASE/state" ;;
      *"@tp_viewer_pane"*) echo %9 ;;
      *"@tp_viewing"*) cat "$FAKE_BASE/viewing" 2>/dev/null || true ;;
    esac ;;
  list-panes) echo x >> "$FAKE_BASE/loads"; echo tmux >> "$FAKE_BASE/calls"; printf '%%1 0\\n%%2 0\\n%%3 0\\n%%4 0\\n' ;;
  swap-pane) echo "$*" >> "$FAKE_BASE/swaps" ;;
  set-option) for last; do :; done; echo "$last" > "$FAKE_BASE/viewing" ;;
esac
''')
        self.environ = dict(os.environ, FAKE_BASE=str(self.base),
                        PATH=str(self.mockbin) + os.pathsep + os.environ["PATH"],
                        TERM="xterm", TP_SESSION="fake")
        self.environ.pop("TMUX", None)
        self.environ.pop("TMUX_PANE", None)
        self.environ.pop("TP_STATE_DIR", None)  # the fake session defines its own state dir
        master, slave = pty.openpty()
        if self.raw_slave:
            tty.setraw(slave)
        self.slave = os.dup(slave)   # kept to read the tty mode back; closed in stop()
        self.master = master
        self.proc = subprocess.Popen(self.command(),
                                     stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
                                     env=self.environ, start_new_session=True)
        os.close(slave)
        self.addCleanup(self.stop)

    def command(self):
        return ["bash", str(ROOT / "bin/tp-status")]

    def script(self, name, text):
        path = self.mockbin / name
        path.write_text(text)
        path.chmod(0o755)

    def stop(self):
        # Close the pty first: on macOS a process in this session can remain
        # in the exiting state until its controlling pty is released.
        os.close(self.master)
        os.close(self.slave)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)

    def output(self, seconds=0.5):
        end = time.monotonic() + seconds
        data = b""
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], max(0, end - time.monotonic()))
            if ready:
                try:
                    data += os.read(self.master, 65536)
                except OSError:
                    break
        return data

    def output_until(self, predicate, seconds=5.0):
        """Collect PTY output until a frame meets predicate, with a bounded wait."""
        end = time.monotonic() + seconds
        data = b""
        while time.monotonic() < end:
            ready, _, _ = select.select([self.master], [], [], max(0, end - time.monotonic()))
            if ready:
                try:
                    data += os.read(self.master, 65536)
                except OSError:
                    break
                if predicate(data.split(ESC)[-1]):
                    break
        return data

    def first_frame(self, *needles, seconds=8.0, settle=0.0):
        """Await a complete frame (ends with ESC[J) once every needle has arrived
        (bounded); `settle` optionally keeps reading so a repeated paint shows."""
        data = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (
                all(n in data for n in needles) and data.endswith(b"\x1b[J")):
            data += self.output(0.05)
        return data + self.output(settle) if settle else data

    def wait_for(self, predicate, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            self.output(0.02)
        return predicate()

    def barrier(self):
        """Input is handled in order: toggling the usage page and back, each
        observed in a painted frame, proves earlier input was fully processed."""
        os.write(self.master, b"s")
        self.output_until(lambda frame: b"usage" in frame and b"\x1b[J" in frame)
        os.write(self.master, b"s")
        self.output_until(lambda frame: b"s stats" in frame and b"\x1b[J" in frame)

    def wait_viewing(self, name, seconds=5.0):
        """Poll the fake tmux viewing option until it names `name`, bounded."""
        path = self.base / "viewing"
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if path.exists() and path.read_text().strip() == name:
                return
            self.output(0.02)

    def make_jobs(self, count):
        tasks = self.state / "tasks"
        for number in range(1, count + 1):
            directory = tasks / f"job{number}"
            directory.mkdir()
            (directory / "meta.json").write_text(json.dumps({
                "id": f"job{number}", "worker": "pi", "pane": f"%{number}",
                "model": "", "thinking": ""}))
            (directory / "state.json").write_text(json.dumps({
                "state": "working", "model": "test-model", "tokens": {
                    "input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}))
            (directory / "task.md").write_text(f"task {number}\n")

    def swaps(self):
        path = self.base / "swaps"
        return path.read_text().splitlines() if path.exists() else []

    def click(self, y, x=5, button=0, final="M", wait=0.02):
        os.write(self.master, f"\x1b[<{button};{x};{y}{final}".encode())
        return self.output(wait)

    def click_view(self, y, name, x=5):
        """A click that must view `name`: wait (bounded) for the fake tmux to record it."""
        self.click(y, x=x)
        self.wait_viewing(name)
        self.assertEqual((self.base / "viewing").read_text().strip(), name)

    def row_order(self, first):
        return list(dict.fromkeys(re.findall(rb"job\d", first)))

    def lflag(self):
        return termios.tcgetattr(self.slave)[3]


class StatusRepaintTest(StatusPaneFixture):
    def test_mouse_tracking_lifecycle(self):
        first = self.first_frame(b"\x1b[?1006h")
        self.assertTrue(first.startswith(b"\x1b[?25l\x1b[?1000h\x1b[?1006h"), first[:40])
        self.assertNotIn(b"\x1b[?1000l", first)
        self.proc.terminate()
        tail = self.output_until(lambda frame: b"\x1b[?1000l" in frame)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", tail)
        self.assertEqual(self.proc.wait(timeout=3), 143)

    def test_click_maps_rendered_rows(self):
        self.make_jobs(3)
        first = self.first_frame(b"subagents 3", b"job1", b"job2", b"job3")
        self.assertIn(b"subagents 3", first)
        self.assertNotIn(b"subagents 3/", first)
        order = [name.decode() for name in self.row_order(first)]
        self.assertEqual(sorted(order), ["job1", "job2", "job3"])
        # header=1, blank=2, then row + role detail line per task: 3,5,7.
        for y in (1, 2, 4, 6, 8, 20):
            self.click(y)
        self.click(3, button=64)            # wheel
        self.click(3, button=65)
        self.click(3, button=32)            # drag
        self.click(3, button=2)             # right button
        self.click(3, final="m")            # release
        self.click(3, button=4)             # shift-modified
        # Input is ordered: the first real click below is only handled after every
        # ignored event above, and an unexpected swap would show in the exact list.
        for y, name in zip((3, 5, 7), order):
            self.click_view(y, name, x=17)  # multi-digit coordinate handling
        # Each click swaps the previous task back (restore) then the clicked one in.
        self.assertEqual([line.split()[3] for line in self.swaps()],
                         ["%" + order[0][-1], "%" + order[0][-1], "%" + order[1][-1],
                          "%" + order[1][-1], "%" + order[2][-1]])
        self.assertTrue(all(line.endswith("-t %9") for line in self.swaps()))

    def test_multi_digit_coordinates(self):
        self.make_jobs(2)
        (self.base / "lines").write_text("120\n")
        first = self.first_frame(b"job1", b"job2")
        order = [name.decode() for name in self.row_order(first)]
        self.click_view(5, order[1], x=100)

    def test_partial_and_unknown_escape_sequences_do_nothing(self):
        self.make_jobs(2)
        self.first_frame(b"job1", b"job2")
        os.write(self.master, b"\x1b[<0;5")      # truncated mouse report
        self.output(1.8)                          # reader times out (1s) and drops it, with margin
        os.write(self.master, b"\x1b[3~")         # other CSI, fully consumed
        os.write(self.master, b"\x1b[<0;5;3m")    # release
        self.barrier()
        self.assertEqual(self.swaps(), [])
        self.assertFalse((self.base / "viewing").exists())

    def test_compact_and_clipped_rows(self):
        self.make_jobs(4)
        (self.base / "lines").write_text("5\n")
        first = self.first_frame(b"subagents 3/4")
        self.assertIn(b"subagents 3/4", first)
        order = [name.decode() for name in self.row_order(first)]
        self.assertEqual(len(order), 3)
        # compact: no blank line or details; header=1, rows 2..4, 5 is clipped.
        self.click(1)
        self.click(5)
        self.click(6)
        self.click_view(3, order[1])
        self.assertEqual(len(self.swaps()), 1)      # only the real row click swapped

    def test_queue_and_placeholder_rows_are_not_clickable(self):
        first = self.first_frame(b"subagents 0")
        self.assertIn(b"subagents 0", first)
        self.click(3)
        self.click(2)
        self.barrier()                      # placeholder clicks handled before the queue exists
        queue = self.state / "queue"
        queue.mkdir()
        (queue / "queued1.json").write_text(json.dumps({
            "id": "queued1", "created": "2026-10-08T00:00:00+00:00", "role": "coder", "task": "t", "workdir": "/tmp",
            "worker": "pi", "model": "m", "thinking": "low", "route_info": {}, "deliverable": "", "parent": "",
            "after": [], "context": [], "notes": [], "tests": "affected", "queue_reason": "global worker slots full"}))
        # an entry that is not a complete record shows its file name and why, never as an ordinary queued row
        (queue / "q-null.json").write_text("null")
        (queue / "q-partial.json").write_text(json.dumps({"id": "q-partial", "role": "coder"}))
        (queue / "q-broken.json").write_text("{not json")
        shown = self.output_until(lambda frame: all(n in frame for n in (b"queued1", b"q-null", b"q-partial", b"q-broken")))
        frame = shown.split(ESC)[-1]
        self.assertIn(b"queued1  coder  queued  global worker slots full", frame)
        for name in (b"q-null", b"q-partial", b"q-broken"):
            self.assertRegex(frame, name + rb"  queued  start refused: ")
        self.assertNotIn(b"null  null", frame)
        self.click(3)
        self.click(4)
        self.barrier()
        self.assertEqual(self.swaps(), [])

    def test_idle_no_workers_and_resize(self):
        first = self.first_frame(b"no subagents", settle=0.3)   # a duplicate initial repaint would show here
        self.assertIn(b"no subagents", first)
        self.assertEqual(first.count(ESC), 1)
        self.assertEqual(self.output(1.2), b"")      # a full poll tick stays silent
        (self.state / "main").mkdir()
        (self.state / "main" / "state.json").write_text(json.dumps({
            "model": "main-model", "tokens": {"input": 3, "output": 4}}))
        self.assertIn(b"main-model", self.output_until(lambda frame: b"main-model" in frame))
        self.assertEqual(self.output(1.2), b"")
        (self.base / "lines").write_text("25\n")
        resized = self.output_until(lambda frame: b"\x1b[J" in frame)
        self.assertEqual((resized + self.output(1.2)).count(ESC), 1)

    def test_state_change_and_keyboard_selection(self):
        self.make_jobs(2)
        tasks = self.state / "tasks"
        first = self.output_until(lambda frame: "── job2 ".encode() in frame and b"task 2" in frame)
        self.assertIn(b"job2", first)
        self.assertEqual(self.output(1.2), b"")
        os.write(self.master, b"k")
        moved = self.output_until(lambda frame: "── job1 ".encode() in frame and b"task 1" in frame)
        self.assertIn(b"task 1", moved)
        self.assertEqual(moved.count(ESC), 1)
        self.assertEqual(self.output(1.2), b"")
        (tasks / "job1" / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
        (tasks / "job1" / "state.json").write_text(json.dumps({
            "state": "settled", "model": "test-model", "tokens": {
                "input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}))
        changed = self.output_until(lambda frame: b"settled" in frame and "── job1 ".encode() in frame and b"task 1" in frame)
        self.assertIn(b"settled", changed)
        self.assertEqual(changed.count(ESC), 1)
        self.assertIn(b"task 1", changed)

    def write_state(self, name, state, main=False):
        if main:
            directory = self.state / "main"
            directory.mkdir(exist_ok=True)
        else:
            directory = self.state / "tasks" / name
            directory.mkdir(exist_ok=True)
            (directory / "meta.json").write_text(json.dumps({
                "id": name, "worker": state.get("harness", "pi"), "pane": "%1",
                "model": "", "thinking": ""}))
            (directory / "task.md").write_text(f"task {name}\n")
        (directory / "state.json").write_text(json.dumps(state))

    def test_usage_summary_groups_models_and_keeps_legacy_separate(self):
        z = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        self.write_state("pi1", {"harness": "pi", "state": "working", "model": "p/beta", "cost": 0.3,
            "tokens": {"input": 100, "output": 20, "cacheRead": 5000, "cacheWrite": 700},
            "byModel": {"p/alpha": {"input": 60, "output": 10, "cacheRead": 4000, "cacheWrite": 700, "cost": 0.2},
                        "p/beta": {"input": 40, "output": 10, "cacheRead": 1000, "cacheWrite": 0, "cost": 0.1}}})
        self.write_state("cl1", {"harness": "claude", "state": "working", "model": "opus", "cost": 0,
            "costKnown": False,
            "tokens": {"input": 7, "output": 3, "cacheRead": 2000, "cacheWrite": 0},
            "byModel": {"opus": {"input": 7, "output": 3, "cacheRead": 2000, "cacheWrite": 0}}})
        self.write_state("old1", {"harness": "pi", "state": "settled", "model": "p/beta", "cost": 1,
            "tokens": {"input": 1000, "output": 500, "cacheRead": 9000, "cacheWrite": 0}})
        self.write_state("zero1", {"harness": "pi", "state": "starting", "tokens": z, "byModel": {}})
        self.write_state("nostate", {}, main=False)
        (self.state / "tasks" / "nostate" / "state.json").unlink()
        self.write_state("", {"model": "p/beta", "tokens": {"input": 30, "output": 4, "cacheRead": 0, "cacheWrite": 0}}, main=True)
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", self.first_frame(b"records 5")).decode()
        for needle in ("p/alpha", "p/beta", "opus", "unattributed (legacy/partial",
                       "last reported model: p/beta (not an attribution)"):
            self.assertIn(needle, text)
        self.assertIn("in 60  cache-read 4k  cache-write 700  out 10", text)   # alpha not merged with legacy
        self.assertIn("in 7  cache-read 2k  cache-write 0  out 3  $?", text)   # Claude cost unknown
        self.assertIn("in 1k  cache-read 9k  cache-write 0  out 504  $1", text)   # old1 + legacy main
        self.assertIn("records 5 (main 1, workers 4); zero-token 1; workers with no usage data 1", text)
        # in 100+7+1000+30 = 1137 -> 1.1k; cache-read 5000+2000+9000 = 16k; out 20+3+500+4 = 527
        self.assertIn("total  in 1.1k  cache-read 16k  cache-write 700  out 527", text)
        self.assertRegex(text, r"pi1\s+pi\s+working\s+↑100 c5\.7k ↓20")
        self.assertRegex(text, r"cl1\s+claude\s+working\s+↑7 c2k ↓3\s+\$\?")

    def test_overflow_is_bounded_and_navigation_scrolls(self):
        lines = 16                                             # 20 tasks overflow a 16-line pane (smallest non-compact)
        (self.base / "lines").write_text(f"{lines}\n")        # geometry first: no frame may be painted at 24 lines
        # Publish all tasks at once. The list is ordered by meta.json mtime and the
        # window offset is sticky, so a frame painted while tasks are half written
        # (or before their mtimes are set) would leave a different window behind.
        live, stage = self.state, self.base / "stage"
        (stage / "tasks").mkdir(parents=True)
        self.state = stage
        try:
            now = time.time()
            for number in range(1, 21):
                self.write_state(f"job{number:02d}", {"state": "working", "model": "m",
                    "tokens": {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}})
                os.utime(stage / "tasks" / f"job{number:02d}" / "meta.json", (now + number, now + number))
        finally:
            self.state = live
        (live / "tasks").rmdir()
        os.rename(stage / "tasks", live / "tasks")
        data = self.first_frame(b"/20", b"job20", settle=0.3)
        # Earlier frames (the empty list painted at startup) are fine; the frame that
        # lists every task must be the last one, i.e. the view is stable after it.
        frames = data.split(ESC)[1:]
        first = frames[-1]
        self.assertIn(b"job20", first)
        for frame in frames:
            self.assertLessEqual(frame.count(b"\n"), lines - 1)   # never scrolls the header away
        self.assertIn(b"/20", first)
        shown = list(dict.fromkeys(int(m) for m in re.findall(rb"job(\d\d)", first)))
        self.assertEqual(shown, sorted(shown))
        self.assertEqual(shown[-1], 20)                        # newest selected, in view
        self.assertNotIn(1, shown)
        # click mapping covers visible lines only: row at screen line 3 is shown[0].
        self.click_view(3, f"job{shown[0]:02d}")
        self.click(lines)                                      # beyond the frame
        for _ in range(shown[0] - 1):                          # exactly to job01
            os.write(self.master, b"k")
        # keys are processed one repaint each; wait for the frame showing job01
        data = self.output_until(
            lambda frame: b"job01" in frame and b"job02" in frame and b"\x1b[J" in frame,
            seconds=40)
        frames = data.split(ESC)[1:]
        self.assertTrue(frames)
        for frame in frames:
            self.assertLessEqual(frame.count(b"\n"), lines - 1)
        last = frames[-1]
        self.assertIn(b"job01", last)
        self.assertNotIn(b"job20", last)
        os.write(self.master, b"j")
        again = self.output_until(lambda frame: b"\x1b[J" in frame)
        self.assertLessEqual(again.count(b"\n"), lines - 1)
        self.click_view(3, "job01")

    def assert_keys_only_redraw(self):
        """Keys that only move the selection or flip the usage page repaint from the last load: they read no file and
        start no process (every program the pane might start is wrapped and logged by the fixture)."""
        loads, calls = self.base / "loads", self.base / "calls"
        count = lambda path: len(path.read_text().splitlines()) if path.exists() else 0
        for attempt in range(6):
            # Start just after a load has finished, so the 1 s timer cannot fire during the keys.
            seen = count(loads)
            self.assertTrue(self.wait_for(lambda: count(loads) > seen, 3.0), "no timer load seen")
            time.sleep(0.45)
            loads_before, calls_before = count(loads), count(calls)
            for _ in range(10):
                for key in (b"k", b"k", b"j", b"s", b"s", b"j", b"k"):
                    os.write(self.master, key)
            self.barrier()                   # every key above is handled before this returns
            if count(loads) == loads_before:
                break
        else:
            self.fail("the timer kept firing during the keys")
        extra = calls.read_text().splitlines()[calls_before:]
        self.assertEqual(extra, [], "a key-only redraw ran programs: %r" % extra[:10])

    def test_keys_that_only_move_redraw_without_reading_or_running_anything(self):
        self.make_jobs(6)
        for n in range(1, 7):   # every row shows the lines the selected row needs: a task, a reply, a pending request
            d = self.state / "tasks" / ("job%d" % n)
            (d / "task.md").write_text("\n  task line %d\nmore\n" % n)
            (d / "result.md").write_text("reply line %d\n" % n)
        (self.state / "tasks" / "job2" / "permission-request.json").write_text(
            json.dumps({"status": "pending", "target": "~/.ssh"}))
        self.first_frame(b"job6", settle=0.2)
        self.assert_keys_only_redraw()
        os.write(self.master, b"k")
        moved = self.output_until(lambda frame: b"\x1b[J" in frame)
        self.assertIn("── job".encode(), moved)    # the redraw still shows the moved selection
        self.assertIn(b"task line", moved)
        self.assertIn(b"reply line", moved)

    def make_waiting_world(self):
        """The places the slices meet: job1 paused (an unstatused reply), job2 waiting on a human capability approval
        (pending-capability.json), job3 with a pending capability request, job4 with a pending permission prompt; beside
        them a refused queue entry, a malformed one and a valid entry that waits on the paused job1."""
        self.make_jobs(4)
        (self.base / "lines").write_text("40\n")
        tasks = self.state / "tasks"
        state = {"model": "test-model", "tokens": {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}
        (tasks / "job1" / "state.json").write_text(json.dumps(dict(state, state="paused", tools=3)))
        (tasks / "job1" / "result.md").write_text("Done, I think.\n")
        (tasks / "job1" / "checkpoints.json").write_text(json.dumps({"events": [{"action": "pause", "at": 1000, "tools": 3}]}))
        (tasks / "job2" / "pending-capability.json").write_text(json.dumps({"request": "jq tool"}))
        (tasks / "job3" / "capability-request.json").write_text(json.dumps({"status": "pending", "kind": "tool", "what": "jq"}))
        (tasks / "job4" / "permission-request.json").write_text(json.dumps({"status": "pending", "target": "db"}))
        queue = self.state / "queue"
        queue.mkdir()
        (queue / "q-waits.json").write_text(json.dumps({
            "id": "q-waits", "created": "2026-10-08T00:00:00+00:00", "role": "coder", "task": "t", "workdir": "/tmp",
            "worker": "pi", "model": "m", "thinking": "low", "route_info": {}, "deliverable": "", "parent": "",
            "after": ["job1"], "context": [], "notes": [], "tests": "affected", "queue_reason": "waiting for job1"}))
        (queue / "q-null.json").write_text("null")
        (queue / "q-broken.json").write_text("{not json")

    WAITING = (b"job4", b"q-waits", b"q-null", b"q-broken", b"capability approval pending")

    def test_paused_and_awaiting_rows_sit_with_refused_queue_entries(self):
        self.make_waiting_world()
        frame = self.first_frame(*self.WAITING, settle=0.3).split(ESC)[-1]
        plain = [re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", line).replace("\u25b8".encode(), b" ").strip() for line in frame.split(b"\n")]
        rows = {name: line for name in (b"job1", b"job2", b"job3", b"job4") for line in plain if line.startswith(name + b" ")}
        self.assertIn(b"paused", rows[b"job1"])
        self.assertIn(b"needs_input", rows[b"job2"])    # a human capability approval holds the worker at needs_input
        self.assertIn(b"needs_input", rows[b"job3"])
        self.assertIn(b"needs_input", rows[b"job4"])
        self.assertIn(b"capability approval pending: jq tool", frame)
        self.assertIn(b"asks for tool: jq", frame)
        self.assertIn(b"permission: db", frame)
        # the queue lines come after the task rows: the entry waiting on the paused job1, and one line per refusal
        self.assertIn(b"q-waits  coder  queued  waiting for job1", frame)
        for name in (b"q-null", b"q-broken"):
            self.assertRegex(frame, name + rb"  queued  start refused: ")
        self.assertLess(frame.index(b"job4"), frame.index(b"q-waits"))
        self.assertLess(frame.index(b"job4"), frame.index(b"q-null"))
        self.assertIn(b"subagents 4", frame)
        for y in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):    # only the four task rows are clickable, never a queue line
            self.click(y)
        self.barrier()
        self.assertTrue(set(line.split()[3] for line in self.swaps()) <= {"%1", "%2", "%3", "%4"})

    def test_keys_redraw_paused_awaiting_and_refused_rows_without_reading_or_running_anything(self):
        self.make_waiting_world()
        self.first_frame(*self.WAITING, settle=0.2)
        self.assert_keys_only_redraw()
        self.output(0.3)    # frames the pane painted for the keys above may still be unread; they are not the answer to this key
        os.write(self.master, b"k")
        needles = (b"paused", b"capability approval pending", b"start refused: ")
        moved = self.output_until(lambda frame: b"\x1b[J" in frame and all(n in frame for n in needles))
        last = moved.split(ESC)[-1]
        for needle in needles:
            self.assertIn(needle, last)

    def test_request_and_reply_lines_come_from_the_load_for_every_row(self):
        self.make_jobs(3)
        for n in (1, 2, 3):
            (self.state / "tasks" / ("job%d" % n) / "task.md").write_text("task text %d\n" % n)
        (self.state / "tasks" / "job1" / "result.md").write_text("\n\nreply text 1\n")
        (self.state / "tasks" / "job2" / "permission-request.json").write_text(
            json.dumps({"status": "pending", "target": "~/.ssh"}))
        self.first_frame(b"job3", settle=0.2)
        os.write(self.master, b"k")
        os.write(self.master, b"k")
        seen = self.output_until(lambda frame: b"reply text 1" in frame)
        self.assertIn(b"task text 1", seen)
        self.assertIn(b"permission: ~/.ssh", self.output(0.1) + seen)

    def test_stats_page_toggle_is_bounded_and_keeps_list_semantics(self):
        self.write_state("pi1", {"harness": "pi", "state": "working", "model": "p/a",
            "tokens": {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8},
            "byModel": {"p/a": {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8, "cost": 0.5}}})
        (self.base / "lines").write_text("12\n")          # short pane: summary not inline
        first = self.first_frame(b"s stats")
        self.assertIn(b"s stats", first)
        self.assertNotIn(b"session tokens", first)
        os.write(self.master, b"s")
        page = self.output_until(lambda frame: b"usage" in frame and b"\x1b[J" in frame)
        text = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", page).decode()
        self.assertIn("usage", text)
        self.assertIn("p/a", text)
        self.assertIn("in 5  cache-read 7  cache-write 8  out 6  $0.5", text)
        self.assertNotIn("pi1  ", text)
        self.assertLessEqual(page.count(b"\n"), 11)
        self.click(2)                                     # no rows on the usage page
        os.write(self.master, b"\r")
        os.write(self.master, b"s")                       # ordered after the ignored input
        back = self.output_until(lambda frame: b"pi1" in frame and b"\x1b[J" in frame)
        self.assertIn(b"pi1", back)
        self.assertEqual(self.swaps(), [])
        self.click_view(2, "pi1")                         # compact pane: header, then rows

    def test_stats_page_overflow_is_counted_not_scrolled(self):
        self.write_state("pi1", {"harness": "pi", "state": "working", "model": "p/a",
            "tokens": {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8}})
        (self.base / "lines").write_text("5\n")
        self.first_frame(b"pi1")
        os.write(self.master, b"s")
        small = self.output_until(lambda frame: b"more lines" in frame and b"\x1b[J" in frame)
        self.assertIn(b"more lines", small)
        self.assertLessEqual(small.count(b"\n"), 4)

    def test_inline_summary_lines_are_clipped_to_pane_width(self):
        long = "provider/" + "very-long-model-name-" * 6
        self.write_state("pi1", {"harness": "pi", "state": "working", "model": long,
            "tokens": {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8},
            "byModel": {long: {"input": 5, "output": 6, "cacheRead": 7, "cacheWrite": 8, "cost": 0.5}}})
        first = self.first_frame(b"session tokens")
        self.assertIn(b"session tokens", first)
        plain = re.sub(rb"\x1b\[[0-9;?]*[A-Za-z]", b"", first).decode()
        self.assertTrue(all(len(line) <= 80 for line in plain.split("\n")))
        self.assertLessEqual(first.count(b"\n"), 23)


class StatusEchoTtyTest(StatusPaneFixture):
    """A cooked, echoing pty: what the status pane really gets from tmux."""
    raw_slave = False

    def command(self):
        # A parent shell owns the tty and outlives the status process, as in a
        # pane: it records the tty mode the status process left behind.
        return ["bash", "-c", 'exec 3<&0; bash "$0" <&3 & echo $! > "$FAKE_BASE/pid"; wait $!; '
                'echo $? > "$FAKE_BASE/rc.tmp"; stty -a > "$FAKE_BASE/stty_after"; '
                'mv "$FAKE_BASE/rc.tmp" "$FAKE_BASE/rc"', str(ROOT / "bin/tp-status")]

    def signal_status(self, number):
        """Signal the status process; return (output, exit status, tty flags left to the parent)."""
        os.kill(int((self.base / "pid").read_text()), number)
        tail = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (self.base / "rc").exists():
            tail += self.output(0.05)
        flags = (self.base / "stty_after").read_text().split()
        return tail, int((self.base / "rc").read_text()), flags

    def test_wheel_flood_is_not_echoed_and_click_still_maps(self):
        self.make_jobs(3)
        self.assertTrue(self.lflag() & termios.ECHO)          # the fixture really echoes
        first = self.first_frame(b"job1", b"job2", b"job3")
        order = [name.decode() for name in self.row_order(first)]
        self.assertEqual(sorted(order), ["job1", "job2", "job3"])
        self.assertFalse(self.lflag() & termios.ECHO)         # off between reads too
        start = time.monotonic()
        data = b""
        for burst in range(12):                               # 600 wheel reports
            for number in range(50):
                os.write(self.master, f"\x1b[<{64 + number % 2};56;{3 + number % 9}M".encode())
            data += self.output(0.05)
        os.write(self.master, b"\x1b[<0;17;5M\x1b[<0;17;5m")  # press + release on the second row
        path = self.base / "viewing"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not path.exists():
            data += self.output(0.05)
        elapsed = time.monotonic() - start
        data += self.output(0.6)
        self.assertNotRegex(data, rb"<\d+;\d+;\d+[Mm]")       # no raw SGR, as ^[[<.. or bytes
        self.assertNotIn(b"^[", data)
        self.assertNotIn(b"\x07", data)                       # no input-queue overflow bell
        self.assertTrue(path.exists(), "press after the flood was lost or still queued")
        self.assertEqual(path.read_text().strip(), order[1])
        self.assertEqual(len(self.swaps()), 1)                # the wheel viewed nothing
        self.assertLess(elapsed, 8, "flood left an input backlog")
        frames = data.split(ESC)[1:]
        self.assertLessEqual(len(frames), int(elapsed + 0.6) + 3)   # bounded repaint, not per event
        for frame in frames:
            self.assertLessEqual(frame.count(b"\n"), 23)      # nothing scrolls the pane
        self.assertEqual(data.split(ESC)[0], b"")             # nothing printed outside frames
        self.assertFalse(self.lflag() & termios.ECHO)
        tail, status, flags = self.signal_status(15)
        self.assertIn(b"\x1b[?1006l\x1b[?1000l", tail)
        self.assertEqual(status, 143)
        self.assertIn("echo", flags)                          # previous mode restored on exit
        self.assertIn("icanon", flags)

    def test_keyboard_enter_and_mode_restore_on_hangup_signal(self):
        self.make_jobs(2)
        first = self.first_frame(b"job1", b"job2")
        order = [name.decode() for name in self.row_order(first)]
        os.write(self.master, b"k")
        marker = f"── {order[0]} ".encode()
        moved = self.output_until(lambda frame: marker in frame)
        self.assertNotIn(b"k\x1b", moved)                     # the key itself is not echoed
        os.write(self.master, b"\r")                          # cooked tty: ICRNL delivers Enter
        self.wait_viewing(order[0])
        self.assertEqual((self.base / "viewing").read_text().strip(), order[0])
        _, status, flags = self.signal_status(1)              # SIGHUP
        self.assertEqual(status, 143)
        self.assertIn("echo", flags)
        self.assertIn("icanon", flags)


if __name__ == "__main__":
    unittest.main()
