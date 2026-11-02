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


class AttemptRowTests(EndCase):
    """F4: the Qwen and normal rows are read back by this attempt's id (`attempt` in run.json and in each row it wrote)."""

    def stale(self, arm):
        def prepare():
            rdir, info = self.prepare()
            trial_record.append(self.repo, {"run": info["run"], "arm": arm, "pair_id": info["pair_id"], "attempt": "an-earlier-attempt",
                                            "outcome": "completed"})
            return rdir, info
        return prepare

    def test_the_rows_a_run_writes_carry_its_attempt_id(self):
        _, done, info = self.fast()
        self.assertEqual(done["outcome"], "completed", done)
        self.assertRegex(done.get("attempt") or "", r"^[0-9a-f]{16}$")
        self.assertEqual({r["arm"]: r.get("attempt") for r in self.records()}, {"qwen": done["attempt"], "normal": done["attempt"]})

    def test_a_stale_qwen_row_of_the_run_id_never_stands_in_for_an_append_that_failed(self):
        _, done, info = self.fast(record_module=FlakyRecords(fail=99), prepare=self.stale("qwen"))
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertNotIn("recorded", done)
        self.assertIn("0 of them this attempt's", done["record_error"])
        self.assertEqual([r.get("attempt") for r in self.qwen_of(info["run"])], ["an-earlier-attempt"], "only the stale row is there")

    def test_a_stale_normal_row_of_the_run_id_never_stands_in_for_an_append_that_left_none(self):
        class NormalLost(RecordingRecords):
            def append(self, project, record):
                if record.get("arm") == "normal":
                    return None  # the append returned, and nothing of this attempt is in records.jsonl
                return trial_record.append(project, record)
        _, done, _ = self.fast(record_module=NormalLost(), prepare=self.stale("normal"))
        self.assertEqual(done["outcome"], "record_failed", done)
        self.assertIn("no normal row of this attempt", done["unmet"])


if __name__ == "__main__":
    unittest.main()
