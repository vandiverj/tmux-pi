"""Each record check of tools/smoke/g3_e2e.py fails, at its step and by name, when the record it reads (or the tool answer it trusts) is broken.

`--tamper NAME` (fake mode only) breaks one record just before the check that reads it. TAMPER_EXPECT is the table: tamper -> (the step that
must fail, a piece of the check's description that the error must contain). Two cross-checks keep the table honest: every tamper the script
offers has a row, every row names text that really is in the passing flow's assertions, and every assertion of the passing flow is covered by a
row (or is in UNTAMPERED with the test that covers it instead). The leftover cases leave something on purpose that the leftover check must name."""
import session_env  # noqa: F401  first: drops the caller's session (g3_e2e_support, once cached, no longer does)
import g3_e2e_support as support

import json
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from g3_e2e_support import Base, NOTHING_MADE, Parallel, g3, records

setUpModule = support.set_up_module

TAMPER_EXPECT = {
    "not-git": ("setup", "the repository is a git checkout"),
    "bad-lane-config": ("setup", "lane config"),
    "dirty-repo": ("setup", "no uncommitted tracked change"),
    "on-scratch-branch": ("setup", "is not the scratch branch"),
    "integration-exists": ("setup", "scratch integration branch does not exist yet"),
    "lane-branch-exists": ("setup", "no lane branch of this name exists"),
    "lane-record-exists": ("setup", "no lane record of this name exists"),
    "worktree-exists": ("setup", "nothing is registered or exists at the configured worktree path"),
    "session-exists": ("setup", "no tmux session of this name exists"),
    "pinode-mismatch": ("setup", "tmux session carries the caller's TP_PI_NODE"),
    "dispatch-id": ("dispatch", "dispatch returned a task id"),
    "dispatch-queued": ("dispatch", "started, not queued"),
    "dispatch-meta": ("dispatch", "meta.json names the lane and step"),
    "dispatch-pane": ("dispatch", "a pane is recorded"),
    "dispatch-lane": ("dispatch", "lane record: open"),
    "dispatch-worktree": ("dispatch", "the lane worktree exists"),
    "no-prompt": ("prompt", "permission prompt"),
    "prompt-no-reason": ("prompt", "carries a reason"),
    "trust-dialog": ("prompt", "folder-trust dialog"),
    "still-pending": ("decline", "to leave pending after Escape"),
    "authorized": ("decline", "declined request is not authorized"),
    "no-redirect-record": ("decline", "messages.jsonl holds the redirect"),
    "settled-odd": ("decline", "is declined (by the redirect's turn) or denied"),
    "declined-no-event": ("decline", "carries the event that settled it"),
    "unconfirmed-redirect": ("decline", "the redirect was confirmed submitted"),
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
    "pane-alive": ("collect", "no task pane is still running"),
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
    "leftover-worktree": "worktree /", "leftover-branch": "branch lane/g3-", "leftover-lane-record": "lane record ", "leftover-pane": "sa:g3-left",
}      # the worktree and the branch are at the exact path and name the lane had: the run made those, so its cleanup removes them again
UNCLEANABLE = ("leftover-lane-record",)   # a record directory without a lane.json: the cleanup reports it (lane remove --abandon cannot take it), never deletes it
COLLISIONS = ("integration-exists", "lane-branch-exists", "lane-record-exists", "worktree-exists", "session-exists")   # found, not made: they must survive
UNTAMPERED = {
    "the leftover probes ran to the end": "tests/test_g3_e2e.py BoundedTest (a probe that hangs is a recorded error, and this check fails on it)",
}
WAITS = ["--prompt-wait", "4", "--settle-wait", "4", "--review-wait", "4"]      # the never-settles cases wait this long, not minutes
CASES = {"flow": []}
CASES.update({f"tamper-{name}": ["--tamper", name, *WAITS, *(["--keep"] if name in COLLISIONS else [])] for name in g3.TAMPERS})   # --keep: the temp repo stays, to be inspected
_PARALLEL = Parallel(CASES, workers=16)


def results():
    return _PARALLEL()


