"""The summarize_files qualification kit: tests/calibration/summarize/ (chores, answers, fixtures), tools/qual/summarize_check.py
(the scorer) and tools/qual/summarize_run.py (the runner). No model is run: the worker is a stub script."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import itertools
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


# ---- the generated attack (round 7): every mutation of the reference reply of a chore, built from the reference, not listed by hand ----
MISSING, UNCITED, NOT_FACT = (4, "missing"), (2, "uncited"), (2, "not_a_fact_line")
SUBJECT_RULES = {(6, "wrong_subject"), (6, "no_subject")}
IDENT = re.compile(r"[A-Za-z_]\w*")
# round 8, written here and not read from the checker: what may precede a list marker, and every bullet rule 11 names
PREFIXES = ["\u00a0", "\u2003", "\u3000", "\u2009", ">", "> ", ">>"]
BULLET_CHARS = ["-", "*", "+", "\u2022", "\u2023", "\u25e6", "\u2043", "\u2013", "\u2014", "\u00b7"]
NUMBERED = ["1.", "2)", "10."]
STRAY, REPEATED, ORDER = (2, "stray_citation"), (7, "repeated_step"), (None, "order")


def split_line(text):
    """(claim, citation list) of a reference fact line."""
    m = CITE.search(text)
    return text[2:m.start()].rstrip(), m.group(1)


def subjects(answer):
    return {int(k): Path(v["name"]).name for k, v in answer.get("behaviour", {}).items()}


def behaviour_words(answer, k, claim):
    """(start, end) of each word of the claim that the regexes of behaviour fact k match, with the chore's names masked first."""
    names = summarize_check.names_in_code(FIXTURES, answer["cite_files"])
    masked = IDENT.sub(lambda m: " " * len(m.group(0)) if m.group(0) in names else m.group(0), claim)
    spans = set()
    for alternative in answer["required"][k]:
        for m in re.finditer(alternative, masked, re.I):
            for word in IDENT.finditer(masked):
                if word.start() < m.end() and m.start() < word.end() and m.end() > m.start():
                    spans.add(word.span())
    return sorted(spans)


def wrap_words(claim, spans, before, after):
    for start, end in reversed(spans):
        claim = claim[:start] + before + claim[start:end] + after + claim[end:]
    return claim


