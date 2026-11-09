"""TV1: a Claude worker stopped at Claude Code's folder-trust dialog is reported as needs_input (the folder and the fix), never left as "starting".

One private tmux server (a `-L` socket added by a PATH shim, TMUX_TMPDIR in a temp dir before the first tmux call) holds panes that show the dialog, ordinary
output that merely says "trust", and nothing. The same task records are read through `tp-agent inspect`, `tp-agent list`, and lib/tp_detail.py (the status
pane and tp-list). The shim logs every tmux call, so the tests also show that nothing is ever sent to a pane, and a `capture-pane` that hangs is bounded."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "lib"))
import tp_agent_inproc  # noqa: E402
from session_env import outside_session  # noqa: E402

import trust_dialog  # noqa: E402

SOCKET = "tptrust"
SHIM = """#!/bin/sh
# every call is logged; capture-pane hangs (a grandchild holds the pipe) for the panes TRUST_TEST_HANG lists ("all" for every pane); the server is always the private one
printf '%s\\n' "$*" >> "$TRUST_TEST_LOG"
if [ "$1" = capture-pane ] && [ -n "$TRUST_TEST_HANG" ]; then
  for pane; do :; done   # the target is the last argument
  case " $TRUST_TEST_HANG " in *" all "*|*" $pane "*) sleep 60 ;; esac
fi
exec "$TRUST_TEST_REAL_TMUX" -L {socket} "$@"
"""
DIALOG = """ Accessing workspace:

 {folder}

 Quick safety check: Is this a project you created or one you trust? (Like your own
 code, a well-known open source project, or work from your team). If not, take a
 moment to review what's in this folder first.

 Claude Code'll be able to read, edit, and execute files here.

 Security guide

 ❯ 1. Yes, I trust this folder
   2. No, exit

 Enter to confirm · Esc to cancel
