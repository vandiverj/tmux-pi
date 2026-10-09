"""Where the slices meet: the paused state, the queue refusals, the pending capability file and the status rows.

Each slice has its own tests; these cover what only holds when two of them run together. tmux is a stub that answers
the state directory and sixteen live panes (%1..%16); nothing here starts a worker.

- tp-list and tp-status draw a capability awaiting human approval (pending-capability.json) the way they draw the
  other two request files, and in the same words (lib/tp_detail.py is tp-status's reader, bin/tp-list has its own).
- A paused task, a task awaiting approval and a refused queue entry sit together in tp-list, `tp-agent list` and the
  session index; the refusal is in the index's active group with its reason.
- A queued task that waits on a paused predecessor stays queued (tick, dispatch) beside a malformed sibling entry,
  and starts once the predecessor has a status.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SEP = "\x1f"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


AGENT = load("tp_agent_for_joins", ROOT / "bin/tp-agent")
PAUSE = {"action": "pause", "at": 1000, "tools": 3}
STATE = {"model": "m", "updated": "2026-10-08T12:00:00Z", "tokens": {"input": 1, "cacheRead": 0, "cacheWrite": 0, "output": 2}}


class JoinFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        (self.state / "tasks").mkdir(parents=True)
        (self.state / "queue").mkdir()
        fake = self.root / "bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
  has-session) exit 0 ;;
  show-options) case "$*" in *@tp_state_dir*) printf '%s\\n' "$TP_STATE_DIR" ;; esac ;;
  list-panes) for n in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do printf '%%%s 0\\n' "$n"; done ;;
esac
exit 0
''')
        tmux.chmod(0o755)
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "joins",
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)
        patcher = mock.patch.dict(os.environ, {"TP_STATE_DIR": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.panes = 0

    def task(self, ident, state="working", result=None, events=(), files=None):
        """A task dir: working (or the given state.json state) in a live pane."""
        self.panes += 1
        d = self.state / "tasks" / ident
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"id": ident, "worker": "pi", "pane": f"%{self.panes}", "model": "", "thinking": ""}))
        (d / "state.json").write_text(json.dumps(dict(STATE, state=state, tools=3)))
        (d / "task.md").write_text(f"task {ident}\n")
        if result is not None:
            (d / "result.md").write_text(result)
        if events:
            (d / "checkpoints.json").write_text(json.dumps({"events": list(events)}))
        for name, content in (files or {}).items():
            (d / name).write_text(content if isinstance(content, str) else json.dumps(content))
        os.utime(d / "meta.json", (1_000_000 + self.panes, 1_000_000 + self.panes))   # oldest first, as tp_rows orders them
        return d

    def queued(self, ident, after=(), **extra):
        item = {"id": ident, "created": "2026-10-08T00:00:00+00:00", "role": "coder", "task": "t", "workdir": "/tmp",
                "worker": "pi", "model": "m", "thinking": "low", "route_info": {}, "deliverable": "", "parent": "",
                "after": list(after), "context": [], "notes": [], "tests": "affected", **extra}
        (self.state / "queue" / f"{ident}.json").write_text(json.dumps(item))
        return item

    def run_tool(self, tool, *args):
        run = subprocess.run([str(ROOT / "bin" / tool), *args], env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        return run.stdout

    def tp_list(self):
        """({id: (status, detail)}, the queue lines) from tp-list's text: a task row is `id worker status ...` with its
        detail on the line below (four spaces in); a queue line is `id role queued ...` or `id queued start refused ...`."""
        rows, queue, last = {}, [], None
        for line in self.run_tool("tp-list").splitlines():
            fields = line.split()
            if line.startswith("    "):
                rows[last] = (rows[last][0], line.strip())
            elif "queued" in fields[1:3]:
                queue.append(line)
            else:
                last = fields[0]
                rows[last] = (fields[2], "")
        return rows, queue


class PendingCapabilityRowTest(JoinFixture):
    def test_tp_list_shows_a_pending_capability_file_like_the_other_request_files(self):
        self.task("by-file", files={"pending-capability.json": {"request": "jq tool"}})
        self.task("by-request", files={"capability-request.json": {"status": "pending", "kind": "tool", "what": "jq"}})
        self.task("by-prompt", files={"permission-request.json": {"status": "pending", "target": "db"}})
        self.task("plain")
        rows, _ = self.tp_list()
        # tp-list words the other two request files its own way (their text, no "asks for" or "permission:"); the
        # approval is a third request line in front of the rest of the detail
        self.assertEqual(rows["by-file"][0], "needs_input")
        self.assertTrue(rows["by-file"][1].startswith("capability approval pending: jq tool · role generic"), rows["by-file"])
        self.assertEqual(rows["by-request"][0], "needs_input")
        self.assertTrue(rows["by-request"][1].startswith("jq · role generic"), rows["by-request"])
        self.assertEqual(rows["by-prompt"][0], "needs_input")
        self.assertTrue(rows["by-prompt"][1].startswith("db · role generic"), rows["by-prompt"])
        self.assertEqual(rows["plain"][0], "working")
        self.assertTrue(rows["plain"][1].startswith("role generic"), rows["plain"])

    def test_a_paused_task_keeps_its_state_beside_a_pending_capability_file(self):
        # The recorded pause decides the state (lib/contract.py); the approval is noted on the row, as for a prompt.
        self.task("paused-and-asking", state="paused", result="Done, I think.\n", events=[PAUSE],
                  files={"pending-capability.json": {"request": "jq tool"}})
        rows, _ = self.tp_list()
        self.assertEqual(rows["paused-and-asking"][0], "paused")
        self.assertTrue(rows["paused-and-asking"][1].startswith("capability approval pending: jq tool · role generic"), rows["paused-and-asking"])

    def test_tp_list_and_the_status_pane_reader_say_the_same_for_every_request_file_shape(self):
        """bin/tp-list and lib/tp_detail.py (tp-status) each work out status and detail; for the same task directories,
        lifecycle text and rows they must agree, whatever the request files hold."""
        shapes = {
            "file-text": {"pending-capability.json": {"request": "Sequential Thinking | reason=tradeoffs"}},
            "file-newline": {"pending-capability.json": {"request": "line one\nline two"}},
            "file-empty-request": {"pending-capability.json": {"request": ""}},
            "file-number": {"pending-capability.json": {"request": 3}},
            "file-no-request": {"pending-capability.json": {}},
            "file-array": {"pending-capability.json": ["x"]},
            "file-broken": {"pending-capability.json": "{not json"},
            "file-and-prompt": {"pending-capability.json": {"request": "a"}, "permission-request.json": {"status": "pending", "target": "t"}},
            "file-and-request": {"pending-capability.json": {"request": "a"}, "capability-request.json": {"status": "pending", "kind": "k", "what": "w"}},
            "answered": {"capability-request.json": {"status": "answered", "kind": "k", "what": "w"}},
        }
        for name, files in shapes.items():
            self.task(name, files=files)
        self.task("paused-file", state="paused", result="Done, I think.\n", events=[PAUSE], files={"pending-capability.json": {"request": "p"}})
        self.task("settled-file", state="waiting", result="Done.\nRESULT_STATUS: complete\n", files={"pending-capability.json": {"request": "s"}})
        listed, _ = self.tp_list()
        rows = subprocess.run(["python3", str(ROOT / "lib/tp_rows.py"), str(self.state / "tasks"), "".join(f"%{n} 0\n" for n in range(1, 17))],
                              text=True, capture_output=True, check=True, env=self.env).stdout
        lifecycle = subprocess.run([str(ROOT / "bin/tp-contract"), "status"], env=self.env, text=True, capture_output=True, check=True).stdout
        detail = subprocess.run(["python3", str(ROOT / "lib/tp_detail.py"), str(self.state / "tasks"), lifecycle], input=rows,
                                text=True, capture_output=True, check=True, env=self.env).stdout
        pane_side = {}
        for row, out in zip(rows.splitlines(), detail.splitlines()):
            status, text = out.split(SEP)[:2]
            pane_side[row.split(SEP)[0]] = (status, text)
        self.assertEqual(set(pane_side), set(listed))
        for ident, expected in pane_side.items():
            with self.subTest(task=ident):
                if ident in ("file-and-prompt", "file-and-request"):
                    # the two readers have always worded the prompt and the request differently; the approval is the
                    # same text, and first, on both
                    self.assertEqual(listed[ident][0], expected[0])
                    self.assertEqual(listed[ident][1].split(" · ")[0], expected[1].split(" · ")[0])
                else:
                    self.assertEqual(listed[ident], expected)
        self.assertTrue(pane_side["paused-file"][1].startswith("capability approval pending: p · role generic"), pane_side["paused-file"])
        self.assertEqual(pane_side["paused-file"][0], "paused")
        self.assertTrue(pane_side["file-no-request"][1].startswith("capability approval pending · role generic"), pane_side["file-no-request"])
        self.assertEqual(pane_side["file-newline"][1].split(" · ")[0], "capability approval pending: line one line two")


class UnreadableRequestTest(JoinFixture):
    """A request file that is not exactly one JSON object shows its task as needing attention, in the words "unreadable
    request", in tp-list, in `tp-contract status` and in the pane's reader, and never aborts the list."""

    SHAPES = {"null-then-object": 'null\n{"status":"pending","target":"db"}',
              "two-objects": '{"status":"pending","target":"a"}\n{"status":"pending","target":"b"}',
              "truncated": '{"status":"pend', "empty": ""}

    def test_tp_list_the_lifecycle_and_the_pane_agree_on_every_shape(self):
        for shape, content in self.SHAPES.items():
            self.task("perm-" + shape, files={"permission-request.json": content})
            self.task("cap-" + shape, files={"capability-request.json": content})
        self.task("neighbour")
        listed, _ = self.tp_list()
        self.assertEqual(len(listed), 2 * len(self.SHAPES) + 1)
        lifecycle = self.run_tool("tp-contract", "status")
        life = {line.split(SEP)[0]: line.split(SEP)[1] for line in lifecycle.splitlines()}
        rows = subprocess.run(["python3", str(ROOT / "lib/tp_rows.py"), str(self.state / "tasks"), "".join(f"%{n} 0\n" for n in range(1, 17))],
                              text=True, capture_output=True, check=True, env=self.env).stdout
        detail = subprocess.run(["python3", str(ROOT / "lib/tp_detail.py"), str(self.state / "tasks"), lifecycle], input=rows,
                                text=True, capture_output=True, check=True, env=self.env).stdout
        pane_side = {row.split(SEP)[0]: tuple(out.split(SEP)[:2]) for row, out in zip(rows.splitlines(), detail.splitlines())}
        for ident in listed:
            with self.subTest(task=ident):
                if ident == "neighbour":
                    self.assertEqual(listed[ident][0], "working")
                    self.assertNotIn("unreadable request", listed[ident][1])
                    continue
                self.assertEqual(life[ident], "needs_input")
                self.assertEqual(listed[ident][0], "needs_input")
                self.assertTrue(listed[ident][1].startswith("unreadable request: "), listed[ident])
                self.assertEqual(listed[ident], pane_side[ident])


    def test_an_entry_that_exists_but_cannot_be_read_is_unreadable_in_tp_list_the_lifecycle_and_the_pane(self):
        for kind in ("dangling", "dir"):
            for key, fname in (("perm", "permission-request.json"), ("cap", "capability-request.json")):
                d = self.task("%s-%s" % (key, kind))
                if kind == "dangling":
                    os.symlink("missing-request.json", d / fname)
                else:
                    (d / fname).mkdir()
        self.task("neighbour")
        listed, _ = self.tp_list()
        self.assertEqual(len(listed), 5)
        lifecycle = self.run_tool("tp-contract", "status")
        life = {line.split(SEP)[0]: line.split(SEP)[1] for line in lifecycle.splitlines()}
        rows = subprocess.run(["python3", str(ROOT / "lib/tp_rows.py"), str(self.state / "tasks"), "".join(f"%{n} 0\n" for n in range(1, 17))],
                              text=True, capture_output=True, check=True, env=self.env).stdout
        detail = subprocess.run(["python3", str(ROOT / "lib/tp_detail.py"), str(self.state / "tasks"), lifecycle], input=rows,
                                text=True, capture_output=True, check=True, env=self.env).stdout
        pane_side = {row.split(SEP)[0]: tuple(out.split(SEP)[:2]) for row, out in zip(rows.splitlines(), detail.splitlines())}
        for ident in listed:
            with self.subTest(task=ident):
                if ident == "neighbour":
                    self.assertEqual(listed[ident][0], "working")
                    self.assertNotIn("unreadable request", listed[ident][1])
                    continue
                self.assertEqual(life[ident], "needs_input")
                self.assertEqual(listed[ident][0], "needs_input")
                self.assertTrue(listed[ident][1].startswith("unreadable request: "), listed[ident])
                self.assertEqual(listed[ident], pane_side[ident])


