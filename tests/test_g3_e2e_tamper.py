"""Each record check of tools/smoke/g3_e2e.py fails, at its step and by name, when the record it reads (or the tool answer it trusts) is broken.

`--tamper NAME` (fake mode only) breaks one record just before the check that reads it. TAMPER_EXPECT is the table: tamper -> (the step that
must fail, a piece of the check's description that the error must contain). Two cross-checks keep the table honest: every tamper the script
offers has a row, every row names text that really is in the passing flow's assertions, and every assertion of the passing flow is covered by a
row (or is in UNTAMPERED with the test that covers it instead). The leftover cases leave something on purpose that the leftover check must name."""
import g3_e2e_support as support  # first: it imports session_env, which drops the caller's session

import unittest
from pathlib import Path

from g3_e2e_support import Base, Parallel, g3, records

setUpModule, tearDownModule = support.set_up_module, support.tear_down_module

TAMPER_EXPECT = {
    "not-git": ("setup", "the repository is a git checkout"),
    "dirty-repo": ("setup", "no uncommitted tracked change"),
    "on-scratch-branch": ("setup", "is not the scratch branch"),
    "integration-exists": ("setup", "scratch integration branch does not exist yet"),
    "lane-branch-exists": ("setup", "no lane branch of this name exists"),
    "dispatch-id": ("dispatch", "dispatch returned a task id"),
    "dispatch-queued": ("dispatch", "started, not queued"),
    "dispatch-meta": ("dispatch", "meta.json names the lane and step"),
    "dispatch-pane": ("dispatch", "a pane is recorded"),
    "dispatch-lane": ("dispatch", "lane record: open"),
    "dispatch-worktree": ("dispatch", "the lane worktree exists"),
    "no-prompt": ("prompt", "permission prompt"),
    "prompt-no-reason": ("prompt", "carries a reason"),
    "still-pending": ("decline", "to leave pending after Escape"),
    "authorized": ("decline", "declined request is not authorized"),
    "no-redirect-record": ("decline", "messages.jsonl holds the redirect"),
    "first-never-settles": ("decline", "the first attempt to settle"),
    "first-complete": ("decline", "result_status incomplete"),
    "first-changed": ("decline", "still unchanged"),
    "first-not-stopped": ("retry", "the first attempt is stopped"),
    "retry-same-id": ("retry", "retry started a new task"),
    "retry-meta": ("retry", "retry keeps the lane and step"),
    "task-state": ("retry", "the retry to settle"),
    "retry-incomplete": ("retry", "result_status complete"),
    "retry-extra-file": ("retry", "exactly the one new file"),
    "retry-wrong-line": ("retry", "exactly the requested line"),
    "reviewer-id": ("review", "tp-review printed a reviewer task id"),
    "review-of": ("review", "review-of names the reviewed task"),
    "same-provider": ("review", "provider differs"),
    "independence": ("review", "independence is different_provider"),
    "reviewed-tree": ("review", "names the reviewed tree"),
    "reviewer-never-settles": ("review", "the reviewer to settle"),
    "review-verdict": ("review", "an approving verdict"),
    "review-incomplete": ("review", "has a Coverage section"),
    "close-dirty": ("close", "committed on the lane branch and the tree is clean"),
    "lane-record": ("close", "lane record: the step is closed"),
    "merge-parents": ("merge", "a merge of the previous branch tip and the lane tip"),
    "merge-extra-file": ("merge", "changed exactly one file"),
    "scratch-commit": ("merge", "added file holds exactly the requested line"),
    "main-moved": ("merge", "own branch did not move"),
    "collect-id": ("collect", "returned the task's record"),
    "collect-state": ("collect", "collected tasks are completed"),
    "collect-reaped": ("collect", "reaped.json"),
    "pane-alive": ("collect", "panes of the three tasks are gone"),
    "remove-abandoned": ("remove", "not abandoned"),
    "worktree-back": ("remove", "worktree directory is gone"),
    "lane-back": ("remove", "archived, no longer listed"),
    "leftover-worktree": ("leftovers", "no worktree, branch, lane record or worker pane"),
    "leftover-branch": ("leftovers", "no worktree, branch, lane record or worker pane"),
    "leftover-lane-record": ("leftovers", "no worktree, branch, lane record or worker pane"),
    "leftover-pane": ("leftovers", "no worktree, branch, lane record or worker pane"),
    "session-kept": ("leftovers", "tmux session is gone"),
}
LEFT = {      # a thing left on purpose -> how the leftover check must name it
    "leftover-worktree": "worktree ", "leftover-branch": "branch left-g3-smoke", "leftover-lane-record": "lane record ", "leftover-pane": "sa:g3-left",
}
UNCLEANABLE = ("leftover-worktree", "leftover-branch", "leftover-lane-record")   # not made by the run's own steps: the cleanup reports them, never removes them
UNTAMPERED = {
    "the leftover probes ran to the end": "tests/test_g3_e2e.py BoundedTest (a probe that hangs is a recorded error, and this check fails on it)",
}
WAITS = ["--prompt-wait", "4", "--settle-wait", "4", "--review-wait", "4"]      # the never-settles cases wait this long, not minutes
CASES = {"flow": []}
CASES.update({f"tamper-{name}": ["--tamper", name, *WAITS] for name in g3.TAMPERS})
_PARALLEL = Parallel(CASES, workers=16)


