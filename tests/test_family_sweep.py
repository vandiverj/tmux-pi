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
        self.assertEqual([(names, tokens) for _, names, tokens, _ in got],
                         [(["date-parse"], ["lib/a.py:12", "lib/b.py:30-34"]), ([], [])])
        self.assertNotIn("never_listed", " ".join(text for text, _, _, _ in got))

    def test_a_reply_that_is_not_a_verdict_has_no_findings(self):
        self.assertEqual(family_sweep.findings("looks fine at lib/a.py:1"), [])


SHAPES = [  # (input shape, text, expected sites): every row failed or was not covered before the rework
    ("plain", "at lib/a.py:12 here", ["lib/a.py:12"]),
    ("backticked", "at `lib/a.py:12` here", ["lib/a.py:12"]),
    ("fenced", "x\n```\nlib/a.py:12: boom\n```", ["lib/a.py:12"]),
    ("tilde fenced", "x\n~~~\nlib/a.py:12\n~~~", ["lib/a.py:12"]),
    ("line range", "lib/a.py:12-15", ["lib/a.py:12-15"]),
    ("en dash range", "lib/a.py:12\u201315", ["lib/a.py:12-15"]),
    ("line and column", "lib/a.py:12:7", ["lib/a.py:12"]),
    ("quoted path with space, backticks", "`my dir/a file.py:12`", ["my dir/a file.py:12"]),
    ("quoted path with space, double quotes", 'see "my dir/a.py:3-4" now', ["my dir/a.py:3-4"]),
    ("escaped space", "my\\ dir/a.py:12", []),
    ("windows path", "C:\\repo\\lib\\a.py:12", ["C:\\repo\\lib\\a.py:12"]),
    ("windows path, forward slashes", "C:/repo/lib/a.py:12", ["C:/repo/lib/a.py:12"]),
    ("windows relative", "lib\\a.py:12", ["lib\\a.py:12"]),
    ("windows path with a space in a directory", "C:\\Program Files\\x\\a.py:12", []),
    ("absolute posix", "in /srv/app/lib/a.py:12", ["/srv/app/lib/a.py:12"]),
    ("home relative", "in ~/proj/a.py:7", ["~/proj/a.py:7"]),
    ("dot relative", "in ./lib/a.py:7 and ../x/b.py:8", ["./lib/a.py:7", "../x/b.py:8"]),
    ("URL with port", "see http://example.com:8080/x and example.com:80", []),
    ("URL then a path", "http://localhost:8080/ then lib/a.py:5", ["lib/a.py:5"]),
    ("host:port in prose", "connect to db.example.com:5432 first", []),
    ("ip:port", "127.0.0.1:8080", []),
    ("no extension, with directory", "bin/tp-agent:1402", ["bin/tp-agent:1402"]),
    ("no extension, bare name", "Makefile:3 and Note:12", ["Makefile:3"]),
    ("trailing punctuation", "(lib/a.py:12), lib/b.py:3. lib/c.py:4; lib/d.py:5!", ["lib/a.py:12", "lib/b.py:3", "lib/c.py:4", "lib/d.py:5"]),
    ("markdown link", "[x](lib/a.py:12)", ["lib/a.py:12"]),
    ("a time and a version", "at 12:30 on v1.2:4", []),
    ("path then word, not a line", "lib/a.py:fix", []),
    ("scoped package path", "node_modules/@scope/pkg/i.js:9", ["node_modules/@scope/pkg/i.js:9"]),
    ("UNC path", "\\\\server\\share\\a.py:4", ["\\\\server\\share\\a.py:4"]),
    ("file URL", "file:///srv/a.py:9", []),
    ("comma list keeps the first line only", "lib/a.py:12,14", ["lib/a.py:12"]),
    ("repeat", "lib/a.py:1 lib/a.py:1", ["lib/a.py:1"]),
    ("unquoted line and column", "bin/tp-agent:1402:9 here", ["bin/tp-agent:1402"]),
    ("backticked line and column", "`lib/a.py:12:7`", ["lib/a.py:12"]),
    ("backticked range", "`lib/a.py:12-15`", ["lib/a.py:12-15"]),
    ("quoted spaced path with column", "`my dir/a file.py:12:7`", ["my dir/a file.py:12"]),
    ("double-quoted spaced path with column", 'see "my dir/a.py:3:1" now', ["my dir/a.py:3"]),
    ("single-quoted spaced path", "see 'my dir/a.py:3-4' now", ["my dir/a.py:3-4"]),
    ("windows spaced path with column, quoted", "`C:\\Program Files\\x\\a.py:12:3`", ["C:\\Program Files\\x\\a.py:12"]),
    ("quoted span holding two sites", "`lib/a.py:1 lib/b.py:2`", []),
    ("quoted span holding two spaced sites", "`my dir/a.py:1 and my dir/b.py:2`", []),
    ("quoted span with a site and prose", "`lib/a.py:12 is wrong`", []),
    ("quoted span with prose before the site", '"see lib/a.py:12 now"', []),
    ("quoted spaced path with trailing junk", "`my dir/a.py:12 extra`", []),
    ("quoted span that is not a site", "run `git grep -n foo` at lib/a.py:3", ["lib/a.py:3"]),
    ("quoted host:port is neither", "`db.example.com:5432`", []),
    ("apostrophes are not quotes", "the reviewer's lib/a.py:12 and the author's bin/x:3", ["lib/a.py:12", "bin/x:3"]),
    ("contractions", "don't see lib/a.py:3, it's fine", ["lib/a.py:3"]),
    ("padded code span", "` lib/a.py:3 `", ["lib/a.py:3"]),
    ("two sites in separate spans", "`x.py:1`\n`y.py:2`", ["x.py:1", "y.py:2"]),
    ("site after a quoted span", "`my dir/a.py:1` then lib/b.py:2", ["my dir/a.py:1", "lib/b.py:2"]),
]
AMBIGUOUS_QUOTED = ["`lib/a.py:1 lib/b.py:2`", "`my dir/a.py:1 and my dir/b.py:2`", "`lib/a.py:12 is wrong`", '"see lib/a.py:12 now"',
                    "`my dir/a.py:12 extra`", "`lib/a.py`:12", "`lib/a.py:12:`"]


