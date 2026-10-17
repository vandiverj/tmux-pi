"""A paused task is never classified as a state that satisfies --after.

tp-agent (queue admission) and tp-contract (slot) classify a task with one function,
contract.lifecycle, fed by one reader of the task's files, contract.task_lifecycle. This table runs
both tools over the cross product of persisted state, exit code, pane (alive / dead / missing), last
checkpoint event, result status and main's markers (stopped.json, escalated, collected.json), and
checks on every row that
- tp-agent returns exactly what tp-contract returns for the same files with the pane present
  (tp-agent does not look at panes; that is the only fact the two do not share);
- a recorded pause, or a reply without a RESULT_STATUS line, never satisfies --after, at admission
  or at slot, whatever the pane and the exit code say.
Every combination is included, including ones that cannot occur in practice (an exit file with a live
pane, a pause event on a task whose state.json says "paused"): a paused record must not satisfy --after
whichever other facts the files hold. Unknown and unreadable predecessor ids refuse at both."""
import contextlib
import importlib.machinery
import importlib.util
import io
import itertools
import json
import os
import subprocess
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT / "lib"))
import contract


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


AGENT = load("tp_agent_under_test", ROOT / "bin/tp-agent")
CONTRACT_CLI = load("tp_contract_under_test", ROOT / "bin/tp-contract")


def queue_item(parent):
    """A valid queue item (tp-agent's queue_item check) that waits on one predecessor: capacity takes only such an item."""
    return {"id": "coder-0000000001", "role": "coder", "worker": "claude", "model": "x/y", "thinking": "low", "task": "t",
            "workdir": "/tmp", "tests": "affected", "route_info": {}, "created": "2026-01-01T00:00:00Z", "deliverable": "",
            "parent": "", "after": [parent], "context": [], "notes": []}


PAUSE = {"action": "pause", "at": 1000, "tools": 3}
EXTEND = {"action": "extend", "at": 2000}
STATES = {  # persisted state.json
    "none": None,                       # no readable state.json
    "paused": {"state": "paused", "tools": 3},
    "waiting": {"state": "waiting", "tools": 3},
    "error": {"state": "error", "tools": 3},
    "working_same_tools": {"state": "working", "tools": 3},
    "working_more_tools": {"state": "working", "tools": 5},  # worked again after the pause: the pause is cleared
}
EXITS = (None, "0", "1")
PANES = ("alive", "dead", "gone")
EVENTS = {"none": [], "pause": [PAUSE], "pause_then_extend": [PAUSE, EXTEND], "extend_then_pause": [EXTEND, PAUSE]}
RESULTS = {"missing": None, "unstatused": "Done, I think.\n",
           "question": "Done, but which file should I change?\n", "statused": "Done.\nRESULT_STATUS: complete\n"}
MARKERS = {"none": None, "collected": "collected.json", "stopped": "stopped.json", "escalated": "escalated"}
SATISFYING = {"settled", "completed", "failed", "killed", "stopped", "escalated"}  # DEPENDENCY_DONE; paused is not among them
AT_WORK = ("none", "working_same_tools", "working_more_tools")


def recorded_pause_expected(state_name, events_name):
    """The rule, restated independently of the implementation."""
    if state_name == "paused":
        return True
    if events_name in ("pause", "extend_then_pause"):
        return state_name != "working_more_tools"
    return False


class PauseClassificationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        (self.base / "tasks").mkdir()
        patcher = mock.patch.dict(os.environ, {"TP_STATE_DIR": str(self.base), "TP_WORK_DIR": str(self.base / "work-store")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, ident, state, exit_code, events, result, marker=None):
        d = self.base / "tasks" / ident
        d.mkdir(exist_ok=True)
        for name in ("state.json", "exit", "checkpoints.json", "result.md", *filter(None, MARKERS.values())):
            (d / name).unlink(missing_ok=True)
        (d / "meta.json").write_text(json.dumps({"id": ident, "worker": "claude", "pane": "%1", "model": "x/y"}))
        if state is not None:
            (d / "state.json").write_text(json.dumps(dict(state, updated="2026-10-08T12:00:00Z")))
        if exit_code is not None:
            (d / "exit").write_text(exit_code + "\n")
        if events:
            (d / "checkpoints.json").write_text(json.dumps({"events": events}))
        if result is not None:
            (d / "result.md").write_text(result)
        if marker:
            (d / marker).write_text("{}\n" if marker.endswith(".json") else "other-task\n")
        return d

    def slot(self, after, pane):
        out = io.StringIO()
        with mock.patch.object(CONTRACT_CLI, "panes", lambda: {} if pane == "gone" else {"%1": pane}), contextlib.redirect_stdout(out):
            CONTRACT_CLI.cmd_slot(types.SimpleNamespace(after=after, model="x/y"))
        return json.loads(out.getvalue())

    def cli(self, d, pane):
        meta = json.loads((d / "meta.json").read_text())
        return CONTRACT_CLI.task_state(d, meta, {} if pane == "gone" else {"%1": pane})[0]

    def classify(self, d, pane):
        return AGENT.lifecycle(d), self.cli(d, pane)

    def test_no_paused_task_satisfies_after_in_any_combination(self):
        cells = itertools.product(STATES, EXITS, PANES, EVENTS, RESULTS, MARKERS)
        counts = {"recorded": 0, "unstatused": 0, "unstatused_no_checkpoint_pane_or_exit": 0, "control": 0, "rows": 0, "marked_unsatisfying": 0}
        for state_name, exit_code, pane, events_name, result_name, marker_name in cells:
            counts["rows"] += 1
            label = f"state={state_name} exit={exit_code} pane={pane} events={events_name} result={result_name} marker={marker_name}"
            d = self.make("dep", STATES[state_name], exit_code, EVENTS[events_name], RESULTS[result_name], MARKERS[marker_name])
            agent, cli = self.classify(d, pane)
            admitted, why = AGENT.capacity(self.base, queue_item("dep"))
            slot = self.slot("dep", pane)
            # The two tools are one classifier: tp-agent equals tp-contract on every row, the pane being the
            # one fact tp-agent does not observe (it is read as present).
            self.assertEqual(agent, self.cli(d, "alive"), label)
            if pane == "alive":
                self.assertEqual(agent, cli, label)
            # The gates follow the classification, on every row.
            self.assertEqual(slot["states"], {"dep": cli}, label)
            recorded = recorded_pause_expected(state_name, events_name)
            unstatused = result_name in ("unstatused", "question")  # a question without a status is still unstatused
            # Admission to a dependent is the shared answer, not membership of a state set: a recorded pause or an
            # unstatused reply never satisfies it, whatever state is reported (main's markers included).
            self.assertEqual(slot["ok"], cli in SATISFYING and not (recorded or unstatused), label)
            if admitted:
                self.assertIn(agent, ("settled", "completed"), label)
                self.assertTrue(slot["ok"], label)
            marked = marker_name in ("stopped", "escalated")
            if marked:
                # Main ended the task itself (tp-kill, or a hand-off to another task): that is the reported state.
                self.assertEqual((agent, cli), (marker_name, marker_name), label)
                counts["marked_unsatisfying"] += recorded or unstatused
            if recorded:
                counts["recorded"] += 1
                if not marked:
                    self.assertEqual((agent, cli), ("paused", "paused"), label)
            if unstatused and marked:
                counts["unstatused"] += 1
            elif unstatused:
                # A reply without a result status, with or without a pause checkpoint, with or without a pane or an
                # exit code, never satisfies --after. It reads paused unless the worker is visibly at work again.
                counts["unstatused"] += 1
                if not recorded and (pane != "alive" or exit_code is not None):
                    counts["unstatused_no_checkpoint_pane_or_exit"] += 1
                if state_name not in AT_WORK or pane != "alive" or exit_code is not None:
                    self.assertEqual(cli, "paused", label)
                if state_name not in AT_WORK or exit_code is not None:
                    self.assertEqual(agent, "paused", label)
                self.assertIn(cli, ("paused", "starting", "working"), label)
                self.assertIn(agent, ("paused", "starting", "working"), label)
            if recorded or unstatused:
                self.assertTrue(marked or {agent, cli}.isdisjoint(SATISFYING), label)
                self.assertFalse(admitted, label)
                self.assertFalse(slot["ok"], label)
            elif state_name == "waiting" and exit_code in (None, "0") and result_name == "statused" and not marked and pane != "gone" \
                    and not (exit_code is None and pane == "dead"):
                # Positive control: a finished, statused reply still satisfies --after (the rule is not "refuse all").
                counts["control"] += 1
                want = "completed" if marker_name == "collected" else "settled"
                self.assertEqual((agent, cli), (want, want), label)
                self.assertTrue(admitted, f"{label}: {why}")
                self.assertTrue(slot["ok"], label)
        self.assertEqual(counts["rows"], 3456)
        self.assertGreater(counts["recorded"], 200)
        self.assertGreater(counts["unstatused"], 400)
        self.assertGreater(counts["unstatused_no_checkpoint_pane_or_exit"], 100)
        self.assertGreater(counts["control"], 8)
        self.assertGreater(counts["marked_unsatisfying"], 100)

    def test_an_unstatused_question_is_paused_never_needs_input(self):
        for pane, exit_code in itertools.product(("alive", "dead", "gone"), (None, "0", "1")):
            d = self.make("dep", STATES["waiting"], exit_code, [], RESULTS["question"])
            self.assertEqual(AGENT.lifecycle(d), "paused", (pane, exit_code))
            self.assertEqual(self.cli(d, pane), "paused", (pane, exit_code))
            self.assertFalse(AGENT.capacity(self.base, queue_item("dep"))[0])
            self.assertFalse(self.slot("dep", pane)["ok"])
        d = self.make("dep", STATES["waiting"], None, [], RESULTS["question"])
        self.assertEqual(AGENT.write_index(self.base)["unstatused"][0]["id"], "dep")

    def test_both_tools_call_the_one_classifier(self):
        """Neither tool keeps a classifier of its own: both go through contract.task_lifecycle."""
        self.assertIs(AGENT.task_lifecycle, contract.task_lifecycle)
        self.assertIs(CONTRACT_CLI.task_lifecycle, contract.task_lifecycle)
        d = self.make("dep", STATES["waiting"], None, [], RESULTS["statused"])
        with mock.patch.object(contract, "lifecycle", return_value="sentinel") as one:
            self.assertEqual(self.classify(d, "alive"), ("sentinel", "sentinel"))
        self.assertEqual(one.call_count, 2)
        for name in ("recorded_pause", "has_result_status"):
            self.assertFalse(hasattr(AGENT, name), name)

    def test_unstatused_reply_without_a_checkpoint_is_paused_whatever_the_pane_and_exit(self):
        # The three cases a review reproduced: {state: waiting}, an unstatused reply, no pause checkpoint.
        for pane, exit_code in (("gone", None), ("dead", "1"), ("dead", None), ("gone", "1"), ("dead", "0"), ("alive", None)):
            d = self.make("dep", STATES["waiting"], exit_code, [], RESULTS["unstatused"])
            self.assertEqual(self.classify(d, pane), ("paused", "paused"), (pane, exit_code))
            slot = self.slot("dep", pane)
            self.assertEqual((slot["ok"], slot["states"]), (False, {"dep": "paused"}), (pane, exit_code))
            self.assertFalse(AGENT.capacity(self.base, queue_item("dep"))[0], (pane, exit_code))
        # Decided before pane and exit handling, in the function itself.
        for pane in (None, "alive", "dead", "gone"):
            for exit_code in (None, "0", "1"):
                self.assertEqual(contract.lifecycle({"state": "waiting"}, pane, reply=RESULTS["unstatused"], exit_code=exit_code),
                                 "paused", (pane, exit_code))

    def test_paused_wins_over_a_clean_exit_and_a_dead_pane(self):
        # The cases a review reproduced: paused state + exit 0 + statused reply; dead pane + pause event + statused reply.
        d = self.make("dep", STATES["paused"], "0", [], RESULTS["statused"])
        self.assertEqual(AGENT.lifecycle(d), "paused")
        self.assertFalse(AGENT.capacity(self.base, queue_item("dep"))[0])
        d = self.make("dep", STATES["waiting"], "0", [PAUSE], RESULTS["statused"])
        self.assertEqual(self.classify(d, "dead"), ("paused", "paused"))
        self.assertFalse(self.slot("dep", "dead")["ok"])
        self.assertEqual(contract.lifecycle({"state": "waiting", "updated": "2026-10-08T12:00:00Z", "tools": 3}, "dead",
                                            [PAUSE], reply=RESULTS["statused"], exit_code="0"), "paused")

    def test_a_later_extend_or_visible_work_clears_the_pause(self):
        d = self.make("dep", STATES["waiting"], "0", EVENTS["pause_then_extend"], RESULTS["statused"])
        self.assertEqual(self.classify(d, "dead"), ("settled", "settled"))
        d = self.make("dep", STATES["working_more_tools"], None, EVENTS["pause"], None)
        self.assertEqual(self.classify(d, "alive"), ("working", "working"))

    def test_a_pause_that_coexists_with_a_request_stays_paused(self):
        # pending-capability.json, or a live permission prompt, does not turn a recorded pause into needs_input.
        d = self.make("dep", STATES["paused"], None, [PAUSE], RESULTS["unstatused"])
        (d / "pending-capability.json").write_text("{}\n")
        self.assertEqual(self.classify(d, "alive"), ("paused", "paused"))
        (d / "pending-capability.json").unlink()
        (d / "permission-request.json").write_text(json.dumps({"status": "pending"}))
        self.assertEqual(self.classify(d, "alive"), ("paused", "paused"))
        d = self.make("dep", {"state": "permission", "tools": 3}, None, [PAUSE], RESULTS["statused"])
        self.assertEqual(self.classify(d, "alive"), ("paused", "paused"))
        self.assertEqual(AGENT.record(d)["status"], "paused")
        # Saturation: many active and pending rows around it; the pause is still in the never-truncated tier
        # (paused/unstatused, which the plugin emits first), and in no truncatable group.
        for i in range(40):
            self.make(f"w{i}", {"state": "working", "tools": 1}, None, [], None)
            self.make(f"p{i}", STATES["waiting"], "0", [], RESULTS["statused"])
        (self.base / "tasks/dep/pending-capability.json").write_text("{}\n")
        index = AGENT.write_index(self.base)
        self.assertEqual([r["id"] for r in index["paused"]], ["dep"])
        for group in ("active", "pending", "unstatused"):
            self.assertNotIn("dep", [r["id"] for r in index[group]], group)
        self.assertGreaterEqual(len(index["active"]), 40)

    def test_pending_capability_file_is_exported_in_list_and_index_rows(self):
        # The real pending-capability.json, written to a task dir, reaches tp-agent's list and index rows as
        # capability_pending (what the plugin shows as [awaits: capability approval pending]); a task without
        # the file exports false.
        d = self.make("dep", STATES["paused"], None, [PAUSE], RESULTS["statused"])
        self.make("plain", STATES["paused"], None, [PAUSE], RESULTS["statused"])
        (d / "pending-capability.json").write_text('{"request": "x"}\n')
        self.assertIs(AGENT.record(d)["capability_pending"], True)
        self.assertIs(AGENT.record(self.base / "tasks/plain")["capability_pending"], False)
        index = AGENT.write_index(self.base)
        rows = {r["id"]: r for r in index["paused"]}
        self.assertIs(rows["dep"]["capability_pending"], True)
        self.assertIs(rows["plain"]["capability_pending"], False)
        self.assertIsNone(rows["dep"]["capability_request"])
        out = subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), "index", "--no-tick"], capture_output=True, text=True,
                             env=dict(os.environ, TP_STATE_DIR=str(self.base)), check=True).stdout
        self.assertIs({r["id"]: r for r in json.loads(out)["paused"]}["dep"]["capability_pending"], True)
        out = subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), "list"], capture_output=True, text=True,
                             env=dict(os.environ, TP_STATE_DIR=str(self.base)), check=True).stdout
        self.assertIs({r["id"]: r for r in json.loads(out)}["dep"]["capability_pending"], True)

    def test_a_stopped_task_reads_stopped_in_both_tools(self):
        # tp-kill on a paused worker: main ended it, so it is not left paused for ever in tp-contract only.
        d = self.make("dep", STATES["paused"], None, [PAUSE], RESULTS["unstatused"], "stopped.json")
        self.assertEqual(self.classify(d, "gone"), ("stopped", "stopped"))

    def test_unknown_and_unreadable_predecessors_refuse_at_both(self):
        admitted, why = AGENT.capacity(self.base, queue_item("nope"))
        self.assertEqual((admitted, why), (False, "dependency nope has not started"))
        slot = self.slot("nope", "alive")
        self.assertFalse(slot["ok"])
        self.assertIn("unknown task nope", slot["reason"])
        self.assertEqual(slot["states"], {"nope": "unknown"})
        for junk in ("not json", "[]", "[1]", "null", ""):
            d = self.make("junk", STATES["waiting"], "0", [], RESULTS["statused"])
            (d / "meta.json").write_text(junk)
            admitted, why = AGENT.capacity(self.base, queue_item("junk"))
            self.assertFalse(admitted, junk)
            self.assertIn("unreadable", why)
            slot = self.slot("junk", "dead")
            self.assertFalse(slot["ok"], junk)
            self.assertEqual(slot["states"], {"junk": "unknown"}, junk)
        # One bad id among good ones refuses the lot.
        self.make("good", STATES["waiting"], "0", [], RESULTS["statused"])
        self.assertFalse(self.slot("good,nope", "dead")["ok"])

    def test_a_recorded_killed_state_is_told_apart_from_a_missing_task(self):
        self.make("gone", STATES["working_same_tools"], None, [], None)
        slot = self.slot("gone", "gone")
        self.assertEqual((slot["ok"], slot["states"]), (True, {"gone": "killed"}))  # killed is a recorded end state
        self.assertFalse(self.slot("never-existed", "gone")["ok"])


if __name__ == "__main__":
    unittest.main()
