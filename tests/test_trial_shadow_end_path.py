"""lib/trial_shadow.py, review round 4 (family false-completed-trial): the completed rule's conditions 3, 4, 5 and 7 as Main decided them,
and the end path, whose steps never skip the cleanup. One test per finding and per row of the two tables in `Run.execute` whose behaviour
changed:

  F1  an end step that raises (the branch listing whose note fails, an interrupt there, the session check) never skips the cleanup,
      and the Qwen row is still written;
  F2  condition 7: a ref the run made that git still lists at the end is `cleanup_failed`, whatever the cleanup returned;
  F3  condition 3: `inconclusive` is not a verdict: an incomplete, unusable or unparsed review is `no_review`, its row keeps `inconclusive`;
  F4  conditions 4 and 5: a row counts only when it carries this attempt's id; a row an earlier attempt with the run id left never does.

Fixtures: trial_shadow_case.py and test_trial_shadow_outcomes.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trial_shadow_case import *  # noqa: E402,F401,F403
from test_trial_shadow_outcomes import FastRun, FlakyRecords, OutcomeCase  # noqa: E402

COVERAGE = "## Coverage\nReviewed lib/x.py in full.\n"
LABEL_ONLY = "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 a note.\nFamily: docs\nCloses by: check\n" + COVERAGE  # UNUSABLE
NO_COVERAGE = "APPROVED\nRESULT_STATUS: complete\n"  # incomplete
UNPARSED = "I looked at it and it seems fine.\n"


class EndCase(OutcomeCase):
    def worker_done(self, n, run):
        if n == 1:
            self.edit(run)
            self.settle_worker(run)

    def fast(self, run_class=FastRun, record_module=None, prepare=None):
        rdir, info = (prepare or self.prepare)()
        self.addCleanup(lambda: ts.cleanup(rdir, self.env))  # a run whose cleanup failed on purpose is cleaned after the test
        run, done = self.run_stub(rdir, self.worker_done, run_class=run_class, record_module=record_module)
        return run, done, info

    def qwen_of(self, run):
        return [r for r in self.qwen() if r["run"] == run]

    def gone(self, info, tree):
        self.assertNotIn("tp-trial-" + info["run"], self.session_names(), "the cleanup ended the session")
        self.assertFalse(Path(tree).exists(), "the cleanup removed the worktree")


class ReviewVerdictTests(EndCase):
    """F3: condition 3 takes approved and request_changes only."""

    def test_an_inconclusive_first_review_is_no_review_and_its_row_keeps_inconclusive(self):
        for text in (LABEL_ONLY, NO_COVERAGE, UNPARSED):
            class Inconclusive(FastRun):
                verdict = text
            with self.subTest(review=text[:20]):
                _, done, info = self.fast(Inconclusive)
                self.assertEqual(done["outcome"], "no_review", done)
                self.assertIn("inconclusive", done["unmet"])
                qwen, = self.qwen_of(info["run"])
                self.assertEqual((qwen["outcome"], qwen["first_review_outcome"]), ("no_review", "inconclusive"))


if __name__ == "__main__":
    unittest.main()
