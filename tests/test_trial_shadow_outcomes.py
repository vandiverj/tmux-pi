"""lib/trial_shadow.py: a run is `completed` only when every condition of the completed rule holds at the end, read from evidence (review
fix rounds 1 to 3, family false-completed-trial): the worker was ended, the work was collected from the proven tree, a review settled with a
verdict, exactly one Qwen row of the run is in records.jsonl, the normal arm's row is there or its absence is one the rule allows and names,
and the cleanup ended the session and removed the worktree. Otherwise the outcome names the first condition that does not hold, the Qwen row
says the same, and `tp-trial run` exits non-zero. Fixtures: trial_shadow_case.py."""
import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403

sys.path.insert(0, str(ROOT / "lib"))
import trial_report  # noqa: E402

APPROVED = "APPROVED\nRESULT_STATUS: complete\n## Coverage\nReviewed lib/x.py in full.\n"


class FlakyRecords(RecordingRecords):
    """The real record module whose append of a qwen record fails `fail` times (L2's append refuses a records file that is not plain)."""

    def __init__(self, fail):
        super().__init__()
        self.fail, self.tries = fail, 0

    def append(self, project, record):
        if record.get("arm") == "qwen":
            self.tries += 1
            if self.tries <= self.fail:
                raise ts.ShadowError("{} is not a plain file; the record was not written".format(self.RECORDS))
        return trial_record.append(project, record)


class NoReview(ts.Run):
    """A run whose `tp-agent review` is refused."""

    def agent(self, *args, **kw):
        if args[:1] == ("review",):
            return subprocess.CompletedProcess(args, 1, "", "review refused")
        return super().agent(*args, **kw)


class NoReviewRun(NoReview, StubSpawnRun):
    pass


class StubReview(ts.Run):
    """A run whose review is what tp-review leaves once its reviewer settled (a reviewer task with its verdict in result.md, the author's
    meta.json naming it), made by hand: what these tests check is the end of the run, and a real reviewer costs some 20 s each here.
    `verdict` None is a reviewer that settled without writing one."""
    verdict = APPROVED

    def review(self):
        self.note(phase="review")
        rid = "review-" + self.run
        task = self.task_path(rid)
        task.mkdir()
        (task / "meta.json").write_text(json.dumps({"id": rid, "worker": "pi", "model": "claude-sonnet-5-5", "thinking": "medium",
                                                    "role": "reviewer", "created": "2026-10-31T10:05:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": "waiting"}))
        (task / "review-of").write_text(self.task_id)
        if self.verdict is not None:
            (task / "result.md").write_text(self.verdict)
        meta_file = self.task_path() / "meta.json"
        meta_file.write_text(json.dumps(dict(json.loads(meta_file.read_text()), reviews=[rid])))
        self.review_id, self.review_result = rid, "settled"
        self.note(review_id=rid, review_wait="settled")
        return rid


class FastRun(StubReview, StubSpawnRun):
    pass


class OutcomeCase(ShadowCase):
    def settle_then_review(self, approve=True):
        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.settle_worker(run)
            elif approve:
                for p in (run.state / "tasks").iterdir():
                    if (p / "review-of").exists() and not (p / "state.json").exists():
                        self.settle(p, "waiting", APPROVED)
        return poll

    def qwen(self):
        return [r for r in self.records() if r["arm"] == "qwen"]

    def report(self):
        records, bad = trial_report.load(self.repo / ".tmux-pi" / "trial" / "records.jsonl")
        return trial_report.summarize(records, bad)