def results():
    return _PARALLEL()


class TamperTest(Base):
    def test_the_table_and_the_script_agree(self):
        self.assertEqual(set(TAMPER_EXPECT), set(g3.TAMPERS))
        for name, (step, _) in TAMPER_EXPECT.items():
            self.assertEqual(g3.TAMPERS[name], step, name)
            self.assertIn(step, g3.STEPS)

    def test_every_row_names_a_check_the_passing_flow_makes_and_every_check_has_a_row(self):
        flow = results()["flow"][1]
        asserted = {step["name"]: step["asserted"] for step in flow["steps"]}
        for name, (step, text) in TAMPER_EXPECT.items():
            with self.subTest(tamper=name):
                self.assertTrue(any(text in a for a in asserted[step]), f"'{text}' is not in the assertions of step {step}: {asserted[step]}")
        for step, checks in asserted.items():
            for check in checks:
                with self.subTest(step=step, check=check):
                    covered = any(s == step and text in check for s, text in TAMPER_EXPECT.values())
                    self.assertTrue(covered or check in UNTAMPERED, f"no tampered-record case breaks: {check}")

    def test_each_broken_record_fails_its_check(self):
        for name, (step, text) in TAMPER_EXPECT.items():
            with self.subTest(tamper=name):
                rc, summary, err, _ = results()[f"tamper-{name}"]
                self.assertEqual(rc, 1, summary.get("error"))
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["failed_step"], step)
                self.assertEqual(summary["steps"][-1]["name"], step)
                self.assertIn(f"step '{step}'", err)
                self.assertIn(text, summary["error"])
                self.assertTrue(summary["cleanup"]["ran"])
                self.assertFalse(Path(summary["repo"]).exists())
                self.assertFalse(Path(summary["tmux_tmpdir"]).exists())
                if name not in UNCLEANABLE:
                    self.assert_clean(summary)

    def test_a_real_leftover_is_named_by_the_check_and_by_the_final_report(self):
        for name, piece in LEFT.items():
            with self.subTest(tamper=name):
                _, summary, err, _ = results()[f"tamper-{name}"]
                self.assertIn(piece, summary["error"], "the leftover check names what was left")
                if name in UNCLEANABLE:       # something the run did not make: the cleanup cannot know to remove it, so it reports it
                    self.assertTrue(any(piece in line for line in summary["cleanup"]["leftovers"] + summary["cleanup"]["errors"]), summary["cleanup"])
                    self.assertIn("cleanup:", err)

    def test_a_session_left_alive_is_named(self):
        _, summary, _, _ = results()["tamper-session-kept"]
        self.assertIn("in session g3-smoke-", summary["error"])

    def test_the_passing_flow_is_clean(self):
        rc, summary, err, _ = results()["flow"]
        self.assertEqual((rc, err), (0, ""))
        self.assert_clean(summary)

    def test_every_run_stopped_its_own_server_once_or_never_started_one(self):
        runs = results()
        calls = records()
        stopped = support.audit(calls, support.RUNS)
        for label, value in runs.items():
            summary = value[1]
            started = any(c["TMUX_TMPDIR"] == summary["tmux_tmpdir"] and "new-session" in c["argv"] for c in calls)
            self.assertEqual(stopped.get(summary["tmux_tmpdir"], 0), 1 if started else 0, label)


if __name__ == "__main__":
    unittest.main()