def tear_down_module():
    for name in COLLISIONS:       # the collision runs kept their temp directory (--keep) for the survival checks
        value = _PARALLEL.done.get(f"tamper-{name}")
        if value and value[1].get("tmp"):
            shutil.rmtree(value[1]["tmp"], ignore_errors=True)
    support.tear_down_module()


tearDownModule = tear_down_module


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
                self.assertEqual(Path(summary["repo"]).exists(), name in COLLISIONS)      # a collision run keeps its repo (--keep) to be inspected
                self.assertFalse(Path(summary["tmux_tmpdir"]).exists())
                if name not in UNCLEANABLE + COLLISIONS:
                    self.assert_clean(summary)

    def test_a_real_leftover_is_named_by_the_check_and_by_the_final_report(self):
        for name, piece in LEFT.items():
            with self.subTest(tamper=name):
                _, summary, err, _ = results()[f"tamper-{name}"]
                self.assertIn(piece, summary["error"], "the leftover check names what was left")
                if name in UNCLEANABLE:       # something the run did not make: the cleanup cannot know to remove it, so it reports it
                    self.assertTrue(any(piece in line for line in summary["cleanup"]["leftovers"] + summary["cleanup"]["errors"]), summary["cleanup"])
                    self.assertIn("cleanup:", err)

    def test_a_worker_at_the_trust_dialog_fails_the_prompt_step_at_once_naming_the_folder_and_the_fix(self):
        """TV1: the fake worker pane shows Claude Code's folder-trust screen for the lane worktree. Step prompt fails with the owner's fix, not
        with "no permission prompt ... within N s", the dialog is never answered (no key sent to the pane), and the cleanup leaves nothing."""
        rc, summary, err, _ = results()["tamper-trust-dialog"]
        worktree = summary["names"]["lane_worktree"]
        self.assertEqual(summary["failed_step"], "prompt")
        self.assertIn(f"worker stopped at Claude Code's folder-trust dialog for {worktree}; the owner runs bin/tp-claude-bootstrap {worktree} once, then reruns", summary["error"])
        self.assertNotIn("within", summary["error"])
        step = summary["steps"][-1]
        self.assertEqual(step["name"], "prompt")
        self.assertLess(step["seconds"], 4, "--prompt-wait was 4 s: failing at once is faster than waiting it out")
        self.assertFalse([c for c in records() if c["TMUX_TMPDIR"] == summary["tmux_tmpdir"] and "send-keys" in c["argv"]], "the dialog is the owner's to answer")
        self.assert_clean(summary)

    def test_the_bootstrap_command_the_trust_dialog_failure_prints_is_one_quoted_argument(self):
        """TV1 round 2 (bootstrap-command-quoting): the owner pastes `bin/tp-claude-bootstrap <dir>`; a folder with a space or a shell character
        must come back from shlex.split as exactly that one argument. Smoke.inspect is called in-process with a canned reply: no tmux, no process."""
        smoke = g3.Smoke(g3.parse(["--fake", "--out", str(self.out)]))
        self.addCleanup(shutil.rmtree, smoke.tmp, ignore_errors=True)
        self.addCleanup(shutil.rmtree, smoke.tmux_tmp, ignore_errors=True)
        smoke.step_name = "prompt"
        for folder in ("/plain/dir", "/tmp/my project", "/tmp/a$HOME `id` 'q' \"d\" ;rm -rf x", "/tmp/$(touch pwned)/w", "/tmp/it's"):
            with self.subTest(folder=folder):
                reply = {"status": "needs_input", "trust_dialog": {"dir": folder}}
                with mock.patch.object(smoke, "agent_json", return_value=reply), self.assertRaises(g3.SmokeError) as raised:
                    smoke.inspect("coder-x")
                message = raised.exception.message
                self.assertTrue(message.startswith(f"worker stopped at Claude Code's folder-trust dialog for {folder}; the owner runs "), message)
                command = message.split("; the owner runs ", 1)[1].rsplit(" once, then reruns", 1)[0]
                self.assertEqual(shlex.split(command), ["bin/tp-claude-bootstrap", folder])

    def test_a_session_left_alive_is_named(self):
        _, summary, _, _ = results()["tamper-session-kept"]
        self.assertIn(f"in session {summary['run']}", summary["error"])

    def git(self, summary, *args):
        return subprocess.run(["git", "-C", summary["repo"], *args], text=True, capture_output=True, timeout=60).stdout.strip()

    def test_a_name_already_taken_is_refused_and_what_was_found_survives(self):
        """F2 (round 3): setup refuses before it makes anything, and the cleanup removes only what the run made, so the colliding resource is
        still there afterwards, unchanged. The collision runs kept their temp repo (--keep) so this can look."""
        for name in COLLISIONS:
            with self.subTest(tamper=name):
                _, summary, err, _ = results()[f"tamper-{name}"]
                self.assertEqual(summary["failed_step"], "setup")
                self.assertEqual(summary["made"], NOTHING_MADE)
                self.assertEqual(summary["cleanup"]["errors"], [], "the cleanup removed nothing, so there was nothing for it to fail at")
                self.assertTrue(Path(summary["repo"]).is_dir())
        # the integration branch that was already there: present, still at the commit it was made at
        _, summary, _, _ = results()["tamper-integration-exists"]
        self.assertEqual(self.git(summary, "rev-parse", "lane/integrate"), self.git(summary, "rev-parse", "main"))      # the default config's name
        for piece in ("lane/integrate", "wait for the other run", "remove the branch yourself"):         # the refusal names the branch and says what to do
            self.assertIn(piece, summary["error"])
        self.assertEqual(summary["names"]["integration_branch"], "lane/integrate")
        # someone's lane branch holding a commit that is on no other branch: present at that commit
        _, summary, _, _ = results()["tamper-lane-branch-exists"]
        branch = f"lane/{summary['run']}"
        self.assertEqual(self.git(summary, "log", "-1", "--format=%s", branch), g3.FOREIGN_COMMIT)
        sha = self.git(summary, "rev-parse", branch)
        self.assertNotEqual(sha, self.git(summary, "rev-parse", "main"))
        self.assertEqual(self.git(summary, "branch", "--contains", sha, "--format=%(refname:short)").split(), [branch])
        self.assertTrue(any(f"branch {branch}" in line for line in summary["cleanup"]["leftovers"]), "reported as a leftover, not removed")
        # someone's lane record: the same file with the same content
        _, summary, _, _ = results()["tamper-lane-record-exists"]
        common = (Path(summary["repo"]) / self.git(summary, "rev-parse", "--git-common-dir")).resolve()
        record = common / ("tmux" + "-pi") / "lanes" / summary["run"] / "lane.json"
        self.assertEqual(json.loads(record.read_text()), g3.FOREIGN_LANE)
        self.assertTrue(any("lane record" in line for line in summary["cleanup"]["leftovers"]))
        # a registered worktree of the name: still registered, its directory still there
        _, summary, _, _ = results()["tamper-worktree-exists"]
        foreign = Path(summary["names"]["lane_worktree"])        # the exact path the lane would have had
        self.assertEqual(foreign.name, f"{Path(summary['repo']).name}-wt-{summary['run']}")
        self.assertTrue(foreign.is_dir())
        self.assertIn(f"worktree {foreign}", self.git(summary, "worktree", "list", "--porcelain"))
        self.assertIn(f"worktree {foreign}", summary["cleanup"]["leftovers"])

    def test_a_session_of_the_name_that_already_exists_survives_with_its_panes(self):
        _, summary, err, _ = results()["tamper-session-exists"]
        self.assertEqual(summary["failed_step"], "setup")
        self.assertFalse(summary["made"]["session"])
        calls = [c["argv"] for c in records() if c["TMUX_TMPDIR"] == summary["tmux_tmpdir"]]
        self.assertFalse([a for a in calls if "kill-session" in a or "kill-pane" in a], "the run killed nothing")
        # the leftover list is taken after the cleanup's own actions and before the private server is stopped: session and pane were still there
        self.assertTrue(any(line.startswith("pane ") and f"in session {summary['run']}" in line for line in summary["cleanup"]["leftovers"]), summary["cleanup"])

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
