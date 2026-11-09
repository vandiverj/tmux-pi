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


CITATION = re.compile(r"((?:src|docs)/[\w./]+):(\d+)(?:-(\d+))?")
QUOTED = re.compile(r" `[^`]*`")
_LINES = {}


def unquoted(text):
    return QUOTED.sub("", text)


def labels_of(answer):
    return [fact[0] for fact in answer["required"]]


def fixture_lines(name):
    if name not in _LINES:
        _LINES[name] = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return _LINES[name]


def stating(answer, k):
    """Indexes into answer["reference"] of the sentences that state fact k."""
    return [i for i, s in enumerate(answer["reference"]) if k in s["facts"]]


def reply_with(answer, changes):
    """The reference reply with the sentences in changes ({index: new text}) replaced; None drops one."""
    texts = [changes.get(i, s["text"]) for i, s in enumerate(answer["reference"])]
    return "\n".join(t for t in texts if t is not None)


def failed(result):
    return result["missing"] + result["missing_citation"] + result["unsupported"]


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
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            self.assertTrue(len(answer["required"]) >= 2 and answer["forbidden"], chore["id"])
            for fact in answer["required"]:
                for alternative in fact:
                    re.compile(alternative)
            for pattern in answer["forbidden"]:
                re.compile(pattern)
            for sentence in answer["reference"]:
                self.assertEqual(len(CITATION.findall(sentence["text"])), 1, sentence["text"])  # one citation per sentence
            result = summarize_check.check(reference(answer), answer, FIXTURES)
            self.assertTrue(result["pass"], "{}: {}".format(chore["id"], result))
            self.assertEqual(result["unchecked"], 0, chore["id"])  # every citation is a quoted one or vouched for by a support
            # the right numbers without the quotes pass as well: support, not the quote, is what ties a number to the fact
            bare = summarize_check.check(unquoted(reference(answer)), answer, FIXTURES)
            self.assertTrue(bare["pass"], "{}: {}".format(chore["id"], bare))
            self.assertEqual(bare["unchecked"], 0, chore["id"])
            empty = summarize_check.check("", answer, FIXTURES)
            self.assertFalse(empty["pass"], chore["id"])
            self.assertEqual(len(empty["missing"]), len(answer["required"]), chore["id"])

    def test_every_fact_is_needed_and_the_reference_is_complete(self):
        # The reference reply is hand-written, one cited sentence or bullet per fact (reference[].facts says which facts it states).
        # It passes; and with the sentences that state fact k removed the reply fails, with k among the failed facts: `missing`
        # when nothing else states it, `unsupported` when another sentence names it (an export's name, in the sentence on what it
        # does) but cites lines that are not the evidence of k. So each fact is needed and the reference leaves none out.
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
            self.assertEqual((result["order"], result["forbidden_hit"], result["bad_citations"]), ([], [], []), where)
            # nothing else fails but the facts only those sentences stated and the facts they were the evidence of
            self.assertEqual(sorted(set(failed(result)) - {labels[n] for n in gone} - {labels[k]}), [], where)

    def test_a_signature_alone_does_not_say_what_a_function_does(self):
        # Round 5, finding 1: `NAME (file:SIGNATURE_LINE `signature`)` in place of the sentence on what NAME does names it and quotes
        # its signature (the parameter names included): every behaviour fact must then be `missing`, however it is cited.
        count = 0
        for chore_id, answer, k in self.facts():
            behaviour = answer.get("behaviour", {}).get(str(k))
            if behaviour is None:
                continue
            count += 1
            name = behaviour["name"]
            sig_file, sig_line = behaviour["sig"].rsplit(":", 1)
            signature = fixture_lines(sig_file)[int(sig_line) - 1].strip()
            for form in ("{name} ({file}:{line} `{sig}`)", "{name} ({file}:{line})", "{name}: `{sig}` ({file}:{line})", "`{sig}` ({file}:{line})"):
                sentence = form.format(name=name, file=sig_file, line=sig_line, sig=signature)
                where = "{} fact {} {}".format(chore_id, k, sentence)
                changes = {i: sentence for i in stating(answer, k)}
                result = summarize_check.check(reply_with(answer, changes), answer, FIXTURES)
                self.assertFalse(result["pass"], where)
                self.assertIn(labels_of(answer)[k], result["missing"], where)
                self.assertEqual((result["bad_citations"], result["forbidden_hit"]), ([], []), where)
        self.assertGreaterEqual(count, 60)
        for chore in self.chores:
            if chore["id"].startswith("exports-") and chore["id"] != "exports-settings":
                self.assertTrue(self.answers[chore["id"]].get("behaviour"), chore["id"])  # an exports chore asks what each does

    def test_the_reviewers_reproducers(self):
        utils, cache, worker = self.answers["exports-utils"], self.answers["sum-cache"], self.answers["exports-worker"]
        # F1: chunked's sentence replaced by its name and signature; the rest of the complete answer is unchanged
        k = labels_of(utils).index(next(f[0] for f in utils["required"] if "chunked" in f[0] and f[0] != r"\bchunked\b"))
        sentence = "chunked (src/utils.py:16 `def chunked(items, size):`)."
        result = summarize_check.check(reply_with(utils, {i: sentence for i in stating(utils, k)}), utils, FIXTURES)
        self.assertFalse(result["pass"])
        self.assertEqual(result["missing"], [labels_of(utils)[k]])
        # F2: a nearer heading of an unknown file, whatever its extension, sets the context
        block = "\n### src/cache.py\n### nonexistent.xyz\n- LRUCache Line 6\n"
        self.assertTrue(summarize_check.check(reference(cache), cache, FIXTURES)["pass"])
        result = summarize_check.check(reference(cache) + block, cache, FIXTURES)
        self.assertFalse(result["pass"])
        self.assertEqual(result["bad_citations"], ["Line 6 (unknown_file)"])
        # F3: the implementation line and the docstring are evidence of what a function does; the signature is not
        cases = (("exports-worker", "echo returns", "src/worker.py:22 `return payload`", "src/worker.py:21"),
                 ("exports-cache", "get returns", "src/cache.py:14 `Return the cached value", "src/cache.py:13"))
        for chore_id, start, cited, signature in cases:
            answer = self.answers[chore_id]
            index = next(i for i, s in enumerate(answer["reference"]) if s["text"].startswith(start))
            text = answer["reference"][index]["text"]
            self.assertIn(cited, text)
            self.assertTrue(summarize_check.check(reference(answer), answer, FIXTURES)["pass"], text)
            moved = re.sub(r"src/\w+\.py:\d+(?: `[^`]*`)?", signature, text)
            result = summarize_check.check(reply_with(answer, {index: moved}), answer, FIXTURES)
            self.assertFalse(result["pass"], moved)
            self.assertEqual(len(result["unsupported"]), 1, moved)

    def test_citing_each_support_line_passes_and_every_line_outside_the_set_fails(self):
        # For every fact and every sentence that states it: the same sentence citing each line of its support_lines (alone,
        # unquoted) passes; citing any line outside the set, in the file the sentence cites, with the same width, fails as
        # `unsupported` and as nothing else. Qwen's known failure is a plausible, in-range, wrong line number (12 of 15 in Main's smoke).
        passed = shifted = 0
        for chore_id, answer, k in self.facts():
            labels = labels_of(answer)
            sets = summarize_check.support_sets(answer["support_lines"][k])
            for i in stating(answer, k):
                text = unquoted(answer["reference"][i]["text"])
                name, a, b = CITATION.search(text).groups()
                width = int(b or a) - int(a) + 1
                cases = [("{}:{}".format(n_name, n), True) for n_name, lines in sorted(sets.items()) for n in sorted(lines)]
                size = len(fixture_lines(name))
                for first in range(1, size - width + 2):
                    if not set(range(first, first + width)) & sets.get(name, set()):
                        cases.append(("{}:{}".format(name, first) if width == 1 else "{}:{}-{}".format(name, first, first + width - 1), False))
                for citation, ok in cases:
                    result = summarize_check.check(reply_with(answer, {i: CITATION.sub(citation, text)}), answer, FIXTURES)
                    where = "{} fact {} cites {}: {}".format(chore_id, k, citation, result)
                    self.assertEqual(result["pass"], ok, where)
                    if ok:
                        passed += 1
                    else:
                        shifted += 1
                        self.assertEqual(result["unsupported"], [labels[k]], where)
                        self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"], result["forbidden_hit"]),
                                         ([], [], [], []), where)
        self.assertGreaterEqual(passed, 250)
        self.assertGreaterEqual(shifted, 144)  # round 4 tried 144 wrong windows; this tries every one

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
                        text = CITATION.sub("{}:{}".format(name, n), unquoted(answer["reference"][i]["text"]))
                        result = summarize_check.check(reply_with(answer, {i: text}), answer, FIXTURES)
                        self.assertFalse(result["pass"], "{} fact {} cites {}:{}".format(chore_id, k, name, n))
                        self.assertEqual(result["unsupported"], [labels[k]], "{} fact {} cites {}:{}".format(chore_id, k, name, n))
        self.assertGreater(count, 100)

    def test_a_heading_of_an_unknown_file_of_any_extension_makes_the_lines_under_it_bad(self):
        # Round 5, finding 2, for every chore: a heading (or bullet) that names a file the chore did not allow sets the context
        # whatever its extension, and never falls back to the allowed heading above it.
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            allowed = answer["cite_files"][0]
            fine = "{}\n### {}\n- Line 1".format(reference(answer), allowed)
            self.assertEqual(summarize_check.check(fine, answer, FIXTURES)["bad_citations"], [], chore["id"])
            for unknown in ("nonexistent.xyz", "notes.txt", "README.md", "config.json", "src/nonexistent", "docs/nothing", "Makefile.in"):
                for form in ("### {allowed}\n### {unknown}\n- Line 1", "### {allowed}\n- {unknown}\n  - Line 1",
                             "- {allowed}\n- {unknown}: Line 1"):
                    reply = reference(answer) + "\n" + form.format(allowed=allowed, unknown=unknown)
                    result = summarize_check.check(reply, answer, FIXTURES)
                    where = "{} {!r}: {}".format(chore["id"], form.format(allowed=allowed, unknown=unknown), result)
                    self.assertFalse(result["pass"], where)
                    self.assertEqual(result["bad_citations"], ["Line 1 (unknown_file)"], where)

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
        # the reviewer's reversed reply, and the right order in one citation (a fact needs a citation, not a line of its own)
        states = self.answers["steps-states"]
        backwards = summarize_check.check("dead, retrying, succeeded, running, pending (docs/lifecycle.md:5-9).", states, FIXTURES)
        self.assertEqual((backwards["pass"], backwards["order"], backwards["missing"]), (False, labels_of(states)[1:], []))
        forwards = summarize_check.check("pending, running, succeeded, retrying, dead (docs/lifecycle.md:5-9).", states, FIXTURES)
        self.assertTrue(forwards["pass"], forwards)

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
        # F4 of the review: no row of a table has more or fewer cells than the row above it
        for table in re.findall(r"(?:^\|.*\n)+", text, re.M):
            widths = [len(re.findall(r"(?<!\\)\|", row)) for row in table.splitlines()]
            self.assertEqual(len(set(widths)), 1, table.splitlines()[0])