class StatusOfNamedTasksTest(JoinFixture):
    """The status pane asks `tp-contract status ID...` for the rows it shows, not for every record."""

    def test_named_tasks_get_the_lines_the_whole_listing_gives_them_and_no_others(self):
        self.task("a")
        self.task("b", state="paused", result="Done, I think.\n", events=[PAUSE])
        self.task("c", files={"permission-request.json": {"status": "pending", "target": "db"}})
        self.task("d")
        everything = self.run_tool("tp-contract", "status").splitlines()
        self.assertEqual(sorted(line.split(SEP)[0] for line in everything), ["a", "b", "c", "d"])
        named = self.run_tool("tp-contract", "status", "d", "b").splitlines()
        self.assertEqual(sorted(named), sorted(line for line in everything if line.split(SEP)[0] in ("b", "d")))
        self.assertEqual(self.run_tool("tp-contract", "status", "nope"), "")      # an id that is not a task has no line
        self.assertEqual(self.run_tool("tp-contract", "status", "a", "nope").splitlines(),
                         [line for line in everything if line.startswith("a" + SEP)])


class WaitingRowsTest(JoinFixture):
    def test_paused_awaiting_and_refused_rows_appear_together_in_every_reader(self):
        self.task("dep", state="paused", result="Done, I think.\n", events=[PAUSE])
        self.task("asking", files={"pending-capability.json": {"request": "jq tool"}})
        self.queued("q-waits", after=["dep"], queue_reason="waiting for dep")
        (self.state / "queue" / "q-null.json").write_text("null")
        (self.state / "queue" / "q-broken.json").write_text("{not json")
        rows, queue = self.tp_list()
        self.assertEqual(rows["dep"][0], "paused")
        self.assertEqual(rows["asking"][0], "needs_input")
        self.assertEqual(sorted(line.split()[0] for line in queue), ["q-broken", "q-null", "q-waits"])
        self.assertIn("  q-waits  coder  queued  waiting for dep", queue)
        by_name = {line.split()[0]: line for line in queue}
        for name in ("q-null", "q-broken"):
            self.assertIn(f"  {name}  queued  start refused: ", by_name[name])
        index = json.loads(self.run_tool("tp-agent", "index", "--no-tick"))
        self.assertEqual([r["id"] for r in index["unstatused"]], ["dep"])
        self.assertEqual([r["id"] for r in index["paused"]], [])
        active = {r["id"]: r for r in index["active"]}
        self.assertEqual(active["asking"]["status"], "needs_input")
        self.assertIs(active["asking"]["capability_pending"], True)
        for name in ("q-null", "q-broken"):   # the refusals are active rows carrying their reason, not omitted and not paused
            self.assertEqual(active[name]["status"], "queued")
            self.assertTrue(active[name]["reason"].startswith("start refused: "), active[name])
        self.assertEqual(active["q-waits"]["reason"], "waiting for dep")
        listed = {r["id"]: r for r in json.loads(self.run_tool("tp-agent", "list"))}
        self.assertEqual({k: listed[k]["status"] for k in ("dep", "asking", "q-null", "q-broken", "q-waits")},
                         {"dep": "paused", "asking": "needs_input", "q-null": "queued", "q-broken": "queued", "q-waits": "queued"})
        durable = json.loads((self.state / "session-index.json").read_text())
        self.assertEqual(durable["active"], index["active"])
        self.assertEqual(durable["unstatused"], index["unstatused"])


