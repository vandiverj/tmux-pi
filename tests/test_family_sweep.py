"""lib/family_sweep.py: the generated sweep.md (site extraction, findings, the required table, an unparseable finding kept)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import family_sweep  # noqa: E402
import review_rounds  # noqa: E402

COVERAGE = "\nCoverage\nI read lib/never_listed.py:99 and the callers.\n"


def review(*findings):
    return "REQUEST_CHANGES\nRESULT_STATUS: complete\n" + "".join(f + "\n\n" for f in findings) + COVERAGE


F1 = "F1 [defect] the date is parsed twice at lib/a.py:12 and `lib/b.py:30-34`.\nFamily: date-parse\nCloses by: code"
F2 = "F2 [defect] the same parse at bin/tp-x:7 again.\nFamily: date-parse\nCloses by: code"
F3 = "F3 [defect] the error path is odd somewhere in the importer.\nCloses by: code"


class SiteTests(unittest.TestCase):
    def test_file_line_tokens_in_prose_and_backticks_in_order_without_repeats(self):
        self.assertEqual(family_sweep.sites("at lib/a.py:12, `lib/b.py:30-34`, bin/tp-agent:1402 and lib/a.py:12"),
                         ["lib/a.py:12", "lib/b.py:30-34", "bin/tp-agent:1402"])

    def test_things_that_are_not_sites_are_not_read_as_sites(self):
        self.assertEqual(family_sweep.sites("http://example.com:80 at 12:30 and foo.py:3x and v1.2:4"), [])

    def test_the_findings_end_at_their_closes_by_line_and_coverage_is_not_a_finding(self):
        got = family_sweep.findings(review(F1, F3))
        self.assertEqual([(names, tokens) for _, names, tokens in got],
                         [(["date-parse"], ["lib/a.py:12", "lib/b.py:30-34"]), ([], [])])
        self.assertNotIn("never_listed", " ".join(text for text, _, _ in got))

    def test_a_reply_that_is_not_a_verdict_has_no_findings(self):
        self.assertEqual(family_sweep.findings("looks fine at lib/a.py:1"), [])


class BuildTests(unittest.TestCase):
    def state(self, *replies):
        """A state directory with an author chain: author-N has review-N, each author the parent of the next."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for number, reply in enumerate(replies, 1):
            for ident, meta in ((f"author-{number}", {"id": f"author-{number}", "reviews": [f"review-{number}"],
                                                       **({"parent": f"author-{number - 1}"} if number > 1 else {})}),
                                (f"review-{number}", {"id": f"review-{number}"})):
                directory = Path(tmp.name) / "tasks" / ident
                directory.mkdir(parents=True)
                (directory / "meta.json").write_text(json.dumps(meta))
            (Path(tmp.name) / "tasks" / f"review-{number}" / "result.md").write_text(reply)
        return tmp.name

    def test_every_cited_site_of_every_earlier_finding_is_named_with_the_reviews_that_cited_it(self):
        text = family_sweep.build(self.state(review(F1), review(F2, F3)), "author-2", "date-parse")
        for site in ("lib/a.py:12", "lib/b.py:30-34", "bin/tp-x:7"):
            self.assertIn(f"- `{site}` (cited by", text)
        self.assertIn("| `bin/tp-x:7` | | |", text)
        self.assertNotIn("never_listed", text)  # Coverage is not a finding

    def test_the_families_the_rule_in_the_reviewers_words_the_search_and_the_table_are_there(self):
        text = family_sweep.build(self.state(review(F1), review(F2)), "author-2", "date-parse", "/work/tree")
        self.assertIn("# Sweep: finding family `date-parse`", text)
        self.assertIn("- review-1 (round 1): `date-parse`", text)
        self.assertIn("- review-2 (round 2): `date-parse`", text)
        self.assertIn("> F2 [defect] the same parse at bin/tp-x:7 again.", text)  # the latest review's words, quoted
        self.assertNotIn("> F1 [defect]", text)
        self.assertIn("git -C /work/tree grep -n", text)
        self.assertIn("tp-ctx", text)
        self.assertIn("| Site | Disposition | Evidence |", text)
        for disposition in ("fixed", "already correct", "out of scope", "unresolved"):
            self.assertIn(f"`{disposition}`", text)

    def test_the_unnamed_family_quotes_the_findings_of_the_latest_review_that_ask_for_code(self):
        text = family_sweep.build(self.state(review(F3), review(F3.replace("odd", "still odd"))), "author-2", None)
        self.assertIn("# Sweep: the unnamed family (REQUEST_CHANGES twice in a row)", text)
        self.assertIn("- review-1 (round 1): no Family: line", text)
        self.assertIn("still odd", text)

    def test_a_finding_with_no_site_is_listed_verbatim_and_not_dropped(self):
        text = family_sweep.build(self.state(review(F3), review(F1)), "author-2", None)
        self.assertIn("Findings with no file:line token, verbatim", text)
        self.assertIn("> F3 [defect] the error path is odd somewhere in the importer.", text)
        self.assertIn("From review review-1:", text)

    def test_a_review_that_cannot_be_read_is_named_and_does_not_stop_the_sweep(self):
        state = self.state(review(F1), review(F2))
        (Path(state) / "tasks" / "review-1" / "result.md").write_bytes(b"\xff\xfe not utf-8")
        text = family_sweep.build(state, "author-2", None)
        self.assertIn("could not be read, so their sites are not listed: review-1", text)
        self.assertIn("`bin/tp-x:7`", text)

    def test_an_approval_in_the_chain_carries_no_findings(self):
        approved = "APPROVED\nRESULT_STATUS: complete\n" + COVERAGE
        text = family_sweep.build(self.state(approved, review(F2)), "author-2", None)
        self.assertNotIn("review-1 (round", text)
        self.assertIn("`bin/tp-x:7`", text)

    def test_a_long_finding_is_shortened_but_its_sites_are_listed_in_full(self):
        long = "F1 [defect] " + "word " * 2000 + "at lib/deep.py:5\nCloses by: code"
        text = family_sweep.build(self.state(review(long), review(long)), "author-2", None)
        self.assertIn("shortened", text)
        self.assertIn("- `lib/deep.py:5` (cited by review-1, review-2)", text)
        self.assertLess(len(text), 20000)

    def test_a_chain_that_cannot_be_walked_raises_refused(self):
        with self.assertRaises(review_rounds.Refused):
            family_sweep.build(tempfile.gettempdir() + "/no-such-state", "author-1", None)

    def test_is_sweep_only_for_a_sweep_in_a_task_directory(self):
        self.assertTrue(family_sweep.is_sweep("/s/tasks/coder-1/sweep.md"))
        self.assertFalse(family_sweep.is_sweep("/s/docs/sweep.md"))
        self.assertFalse(family_sweep.is_sweep("/s/tasks/coder-1/other.md"))


if __name__ == "__main__":
    unittest.main()
