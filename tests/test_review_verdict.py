"""The reviewer verdict parser: three verdicts, anything else refused; approval with notes carries its notes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import review_verdict  # noqa: E402


class VerdictTests(unittest.TestCase):
    def test_the_three_verdicts_parse(self):
        self.assertEqual(review_verdict.parse("APPROVED\nRESULT_STATUS: complete\nfine"), ("APPROVED", "fine"))
        self.assertEqual(review_verdict.parse("REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 defect: x")[0], "REQUEST_CHANGES")
        self.assertEqual(review_verdict.parse("\ufeffAPPROVED\n"), ("APPROVED", ""))
        self.assertEqual(review_verdict.parse("REQUEST_CHANGES: F1 is a defect"), ("REQUEST_CHANGES", "F1 is a defect"))

    def test_an_unknown_verdict_is_refused_never_read_as_approval(self):
        for text in ("", "No verdict.", "**APPROVED**: x", "APPROVED_PENDING: later", "APPROVEDX", "approved", "NOT APPROVED: no",
                     "REQUEST_CHANGES_PLEASE", "LGTM", "APPROVED_WITH_NOTE\nnotes",
                     "REQUEST_CHANGES:", "REQUEST_CHANGES:   "):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    review_verdict.parse(text)
                self.assertFalse(review_verdict.approves(text))

    def test_approval_with_notes_returns_the_notes_without_the_result_status_line(self):
        verdict, notes = review_verdict.parse("APPROVED_WITH_NOTES\nRESULT_STATUS: complete\nN1 test gap: empty case.\nN2 wording.")
        self.assertEqual(verdict, "APPROVED_WITH_NOTES")
        self.assertEqual(notes, "N1 test gap: empty case.\nN2 wording.")
        self.assertTrue(review_verdict.approves("APPROVED_WITH_NOTES\nnotes\nCoverage\nreviewed a.py and checked its callers"))

    def test_a_suffix_after_the_verdict_never_approves(self):
        # the only longer form is "REQUEST_CHANGES: reason"; every other suffix is an unknown verdict
        for text in ("APPROVED-OTHER\nRESULT_STATUS: complete", "APPROVED: checked.", "APPROVED. fine", "APPROVED fine",
                     "APPROVED_WITH_NOTES: one wording gap", "APPROVED_WITH_NOTES - gap\nnotes", "APPROVED_WITH_NOTESX\nn",
                     "APPROVED:", "APPROVED_OTHER", "APPROVED_WITH_NOTES_LATER\nn"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    review_verdict.parse(text)
                self.assertFalse(review_verdict.approves(text))
        self.assertFalse(review_verdict.approves("REQUEST_CHANGES: reason"))

    def test_the_first_line_is_the_whole_line_even_when_a_later_line_is_a_verdict(self):
        self.assertEqual(review_verdict.parse("REQUEST_CHANGES\nAPPROVED\n")[0], "REQUEST_CHANGES")
        with self.assertRaises(ValueError):
            review_verdict.parse("Verdict follows.\nAPPROVED")

    def test_approval_with_notes_but_no_notes_is_refused(self):
        for text in ("APPROVED_WITH_NOTES", "APPROVED_WITH_NOTES\nRESULT_STATUS: complete\n"):
            with self.assertRaises(ValueError):
                review_verdict.parse(text)

    def test_notes_may_quote_earlier_rounds_verdicts(self):
        text = ("APPROVED_WITH_NOTES\nRESULT_STATUS: complete\n"
                "Round 1 REQUEST_CHANGES F1 was fixed; round 2 said REQUEST_CHANGES for F2, also fixed.\nN1 wording.\nCoverage\nreviewed a.py and checked its callers")
        verdict, notes = review_verdict.parse(text)
        self.assertEqual(verdict, "APPROVED_WITH_NOTES")
        self.assertIn("Round 1 REQUEST_CHANGES F1 was fixed", notes)
        self.assertTrue(review_verdict.approves(text))
        self.assertEqual(review_verdict.parse("APPROVED\nRESULT_STATUS: complete\nRound 1 was REQUEST_CHANGES; now fixed.")[0], "APPROVED")

    def test_an_approval_whose_result_status_line_says_request_changes_is_refused(self):
        for head in ("APPROVED", "APPROVED_WITH_NOTES"):
            with self.assertRaises(ValueError):
                review_verdict.parse(f"{head}\nRESULT_STATUS: REQUEST_CHANGES\nnote")

    def test_command_line_prints_json_or_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            good, bad = Path(tmp, "good.md"), Path(tmp, "bad.md")
            good.write_text("APPROVED_WITH_NOTES\nnote\nCoverage\nreviewed a.py and checked its callers")
            bad.write_text("maybe")
            ok = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(good)], capture_output=True, text=True)
            self.assertEqual(json.loads(ok.stdout), {"verdict": "APPROVED_WITH_NOTES", "notes": "note\nCoverage\nreviewed a.py and checked its callers",
                                                  "complete": True, "incomplete_reason": None})
            no = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(bad)], capture_output=True, text=True)
            self.assertEqual(no.returncode, 1)
            self.assertIn("unparseable", no.stderr)


if __name__ == "__main__":
    unittest.main()