class RecordFailureTests(OutcomeCase):
    """F1: `recorded` is set only by an append that succeeded."""

    def test_a_qwen_record_that_cannot_be_appended_is_not_a_completed_trial(self):
        rdir, _ = self.prepare()
        records = FlakyRecords(fail=99)
        _, done = self.run_stub(rdir, self.settle_then_review(), record_module=records)
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertIn("not a plain file", done["record_error"])
        self.assertNotIn("recorded", done, "the run did not record, and does not say it did")
        self.assertEqual(records.tries, 2, "the append was tried again at the end of the run")
        self.assertEqual(self.qwen(), [])
        self.assertEqual([r["arm"] for r in self.records()], ["normal"], "the other arm is still written")
        self.assertEqual(done["cleanup_errors"], [])

    def test_a_record_that_fails_once_is_written_by_the_retry_and_the_run_is_completed(self):
        rdir, _ = self.prepare()
        records = FlakyRecords(fail=1)
        _, done = self.run_stub(rdir, self.settle_then_review(), record_module=records)
        self.assertEqual(done["outcome"], "completed", done)
        self.assertIn("recorded", done)
        self.assertIsNone(done["record_error"], "the failure that was retried is not left as a missing record")
        self.assertIn("not a plain file", done["record_retried"])
        self.assertEqual(records.tries, 2)
        qwen, = self.qwen()
        self.assertEqual((qwen["outcome"], qwen["first_review_outcome"]), ("completed", "approved"), "one row, with the outcome the run ended with")

    def test_a_record_that_fails_on_a_run_that_did_not_complete_keeps_that_outcome(self):
        rdir, _ = self.prepare(timeout_s=300)
        records = FlakyRecords(fail=99)
        _, done = self.run_stub(rdir, record_module=records)
        self.assertEqual(done["outcome"], "timeout", done)
        self.assertIn("record_error", done)

    def test_a_record_the_builder_cannot_make_is_the_same_failure(self):
        class Broken(RecordingRecords):
            def build(self, *args, **kw):
                raise ValueError("cannot read the task")
        rdir, _ = self.prepare()
        _, done = self.run_stub(rdir, self.settle_then_review(), record_module=Broken())
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertIn("ValueError", done["record_error"])


class ReviewOutcomeTests(OutcomeCase):
    """F2: a review that did not run, or did not finish, is not a completed trial; the report counts it as no review."""

    def test_a_review_that_cannot_start_is_no_review_and_the_attempt_is_still_recorded(self):
        rdir, _ = self.prepare()
        _, done = self.run_stub(rdir, self.settle_then_review(), run_class=NoReviewRun)
        self.assertEqual(done["outcome"], "no_review", done)
        self.assertIn("review refused", done["review_error"])
        self.assertIsNone(done.get("review_id"))
        qwen, = self.qwen()
        self.assertEqual((qwen["outcome"], qwen["first_review_outcome"]), ("no_review", None))
        counts = self.report()["by_arm"]["qwen"]
        self.assertEqual((counts["no_review"], counts["approved"]), (1, 0))

    def test_a_reviewer_that_does_not_finish_in_time_is_a_review_timeout(self):
        rdir, _ = self.prepare(review_timeout_s=300)
        _, done = self.run_stub(rdir, self.settle_then_review(approve=False))
        self.assertEqual((done["outcome"], done["review_wait"]), ("review_timeout", "timeout"), done)
        self.assertTrue(done["review_id"])
        qwen, = self.qwen()
        self.assertEqual((qwen["outcome"], qwen["first_review_outcome"]), ("review_timeout", None))
        self.assertEqual(self.report()["by_arm"]["qwen"]["no_review"], 1)

    def test_a_reviewer_that_fails_is_no_review(self):
        rdir, _ = self.prepare()

        def poll(n, run):
            if n == 1:
                self.edit(run)
                self.settle_worker(run)
            else:
                for p in (run.state / "tasks").iterdir():
                    if (p / "review-of").exists():
                        (p / "exit").write_text("1\n")
                        self.tmux("kill-pane", "-t", json.loads((p / "meta.json").read_text())["pane"])
        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["review_wait"], "failed", done)
        self.assertEqual(done["outcome"], "no_review")

    def test_a_review_that_ran_is_completed(self):
        rdir, _ = self.prepare()
        _, done = self.run_stub(rdir, self.settle_then_review())
        self.assertEqual((done["outcome"], done["review_wait"]), ("completed", "settled"), done)
        self.assertEqual(self.qwen()[0]["first_review_outcome"], "approved")


