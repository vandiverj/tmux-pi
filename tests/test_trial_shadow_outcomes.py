"""lib/trial_shadow.py: a run is `completed` only when every step after the worker ended succeeded (review fix round 1, family
false-completed-trial). A worker that settled is then reviewed and recorded; a review that could not run, a reviewer that did not finish, a
record that could not be written, and a tree that could not be collected each end the run with an outcome of their own, and the attempt is
still recorded where it can be. Fixtures: trial_shadow_case.py."""
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



if __name__ == "__main__":
    unittest.main()