def mutations(answer):
    """Every mutation of the reference reply of a chore: (kind, reply, any_of, expect_pass). A mutated reply must fail and the failures
    must include one of any_of ((rule, kind) pairs); expect_pass is True for the few that change nothing the rules look at."""
    ref = answer["reference"]
    texts = [s["text"] for s in ref]
    parts = [split_line(t) for t in texts]
    support = [summarize_check.support_sets(entry) for entry in answer["support_lines"]]
    subject = subjects(answer)
    size = {name: len(fixture_lines(name)) for name in answer["cite_files"]}
    outside = next(f for f in ("src/jobq.py", "docs/limits.md", "src/utils.py") if f not in answer["cite_files"])

    def reply(changes=None, extra=(), first=()):
        return "\n".join(list(first) + [(changes or {}).get(i, t) for i, t in enumerate(texts) if (changes or {}).get(i, t) is not None] + list(extra))

    def lines_of(k):
        return [i for i, s in enumerate(ref) if k in s["facts"]]

    def cite_text(name, a, b=None):
        return "{}:{}".format(name, a) if b is None or b == a else "{}:{}-{}".format(name, a, b)

    def each(f):  # f(claim, cite) -> the mutated line, for every line at once
        return {i: f(claim, cite) for i, (claim, cite) in enumerate(parts)}

    def line(claim, cite):
        return "- {} [{}]".format(claim, cite)

    # a. subject swap: the subject tokens of the lines of two behaviour facts with different subjects trade places
    for k1, k2 in itertools.combinations(sorted(subject), 2):
        i, j = lines_of(k1)[0], lines_of(k2)[0]
        if subject[k1] != subject[k2] and i != j:
            (c1, q1), (c2, q2) = parts[i], parts[j]
            assert c1.startswith(subject[k1]) and c2.startswith(subject[k2]), (answer["id"], k1, k2)
            yield ("a subject swap", reply({i: line(subject[k2] + c1[len(subject[k1]):], q1), j: line(subject[k1] + c2[len(subject[k2]):], q2)}),
                   SUBJECT_RULES | {(3, "unsupported")}, False)
    # b. an intro line naming every fact (all claims, all citations) before the rest, in order and reversed, in a chore with an order
    if answer.get("order"):
        intro = "- {} [{}]".format("; ".join(c for c, _ in parts), ", ".join(q for _, q in parts))
        yield "b intro line, rest in order", reply(first=[intro]), {(1, "several_in_one_line")}, False
        yield "b intro line, rest reversed", "\n".join([intro] + texts[::-1]), {(1, "several_in_one_line")}, False
    # c. code spans around the behaviour words of a behaviour line
    for k in sorted(subject):
        for i in lines_of(k):
            claim, cite = parts[i]
            spans = behaviour_words(answer, k, claim)
            assert spans, (answer["id"], k, claim)
            for n in (1, 2, 3):
                tick = "`" * n
                yield "c code span of %d" % n, reply({i: line(wrap_words(claim, spans, tick, tick), cite)}), {MISSING}, False
                yield "c code span of %d with spaces" % n, reply({i: line(wrap_words(claim, spans, tick + " ", " " + tick), cite)}), {MISSING}, False
            yield "c unmatched backtick", reply({i: line("` " + wrap_words(claim, spans, "``", "``"), cite)}), {MISSING}, False
    # d. list items appended: bare and indented, starred and numbered
    ok = "[{}]".format(parts[0][1])
    for item in ("-", "- ", "-\t", "- \t", "  - x " + ok, "* x " + ok, "1. x " + ok):
        yield "d list item", reply(extra=[item]), {UNCITED, NOT_FACT}, False
    # e. Main's round-6 kinds
    for n in (-3, -2, -1, 1, 2, 3):  # every citation moved by n lines
        moved, fine = {}, True
        for i, (claim, cite) in enumerate(parts):
            name, lines = cite.rsplit(":", 1)
            a, _, b = lines.partition("-")
            a2, b2 = int(a) + n, int(b or a) + n
            moved[i] = line(claim, cite_text(name, a2, b2))
            rng = set(range(a2, b2 + 1))
            fine = fine and 1 <= a2 and b2 <= size[name] and all(rng & support[k].get(name, set()) for k in ref[i]["facts"])
        yield "e shift %+d" % n, reply(moved), {(3, "unsupported"), (3, "range")}, fine
    yield "e citation on the next line", "\n".join(l for t in parts for l in ("- " + t[0], "[" + t[1] + "]")), {UNCITED}, False
    yield "e heading file", "\n".join(l for claim, cite in parts for l in ("### " + cite.rsplit(":", 1)[0], "- {} (Line {})".format(claim, cite.rsplit(":", 1)[1]))), {UNCITED}, False
    yield "e basename", reply(each(lambda c, q: line(c, Path(q.rsplit(":", 1)[0]).name + ":" + q.rsplit(":", 1)[1]))), {(3, "unknown_file")}, False
    yield "e generic claim", reply(each(lambda c, q: line("This part of the code is relevant to the question", q))), {MISSING}, False
    yield "e one line", "- {} [{}]".format("; ".join(c for c, _ in parts), ", ".join(q for _, q in parts)), {(1, "shared_line"), (1, "several_in_one_line"), MISSING}, False
    for i in range(len(texts)):
        yield "e drop a line", reply({i: None}), {MISSING, (3, "unsupported")}, False
    yield "e wide range", reply(each(lambda c, q: line(c, cite_text(q.rsplit(":", 1)[0], 1, size[q.rsplit(":", 1)[0]])))), {(3, "range_too_wide")}, False
    yield "e extra non-chore cite", reply(each(lambda c, q: line(c, q + ", " + outside + ":1"))), {(3, "unknown_file")}, False
    yield "e star bullets", reply({i: "* " + t[2:] for i, t in enumerate(texts)}), {NOT_FACT}, False
    # f. a prefix in front of "- x [cite]" and "- x" (round 8, rule 11), and in front of every reference line
    for prefix in PREFIXES:
        yield "f prefix, with a citation", reply(extra=[prefix + "- x " + ok]), {NOT_FACT}, False
        yield "f prefix, no citation", reply(extra=[prefix + "- x"]), {NOT_FACT}, False
        yield "f prefix on every line", reply(each(lambda c, q: prefix + line(c, q))), {NOT_FACT}, False
    # g. each bullet of rule 11 in place of "- ": appended with a citation, appended bare, and on every reference line
    for bullet in [b for b in BULLET_CHARS if b != "-"] + NUMBERED:
        yield "g bullet, with a citation", reply(extra=[bullet + " x " + ok]), {NOT_FACT}, False
        yield "g bullet, no citation", reply(extra=[bullet + " x"]), {NOT_FACT}, False
        yield "g bullet, bare", reply(extra=[bullet]), {NOT_FACT}, False
        yield "g bullet on every line", reply({i: bullet + " " + t[2:] for i, t in enumerate(texts)}), {NOT_FACT}, False
    # h. a fabricated citation (rule 10) on a prose line, a heading, a quote, inside a code span and inside a code block
    past = "[{}:{}]".format(parts[0][1].rsplit(":", 1)[0], size[parts[0][1].rsplit(":", 1)[0]] + 50)
    for fab, kind in (("[src/nonexistent.rs:999]", (3, "unknown_file")), (past, (3, "range"))):
        for where, wrap in (("prose", "Also see {}"), ("heading", "### {}"), ("quote", "> {}"), ("code span", "`{}`"), ("code block", "```\n{}\n```"),
                            ("sentence", "Note: " + parts[0][0] + " {}")):
            yield "h fabricated citation, " + where, reply(extra=[wrap.format(fab)]), {kind}, False
    # i. in a chore with an order: every line that meets an ordered fact counts, in reply order (rule 12)
    if answer.get("order"):
        yield "i reference + reversed", "\n".join(texts + texts[::-1]), {REPEATED}, False
        yield "i reversed + reference", "\n".join(texts[::-1] + texts), {REPEATED}, False
        yield "i reference + reference", "\n".join(texts + texts), {REPEATED}, False
        yield "i reversed", "\n".join(texts[::-1]), {ORDER}, False
        for i in range(len(texts)):
            yield "i one step repeated", reply(extra=[texts[i]]), {REPEATED}, False
            yield "i one step repeated in place", "\n".join(texts[:i + 1] + texts[i:]), {REPEATED}, False


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
        for text, expected in (("- {} [docs/lifecycle.md:5-9]".format(one), (1, "several_in_one_line")),
                               ("{} (docs/lifecycle.md:5-9).".format(one), (4, "missing"))):
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
        # Rule 2, for every chore: headings, bold lines, prose, blank lines and quotes, with citations and file names in them, are
        # no list items: they are ignored, adding them changes nothing (the reference still passes, nothing is unchecked, nothing
        # is bad); and a reply made only of them states nothing. A list item that is not a fact line (rule 9) supplies nothing
        # either, and fails the reply.
        noise = ["# Summary", "", "### src/cache.py", "**src/jobq.py**", "Line 6 -- this is not a fact line (src/cache.py:6).",
                 "-no space after the dash (src/cache.py:6)", "> quote (src/cache.py:6)", "The files hold it: src/cache.py:6, Lines 1-99.",
                 "### archive.7z", "Not in the files.", "---", "*emphasis* (src/cache.py:6)", "1.5 is a number (src/cache.py:6)"]
        # with the citation in brackets such a line is no longer ignored (rule 10): it holds a citation and is no fact line
        strays = [line.replace("(src/cache.py:6)", "[src/cache.py:6]") for line in noise if "(src/cache.py:6)" in line and "Line 6" not in line]
        items = ["  - indented bullet [src/cache.py:6]", "* starred bullet [src/cache.py:6]", "+ plus bullet [src/cache.py:6]",
                 "1. numbered [src/cache.py:6]", "2) numbered [src/cache.py:6]", "-\ttab bullet [src/cache.py:6]"]
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            lines = [s["text"] for s in answer["reference"]]
            spread = []
            for n, line in enumerate(lines):
                spread += [noise[n % len(noise)], line]
            result = self.check("\n".join(noise + spread + noise), chore["id"])
            self.assertTrue(result["pass"], "{}: {}".format(chore["id"], result))
            self.assertEqual((result["unchecked"], result["failures"], result["bad_citations"], result["uncited"]), (0, [], [], []), chore["id"])
            only = self.check("\n".join(noise), chore["id"])
            self.assertEqual((only["bad_citations"], only["uncited"], only["unchecked"]), ([], [], 0), chore["id"])
            self.assertEqual(len(only["missing"]), len(answer["required"]), chore["id"])
            for stray in strays:
                result = self.check("\n".join(lines + [stray]), chore["id"])
                self.assertFalse(result["pass"], (chore["id"], stray))
                self.assertEqual((result["missing"], result["uncited"], rules(result) & {STRAY}), ([], [stray], {STRAY}), (chore["id"], stray))
            for item in items:  # states nothing, and the reply fails for it
                result = self.check("\n".join(lines + [item]), chore["id"])
                self.assertFalse(result["pass"], (chore["id"], item))
                self.assertEqual((result["missing"], result["uncited"], result["unchecked"], rules(result)),
                                 ([], [item], 0, {(2, "not_a_fact_line")} | ({(3, "unknown_file")} if "src/cache.py" not in answer["cite_files"] else set())),
                                 (chore["id"], item))  # a citation of a file the chore does not name is bad wherever it is (rule 10)
            # the reference as a list of anything but "- " lines states nothing
            for mark in ("* ", "1. ", "  - ", "", "-"):
                other = "\n".join(mark + t[2:] if mark != "-" else "-" + t[2:] for t in lines)
                result = self.check(other, chore["id"])
                self.assertEqual(len(result["missing"]), len(answer["required"]), (chore["id"], mark))
                # every such line holds a bracketed citation and is no fact line: a list item of another kind is not_a_fact_line (rules 9, 11),
                # any other line is a stray_citation (rule 10); a step that starts "1. pending" is a numbered item once the dash is gone
                self.assertEqual(len(result["uncited"]), len(lines), (chore["id"], mark))
                self.assertTrue(rules(result) & {NOT_FACT, STRAY}, (chore["id"], mark))
                self.assertLessEqual({f["kind"] for f in result["failures"] if f["rule"] == 2}, {"not_a_fact_line", "stray_citation"}, (chore["id"], mark))

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

    def test_the_references_start_with_their_subject(self):
        # Rule 6: every reference line of a behaviour fact starts with the name that fact is about (answer["behaviour"][k]["name"]),
        # and the checker reads that name as the subject. The sig line is where the name is declared.
        count = 0
        for chore_id, answer, k in self.facts():
            behaviour = answer.get("behaviour", {}).get(str(k))
            if behaviour is None:
                continue
            names = summarize_check.names_in_code(FIXTURES, answer["cite_files"])
            for i in stating(answer, k):
                claim = summarize_check.parse_fact_line(answer["reference"][i]["text"])[0]
                self.assertTrue(claim.startswith(behaviour["name"]), "{} fact {}: {}".format(chore_id, k, claim))
                self.assertEqual(summarize_check.subject_of(claim, names, answer["cite_files"]), Path(behaviour["name"]).name, claim)
                count += 1
            sig_file, sig_line = behaviour["sig"].rsplit(":", 1)
            self.assertIn(sig_file, answer["cite_files"], chore_id)
            if behaviour["name"].endswith(".py"):
                self.assertEqual(Path(sig_file).name, behaviour["name"], chore_id)  # a file subject: the sig line is in that file
            else:
                self.assertIn(behaviour["name"], fixture_lines(sig_file)[int(sig_line) - 1], "{} fact {}".format(chore_id, k))
        self.assertGreaterEqual(count, 65)

    def test_review_6_reproducers_fail_for_their_rule(self):
        # review-coder-d50afe8142-204331 reproduce.py, F1 to F4, as replies with the rule and kind each must fail.
        worker, states = self.answers["exports-worker"], self.answers["steps-states"]
        right = reference(worker)
        echo = worker["reference"][8]["text"]
        self.assertEqual(echo, "- echo returns its payload unchanged [src/worker.py:22]")
        # F1: sleep_job's name on echo's claim and line, and echo's on sleep_job's: the subject is not the fact's (rule 6)
        swapped = right.replace("- echo returns", "- sleep_job returns").replace("- sleep_job sleeps", "- echo sleeps")
        result = self.check(swapped, "exports-worker")
        self.assertFalse(result["pass"])
        self.assertEqual(rules(result), {(4, "missing"), (6, "wrong_subject")})
        self.assertEqual(result["missing"], [labels_of(worker)[8], labels_of(worker)[9]])
        # the pairs the reviewer named: Job and JobQueue, parse_duration and chunked, get and put
        for chore_id, one, other in (("exports-jobq", "- Job holds", "- JobQueue holds"), ("exports-utils", "- parse_duration converts", "- chunked yields"),
                                     ("exports-cache", "- get returns", "- put stores")):
            answer = self.answers[chore_id]
            words = [(s["text"], s["text"].split(" ", 2)) for s in answer["reference"] if s["text"].startswith((one, other))]
            self.assertEqual(len(words), 2, chore_id)
            (t1, (_, n1, _)), (t2, (_, n2, _)) = words
            both = reference(answer).replace(t1, "\0").replace(t2, t1.replace(n1, n2, 1)).replace("\0", t2.replace(n2, n1, 1))
            result = self.check(both, chore_id)
            self.assertFalse(result["pass"], chore_id)
            self.assertTrue(rules(result) & (SUBJECT_RULES | {(3, "unsupported")}), (chore_id, rules(result)))
            self.assertEqual(len(result["missing"]) + len(result["unsupported"]), 2, chore_id)
        # F2: a line that names every state before the states in reverse order (and before them in order): rule 1, several_in_one_line
        lines = [s["text"] for s in states["reference"]]
        intro = "- The states are dead, retrying, succeeded, running, pending [docs/lifecycle.md:5-9]"
        for name, tail in (("reversed", lines[::-1]), ("in order", lines)):
            result = self.check("\n".join([intro] + tail), "steps-states")
            self.assertFalse(result["pass"], name)
            self.assertIn((1, "several_in_one_line"), rules(result), name)
            self.assertEqual(result["uncited"], [intro], name)
        result = self.check("\n".join(lines[::-1]), "steps-states")  # without the intro line the order is the order of the lines that met the facts
        self.assertEqual((result["order"], rules(result)), (labels_of(states)[1:], {(None, "order")}))
        # F3: double backticks hide a behaviour word as single backticks do (rule 8, then rule 5): the fact is missing
        for ticks in ("`", "``", "```", "````"):
            result = self.check(right.replace(echo, "- echo {t}returns{t} [src/worker.py:22]".format(t=ticks)), "exports-worker")
            self.assertFalse(result["pass"], ticks)
            self.assertIn(MISSING, rules(result), ticks)
            self.assertEqual(result["missing"], [labels_of(worker)[8]], ticks)
        # F4: a bare bullet, "- " and "- <tab>" are list items that are no fact line: rule 2, and the reply fails
        for bare in ("-", "- ", "-\t", "- \t", "- \t "):
            result = self.check(right + "\n" + bare, "exports-worker")
            self.assertFalse(result["pass"], repr(bare))
            self.assertEqual(result["uncited"], [bare], repr(bare))
            self.assertTrue(rules(result) <= {UNCITED, NOT_FACT} and rules(result), repr(bare))
            self.assertEqual(result["missing"], [], repr(bare))

    def test_review_7_and_mains_round_8_reproducers_fail_for_their_rule(self):
        # review-coder-07bbf506d8-210919 F1 and the lines Main appended to a passing reference (use-jobqueue): each fails for its rule and
        # kind, and a fabricated citation on any of them is counted in bad_citations
        right = reference(self.answers["use-jobqueue"])
        self.assertTrue(self.check(right, "use-jobqueue")["pass"])
        fake = "[src/nonexistent.rs:999]"
        bad = ["src/nonexistent.rs:999 (unknown_file)"]
        cases = [("review F1: em space, no citation", "\u2003- no citation", NOT_FACT, []),
                 ("review F1: nbsp, fabricated", "\u00a0- fabricated " + fake, NOT_FACT, bad),
                 ("Main: also see", "Also see " + fake, STRAY, bad),
                 ("Main: note", "Note: cli.py creates a JobQueue " + fake, STRAY, bad),
                 ("Main: bullet dot", "\u2022 x " + fake, NOT_FACT, bad), ("Main: en dash", "\u2013 x " + fake, NOT_FACT, bad),
                 ("Main: quote bullet", "> - x " + fake, NOT_FACT, bad), ("Main: ideographic space", "\u3000- x", NOT_FACT, []),
                 ("Main: one space, no citation", " - no citation", NOT_FACT, []), ("Main: one space, fabricated", " - fabricated " + fake, NOT_FACT, bad)]
        for name, extra, expected, bad_citations in cases:
            result = self.check(right + "\n" + extra, "use-jobqueue")
            self.assertFalse(result["pass"], name)
            self.assertIn(expected, rules(result), name)
            self.assertEqual(result["bad_citations"], bad_citations, name)
            self.assertEqual(result["uncited"], [extra], name)
            self.assertEqual((result["missing"], result["unsupported"], result["order"]), ([], [], []), name)

    def test_generated_attack_every_mutation_of_every_reference_fails(self):
        # Not a list of examples: for each of the 24 chores every mutation of its reference reply is built (mutations(), above) and
        # must fail with a failure naming one of the rules it is for. A mutation that changes nothing the rules look at (a citation
        # shifted onto another line of the same evidence) must pass: the oracle and the checker agree. Counts per kind are printed
        # when QUAL_ATTACK_COUNTS is set.
        counts = {}
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            self.assertTrue(self.check(reference(answer), chore["id"])["pass"], chore["id"])
            for kind, reply, any_of, expect_pass in mutations(answer):
                result = self.check(reply, chore["id"])
                count = counts.setdefault(kind, {"mutations": 0, "failed": 0, "passed": 0, "rule_6": 0})
                count["mutations"] += 1
                where = "{} / {}: {!r}".format(chore["id"], kind, reply)
                if expect_pass:
                    self.assertTrue(result["pass"], where + " " + str(result["failures"]))
                    count["passed"] += 1
                    continue
                self.assertFalse(result["pass"], where)
                self.assertTrue(any_of & rules(result), "{} -> {}".format(where, sorted(rules(result), key=str)))
                if kind.startswith("h "):
                    self.assertTrue(result["bad_citations"], where)  # rule 10: counted in bad_citations, so in the run's fabricated count
                count["failed"] += 1
                count["rule_6"] += bool(SUBJECT_RULES & rules(result))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            for kind in sorted(counts):
                print("attack {:<28} {}".format(kind, counts[kind]), file=sys.stderr)
        self.assertEqual(sum(c["passed"] for c in counts.values()), sum(c["passed"] for k, c in counts.items() if k.startswith("e shift")))
        floors = {"a subject swap": 95, "b intro line, rest in order": 2, "b intro line, rest reversed": 2, "c code span of 1": 65, "c code span of 2": 65,
                  "c code span of 3": 65, "c code span of 1 with spaces": 65, "c code span of 2 with spaces": 65, "c code span of 3 with spaces": 65,
                  "c unmatched backtick": 65, "d list item": 24 * 7, "e shift -3": 24, "e shift +3": 24, "e citation on the next line": 24,
                  "e heading file": 24, "e basename": 24, "e generic claim": 24, "e one line": 24, "e drop a line": 100, "e wide range": 24,
                  "e extra non-chore cite": 24, "e star bullets": 24,
                  "f prefix, with a citation": 24 * 7, "f prefix, no citation": 24 * 7, "f prefix on every line": 24 * 7,
                  "g bullet, with a citation": 24 * 12, "g bullet, no citation": 24 * 12, "g bullet, bare": 24 * 12, "g bullet on every line": 24 * 12,
                  "h fabricated citation, prose": 48, "h fabricated citation, heading": 48, "h fabricated citation, quote": 48,
                  "h fabricated citation, code span": 48, "h fabricated citation, code block": 48, "h fabricated citation, sentence": 48,
                  "i reference + reversed": 2, "i reversed + reference": 2, "i reference + reference": 2, "i reversed": 2, "i one step repeated": 11,
                  "i one step repeated in place": 11}
        for kind, floor in floors.items():
            self.assertGreaterEqual(counts[kind]["mutations"], floor, kind)
        self.assertGreater(counts["a subject swap"]["rule_6"], 0)
        self.assertEqual({k for k in counts if k.startswith("e shift")}, {"e shift %+d" % n for n in (-3, -2, -1, 1, 2, 3)})

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
        ignored = [("a sentence", "alpha and beta (src/cache.py:6)"), ("a heading", "### alpha beta (src/cache.py:6)"),
                   ("a bold line", "**alpha beta** (src/cache.py:6)"), ("no space", "-alpha beta (src/cache.py:6)"),
                   ("a quote", "> alpha beta (src/cache.py:6)")]
        for name, reply in ignored:
            result = self.check(reply)
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"]),
                             (False, ["alpha", "beta"], [], []), name)
            self.assertEqual((rules(result), result["unchecked"]), ({(4, "missing")}, 0), name)  # no claim, no citation, nothing to check
            # the same line with a citation in brackets is not ignored (rule 10): it holds a citation and is no fact line
            result = self.check(reply.replace("(src/cache.py:6)", c))
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"], result["unchecked"]),
                             (False, ["alpha", "beta"], [reply.replace("(src/cache.py:6)", c)], [], 0), name)
            self.assertEqual(rules(result), {(2, "stray_citation"), (4, "missing")}, name)
        # a list item that is no fact line (rule 9) states nothing and fails the reply
        for name, reply in [("a starred bullet", "* alpha beta {}".format(c)), ("a numbered item", "1. alpha beta {}".format(c)),
                            ("an indented bullet", "  - alpha beta {}".format(c)), ("a tab", "-\talpha beta {}".format(c)),
                            ("a plus bullet", "+ alpha beta {}".format(c)), ("a numbered item with a bracket", "2) alpha beta {}".format(c))]:
            result = self.check(reply)
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"]),
                             (False, ["alpha", "beta"], [reply], []), name)
            self.assertEqual((rules(result), result["unchecked"]), ({(2, "not_a_fact_line"), (4, "missing")}, 0), name)
        # the neighbours supply nothing, in either direction
        cases = [("heading above", "### src/cache.py\n- alpha beta", ["- alpha beta"]),
                 ("the line above", "- alpha {}\n- beta".format(c), ["- beta"]),
                 ("the line below", "- alpha\n- beta {}".format(c), ["- alpha"]),
                 ("a wrapped bullet", "- alpha\n  beta {}".format(c), ["- alpha", "  beta " + c]),  # the second line holds a citation (rule 10)
                 ("a lazy continuation", "- alpha\nbeta {}".format(c), ["- alpha", "beta " + c]),
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

    def test_rule_6_a_behaviour_fact_is_met_only_by_a_line_about_its_subject(self):
        put = self.line("def put(self, key, value):")
        answer = dict(self.ANSWER, required=[[r"\bholds?\b"], [r"\bstores?\b"]], support_lines=[{"src/cache.py": ["6"]}, {"src/cache.py": [str(put)]}],
                      behaviour={"0": {"name": "LRUCache", "sig": "src/cache.py:6"}, "1": {"name": "put", "sig": "src/cache.py:{}".format(put)}})
        tail = "\n- put stores a value [src/cache.py:{}]".format(put)
        cases = [  # (name, the line for fact 0, passes, kind of the line's failure)
            ("the subject first", "- LRUCache holds items [src/cache.py:6]", True, None),
            ("the subject in backticks", "- `LRUCache` holds items [src/cache.py:6]", True, None),
            ("a subject in double backticks", "- ``LRUCache`` holds items [src/cache.py:6]", True, None),
            ("a word before it that is no name", "- Here LRUCache holds items [src/cache.py:6]", True, None),
            ("the file's name is not the class", "- cache.py holds items [src/cache.py:6]", False, "wrong_subject"),
            ("another name first", "- put holds items [src/cache.py:6]", False, "wrong_subject"),
            ("a name of the code first, as a plain word", "- The key LRUCache holds items [src/cache.py:6]", False, "wrong_subject"),
            ("no name at all", "- It holds items [src/cache.py:6]", False, "no_subject"),
            ("a name that is not in the code", "- Cache holds items [src/cache.py:6]", False, "no_subject"),
            ("the dotted form takes its first part", "- LRUCache.put holds items [src/cache.py:6]", True, None),
            ("a longer name that starts the same", "- LRUCacheX holds items [src/cache.py:6]", False, "no_subject"),
        ]
        for name, fact_line, ok, kind in cases:
            result = self.check(fact_line + tail, answer)
            self.assertEqual(result["pass"], ok, "{}: {}".format(name, result))
            self.assertEqual(rules(result), set() if ok else {(4, "missing"), (6, kind)}, "{}: {}".format(name, result))
            self.assertEqual(result["missing"], [] if ok else [r"\bholds?\b"], name)
        # a line about the wrong subject is not wrong when it meets another fact: only a line that meets none is named
        both = self.check("- LRUCache holds items [src/cache.py:6]\n- LRUCache stores a value [src/cache.py:{}]".format(put), answer)
        self.assertEqual((both["pass"], both["missing"], rules(both)), (False, [r"\bstores?\b"], {(4, "missing"), (6, "wrong_subject")}))
        # a file's name is a subject, in full or bare, as the chore names it; another file's name is not
        by_file = dict(answer, behaviour={"0": {"name": "cache.py", "sig": "src/cache.py:6"}, "1": {"name": "put", "sig": "src/cache.py:1"}})
        for text, ok in (("- cache.py holds items", True), ("- src/cache.py holds items", True), ("- In cache.py, it holds items", True),
                         ("- jobq.py holds items", False), ("- src/cache.py.bak holds items", False)):
            self.assertEqual(self.check(text + " [src/cache.py:6]" + tail, by_file)["pass"], ok, text)
        names, files = summarize_check.names_in_code(FIXTURES, ["src/cache.py"]), ["src/cache.py"]
        for claim, subject in (("put stores", "put"), ("`put` stores", "put"), ("the put and the get", "put"), ("src/cache.py has put", "cache.py"),
                               ("see cache.py:6", "cache.py"), ("nothing here", None), ("", None), ("a b c", None)):
            self.assertEqual(summarize_check.subject_of(claim, names, files), subject, claim)

    def test_rule_7_order_is_read_on_the_lines_that_met_the_facts(self):
        c = self.C
        ordered = dict(self.ANSWER, order=[0, 1])
        self.assertTrue(self.check("- alpha {c}\n- beta {c}".format(c=c), ordered)["pass"])
        late = self.check("- beta {c}\n- alpha {c}".format(c=c), ordered)
        self.assertEqual((late["pass"], late["order"], rules(late)), (False, ["beta"], {(None, "order")}))
        # a line that states both ordered facts is no line of either (rule 1), whatever follows it
        for reply in ("- alpha and beta {c}\n- beta {c}\n- alpha {c}", "- alpha and beta {c}\n- alpha {c}\n- beta {c}", "- alpha {c}\n- beta {c}\n- alpha and beta {c}"):
            result = self.check(reply.format(c=c), ordered)
            self.assertFalse(result["pass"], reply)
            self.assertIn((1, "several_in_one_line"), rules(result), reply)
            self.assertEqual(result["uncited"], [next(l for l in reply.format(c=c).split("\n") if "and" in l)], reply)
        # where the chore asks for no order such a line is only an extra line, as before
        self.assertTrue(self.check("- alpha and beta {c}\n- beta {c}\n- alpha {c}".format(c=c))["pass"])
        # one of the two ordered facts in a line with an unordered one is fine
        three = dict(ordered, forbidden=[], required=ordered["required"] + [["gamma"]], support_lines=ordered["support_lines"] + [{"src/cache.py": ["1-36"]}])
        self.assertTrue(self.check("- alpha and gamma {c}\n- beta {c}\n- gamma {c}".format(c=c), three)["pass"])

    def test_rule_8_every_code_span_is_removed_as_commonmark_reads_it(self):
        cases = [("a `b` c", "a   c"), ("a ``b`` c", "a   c"), ("a ```b``` c", "a   c"), ("a ` b ` c", "a   c"), ("a `` b ` c `` d", "a   d"),
                 ("a `b c", "a  b c"), ("a ``b` c", "a  b c"), ("a `b`` c", "a  b`` c".replace("``", " ")), ("` ``b`` c", "  c"),
                 ("`a` `b`", "  "), ("```a`b``c```", " "), ("a ```b`` c ```d`` e", "a d e"), ("a ``b ``` c ``` d ``", "a  "),
                 ("no ticks", "no ticks"), ("`", " "), ("``", " "), ("a`b`c", "a c")]
        for text, expected in cases:
            self.assertEqual(" ".join(summarize_check.without_code_spans(text).split()), " ".join(expected.split()), text)
        answer = dict(self.ANSWER, required=[[r"\bholds?\b"], [r"\bbeta\b"]], behaviour={"0": {"name": "LRUCache", "sig": "src/cache.py:6"}})
        tail = "\n- beta {}".format(self.C)
        for ticks in ("`", "``", "```", "````"):  # the word between a run and the next run of the same length is code: it supplies nothing
            for pad in ("", " "):
                result = self.check("- LRUCache {t}{p}holds{p}{t} items [src/cache.py:6]{tail}".format(t=ticks, p=pad, tail=tail), answer)
                self.assertFalse(result["pass"], ticks + pad)
                self.assertIn((4, "missing"), rules(result), ticks + pad)
        for text in ("- LRUCache `holds items [src/cache.py:6]", "- LRUCache ``holds` items [src/cache.py:6]", "- LRUCache holds` items [src/cache.py:6]",
                     "- LRUCache holds ``` items [src/cache.py:6]"):  # no closing run of the same length: the backtick is a character and the words stay
            self.assertTrue(self.check(text + tail, answer)["pass"], text)
        # a stray run does not stop a later span from being removed
        self.assertFalse(self.check("- LRUCache ` ``holds`` items [src/cache.py:6]" + tail, answer)["pass"])
        self.assertFalse(self.check("- LRUCache ``` ``holds`` items [src/cache.py:6]" + tail, answer)["pass"])  # runs of 3, 2 and 2: the 3 is literal
        self.assertTrue(self.check("- LRUCache ```x`` ``holds`` items [src/cache.py:6]" + tail, answer)["pass"])  # the 2 and 2 around x close on each other

    def test_rule_9_every_list_item_is_graded_as_it_is_written(self):
        c = self.C
        base = "- alpha {c}\n- beta {c}".format(c=c)
        self.assertTrue(self.check(base)["pass"])
        cases = [("bare dash", "-", NOT_FACT), ("dash space", "- ", UNCITED), ("dash tab", "-\t", NOT_FACT), ("dash space tab", "- \t", UNCITED),
                 ("dash two spaces", "-  ", UNCITED), ("dash nbsp", "-\u00a0", NOT_FACT), ("dash crlf", "-\r", NOT_FACT), ("indented bare", "  -", NOT_FACT),
                 ("indented", "  - x " + c, NOT_FACT), ("tab indented", "\t- x " + c, NOT_FACT), ("star", "* x " + c, NOT_FACT),
                 ("bare star", "*", NOT_FACT), ("plus", "+ x " + c, NOT_FACT), ("number", "1. x " + c, NOT_FACT), ("number paren", "12) x " + c, NOT_FACT),
                 ("bare number", "3.", NOT_FACT), ("dash with no citation", "- x", UNCITED), ("dash with a Line N", "- x (Line 6)", UNCITED),
                 ("dash with a bad list", "- x [src/cache.py:six]", UNCITED), ("dash tab claim", "-\tx " + c, NOT_FACT)]
        for name, item, expected in cases:
            for where, reply in (("last", base + "\n" + item), ("first", item + "\n" + base), ("between", "- alpha {c}\n{i}\n- beta {c}".format(c=c, i=item))):
                result = self.check(reply)
                self.assertFalse(result["pass"], "{} {}: {!r}".format(name, where, reply))
                self.assertEqual((result["missing"], rules(result)), ([], {expected}), "{} {}".format(name, where))
                self.assertEqual(result["uncited"], [item.rstrip("\r")], "{} {}".format(name, where))
        for name, item in (("a dash with no space", "-x"), ("a rule", "---"), ("bold", "**x**"), ("emphasis", "*x*"), ("a decimal", "1.5 x"),
                           ("a quote", "> x"), ("a heading", "# - x"), ("a year", "2026-11-08"), ("a dash in the text", "see - x"), ("a blank", ""),
                           ("blank spaces", "   "), ("a bullet glued to its text", "\u2022x"), ("a dash glued to a word", "\u2013x"),
                           ("a path with a line, no bracket", "see src/cache.py:6")):
            self.assertTrue(self.check(base + "\n" + item)["pass"], name)  # not list items, no citation in brackets: ignored
        # the failure says the line number and the line
        result = self.check(base + "\n- \t")
        self.assertEqual(result["failures"], [{"rule": 2, "kind": "uncited", "line": 3, "text": "- \t"}])

    def test_rule_10_every_citation_is_checked_wherever_it_is(self):
        c, base = self.C, "- alpha {c}\n- beta {c}".format(c=self.C)
        self.assertTrue(self.check(base)["pass"])
        fake = "[src/nonexistent.rs:999]"
        # (name, the line added to a passing reply, the failure kind of the line itself)
        cases = [("prose", "Also see " + fake, STRAY), ("a sentence", "Note: cli.py creates a JobQueue " + fake, STRAY),
                 ("a heading", "### " + fake, STRAY), ("a quote", "> " + fake, STRAY), ("a quote bullet", "> - x " + fake, NOT_FACT),
                 ("a code span", "see `" + fake + "`", STRAY), ("a code block", "```\n" + fake + "\n```", STRAY),
                 ("a bullet dot", "\u2022 x " + fake, NOT_FACT), ("an en dash bullet", "\u2013 x " + fake, NOT_FACT),
                 ("an ideographic space", "\u3000- x " + fake, NOT_FACT), ("an indented bullet", " - fabricated " + fake, NOT_FACT),
                 ("an em space", "\u2003- fabricated " + fake, NOT_FACT), ("a star", "* x " + fake, NOT_FACT),
                 ("a bold line", "**x** " + fake, STRAY), ("no space after the dash", "-x " + fake, STRAY),
                 ("a mixed list", "see [src/nonexistent.rs:999, foo]", STRAY), ("a second list", "see [foo] and " + fake, STRAY),
                 ("a range past the end", "see [src/cache.py:6-9999]", STRAY), ("an absolute path", "see [/tmp/x.py:3]", STRAY),
                 ("fullwidth digits", "see [src/nonexistent.rs:\uff19\uff19\uff19]", STRAY),
                 ("a huge number", "see [src/cache.py:" + "9" * 5000 + "]", STRAY)]
        for name, extra, (rule, kind) in cases:
            result = self.check(base + "\n" + extra)
            self.assertFalse(result["pass"], name)
            self.assertEqual(len(result["bad_citations"]), 1, "{}: {}".format(name, result))  # counted: the run's fabricated count reads this
            self.assertEqual((result["missing"], result["unchecked"]), ([], 0), name)
            line = extra.split("\n")[1] if extra.startswith("```") else extra
            self.assertIn({"rule": rule, "kind": kind, "line": 3 + (extra.startswith("```")), "text": line}, result["failures"], name)
            self.assertTrue({(3, "unknown_file"), (3, "range")} & rules(result), name)
            self.assertEqual(result["uncited"], [line], name)
        # the exact counts for the two lines of Main's check: one bad citation each, in bad_citations
        for extra, bad in (("Also see " + fake, "src/nonexistent.rs:999 (unknown_file)"), ("see [src/cache.py:6-9999]", "src/cache.py:6-9999 (range)")):
            self.assertEqual(self.check(base + "\n" + extra)["bad_citations"], [bad])
        # a good citation that is not on a fact line is no fact line's: it still fails the reply, and is bad_citations-free
        for extra in ("see " + c, "> " + c, "# " + c, "see `" + c + "`", "1.5 is a number " + c):
            result = self.check(base + "\n" + extra)
            self.assertEqual((result["pass"], result["bad_citations"], result["uncited"], rules(result)), (False, [], [extra], {STRAY}), extra)
        # a fabricated citation is counted once per distinct citation however often it is written
        twice = self.check(base + "\nsee {f}\nsee {f}".format(f=fake))
        self.assertEqual(twice["bad_citations"], ["src/nonexistent.rs:999 (unknown_file)"])
        # on a fact line the citation is the line's own: the claim holds no other citation list, a mixed one included
        for extra in ("- gamma {f} {c}".format(f=fake, c=c), "- gamma [src/nonexistent.rs:999, foo] " + c, "- gamma {c} {c}".format(c=c)):
            result = self.check("- alpha {c}\n- beta {c}\n{e}".format(c=c, e=extra))
            self.assertEqual((result["pass"], result["uncited"], (1, "two_citation_lists") in rules(result)), (False, [extra], True), extra)
        self.assertEqual(self.check("- alpha {c}\n- beta {c}\n- gamma {f} {c}".format(c=c, f=fake))["bad_citations"], ["src/nonexistent.rs:999 (unknown_file)"])
        # a bracket that holds no citation-shaped item is not a citation: ignored on a line that is no list item
        for extra in ("see [note] and [1] and [a b: c]", "[x:y]", "dict[str, int]"):
            self.assertTrue(self.check(base + "\n" + extra)["pass"], extra)

    def test_rule_11_list_items_by_any_whitespace_and_any_bullet(self):
        c = self.C
        base = "- alpha {c}\n- beta {c}".format(c=c)
        self.assertTrue(self.check(base)["pass"])
        prefixes = ["", " ", "  ", "\t", "\u00a0", "\u2003", "\u3000", "\u2009", "\u200b", "\ufeff", "\u202a", "\x00", "\x1b", "\x0b", ">", "> ", ">>", "> >", ">\u00a0", " > ", "\u3000>\u2003"]
        for prefix in prefixes:
            for bullet in BULLET_CHARS + NUMBERED:
                for tail in (" x " + c, " x", "", "\u2003x", "\tx"):
                    item = prefix + bullet + tail
                    if item.startswith("- ") and tail.startswith(" x"):
                        continue  # "- x [cite]" is a fact line; "- x" is rule 9's (uncited)
                    result = self.check(base + "\n" + item)
                    where = repr(item)
                    self.assertFalse(result["pass"], where)
                    self.assertEqual((result["uncited"], result["missing"], result["bad_citations"]), ([item], [], []), where)
                    self.assertEqual(rules(result), {NOT_FACT}, where)
        # "- x" at column 0 is an uncited fact line, not a not_a_fact_line; "- " + a bullet is a fact line whose claim is the rest
        self.assertEqual(rules(self.check(base + "\n- x")), {UNCITED})
        # only "- " + claim + citation list at column 0 is a fact line: a prefix or another bullet in front of the reference never is
        for prefix in ("\u00a0", "\u2003", "\u3000", "\u2009", ">", "> ", ">>", " "):
            result = self.check("\n".join(prefix + t for t in base.split("\n")))
            self.assertEqual((result["pass"], result["missing"], rules(result)), (False, ["alpha", "beta"], {NOT_FACT, (4, "missing")}), repr(prefix))
        for bullet in BULLET_CHARS[1:] + NUMBERED:
            result = self.check("\n".join(bullet + " " + t[2:] for t in base.split("\n")))
            self.assertEqual((result["pass"], result["missing"], rules(result)), (False, ["alpha", "beta"], {NOT_FACT, (4, "missing")}), bullet)
        # not list items (a bullet glued to its text, no marker), so ignored when they hold no bracketed citation
        for item in ("\u2022x", "\u2013x", "\u00b7x", "1.5", "1x", ">x", "> x", "\u200bx", "\u3000x", "a - b", "2026-11-08"):
            self.assertTrue(self.check(base + "\n" + item)["pass"], repr(item))
        # unprefixed and is_list_item agree with str.isspace for every Unicode space
        spaces = [chr(i) for i in range(0x3100) if chr(i).isspace()]  # the last Unicode space is U+3000
        self.assertGreater(len(spaces), 20)
        self.assertFalse(summarize_check.is_list_item("-x"))
        for space in spaces:
            self.assertTrue(summarize_check.is_list_item(space + "- x"), repr(space))
            self.assertTrue(summarize_check.is_list_item("-" + space + "x"), repr(space))

    def test_rule_12_every_line_that_states_an_ordered_fact_counts(self):
        c = self.C
        ordered = dict(self.ANSWER, order=[0, 1])
        ab, ba = ["- alpha " + c, "- beta " + c], ["- beta " + c, "- alpha " + c]
        self.assertTrue(self.check("\n".join(ab), ordered)["pass"])
        cases = [  # (name, the lines, the failures as (rule, kind))
            ("reference + reversed copy", ab + ba, {REPEATED}), ("reversed copy + reference", ba + ab, {REPEATED, ORDER}),
            ("reference + reference", ab + ab, {REPEATED}), ("one step repeated at the end", ab + ab[:1], {REPEATED}),
            ("one step repeated in place", ab[:1] + ab, {REPEATED}), ("the later step twice", ab + ab[1:], {REPEATED}),
            ("reversed once", ba, {ORDER}), ("three times", ab + ab + ab, {REPEATED}),
            ("a restatement with other citations", ab + ["- alpha again [src/cache.py:20]"], {REPEATED}),
            ("a restatement with no valid citation", ab + ["- alpha again [src/cache.py:9999]"], {REPEATED, (3, "range")}),
        ]
        for name, lines, expected in cases:
            result = self.check("\n".join(lines), ordered)
            self.assertFalse(result["pass"], name)
            self.assertEqual(rules(result), expected, "{}: {}".format(name, result["failures"]))
            self.assertEqual((result["missing"], result["unsupported"]), ([], []), name)
        # the failure names the repeated line, and "order" lists the fact
        result = self.check("\n".join(ab + ba), ordered)
        self.assertEqual(sorted((f for f in result["failures"] if f["kind"] == "repeated_step"), key=lambda f: f["line"]),
                         [{"rule": 7, "kind": "repeated_step", "line": 3, "text": ba[0]}, {"rule": 7, "kind": "repeated_step", "line": 4, "text": ba[1]}])
        self.assertEqual(sorted(result["order"]), ["alpha", "beta"])
        # the other lines of the reply do not count: an unrelated line, and an ordered fact's line next to an unordered fact's
        three = dict(ordered, forbidden=[], required=ordered["required"] + [["gamma"]], support_lines=ordered["support_lines"] + [{"src/cache.py": ["1-36"]}])
        self.assertTrue(self.check("- alpha {c}\n- an aside {c}\n- beta {c}\n- gamma {c}\n- gamma again {c}".format(c=c), three)["pass"])
        # where the chore asks for no order a repeated line is allowed, as in round 7
        for lines in (ab + ab, ab + ba, ba + ab):
            self.assertTrue(self.check("\n".join(lines))["pass"], lines)

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
        nested = [line for line in reply.splitlines() if line.startswith("  - ")]  # list items too (rule 9), but not fact lines
        self.assertFalse(result["pass"])
        self.assertEqual(result["missing"], labels_of(answer))
        self.assertEqual(sorted(result["uncited"]), sorted(bullets + nested))
        self.assertEqual((len(bullets), len(nested)), (9, 5))
        self.assertEqual((result["bad_citations"], result["unsupported"], result["missing_citation"], result["unchecked"]), ([], [], [], 0))
        self.assertEqual(rules(result), {(2, "uncited"), (2, "not_a_fact_line"), (4, "missing")})
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
        self.assertIn("Start each fact line with the name it is about", prompt)  # rule 6: the subject comes first
        self.assertEqual(summarize_check.subject_of(example.group(1)[2:], {"load_config"}, ["src/config.py"]), "load_config")
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
        # a fabricated citation on a prose line is counted like one on a fact line (rule 10), and the line is a failure of its own
        self.reply.write_text(self.example() + "\nAlso see [src/nonexistent.rs:999]\n> [src/cache.py:9999]")
        _, records, summary = self.run_main()
        self.assertEqual((records[0]["pass"], records[0]["bad_citations"]), (False, ["src/nonexistent.rs:999 (unknown_file)", "src/cache.py:9999 (range)"]))
        self.assertEqual({(f["rule"], f["kind"]) for f in records[0]["failures"]}, {(3, "unknown_file"), (3, "range"), (2, "stray_citation")})
        self.assertEqual((summary["passed"], summary["fabricated_citations"]), (0, 2))
        self.assertEqual(summary["failure_classes"], {"bad_citations": 1, "uncited": 1})
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
