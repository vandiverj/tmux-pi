"""Lanes L4: the review gate, `tp-agent lane close NAME STEP`, and the next-step rule (design checks 15, 16 and 17; section 4.2 and
the eleven rows of section 8). The repositories are temporary ones, tmux is the private -L server of tests/lane_fixture.py, and no
model runs: a reviewer's task directory is made by hand, with the files tp-review would leave in it (meta.json with the lane tags,
review-of, reviewed-state.json, state.json, result.md). What the gate reads is those files and git; so that is what is faked.

Every refusal is also held to be side-effect free: the state directory (every file's bytes), the lane directory (lane.json and
lease.json), the worktree list, the branches, the lane's HEAD and its status are the same after as before."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_lane_writer import GIT_ENV, LaneCase, git  # noqa: E402
import lane_steps  # noqa: E402

APPROVED = "APPROVED\n\nThe change does what the task asked.\n\n## Coverage\nI read lib/x.py and the callers it changes and ran the tests.\n\nRESULT_STATUS: complete\n"
WITH_NOTES = "APPROVED_WITH_NOTES\n\nOne naming nit in lib/x.py, nothing blocking.\n\n## Coverage\nI read lib/x.py and the callers it changes.\n\nRESULT_STATUS: complete\n"
CHANGES = "REQUEST_CHANGES: the helper returns the wrong value\n\n## Coverage\nI read lib/x.py and traced both of its callers.\n\nRESULT_STATUS: complete\n"
COVERAGE = "\n## Coverage\nI read lib/x.py and traced both of its callers.\n\nRESULT_STATUS: complete\n"
LABELLED = {  # REQUEST_CHANGES replies by what their findings say they close by (lib/review_verdict.py closes_by)
    "code": "REQUEST_CHANGES\n\nF1 [defect] the helper returns the wrong value. Closes by: code\n" + COVERAGE,
    "check and none": "REQUEST_CHANGES\n\nF1 [note] the report has no test count. Closes by: check\nF2 [note] wording. Closes by: none\n" + COVERAGE,
    "Markdown check": "REQUEST_CHANGES\n\nF1 [note] the report shows no log. **Closes by:** `check`\n" + COVERAGE,
    "no labels": "REQUEST_CHANGES\n\nF1 [defect] the helper returns the wrong value.\nF2 [note] wording.\n" + COVERAGE,
    "check and code": "REQUEST_CHANGES\n\nF1 [note] no log. Closes by: check\nF2 [defect] wrong value. Closes by: code\n" + COVERAGE,
    # a label counts only where it ends a finding (review_verdict.closes_by): a quotation of one is no label
    "defect, Coverage quotes check": "REQUEST_CHANGES\n\nF1 [defect] the helper returns the wrong value.\n\n## Coverage\nI read lib/x.py, quoting `Closes by: check` from the rule.\n\nRESULT_STATUS: complete\n",
    "check, prose quotes code": "REQUEST_CHANGES\n\nF1 [note] no log. Closes by: check\n\nThe rule says \"`Closes by: code`\" for a defect.\n" + COVERAGE,
    "check, Verification holds code": "REQUEST_CHANGES\n\nF1 [note] no log. Closes by: check\n\n## Verification\nCloses by: code\n" + COVERAGE,
    "bold code with a full stop": "REQUEST_CHANGES\n\nF1 [defect] wrong value. **Closes by: code.**\n" + COVERAGE,
}
CHECK_ONLY = LABELLED["check and none"]
NO_COVERAGE = "APPROVED\n\nLooks fine to me.\n\nRESULT_STATUS: complete\n"
NOT_A_VERDICT = "I think this is fine.\n\n## Coverage\nI read lib/x.py and the callers it changes.\n\nRESULT_STATUS: complete\n"
DECORATED = "**APPROVED**\n\n## Coverage\nI read lib/x.py and the callers it changes.\n\nRESULT_STATUS: complete\n"


class StepCase(LaneCase):
    """A lane `alpha` whose steps are driven by hand: writers through tp-agent, reviewers as the files tp-review leaves."""

    def setUp(self):
        super().setUp()
        self.made = 0

    # --- the lane and its tree --------------------------------------------------------------------------------------

    def exists(self):
        return (self.lane_dir() / "lane.json").exists()

    def writer(self, step="s1", settled=True):
        """A coder task in `step` (the first one makes the lane); settled with a reply unless `settled` is False."""
        item, task = self.existing(step=step) if self.exists() else self.lane_ok(step=step)
        if settled:
            self.settle(task)
        return item["id"], task

    def edit(self, name="s1.py", text="work\n"):
        (self.worktree / "lib" / name).write_text(text)

    def commit(self, message="the step's work"):
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-q", "-m", message)
        return git(self.worktree, "rev-parse", "HEAD")

    def tree(self):
        """The tree of the working tree as it is now, made without the lane's index: what a reviewer reviews."""
        env = {**GIT_ENV, "GIT_INDEX_FILE": str(self.root / "probe-index")}
        for args in (("read-tree", "HEAD"), ("add", "-A")):
            subprocess.run(["git", "-C", str(self.worktree), *args], env=env, check=True, capture_output=True)
        out = subprocess.run(["git", "-C", str(self.worktree), "write-tree"], env=env, check=True, capture_output=True, text=True)
        (self.root / "probe-index").unlink()
        return out.stdout.strip()

    def record(self):
        return json.loads((self.lane_dir() / "lane.json").read_text())

    # --- a reviewer, as tp-review leaves it -------------------------------------------------------------------------

    def review(self, author, text=APPROVED, tree=None, life="settled", state=True, step="s1", workdir=None):
        """A reviewer task of `author`. life: settled (state.json + result.md), running (no state yet), failed (exit 1), nothing
        (waiting but no result.md). `tree` defaults to the working tree as it is now."""
        self.made += 1
        ident = f"review-fabricated{self.made:02d}"
        task = self.state / "tasks" / ident
        task.mkdir()
        meta = {"id": ident, "role": "code-reviewer", "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "workdir": str(self.worktree), "lane": "alpha", "lane_step": step, "preamble_from": str(self.repo)}
        (task / "meta.json").write_text(json.dumps(meta))
        (task / "review-of").write_text(author + "\n")
        if state:
            (task / "reviewed-state.json").write_text(json.dumps({"workdir": str(workdir or self.worktree), "top": str(self.worktree),
                                                                 "tree": tree or self.tree()}))
        if life == "settled":
            self.settle(task, result=text)
        elif life == "failed":
            (task / "exit").write_text("1\n")
        elif life == "nothing":
            self.settle(task, result=None)
        elif life != "running":
            raise AssertionError(life)
        return ident, task

    def approved_and_committed(self, text=APPROVED, step="s1"):
        """Step `step` done as the design wants it: a settled writer, its work committed, a review of exactly that tree."""
        author, _ = self.writer(step)
        self.edit(f"{step}.py")
        self.commit()
        reviewer, _ = self.review(author, text, step=step)
        return author, reviewer

    # --- what a refusal must not touch -------------------------------------------------------------------------------

    def snap(self):
        files = {}
        for base in (self.state, self.lane_dir().parent):
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    files[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        facts = {"files": files, "worktrees": git(self.repo, "worktree", "list"), "branches": git(self.repo, "branch", "--list", "-a"),
                 "repo-status": git(self.repo, "status", "--porcelain")}
        if self.worktree.exists():
            facts["head"] = git(self.worktree, "rev-parse", "HEAD")
            facts["status"] = git(self.worktree, "status", "--porcelain", "--untracked-files=all")
            facts["stash"] = git(self.worktree, "stash", "list")
        return facts

    def untouched(self, make_run, *words):
        """Run `make_run()`, which must be refused naming `words`, and show that it changed nothing. Returns stderr."""
        before = self.snap()
        run = make_run()
        self.refused(run, *words)
        self.assertEqual(run.stdout, "")
        self.assertEqual(self.snap(), before, "a refusal changed something")
        return run.stderr

    def close(self, step="s1", name="alpha"):
        return self.raw("lane", "close", name, step, "--repo", str(self.repo))

    def closes(self, step="s1"):
        run = self.close(step)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def next_step(self, step="s2"):
        return self.lane_dispatch(step=step, extra=())

    def lanes(self):
        run = self.raw("lanes", "--repo", str(self.repo))
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)[0]

    def reset(self):
        """Back to no lane: the sub-tests of one row each start from nothing."""
        if self.exists():
            git(self.repo, "worktree", "remove", "--force", str(self.worktree))
            git(self.repo, "branch", "-D", "lane/alpha")
            shutil.rmtree(self.lane_dir())
        for path in (self.state / "tasks").iterdir():
            shutil.rmtree(path)
        for path in (self.state / "queue").glob("*.json") if (self.state / "queue").is_dir() else []:
            path.unlink()
        lease = self.lane_dir().parent
        shutil.rmtree(lease, ignore_errors=True)
        self.made = 0


class GateRowsTests(StepCase):
    """Design section 8, rows 1 to 5: the gate refuses until the committed tree is approved."""

    def test_row_1_step_b_waits_while_a_s_author_is_working(self):
        author, _ = self.writer(settled=False)
        stderr = self.untouched(self.next_step, "lane alpha: step s1 is not closed", author, f"tp-agent stop {author}")
        self.assertIn("The next step starts after the review of s1 finishes", stderr)
        self.assertEqual(self.lanes()["waiting_on"], "worker")

    def test_row_1_step_b_waits_while_the_author_is_settled_and_unreviewed(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        stderr = self.untouched(self.next_step, "step s1 is not closed", "has no review yet", f"tp-agent review {author} --force")
        self.assertIn("tp-agent lane close alpha s1", stderr)
        self.assertEqual(self.lanes()["waiting_on"], "review")

    def test_row_1_step_b_waits_while_the_review_is_running(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        reviewer, _ = self.review(author, life="running")
        self.untouched(self.next_step, "step s1 is not closed", f"{reviewer} is reviewing this tree", f"tp-agent stop {reviewer}")
        self.assertEqual(self.lanes()["waiting_on"], "review")

    def test_row_1_step_b_waits_after_request_changes(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        reviewer, _ = self.review(author, CHANGES)
        self.untouched(self.next_step, "step s1 is not closed", f"{reviewer} asked for changes", f"tp-agent retry {author}")
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-dispatch")

    def test_row_2_approved_but_uncommitted_cannot_be_closed(self):
        author, _ = self.writer()
        self.edit()
        reviewer, _ = self.review(author)  # the reviewed tree is the dirty working tree
        stderr = self.untouched(self.close, "step s1 of lane alpha cannot be closed", "the approved work is not committed", "1 uncommitted path(s)",
                                f"git -C {self.worktree} add -A", "tp-agent lane close alpha s1")
        self.assertIn(str(self.worktree), stderr)
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-commit")
        self.untouched(self.next_step, "step s1 is not closed", "the approved work is not committed")

    def test_row_2_the_printed_commit_then_close_works(self):
        author, _ = self.writer()
        self.edit()
        self.review(author)
        self.untouched(self.close, "not committed")
        git(self.worktree, "add", "-A")
        git(self.worktree, "commit", "-q", "-m", "as reviewed")
        entry = self.closes()["closed"][0]
        self.assertEqual(entry["commit"], git(self.worktree, "rev-parse", "HEAD"))

    def test_row_3_one_more_commit_after_the_review_makes_it_stale(self):
        author, reviewer = self.approved_and_committed()
        self.edit("later.py", "a change nobody reviewed\n")
        self.commit("one more")
        stderr = self.untouched(self.close, "cannot be closed", "no review is of this tree", f"{reviewer}: it reviewed tree",
                                f"tp-agent review {author} --force")
        self.assertNotIn("not committed", stderr)
        self.assertEqual(self.record()["closed"], [])

    def test_row_3_an_edit_after_the_review_makes_it_stale_too(self):
        author, reviewer = self.approved_and_committed()
        self.edit("later.py", "uncommitted and unreviewed\n")
        self.untouched(self.close, "no review is of this tree", reviewer)

    def test_row_3_a_review_of_another_working_tree_does_not_count(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.review(author, workdir=self.repo)
        self.untouched(self.close, "no review is of this tree")

    def test_row_4_two_reviews_of_one_tree_one_asking_for_changes_do_not_approve(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.review(author, APPROVED)
        rejecting, _ = self.review(author, CHANGES)
        self.untouched(self.close, f"{rejecting} asked for changes to this tree")
        self.review(author, WITH_NOTES)  # a later approval of the same tree does not undo it
        self.untouched(self.close, f"{rejecting} asked for changes to this tree")

    def judge(self, text):
        author, _ = self.writer()
        self.edit()
        self.commit()
        _, task = self.review(author, text)
        return lane_steps._judge({"path": task})  # the gate's one reader of a review reads nothing else of the task

    def test_closes_by_a_request_changes_with_a_code_finding_or_no_labels_rejects(self):
        for name in ("code", "no labels", "check and code", "defect, Coverage quotes check", "bold code with a full stop"):
            with self.subTest(name):
                self.reset()
                self.assertEqual(self.judge(LABELLED[name]), ("rejects", "REQUEST_CHANGES"))

    def test_closes_by_a_request_changes_with_labels_but_no_code_is_unusable_not_a_rejection(self):
        for name in ("check and none", "Markdown check", "check, prose quotes code", "check, Verification holds code"):
            with self.subTest(name):
                self.reset()
                self.assertEqual(self.judge(LABELLED[name]), ("unusable", "REQUEST_CHANGES without a Closes by: code finding"))

    def test_closes_by_an_approval_with_check_labels_still_approves(self):
        text = "APPROVED_WITH_NOTES\n\nN1 the report has no test count. Closes by: check\n" + COVERAGE
        self.assertEqual(self.judge(text), ("approves", "APPROVED_WITH_NOTES"))

    def test_closes_by_an_unusable_review_does_not_block_and_a_later_approval_closes_the_step(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        unusable, _ = self.review(author, CHECK_ONLY)
        self.untouched(self.close, "cannot be closed", "is usable and complete", unusable, "REQUEST_CHANGES without a Closes by: code finding",
                       f"tp-agent review {author} --force")
        self.assertEqual(self.lanes()["waiting_on"], "review")  # the next move is a review, not a change to the work
        good, _ = self.review(author)
        entry = self.closes()["closed"][0]
        self.assertEqual((entry["review"], entry["verdict"]), (good, "APPROVED"))

    def test_closes_by_a_code_finding_still_blocks_after_an_unusable_review_and_a_later_approval(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.review(author, CHECK_ONLY)
        rejecting, _ = self.review(author, LABELLED["code"])
        self.review(author)
        self.untouched(self.close, f"{rejecting} asked for changes to this tree")

    def test_row_5_an_unreadable_verdict_never_approves(self):
        for name, text in (("a free-text verdict", NOT_A_VERDICT), ("a decorated verdict", DECORATED), ("a missing Coverage section", NO_COVERAGE),
                           ):
            with self.subTest(name):
                self.reset()
                author, _ = self.writer()
                self.edit()
                self.commit()
                reviewer, _ = self.review(author, text)
                self.untouched(self.close, "cannot be closed", "is usable and complete", reviewer)

    def test_row_5_a_review_that_is_not_utf8_or_has_no_result_or_no_reviewed_state_never_approves(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        broken, task = self.review(author)
        (task / "result.md").write_bytes(b"APPROVED\n\xff\xfe\nRESULT_STATUS: complete\n")  # not UTF-8: the lifecycle reads no reply
        self.untouched(self.close, "cannot be closed", broken, f"tp-agent stop {broken}")
        silent, _ = self.review(author, life="nothing")  # a reviewer that stopped without a result
        self.untouched(self.close, "cannot be closed", f"tp-agent stop")
        stateless, _ = self.review(author, state=False)
        self.untouched(self.close, "cannot be closed")
        self.assertEqual(self.record()["closed"], [])

    def test_row_5_a_review_whose_status_line_is_not_a_status_is_paused_and_blocks_until_stopped(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        odd, _ = self.review(author, "APPROVED\n\n## Coverage\nI read lib/x.py and the callers.\n\nRESULT_STATUS: REQUEST_CHANGES\n")
        self.untouched(self.close, f"{odd} is reviewing this tree and is paused", f"tp-agent stop {odd}")

    def test_row_5_a_failed_reviewer_never_approves_and_a_good_one_still_can(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        failed, _ = self.review(author, life="failed")
        self.untouched(self.close, f"{failed}: it ended failed", f"tp-agent review {author} --force")
        good, _ = self.review(author)  # an unusable review is not a vote: the second review of the same tree decides
        entry = self.closes()["closed"][0]
        self.assertEqual((entry["review"], entry["verdict"]), (good, "APPROVED"))

    def test_a_review_missing_reviewed_state_is_never_counted_even_when_it_approves(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        stateless, _ = self.review(author, state=False)
        self.untouched(self.close, "no review is of this tree", f"{stateless}: no reviewed-state.json")

class CloseAndNextStepTests(StepCase):
    """Design section 8, rows 6 to 11, and checks 15 to 17."""

    def test_row_6_a_clean_approved_commit_closes_and_step_b_starts_from_it(self):
        author, reviewer = self.approved_and_committed()
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-close")
        committed = git(self.worktree, "rev-parse", "HEAD")
        record = self.closes()
        entry = record["closed"][0]
        self.assertEqual({k: entry[k] for k in ("step", "commit", "tree", "review", "verdict")},
                         {"step": "s1", "commit": committed, "tree": git(self.worktree, "rev-parse", "HEAD^{tree}"), "review": reviewer,
                          "verdict": "APPROVED"})
        self.assertRegex(entry["closed_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(self.record()["closed"], record["closed"])
        row = self.lanes()
        self.assertEqual((row["open_step"], row["waiting_on"]), (None, "orchestrator-dispatch"))
        item, task = self.existing(step="s2")
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), committed)  # step B starts from exactly the closed commit
        self.assertEqual(json.loads((task / "meta.json").read_text())["lane_step"], "s2")
        self.assertEqual(self.lanes()["open_step"], "s2")

    def test_row_6_a_completed_same_provider_user_approved_review_closes_like_any_other(self):
        author, reviewer = self.approved_and_committed()
        meta_path = self.state / "tasks" / reviewer / "meta.json"
        meta = json.loads(meta_path.read_text())
        meta["review_policy"] = "required"
        meta["reviewer"] = {"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high",
                            "independence": "same_provider_user_approved", "approved_by_user": "use claude"}
        meta_path.write_text(json.dumps(meta))
        entry = self.closes()["closed"][0]
        self.assertEqual((entry["review"], entry["verdict"]), (reviewer, "APPROVED"))

    def test_row_6_an_approval_with_notes_closes_and_is_recorded_as_such(self):
        _, reviewer = self.approved_and_committed(WITH_NOTES)
        self.assertEqual(self.closes()["closed"][0]["verdict"], "APPROVED_WITH_NOTES")

    def test_row_6_a_second_approval_replaces_an_unusable_review_and_is_the_one_recorded(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        self.review(author, NO_COVERAGE)
        good, _ = self.review(author, APPROVED)
        self.assertEqual(self.closes()["closed"][0]["review"], good)

    def test_row_6_a_dispatch_into_the_open_step_is_not_held_by_the_rule(self):
        # checks 16 and 17 only hold back a different step: a fix after REQUEST_CHANGES goes into the same step, dirty tree and all
        author, _ = self.writer()
        self.edit()
        self.review(author, CHANGES)
        self.edit("more.py", "another file\n")
        item, task = self.existing(step="s1")
        self.assertEqual(json.loads((task / "meta.json").read_text())["lane_step"], "s1")

    def test_row_7_a_commit_outside_the_tool_after_the_close_refuses_step_b_and_the_printed_save_works(self):
        self.approved_and_committed()
        closed = self.closes()["closed"][0]["commit"]
        self.edit("outside.py", "made by hand\n")
        outside = self.commit("by hand")
        self.untouched(self.next_step, "lane alpha has changes since step s1 closed", f"HEAD {outside[:12]}", "1 commit(s) after the closed one",
                       f"git -C {self.worktree} branch saved/alpha-{outside[:8]} HEAD", f"reset --hard {closed}")
        git(self.worktree, "branch", f"saved/alpha-{outside[:8]}", "HEAD")
        git(self.worktree, "reset", "-q", "--hard", closed)
        self.existing(step="s2")
        self.assertEqual(git(self.repo, "rev-parse", f"saved/alpha-{outside[:8]}"), outside)  # the work is not lost

    def test_row_7_uncommitted_work_after_the_close_refuses_step_b_and_the_printed_stash_works(self):
        self.approved_and_committed()
        self.closes()
        self.edit("outside.py", "left in the tree\n")
        self.untouched(self.next_step, "lane alpha has changes since step s1 closed", "1 uncommitted path(s)", f"git -C {self.worktree} stash push -u")
        git(self.worktree, "stash", "push", "-u", "-m", "lane alpha: before step s2")
        self.existing(step="s2")
        self.assertIn("outside.py", git(self.worktree, "stash", "show", "--include-untracked", "--name-only", "stash@{0}"))

    def test_row_7_a_lane_never_stepped_that_has_stray_changes_is_held_the_same_way(self):
        self.writer()
        self.edit()
        # no step is open once its tasks are gone from the session; the base is what the changes are measured from
        shutil.rmtree(self.state / "tasks")
        (self.state / "tasks").mkdir()
        self.untouched(self.next_step, "lane alpha has changes since the lane's base", "1 uncommitted path(s)", "stash push -u")

    def test_row_8_closing_a_closed_step_says_so(self):
        self.approved_and_committed()
        self.closes()
        before = self.record()
        stderr = self.untouched(self.close, "step s1 is already closed at", before["closed"][0]["commit"], "nothing to do")
        self.assertIn("tp-agent dispatch --lane alpha --step NEW-ID", stderr)
        self.assertEqual(self.record(), before)

    def test_row_8_closing_a_step_that_is_not_the_open_one_names_the_open_one(self):
        self.writer("s1")
        newer = self.state / "tasks/coder-newer00001"  # a lane from before the rule: s2 begun while s1 was still open
        newer.mkdir()
        meta = json.loads(next((self.state / "tasks").glob("coder-*/meta.json")).read_text())
        meta.update(id="coder-newer00001", lane_step="s2", created=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        (newer / "meta.json").write_text(json.dumps(meta))
        self.settle(newer)
        # every step that is not closed is open: the oldest is the one `lane close` takes first
        self.untouched(lambda: self.close("s2"), "step s2 is not lane alpha's open step (s1, the oldest that is not closed)", "tp-agent lane close alpha s1")
        self.assertEqual(self.lanes()["open_step"], "s1")

    def test_row_8_a_step_no_task_of_this_session_carries_is_named_with_the_steps_there_are(self):
        self.writer("s1")
        self.untouched(lambda: self.close("nope"), "lane alpha has no step nope in this session", "its steps in this session are s1",
                       "tp-agent lanes alpha")

    def test_row_8_a_lane_that_does_not_exist_and_a_bad_step_id(self):
        self.untouched(lambda: self.close("s1", "ghost"), "there is no lane ghost", "tp-agent lanes")
        self.writer()
        self.untouched(lambda: self.close("bad/step"), "invalid step id")

    def test_an_empty_step_closes_without_a_review_and_is_recorded_as_empty(self):
        # owner-directed: a step whose tasks all settled and left the tree as the last closed commit had it has nothing to review
        self.writer()
        base = self.record()["base"]
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-close")
        entry = self.closes()["closed"][0]
        self.assertEqual({k: entry[k] for k in ("step", "commit", "tree", "review", "verdict")},
                         {"step": "s1", "commit": base["commit"], "tree": git(self.worktree, "rev-parse", "HEAD^{tree}"), "review": None, "verdict": "EMPTY"})
        self.assertEqual(self.lanes()["steps_closed"], 1)  # the record reads back through lane_record and lane_reality
        self.existing(step="s2")  # and the next step starts from it

    def test_an_empty_step_is_closed_without_looking_at_a_review_of_the_unchanged_tree(self):
        author, _ = self.writer()
        self.review(author, CHANGES)  # a reviewer of a tree nobody changed: it judged nothing that was changed
        self.assertEqual(self.closes()["closed"][0]["verdict"], "EMPTY")

    def test_an_empty_step_still_waits_for_its_writer(self):
        author, _ = self.writer(settled=False)
        self.untouched(self.close, "cannot be closed", f"{author} is running", f"tp-agent stop {author}")
        self.settle(self.state / "tasks" / author)
        self.assertEqual(self.closes()["closed"][0]["verdict"], "EMPTY")

    def test_an_empty_step_is_not_one_with_a_change_in_the_tree(self):
        author, _ = self.writer()
        self.edit()  # uncommitted: not empty, so the review gate applies
        self.untouched(self.close, "cannot be closed", "has no review yet")
        self.commit()
        self.untouched(self.close, "cannot be closed", "has no review yet")

    def test_a_second_step_that_changed_nothing_closes_empty_at_the_first_closed_commit(self):
        self.approved_and_committed()
        first = self.closes()["closed"][0]
        author, _ = self.writer("s2")
        entry = self.closes("s2")["closed"][1]
        self.assertEqual((entry["step"], entry["commit"], entry["tree"], entry["review"], entry["verdict"]),
                         ("s2", first["commit"], first["tree"], None, "EMPTY"))

    def test_a_step_whose_commit_and_revert_leave_the_last_closed_tree_is_empty_and_records_the_head(self):
        self.approved_and_committed()
        first = self.closes()["closed"][0]
        self.writer("s2")
        self.edit("temp.py", "x\n")
        self.commit("add")
        git(self.worktree, "rm", "-q", "lib/temp.py")
        head = self.commit("revert")
        entry = self.closes("s2")["closed"][1]
        self.assertEqual((entry["commit"], entry["tree"], entry["verdict"]), (head, first["tree"], "EMPTY"))
        self.existing(step="s3")

    def test_row_9_moving_the_open_steps_tasks_to_archive_changes_no_answer(self):
        author, reviewer = self.approved_and_committed()
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-close")
        (self.state / "archive").mkdir()
        for ident in (author, reviewer):
            shutil.move(str(self.state / "tasks" / ident), str(self.state / "archive" / ident))
        self.assertEqual((self.lanes()["open_step"], self.lanes()["waiting_on"]), ("s1", "orchestrator-close"))
        self.untouched(self.next_step, "step s1 is not closed", "it is approved and committed but not recorded", "tp-agent lane close alpha s1")
        self.assertEqual(self.closes()["closed"][0]["review"], reviewer)

    def test_row_9_an_archived_unreviewed_author_cannot_be_reviewed_so_the_message_names_a_new_task(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        (self.state / "archive").mkdir()
        shutil.move(str(self.state / "tasks" / author), str(self.state / "archive" / author))
        stderr = self.untouched(self.close, "cannot be closed", "No writer of the step can be reviewed",
                                "tp-agent dispatch --lane alpha --step s1 --role coder --task")
        self.assertNotIn(f"tp-agent review {author}", stderr)
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-dispatch")

    def test_row_10_a_review_policy_that_says_none_is_needed_does_not_open_the_gate(self):
        # a researcher's work needs no review by the policy: `tp-agent review` says so and starts nothing, and the gate still wants one
        item, task = self.lane_ok(role="researcher")
        self.settle(task)
        self.edit()
        self.commit()
        run = self.raw("review", item["id"])
        self.assertEqual((run.returncode, json.loads(run.stdout)["status"]), (0, "not_needed"), run.stdout + run.stderr)
        self.assertEqual(list((self.state / "tasks").glob("review-*")), [])
        self.untouched(self.close, "cannot be closed", "has no review yet", f"tp-agent review {item['id']} --force")
        self.untouched(self.next_step, "step s1 is not closed", "has no review yet")
        self.assertEqual(self.record()["closed"], [])
        forced = self.raw("review", item["id"], "--force")  # the printed command: it does start one
        self.assertEqual(json.loads(forced.stdout)["status"], "started", forced.stdout + forced.stderr)

    def test_row_11_a_branch_switched_under_a_closed_step_refuses(self):
        self.approved_and_committed()
        self.closes()
        git(self.worktree, "checkout", "-q", "-b", "elsewhere")
        for make in (self.next_step, self.close):
            self.untouched(make, "lane alpha: worktree", "is on elsewhere, expected lane/alpha", "check it out again")

    def test_row_11_a_removed_worktree_refuses(self):
        self.approved_and_committed()
        self.closes()
        git(self.repo, "worktree", "remove", "--force", str(self.worktree))
        for make in (self.next_step, self.close):
            self.untouched(make, f"lane alpha: worktree {self.worktree} does not exist", "restore it")

    def test_row_11_an_edited_closed_tree_refuses(self):
        self.approved_and_committed()
        self.closes()
        path = self.lane_dir() / "lane.json"
        record = json.loads(path.read_text())
        record["closed"][0]["tree"] = "0" * 40
        path.write_text(json.dumps(record))
        for make in (self.next_step, self.close):
            self.untouched(make, "lane alpha: closed step s1 records tree " + "0" * 40, "lane.json was changed by hand")

    def test_row_11_a_branch_moved_below_a_closed_commit_refuses_and_the_printed_command_puts_it_back(self):
        self.approved_and_committed()
        closed = self.closes()["closed"][0]["commit"]
        git(self.worktree, "reset", "-q", "--hard", self.record()["base"]["commit"])
        stderr = self.untouched(self.next_step, f"closed step s1 commit {closed} is not on branch lane/alpha", "put the branch back on it")
        command = stderr.split("(`")[1].split("`)")[0]
        run = subprocess.run(["sh", "-c", command], env=GIT_ENV, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), closed)
        self.existing(step="s2")

    def test_check_15_a_task_into_a_closed_step_is_refused_and_a_new_id_works(self):
        author, _ = self.approved_and_committed()
        commit = self.closes()["closed"][0]["commit"]
        self.untouched(lambda: self.lane_dispatch(step="s1", extra=()), f"step s1 of lane alpha was closed at {commit}", "start a new step id",
                       "tp-agent dispatch --lane alpha --step NEW-ID")
        self.existing(step="s1-fix")

    def test_check_15_a_retry_into_a_closed_step_is_refused(self):
        author, _ = self.approved_and_committed()
        self.cli("collect", author)
        self.closes()
        self.untouched(lambda: self.raw("retry", author, "--reason", "Again", "--worker", "pi", "--model", MODEL), "was closed at", "start a new step id")

    def test_check_16_names_the_command_that_clears_each_wait(self):
        author, _ = self.writer(settled=False)
        self.untouched(self.next_step, "step s1 is not closed", f"tp-agent stop {author}")

    def test_the_lanes_row_follows_the_step(self):
        first = self.lane_ok()
        row = self.lanes()
        self.assertEqual((row["open_step"], row["waiting_on"], row["queued"]), ("s1", "worker", None))
        self.settle(first[1])
        self.edit()
        self.commit()
        self.assertEqual(self.lanes()["waiting_on"], "review")
        self.review(first[0]["id"])
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-close")
        self.closes()
        self.assertEqual(self.lanes()["waiting_on"], "orchestrator-dispatch")

    def test_a_lane_in_one_session_is_not_held_by_steps_of_another(self):
        # the design delta: tasks are per session. A session that has no task of the lane sees no open step.
        self.approved_and_committed()
        other = self.root / "other-state"
        (other / "tasks").mkdir(parents=True)
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "lane", "close", "alpha", "s1", "--repo", str(self.repo)],
                             env={**self.env, "TP_STATE_DIR": str(other)}, text=True, capture_output=True, timeout=60)
        self.refused(run, "lane alpha has no step s1 in this session", "no task of this session belongs to the lane", "TP_STATE_DIR=")


class OpenStepTests(StepCase):
    """Which step is open is read from every step that is not closed, never from the newest task; and a task a lane names whose
    record cannot be read keeps its step open (a task no lane names is an orphan, as before)."""

    def two_steps(self):
        """s1 approved, committed and closed; s2 begun, settled and unreviewed (no change in the tree: empty, but not closed)."""
        author, reviewer = self.approved_and_committed()
        self.closes()
        second, task = self.writer("s2")
        return author, reviewer, second, task

    def test_a_late_review_of_a_closed_step_does_not_hide_the_open_one(self):
        author, reviewer, second, _ = self.two_steps()
        self.edit("s2.py")
        self.commit("s2 work")  # unreviewed, so s2 cannot be closed
        self.untouched(lambda: self.next_step("s3"), "lane alpha: step s2 is not closed", "has no review yet", f"tp-agent review {second} --force")
        self.assertEqual(self.lanes()["open_step"], "s2")
        later, _ = self.review(author, step="s1")  # a review of the closed s1, newer than every task of s2
        self.assertEqual(self.lanes()["open_step"], "s2", "the newest task is a review of s1; s2 is still the open step")
        self.untouched(lambda: self.next_step("s3"), "lane alpha: step s2 is not closed", "has no review yet")
        self.assertEqual(self.lanes()["waiting_on"], "review")
        self.assertEqual(self.record()["closed"][0]["review"], reviewer)  # s1's record is not touched by the later review
        self.untouched(lambda: self.close("s1"), "step s1 is already closed")
        # the way out the refusal prints: review s2's author, approve, commit as reviewed, close, and s3 starts
        self.review(second, step="s2")
        self.closes("s2")
        self.existing(step="s3")

    def test_every_step_that_is_not_closed_is_open_and_a_task_for_any_of_them_is_not_held_back(self):
        self.writer("s1")
        newer = self.state / "tasks/coder-newer00003"  # from before the rule: s2 begun while s1 was open
        newer.mkdir()
        meta = json.loads(next((self.state / "tasks").glob("coder-*/meta.json")).read_text())
        meta.update(id="coder-newer00003", lane_step="s2", created="2999-01-01T00:00:00Z")
        (newer / "meta.json").write_text(json.dumps(meta))
        self.settle(newer)
        self.assertEqual(self.lanes()["open_step"], "s1")  # the oldest unclosed one, though s2's task is newer
        self.existing(step="s2")  # not a new step: it is open already
        self.untouched(lambda: self.next_step("s3"), "lane alpha: step s1 is not closed")

    def test_a_late_review_of_a_closed_step_does_not_hide_a_queued_step_either(self):
        author, _, second, task = self.two_steps()
        self.edit("s2.py")
        self.commit("s2 work")
        queued = {"id": "coder-queued0004", "created": "2999-01-01T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s2", "preamble_from": str(self.repo)}
        shutil.rmtree(task)  # s2 is known only by its queued item
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue/coder-queued0004.json").write_text(json.dumps(queued))
        self.review(author, step="s1")
        self.assertEqual(self.lanes()["open_step"], "s2")
        self.untouched(lambda: self.next_step("s3"), "lane alpha: step s2 is not closed", "coder-queued0004 is queued for step s2")

    # --- an unreadable record that a lane names ----------------------------------------------------------------------------

    def break_meta(self, task, how="garbage"):
        if how == "garbage":
            (task / "meta.json").write_text("{broken")
        elif how == "missing":
            (task / "meta.json").unlink()
        elif how == "not-an-object":
            (task / "meta.json").write_text("[1, 2]")
        else:
            raise AssertionError(how)

    def test_an_unreadable_record_keeps_the_step_it_belongs_to_open(self):
        for how in ("garbage", "missing", "not-an-object"):
            with self.subTest(how):
                self.reset()
                author, task = self.writer()
                self.edit()
                self.commit()
                self.untouched(self.next_step, "lane alpha: step s1 is not closed", "has no review yet")
                self.break_meta(task, how)
                stderr = self.untouched(self.next_step, "lane alpha: step s1 is not closed", f"{author} ({task})",
                                        "belongs to step s1 but its meta.json cannot be read", f"`rm -r {task}`")
                self.assertNotIn("has no review yet", stderr)
                self.assertEqual(self.lanes()["open_step"], "s1")
                self.assertEqual(self.lanes()["waiting_on"], "worker")
                self.untouched(self.close, "step s1 of lane alpha cannot be closed", f"{author} ({task})", f"`rm -r {task}`")

    def test_an_unreadable_record_blocks_even_a_step_that_would_close_empty_or_approved(self):
        author, task = self.writer()  # nothing changed: empty, closable
        self.break_meta(task)
        self.untouched(self.close, "cannot be closed", f"{author} ({task})", "meta.json cannot be read")
        self.untouched(self.next_step, "step s1 is not closed", f"`rm -r {task}`")

    def test_an_unreadable_record_that_no_lane_names_stays_an_orphan(self):
        self.approved_and_committed()
        self.closes()
        orphan = self.state / "tasks/coder-orphan00001"
        orphan.mkdir()
        (orphan / "meta.json").write_text("{broken")  # no dispatch.json, no lease: it names no lane
        other = self.state / "tasks/coder-orphan00002"
        other.mkdir()
        (other / "dispatch.json").write_text(json.dumps({"lane": "beta", "lane_step": "b1"}))  # another lane's: alpha is not held by it
        self.existing(step="s2")
        self.assertEqual(self.lanes()["open_step"], "s2")
        stderr = self.untouched(lambda: self.next_step("s3"), "lane alpha: step s2 is not closed")  # refused for s2 alone
        self.assertNotIn("orphan", stderr)

    def test_an_unreadable_record_of_a_closed_step_is_not_held_against_the_next_step(self):
        author, reviewer = self.approved_and_committed()
        self.closes()
        self.break_meta(self.state / "tasks" / author)
        self.existing(step="s2")

    def test_a_lane_names_an_unreadable_task_by_its_lease_when_it_has_no_dispatch_record(self):
        author, task = self.writer()
        self.edit()
        self.commit()
        (task / "dispatch.json").unlink()
        self.break_meta(task)
        lease = self.lane_dir() / "lease.json"
        lease.write_text(json.dumps({"schema": "tp-lane-lease:v1", "holders": [
            {"kind": "worker", "access": "write", "task": author, "role": "coder", "session": str(self.state), "pid": 99999999,
             "start": "Mon Jan  1 00:00:00 2001", "pane": None, "taken": "2026-10-08T00:00:00+00:00"}]}))
        stderr = self.untouched(self.next_step, f"lane alpha: {author} ({task})", "belongs to an open step of this lane", f"`rm -r {task}`",
                                "The next step starts after that is cleared")
        self.assertNotIn("has no review yet", stderr)
        self.untouched(self.close, "has no step s1 in this session")  # the lease does not place it in a step: no step to close

    def test_a_running_process_of_an_unreadable_task_is_ended_before_it_is_removed(self):
        author, task = self.writer()
        pane, _ = self.pane(self.worker_command(task))  # a process started from the task's run.sh
        self.break_meta(task)
        self.untouched(self.next_step, f"{author} ({task})", f"`tmux kill-pane -t {pane}` (a process of it is running), then `rm -r {task}`")
        self.tmux("kill-pane", "-t", pane)
        self.untouched(self.next_step, f"`rm -r {task}`")


class UnitTests(StepCase):
    def test_a_session_with_no_state_directory_lists_nulls_and_warns(self):
        self.writer()
        env = {k: v for k, v in self.env.items() if k != "TP_STATE_DIR"}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "lanes", "--repo", str(self.repo)], env=env, text=True, capture_output=True, timeout=60, cwd=self.root)
        self.assertEqual(run.returncode, 0, run.stderr)
        row = json.loads(run.stdout)[0]
        self.assertEqual((row["open_step"], row["queued"], row["waiting_on"]), (None, None, None))
        self.assertIn("warning: no session state directory", run.stderr)


class OwnerOverrideTests(StepCase):
    """`lane close --override-review --reason TEXT`: the owner, at a terminal, closes over a review that asked for changes. The
    terminal check is the CLI's; the gate is close_step's, driven here in-process as the CLI drives it after the code is typed."""

    def rejected(self):
        author, _ = self.writer()
        self.edit()
        self.commit()
        rejecting, _ = self.review(author, CHANGES)
        return author, rejecting

    def override(self, reason="owner ran the tests the review asked for"):
        common, _ = lane_steps.lane_commands._main_repo(str(self.repo))
        return lane_steps.close_step(self.state, common, "alpha", "s1", override_reason=reason)

    def close_over(self, *extra, env=None):
        args = ("lane", "close", "alpha", "s1", "--repo", str(self.repo), *extra)
        return subprocess.run([str(ROOT / "bin/tp-agent"), *args], env=env or self.env, text=True, capture_output=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def test_an_agent_session_is_refused_before_it_is_asked(self):
        self.rejected()
        env = {**self.env, "TP_TASK_ID": "coder-0000000000"}
        self.untouched(lambda: self.close_over("--override-review", "--reason", "x", env=env), "TP_TASK_ID is set")

    def test_without_a_terminal_it_is_refused(self):
        self.rejected()
        env = {k: v for k, v in self.env.items() if k not in ("TP_TASK_ID", "TP_TASK_DIR", "TP_MODE")}
        self.untouched(lambda: self.close_over("--override-review", "--reason", "x", env=env), "must be a terminal")

    def test_a_reason_is_required_and_goes_only_with_the_override(self):
        self.rejected()
        self.untouched(lambda: self.close_over("--override-review"), "needs a --reason")
        self.untouched(lambda: self.close_over("--override-review", "--reason", "  "), "needs a --reason")
        self.untouched(lambda: self.close_over("--reason", "x"), "--reason goes with --override-review")
        with self.assertRaisesRegex(ValueError, "needs a --reason"):
            self.override("  ")

    def test_the_owner_closes_over_a_rejecting_review_and_it_is_recorded_as_such(self):
        _, rejecting = self.rejected()
        committed = git(self.worktree, "rev-parse", "HEAD")
        entry = self.override()["closed"][0]
        self.assertEqual({k: entry[k] for k in ("step", "commit", "review", "verdict", "override_reason")},
                         {"step": "s1", "commit": committed, "review": rejecting, "verdict": "OWNER_OVERRIDE",
                          "override_reason": "owner ran the tests the review asked for"})
        self.assertEqual(self.record()["closed"][0], entry)  # it survives the record's own check on read
        self.assertEqual((self.lanes()["open_step"], self.lanes()["waiting_on"]), (None, "orchestrator-dispatch"))

    def test_it_lifts_nothing_but_the_rejection(self):
        author, _ = self.rejected()
        self.review(author, life="running")
        with self.assertRaisesRegex(ValueError, "is reviewing this tree"):
            self.override()
        self.reset()
        author, _ = self.writer()
        self.edit()
        self.review(author, CHANGES)  # of the uncommitted tree
        with self.assertRaisesRegex(ValueError, "not committed"):
            self.override()
        self.reset()
        self.writer(settled=False)
        with self.assertRaisesRegex(ValueError, "the step's work is not finished"):
            self.override()
        self.assertEqual(self.record()["closed"], [])

    def at_terminal(self, answer):
        """Run the override in a pseudo-terminal; `answer(code)` is what is typed back. Returns (exit status, all output)."""
        import pty
        import re
        env = {k: v for k, v in self.env.items() if k not in ("TP_TASK_ID", "TP_TASK_DIR", "TP_MODE")}
        argv = [str(ROOT / "bin/tp-agent"), "lane", "close", "alpha", "s1", "--repo", str(self.repo), "--override-review", "--reason", "owner says so"]
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(argv[0], argv, env)
        out = b""
        while b"> " not in out:
            out += os.read(fd, 1024)
        code = re.search(rb"\n  ([a-z]+(?:-[a-z]+){3})\r?\n", out).group(1).decode()
        os.write(fd, (answer(code) + "\n").encode())
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, status = os.waitpid(pid, 0)
        os.close(fd)
        return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")

    def test_at_a_terminal_the_typed_code_decides(self):
        _, rejecting = self.rejected()
        status, out = self.at_terminal(lambda code: "wrong-code")
        self.assertEqual(status, 1, out)
        self.assertIn("did not match", out)
        self.assertEqual(self.record()["closed"], [])
        status, out = self.at_terminal(lambda code: code)
        self.assertEqual(status, 0, out)
        self.assertEqual((self.record()["closed"][0]["verdict"], self.record()["closed"][0]["review"]), ("OWNER_OVERRIDE", rejecting))

    def test_with_no_rejecting_review_there_is_nothing_to_override(self):
        self.approved_and_committed()
        with self.assertRaisesRegex(ValueError, "nothing to override"):
            self.override()
        self.assertEqual(self.record()["closed"], [])


if __name__ == "__main__":
    unittest.main()