class QueueBehindAPausedPredecessorTest(JoinFixture):
    def test_tick_leaves_a_dependent_queued_beside_a_malformed_entry_until_the_predecessor_has_a_status(self):
        dep = self.task("dep", state="waiting", result="Done, I think.\n")
        queued = self.queued("q-waits", after=["dep"])
        (self.state / "queue" / "q-broken.json").write_text("{not json")
        launched = []

        def launch(base, item):
            launched.append(item["id"])
            return {"id": item["id"]}

        with mock.patch.object(AGENT, "launch", side_effect=launch):
            first = AGENT.tick(self.state)
            self.assertEqual((launched, first["started"]), ([], []))
            self.assertEqual([r["id"] for r in first["refused"]], ["q-broken"])
            # the reply has no RESULT_STATUS: the predecessor is paused, not done, and the entry is left as it was
            self.assertEqual(AGENT.capacity(self.state, AGENT.queue_item(queued, "q-waits")), (False, "waiting for dep"))
            self.assertEqual(json.loads((self.state / "queue" / "q-waits.json").read_text()), queued)
            self.assertTrue((self.state / "queue" / "q-broken.json").exists())   # a refusal is never deleted
            index = json.loads((self.state / "session-index.json").read_text())
            self.assertEqual([r["id"] for r in index["unstatused"]], ["dep"])
            self.assertEqual({r["id"] for r in index["active"]}, {"q-waits", "q-broken"})
            # a recorded pause holds it too, even when the reply carries a status
            (dep / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
            (dep / "checkpoints.json").write_text(json.dumps({"events": [PAUSE]}))
            self.assertEqual(AGENT.tick(self.state)["started"], [])
            self.assertEqual(launched, [])
            # the pause is cleared (extend) and the reply has a status: the dependent starts, the refusal stays
            (dep / "checkpoints.json").write_text(json.dumps({"events": [PAUSE, {"action": "extend", "at": 2000}]}))
            done = AGENT.tick(self.state)
        self.assertEqual((launched, done["started"]), (["q-waits"], ["q-waits"]))
        self.assertEqual([r["id"] for r in done["refused"]], ["q-broken"])
        self.assertFalse((self.state / "queue" / "q-waits.json").exists())
        self.assertTrue((self.state / "queue" / "q-broken.json").exists())

    def test_dispatch_after_a_paused_task_queues_instead_of_launching(self):
        self.task("dep", state="waiting", result="Done, I think.\n")
        args = argparse.Namespace(role="coder", task="follow up", worker=None, model=None, thinking=None, level=None,
                                  approve_effort=False, local_only=False, workdir="/tmp", deliverable="", parent=None,
                                  after=["dep"], context=[], note=[], tests="affected", preroute=("pi", "m", "low", {}))
        with mock.patch.object(AGENT, "check_workdir", return_value="/tmp"), \
                mock.patch.object(AGENT, "launch", side_effect=AssertionError("launched behind a paused task")):
            value = AGENT.dispatch(args, self.state)
        self.assertEqual((value["status"], value["reason"]), ("queued", "waiting for dep"))
        self.assertEqual(json.loads((self.state / "queue" / f"{value['id']}.json").read_text())["queue_reason"], "waiting for dep")
        index = json.loads((self.state / "session-index.json").read_text())
        self.assertEqual([r["id"] for r in index["unstatused"]], ["dep"])
        self.assertIn(value["id"], [r["id"] for r in index["active"]])

    def test_launch_hands_a_settled_predecessors_result_to_the_dependent_only(self):
        # launch trusts capacity(): the paused cases above never reach it. What it does for an admitted one:
        self.task("dep", state="waiting", result="Done.\nRESULT_STATUS: complete\n")
        item = AGENT.queue_item(self.queued("next", after=["dep"]), "next")
        spawned = []

        def spawn(command, **_):
            spawned.append(command)
            made = self.state / "tasks" / "next"
            made.mkdir(exist_ok=True)
            (made / "meta.json").write_text(json.dumps({"id": "next", "worker": "pi", "pane": "%1"}))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(AGENT.subprocess, "run", side_effect=spawn):
            AGENT.launch(self.state, item)
        command = next(c for c in spawned if "--task-id" in c)  # launch also asks git for the start commit (T12)
        self.assertEqual(command[command.index("--task-id") + 1], "next")
        self.assertIn(f"Dependency dep result: {self.state / 'tasks' / 'dep' / 'result.md'}", command)


class StopWarningTest(JoinFixture):
    """tp-kill ended the process but could not refresh task.md: `tp-agent stop` stays a success and says so."""

    def stop(self, kill_stderr):
        import contextlib
        import io
        self.task("task1")
        done = subprocess.CompletedProcess([], 0, "", kill_stderr)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(AGENT.subprocess, "run", return_value=done) as run, \
                mock.patch.object(sys, "argv", ["tp-agent", "stop", "task1"]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            AGENT.main()
        self.assertEqual(run.call_args[0][0][-1], "task1")
        return json.loads(out.getvalue()), err.getvalue()

    def test_a_refresh_warning_from_tp_kill_is_passed_on_in_stderr_and_in_the_json(self):
        warning = "tp-kill: task.md of task1 was not refreshed: tp-contract status failed"
        value, err = self.stop(warning + "\n")
        self.assertEqual((value["id"], value["status"]), ("task1", "stopped"))
        self.assertEqual(value["warning"], warning)
        self.assertIn(warning, err)

    def test_a_clean_stop_has_no_warning(self):
        value, err = self.stop("")
        self.assertEqual(value, {"id": "task1", "status": "stopped"})
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