class CheckerTests(unittest.TestCase):
    # Any line of cache.py supports both facts here; test_support_ties_a_citation_to_the_fact narrows it.
    ANSWER = {"required": [["alpha", "first"], ["beta"]], "forbidden": [r"\bgamma\b"], "cite_files": ["src/cache.py"],
              "support_lines": [{"src/cache.py": ["1-36"]}, {"src/cache.py": ["1-36"]}]}
    TWO = dict(ANSWER, cite_files=["src/cache.py", "src/jobq.py"],
               support_lines=[{"src/cache.py": ["1-36"], "src/jobq.py": ["1-49"]}] * 2)
    C = "(src/cache.py:6)"  # `class LRUCache:`

    def check(self, reply, answer=None):
        return summarize_check.check(reply, answer or self.ANSWER, FIXTURES)

    def line(self, needle):
        lines = (FIXTURES / "src/cache.py").read_text().splitlines()
        return next(i for i, text in enumerate(lines, 1) if needle in text)

    def test_pass(self):
        self.assertEqual(self.check("The first thing and beta {}.".format(self.C)),
                         {"pass": True, "missing": [], "missing_citation": [], "unsupported": [], "order": [], "forbidden_hit": [],
                          "bad_citations": [], "unchecked": 0})

    def test_missing_fact(self):
        result = self.check("Only beta {}.".format(self.C))
        self.assertFalse(result["pass"])
        self.assertEqual((result["missing"], result["missing_citation"]), (["alpha"], []))

    def test_forbidden_hit(self):
        result = self.check("alpha beta, and gamma too {}".format(self.C))
        self.assertFalse(result["pass"])
        self.assertEqual(result["forbidden_hit"], [r"\bgamma\b"])
        self.assertEqual((result["missing"], result["missing_citation"]), ([], []))

    def test_citations(self):
        n = self.line("class LRUCache:")
        self.assertEqual(n, 6)
        good = "alpha beta "
        cases = [
            ("path:line without quote", good + "src/cache.py:{}".format(n), True),
            ("quote after matches", good + 'src/cache.py:{} `class LRUCache:`'.format(n), True),
            ("quote after, separator", good + 'src/cache.py:{}: "class  LRUCache:"'.format(n), True),
            ("quote before matches", good + '`class LRUCache:` (src/cache.py:{})'.format(n), True),
            ("range holds quote", good + 'src/cache.py:{}-{} `class LRUCache:`'.format(n - 1, n + 1), True),
            ("en dash range", good + "src/cache.py:{}\u2013{}".format(n - 1, n + 1), True),
            ("absolute path to a cite file", good + "/tmp/x/src/cache.py:{}".format(n), True),
            ("bare file name of a cite file", good + "cache.py:{}".format(n), True),
            ("name closed before the colon", good + "`src/cache.py`:{}".format(n), True),
            ("a clock time and a URL are not citations", good + "at 10:30 on http://localhost:8080 {}".format(self.C), True),
            ("quote not on that line", good + 'src/cache.py:{} `def put(self, key, value):`'.format(n), False),
            ("quote before not on that line", good + '`def put(self, key, value):` (src/cache.py:{})'.format(n), False),
            ("line past the end", good + "src/cache.py:9999", False),
            ("a range of 5 lines", good + "src/cache.py:{}-{}".format(n - 4, n), True),
            ("a range of 6 lines", good + "src/cache.py:{}-{}".format(n - 5, n), False),
            ("a whole file", good + "src/cache.py:1-36", False),
            ("a 6-line range holding the quote", good + 'src/cache.py:1-6 `class LRUCache:`', False),
            ("line zero", good + "src/cache.py:0", False),
            ("range past the end", good + "src/cache.py:{}-9999".format(n), False),
            ("file not in cite_files", good + "src/jobq.py:3", False),
            ("file does not exist", good + "src/nothing.py:3", False),
            # any extension or none: a fabricated file is as bad as a fabricated line
            ("unlisted extension, no such file", good + "src/nonexistent.rs:999", False),
            ("known file type outside cite_files", good + "docs/deploy.md:3", False),
            ("no extension", good + "Makefile:3", False),
            ("bare name of no cite file", good + "worker.py:31", False),
            ("bare name, no extension", good + "nothing:3", False),
        ]
        for name, reply, ok in cases:
            result = self.check(reply)
            self.assertEqual(result["pass"], ok, "{}: {}".format(name, result))
            self.assertEqual(bool(result["bad_citations"]), not ok, "{}: {}".format(name, result))
        wide = self.check(good + "src/cache.py:1-6")
        self.assertEqual(wide["bad_citations"], ["src/cache.py:1-6 (range_too_wide)"])
        self.assertEqual(self.check("alpha beta Lines 1-6 of cache.py")["bad_citations"], ["src/cache.py: Lines 1-6 (range_too_wide)"])

    def test_a_fact_needs_a_citation_on_its_line_or_bullet(self):
        c = self.C
        cases = [
            ("no citation", "alpha and beta", ["alpha", "beta"]),
            ("one line, one citation", "alpha and beta {}".format(c), []),
            ("another line", "alpha {}\nbeta".format(c), ["beta"]),
            ("a blank line ends the scope", "alpha\n\nbeta {}".format(c), ["alpha"]),
            ("bullets", "- alpha {}\n- beta".format(c), ["beta"]),
            ("a wrapped bullet is one bullet", "- alpha\n  beta {}".format(c), []),
            ("a lazy continuation line", "- alpha\nbeta {}".format(c), []),
            ("a numbered item", "1. alpha {}\n2. beta {}".format(c, c), []),
            ("a sub-bullet is its own bullet", "- alpha {}\n  - beta".format(c), ["beta"]),
            ("a heading is not a bullet", "### alpha\n- beta {}".format(c), ["alpha"]),
            ("a bad citation does not support", "alpha beta src/cache.py:9999", ["alpha", "beta"]),
        ]
        for name, reply, expected in cases:
            result = self.check(reply)
            self.assertEqual(result["missing_citation"], expected, "{}: {}".format(name, result))
            self.assertEqual(result["missing"], [], name)
            self.assertEqual(result["pass"], not expected, name)

    def test_support_ties_a_citation_to_the_fact(self):
        # support_lines: a citation must overlap the set of lines that are evidence of the fact; a quote alone, or a number in range,
        # is not enough.
        put, get = self.line("def put(self, key, value):"), self.line("def get(self, key, default=None):")
        answer = dict(self.ANSWER, support_lines=[{"src/cache.py": ["6"]}, {"src/cache.py": [str(put)]}])
        both = "- alpha (src/cache.py:{c})\n- beta (src/cache.py:{p})"
        cases = [
            ("both right, no quotes", both.format(c=6, p=put), [], 0),
            ("both right, quoted", "- alpha (src/cache.py:6 `class LRUCache:`)\n- beta (src/cache.py:{} `def put(self, key, value):`)".format(put), [], 0),
            ("one number moved in range", both.format(c=6, p=get), ["beta"], 1),
            ("both moved in range", both.format(c=7, p=get), ["alpha", "beta"], 2),
            ("moved, and the quote is right for the moved line", "- alpha (src/cache.py:6)\n- beta (src/cache.py:{} `def get(self, key, default=None):`)".format(get), ["beta"], 0),
            ("a range that holds the right line", "- alpha (src/cache.py:6)\n- beta (src/cache.py:{}-{})".format(put - 2, put + 2), [], 0),
            ("a range that stops short of it", "- alpha (src/cache.py:6)\n- beta (src/cache.py:{}-{})".format(put - 4, put - 1), ["beta"], 1),
            ("one of two citations on the line is enough", "- alpha beta (src/cache.py:6, src/cache.py:{})".format(put), [], 0),
            ("the wrong file does not support", "alpha beta (src/jobq.py:3)", ["alpha", "beta"], 1),
        ]
        for name, reply, unsupported, unchecked in cases:
            result = self.check(reply, dict(answer, cite_files=["src/cache.py", "src/jobq.py"]) if "jobq" in reply else answer)
            self.assertEqual(result["unsupported"], unsupported, "{}: {}".format(name, result))
            self.assertEqual((result["missing"], result["missing_citation"], result["bad_citations"]), ([], [], []), name)
            self.assertEqual(result["unchecked"], unchecked, "{}: {}".format(name, result))
            self.assertEqual(result["pass"], not unsupported, name)
        # unsupported is not missing_citation: no good citation at all stays missing_citation
        self.assertEqual(self.check("alpha and beta", answer)["unsupported"], [])
        self.assertEqual(self.check("alpha and beta", answer)["missing_citation"], ["alpha", "beta"])
        with self.assertRaises(ValueError):
            self.check("alpha beta", dict(answer, support_lines=[{"src/cache.py": ["6"]}]))  # one entry for two facts
        for bad in ("x", "", "14-", "-3", "20-14", "1,2", "0x3"):
            with self.assertRaises(ValueError, msg=bad):
                self.check("alpha beta", dict(answer, support_lines=[{"src/cache.py": [bad]}, {"src/cache.py": ["6"]}]))
        self.assertEqual(summarize_check.support_sets({"src/cache.py": ["6", "20-22", "21"], "a": []}),
                         {"src/cache.py": {6, 20, 21, 22}, "a": set()})

    def test_every_citation_is_checked_and_the_unvouched_are_counted(self):
        # A quoted citation is checked by its quote. An unquoted one counts only through a fact's support; one on a line with no fact
        # (or matching no support there) is `unchecked`: counted, not a failure.
        answer = dict(self.ANSWER, support_lines=[{"src/cache.py": ["6"]}, {"src/cache.py": ["6"]}])
        cases = [
            ("a cited fact line only", "alpha beta (src/cache.py:6)", 0),
            ("an unquoted citation on a line with no fact", "alpha beta (src/cache.py:6)\nSee also src/cache.py:20.", 1),
            ("the same, quoted", 'alpha beta (src/cache.py:6)\nSee also src/cache.py:20 `del self._items[key]`.', 0),
            ("an unquoted miss beside a vouching citation", "alpha beta (src/cache.py:6, src/cache.py:20)", 1),
            ("two on a line with no fact", "alpha beta (src/cache.py:6)\nsrc/cache.py:20 and src/cache.py:21", 2),
            ("a bad citation is bad, not unchecked", "alpha beta (src/cache.py:6)\nsrc/cache.py:999", 0),
        ]
        for name, reply, unchecked in cases:
            result = self.check(reply, answer)
            self.assertEqual(result["unchecked"], unchecked, "{}: {}".format(name, result))
            self.assertEqual(result["pass"], name != "a bad citation is bad, not unchecked", "{}: {}".format(name, result))

    def test_line_n_under_a_file_heading_or_bullet(self):
        put = self.line("def put(self, key, value):")
        cases = [
            ("heading", "### `src/cache.py`\n- **Line 6** \u2014 alpha\n- Lines {}-{}: beta".format(put, put + 1), True),
            ("heading, bare name, lower case", "## cache.py\n- alpha (line 6)\n- beta (lines 13-14)", True),
            ("the bullet above names the file", "- src/cache.py\n  - Line 6 \u2014 alpha beta", True),
            ("the same line names the file", "alpha beta: see src/cache.py, line 6", True),
            ("quote after matches", "### src/cache.py\n- Line 6 `class LRUCache:` alpha beta", True),
            ("quote after is not on that line", "### src/cache.py\n- Line 6 `def put(self, key, value):` alpha beta", False),
            ("line past the end", "### src/cache.py\n- Line 9999 \u2014 alpha beta", False),
            ("no file named above", "- Line 6 \u2014 alpha beta", False),
            ("a heading names a file outside cite_files", "### src/jobq.py\n- Line 3 \u2014 alpha beta", False),
            ("the nearest file heading is not a cited file", "### src/cache.py\n### src/jobq.py\n- Line 6 \u2014 alpha beta", False),
            ("a plain paragraph is not a heading", "src/cache.py\n\n- Line 6 \u2014 alpha beta", False),
        ]
        for name, reply, ok in cases:
            result = self.check(reply)
            self.assertEqual((result["pass"], bool(result["bad_citations"])), (ok, not ok), "{}: {}".format(name, result))
            self.assertEqual(result["missing"], [], name)
        two = self.check("### src/cache.py and src/jobq.py\n- Line 6 \u2014 alpha beta", self.TWO)
        self.assertEqual(two["bad_citations"], ["Line 6 (ambiguous_context)"])
        self.assertFalse(two["pass"])
        under = self.check("### src/jobq.py\n- Line 3 \u2014 alpha beta\n### src/cache.py\n- Line 6 \u2014 alpha beta", self.TWO)
        self.assertTrue(under["pass"], under)

    def test_line_n_context_must_be_one_allowed_file_named_whole(self):
        # The nearest heading or bullet that names any file-like token sets the file; it must name exactly one, whole (a path
        # or a bare name, not a prefix of a longer name), and it must be a cite_files entry. The first three are the reviewer's.
        ambiguous, not_allowed, none = "Line 6 (ambiguous_context)", "Line 6 (unknown_file)", "Line 6 (no_context)"
        cases = [
            ("a backup of the file is not the file", "### src/cache.py.bak\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a nearer heading of a file that is not there", "### src/cache.py\n### src/nonexistent.zig\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("an allowed and a disallowed file", "### src/cache.py and src/nonexistent.rs\n- alpha beta Line 6", self.ANSWER, [ambiguous]),
            ("two allowed files", "### src/cache.py and src/jobq.py\n- alpha beta Line 6", self.TWO, [ambiguous]),
            ("a bare backup name", "### cache.py.bak\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a longer name that ends the same", "### my_cache.py\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a name with another extension", "### src/cache.pyc\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a path of any extension", "### src/cache.xyz\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a nearer bullet of a file that is not there", "### src/cache.py\n- src/nonexistent.zig\n  - alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a nearer bullet of another cited file", "### src/cache.py\n- src/jobq.py\n  - alpha beta Line 6", self.TWO, []),  # jobq.py has 6 lines
            ("no file anywhere above", "- alpha beta Line 6", self.ANSWER, [none]),
            ("e.g. and i.e. are not file names", "### Notes, e.g. or i.e. only\n- alpha beta Line 6", self.ANSWER, [none]),
            ("a call is not a file name", "### Calls queue.pop(now) and slugify(text)\n- alpha beta Line 6", self.ANSWER, [none]),
            ("a version is not a file name", "### Python 3.12 and v1.2\n- alpha beta Line 6", self.ANSWER, [none]),
            # the price of "any extension": a dotted identifier in a heading reads as a file, so its lines are bad, not guessed at
            ("a dotted identifier in a heading is read as a file", "### Notes on queue.dead\n- alpha beta Line 6", self.ANSWER, [not_allowed]),
            ("a dotted identifier beside the file", "### LRUCache.get in src/cache.py\n- alpha beta Line 6", self.ANSWER, [ambiguous]),
            ("a dotted identifier in a code span with a space", "### `LRUCache.get(key)` in src/cache.py\n- alpha beta Line 6", self.ANSWER, []),
            ("the same file twice", "### src/cache.py (cache.py)\n- alpha beta Line 6", self.ANSWER, []),
            ("an absolute path to the file", "### /tmp/x/src/cache.py\n- alpha beta Line 6", self.ANSWER, []),
            ("a dotted name in the quoted code", "### src/cache.py\n- alpha beta Line 30 `self._items.popitem(last=False)`", self.ANSWER, []),
            ("a skipped heading with no file", "### src/cache.py\n### The code\n- alpha beta Line 6", self.ANSWER, []),
        ]
        for name, reply, answer, bad in cases:
            result = self.check(reply, answer)
            self.assertEqual(result["bad_citations"], bad, "{}: {}".format(name, result))
            self.assertEqual(result["pass"], not bad, "{}: {}".format(name, result))

    def test_citations_are_removed_before_a_fact_is_matched(self):
        # Round 5, finding 1: a citation (its numbers, the quote beside it, the brackets it leaves empty) never satisfies a fact.
        # A name plus a quoted signature has to be backed by words that say the fact.
        answer = dict(self.ANSWER, required=[[r"\bLRUCache\b[^\n]*\bclass\b"], [r"\bbeta\b"]])
        cases = [
            ("the words say it", "LRUCache is a class and beta (src/cache.py:6)", True),
            ("only the quote says it", "LRUCache and beta (src/cache.py:6 `class LRUCache:`)", False),
            ("the quote before the citation", "LRUCache and beta: `class LRUCache:` (src/cache.py:6)", False),
            ("a Line N reference with its quote", "### src/cache.py\n- LRUCache Line 6 `class LRUCache:`\n- beta Line 6", False),
            ("the same, with words", "### src/cache.py\n- LRUCache is a class, Line 6 `class LRUCache:`\n- beta Line 6", True),
            ("a word glued by the citation is not made", "LRUCache(src/cache.py:6) class and beta (src/cache.py:6)", True),
        ]
        for name, reply, ok in cases:
            result = self.check(reply, answer)
            self.assertEqual(result["pass"], ok, "{}: {}".format(name, result))
            self.assertEqual(result["missing"], [] if ok else [answer["required"][0][0]], "{}: {}".format(name, result))
        # the file name is not removed from running text (it can be what the fact says); inside brackets it goes with the citation
        named = dict(self.ANSWER, required=[[r"cache\.py"], [r"\bbeta\b"]])
        self.assertTrue(self.check("cache.py:6 holds it and beta (src/cache.py:6)", named)["pass"])
        self.assertEqual(self.check("it holds beta (src/cache.py:6)", named)["missing"], [r"cache\.py"])
        # a forbidden pattern is looked for in the reply as written and in the text left
        forbidden = dict(self.ANSWER, forbidden=[r"cache\.py:6\b"])
        self.assertEqual(self.check("alpha beta (src/cache.py:6)", forbidden)["forbidden_hit"], [r"cache\.py:6\b"])

    def test_mains_wrong_line_reproducer_and_the_right_numbers_without_quotes(self):
        # Main (round 4): use-jobqueue's own reference with the quotes removed and each number changed to a wrong one in range
        # passed with no bad citation, because an unquoted citation was only checked to be in range.
        answer = next(a for a in jsonl("answers.jsonl") if a["id"] == "use-jobqueue")
        right = unquoted(reference(answer))
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

    def test_main_smoke_reply_is_scored_by_the_new_rules(self):
        # Main's live smoke (use-jobqueue, 2026-11-08) passed the first checker with zero bad citations: it cited "Line N" under
        # "### src/<file>" headings. Of its 15 line numbers 3 are right (cli.py 29, 31, 34); 12 are wrong. Round 5: 11 citations are
        # bad (the 8 whose quote or range is wrong, the 21-line "Lines 18-38", and worker.py "Line 35", "Line 40" and "Line 45":
        # the bullet of Line 35 names `queue.dead`, which reads as a file that is not one of the chore's, and the two bullets below
        # it have no file of their own, so the nearest one that names a file is that bullet); so facts 3 to 6 have no good citation.
        # The right cli.py 29 and 31 support their facts; cli.py 9 and 34 are unchecked.
        reply = (DATA / "samples" / "smoke-use-jobqueue.txt").read_text(encoding="utf-8")
        answer = next(a for a in jsonl("answers.jsonl") if a["id"] == "use-jobqueue")
        result = summarize_check.check(reply, answer, FIXTURES)
        labels = labels_of(answer)
        self.assertFalse(result["pass"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["missing_citation"], labels[2:])  # jobq.py defines it; worker.py receives, pops, handles failure
        self.assertEqual(result["unsupported"], [])
        self.assertEqual(result["order"], [])
        self.assertEqual(result["bad_citations"], [
            "src/jobq.py: Lines 18\u201338 (range_too_wide)", "src/jobq.py: line 19", "src/jobq.py: line 23", "src/jobq.py: line 29",
            "src/jobq.py: line 34", "src/jobq.py: line 3", "src/jobq.py: line 39", "src/worker.py: Line 31", "Line 35 (unknown_file)",
            "Line 40 (unknown_file)", "Line 45 (unknown_file)"])
        self.assertEqual(result["unchecked"], 2)

    def test_a_reply_under_file_headings_passes_and_a_bare_name_is_not_a_heading(self):
        # "Line N" under a heading (or bold line, or bullet) that is only a file's name; the fact is found in the lines below it.
        answer = next(a for a in jsonl("answers.jsonl") if a["id"] == "use-jobqueue")
        grouped = ("### src/cli.py\n- Line 29 \u2014 creates a JobQueue\n- Line 31 \u2014 pushes a job onto it\n"
                   "### src/jobq.py\n- Line 23 \u2014 defines the JobQueue class\n"
                   "### src/worker.py\n- Line 30 \u2014 takes the queue as an argument\n- Line 32 \u2014 pops jobs from it\n"
                   "- Line 44 \u2014 fails the job so that it is retried\n")
        result = summarize_check.check(grouped, answer, FIXTURES)
        self.assertTrue(result["pass"], result)
        bold = grouped.replace("### src/", "**src/").replace(".py\n", ".py**\n")
        self.assertTrue(summarize_check.check(bold, answer, FIXTURES)["pass"], bold)
        nested = ("- src/cli.py\n  - Line 29 \u2014 creates a JobQueue\n  - Line 31 \u2014 pushes a job\n- src/jobq.py\n  - Line 23 \u2014 defines JobQueue\n"
                  "- src/worker.py\n  - Line 30 \u2014 takes the queue as an argument\n  - Line 32 \u2014 pops jobs\n  - Line 44 \u2014 fails the job\n")
        self.assertTrue(summarize_check.check(nested, answer, FIXTURES)["pass"], nested)
        # a wrong line under the heading is unsupported, as anywhere
        self.assertEqual(summarize_check.check(grouped.replace("Line 32", "Line 36"), answer, FIXTURES)["unsupported"],
                         [labels_of(answer)[4]])
        # the file's name alone on a line of running text, with its citation removed, is not a heading
        bare = ("src/cli.py (src/cli.py:23 `def main(argv=None):`)\n" + "\n".join(s["text"] for s in answer["reference"][2:]))
        self.assertEqual(summarize_check.check(bare, answer, FIXTURES)["missing"], labels_of(answer)[:2])


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
        self.reply.write_text("worker.py uses LRUCache (src/worker.py:999).")
        _, records, summary = self.run_main()
        self.assertFalse(records[0]["pass"])
        self.assertEqual(records[0]["bad_citations"], ["src/worker.py:999"])
        self.assertTrue(records[0]["forbidden_hit"] and records[0]["missing"])
        self.assertEqual((summary["passed"], summary["fabricated_citations"]), (0, 1))
        self.assertEqual(set(summary["failure_classes"]), {"missing", "forbidden_hit", "bad_citations"})
        # a wrong line in range, no quote: scored `unsupported`, and the unquoted citation nothing vouched for is counted
        self.reply.write_text(unquoted(self.example()).replace("src/cache.py:6", "src/cache.py:1") + "\nsee src/worker.py:30")
        _, records, summary = self.run_main()
        self.assertEqual((records[0]["pass"], records[0]["unsupported"], records[0]["unchecked"]), (False, [records[0]["unsupported"][0]], 2))  # cache.py:1 and worker.py:30
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


if __name__ == "__main__":
    unittest.main()
