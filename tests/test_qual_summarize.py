"""The summarize_files qualification kit: tests/calibration/summarize/ (chores, answers, fixtures), tools/qual/summarize_check.py
(the scorer) and tools/qual/summarize_run.py (the runner). No model is run: the worker is a stub script."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "qual"))
import summarize_check
import summarize_run

DATA = ROOT / "tests" / "calibration" / "summarize"
FIXTURES = DATA / "fixtures"
MODEL = "lmstudio/qwen/qwen3.6-35b-a3b"


# What bin/tp-route really prints, captured 2026-11-08 with `tp-route --no-log --local-only "<task>"` (the first from a run with the
# local router up; the others from the same code with a stub router saying shape summarize_files, which the catalog calls candidate).
# There is no "shape" key in the output, in the selection record or anywhere else the tool writes.
ROUTE_OK = ('{"worker":"pi","model":"lmstudio/qwen/qwen3.6-35b-a3b","thinking":"off","level":"local","provider":"local",'
            '"effort":"off","effort_abstract":"low","review":"optional","router_review":"none","source":"router_resolved",'
            '"reason":"Task asks to find files using \'X\', which is a bounded search/count operation suitable for Level 1.",'
            '"decided_by":"lmstudio/qwen/qwen3.6-35b-a3b","steps":["local-only: level local"],"approvals":{"effort_above_high":false},'
            '"local_only":true,"catalog_version":1,"catalog_sha":"2fffdc99a6ef","prompt_sha256":"24f00e231b59",'
            '"card_sha256":"2a1d216518c642fcd8da01bc0285384d4968a18efb58f40146cc91e28098adb0","selection_id":null,"substituted":true}')
ROUTE_SHAPE_STOP = "tp-route: stop and ask the user: local-only task outside the qualified local shapes\n"  # exit 2
ROUTE_REFUSED = "tp-route: local-only task: level mid has no local model; choose a level with a local model (local)\n"  # exit 1


def jsonl(name):
    return summarize_run.read_jsonl(DATA / name)


def reference(answer):
    return "\n".join(s["text"] for s in answer["reference"])


CITE = re.compile(r"\[([^\[\]]*)\]$")  # the trailing citation list of a fact line
_LINES = {}


def fixture_lines(name):
    if name not in _LINES:
        _LINES[name] = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return _LINES[name]


def labels_of(answer):
    return [fact[0] for fact in answer["required"]]


def stating(answer, k):
    """Indexes into answer["reference"] of the fact lines that state fact k."""
    return [i for i, s in enumerate(answer["reference"]) if k in s["facts"]]


def reply_with(answer, changes):
    """The reference reply with the fact lines in changes ({index: new text}) replaced; None drops one."""
    texts = [changes.get(i, s["text"]) for i, s in enumerate(answer["reference"])]
    return "\n".join(t for t in texts if t is not None)


def cited(text, citation):
    """The fact line text with its citation list replaced by citation."""
    return CITE.sub("[" + citation + "]", text)


def failed(result):
    return result["missing"] + result["missing_citation"] + result["unsupported"]


def rules(result):
    """{(rule, kind)} of the failures: which rule each failed line (or fact) broke."""
    return {(f["rule"], f["kind"]) for f in result["failures"]}


def answer_of(chore_id):
    return next(a for a in jsonl("answers.jsonl") if a["id"] == chore_id)


class DataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chores = jsonl("chores.jsonl")
        cls.answers = {a["id"]: a for a in jsonl("answers.jsonl")}

    def facts(self):
        """(chore id, answer, k) for every required fact of every chore."""
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            for k in range(len(answer["required"])):
                yield chore["id"], answer, k

    def check(self, reply, chore_id):
        return summarize_check.check(reply, self.answers[chore_id], FIXTURES)

    def test_chores_and_answers_pair_up(self):
        ids = [c["id"] for c in self.chores]
        self.assertEqual(len(ids), 24)
        self.assertEqual(len(set(ids)), 24)
        self.assertEqual(set(ids), set(self.answers))
        self.assertEqual(len(self.answers), 24)

    def test_splits_are_12_and_12(self):
        splits = [c["split"] for c in self.chores]
        self.assertEqual((splits.count("tune"), splits.count("holdout")), (12, 12))
        self.assertEqual(len(splits), 24)

    def test_every_named_fixture_exists_and_is_cited_only_from_named_files(self):
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            self.assertEqual(answer["cite_files"], chore["files"], chore["id"])
            for name in chore["files"]:
                self.assertTrue((FIXTURES / name).is_file(), "{}: {}".format(chore["id"], name))
                self.assertIn(name, chore["task"], chore["id"])

    def test_support_is_a_set_of_lines_and_every_anchor_is_on_its_line(self):
        # support_lines: the fixture lines that are evidence of a fact; anchors: a substring of each, so an edit of a fixture that
        # moves the evidence breaks this test and not the scoring quietly.
        for chore_id, answer, k in self.facts():
            where = "{} fact {}".format(chore_id, k)
            self.assertNotIn("support", answer, where)
            self.assertEqual((len(answer["support_lines"]), len(answer["anchors"])), (len(answer["required"]),) * 2, chore_id)
            sets = summarize_check.support_sets(answer["support_lines"][k])
            anchors = answer["anchors"][k]
            self.assertTrue(sets, where)
            self.assertEqual(set(anchors), set(sets), where)
            for name, lines in sets.items():
                self.assertIn(name, answer["cite_files"], where)
                self.assertLessEqual(max(lines), len(fixture_lines(name)), where)
                self.assertEqual({int(n) for n in anchors[name]}, lines, where)
                for n, text in anchors[name].items():
                    self.assertTrue(text.strip() and text in fixture_lines(name)[int(n) - 1], "{} {}:{} {!r}".format(where, name, n, text))

    def test_patterns_compile_and_the_reference_reply_passes(self):
        # Main's probe: the references pass, 24 of 24. Each reference line is a fact line (rule 1) and the reply holds nothing else.
        passed = 0
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            self.assertTrue(len(answer["required"]) >= 2 and answer["forbidden"], chore["id"])
            for fact in answer["required"]:
                for alternative in fact:
                    re.compile(alternative)
            for pattern in answer["forbidden"]:
                re.compile(pattern)
            for sentence in answer["reference"]:
                claim, cites, kind = summarize_check.parse_fact_line(sentence["text"])
                self.assertEqual((kind, len(cites or [])), (None, 1), sentence["text"])  # one fact line, one citation
                self.assertNotIn("`", sentence["text"])
            result = self.check(reference(answer), chore["id"])
            self.assertTrue(result["pass"], "{}: {}".format(chore["id"], result))
            self.assertEqual((result["unchecked"], result["failures"], result["uncited"]), (0, [], []), chore["id"])
            passed += result["pass"]
            empty = self.check("", chore["id"])
            self.assertFalse(empty["pass"], chore["id"])
            self.assertEqual(len(empty["missing"]), len(answer["required"]), chore["id"])
            self.assertEqual(rules(empty), {(4, "missing")}, chore["id"])  # nothing stated: rule 4, for every fact
        self.assertEqual(passed, 24)

    def test_every_fact_is_needed_and_the_reference_is_complete(self):
        # The reference reply is hand-written, one fact line per fact (reference[].facts says which facts it states). It passes;
        # and with the lines that state fact k removed the reply fails, with k among the failed facts: `missing` when nothing else
        # states it, `unsupported` when another line names it (an export's name, in the line on what it does) but cites lines that
        # are not the evidence of k. So each fact is needed and the reference leaves none out.
        for chore_id, answer, k in self.facts():
            labels = labels_of(answer)
            kept = [s for s in answer["reference"] if k not in s["facts"]]
            gone = [n for n in range(len(labels)) if not any(n in s["facts"] for s in kept)]
            result = summarize_check.check("\n".join(s["text"] for s in kept), answer, FIXTURES)
            where = "{} without fact {}: {}".format(chore_id, k, result)
            self.assertFalse(result["pass"], where)
            self.assertIn(labels[k], failed(result), where)
            self.assertEqual(result["missing"], [labels[n] for n in gone if labels[n] in result["missing"]], where)
            self.assertTrue(set(labels[n] for n in gone) <= set(failed(result)), where)
            self.assertEqual((result["order"], result["forbidden_hit"], result["bad_citations"], result["uncited"]), ([], [], [], []), where)
            # nothing else fails but the facts only those lines stated and the facts they were the evidence of
            self.assertEqual(sorted(set(failed(result)) - {labels[n] for n in gone} - {labels[k]}), [], where)
            self.assertTrue(rules(result) <= {(4, "missing"), (3, "unsupported"), (3, "missing_citation")}, where)

    def test_names_and_signature_text_alone_fail_rule_5(self):
        # Round 5 / rule 5: `NAME [file:SIGNATURE_LINE]`, the signature as written (parameter names included), or both, in place of
        # the line on what NAME does: every behaviour fact must then be `missing`, with rule 5, however it is cited.
        count = 0
        for chore_id, answer, k in self.facts():
            behaviour = answer.get("behaviour", {}).get(str(k))
            if behaviour is None:
                continue
            count += 1
            name, where_sig = behaviour["name"], behaviour["sig"]
            sig_file, sig_line = where_sig.rsplit(":", 1)
            signature = fixture_lines(sig_file)[int(sig_line) - 1].strip()
            for form in ("- {name} [{at}]", "- `{sig}` [{at}]", "- {name}: `{sig}` [{at}]", "- {name}: {sig} [{at}]", "- {sig} [{at}]"):
                line = form.format(name=name, at=where_sig, sig=signature)
                text = "{} fact {} {}".format(chore_id, k, line)
                result = summarize_check.check(reply_with(answer, {i: line for i in stating(answer, k)}), answer, FIXTURES)
                self.assertFalse(result["pass"], text)
                self.assertIn(labels_of(answer)[k], failed(result), text)  # not met by that line (met by no line, or only by one that cites other lines)
                self.assertEqual((result["bad_citations"], result["forbidden_hit"], result["uncited"]), ([], [], []), text)
                self.assertIn(line, result["identifier_only"], text)
                self.assertIn((5, "identifier_only"), rules(result), text)  # the line is no claim: rule 5
        self.assertGreaterEqual(count, 60)
        for chore in self.chores:
            if chore["id"].startswith("exports-") and chore["id"] != "exports-settings":
                self.assertTrue(self.answers[chore["id"]].get("behaviour"), chore["id"])  # an exports chore asks what each does

    def test_a_behaviour_fact_is_met_by_words_and_not_by_names(self):
        # Rule 5 from the other side: the reference claims of a behaviour fact are made of words, so they still meet it with every
        # name removed from them (the checker removes them before it matches).
        count = 0
        for chore_id, answer, k in self.facts():
            if str(k) not in answer.get("behaviour", {}):
                continue
            names = summarize_check.names_in_code(FIXTURES, answer["cite_files"])
            for i in stating(answer, k):
                claim = summarize_check.parse_fact_line(answer["reference"][i]["text"])[0]
                self.assertTrue(any(re.search(a, summarize_check.without_names(claim, names), re.I) for a in answer["required"][k]),
                                "{} fact {}: {}".format(chore_id, k, claim))
                count += 1
        self.assertGreaterEqual(count, 65)

    def test_the_reviewers_reproducers_fail_for_their_rule(self):
        # Every reproducer of reviews 2-5, as a reply, with the rule it must fail.
        jobq, utils, cache, states = (self.answers[c] for c in ("exports-jobq", "exports-utils", "sum-cache", "steps-states"))
        job = next(k for k, f in enumerate(jobq["required"]) if jobq["behaviour"].get(str(k), {}).get("name") == "Job")
        chunked = next(k for k in range(len(utils["required"])) if utils["behaviour"].get(str(k), {}).get("name") == "chunked")
        # F1: an identifier and its parameter names are not what the code does -- rule 5
        result = summarize_check.check(reply_with(jobq, {i: "- Job(name, payload, priority=0) [src/jobq.py:15]" for i in stating(jobq, job)}),
                                       jobq, FIXTURES)
        self.assertIn(labels_of(jobq)[job], failed(result))
        self.assertEqual(result["identifier_only"], ["- Job(name, payload, priority=0) [src/jobq.py:15]"])
        self.assertIn((5, "identifier_only"), rules(result))
        # F1: chunked's name and signature, as the fact line and as the round-5 prose (which is no fact line at all -- rule 4)
        signature = "- chunked(items, size) [src/utils.py:16]"
        prose = "chunked (src/utils.py:16 `def chunked(items, size):`)."
        for text, expected in ((signature, (5, "identifier_only")), (prose, (4, "missing"))):
            result = summarize_check.check(reply_with(utils, {i: text for i in stating(utils, chunked)}), utils, FIXTURES)
            self.assertFalse(result["pass"], text)
            self.assertIn(labels_of(utils)[chunked], failed(result), text)
            self.assertIn(expected, rules(result), text)
            self.assertEqual(result["identifier_only"], [signature] if text == signature else [], text)
        # F1: the heading-spanning creation bullet: the heading says the file and the bullet says the claim, and neither is a fact line
        jobqueue = self.answers["use-jobqueue"]
        for text in ("### src/cli.py creates a JobQueue\n- Line 29", "### src/cli.py\n- creates a JobQueue (Line 29)",
                     "**src/cli.py**\n- creates a JobQueue\n  - Line 29"):
            result = summarize_check.check(reply_with(jobqueue, {0: text}), jobqueue, FIXTURES)
            self.assertFalse(result["pass"], text)
            self.assertEqual(result["missing"], [labels_of(jobqueue)[0]], text)
            self.assertIn((4, "missing"), rules(result), text)
            if "- " in text:
                self.assertIn((2, "uncited"), rules(result), text)  # a "- " line without its own citation list
        # F3: a heading of any file name is ignored (rule 2), whatever its extension; the lines under it are uncited bullets
        for heading in ("### archive.7z", "### nonexistent.xyz", "### src/nonexistent.rs", "### src/cache.py.bak"):
            result = summarize_check.check(reference(cache) + "\n" + heading + "\n- LRUCache Line 6\n", cache, FIXTURES)
            self.assertFalse(result["pass"], heading)
            self.assertEqual((result["bad_citations"], result["uncited"]), ([], ["- LRUCache Line 6"]), heading)
            self.assertEqual(rules(result), {(2, "uncited")}, heading)
        # F3: a fact line that names such a file itself is a bad citation (rule 3), and does not fall back to a file above it
        for path in ("archive.7z", "nonexistent.xyz", "src/nonexistent.rs", "src/cache.py.bak", "docs/deploy.md"):
            result = summarize_check.check("{}\n- this entry is an extra remark [{}:6]".format(reference(cache), path), cache, FIXTURES)
            self.assertFalse(result["pass"], path)
            self.assertEqual(result["bad_citations"], [path + ":6 (unknown_file)"], path)
            self.assertEqual(rules(result), {(3, "unknown_file")}, path)
        # F3: the implementation line and the docstring are evidence of what a function does; the signature is not -- rule 3
        cases = (("exports-worker", "- echo returns", "src/worker.py:22", "src/worker.py:21"),
                 ("exports-cache", "- get returns", "src/cache.py:14", "src/cache.py:13"))
        for chore_id, start, at, signature_at in cases:
            answer = self.answers[chore_id]
            index = next(i for i, s in enumerate(answer["reference"]) if s["text"].startswith(start))
            text = answer["reference"][index]["text"]
            self.assertTrue(text.endswith("[" + at + "]"), text)
            self.assertTrue(summarize_check.check(reference(answer), answer, FIXTURES)["pass"], text)
            moved = cited(text, signature_at)
            result = summarize_check.check(reply_with(answer, {index: moved}), answer, FIXTURES)
            self.assertFalse(result["pass"], moved)
            self.assertEqual(len(result["unsupported"]), 1, moved)
            self.assertEqual(rules(result), {(3, "unsupported")}, moved)
        # the reversed one-liner: no fact line of its own for any fact but one -- rule 1 (and not a fact line without its "- ")
        one = "dead, retrying, succeeded, running, pending"
        for text, expected in (("- {} [docs/lifecycle.md:5-9]".format(one), (1, "shared_line")), ("{} (docs/lifecycle.md:5-9).".format(one), (4, "missing"))):
            for words in (one, ", ".join(one.split(", ")[::-1])):
                result = summarize_check.check(text.replace(one, words), states, FIXTURES)
                self.assertFalse(result["pass"], text)
                self.assertIn(expected, rules(result), text)
                self.assertGreaterEqual(len(result["missing"]), 4, text)

    def test_citing_each_support_line_passes_and_every_line_outside_the_set_fails(self):
        # For every fact and every line that states it: the same fact line citing each line of its support_lines (alone) passes;
        # citing any line outside the set, in the file the line cites, with the same width, fails as `unsupported` (rule 3) and as
        # nothing else. Qwen's known failure is a plausible, in-range, wrong line number (12 of 15 in Main's smoke).
        passed = shifted = 0
        for chore_id, answer, k in self.facts():
            labels = labels_of(answer)
            sets = summarize_check.support_sets(answer["support_lines"][k])
            for i in stating(answer, k):
                text = answer["reference"][i]["text"]
                name, a = CITE.search(text).group(1).split(",")[0].rsplit(":", 1)
                a, _, b = a.partition("-")
                width = int(b or a) - int(a) + 1
                cases = [("{}:{}".format(n_name, n), True) for n_name, lines in sorted(sets.items()) for n in sorted(lines)]
                size = len(fixture_lines(name))
                for first in range(1, size - width + 2):
                    if not set(range(first, first + width)) & sets.get(name, set()):
                        cases.append(("{}:{}".format(name, first) if width == 1 else "{}:{}-{}".format(name, first, first + width - 1), False))
                for citation, ok in cases:
                    result = summarize_check.check(reply_with(answer, {i: cited(text, citation)}), answer, FIXTURES)
                    where = "{} fact {} cites {}: {}".format(chore_id, k, citation, result)
                    self.assertEqual(result["pass"], ok, where)
                    if ok:
                        passed += 1
                    else:
                        shifted += 1
                        self.assertEqual(result["unsupported"], [labels[k]], where)
                        self.assertEqual(rules(result), {(3, "unsupported")}, where)
                        self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"], result["forbidden_hit"]),
                                         ([], [], [], []), where)
        self.assertGreaterEqual(passed, 250)
        self.assertGreaterEqual(shifted, 144)  # round 4 tried 144 wrong windows; this tries every one

    def test_every_line_shifted_passes_none(self):
        # Main's probe: the reference with EVERY citation moved off the evidence (to the nearest line, in the same file and of the same
        # width, that is outside the support of every fact the line states) passes in no chore, and every fact is `unsupported`.
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            lines = []
            for s in answer["reference"]:
                name, a = CITE.search(s["text"]).group(1).rsplit(":", 1)
                first, _, last = a.partition("-")
                width = int(last or first) - int(first) + 1
                evidence = set()
                for entry in answer["support_lines"]:  # outside the evidence of every fact, so no other fact is met by the line
                    evidence |= summarize_check.support_sets(entry).get(name, set())
                size = len(fixture_lines(name))
                moved = next((n for d in range(1, size) for n in (int(first) - d, int(first) + d)
                              if 1 <= n and n + width - 1 <= size and not set(range(n, n + width)) & evidence), None)
                self.assertIsNotNone(moved, (chore["id"], s["text"]))
                lines.append(cited(s["text"], "{}:{}".format(name, moved) + ("-{}".format(moved + width - 1) if width > 1 else "")))
            result = self.check("\n".join(lines), chore["id"])
            self.assertFalse(result["pass"], chore["id"])
            self.assertEqual(result["unsupported"], labels_of(answer), chore["id"])
            self.assertEqual(rules(result), {(3, "unsupported")}, chore["id"])

    def test_the_nearest_line_outside_the_set_fails(self):
        # The line just before and just after the set of each fact (when it exists), cited alone: the boundary of the set is exact.
        count = 0
        for chore_id, answer, k in self.facts():
            labels = labels_of(answer)
            for name, lines in summarize_check.support_sets(answer["support_lines"][k]).items():
                size = len(fixture_lines(name))
                for n in {min(lines) - 1, max(lines) + 1}:
                    if not 1 <= n <= size or n in lines:
                        continue
                    count += 1
                    for i in stating(answer, k):
                        text = cited(answer["reference"][i]["text"], "{}:{}".format(name, n))
                        result = summarize_check.check(reply_with(answer, {i: text}), answer, FIXTURES)
                        self.assertFalse(result["pass"], "{} fact {} cites {}:{}".format(chore_id, k, name, n))
                        self.assertEqual(result["unsupported"], [labels[k]], "{} fact {} cites {}:{}".format(chore_id, k, name, n))
        self.assertGreater(count, 100)

    def test_a_fact_line_citing_a_file_that_is_not_the_chores_fails_rule_3(self):
        # Main's probe, for every fact line of every chore: the same line with its file changed to a fixture file the chore did not
        # name, or to one that does not exist, fails as unknown_file and nothing earlier in the reply supplies another.
        every = sorted(str(p.relative_to(FIXTURES)) for p in FIXTURES.rglob("*") if p.is_file())
        count = 0
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            others = [f for f in every if f not in answer["cite_files"]] + ["src/nonexistent.rs", "archive.7z"]
            for i, s in enumerate(answer["reference"]):
                for other in others:
                    line = cited(s["text"], "{}:{}".format(other, CITE.search(s["text"]).group(1).rsplit(":", 1)[1]))
                    result = summarize_check.check(reply_with(answer, {i: line}), answer, FIXTURES)
                    where = "{}: {}".format(chore["id"], line)
                    self.assertFalse(result["pass"], where)
                    self.assertEqual(result["bad_citations"], [other + ":" + line.rsplit(":", 1)[1][:-1] + " (unknown_file)"], where)
                    self.assertIn((3, "unknown_file"), rules(result), where)
                    count += 1
        self.assertGreater(count, 300)

    def test_lines_that_are_not_fact_lines_count_for_nothing(self):
        # Rule 2, for every chore: headings, bold lines, prose, blank lines, indented bullets and numbered or starred items, with
        # citations and file names in them, are ignored: adding them changes nothing (the reference still passes, nothing is
        # unchecked, nothing is bad); and a reply made only of them states nothing.
        noise = ["# Summary", "", "### src/cache.py", "**src/jobq.py**", "Line 6 -- this is not a fact line (src/cache.py:6).",
                 "  - indented bullet [src/cache.py:6]", "* starred bullet [src/cache.py:6]", "1. numbered [src/cache.py:6]",
                 "-no space after the dash [src/cache.py:6]", "> quote [src/cache.py:6]", "The files hold it: src/cache.py:6, Lines 1-99.",
                 "- Line 6", "### archive.7z", "Not in the files."]
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            ignorable = [n for n in noise if not n.startswith("- ")]
            lines = [s["text"] for s in answer["reference"]]
            spread = []
            for n, line in enumerate(lines):
                spread += [ignorable[n % len(ignorable)], line]
            result = self.check("\n".join(ignorable + spread + ignorable), chore["id"])
            self.assertTrue(result["pass"], "{}: {}".format(chore["id"], result))
            self.assertEqual((result["unchecked"], result["failures"], result["bad_citations"], result["uncited"]), (0, [], [], []), chore["id"])
            only = self.check("\n".join(ignorable), chore["id"])
            self.assertEqual((only["bad_citations"], only["uncited"], only["unchecked"]), ([], [], 0), chore["id"])
            self.assertEqual(len(only["missing"]), len(answer["required"]), chore["id"])
            # the reference as a list of anything but "- " lines states nothing
            for mark in ("* ", "1. ", "  - ", "", "-"):
                other = "\n".join(mark + t[2:] if mark != "-" else "-" + t[2:] for t in lines)
                self.assertEqual(len(self.check(other, chore["id"])["missing"]), len(answer["required"]), (chore["id"], mark))

    def test_a_reply_with_only_the_first_required_fact_fails(self):
        # Each item of a list chore (steps, states, exports, usages) is its own fact, so one item cannot satisfy the chore.
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            only = "\n".join(s["text"] for s in answer["reference"] if 0 in s["facts"])
            result = summarize_check.check(only, answer, FIXTURES)
            self.assertNotIn(answer["required"][0][0], result["missing"] + result["missing_citation"], chore["id"])
            self.assertFalse(result["pass"], "{}: {}".format(chore["id"], result))

    def test_order_is_checked_where_the_chore_asks_for_it(self):
        ordered = {c["id"] for c in self.chores if "order" in self.answers[c["id"]]}
        self.assertEqual(ordered, {"steps-states", "steps-deploy"})  # the chores whose task text asks for an order
        for chore_id in sorted(ordered):
            answer = self.answers[chore_id]
            labels = [fact[0] for fact in answer["required"]]
            self.assertEqual(answer["order"], list(range(len(labels))), chore_id)  # one fact per item, all in the order
            lines = [s["text"] for s in answer["reference"]]
            for name, shuffled, expected in (("reversed", lines[::-1], labels[1:]),
                                             ("first two swapped", lines[1::-1] + lines[2:], [labels[1]])):
                result = summarize_check.check("\n".join(shuffled), answer, FIXTURES)
                where = "{} {}: {}".format(chore_id, name, result)
                self.assertEqual(result["order"], expected, where)
                self.assertFalse(result["pass"], where)
                self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"]), ([], [], []), where)
                self.assertEqual({r for r, _ in rules(result)}, {None}, where)  # the order is no rule of the fact-line format

    def test_the_audit_table_covers_every_chore_and_fact(self):
        # tests/calibration/experiments.md: one row per chore, naming each of its required facts as F1..Fn and its support lines.
        text = (ROOT / "tests" / "calibration" / "experiments.md").read_text(encoding="utf-8")
        for chore in self.chores:
            rows = [line for line in text.splitlines() if line.startswith("| {} |".format(chore["id"]))]
            self.assertEqual(len(rows), 1, chore["id"])
            answer = self.answers[chore["id"]]
            n = len(answer["required"])
            self.assertEqual(sorted(set(re.findall(r"\bF(\d+)\b", rows[0])), key=int), [str(i) for i in range(1, n + 1)], chore["id"])
            for k, entry in enumerate(answer["support_lines"]):
                for name, specs in entry.items():
                    self.assertIn("{}:{}".format(Path(name).name, ",".join(specs)), rows[0], chore["id"])
            self.assertEqual(len(rows[0].split(" | ")), 5, chore["id"])
        # no row of a table has more or fewer cells than the row above it
        for table in re.findall(r"(?:^\|.*\n)+", text, re.M):
            widths = [len(re.findall(r"(?<!\\)\|", row)) for row in table.splitlines()]
            self.assertEqual(len(set(widths)), 1, table.splitlines()[0])


class CheckerTests(unittest.TestCase):
    # Any line of cache.py supports both facts here; test_support_ties_a_citation_to_the_fact narrows it.
    ANSWER = {"required": [["alpha", "first"], ["beta"]], "forbidden": [r"\bgamma\b"], "cite_files": ["src/cache.py"],
              "support_lines": [{"src/cache.py": ["1-36"]}, {"src/cache.py": ["1-36"]}]}
    TWO = dict(ANSWER, cite_files=["src/cache.py", "src/jobq.py"],
               support_lines=[{"src/cache.py": ["1-36"], "src/jobq.py": ["1-49"]}] * 2)
    ONE = dict(ANSWER, required=[["alpha", "first"]], support_lines=[{"src/cache.py": ["1-36"]}])  # one fact: "alpha" on a line
    C = "[src/cache.py:6]"  # `class LRUCache:`

    def check(self, reply, answer=None):
        return summarize_check.check(reply, answer or self.ANSWER, FIXTURES)

    def line(self, needle):
        lines = (FIXTURES / "src/cache.py").read_text().splitlines()
        return next(i for i, text in enumerate(lines, 1) if needle in text)

    def test_pass(self):
        self.assertEqual(self.check("# Answer\n- The first thing {c}\n\n- beta {c}\nThat is all.".format(c=self.C)),
                         {"pass": True, "missing": [], "missing_citation": [], "unsupported": [], "order": [], "forbidden_hit": [],
                          "bad_citations": [], "unchecked": 0, "uncited": [], "identifier_only": [], "failures": []})

    def test_missing_fact(self):
        result = self.check("- Only beta {}".format(self.C))
        self.assertFalse(result["pass"])
        self.assertEqual((result["missing"], result["missing_citation"]), (["alpha"], []))
        self.assertEqual(result["failures"], [{"rule": 4, "kind": "missing", "line": None, "text": "alpha"}])

    def test_forbidden_hit(self):
        result = self.check("- alpha {c}\n- beta, and gamma too {c}".format(c=self.C))
        self.assertFalse(result["pass"])
        self.assertEqual(result["forbidden_hit"], [r"\bgamma\b"])
        self.assertEqual((result["missing"], result["missing_citation"]), ([], []))
        # a fabrication is looked for in the reply as written, an ignored line included
        result = self.check("- alpha {c}\n- beta {c}\ngamma, said a heading".format(c=self.C))
        self.assertEqual(result["forbidden_hit"], [r"\bgamma\b"])

    def test_citations(self):
        n = self.line("class LRUCache:")
        self.assertEqual(n, 6)
        good = "- alpha "
        cases = [
            ("path:line", good + "[src/cache.py:{}]".format(n), None),
            ("a range", good + "[src/cache.py:{}-{}]".format(n - 1, n + 1), None),
            ("two citations in the list", good + "[src/cache.py:{}, src/cache.py:{}]".format(n, n + 1), None),
            ("a space after the comma is optional", good + "[src/cache.py:{},src/cache.py:{}]".format(n, n + 1), None),
            ("trailing spaces", good + "[src/cache.py:{}]   ".format(n), None),
            ("a bracket in the claim that is no citation list", "- alpha [note] and more [src/cache.py:{}]".format(n), None),
            ("a range of 5 lines", good + "[src/cache.py:{}-{}]".format(n - 4, n), None),
            ("a range of 6 lines", good + "[src/cache.py:{}-{}]".format(n - 5, n), "range_too_wide"),
            ("a whole file", good + "[src/cache.py:1-36]", "range_too_wide"),
            ("line zero", good + "[src/cache.py:0]", "range"),
            ("line past the end", good + "[src/cache.py:9999]", "range"),
            ("range past the end", good + "[src/cache.py:{}-9999]".format(n), "range"),
            ("a reversed range", good + "[src/cache.py:9-6]", "range"),
            ("an absolute path to a cite file", good + "[/tmp/x/src/cache.py:{}]".format(n), "unknown_file"),
            ("the bare name of a cite file", good + "[cache.py:{}]".format(n), "unknown_file"),
            ("a dot-slash path", good + "[./src/cache.py:{}]".format(n), "unknown_file"),
            ("file not in cite_files", good + "[src/jobq.py:3]", "unknown_file"),
            ("file does not exist", good + "[src/nothing.py:3]", "unknown_file"),
            ("unlisted extension, no such file", good + "[src/nonexistent.rs:999]", "unknown_file"),
            ("known file type outside cite_files", good + "[docs/deploy.md:3]", "unknown_file"),
            ("no extension", good + "[Makefile:3]", "unknown_file"),
            ("a backup of the file", good + "[src/cache.py.bak:3]", "unknown_file"),
            ("a longer name that ends the same", good + "[my_src/cache.py:3]", "unknown_file"),
            # not a citation list: the line is uncited (rule 2), whatever else it holds
            ("an en dash range", good + "[src/cache.py:{}–{}]".format(n - 1, n + 1), "uncited"),
            ("a quote after the list", good + "[src/cache.py:{}] `class LRUCache:`".format(n), "uncited"),
            ("parentheses", good + "(src/cache.py:{})".format(n), "uncited"),
            ("no file", good + "[line {}]".format(n), "uncited"),
            ("an empty list", good + "[]", "uncited"),
            ("no colon", good + "[src/cache.py {}]".format(n), "uncited"),
            ("one good citation and one that is not", good + "[src/cache.py:{}, line 7]".format(n), "uncited"),
            ("an unclosed list", good + "[src/cache.py:{}".format(n), "uncited"),
        ]
        rule = {"uncited": 2, None: None}
        for name, reply, kind in cases:
            result = self.check(reply, self.ONE)
            self.assertEqual(result["pass"], kind is None, "{}: {}".format(name, result))
            self.assertEqual(bool(result["bad_citations"]), kind not in (None, "uncited"), "{}: {}".format(name, result))
            self.assertEqual(bool(result["uncited"]), kind == "uncited", "{}: {}".format(name, result))
            if kind:
                self.assertIn((rule.get(kind, 3), kind), rules(result), "{}: {}".format(name, result))
        wide = self.check(good + "[src/cache.py:1-6]", self.ONE)
        self.assertEqual(wide["bad_citations"], ["src/cache.py:1-6 (range_too_wide)"])

    def test_rule_1_one_fact_per_line_with_one_citation_list(self):
        c = self.C
        two = self.check("- alpha {c} and beta {c}".format(c=c))
        self.assertFalse(two["pass"])
        self.assertEqual(rules(two), {(1, "two_citation_lists")} | {(4, "missing")})
        self.assertEqual(two["uncited"], ["- alpha {c} and beta {c}".format(c=c)])
        # one line meets one fact: the other fact it names is `missing` under rule 1
        both = self.check("- alpha and beta {}".format(c))
        self.assertEqual((both["pass"], both["missing"], rules(both)), (False, ["beta"], {(1, "shared_line")}))
        self.assertTrue(self.check("- alpha {c}\n- beta {c}".format(c=c))["pass"])
        # a line that fits both facts is not wasted when another line fits only one of them
        answer = dict(self.ANSWER, required=[["alpha"], ["beta"]])
        self.assertTrue(self.check("- alpha and beta {c}\n- alpha {c}".format(c=c), answer)["pass"])
        self.assertTrue(self.check("- alpha {c}\n- alpha and beta {c}".format(c=c), answer)["pass"])
        self.assertFalse(self.check("- alpha and beta {c}\n- again {c}".format(c=c), answer)["pass"])

    def test_rule_2_other_lines_are_ignored_and_an_uncited_bullet_fails(self):
        c = self.C
        ignored = [("a sentence", "alpha and beta {}".format(c)), ("a heading", "### alpha beta {}".format(c)),
                   ("a bold line", "**alpha beta** {}".format(c)), ("a starred bullet", "* alpha beta {}".format(c)),
                   ("a numbered item", "1. alpha beta {}".format(c)), ("an indented bullet", "  - alpha beta {}".format(c)),
                   ("no space", "-alpha beta {}".format(c)), ("a tab", "-\talpha beta {}".format(c)),
                   ("a quote", "> alpha beta {}".format(c))]
        for name, reply in ignored:
            result = self.check(reply)
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"]),
                             (False, ["alpha", "beta"], [], []), name)
            self.assertEqual((rules(result), result["unchecked"]), ({(4, "missing")}, 0), name)  # no claim, no citation, nothing to check
        # the neighbours supply nothing, in either direction
        cases = [("heading above", "### src/cache.py\n- alpha beta", ["- alpha beta"]),
                 ("the line above", "- alpha {}\n- beta".format(c), ["- beta"]),
                 ("the line below", "- alpha\n- beta {}".format(c), ["- alpha"]),
                 ("a wrapped bullet", "- alpha\n  beta {}".format(c), ["- alpha"]),
                 ("a lazy continuation", "- alpha\nbeta {}".format(c), ["- alpha"]),
                 ("a citation in running text", "- alpha beta\nsee src/cache.py:6", ["- alpha beta"]),
                 ("a path with no line", "- alpha beta [src/cache.py]", ["- alpha beta [src/cache.py]"]),
                 ("a Line N", "- alpha beta (Line 6)", ["- alpha beta (Line 6)"]),
                 ("brackets with no path", "- alpha beta [6]", ["- alpha beta [6]"])]
        for name, reply, uncited in cases:
            result = self.check(reply)
            self.assertEqual(result["uncited"], uncited, name)
            self.assertFalse(result["pass"], name)
            self.assertIn((2, "uncited"), rules(result), name)
        # one uncited bullet fails a reply whose facts are all stated and cited: no partial credit
        reply = "- alpha {c}\n- beta {c}".format(c=c)
        self.assertTrue(self.check(reply)["pass"])
        result = self.check(reply + "\n- an aside with no citation")
        self.assertEqual((result["pass"], result["missing"], result["uncited"]), (False, [], ["- an aside with no citation"]))
        self.assertEqual(result["failures"], [{"rule": 2, "kind": "uncited", "line": 3, "text": "- an aside with no citation"}])

    def test_rule_3_a_citation_must_cover_a_support_line_of_its_own_fact_line(self):
        # support_lines: the citations of a fact line must overlap the set of lines that are evidence of the fact it states.
        put, get = self.line("def put(self, key, value):"), self.line("def get(self, key, default=None):")
        answer = dict(self.ANSWER, support_lines=[{"src/cache.py": ["6"]}, {"src/cache.py": [str(put)]}])
        both = "- alpha [src/cache.py:{c}]\n- beta [src/cache.py:{p}]"
        cases = [
            ("both right", both.format(c=6, p=put), [], 0),
            ("one number moved in range", both.format(c=6, p=get), ["beta"], 1),
            ("both moved in range", both.format(c=7, p=get), ["alpha", "beta"], 2),
            ("a range that holds the right line", "- alpha [src/cache.py:6]\n- beta [src/cache.py:{}-{}]".format(put - 2, put + 2), [], 0),
            ("a range that stops short of it", "- alpha [src/cache.py:6]\n- beta [src/cache.py:{}-{}]".format(put - 4, put - 1), ["beta"], 1),
            ("one of two citations on the line is enough", "- alpha [src/cache.py:6, src/cache.py:{p}]\n- beta [src/cache.py:6, src/cache.py:{p}]".format(p=put), [], 2),
            ("the wrong file does not support", "- alpha [src/jobq.py:3]\n- beta [src/jobq.py:3]", ["alpha", "beta"], 2),
        ]
        for name, reply, unsupported, unchecked in cases:
            result = self.check(reply, dict(answer, cite_files=["src/cache.py", "src/jobq.py"]) if "jobq" in reply else answer)
            self.assertEqual(result["unsupported"], unsupported, "{}: {}".format(name, result))
            self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"]), ([], [], []), name)
            self.assertEqual(result["unchecked"], unchecked, "{}: {}".format(name, result))
            self.assertEqual(result["pass"], not unsupported, name)
            self.assertEqual(rules(result), {(3, "unsupported")} if unsupported else set(), name)
        # the support of a fact is for that fact only: alpha's line does not vouch for beta
        swapped = self.check("- alpha [src/cache.py:{}]\n- beta [src/cache.py:6]".format(put), answer)
        self.assertEqual(swapped["unsupported"], ["alpha", "beta"])
        # unsupported is not missing_citation: a fact line with no good citation at all is that
        bad = self.check("- alpha [src/cache.py:9999]\n- beta [src/cache.py:{}]".format(put), answer)
        self.assertEqual((bad["missing_citation"], bad["unsupported"], bad["bad_citations"]), (["alpha"], [], ["src/cache.py:9999 (range)"]))
        self.assertEqual(rules(bad), {(3, "range"), (3, "missing_citation")})
        with self.assertRaises(ValueError):
            self.check("- alpha {}".format(self.C), dict(answer, support_lines=[{"src/cache.py": ["6"]}]))  # one entry for two facts
        for spec in ("x", "", "14-", "-3", "20-14", "1,2", "0x3"):
            with self.assertRaises(ValueError, msg=spec):
                self.check("- alpha {}".format(self.C), dict(answer, support_lines=[{"src/cache.py": [spec]}, {"src/cache.py": ["6"]}]))
        self.assertEqual(summarize_check.support_sets({"src/cache.py": ["6", "20-22", "21"], "a": []}),
                         {"src/cache.py": {6, 20, 21, 22}, "a": set()})

    def test_every_citation_is_checked_and_the_unvouched_are_counted(self):
        # A good citation counts through the support of a fact the line states; one on a line that states no fact, or that matches no
        # support there, is `unchecked`: counted, not a failure. A bad citation is bad, not unchecked.
        answer = dict(self.ONE, support_lines=[{"src/cache.py": ["6"]}])
        cases = [
            ("a fact line only", "- alpha [src/cache.py:6]", 0, True),
            ("a citation on a line that states no fact", "- alpha [src/cache.py:6]\n- see also [src/cache.py:20]", 1, True),
            ("a miss beside a vouching citation", "- alpha [src/cache.py:6, src/cache.py:20]", 1, True),
            ("two on a line that states no fact", "- alpha [src/cache.py:6]\n- aside [src/cache.py:20, src/cache.py:21]", 2, True),
            ("a bad citation is bad, not unchecked", "- alpha [src/cache.py:6]\n- aside [src/cache.py:999]", 0, False),
            ("a line that is not a fact line has no citation to count", "- alpha [src/cache.py:6]\nsee src/cache.py:20", 0, True),
        ]
        for name, reply, unchecked, ok in cases:
            result = self.check(reply, answer)
            self.assertEqual((result["unchecked"], result["pass"]), (unchecked, ok), "{}: {}".format(name, result))

    def test_rule_4_a_fact_is_met_by_one_line_that_also_supports_it(self):
        put = self.line("def put(self, key, value):")
        answer = dict(self.ANSWER, support_lines=[{"src/cache.py": ["6"]}, {"src/cache.py": [str(put)]}])
        # the claim on one line and the support on another do not add up: each line must do both
        split = self.check("- alpha [src/cache.py:{p}]\n- beta [src/cache.py:6]".format(p=put), answer)
        self.assertEqual((split["pass"], split["unsupported"]), (False, ["alpha", "beta"]))
        # a heading or an earlier line that states the claim does not carry it to a line that only cites
        head = self.check("### alpha\n- beta [src/cache.py:{}]\n- [src/cache.py:6]".format(put), answer)
        self.assertEqual((head["pass"], head["missing"]), (False, ["alpha"]))
        # the right claim on a line with a wrong citation is not rescued by a second line with the right citation but another claim
        rescue = self.check("- alpha [src/cache.py:7]\n- beta and gamma [src/cache.py:{}]\n- delta [src/cache.py:6]".format(put), answer)
        self.assertEqual((rescue["pass"], rescue["unsupported"]), (False, ["alpha"]))
        # both on their own lines, and in either order, pass
        self.assertTrue(self.check("- alpha [src/cache.py:6]\n- beta [src/cache.py:{}]".format(put), answer)["pass"])
        self.assertTrue(self.check("- beta [src/cache.py:{}]\n- alpha [src/cache.py:6]".format(put), answer)["pass"])
        # a claim cannot cross lines
        cross = self.check("- alp\n- ha beta [src/cache.py:6]", answer)
        self.assertEqual(cross["missing"], ["alpha"])

    def test_rule_5_a_name_and_a_signature_are_not_a_behaviour(self):
        answer = dict(self.ANSWER, required=[[r"\bholds?\b"], [r"\bbeta\b"]], behaviour={"0": {"name": "LRUCache", "sig": "src/cache.py:6"}})
        cases = [
            ("the words say it", "- LRUCache holds items [src/cache.py:6]", True),
            ("the name alone", "- LRUCache [src/cache.py:6]", False),
            ("the signature in backticks", "- `class LRUCache:` [src/cache.py:6]", False),
            ("the signature as written", "- class LRUCache: [src/cache.py:6]", False),
            ("the name and the signature", "- LRUCache `class LRUCache:` [src/cache.py:6]", False),
            ("the file and the name", "- src/cache.py LRUCache [src/cache.py:6]", False),
            ("a path:line in the claim", "- LRUCache src/cache.py:6 [src/cache.py:6]", False),
            ("the verb in backticks", "- LRUCache `holds` [src/cache.py:6]", False),
            ("a name that is a verb in the code", "- LRUCache(self, capacity, ttl=None) [src/cache.py:6]", False),
        ]
        for name, fact_line, ok in cases:
            result = self.check(fact_line + "\n- beta {}".format(self.C), answer)
            self.assertEqual(result["pass"], ok, "{}: {}".format(name, result))
            self.assertEqual(result["missing"], [] if ok else [r"\bholds?\b"], name)
            self.assertEqual(result["identifier_only"], [] if ok else [fact_line], name)
            self.assertEqual(rules(result), set() if ok else {(4, "missing"), (5, "identifier_only")}, "{}: {}".format(name, result))
        # a fact that is not a behaviour keeps its regex on the whole claim: a name can be what it says
        plain = dict(answer, required=[[r"\bLRUCache\b"], [r"\bbeta\b"]], behaviour={})
        self.assertTrue(self.check("- LRUCache [src/cache.py:6]\n- beta {}".format(self.C), plain)["pass"])
        # a line of names that a fact needs is not flagged: it is that fact (and one with words is preferred to it)
        both = self.check("- LRUCache [src/cache.py:6]\n- LRUCache [src/cache.py:6]\n- beta {}".format(self.C), plain)
        self.assertEqual((both["pass"], both["identifier_only"]), (False, ["- LRUCache [src/cache.py:6]"]))
        # a fact line of words that do not say the fact is rule 4 only
        words = self.check("- LRUCache is a thing [src/cache.py:6]\n- beta {}".format(self.C), answer)
        self.assertEqual(rules(words), {(4, "missing")})

    def test_the_symbol_table_comes_from_the_fixture(self):
        # Rule 5's names are computed with ast from the files the chore names: change a file and the names change with it.
        with tempfile.TemporaryDirectory() as temp:
            src = Path(temp) / "src"
            src.mkdir()
            (src / "a.py").write_text("import time\nfrom os import path as p\n\ndef fetch(url, retries=3):\n    return time.monotonic()\n"
                                      "\nclass Box:\n    LIMIT = 3\n    def put(self, item, *, slot):\n        self.d['key_one'] = item\n")
            names = summarize_check.names_in_code(temp, ["src/a.py"])
            for name in ("time", "monotonic", "p", "fetch", "url", "retries", "Box", "LIMIT", "put", "self", "item", "slot", "d", "key_one"):
                self.assertIn(name, names)
            for word in ("returns", "stores", "def", "return", "the", "fetches"):
                self.assertNotIn(word, names)
            self.assertEqual(summarize_check.without_names("fetch(url, retries=3) returns a time", names).split(), ["(", ",", "=3)", "returns", "a"])
            (src / "a.py").write_text("def fetched(address):\n    pass\n")
            os.utime(src / "a.py", (1, 2 ** 31 - 1))  # a new mtime, so the cache cannot hide the edit
            again = summarize_check.names_in_code(temp, ["src/a.py"])
            self.assertTrue({"fetched", "address"} <= again and not {"fetch", "url", "Box"} & again, again)
            (src / "a.py").write_text("def broken(:\n")  # code that does not parse: no names, no crash (the text is not in the table)
            os.utime(src / "a.py", (1, 2 ** 31 - 2))
            self.assertEqual(summarize_check.names_in_code(temp, ["src/a.py"]), frozenset())
            self.assertEqual(summarize_check.names_in_code(temp, ["src/missing.py", "docs/x.md"]), frozenset())
        # every identifier on the signature line of every behaviour fact is a name, a keyword or a file name
        for answer in jsonl("answers.jsonl"):
            names = summarize_check.names_in_code(FIXTURES, answer["cite_files"])
            for k, behaviour in answer.get("behaviour", {}).items():
                sig_file, sig_line = behaviour["sig"].rsplit(":", 1)
                if not sig_file.endswith(".py"):
                    continue
                left = summarize_check.without_files(summarize_check.without_names(fixture_lines(sig_file)[int(sig_line) - 1], names), answer["cite_files"])
                self.assertFalse(re.search(r"[A-Za-z]{2,}", left), "{} fact {}: {!r}".format(answer["id"], k, left))

    def test_mains_wrong_line_reproducer(self):
        # Main (round 4): use-jobqueue's own reference with each number changed to a wrong one in range passed with no bad citation,
        # because an unquoted citation was only checked to be in range.
        answer = answer_of("use-jobqueue")
        right = reference(answer)
        self.assertTrue(summarize_check.check(right, answer, FIXTURES)["pass"])
        wrong = right
        for old, new in (("cli.py:29", "cli.py:5"), ("cli.py:31", "cli.py:7"), ("jobq.py:23", "jobq.py:3"), ("worker.py:30", "worker.py:2"),
                         ("worker.py:32", "worker.py:4"), ("worker.py:44", "worker.py:6")):
            self.assertIn(old, wrong)
            wrong = wrong.replace(old, new)
        result = summarize_check.check(wrong, answer, FIXTURES)
        self.assertFalse(result["pass"])
        self.assertEqual(result["unsupported"], labels_of(answer))  # all six facts
        self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"]), ([], [], []))
        self.assertEqual(result["unchecked"], 6)
        self.assertEqual(rules(result), {(3, "unsupported")})

    def test_main_smoke_reply_is_scored_by_the_fact_line_rules(self):
        # Main's live smoke (use-jobqueue, 2026-11-08) passed the first checker with zero bad citations: it cited "Line N" under
        # "### src/<file>" headings. It is not in the fact-line format: its bullets are "- **Line 29** -- ..." with no trailing
        # citation list, so each is `uncited` (rule 2), the headings and the prose are ignored, and no fact is stated (rule 4).
        reply = (DATA / "samples" / "smoke-use-jobqueue.txt").read_text(encoding="utf-8")
        answer = answer_of("use-jobqueue")
        result = summarize_check.check(reply, answer, FIXTURES)
        bullets = [line for line in reply.splitlines() if line.startswith("- ")]
        self.assertFalse(result["pass"])
        self.assertEqual(result["missing"], labels_of(answer))
        self.assertEqual(result["uncited"], bullets)
        self.assertEqual(len(bullets), 9)
        self.assertEqual((result["bad_citations"], result["unsupported"], result["missing_citation"], result["unchecked"]), ([], [], [], 0))
        self.assertEqual(rules(result), {(2, "uncited"), (4, "missing")})
        # the same facts in the format, with the smoke reply's own wrong numbers, are caught by rule 3
        wrong = "\n".join(["- jobq.py defines the JobQueue class [src/jobq.py:19]", "- worker.py pops the next job [src/worker.py:31]",
                           "- cli.py creates a JobQueue [src/cli.py:29]", "- cli.py pushes a job onto the queue [src/cli.py:31]"])
        result = summarize_check.check(wrong, answer, FIXTURES)
        self.assertEqual(rules(result) - {(4, "missing")}, {(3, "unsupported")})
        self.assertIn(labels_of(answer)[2], result["unsupported"])  # jobq.py:19 is not the line that defines JobQueue

    def test_the_prompt_example_is_a_fact_line(self):
        # R6: the prompt in config/local.example.json tells the model this format, with one example, and the example parses.
        prompt = json.loads((ROOT / "config/local.example.json").read_text())["prompts"][MODEL]
        example = re.search(r"for example: (- [^\n]*?\[[^\[\]]+\])\.", prompt)
        self.assertTrue(example, prompt)
        claim, cites, kind = summarize_check.parse_fact_line(example.group(1))
        self.assertEqual((kind, len(cites)), (None, 1), example.group(1))
        self.assertIn("- <claim> [path:line]", prompt)
        self.assertIn("at most 5 lines", prompt)
        self.assertEqual(summarize_check.MAX_RANGE, 5)


class RunnerTests(unittest.TestCase):
    CHORE = "use-lrucache"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.work = self.dir / "work"  # where the runner makes its temp dirs
        self.work.mkdir()
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "worker.log"
        self.reply = self.dir / "reply.txt"
        patch = mock.patch.object(tempfile, "tempdir", str(self.work))
        patch.start()
        self.addCleanup(patch.stop)
        for name in ("tmux", "git"):  # a call to either from the runner (or its children) lands in tools.log
            self.script(name, 'echo "$0 $*" >> {}\n'.format(self.dir / "tools.log"))
        self.script("pi", 'pwd >> "$QUAL_LOG"; env | grep -E "^(TMUX|TP_|CLAUDE_CODE_SESSION)" >> "$QUAL_LOG"; '
                          'echo "ARGS $*" >> "$QUAL_LOG"; ls src >> "$QUAL_LOG"; cat "$QUAL_REPLY"\n')
        self.route_files = {}
        for name, (out, err, code) in {"route": (ROUTE_OK, "", 0), "route_shape_stop": ("", ROUTE_SHAPE_STOP, 2),
                                       "route_refused": ("", ROUTE_REFUSED, 1), "route_garbage": ("not json", "", 0)}.items():
            (self.dir / (name + ".out")).write_text(out)
            (self.dir / (name + ".err")).write_text(err)
            self.script(name, 'echo "ARGS $*" >> "$QUAL_LOG"; cat {0}/{1}.out; cat {0}/{1}.err >&2; exit {2}\n'.format(self.dir, name, code))
        self.script("slow", "exec sleep 30\n")
        self.env = {"PATH": "{}:{}".format(self.bin, os.environ["PATH"]), "QUAL_LOG": str(self.log), "QUAL_REPLY": str(self.reply),
                    "TMUX": "/tmp/tmux-1/default,1,0", "TMUX_PANE": "%3", "TP_TASK_ID": "t1", "TP_MODE": "worker",
                    "TP_LOCAL_CONFIG": "/nonexistent", "CLAUDE_CODE_SESSION_ID": "abc"}
        self.prompt = self.dir / "prompt.txt"
        self.prompt.write_text("DRAFT RULE\n")

    def script(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def example(self):
        return reference(next(a for a in jsonl("answers.jsonl") if a["id"] == self.CHORE))

    def run_main(self, *extra, command="pi"):
        out = self.dir / "out.jsonl"
        argv = ["--model", MODEL, "--split", "tune", "--only", self.CHORE, "--out", str(out), "--prompt-file", str(self.prompt),
                "--pi-command", str(self.bin / command), "--route-command", str(self.bin / "route"), *extra]
        with mock.patch("builtins.print"):
            rc = summarize_run.main(argv, env=self.env)
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        return rc, lines[:-1], lines[-1]

    def assert_clean(self):
        self.assertEqual(list(self.work.iterdir()), [], "temp dir left behind")
        self.assertFalse((self.dir / "tools.log").exists(), "tmux or git was called")

    def test_records_summary_and_a_clean_child_env(self):
        self.reply.write_text(self.example())
        rc, records, summary = self.run_main("--runs", "2")
        self.assertEqual(rc, 0)
        self.assertEqual([(r["chore"], r["run"], r["pass"], r["error"]) for r in records],
                         [(self.CHORE, 1, True, None), (self.CHORE, 2, True, None)])
        self.assertEqual((summary["total"], summary["passed"], summary["pass_rate"], summary["fabricated_citations"]), (2, 2, 1.0, 0))
        self.assertEqual(summary["unchecked_citations"], 0)
        self.assertIn("median_seconds", summary)
        seen = self.log.read_text()
        self.assertEqual(re.findall(r"^(?:TMUX\w*|TP_\w+|CLAUDE_CODE_SESSION_ID)=", seen, re.M), ["TP_LOCAL_CONFIG="] * 2)
        self.assertIn("--append-system-prompt DRAFT RULE", seen)
        self.assertIn("--model " + MODEL, seen)
        self.assertEqual(sorted(set(re.findall(r"^\w+\.py$", seen, re.M))), ["cache.py", "cli.py", "jobq.py", "worker.py"])
        self.assertEqual(len(re.findall(r"^" + re.escape(os.path.realpath(self.work)), seen, re.M)), 2)  # cwd was a fresh temp dir each run
        self.assert_clean()

    def test_failing_reply_is_scored_and_classed(self):
        # the record keeps the raw reply and names, per failed line, the rule it broke; an uncited "- " line is its own class
        raw = "## Notes\n- worker.py uses LRUCache [src/worker.py:999]\n- an aside with no citation\n"
        self.reply.write_text(raw)
        _, records, summary = self.run_main()
        self.assertFalse(records[0]["pass"])
        self.assertEqual(records[0]["reply"].strip(), raw.strip())
        self.assertEqual(records[0]["bad_citations"], ["src/worker.py:999 (range)"])
        self.assertTrue(records[0]["forbidden_hit"] and records[0]["missing"])
        self.assertEqual({(f["rule"], f["kind"]) for f in records[0]["failures"]}, {(3, "range"), (2, "uncited"), (4, "missing")})
        self.assertEqual((summary["passed"], summary["fabricated_citations"]), (0, 1))
        self.assertEqual(set(summary["failure_classes"]), {"missing", "forbidden_hit", "bad_citations", "uncited"})
        # a wrong line in range: scored `unsupported`, and the good citation nothing vouches for is counted
        self.reply.write_text(self.example().replace("src/cache.py:6", "src/cache.py:1") + "\n- see worker [src/worker.py:30]")
        _, records, summary = self.run_main()
        self.assertEqual((records[0]["pass"], len(records[0]["unsupported"]), records[0]["unchecked"]), (False, 1, 2))  # cache.py:1 and worker.py:30
        self.assertEqual(summary["failure_classes"], {"unsupported": 1})
        self.assertEqual((summary["fabricated_citations"], summary["unchecked_citations"]), (0, 2))
        self.assert_clean()

    def test_timeout_kills_the_worker_and_removes_the_temp_dir(self):
        _, records, summary = self.run_main("--timeout", "0.5", command="slow")
        self.assertEqual((records[0]["error"], records[0]["pass"], records[0]["rc"]), ("timeout", False, None))
        self.assertEqual(summary["failure_classes"]["timeout"], 1)
        self.assert_clean()

    def test_route_only_records_what_tp_route_gives_through_the_cli(self):
        out = self.dir / "route.jsonl"
        proc = subprocess.run([sys.executable, str(ROOT / "tools/qual/summarize_run.py"), "--route-only", "--only", self.CHORE,
                               "--out", str(out), "--route-command", str(self.bin / "route")], env=self.env, text=True,
                              capture_output=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        record, summary = [json.loads(x) for x in out.read_text().splitlines()]
        real = json.loads(ROUTE_OK)
        self.assertEqual({k: record[k] for k in ("chore", "rc", "outcome", "level", "model", "source", "reason", "stderr")},
                         {"chore": self.CHORE, "rc": 0, "outcome": "routed", "level": "local", "model": real["model"],
                          "source": "router_resolved", "reason": real["reason"], "stderr": None})
        self.assertNotIn("shape", record)
        self.assertEqual((summary["outcomes"], summary["levels"], summary["total"]), ({"routed": 1}, {"local": 1}, 1))
        self.assertIn("--no-log --local-only", self.log.read_text())
        self.assert_clean()

    def test_route_only_classifies_stops_refusals_and_bad_output(self):
        cases = [("route_shape_stop", 2, "shape_not_qualified", ROUTE_SHAPE_STOP.strip()),
                 ("route_refused", 1, "error", ROUTE_REFUSED.strip()),
                 ("route_garbage", 0, "bad_output", None),
                 ("slow", None, "timeout", None)]
        for command, rc, outcome, stderr in cases:
            out = self.dir / (command + ".jsonl")
            argv = ["--route-only", "--only", self.CHORE, "--out", str(out), "--route-command", str(self.bin / command), "--timeout", "0.5"]
            with mock.patch("builtins.print"):
                summarize_run.main(argv, env=self.env)
            record, summary = [json.loads(x) for x in out.read_text().splitlines()]
            self.assertEqual((record["rc"], record["outcome"], record["stderr"], record["level"]), (rc, outcome, stderr, None), command)
            self.assertEqual((summary["outcomes"], summary["levels"]), ({outcome: 1}, {}), command)
        self.assert_clean()

    def test_classify_route(self):
        classify = summarize_run.classify_route
        self.assertEqual(classify(2, "", "tp-route: stop and ask the user: no available model at any level\n")["outcome"], "stop")
        self.assertEqual(classify(4, "", "tp-route: stop and ask the user: model catalog unreadable\n")["outcome"], "stop")
        self.assertEqual(classify(2, "", ROUTE_SHAPE_STOP)["outcome"], "shape_not_qualified")
        self.assertEqual(classify(0, ROUTE_OK, "")["outcome"], "routed")
        self.assertEqual(classify(0, "[]", "")["outcome"], "bad_output")

    def test_arguments(self):
        out = str(self.dir / "o.jsonl")
        for argv in (["--out", out], ["--model", MODEL, "--out", out, "--runs", "0"], ["--model", MODEL, "--out", out, "--split", "x"]):
            with self.assertRaises(SystemExit) as caught, mock.patch("sys.stderr"):
                summarize_run.main(argv, env=self.env)
            self.assertEqual(caught.exception.code, 2)
        with self.assertRaises(SystemExit) as caught:
            summarize_run.main(["--model", MODEL, "--out", out, "--only", "nope"], env=self.env)
        self.assertIn("nope", str(caught.exception.code))

    def test_system_prompt_comes_from_the_machine_config(self):
        local = self.dir / "local.json"
        local.write_text(json.dumps({"prompts": {MODEL: "FROM CONFIG"}}))
        with mock.patch.dict(os.environ, {"TP_LOCAL_CONFIG": str(local)}):
            self.assertEqual(summarize_run.system_prompt(MODEL), "FROM CONFIG")
            self.assertEqual(summarize_run.system_prompt(MODEL, str(self.prompt)), "DRAFT RULE")
        example = json.loads((ROOT / "config/local.example.json").read_text())["prompts"][MODEL]
        self.assertIn("grep -c", example)
        self.assertIn("path:line", example)
        self.assertIn("- <claim> [path:line]", example)


if __name__ == "__main__":
    unittest.main()
