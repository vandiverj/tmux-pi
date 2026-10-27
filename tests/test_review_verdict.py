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
                                                  "complete": True, "incomplete_reason": None, "closes_by": [], "unusable_reason": None})
            no = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(bad)], capture_output=True, text=True)
            self.assertEqual(no.returncode, 1)
            self.assertIn("unparseable", no.stderr)


# (name, reply, labels it carries, whether the REQUEST_CHANGES is unusable: labelled, none of them `code`)
CLOSES_BY_ROWS = (
    ("a code finding", "REQUEST_CHANGES\nF1 defect: wrong value. Closes by: code\n", ["code"], False),
    ("check and none", "REQUEST_CHANGES\nF1 defect: no count in the report. Closes by: check\nF2 note: wording. Closes by: none\n",
     ["check", "none"], True),
    ("Markdown bold and backticks", "REQUEST_CHANGES\nF1 **Closes by:** `check`\n", ["check"], True),
    ("bold around the whole label", "REQUEST_CHANGES\nF1 **Closes by: decision**\n", ["decision"], True),
    ("case and spacing", "REQUEST_CHANGES\nF1 CLOSES BY:Check\nF2 closes  by :  none\n", ["check", "none"], True),
    ("no labels at all", "REQUEST_CHANGES\nF1 defect: wrong value.\nF2 note: wording.\n", [], False),
    ("check and code", "REQUEST_CHANGES\nF1 Closes by: check\nF2 Closes by: code\n", ["check", "code"], False),
    ("a label with another value", "REQUEST_CHANGES\nF1 Closes by: maybe\nF2 Closes by: codebase\n", [], False),
    ("a list item with a decision", "REQUEST_CHANGES: F1\n- F1 (defect) Closes by: `decision`\n", ["decision"], True),
    ("approval with check labels", "APPROVED_WITH_NOTES\nN1 Closes by: check\nCoverage\nreviewed a.py and checked its callers", ["check"], False),
    # a label counts only where it ends a finding: not quoted, not in a Coverage or Verification section, not mid-sentence
    ("a defect with no label, Coverage quotes a label in backticks",
     "REQUEST_CHANGES\nF1 [defect] wrong value.\n\n## Coverage\nI read a.py and the labels are missing, so I am quoting `Closes by: check` here.\n", [], False),
    ("a defect with no label, Coverage ends with a bare label",
     "REQUEST_CHANGES\nF1 [defect] wrong value.\n\nCoverage\nI read a.py; the author's report says Closes by: check\n", [], False),
    ("check finding, later prose quotes code",
     "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n\nThe rule says \"`Closes by: code`\" for a defect.\n", ["check"], True),
    ("check finding, later prose has an open double quote",
     "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\nThe template says \"Closes by: code\n", ["check"], True),
    ("check finding, Verification heading holds a code line",
     "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n\n## Verification\nCloses by: code\n", ["check"], True),
    ("check finding, bold Verification line holds a code line",
     "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n\n**Verification:** the template\nCloses by: code\n", ["check"], True),
    ("a label after the Coverage section's next heading counts",
     "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n\n## Coverage\nread a.py and its callers.\n\n## Findings\nF2 [defect] wrong. Closes by: code\n",
     ["check", "code"], False),
    ("bold label with the full stop inside", "REQUEST_CHANGES\nF1 [defect] wrong value. **Closes by: code.**\n", ["code"], False),
    ("a full stop after the label", "REQUEST_CHANGES\nF1 [note] no count. Closes by: check.\n", ["check"], True),
    ("a blockquoted code label", "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n> Closes by: code\n", ["check"], True),
    ("a code label in a fenced block", "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n```\nCloses by: code\n```\n", ["check"], True),
    ("an unclosed fence cannot be read", "REQUEST_CHANGES\nF1 [note] no count. Closes by: check\n```\nx\n", [], False),
    ("a label with text after it", "REQUEST_CHANGES\nF1 [defect] wrong. Closes by: code is what the template says\n", [], False),
    ("a label wrapped whole in backticks", "REQUEST_CHANGES\nF1 [defect] wrong. `Closes by: code`\n", [], False),
)


class ClosesByTests(unittest.TestCase):
    def test_the_labels_found_in_a_reply(self):
        for name, text, labels, _ in CLOSES_BY_ROWS:
            with self.subTest(name):
                self.assertEqual(review_verdict.closes_by(text), labels)

    def test_only_a_request_changes_with_labels_and_no_code_is_unusable(self):
        for name, text, labels, unusable in CLOSES_BY_ROWS:
            with self.subTest(name):
                reason = review_verdict.request_changes_problem(text)
                self.assertEqual(reason, "REQUEST_CHANGES without a Closes by: code finding" if unusable else None)

    def test_an_unusable_review_is_still_no_approval_and_keeps_its_stated_verdict(self):
        text = "REQUEST_CHANGES\nF1 Closes by: check\n"
        self.assertEqual(review_verdict.parse(text)[0], "REQUEST_CHANGES")
        self.assertFalse(review_verdict.approves(text))

    def test_a_reply_that_is_not_a_verdict_has_no_problem_to_report(self):
        self.assertIsNone(review_verdict.request_changes_problem("maybe\nF1 Closes by: check"))

    def test_command_line_reports_the_labels_and_why_the_review_is_unusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "r.md")
            path.write_text("REQUEST_CHANGES\nF1 Closes by: check\n")
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(path)], capture_output=True, text=True)
            value = json.loads(out.stdout)
            self.assertEqual((value["verdict"], value["closes_by"]), ("REQUEST_CHANGES", ["check"]))
            self.assertEqual(value["unusable_reason"], "REQUEST_CHANGES without a Closes by: code finding")


if __name__ == "__main__":
    unittest.main()