class CollectOutcomeTests(OutcomeCase):
    """The rest of the family: a step after the worker ended that cannot do its work does not end in `completed`."""

    def test_a_tree_that_is_gone_when_the_work_is_collected_is_an_error_and_nothing_is_reviewed(self):
        rdir, _ = self.prepare()

        def poll(n, run):
            self.settle_worker(run)
            shutil.rmtree(str(run.worktree))
        _, done = self.run_stub(rdir, poll)
        self.assertEqual(done["outcome"], "error", done)
        self.assertIn("worktree", done["error"])
        self.assertIsNone(done.get("review_id"))
        self.assertEqual([r["outcome"] for r in self.qwen()], ["error"])


class CompletedRuleTests(OutcomeCase):
    """Round 3: one rule, checked at the end from evidence. One test per condition; the first is the control that meets them all."""

    def worker_done(self, n, run):
        if n == 1:
            self.edit(run)
            self.settle_worker(run)

    def fast(self, run_class=FastRun, record_module=None, prepare=None):
        rdir, info = (prepare or self.prepare)()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))  # a run whose cleanup failed on purpose is cleaned after the test
        run, done = self.run_stub(rdir, self.worker_done, run_class=run_class, record_module=record_module)
        return run, done, info

    def test_a_run_that_meets_every_condition_is_completed_with_one_qwen_row_saying_so(self):
        run, done, info = self.fast()
        self.assertEqual(done["outcome"], "completed", done)
        qwen, = self.qwen()
        self.assertEqual((qwen["outcome"], qwen["first_review_outcome"], qwen["run"]), ("completed", "approved", info["run"]))
        self.assertEqual([r["arm"] for r in self.records()].count("normal"), 1)
        self.assertEqual(done["cleanup_errors"], [])
        self.assertIn("recorded", done)
        self.assertNotIn("tp-trial-" + info["run"], self.session_names())
        self.assertFalse(self.tree(run.rdir).exists())

    def test_worker_ended_a_worker_whose_end_was_never_seen_is_not_completed(self):
        class NotEnded(FastRun):
            def end_worker(self, outcome):
                return None  # the end command was never run, so nothing shows the worker ended
        _, done, _ = self.fast(NotEnded)
        self.assertEqual(done["outcome"], "error", done)
        self.assertIn("worker", done["unmet"])
        self.assertEqual([r["outcome"] for r in self.qwen()], ["error"], "the row says what the run says")

    def test_collected_work_whose_patch_is_not_in_the_run_dir_is_not_completed(self):
        class PatchGone(FastRun):
            def review(self):
                (self.rdir / "final.patch").unlink()
                return super().review()
        _, done, _ = self.fast(PatchGone)
        self.assertEqual(done["outcome"], "error", done)
        self.assertIn("final.patch", done["unmet"])
        self.assertEqual([r["outcome"] for r in self.qwen()], ["error"])

    def test_review_a_reviewer_that_settled_without_a_verdict_is_no_review(self):
        class NoVerdict(FastRun):
            verdict = None
        _, done, _ = self.fast(NoVerdict)
        self.assertEqual(done["outcome"], "no_review", done)
        qwen, = self.qwen()
        self.assertEqual((qwen["outcome"], qwen["first_review_outcome"]), ("no_review", None))

    def test_qwen_row_f2_a_task_meta_json_gone_after_the_review_is_record_failed(self):
        """Review round 2 F2: the append is skipped when meta.json is not there; the readback finds no row."""
        class MetaGone(FastRun):
            def review(self):
                rid = super().review()
                (self.task_path() / "meta.json").unlink()
                return rid
        _, done, _ = self.fast(MetaGone)
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertEqual(self.qwen(), [])
        self.assertNotIn("recorded", done)
        self.assertIn("meta.json", done["record_error"])

    def test_qwen_row_a_second_row_of_the_run_in_the_records_is_record_failed(self):
        def prepare():
            rdir, info = self.prepare()
            trial_record.append(self.repo, {"run": info["run"], "arm": "qwen", "pair_id": info["pair_id"], "outcome": "completed"})
            return rdir, info
        _, done, _ = self.fast(prepare=prepare)
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertIn("2", done["record_error"])

    def test_normal_arm_a_source_task_that_is_gone_is_an_absence_the_rule_allows_and_names(self):
        class SourceGone(FastRun):
            def review(self):
                shutil.rmtree(str(self.main_state / "tasks" / self.info["source_task"]))
                return super().review()
        _, done, _ = self.fast(SourceGone)
        self.assertEqual(done["outcome"], "completed", done)
        self.assertIn("gone", done["normal_record_absent"])
        self.assertEqual([r["arm"] for r in self.records()], ["qwen"])

    def test_normal_arm_a_normal_record_that_cannot_be_written_is_an_absence_the_rule_allows_and_names(self):
        class NormalFails(RecordingRecords):
            def append(self, project, record):
                if record.get("arm") == "normal":
                    raise OSError("disk full")
                return trial_record.append(project, record)
        _, done, _ = self.fast(record_module=NormalFails())
        self.assertEqual(done["outcome"], "completed", done)
        self.assertIn("disk full", done["normal_record_error"])

    def test_normal_arm_a_normal_record_appended_but_not_in_the_records_is_record_failed(self):
        class NormalLost(RecordingRecords):
            def append(self, project, record):
                if record.get("arm") == "normal":
                    return None  # the append returned, and nothing is in records.jsonl
                return trial_record.append(project, record)
        _, done, _ = self.fast(record_module=NormalLost())
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertIn("normal", done["unmet"])

    def test_cleanup_f1_a_session_that_could_not_be_ended_is_cleanup_failed_and_the_row_says_so(self):
        """Review round 2 F1: the session end returned False; the session and the tree stay, so the run is not completed."""
        class SessionStays(FastRun):
            def __init__(self, *args, **kw):
                super().__init__(*args, **kw)
                self.owner.end_session = lambda env: False
        run, done, info = self.fast(SessionStays)
        self.assertEqual(done["outcome"], "cleanup_failed", done)
        self.assertIn("tp-trial-" + info["run"], self.session_names(), "the session is still there")
        self.assertTrue(self.tree(run.rdir).exists())
        self.assertIn("session", done["unmet"])
        self.assertEqual([r["outcome"] for r in self.qwen()], ["cleanup_failed"], "one row, with the outcome the run ended with")

    def test_an_interrupt_during_the_cleanup_is_never_left_as_completed(self):
        class Interrupt(FastRun):
            def cleanup(self):
                raise ts.Interrupted()
        rdir, _ = self.prepare()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))
        with self.assertRaises(ts.Interrupted):
            self.run_stub(rdir, self.worker_done, run_class=Interrupt)
        done = ts.read_run(rdir)
        self.assertEqual((done["outcome"], done["phase"]), ("stopped", "done"), done)


class TpTrialExitTests(unittest.TestCase):
    """`tp-trial run` and `tp-trial shadow --foreground` exit 0 for `completed` and for nothing else."""

    def test_every_outcome_but_completed_exits_non_zero(self):
        loader = importlib.machinery.SourceFileLoader("tp_trial_cli", str(ROOT / "bin" / "tp-trial"))
        spec = importlib.util.spec_from_loader("tp_trial_cli", loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        for outcome in ts.TERMINAL_OUTCOMES:
            with self.subTest(outcome=outcome), mock.patch.object(cli.trial_shadow, "execute", return_value={"outcome": outcome}), \
                    mock.patch("builtins.print"):
                self.assertEqual(cli.cmd_run(mock.Mock(run_dir="/nowhere")), 0 if outcome == "completed" else 1)
        self.assertIn("cleanup_failed", ts.TERMINAL_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