"""
OLD_DIALOG = "Do you trust the files in this folder?\n 1. Yes, proceed\n 2. No, exit\n"
ORDINARY = "verifying trust boundaries\nthe trust store is read only; do not trust user input\nTrust level: high\nthis folder is fine\n"
DIALOG_FOLDER = "/tmp/some-project-wt/lane-x"
LONG_FOLDER = "/private/var/folders/7r/c7qj77bs2n3f24x6zkbdkc_00000gn/T/g3-11091352-1okj-9d25ce-1z3j1jby/some-repository-wt-g3-11091352-1okj-9d25ce"  # wider than the 100-column pane


def wait_for(probe, what, timeout=15):
    """Poll probe() until it is truthy (an event, not a fixed delay)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = probe()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real = shutil.which("tmux")
        if not real:
            raise unittest.SkipTest("tmux is not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="tptrust-")).resolve()
        cls.tmux_tmp = cls.tmp / "tmux"
        cls.tmux_tmp.mkdir()
        cls.log = cls.tmp / "tmux.log"
        cls.log.write_text("")
        (cls.tmp / "bin").mkdir()
        shim = cls.tmp / "bin" / "tmux"
        shim.write_text(SHIM.format(socket=SOCKET))
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
        cls.state = cls.tmp / "state"
        (cls.state / "tasks").mkdir(parents=True)
        cls.env = {**outside_session(), "PATH": f"{cls.tmp / 'bin'}{os.pathsep}{os.environ['PATH']}", "TMUX_TMPDIR": str(cls.tmux_tmp),
                   "TRUST_TEST_LOG": str(cls.log), "TRUST_TEST_REAL_TMUX": real, "TP_STATE_DIR": str(cls.state)}
        cls.screens = {}
        for name, text in (("dialog", DIALOG.format(folder=DIALOG_FOLDER)), ("old", OLD_DIALOG), ("ordinary", ORDINARY),
                           ("long", DIALOG.format(folder=LONG_FOLDER)),
                           ("nofolder", DIALOG.replace(" Accessing workspace:\n\n {folder}\n\n", "")), ("blank", "")):
            screen = cls.tmp / f"{name}.txt"
            screen.write_text(text)
            cls.screens[name] = screen
        cls.panes = {}
        for name, screen in cls.screens.items():
            command = f"cat {screen}; sleep 300"
            if not cls.panes:
                out = cls.tmux("new-session", "-d", "-s", "t", "-x", "100", "-y", "40", "-P", "-F", "#{pane_id}", command)
            else:
                out = cls.tmux("new-window", "-d", "-t", "t:", "-P", "-F", "#{pane_id}", command)
            cls.panes[name] = out.strip()
        for name, text in (("dialog", "Quick safety check"), ("long", "Quick safety check"), ("old", "Do you trust"), ("ordinary", "Trust level")):
            wait_for(lambda pane=cls.panes[name], text=text: text in cls.tmux("capture-pane", "-p", "-t", pane), f"the {name} screen")
        cls.log.write_text("")

    @classmethod
    def tmux(cls, *args):
        return subprocess.run(["tmux", *args], env=cls.env, text=True, capture_output=True, timeout=30, check=True).stdout

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["tmux", "kill-server"], env=cls.env, capture_output=True, timeout=30)
        assert str(cls.tmux_tmp).startswith(tempfile.gettempdir()) or str(cls.tmux_tmp).startswith("/private")
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.log.write_text("")

    def task(self, ident, screen, worker="claude", state="working", workdir="/tmp/the-task-workdir", pane=None, extra=None):
        d = self.state / "tasks" / ident
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps({"id": ident, "worker": worker, "role": "coder", "pane": pane or self.panes[screen], "workdir": workdir}))
        if state:
            (d / "state.json").write_text(json.dumps({"state": state}))
        for name, value in (extra or {}).items():
            (d / name).write_text(json.dumps(value))
        return d

    def with_timeout(self, seconds=None, **more):
        return {**self.env, **more, **({"TP_TRUST_CAPTURE_TIMEOUT": str(seconds)} if seconds else {})}

    def inspect(self, ident, env=None):
        rc, out, err = tp_agent_inproc.run(["inspect", ident], env or self.env, str(self.tmp))
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def listed(self, ident, env=None):
        rc, out, err = tp_agent_inproc.run(["list"], env or self.env, str(self.tmp))
        self.assertEqual(rc, 0, err)
        return next(row for row in json.loads(out) if row["id"] == ident)

    def detail_rows(self, ident, status, listing=False, worker="claude", env=None):
        """What lib/tp_detail.py (the status pane; with listing, bin/tp-list) prints for one row."""
        return self.detail_many([(ident, status)], listing, worker, env)

    def detail_many(self, rows, listing=False, worker="claude", env=None):
        """The same for several rows, in one process: they share a Scanner, as the status pane's rows do."""
        args = ["--list", str(self.state / "tasks"), "", ""] if listing else [str(self.state / "tasks"), ""]
        text = "".join("\x1f".join((ident, worker, status, "", "", "", "m")) + "\n" for ident, status in rows)
        run = subprocess.run([sys.executable, str(ROOT / "lib" / "tp_detail.py"), *args], input=text, text=True, capture_output=True, env=env or self.env, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def tmux_calls(self):
        return [line for line in self.log.read_text().splitlines() if line]


class DetectorTest(unittest.TestCase):
    def test_the_dialog_text_matches_and_a_worker_that_only_says_trust_does_not(self):
        for text in (DIALOG.format(folder="/x"), OLD_DIALOG, "Yes, I trust this folder"):
            self.assertTrue(trust_dialog.shown(text), text)
        for text in (ORDINARY, "", "trust", "I trust the result; the folder is fine\nTrust me", "trusted folders: 3"):
            self.assertFalse(trust_dialog.shown(text), text)

    def test_the_folder_is_the_one_the_dialog_names_else_the_workdir(self):
        self.assertEqual(trust_dialog.folder(DIALOG.format(folder=DIALOG_FOLDER), "/work"), DIALOG_FOLDER)
        self.assertEqual(trust_dialog.folder(DIALOG.format(folder=f"│ {DIALOG_FOLDER} │"), "/work"), DIALOG_FOLDER)
        self.assertEqual(trust_dialog.folder(OLD_DIALOG, "/work"), "/work")
        self.assertEqual(trust_dialog.folder(DIALOG.format(folder="not a path"), "/work"), "/work")

    def test_the_message_names_the_folder_and_the_fix(self):
        message = trust_dialog.message("/a b/c")
        self.assertIn("/a b/c", message)
        self.assertIn("the owner trusts it once: bin/tp-claude-bootstrap '/a b/c'", message)
        for directory in ("/a b/c", "/tmp/$HOME/`id` 'q' \"d\";x", "/plain"):   # the command, run through a shell's splitting, is the directory as one argument
            command = trust_dialog.message(directory).split("the owner trusts it once: ", 1)[1]
            self.assertEqual(shlex.split(command), ["bin/tp-claude-bootstrap", directory])


class InspectTest(Base):
    def test_a_pane_at_the_dialog_is_needs_input_with_the_folder_and_the_fix_in_every_state_it_can_be_starting_or_working(self):
        for state in ("working", None, "starting"):
            with self.subTest(state=state):
                self.task("at-dialog", "dialog", state=state)
                got = self.inspect("at-dialog")
                self.assertEqual(got["status"], "needs_input")
                self.assertEqual(got["trust_dialog"]["dir"], DIALOG_FOLDER)  # the dialog's folder, not the task's workdir
                self.assertIn(f"bin/tp-claude-bootstrap {DIALOG_FOLDER}", got["detail"])
                self.assertIn("the owner trusts it once", got["detail"])
                self.assertIn(DIALOG_FOLDER, got["detail"])

    def test_a_folder_wider_than_the_pane_is_named_whole(self):
        self.assertGreater(len(LONG_FOLDER), 100)
        self.task("wide", "long")
        got = self.inspect("wide")
        self.assertEqual(got["trust_dialog"]["dir"], LONG_FOLDER)
        self.assertIn(f"bin/tp-claude-bootstrap {LONG_FOLDER}", got["detail"])

    def test_the_folder_falls_back_to_the_task_workdir(self):
        self.task("no-folder", "nofolder", workdir="/tmp/fallback-dir")
        got = self.inspect("no-folder")
        self.assertEqual((got["status"], got["trust_dialog"]["dir"]), ("needs_input", "/tmp/fallback-dir"))
        self.task("old-dialog", "old", workdir="/tmp/old-dir")
        self.assertEqual(self.inspect("old-dialog")["trust_dialog"]["dir"], "/tmp/old-dir")

    def test_the_command_in_every_view_is_the_folder_as_one_argument(self):
        """A workdir with a space and a `$`: the printed command, split as a shell would, is exactly the folder (bootstrap-command-quoting)."""
        odd = "/tmp/my project/$HOME/it's"
        self.task("odd-dir", "nofolder", workdir=odd)
        views = {"inspect": self.inspect("odd-dir")["detail"], "list": self.listed("odd-dir")["detail"],
                 "status pane": self.detail_rows("odd-dir", "working"), "tp-list": self.detail_rows("odd-dir", "working", listing=True)}
        for view, text in views.items():
            with self.subTest(view):
                command = text.split("the owner trusts it once: ", 1)[1].split(" · ", 1)[0].strip()
                self.assertEqual(shlex.split(command), ["bin/tp-claude-bootstrap", odd])

    def test_ordinary_output_that_says_trust_changes_nothing(self):
        self.task("ordinary", "ordinary", state="working")
        got = self.inspect("ordinary")
        self.assertEqual(got["status"], "working")
        self.assertNotIn("trust_dialog", got)
        self.assertNotIn("detail", got)
        self.task("blank", "blank", state=None)
        self.assertEqual(self.inspect("blank")["status"], "starting")

    def test_only_a_claude_worker_that_is_starting_or_working_is_looked_at(self):
        self.task("pi-worker", "dialog", worker="pi")
        self.assertNotIn("trust_dialog", self.inspect("pi-worker"))
        self.task("settled", "dialog", state="waiting", extra={"result.md": "x"})
        self.assertNotIn("trust_dialog", self.inspect("settled"))
        self.task("stopped", "dialog", extra={"stopped.json": {}})
        self.assertEqual(self.inspect("stopped")["status"], "stopped")
        self.log.write_text("")
        self.task("gone-pane", "dialog", pane="%9999")
        self.assertEqual(self.inspect("gone-pane")["status"], "working")  # a capture that fails changes nothing

    def test_a_hanging_capture_is_bounded_and_leaves_the_status_alone(self):
        self.task("hang", "dialog")
        t0 = time.monotonic()
        got = self.inspect("hang", self.with_timeout(0.5, TRUST_TEST_HANG="all"))
        took = time.monotonic() - t0
        self.assertEqual(got["status"], "working")
        self.assertNotIn("trust_dialog", got)
        self.assertEqual(got["trust_dialog_check"], "not checked", "a pane that could not be looked at never reads as clear")
        self.assertLess(took, 10, "the shim sleeps 60 s; the capture must be cut at its timeout")

    def test_the_worker_is_never_sent_anything(self):
        self.task("quiet", "dialog")
        self.inspect("quiet")
        self.listed("quiet")
        self.detail_rows("quiet", "working")
        calls = self.tmux_calls()
        self.assertTrue(calls and all(line.startswith("capture-pane") for line in calls), calls)


class ListTest(Base):
    def test_list_and_the_status_views_say_needs_input_with_the_folder_and_the_fix(self):
        self.task("l-dialog", "dialog")
        row = self.listed("l-dialog")
        self.assertEqual((row["status"], row["trust_dialog"]["dir"]), ("needs_input", DIALOG_FOLDER))
        for listing in (False, True):
            with self.subTest(listing=listing):
                out = self.detail_rows("l-dialog", "working", listing=listing)
                self.assertIn("needs_input", out)
                self.assertIn(DIALOG_FOLDER, out)
                self.assertIn(f"the owner trusts it once: bin/tp-claude-bootstrap {DIALOG_FOLDER}", out)
                self.assertNotIn("working", out.split("\x1f")[0] if not listing else out.split("\n")[0].split()[2])

    def test_a_starting_row_is_reported_too(self):
        self.task("l-start", "dialog", state=None)
        self.assertIn("needs_input", self.detail_rows("l-start", "starting"))

    def test_ordinary_output_and_other_rows_are_unchanged_in_every_view(self):
        self.task("l-ordinary", "ordinary")
        self.assertEqual(self.listed("l-ordinary")["status"], "working")
        for listing in (False, True):
            out = self.detail_rows("l-ordinary", "working", listing=listing)
            self.assertIn("working", out)
            self.assertNotIn("needs_input", out)
            self.assertNotIn("tp-claude-bootstrap", out)
        self.task("l-pi", "dialog", worker="pi")
        self.assertNotIn("needs_input", self.detail_rows("l-pi", "working", worker="pi"))

    def test_a_hanging_capture_leaves_every_row_as_it_was(self):
        self.task("l-hang", "dialog")
        env = self.with_timeout(0.5, TRUST_TEST_HANG="all")
        hung = self.listed("l-hang", env)
        self.assertEqual((hung["status"], hung["trust_dialog_check"]), ("working", "not checked"))
        t0 = time.monotonic()
        row = "\x1f".join(("l-hang", "claude", "working", "", "", "", "m")) + "\n"
        run = subprocess.run([sys.executable, str(ROOT / "lib" / "tp_detail.py"), str(self.state / "tasks"), ""], input=row, text=True,
                             capture_output=True, env=env, timeout=60)
        took = time.monotonic() - t0
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertTrue(run.stdout.startswith("working\x1f"), run.stdout)
        self.assertIn("folder-trust dialog not checked", run.stdout)
        self.assertLess(took, 10)


class ScanBoundTest(Base):
    """TV1 round 2 (scan-starvation): one hung pane must not hide a later dialog, and the whole scan stays inside its budget."""

    def hang(self, *names, timeout=0.5, **more):
        return self.with_timeout(timeout, TRUST_TEST_HANG=" ".join(self.panes[n] for n in names), **more)

    def test_a_hung_first_pane_does_not_hide_the_dialog_in_the_next_row_of_list_or_status(self):
        self.task("aa-hang", "ordinary")
        self.task("zz-dialog", "dialog")
        env = self.hang("ordinary")
        rc, out, err = tp_agent_inproc.run(["list"], env, str(self.tmp))
        self.assertEqual(rc, 0, err)
        rows = {row["id"]: row for row in json.loads(out)}
        self.assertEqual([r["id"] for r in json.loads(out)], ["aa-hang", "zz-dialog"], "the hung row comes first")
        self.assertEqual((rows["aa-hang"]["status"], rows["aa-hang"]["trust_dialog_check"]), ("working", "not checked"))
        self.assertEqual((rows["zz-dialog"]["status"], rows["zz-dialog"]["trust_dialog"]["dir"]), ("needs_input", DIALOG_FOLDER))
        self.assertNotIn("trust_dialog_check", rows["zz-dialog"])
        for listing in (False, True):
            with self.subTest(listing=listing):
                lines = self.detail_many([("aa-hang", "working"), ("zz-dialog", "working")], listing, env=env)
                self.assertIn("folder-trust dialog not checked", lines)
                self.assertIn(f"bin/tp-claude-bootstrap {DIALOG_FOLDER}", lines)
                self.assertEqual(lines.count("needs_input"), 1, lines)
                self.assertEqual(lines.count("working"), 1, lines)

    def test_the_whole_list_stays_inside_the_budget_however_many_panes_hang_and_says_which_rows_were_not_looked_at(self):
        count = 10
        for n in range(count):
            self.task(f"hang-{n:02d}", "ordinary")
        self.task("zz-dialog", "dialog")
        env = self.hang("ordinary", timeout=0.5, TP_TRUST_SCAN_BUDGET="1.0")   # unbounded: 10 x 0.5 s = 5 s of hung captures
        t0 = time.monotonic()
        rc, out, err = tp_agent_inproc.run(["list"], env, str(self.tmp))
        took = time.monotonic() - t0
        self.assertEqual(rc, 0, err)
        rows = json.loads(out)
        self.assertLess(took, 3.5, "budget 1 s: the scan stops looking when it is spent")
        self.assertLessEqual(len([c for c in self.tmux_calls() if c.startswith("capture-pane")]), 4, "rows after the budget were not captured")
        self.assertEqual([r["status"] for r in rows if r["id"].startswith("hang-")], ["working"] * count)
        self.assertEqual([r.get("trust_dialog_check") for r in rows if r["id"].startswith("hang-")], ["not checked"] * count)
        self.assertEqual(next(r for r in rows if r["id"] == "zz-dialog").get("trust_dialog_check"), "not checked", "the budget was spent before this row: it says so, it does not look clear")


if __name__ == "__main__":
    unittest.main()
