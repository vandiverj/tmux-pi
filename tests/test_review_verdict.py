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


BRIEF = "# Step\n\n## Done means\n1. First thing.\n   continued line\n2. Second thing.\n3. Third thing.\n\n## Tests\n9. not an item\n"
COVERED = "\nCoverage\nI reviewed a.py and checked its callers and tests.\n"


def reply(section):
    return "APPROVED\nRESULT_STATUS: complete\n" + section + COVERED


class AcceptanceCriteriaTests(unittest.TestCase):
    """R7: a brief or contract with a numbered Done means / Acceptance criteria list makes the reply account for each item."""

    def test_the_numbered_list_under_either_heading_is_read(self):
        for name, text, want in (
                ("heading", BRIEF, [1, 2, 3]),
                ("acceptance heading, any level", "### Acceptance criteria\n\n1) a\n2) b\n", [1, 2]),
                ("bold line", "**Done means**\n1. a\n2. b\n", [1, 2]),
                ("bold line with colon", "**Acceptance criteria:**\n\n1. a\n", [1]),
                ("plain line with colon", "Done means:\n1. a\n2. b\n", [1, 2]),
                ("case", "## DONE MEANS\n1. a\n", [1]),
                ("no list", "## Done means\n- a bullet\n- another\n", []),
                ("no heading", "1. a\n2. b\n", []),
                ("list under another heading", "## Why\n1. a\n2. b\n", []),
                ("a sub-list is not an item", "## Done means\n1. a\n    1. nested\n    2. nested\n2. b\n", [1, 2]),
                ("a list in a fence is not read", "## Done means\n```\n1. a\n```\n", [])):
            with self.subTest(name):
                self.assertEqual(review_verdict.acceptance_items(text), want)

    def test_two_sources_are_joined(self):
        self.assertEqual(review_verdict.acceptance_items(BRIEF, "## Acceptance criteria\n1. x\n4. y\n"), [1, 2, 3, 4])

    def test_a_reply_naming_every_item_is_complete(self):
        for section in ("## Acceptance criteria\n1. met - a.py:3\n2. not met - no test\n3. not checked - no tool\n",
                        "**Acceptance criteria**\n- 1: met, test_a\n- 2: met, test_b\n- 3: met, test_c\n",
                        "Acceptance criteria\n| 1 | met | a.py:1 |\n| 2 | met | a.py:2 |\n| 3 | met | a.py:3 |\n"):
            with self.subTest(section=section):
                text = review_verdict.ReviewText(reply(section), [1, 2, 3])
                self.assertIsNone(review_verdict.assess(text)[2])
                self.assertTrue(review_verdict.approves(text))

    def test_a_reply_without_the_section_or_with_fewer_numbers_is_incomplete_and_the_reason_says_which(self):
        for name, body, fragment in (
                ("no section", "all fine\n", "no Acceptance criteria section"),
                ("two of three", "## Acceptance criteria\n1. met - x\n2. met - y\n", "3"),
                ("none named", "## Acceptance criteria\nall met\n", "1, 2, 3"),
                ("the numbers sit in another section", "## Notes\n1. met\n2. met\n3. met\n", "no Acceptance criteria section")):
            with self.subTest(name):
                text = review_verdict.ReviewText(reply(body), [1, 2, 3])
                verdict, _, problem = review_verdict.assess(text)
                self.assertEqual(verdict, "APPROVED")  # the stated verdict is kept for the record
                self.assertIn("Acceptance criteria", problem)
                self.assertIn(fragment, problem)
                self.assertFalse(review_verdict.approves(text))

    def test_the_section_ends_at_the_next_heading_and_later_numbers_do_not_count(self):
        for name, after in (("plain Coverage", "Coverage\n2. later row about a.py and its tests\n"),
                            ("bold-only line", "**Notes**\n2. a note, not a row\n"),
                            ("markdown heading", "## Notes\n2. a note, not a row\n")):
            with self.subTest(name):
                text = review_verdict.ReviewText(reply("## Acceptance criteria\n1. met - a.py:3\n" + after), [1, 2])
                problem = review_verdict.assess(text)[2]
                self.assertIn("item 2", problem or "")
                self.assertFalse(review_verdict.approves(text))

    def test_rows_before_the_ending_heading_still_count(self):
        text = review_verdict.ReviewText(reply("## Acceptance criteria\n1. met - a\n2. met - b\n\n**Notes**\n3. other\n"), [1, 2])
        self.assertIsNone(review_verdict.assess(text)[2])

    def test_briefs_named_in_a_request_are_the_absolute_md_paths_in_it(self):
        request = "Read and carry out the brief /x/dir/brief.md in full. Also `/y/other.md`, (/z/c.md) and /w/n.md. Not a.md, ./b.md or /x/dir/brief.md.txt or /q/r.mdx\n"
        self.assertEqual(review_verdict.named_briefs(request), ["/x/dir/brief.md", "/y/other.md", "/z/c.md", "/w/n.md"])
        self.assertEqual(review_verdict.named_briefs("/x/brief.md and /x/brief.md\n"), ["/x/brief.md"])
        self.assertEqual(review_verdict.named_briefs("nothing here"), [])

    def test_the_briefs_command_prints_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp, "request.md")
            request.write_text("Read and carry out the brief /x/brief.md\n")
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), "briefs", str(request)], capture_output=True, text=True)
            self.assertEqual(json.loads(out.stdout), {"briefs": ["/x/brief.md"]})

    def test_a_missing_item_names_only_the_missing_numbers(self):
        problem = review_verdict.assess(review_verdict.ReviewText(reply("## Acceptance criteria\n1. met\n3. met\n"), [1, 2, 3]))[2]
        self.assertIn("item 2", problem)
        self.assertNotIn("1, 2", problem)

    def test_a_reply_that_is_also_missing_coverage_still_says_so_first(self):
        text = review_verdict.ReviewText("APPROVED\n## Acceptance criteria\n1. met\n", [1])
        self.assertEqual(review_verdict.assess(text)[2], "the review has no Coverage section")

    def test_without_a_list_nothing_changes(self):
        plain = reply("anything")
        self.assertIsNone(review_verdict.assess(plain)[2])
        self.assertIsNone(review_verdict.assess(review_verdict.ReviewText(plain, []))[2])
        self.assertTrue(review_verdict.approves(plain))

    def test_read_review_applies_the_items_recorded_beside_the_reply_to_every_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "result.md")
            path.write_text(reply("## Acceptance criteria\n1. met\n"))
            self.assertIsNone(review_verdict.assess(review_verdict.read_review(path))[2])  # no marker: the old rule
            Path(tmp, "acceptance-items.json").write_text(json.dumps({"items": [1, 2]}))
            self.assertIn("2", review_verdict.assess(review_verdict.read_review(path))[2])
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(path)], capture_output=True, text=True)
            value = json.loads(out.stdout)
            self.assertFalse(value["complete"])
            self.assertIn("Acceptance criteria", value["incomplete_reason"])
            Path(tmp, "acceptance-items.json").write_text("{not json")  # a marker that cannot be read refuses, never defaults
            with self.assertRaises(ValueError):
                review_verdict.read_review(path)

    def test_the_acceptance_command_prints_the_numbers_of_the_files_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a.md"), Path(tmp, "b.md")
            a.write_text(BRIEF)
            b.write_text("nothing here\n")
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), "acceptance", str(a), str(b)], capture_output=True, text=True)
            self.assertEqual(json.loads(out.stdout), {"items": [1, 2, 3]})
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), "acceptance", str(b)], capture_output=True, text=True)
            self.assertEqual(json.loads(out.stdout), {"items": []})


if __name__ == "__main__":
    unittest.main()