class ShapeTests(unittest.TestCase):
    def test_every_input_shape_gives_the_expected_sites(self):
        for name, text, expected in SHAPES:
            with self.subTest(name):
                self.assertEqual(family_sweep.sites(text), expected)

    def test_a_quoted_span_that_is_not_exactly_one_site_is_ambiguous_and_gives_nothing(self):
        for text in AMBIGUOUS_QUOTED:
            with self.subTest(text):
                self.assertEqual(family_sweep.scan(text), ([], True))

    def test_an_ambiguous_path_is_flagged_not_emitted(self):
        for text in ("my\\ dir/a.py:12", "C:\\Program Files\\x\\a.py:12"):
            with self.subTest(text):
                self.assertEqual(family_sweep.scan(text), ([], True))


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
        self.assertIn("Findings with no file:line site, verbatim", text)
        self.assertIn("> F3 [defect] the error path is odd somewhere in the importer.", text)
        self.assertIn("From review review-1, finding 1:", text)

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

    def test_a_finding_that_fits_is_quoted_whole_however_long(self):
        long = "F1 [defect] " + "word " * 1500 + "at lib/deep.py:5\nCloses by: code"
        text = family_sweep.build(self.state(review(long), review(long)), "author-2", None)
        self.assertIn("> " + long.split("\n")[0], text)
        self.assertNotIn("shortened", text)
        self.assertIn("- `lib/deep.py:5` (cited by review-1, review-2)", text)

    def test_over_budget_findings_become_pointers_and_the_sites_stay_complete(self):
        big = "F%d [defect] " + "word " * 3000 + "at lib/s%d.py:%d\nCloses by: code"
        bigs = [big % (n, n, n) for n in range(1, 5)]
        state = self.state(review(*bigs), review(*bigs))
        text = family_sweep.build(state, "author-2", None)
        self.assertLessEqual(len(text.encode()), family_sweep.SWEEP_BUDGET)
        self.assertLess(family_sweep.SWEEP_BUDGET, family_sweep.SPAWN_MAX_FILE)
        for n in range(1, 5):
            self.assertIn(f"- `lib/s{n}.py:{n}` (cited by review-1, review-2)", text)
            self.assertIn(f"| `lib/s{n}.py:{n}` | | |", text)
        pointers = [line for line in text.split("\n") if line.startswith("- review review-2, finding ")]
        self.assertTrue(pointers)
        self.assertIn("/tasks/review-2/result.md", pointers[0])
        quoted = text.count("\n> F")
        self.assertEqual(quoted + len(pointers), 4)  # every finding of the latest review is quoted whole or pointed at, none cut
        self.assertNotIn("[...", text)

    def test_a_sweep_whose_sites_alone_exceed_the_budget_raises_and_names_nothing_partial(self):
        sites = " ".join(f"lib/f{n}.py:{n}" for n in range(1, 4000))
        with self.assertRaises(family_sweep.SweepTooLarge):
            family_sweep.build(self.state(review(f"F1 [defect] {sites}\nCloses by: code"), review(F2)), "author-2", None)

    def test_review_text_is_redacted_before_it_reaches_sweep_md(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789ab"
        first = f"F1 [defect] token={secret} leaks at lib/a.py:12 and {secret}\nFamily: leak\nCloses by: code"
        second = f"F2 [defect] no site, still token={secret}\nCloses by: code"
        text = family_sweep.build(self.state(review(first), review(second)), "author-2", None)
        self.assertNotIn(secret, text)
        self.assertIn("REDACTED", text)
        self.assertIn("`lib/a.py:12`", text)

    def test_a_secret_shaped_value_in_no_input_slot_reaches_sweep_md(self):
        def key(tag):  # an AWS-access-key-shaped value the redaction pass recognises, built here so no secret sits in this file
            return "AKIA" + (tag * 16)[:16]
        slots = {"family name": key("A"), "gate family": key("B"), "site path": key("C"), "finding body": key("D"), "fenced code": key("E"),
                 "family name, round 2": key("F"), "loose finding": key("G")}
        first = (f"F1 [defect] body {slots['finding body']} at lib/{slots['site path']}.py:12\nFamily: {slots['family name']}\nCloses by: code\n\n"
                 f"F2 [defect] fenced:\n```\nlib/x.py:3 {slots['fenced code']}\n```\nFamily: {slots['family name']}\nCloses by: code")
        second = (f"F1 [defect] no site {slots['loose finding']}\nFamily: {slots['family name, round 2']}\nCloses by: code\n\n"
                  f"F2 [defect] again {slots['gate family']}\nFamily: {slots['gate family']}\nCloses by: code")
        for gate in (slots["gate family"], slots["gate family"].lower(), slots["family name"].lower(), None):  # the gate passes names lower-cased
            with self.subTest(gate=gate):
                text = family_sweep.build(self.state(review(first), review(second)), "author-2", gate)
                for slot, value in slots.items():
                    self.assertNotIn(value, text, slot)
                    self.assertNotIn(value.lower(), text, slot)
                self.assertIn("REDACTED", text)
                if gate and gate != gate.upper():
                    self.assertIn("a family whose name the redaction pass changed", text)

    def test_a_redactor_that_cannot_load_refuses_instead_of_writing_unredacted_text(self):
        state = self.state(review(F1), review(F2))
        original = review_rounds.redactor
        review_rounds.redactor = lambda: (_ for _ in ()).throw(review_rounds.Refused("no redactor"))
        self.addCleanup(setattr, review_rounds, "redactor", original)
        with self.assertRaises(review_rounds.Refused):
            family_sweep.build(state, "author-2", None)

    def test_sites_in_fenced_code_are_listed_and_the_fence_is_in_the_quote(self):
        fenced = "F1 [defect] run this:\n```\nlib/in_fence.py:9: bad call\n```\nCloses by: code"
        text = family_sweep.build(self.state(review(F2), review(fenced)), "author-2", None)
        self.assertIn("- `lib/in_fence.py:9` (cited by review-2)", text)
        self.assertIn("```", text)

    def test_a_family_line_inside_a_fence_is_not_a_family_name(self):
        fenced = "F1 [defect] example:\n```\nFamily: fake\nCloses by: doc\n```\nat lib/a.py:3\nFamily: real\nCloses by: code"
        got = family_sweep.findings(review(fenced))
        self.assertEqual([(names, tokens) for _, names, tokens, _ in got], [(["real"], ["lib/a.py:3"])])

    def test_an_ambiguous_path_is_listed_verbatim_not_cut(self):
        one = "F1 [defect] see my\\ dir/a.py:12 for it\nCloses by: code"
        text = family_sweep.build(self.state(review(one), review(F2)), "author-2", None)
        self.assertNotIn("`dir/a.py:12`", text)
        self.assertIn("Findings with no file:line site, verbatim", text)
        self.assertIn("> F1 [defect] see my\\ dir/a.py:12 for it", text)


    def test_a_chain_that_cannot_be_walked_raises_refused(self):
        with self.assertRaises(review_rounds.Refused):
            family_sweep.build(tempfile.gettempdir() + "/no-such-state", "author-1", None)

    def test_is_sweep_only_for_a_sweep_in_a_task_directory(self):
        self.assertTrue(family_sweep.is_sweep("/s/tasks/coder-1/sweep.md"))
        self.assertFalse(family_sweep.is_sweep("/s/docs/sweep.md"))
        self.assertFalse(family_sweep.is_sweep("/s/tasks/coder-1/other.md"))



TABLE = """Done.

| Site | Disposition | Evidence |
| --- | --- | --- |
| `lib/a.py:12` | fixed | changed the parse |
| `bin/tp-x:7` | **Unresolved** | needs the owner. Closes by: decision, owner picks the format |
| `lib/b.py:30-34` | `unresolved` | blocked, Closes by: none |
| (sites you found) | | |

RESULT_STATUS: complete
"""


class ReportTableTests(unittest.TestCase):
    def test_rows_are_read_with_the_disposition_folded(self):
        rows = family_sweep.report_table(TABLE)
        self.assertEqual([(r[0], r[1]) for r in rows], [("`lib/a.py:12`", "fixed"), ("`bin/tp-x:7`", "unresolved"),
                                                          ("`lib/b.py:30-34`", "unresolved"), ("(sites you found)", "")])

    def test_no_table_is_none_and_the_last_table_wins(self):
        self.assertIsNone(family_sweep.report_table("no table here\n| a | b |\n"))
        two = TABLE + "\n| site | disposition | evidence |\n|---|---|---|\n| x.py:1 | fixed | y |\n"
        self.assertEqual([r[0] for r in family_sweep.report_table(two)], ["x.py:1"])

    def test_an_escaped_pipe_stays_in_its_cell(self):
        rows = family_sweep.report_table("| Site | Disposition | Evidence |\n| - | - | - |\n| a.py:1 | fixed | a \\| b |\n")
        self.assertEqual(rows[0][2], "a | b")

    def test_open_rows_are_unresolved_ones_without_a_real_closes_by_note(self):
        self.assertEqual(family_sweep.open_rows(family_sweep.report_table(TABLE)), ["`lib/b.py:30-34`"])
        self.assertEqual(family_sweep.open_rows(None), [])

    def test_a_note_is_closes_by_followed_by_something_that_says_something(self):
        for evidence, open_ in (("Closes by: decision", False), ("**Closes by:** check, the log", False), ("closes by:", True),
                                ("Closes by: tbd", True), ("closes by", True), ("no note", True)):
            rows = [("a.py:1", "unresolved", evidence)]
            self.assertEqual(bool(family_sweep.open_rows(rows)), open_, evidence)


class PrepareReviewTests(unittest.TestCase):
    def test_the_evidence_file_holds_the_sweep_and_the_rows_and_the_open_ones_are_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep, result = Path(tmp, "sweep.md"), Path(tmp, "result.md")
            sweep.write_text("# Sweep: finding family `date-parse`\n\nbody\n")
            result.write_text(TABLE)
            out = family_sweep.prepare_review(sweep, result, Path(tmp, "out"))
            self.assertEqual(out["open"], ["`lib/b.py:30-34`"])
            self.assertEqual(out["rows"], 4)
            self.assertEqual(out["family"], "date-parse")
            text = Path(out["file"]).read_text()
            self.assertIn("body", text)
            self.assertIn("| `bin/tp-x:7` | unresolved |", text)

    def test_no_table_and_an_unreadable_report_are_said_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sweep = Path(tmp, "sweep.md")
            sweep.write_text("# Sweep: the unnamed family (REQUEST_CHANGES twice in a row)\n")
            out = family_sweep.prepare_review(sweep, Path(tmp, "missing.md"), Path(tmp, "out"))
            self.assertEqual((out["open"], out["rows"], out["family"]), ([], 0, None))
            self.assertIn("no table", Path(out["file"]).read_text())

class ReportFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / "sweep.md").write_text("# Sweep: finding family `date-parse`\n")

    def run_review(self, report=True):
        return family_sweep.prepare_review(self.dir / "sweep.md", self.dir / "result.md", self.dir / "out",
                                           self.dir / "report.md" if report else None)

    def test_report_only_both_and_neither(self):
        (self.dir / "result.md").write_text("see report\n")
        (self.dir / "report.md").write_text(TABLE)
        self.assertEqual((self.run_review()["open"], self.run_review()["source"]), (["`lib/b.py:30-34`"], "report.md"))
        (self.dir / "result.md").write_text("| Site | Disposition | Evidence |\n| - | - | - |\n| a | fixed | x |\n")
        self.assertEqual(self.run_review()["rows"], 4)
        (self.dir / "report.md").unlink()
        self.assertEqual((self.run_review()["rows"], self.run_review()["source"]), (1, "result.md"))
        (self.dir / "result.md").write_text("nothing\n")
        self.assertEqual((self.run_review()["open"], self.run_review()["source"]), ([], None))

    def test_a_report_without_a_table_falls_back_to_the_result(self):
        (self.dir / "report.md").write_text("no table\n")
        (self.dir / "result.md").write_text(TABLE)
        self.assertEqual(self.run_review()["source"], "result.md")

    def test_an_unreadable_report_and_a_missing_named_report_are_open_rows(self):
        (self.dir / "result.md").write_text("x\n")
        (self.dir / "report.md").mkdir()  # exists, cannot be read as a file
        out = self.run_review()
        self.assertEqual(len(out["open"]), 1)
        self.assertIn("cannot be read", out["open"][0])
        (self.dir / "report.md").rmdir()
        (self.dir / "result.md").write_text(f"see {self.dir}/report.md\n")
        self.assertIn("missing", self.run_review()["open"][0])


if __name__ == "__main__":
    unittest.main()
