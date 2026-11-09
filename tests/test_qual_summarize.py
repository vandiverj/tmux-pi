"""The summarize_files qualification kit: tests/calibration/summarize/ (chores, answers, fixtures), tools/qual/summarize_check.py
(the scorer) and tools/qual/summarize_run.py (the runner). No model is run: the worker is a stub script."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import functools
import itertools
import json
import os
import re
import stat
import string
import subprocess
import sys
import tempfile
import time
import unicodedata
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
NONCANON = (15, "noncanonical_citation")
UNPARSED = (21, "unparsed_reference")
# round 9, written here and not read from the checker: the line breaks and spaces rule 13 names, the shapes of a line reference (rule 13) as
# [pieces, default separators between them, takes a range], and what a reference may be wrapped in
BREAKS = ["\n", "\r\n", "\u2028", "\u00a0"]
SHAPES = {
    "path:N": (["{p}", ":", "{a}"], ["", ""], False),
    "path:N-M": (["{p}", ":", "{a}", "-", "{b}"], ["", "", "", ""], True),
    "path : N - M": (["{p}", ":", "{a}", "-", "{b}"], [" ", " ", " ", " "], True),
    "path#LN": (["{p}", "#", "L", "{a}"], ["", "", ""], False),
    "path#LN-LM": (["{p}", "#", "L", "{a}", "-", "L", "{b}"], ["", "", "", "", "", ""], True),
    "path#LN-M": (["{p}", "#", "L", "{a}", "-", "{b}"], ["", "", "", "", ""], True),
    "path line N": (["{p}", "line", "{a}"], [" ", " "], False),
    "path LINE N": (["{p}", "LINE", "{a}"], [" ", " "], False),
    "path, line N": (["{p}", ",", "line", "{a}"], ["", " ", " "], False),
    "path (line N)": (["{p}", "(", "line", "{a}", ")"], [" ", "", " ", ""], False),
    "path lines N-M": (["{p}", "lines", "{a}", "-", "{b}"], [" ", " ", "", ""], True),
}
WRAPPERS = {"none": "See {} for more.", "brackets": "[{}]", "parens": "({})", "backticks": "`{}`", "code block": "```\n{}\n```",
            "html comment": "<!-- {} -->"}
ZEROS = ["0", "０", "٠"]  # ASCII, fullwidth and Arabic-Indic zero: all decimal zeros the scan reads (rule 17); only the first is canonical
PADDINGS = [4, 18, 40, 5000]


def reference_text(shape, p, a, b, at=None, brk=None):
    """A line reference of the shape (SHAPES) to p, lines a and b, with separator number at replaced by brk when at is given."""
    pieces, seps, _ = SHAPES[shape]
    seps = list(seps)
    if at is not None:
        seps[at] = brk
    text = pieces[0]
    for sep, piece in zip(seps, pieces[1:]):
        text += sep + piece
    return text.format(p=p, a=a, b=b)


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
    first_file = parts[0][1].split(",")[0].strip().rsplit(":", 1)[0]
    past = "[{}:{}]".format(first_file, size[first_file] + 50)
    past_range = "[{}:{}-{}]".format(first_file, size[first_file], size[first_file] + 3)  # review 8 F4: a range, not only a line, past the end
    for fab, kind in (("[src/nonexistent.rs:999]", (3, "unknown_file")), (past, (3, "range")), (past_range, (3, "range"))):
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
    # j. rule 13: every shape of a line reference x every wrapper x a break (or no break) at each separator x every target. Rule 15: only the
    # exact bracketed citation on a line of its own is read as a (stray) citation; every other reference is noncanonical, whatever its
    # target. A target that is not in the chore's files or file is a bad citation too (rule 14).
    a = int(parts[0][1].split(",")[0].rsplit(":", 1)[1].partition("-")[0])
    targets = {"valid": (first_file, a, min(a + 1, size[first_file])), "line past end": (first_file, size[first_file] + 7, size[first_file] + 8),
               "range past end": (first_file, size[first_file], size[first_file] + 3), "nonexistent file": ("src/nonexistent.rs", 999, 1000)}
    turn = sum(map(ord, answer["id"]))  # which break a separator gets rotates with the chore, so all four are tried everywhere
    for shape, (pieces, seps, ranged) in SHAPES.items():
        for wrapper, template in WRAPPERS.items():
            for target, (tfile, ta, tb) in targets.items():
                if target == "range past end" and not ranged:
                    continue
                for at in [None] + list(range(len(seps))):
                    turn += 1
                    ref = reference_text(shape, tfile, ta, tb, at, BREAKS[turn % len(BREAKS)])
                    exact = at is None and wrapper == "brackets" and shape in ("path:N", "path:N-M")
                    yield "j " + target, reply(extra=[template.format(ref)]), {STRAY} if exact else {NONCANON}, False
    # k. rule 16: a line number is its value; ASCII leading zeros of any length do not change a reference that passes. Rule 17: other Unicode
    # zeros are read as the same value but earn no credit (the reply is judged as written): the fact line is uncited and noncanonical.
    for zero in ZEROS:
        for n in PADDINGS:
            padded = {i: line(claim, re.sub(r":(\d+)(?:-(\d+))?", lambda m: ":" + zero * n + m.group(1) + (("-" + zero * n + m.group(2)) if m.group(2) else ""), cite))
                      for i, (claim, cite) in enumerate(parts)}
            if zero == "0":
                yield "k padded with %d zeros" % n, reply(padded), set(), True
            else:
                yield "k padded with %d non-ASCII zeros" % n, reply(padded), {NONCANON}, False



# ---- the generated grammar attack (round 10): rules 17 to 21 built from the grammar, not from a list of shapes. Written here, not read from
# the checker. A path holds any printable ASCII punctuation character or a sample of non-ASCII ones (a no-width space, a combining mark, a
# compatibility form that NFKC turns into "/" or into letters, an emoji ...); a line part comes in both orders with every separator and
# every line word, and the parts of a reference sit 0, 1, 4000, 4001 and 100000 characters of spaces or of line breaks apart.
ASCII_PUNCTUATION = string.punctuation  # all 32 printable ASCII punctuation characters
NON_ASCII = ["\u00e9", "\u00df", "\u00f1", "\u4e2d", "\ud55c", "\u044f", "\u05d0", "\u0639", "\u0e01", "\u20ac", "\u2192", "\u2026", "\u200b",
             "\u0301", "\ufb01", "\uff0f", "\u338f", "\u2163", "\U0001f600", "\u00b9"]  # 20: letters of six scripts, symbols, Cf, Mn, NFKC forms
FWD_SEPS = [" ", ":", "#", ",", "(", "\u00b7", ": ", ", ", " (", "\n", ":\n", " \n ", "\u2028", "\u00a0", "\uff1a", "\uff03", "\uff0c", "\u3000"]
LINE_WORDS = ["", "L", "l.", "ln", "line", "lines", "Line", "LINES", "Ln", "l"]
WORD_GAPS = ["", " ", ":", " = ", "#", "\n", "\u00a0"]
RANGE_JOINS = ["-", " - ", "\u2013", "\u2010", "\u2014", "\u2212", "-L", "-l "]
REVERSE_WORDS = ["line", "lines", "L", "l.", "ln", "Line", "LINE"]
REVERSE_TAILS = [" of ", " in ", " at ", ", of ", ") in ", "\nof\n", " OF ", "\u00a0of\u00a0"]
DIGIT_BASES = {"ascii": 0x30, "fullwidth": 0xff10, "arabic-indic": 0x660}
GRAMMAR_WRAPPERS = {"none": "See {} for more.", "brackets": "[{}]", "parens": "({})", "backticks": "`{}`", "double quotes": '"{}"', "single quotes": "'{}'",
                    "angle": "<{}>", "comment": "<!-- {} -->", "link": "[see]({})", "block": "```\n{}\n```", "bold": "**{}**", "prose": "Also {} end",
                    "curly double": "\u201c{}\u201d", "curly single": "\u2018{}\u2019"}
GAPS = [0, 1, 4000, 4001, 100000]
PROBLEMS = {"valid": None, "line past end": "range", "range past end": "range", "nonexistent file": "unknown_file"}


@functools.lru_cache(maxsize=None)
def numeric_characters():
    """Rule 22, written here and not read from the checker: every character the running interpreter's unicodedata gives a numeric value (Nd, Nl and
    No, and the CJK numerals that are Lo), from its own database, so the count differs by Python version."""
    return tuple(chr(code) for code in range(sys.maxunicode + 1) if unicodedata.numeric(chr(code), None) is not None)


def digits_of(n, kind):
    return "".join(chr(DIGIT_BASES[kind] + int(d)) for d in str(n))


def grammar_mutations(answer, turn=None):
    """(kind, reply, problem): every mutation of the reference reply of a chore that appends one line reference built from the grammar. Each
    must fail the reply, for rule 15 (noncanonical_citation; or rule 10 for the exact bracket alone) and for rule 3 as `problem` says, and
    be counted in bad_citations exactly when `problem` is not None."""
    texts = [s["text"] for s in answer["reference"]]
    first = split_line(texts[0])[1].split(",")[0].strip()
    first_file, lines = first.rsplit(":", 1)
    a = int(lines.partition("-")[0])
    size = len(fixture_lines(first_file))
    targets = {"valid": (first_file, a, min(a + 1, size)), "line past end": (first_file, size + 7, size + 8),
               "range past end": (first_file, size, size + 3), "nonexistent file": ("src/nonexistent.rs", 999, 1000)}
    names = list(targets)
    state = [sum(map(ord, answer["id"])) if turn is None else turn]

    def pick(options):
        """Each call moves to the next option of the list: the dimensions not crossed in full rotate, offset by the chore."""
        state[0] += 1
        return options[state[0] % len(options)]

    shift = [sum(map(ord, answer["id"]))]

    def target():
        shift[0] += 1  # a counter of its own: the targets alternate in step with the loops, not with the other dimensions' picks
        name = names[shift[0] % len(names)]
        return name, targets[name]

    def line_part(name, kinds=None):
        """(a, b or None, join, digit kind): a single line or, for a target that needs one, a range."""
        _, ta, tb = targets[name]
        ranged = name == "range past end" or pick([False, True, False])
        return ta, tb if ranged else None, pick(RANGE_JOINS), pick(list(DIGIT_BASES))

    def forward(path, name, sep, word, wgap=None):
        ta, tb, join, dk = line_part(name)
        wgap = pick(WORD_GAPS) if wgap is None else wgap
        return path + sep + word + (wgap if word else "") + digits_of(ta, dk) + ("" if tb is None else join + digits_of(tb, dk))

    def reverse(path, name, word, tail, wgap=None):
        ta, tb, join, dk = line_part(name)
        wgap = pick(WORD_GAPS) if wgap is None else wgap
        return word + wgap + digits_of(ta, dk) + ("" if tb is None else join + digits_of(tb, dk)) + tail + path

    def wrap(text):
        return pick(list(GRAMMAR_WRAPPERS.values())).format(text)

    def made(text, problem, forward=True):
        """(reply, problem): the reply with the reference appended in a wrapper. A wrapper that puts characters in front of the path outside a pair
        of delimiters ("**", or "(" when a "(" inside it takes the ")") makes a path that comes first the path as written, a file no chore has (rule 23):
        unknown_file, whatever the target. In a reverse reference the path comes last, and the wrapper stands in front of the line word."""
        wrapped = wrap(text)
        # "(path(line 6)" has two openers and one closer: the closer takes the inner one, and the outer is no delimiter
        unmatched = wrapped.startswith(("(", "[see](")) and wrapped.count("(") > wrapped.count(")")
        return reply(wrapped), "unknown_file" if forward and (wrapped.startswith("**") or unmatched) else problem

    def reply(extra):
        return "\n".join(texts + [extra])

    # l. a character inside a path: every printable ASCII punctuation character and 20 non-ASCII ones, in a path with a "/" and in one with only
    # an extension, in either order, with a rotating separator, line word and wrapper; the path is no chore file, so the target is a file that is none
    for char in list(ASCII_PUNCTUATION) + NON_ASCII:
        for path in ("src/no{}file", "no{}file.rs"):
            path = path.format(char)
            name = "nonexistent file"
            if pick([True, False]):
                text = forward(path, name, pick(FWD_SEPS), pick(LINE_WORDS))
            else:
                text = reverse(path, name, pick(REVERSE_WORDS), pick(REVERSE_TAILS))
            yield ("l path character",) + made(text, PROBLEMS[name])
    # m. the forward order: every separator x every line word, over the targets
    for sep in FWD_SEPS:
        for word in LINE_WORDS:
            name, (tfile, _, _) = target()
            yield ("m forward",) + made(forward(tfile, name, sep, word), PROBLEMS[name])
    # n. the reverse order: every line word x every tail
    for word in REVERSE_WORDS:
        for tail in REVERSE_TAILS:
            name, (tfile, _, _) = target()
            yield ("n reverse",) + made(reverse(tfile, name, word, tail), PROBLEMS[name], forward=False)
    # p. rule 23 (round 11): a character in front of the path is part of the path as written, so the file is none of the chore's: every printable ASCII
    # punctuation character and the same 20 non-ASCII ones, in front of a chore file or a missing one, in either order, over rotating targets. The
    # reference stands alone in a sentence, with no closing delimiter after it: an opener with no closer is no delimiter.
    for char in list(ASCII_PUNCTUATION) + NON_ASCII:
        name, (tfile, _, _) = target()
        if pick([True, False]):
            text = forward(char + tfile, name, pick(FWD_SEPS), pick(LINE_WORDS))
        else:
            text = reverse(char + tfile, name, pick(REVERSE_WORDS), pick(REVERSE_TAILS))
        yield "p leading character", reply("See " + text + " for more."), "unknown_file"
    # o. the gap: the parts of a reference 0, 1, 4000, 4001 and 100000 characters apart, of spaces and of line breaks. The parts are the path
    # and the colon, the colon and the number, "line N" and "of", "of" and the path, and (in a delimiter) two halves of the path itself.
    # A gap of 100000 line breaks is one form per chore, to keep the test short; every other gap is every form.
    heavy = pick(range(6))
    for fill, label in ((" ", "spaces"), ("\n", "line breaks")):
        for gap in GAPS:
            g = fill * gap
            for form in range(6):
                if form < 2:
                    name, (tfile, ta, tb) = "nonexistent file", targets["nonexistent file"]
                else:
                    name, (tfile, ta, tb) = target()
                    ta = tb if name == "range past end" else ta  # one number: the one past the end
                if gap == 100000 and fill == "\n" and form != heavy:
                    continue
                if form == 0:
                    text = "[src/nonexistent{}file:{}]".format(g, ta)
                elif form == 1:
                    text = "(src/nonexistent{}file line {}-{})".format(g, ta, tb)
                elif form == 2:
                    text = "{}:{}{}".format(tfile, g, ta)
                elif form == 3:
                    text = "{}{}:{}".format(tfile, g, ta)
                elif form == 4:
                    text = "line {} {}of {}".format(ta, g, tfile)  # (a gap of 0 leaves one space)
                else:
                    text = "line {} of {}{}".format(ta, g, tfile)
                yield "o gap %d %s" % (gap, label), reply("See " + text), PROBLEMS[name] if form >= 2 else "unknown_file"


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
            self.assertEqual(rules(result), {(2, "uncited"), UNPARSED}, heading)  # "Line 6" names no file: rule 21 (round 10)
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
        noise = ["# Summary", "", "### src/cache.py", "-no space after the dash", "> quote", "Row six -- this is not a fact line.",
                 "The files hold it: see the files.", "### archive.7z", "Not in the files.", "---", "*emphasis*", "1.5 is a number",
                 "**src/jobq.py**"]
        # a line reference in any shape is no longer ignored (rules 13 and 15): bare, in parentheses, after a file name, ...
        references = ["Line 6 -- this is not a fact line (src/cache.py:6).", "-no space after the dash (src/cache.py:6)", "> quote (src/cache.py:6)",
                      "The files hold it: src/cache.py:6, Lines 1-99.", "*emphasis* (src/cache.py:6)", "1.5 is a number (src/cache.py:6)",
                      "### src/cache.py\nLine 6", "**src/jobq.py**\nLine 6"]
        # with the citation in brackets such a line is no longer ignored (rule 10): it holds a citation and is no fact line
        strays = [line.replace("(src/cache.py:6)", "[src/cache.py:6]") for line in references[:6] if "(src/cache.py:6)" in line and "Line 6" not in line]
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
            for bare in references:  # a valid target earns nothing (rule 15), and counts as no fabrication when it is a chore file
                result = self.check("\n".join(lines + [bare]), chore["id"])
                self.assertFalse(result["pass"], (chore["id"], bare))
                self.assertIn(NONCANON, rules(result), (chore["id"], bare))
                self.assertEqual((result["missing"], result["uncited"]), ([], [bare.split("\n")[0]]), (chore["id"], bare))
                target = "src/cache.py" if "cache" in bare else "src/jobq.py"
                # "**src/jobq.py**": the leading asterisks are part of the path as written (rule 23), a file no chore has
                self.assertEqual(bool(result["bad_citations"]), target not in answer["cite_files"] or bare.startswith("**"), (chore["id"], bare))
            for bare in ("Line 6 -- this is not a fact line.", "The files hold it: see the files, Lines 1-99."):
                result = self.check("\n".join(lines + [bare]), chore["id"])  # rule 21: a line word and a number, with no file, is unparsed
                self.assertEqual((result["pass"], rules(result), result["uncited"], result["bad_citations"]), (False, {UNPARSED}, [bare], []), bare)
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
        # shifted onto another line of the same evidence, a number padded with zeros) must pass: the oracle and the checker agree.
        # Counts per kind are printed when QUAL_ATTACK_COUNTS is set. Kind j (rule 13's shapes) is the next test, for its time.
        counts = self.attack(lambda kind: not kind.startswith("j "))
        self.assertEqual(sum(c["passed"] for c in counts.values()), sum(c["passed"] for k, c in counts.items() if k.startswith(("e shift", "k "))))
        floors = {"a subject swap": 95, "b intro line, rest in order": 2, "b intro line, rest reversed": 2, "c code span of 1": 65, "c code span of 2": 65,
                  "c code span of 3": 65, "c code span of 1 with spaces": 65, "c code span of 2 with spaces": 65, "c code span of 3 with spaces": 65,
                  "c unmatched backtick": 65, "d list item": 24 * 7, "e shift -3": 24, "e shift +3": 24, "e citation on the next line": 24,
                  "e heading file": 24, "e basename": 24, "e generic claim": 24, "e one line": 24, "e drop a line": 100, "e wide range": 24,
                  "e extra non-chore cite": 24, "e star bullets": 24,
                  "f prefix, with a citation": 24 * 7, "f prefix, no citation": 24 * 7, "f prefix on every line": 24 * 7,
                  "g bullet, with a citation": 24 * 12, "g bullet, no citation": 24 * 12, "g bullet, bare": 24 * 12, "g bullet on every line": 24 * 12,
                  "h fabricated citation, prose": 72, "h fabricated citation, heading": 72, "h fabricated citation, quote": 72,
                  "h fabricated citation, code span": 72, "h fabricated citation, code block": 72, "h fabricated citation, sentence": 72,
                  "i reference + reversed": 2, "i reversed + reference": 2, "i reference + reference": 2, "i reversed": 2, "i one step repeated": 11,
                  "i one step repeated in place": 11,
                  "k padded with 4 zeros": 24, "k padded with 18 zeros": 24, "k padded with 40 zeros": 24, "k padded with 5000 zeros": 24,
                  "k padded with 4 non-ASCII zeros": 24 * 2, "k padded with 18 non-ASCII zeros": 24 * 2,
                  "k padded with 40 non-ASCII zeros": 24 * 2, "k padded with 5000 non-ASCII zeros": 24 * 2}
        for kind, floor in floors.items():
            self.assertGreaterEqual(counts[kind]["mutations"], floor, kind)
        self.assertGreater(counts["a subject swap"]["rule_6"], 0)
        self.assertEqual({k for k in counts if k.startswith("e shift")}, {"e shift %+d" % n for n in (-3, -2, -1, 1, 2, 3)})

    def test_generated_attack_every_shape_of_a_line_reference_is_found_and_checked(self):
        # Rules 13 to 15, kind j: 11 shapes x 6 wrappers x (no break, then a break at each separator, rotating through \n, \r\n, U+2028 and
        # U+00A0) x 4 targets (a valid line, a line past the end, a range past the end, a file that is not the chore's) x 24 chores.
        # Each must fail: for rule 15 (or rule 10 for the exact bracket alone on a line), and for rule 3 when the target is bad.
        # Three tests (a valid line, then a line past the end, then the others) to keep each well under 5 s.
        self.assertJ({"j valid": 24 * 11 * 6 * 3})

    def test_generated_attack_every_shape_of_a_line_reference_is_found_and_checked_past_the_end_of_a_file(self):
        self.assertJ({"j line past end": 24 * 11 * 6 * 3})

    def test_generated_attack_every_shape_of_a_line_reference_is_found_and_checked_bad_ranges_and_files(self):
        self.assertJ({"j range past end": 24 * 5 * 6 * 4, "j nonexistent file": 24 * 11 * 6 * 3})

    def assertJ(self, floors):
        counts = self.attack(lambda kind: kind in floors)
        self.assertEqual(sum(c["passed"] for c in counts.values()), 0)
        for kind, floor in floors.items():
            self.assertGreaterEqual(counts[kind]["mutations"], floor, kind)

    def test_generated_grammar_a_character_in_a_path_never_hides_a_reference(self):
        # Rules 17 to 19, kind l: all 32 printable ASCII punctuation characters and 20 non-ASCII ones (no-width space, combining mark, forms NFKC
        # turns into "/" or into letters, an emoji ...) inside a path, once in a path with a "/" and once in one with only an extension, in
        # either order, with a rotating separator, line word, digit set and wrapper, over all 24 chores. The path names no file.
        counts = self.grammar_attack(lambda kind: kind == "l path character")
        self.assertEqual(counts["l path character"]["mutations"], (32 + 20) * 2 * 24)

    def test_generated_grammar_both_orders_with_every_separator_and_line_word(self):
        # Kinds m (forward: 18 separators x 10 line words incl. none) and n (reverse: 7 line words x 8 tails), each over a rotating target
        # (a valid line, a line past the end, a range past the end, a file that is not the chore's), wrapper, range join and digit set
        counts = self.grammar_attack(lambda kind: kind in ("m forward", "n reverse"))
        self.assertEqual((counts["m forward"]["mutations"], counts["n reverse"]["mutations"]), (len(FWD_SEPS) * len(LINE_WORDS) * 24, 56 * 24))
        for kind, problem in (("m forward", "range"), ("n reverse", "range")):
            self.assertGreater(counts[kind]["with " + problem], 0, kind)  # a bad target is among them

    def test_generated_grammar_no_gap_hides_a_reference(self):
        # Rule 20, kind o: the parts of a reference 0, 1, 4000 and 4001 characters apart, of spaces and of line breaks, over all 24 chores,
        # rotating through the four targets
        counts = self.grammar_attack(lambda kind: kind.startswith("o ") and "100000" not in kind)
        expected = {"o gap %d %s" % (gap, label): 24 * 6 for gap in GAPS[:-1] for label in ("spaces", "line breaks")}
        self.assertEqual({kind: count["mutations"] for kind, count in counts.items()}, expected)

    def test_generated_grammar_no_gap_of_a_hundred_thousand_characters_hides_a_reference(self):
        # Rule 20, kind o at 100000 spaces (six forms per chore) and 100000 line breaks (one form per chore, to keep the test short)
        counts = self.grammar_attack(lambda kind: kind.startswith("o ") and "100000" in kind)
        self.assertEqual({kind: count["mutations"] for kind, count in counts.items()},
                         {"o gap 100000 spaces": 24 * 6, "o gap 100000 line breaks": 24})

    def test_generated_grammar_a_character_in_front_of_a_path_is_part_of_the_path(self):
        # Rule 23, kind p: all 32 printable ASCII punctuation characters and the same 20 non-ASCII ones in front of a chore file or a missing one, in
        # either order, over the four targets and all 24 chores: the path is the file as written, so every one is a bad citation (unknown_file)
        counts = self.grammar_attack(lambda kind: kind == "p leading character")
        self.assertEqual(counts["p leading character"]["mutations"], (32 + 20) * 24)
        self.assertEqual(counts["p leading character"]["with unknown_file"], (32 + 20) * 24)

    NUMERIC_FLOOR = {"floor after a line word": "Also line {c}", "floor after a path": "Also src/nonexistent.rs;{c}"}
    NUMERIC_LINE = {"line number": "Also src/nonexistent.rs:{c}", "line number, reverse order": "Also line {c} of src/nonexistent.rs",
                    "end of a range": "Also src/nonexistent.rs:1-{c}"}

    def test_generated_numeric_characters_after_a_line_word_or_a_path_fail_the_floor(self):
        # Rule 22, the floor: every character the running interpreter gives a numeric value (Ethiopic, Roman, circled, fractions, CJK ...) directly after a
        # line word or a path token is a number the grammar did not place: unparsed_reference. The chore of each is rotated through all 24.
        counts = self.numeric_attack(self.NUMERIC_FLOOR, {UNPARSED}, False)
        self.assertEqual(counts, {kind: len(numeric_characters()) for kind in self.NUMERIC_FLOOR})
        self.assertGreater(len(numeric_characters()), 1500)

    def test_generated_numeric_characters_as_a_line_number_fail_the_reply(self):
        # Rule 22, the grammar: the same characters as a line number, in either order and at the end of a range, are read, so the reference fails
        # (noncanonical_citation) and, to a file that is none of the chore's, is a bad citation too
        counts = self.numeric_attack(self.NUMERIC_LINE, {NONCANON, (3, "unknown_file")}, True)
        self.assertEqual(counts, {kind: len(numeric_characters()) for kind in self.NUMERIC_LINE})

    def numeric_attack(self, forms, expected, bad):
        """Every numeric character in every form of forms, appended to the reference of a chore (rotating, so each chore meets each form): the
        reply fails with `expected` among its failures and has bad citations exactly when `bad`. The count of checks per form."""
        counts = dict.fromkeys(forms, 0)
        for i, char in enumerate(numeric_characters()):
            for f, (kind, template) in enumerate(forms.items()):
                chore = self.chores[(i + f) % len(self.chores)]["id"]
                reply = reference(self.answers[chore]) + "\n" + template.format(c=char)
                result = self.check(reply, chore)
                where = "{} / {} / U+{:04X} {}: {!r}".format(chore, kind, ord(char), unicodedata.name(char, "?"), template.format(c=char))
                self.assertFalse(result["pass"], where)
                self.assertTrue(expected <= rules(result), "{} -> {}".format(where, sorted(rules(result), key=str)))
                self.assertEqual(bool(result["bad_citations"]), bad, where + " " + str(result["bad_citations"]))
                counts[kind] += 1
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("numeric characters {} on python {}: {}".format(len(numeric_characters()), sys.version.split()[0], counts), file=sys.stderr)
        return counts

    def grammar_attack(self, select):
        """The grammar mutations of every chore whose kind select() takes, each checked: it fails the reply for rule 15 (or rule 10), has rule 3
        and a bad_citations entry exactly when its target is bad, and never fails for rule 21 alone. The counts per kind."""
        counts = {}
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            for kind, reply, problem in grammar_mutations(answer):
                if not select(kind):
                    continue
                result = self.check(reply, chore["id"])
                count = counts.setdefault(kind, {"mutations": 0, "failed": 0, "with range": 0, "with unknown_file": 0})
                count["mutations"] += 1
                where = "{} / {}: {!r}".format(chore["id"], kind, reply if len(reply) < 600 else reply[:200] + " ... " + reply[-200:])
                found = rules(result)
                self.assertFalse(result["pass"], where)
                self.assertTrue(found & {NONCANON, STRAY}, "{} -> {}".format(where, sorted(found, key=str)))
                self.assertEqual(bool(result["bad_citations"]), problem is not None, where + " " + str(result["bad_citations"]))
                if problem:
                    self.assertIn((3, problem), found, where)
                    count["with " + problem] += 1
                else:
                    self.assertFalse({rule for rule, _ in found} & {3}, where)
                count["failed"] += 1
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            for kind in sorted(counts):
                print("attack {:<28} {}".format(kind, counts[kind]), file=sys.stderr)
        return counts

    def attack(self, select):
        """The mutations (mutations(), above) of every chore whose kind select() takes, each checked; the counts per kind."""
        counts = {}
        for chore in self.chores:
            answer = self.answers[chore["id"]]
            self.assertTrue(self.check(reference(answer), chore["id"])["pass"], chore["id"])
            for kind, reply, any_of, expect_pass in mutations(answer):
                if not select(kind):
                    continue
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
                if kind.startswith("j "):  # rules 14 and 15: a bad target is counted and named; a valid one is not counted as fabricated
                    problem = {"j line past end": "range", "j range past end": "range", "j nonexistent file": "unknown_file"}.get(kind)
                    self.assertEqual(bool(result["bad_citations"]), problem is not None, where + " " + str(result["bad_citations"]))
                    if problem:
                        self.assertIn((3, problem), rules(result), where)
                    else:
                        self.assertFalse({r for r, _ in rules(result)} & {3}, where)
                count["failed"] += 1
                count["rule_6"] += bool(SUBJECT_RULES & rules(result))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            for kind in sorted(counts):
                print("attack {:<28} {}".format(kind, counts[kind]), file=sys.stderr)
        return counts

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
                          "bad_citations": [], "unparsed_references": [], "unchecked": 0, "uncited": [], "identifier_only": [],
                          "failures": []})

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
        ]
        rule = {"uncited": 2, None: None}
        for name, reply, kind in cases:
            result = self.check(reply, self.ONE)
            self.assertEqual(result["pass"], kind is None, "{}: {}".format(name, result))
            self.assertEqual(bool(result["bad_citations"]), kind not in (None, "uncited"), "{}: {}".format(name, result))
            self.assertEqual(bool(result["uncited"]), kind == "uncited", "{}: {}".format(name, result))
            if kind:
                self.assertIn((rule.get(kind, 3), kind), rules(result), "{}: {}".format(name, result))
        # an opener with no closer is no delimiter: it is part of the path as written (rule 23), so "[src/cache.py" is a file no chore has. The line
        # is also uncited (rule 2), because its list is not closed
        result = self.check(good + "[src/cache.py:{}".format(n), self.ONE)
        self.assertEqual((result["pass"], result["bad_citations"], rules(result) >= {UNCITED, (3, "unknown_file")}),
                         (False, ["[src/cache.py:{} (unknown_file)".format(n)], True))
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
        ignored = [("a sentence", "alpha and beta (X)"), ("a heading", "### alpha beta (X)"),
                   ("a bold line", "**alpha beta** (X)"), ("no space", "-alpha beta (X)"),
                   ("a quote", "> alpha beta (X)")]
        for name, reply in ignored:
            result = self.check(reply)
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"]),
                             (False, ["alpha", "beta"], [], []), name)
            self.assertEqual((rules(result), result["unchecked"]), ({(4, "missing")}, 0), name)  # no claim, no citation, nothing to check
            # the same line with a citation in brackets is not ignored (rule 10): it holds a citation and is no fact line
            result = self.check(reply.replace("(X)", c))
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"], result["unchecked"]),
                             (False, ["alpha", "beta"], [reply.replace("(X)", c)], [], 0), name)
            self.assertEqual(rules(result), {(2, "stray_citation"), (4, "missing")}, name)
            # and with the reference in parentheses it is no longer ignored either (rules 13 and 15): a valid target earns nothing
            result = self.check(reply.replace("(X)", "(src/cache.py:6)"))
            self.assertEqual((result["pass"], result["missing"], result["uncited"], result["bad_citations"], result["unchecked"]),
                             (False, ["alpha", "beta"], [reply.replace("(X)", "(src/cache.py:6)")], [], 0), name)
            self.assertEqual(rules(result), {NONCANON, (4, "missing")}, name)
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
                 ("a citation in running text", "- alpha beta\nsee src/cache.py:6", ["- alpha beta", "see src/cache.py:6"]),  # rule 15
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
                 ("bare number", "3.", NOT_FACT), ("dash with no citation", "- x", UNCITED), ("dash with a number", "- x (6)", UNCITED),
                 ("dash with a bad list", "- x [src/cache.py:six]", UNCITED), ("dash tab claim", "-\tx " + c, NOT_FACT)]
        for name, item, expected in cases:
            for where, reply in (("last", base + "\n" + item), ("first", item + "\n" + base), ("between", "- alpha {c}\n{i}\n- beta {c}".format(c=c, i=item))):
                result = self.check(reply)
                self.assertFalse(result["pass"], "{} {}: {!r}".format(name, where, reply))
                self.assertEqual((result["missing"], rules(result)), ([], {expected}), "{} {}".format(name, where))
                self.assertEqual(result["uncited"], [item.rstrip("\r")], "{} {}".format(name, where))
        self.assertEqual(rules(self.check(base + "\n- x (Line 6)")), {UNCITED, UNPARSED})  # a line word and a number: rule 21 as well
        for name, item in (("a dash with no space", "-x"), ("a rule", "---"), ("bold", "**x**"), ("emphasis", "*x*"), ("a decimal", "1.5 x"),
                           ("a quote", "> x"), ("a heading", "# - x"), ("a year", "2026-11-08"), ("a dash in the text", "see - x"), ("a blank", ""),
                           ("blank spaces", "   "), ("a bullet glued to its text", "\u2022x"), ("a dash glued to a word", "\u2013x")):
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
        # fullwidth digits are no canonical citation (rule 17: credit is judged on the text as written) but the scan reads their value
        result = self.check(base + "\nsee [src/nonexistent.rs:\uff19\uff19\uff19]")
        self.assertEqual((result["pass"], result["bad_citations"], rules(result)), (False, ["src/nonexistent.rs:\uff19\uff19\uff19 (unknown_file)"], {(3, "unknown_file"), NONCANON}))
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

    def test_rule_13_a_line_reference_is_found_in_any_shape_anywhere_across_line_breaks(self):
        # review-coder-1e9a5b5bb0 F1 (a bracketed citation split over lines) and Main's F3 shapes: each with a fabricated target fails for
        # rule 15 and rule 3, and is counted in bad_citations (rule 14), however the line break between its parts is written
        c = self.C
        base = "- alpha {c}\n- beta {c}".format(c=c)
        self.assertTrue(self.check(base)["pass"])
        fake, bad = "src/nonexistent.rs", "src/nonexistent.rs"
        for gap in ("\n", "\r\n", "\u2028", "\u00a0", " \n ", "\n\n"):
            for extra in ("Also [{g}{f}:999{g}]", "Also [{f}:{g}999]", "Also [{f}:999{g}]", "Also [{f}{g}:999]", "Also [{f}:999{g}-{g}1000]", "{f}:{g}999",
                          "{f}{g}line{g}999", "{f}{g}#{g}L{g}999", "({f},{g}line 999)", "`{f}{g}:{g}999`", "<!-- {f}:{g}999 -->",
                          "```\n{f}:{g}999\n```", "[[{f}:{g}999]]", "[a [{f}:{g}999] b]", "{f} lines 998{g}-{g}999"):
                extra = extra.format(f=fake, g=gap)
                result = self.check(base + "\n" + extra)
                where = repr(extra)
                self.assertFalse(result["pass"], where)
                self.assertTrue(rules(result) & {NONCANON, STRAY}, (where, result["failures"]))
                self.assertIn((3, "unknown_file"), rules(result), where)
                self.assertEqual(len(result["bad_citations"]), 1, (where, result["bad_citations"]))
                # inside a delimiter the path is everything from the opening one (rule 18), so "<!-- src/x.rs" is the path of the HTML comment
                self.assertTrue(bad in result["bad_citations"][0] and result["bad_citations"][0].endswith("(unknown_file)"), (where, result["bad_citations"]))
                self.assertEqual((result["missing"], result["unsupported"]), ([], []), where)
        for extra in ("See src/nonexistent.rs:999 for more.", "(src/nonexistent.rs:999)", "[src/nonexistent.rs#L999]", "[src/nonexistent.rs line 999]",
                      "`src/nonexistent.rs:999`", "[src/a b.py:3]", "[src/x.py: 3]"):  # Main's F3 list, one by one
            result = self.check(base + "\n" + extra)
            self.assertFalse(result["pass"], extra)
            self.assertEqual(rules(result), {NONCANON, (3, "unknown_file")}, extra)
            self.assertEqual(len(result["bad_citations"]), 1, extra)
        # a line past the end, and a range that runs past it, are "range" (rule 3) too
        for extra, written in (("See src/cache.py:999.", "src/cache.py:999"), ("(src/cache.py#L30-L99)", "src/cache.py#L30-L99"),
                               ("src/cache.py line\n36-37", None), ("src/cache.py:\n36 - 40", None), ("[src/cache.py:36-40]", "src/cache.py:36-40")):
            result = self.check(base + "\n" + extra)
            self.assertIn((3, "range"), rules(result), extra)
            self.assertEqual(len(result["bad_citations"]), 1, extra)
            self.assertTrue(result["bad_citations"][0].endswith("(range)"), extra)

    def test_rule_15_only_the_canonical_form_earns_credit_and_a_valid_other_form_is_not_fabricated(self):
        c = self.C
        base = "- alpha {c}\n- beta {c}".format(c=c)
        for extra in ("See src/cache.py:6 for more.", "(src/cache.py#L6)", "src/cache.py lines 6-8", "[src/cache.py: 6]", "[src/cache.py:6 - 7]",
                      "`src/cache.py:6`", "src/cache.py:\n6", "src/cache.py#L6-L7", "src/cache.py, line 6", "src/cache.py (line 6)", "src/cache.py LINE 6",
                      "<!-- src/cache.py:6 -->", "```\nsrc/cache.py:6\n```", "# src/cache.py:6", "> src/cache.py:6"):
            result = self.check(base + "\n" + extra)
            self.assertFalse(result["pass"], extra)
            self.assertEqual(rules(result), {NONCANON}, extra)
            self.assertEqual(result["bad_citations"], [], extra)  # its target is fine: noncanonical, not fabricated
            self.assertEqual((result["missing"], result["unsupported"]), ([], []), extra)
        # inside a fact line: the canonical list earns its credit, the other reference in the claim is noncanonical and fails the reply
        for line in ("- alpha src/cache.py:6 {c}", "- alpha (src/cache.py#L6) {c}", "- alpha `src/cache.py:6` {c}"):
            result = self.check(line.format(c=c) + "\n- beta " + c)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"], result["missing"]), (False, {NONCANON}, [], []), line)
        # the canonical bracket on a line that is no fact line stays rule 10's stray_citation, not rule 15
        self.assertEqual(rules(self.check(base + "\nSee " + c)), {STRAY})
        # a canonical fact line, on its own, passes: the rule does not touch it; a file named without a number is no reference
        self.assertTrue(self.check(base + "\nSee src/cache.py for more.")["pass"])
        # a line word and a number with no file is rule 21's (round 10), not rule 15's
        for extra in ("(line 6)", "line 6: the first"):
            self.assertEqual(rules(self.check(base + "\n" + extra)), {UNPARSED}, extra)

    def test_rule_13_prose_that_is_not_a_line_reference(self):
        c = self.C
        base = "- alpha {c}\n- beta {c}".format(c=c)
        for prose in ("at 10:30 we start", "version v1.2.3:4", "localhost:8080", "Note: 3 items", "[Time 10:30]", "[note: 3 items]", "ratio 3:2",
                      "key: 5", "the pipeline 3 steps", "2026-11-08", "dict[str, int]", "evicts after 128 entries", "retries 3 times", "version 2.1"):
            result = self.check(base + "\n" + prose)
            self.assertTrue(result["pass"], (prose, result["failures"]))
        # round 10: the grammar has no list of what is prose, so these fail. They are false positives of rules 18 and 21, listed for Main
        # (experiments.md, round 10): a word with "." and letters at its end is a path, and a line word with a number is a line reference
        for prose, expected in (("e.g.: 5 items", {NONCANON, (3, "unknown_file")}), ("a/b 5-6", {NONCANON, (3, "unknown_file")}),
                                ("line 6 of the file", {UNPARSED}), ("see line 6", {UNPARSED}), ("(line 6)", {UNPARSED})):
            self.assertEqual(rules(self.check(base + "\n" + prose)), expected, prose)

    def test_rule_16_a_line_number_is_its_value(self):
        number = summarize_check._number
        self.assertEqual([number(d) for d in ("0", "00", "6", "06", "000000000000000029", "0" * 5000 + "29", "0" * 5000)], [0, 0, 6, 6, 29, 29, 0])
        self.assertEqual([number(d) for d in ("０６", "٠٦", "००१", "٠" * 40 + "7")], [6, 6, 1, 7])
        cap = summarize_check.NUMBER_CAP
        self.assertEqual(number("9" * (cap - 1)), int("9" * (cap - 1)))  # one under the cap: its own value
        self.assertEqual(number("1" * cap), 10 ** cap)  # at the cap and over it: the cap, which no file reaches
        self.assertEqual([number("0" * n + "7" * m) for n in (0, 18, 40, 5000) for m in (cap, cap + 1, 40, 5000)], [10 ** cap] * 16)
        self.assertEqual([number("0" * n + "7") for n in (0, 17, 18, 19, 40, 4299, 4300, 4301, 5000, 100000)], [7] * 10)
        # a canonical citation with padding passes and is the same line, with ASCII zeros at every padding. Fullwidth and Arabic-Indic zeros
        # are not canonical (rule 17: credit is judged on the text as written): the reply fails for rule 15 and the target is not fabricated
        base = "- alpha {}\n- beta {}"
        for zero in ("0", "０", "٠"):
            for n in (1, 4, 17, 18, 19, 40, 4299, 4300, 4301, 5000):
                one = "[src/cache.py:{z}6]".format(z=zero * n)
                two = "[src/cache.py:{z}6-{z}8]".format(z=zero * n)
                result = self.check(base.format(one, two))
                self.assertEqual((result["bad_citations"], result["unsupported"]), ([], []))
                if zero == "0":
                    self.assertTrue(result["pass"], (zero, n, result["failures"]))
                else:
                    self.assertFalse(result["pass"], (zero, n))
                    self.assertIn(NONCANON, rules(result), (zero, n))
        # at the cap and one digit over it, a line number that is no line is "range", with no crash, whatever the zeros in front
        for digits in (cap - 1, cap, cap + 1, 40, 5000):
            for zeros in (0, 18, 40, 5000):
                cite = "src/cache.py:{}{}".format("0" * zeros, "9" * digits)
                result = self.check("- alpha [{}]\n- beta {}".format(cite, self.C))
                self.assertFalse(result["pass"], cite[:40])
                self.assertIn((3, "range"), rules(result), cite[:40])
                self.assertEqual(len(result["bad_citations"]), 1)
                for extra in ("See {}.".format(cite), "[{}-{}]".format(cite, "9" * digits), "src/cache.py line {}{}".format("0" * zeros, "9" * digits)):
                    result = self.check("- alpha {c}\n- beta {c}\n{e}".format(c=self.C, e=extra))
                    self.assertIn((3, "range"), rules(result), extra[:40])

    # ---- round 10: rules 17 to 21 (review-coder-8368348c70-220732 F1, F2, F3 and Main's ls1probe10 and ls1probe11) ----
    def appended(self, extra):
        return self.check("- alpha {c}\n- beta {c}\n{e}".format(c=self.C, e=extra))

    def test_rule_17_the_scan_is_normalized_and_credit_is_judged_as_written(self):
        # Main's F3: a fullwidth colon or digit, and NFKC forms of the separators and the slash. Each is a line reference to a missing file
        # (rule 15 and rule 3, counted as written in bad_citations); a fullwidth digit in a good citation earns no credit but is no fabrication.
        for extra, written in (("src/nonexistent.rs：999", "src/nonexistent.rs：999"), ("src/nonexistent.rs:９９９", "src/nonexistent.rs:９９９"),
                               ("src／nonexistent.rs:999", "src／nonexistent.rs:999"), ("src/nonexistent.rs：٩٩٩", "src/nonexistent.rs：٩٩٩"),
                               ("src/nonexistent.rs　:　999", "src/nonexistent.rs : 999"), ("src/nonexistent.rs＃L999", "src/nonexistent.rs＃L999")):
            result = self.appended(extra)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON, (3, "unknown_file")}, [written + " (unknown_file)"]), extra)
        result = self.appended("See src/cache.py：6 and src/cache.py:６.")
        self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON}, []))
        # a fact line whose citation list uses a fullwidth colon or digit is no fact line: uncited, and its reference is noncanonical
        result = self.check("- alpha [src/cache.py：6]\n- beta " + self.C)
        self.assertEqual((rules(result), result["bad_citations"], result["missing"]), ({UNCITED, NONCANON, (4, "missing")}, [], ["alpha"]))
        # the scan sees through compatibility forms: "ﬁ" is "fi", a no-break space is a space, a zero-width space stays in the path
        self.assertEqual(rules(self.appended("src/nonexistent.rs line 999")), {NONCANON, (3, "unknown_file")})
        result = self.appended("src/non​existent.rs:999")
        self.assertEqual(result["bad_citations"], ["src/non​existent.rs:999 (unknown_file)"])  # one path, not two halves
        self.assertEqual(self.appended("src/cache.py：6")["bad_citations"], [])  # the path survives NFKC: a good target is not fabricated

    def test_rule_18_a_path_has_no_character_list(self):
        # F1: a path character the round-9 list left out ($ ?), then every printable ASCII punctuation character, outside delimiters
        for extra in ("src/no$file:999", "src/no?file:999", "src/no$file#L999", "src/no%file:999", "src/no~file:999"):
            result = self.appended("Also " + extra)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON, (3, "unknown_file")}, [extra + " (unknown_file)"]), extra)
        for char in string.punctuation:
            result = self.appended("Also src/no{}file:999".format(char))
            self.assertIn((3, "unknown_file"), rules(result), char)
            self.assertEqual(len(result["bad_citations"]), 1, char)
        # inside a delimiter a path holds anything, spaces and line breaks too, up to the line part (F2: no window)
        for fill in (" ", "\n", "\t", " "):
            for gap in (0, 1, 4000, 4001, 100000):
                for body in ("[src/nonexistent{g}file line 999]", "(src/nonexistent{g}file:999)", "`src/nonexistent{g}file#L999`", "[src/a{g}b.rs, line 999]"):
                    result = self.appended(body.format(g=fill * gap))
                    self.assertEqual((result["pass"], (3, "unknown_file") in rules(result), len(result["bad_citations"])), (False, True, 1), (body, fill, gap))
        # a chore file written whole wins over a delimiter that holds more: "(see src/cache.py:6)" is a good target, noncanonical only
        for extra in ("(see src/cache.py:6)", "[see src/cache.py line 6]", "\"src/cache.py:6\""):
            result = self.appended(extra)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON}, []), extra)
        # but inside a delimiter that is no part of the file name the path runs from the delimiter (rule 18): a listed false positive
        result = self.appended("<a href=src/cache.py#L6>")
        self.assertEqual((rules(result), result["bad_citations"]), ({NONCANON, (3, "unknown_file")}, ["a href=src/cache.py#L6 (unknown_file)"]))

    def test_rule_19_a_line_part_comes_in_both_orders_with_any_separator_and_line_word(self):
        # F3: the reverse order and the "L" forms
        for extra, problem in (("line 999 of src/nonexistent.rs", "unknown_file"), ("src/nonexistent.rs L999", "unknown_file"), ("src/cli.py:L9999", "unknown_file"),
                               ("lines 998-999 in src/nonexistent.rs", "unknown_file"), ("L999 at src/nonexistent.rs", "unknown_file"),
                               ("line 99\nof src/nonexistent.rs", "unknown_file"), ("ln 9999 of src/cache.py", "range"), ("Lines 6-9999 IN src/cache.py", "range"),
                               ("src/cache.py·ln 9999", "range"), ("src/cache.py (l.9999)", "range"), ("src/cache.py,\nLINES 6 - 9999", "range")):
            result = self.appended(extra)
            self.assertEqual((result["pass"], (3, problem) in rules(result), len(result["bad_citations"]), NONCANON in rules(result)), (False, True, 1, True), extra)
        for extra in ("line 6 of src/cache.py", "lines 6-8 in src/cache.py", "L6 at src/cache.py", "src/cache.py L6", "src/cache.py:L6-L8", "src/cache.py · 6"):
            result = self.appended(extra)
            self.assertEqual((rules(result), result["bad_citations"]), ({NONCANON}, []), extra)  # a good target: noncanonical, not fabricated
        self.assertEqual(rules(self.appended("line 6 of the file")), {UNPARSED})  # no path: rule 21

    def test_rule_20_no_window_and_linear_time(self):
        # the parts of a reference 0, 1, 4000, 4001 and 100000 characters apart, of spaces and of line breaks
        for fill in (" ", "\n"):
            for gap in (0, 1, 4000, 4001, 100000):
                g = fill * gap
                for extra in ("src/cache.py:{g}9999", "src/cache.py{g}:9999", "line 9999 {g}of src/cache.py", "line 9999 of {g}src/cache.py"):
                    result = self.appended(extra.format(g=g))
                    self.assertEqual((result["pass"], (3, "range") in rules(result), len(result["bad_citations"])), (False, True, 1), (extra, fill, gap))
        self.megabyte({"words": "The queue evicts after 128 entries and retries 3 times. ", "line breaks": "\n", "open brackets": "[",
                       "dense references": "src/cache.py:6 ", "line of": "line 6 of src/cache.py ", "floor": "src/a.py;7 ", "compatibility forms": "ﬁ "})

    def test_rule_20_a_megabyte_of_numbers_is_read_in_linear_time(self):
        # the shapes with the most numbers (one for each two characters): the floor reads each, ASCII and fullwidth
        self.megabyte({"numbers": "7 ", "unicode digits": "９ "})

    def megabyte(self, unit):
        """A 1 MB reply is read in about a second, whatever its shape (the limit is 2 s on py3.9). Each shape has a reference to a missing file at
        its end, which is found whatever comes before it."""
        for name, piece in unit.items():
            reply = "- alpha {c}\n- beta {c}\n{body}\nsrc/nonexistent.rs:999".format(c=self.C, body=piece * (1000000 // len(piece)))
            start = time.monotonic()
            result = self.check(reply)
            self.assertLess(time.monotonic() - start, 2, name)
            self.assertIn((3, "unknown_file"), rules(result), name)

    def test_rule_21_a_number_after_a_path_or_a_line_word_that_no_grammar_placed_fails(self):
        # (text, what the floor reports). The reference grammar places "path:N", "path line N" ...; what is left is the floor's
        floor = [("src/cache.py;7", "src/cache.py;7"), ("the file src/cache.py, then 7", None), ("line 6", "line 6"), ("see lines 4-9", "lines 4"),
                 ("Ln 6", "Ln 6"), ("L6", "L6"), ("l. 6", "l. 6"), ("#12", "#12"), ("issue #12 is fixed", "#12"), ("src/nonexistent.rs at line 999", "line 999"),
                 ("(line ９)", "line ９"), ("LINE\n6", "LINE 6"), ("cache.py.7", None), ("see src/cache.py；7", "src/cache.py；7")]
        for text, flagged in floor:
            result = self.appended(text)
            if flagged is None:
                self.assertTrue(result["pass"], (text, result["failures"]))
                continue
            self.assertEqual((result["pass"], rules(result), result["unparsed_references"], result["bad_citations"]), (False, {UNPARSED}, [flagged], []), text)
            self.assertEqual(result["uncited"], [text.split("\n")[0]], text)
        # prose numbers are no references (listed false positives of the floor are in test_rule_13_prose_that_is_not_a_line_reference)
        for text in ("evicts after 128 entries", "retries 3 times", "version 2.1", "at 10:30", "3 of 4 steps", "in 2026", "a file of 7 lines", "Python 3.9", "key: 5"):
            result = self.appended(text)
            self.assertTrue(result["pass"], (text, result["failures"]))
        # a canonical fact line's own citation list is removed before the floor reads the reply; its claim is not
        self.assertEqual(self.check("- alpha {c}\n- beta line 6 {c}".format(c=self.C))["unparsed_references"], ["line 6"])
        self.assertEqual(self.check("- alpha {c}\n- beta {c}".format(c=self.C))["unparsed_references"], [])

    # ---- round 11: rules 22 and 23 (review-coder-71799ae16a-230447 F1 and F2, Main's ls1probe13) ----
    def test_rule_22_a_digit_is_any_character_with_a_numeric_value(self):
        # F1: Ethiopic digits and Roman numerals are not \d. After a line word they are an unparsed reference (rule 21); as a line number the
        # reference is read, fails as noncanonical, and a missing file is a bad citation (rule 3)
        for text in ("line \u1369", "line \u2160", "Lines \u2460", "L\u00bd", "ln \u00b2", "#\u4e00", "line \u3007"):
            result = self.appended("Also " + text)
            self.assertEqual((result["pass"], rules(result), result["unparsed_references"], result["bad_citations"]), (False, {UNPARSED}, [text], []), text)
        for text in ("src/nonexistent.rs:\u1369", "src/nonexistent.rs line \u2160", "line \u2160 of src/nonexistent.rs", "src/nonexistent.rs:1-\u2467",
                     "src/nonexistent.rs:\u00bd", "src/nonexistent.rs#L\u2462"):
            result = self.appended("Also " + text)
            self.assertEqual((result["pass"], rules(result), len(result["bad_citations"])), (False, {NONCANON, (3, "unknown_file")}, 1), text)
        # a numeric character read by value: a good line earns no credit (noncanonical) and is no fabrication; a line past the end is a bad range
        for text, problem in (("src/cache.py:\u136e", None), ("src/cache.py:\u216b", None), ("src/cache.py:6-\u2467", None), ("src/cache.py:\u00b2",  None),
                              ("src/cache.py:\u137b", "range"), ("src/cache.py:\u0f33", "range"), ("src/cache.py:\u2467\u2467", "range")):
            result = self.appended(text)
            self.assertEqual((result["pass"], rules(result), len(result["bad_citations"])),
                             (False, {NONCANON} | ({(3, problem)} if problem else set()), 1 if problem else 0), text)
        # a canonical fact line needs ASCII digits: the same line with another numeric character is uncited and its reference noncanonical
        result = self.check("- alpha [src/cache.py:\u136e]\n- beta " + self.C)
        self.assertEqual((rules(result), result["bad_citations"], result["missing"]), ({UNCITED, NONCANON, (4, "missing")}, [], ["alpha"]))
        # a number made of Nd digits only is read by value as before (rule 16), and prose numbers are no references
        self.assertEqual(rules(self.appended("src/cache.py:\uff10\u0666")), {NONCANON})
        for text in ("evicts after \u2460\u2461 entries", "retries \u00bd of the time", "version \u2160", "the \u4e09 steps"):
            self.assertTrue(self.appended(text)["pass"], text)

    def test_rule_23_delimiters_are_matched_pairs_and_any_other_leading_character_is_part_of_the_path(self):
        # F2: "{" is no delimiter. Every one of these is a reference to the file as written: unknown_file, counted in bad_citations, and noncanonical
        for lead in ("{", "|", "x{", "=", "@", "~", "*", "**", ",", ";", ":", "#", "-", ".", "\\", "\u00ab", "\u201e", "\u200b", "\u0301", "\u4e2d"):
            for text in (lead + "src/cache.py:6", lead + "src/cache.py line 6", "line 6 of " + lead + "src/cache.py"):
                result = self.appended("Also " + text)
                expected = (lead + "src/cache.py").strip()
                self.assertEqual((result["pass"], rules(result), len(result["bad_citations"])), (False, {NONCANON, (3, "unknown_file")}, 1), text)
                self.assertIn(expected, result["bad_citations"][0], text)
        # an opener with no closer is part of the path as written ...
        for text in ("(src/cache.py:6", "[src/cache.py line 6", "<src/cache.py:6", "'src/cache.py:6", '"src/cache.py:6', "`src/cache.py:6", "\u201csrc/cache.py:6",
                     "\u2018src/cache.py:6", "((src/cache.py:6", "src/cache.py:6)(src/cache.py:6"):
            result = self.appended("Also " + text)
            self.assertEqual((result["pass"], rules(result), len(result["bad_citations"])), (False, {NONCANON, (3, "unknown_file")}, 1), text)
        # ... a pair is a delimiter: the path inside it is the file, whatever stands in front of the opener
        for text in ("(src/cache.py:6)", "<src/cache.py:6>", "'src/cache.py:6'", '"src/cache.py:6"', "`src/cache.py:6`", "\u201csrc/cache.py:6\u201d",
                     "\u2018src/cache.py:6\u2019", "((src/cache.py:6))", "((src/cache.py:6)", "{`src/cache.py:6`}", "*(src/cache.py:6)*", "x(src/cache.py line 6)",
                     "line 6 of \u201csrc/cache.py\u201d", "line 6 of (src/cache.py)", "[see](src/cache.py:6)", "don\u2019t src/cache.py:6", "src/cache.py:6\u2019s"):
            result = self.appended("Also " + text)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON}, []), text)
        # references written one after another without a space: the comma or semicolon ends the first, it does not start the second path
        for text in ("src/cache.py:6,src/cache.py:7", "src/cache.py:6;src/cache.py:7", "src/cache.py:6,src/cache.py:7,src/cache.py:8"):
            result = self.appended("Also " + text)
            self.assertEqual((result["pass"], rules(result), result["bad_citations"]), (False, {NONCANON}, []), text)
        result = self.appended("Also src/cache.py:6,src/nonexistent.rs:7")
        self.assertEqual(result["bad_citations"], ["src/nonexistent.rs:7 (unknown_file)"])

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
            ("a bare reference earns nothing and fails the reply (rule 15)", "- alpha [src/cache.py:6]\nsee src/cache.py:20", 0, False),
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
            self.assertEqual(rules(result), set() if ok else {(4, "missing"), (5, "identifier_only")} | ({NONCANON} if "src/cache.py:6 [" in fact_line else set()),
                             "{}: {}".format(name, result))
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
        # the "Line N" under a heading names no file: rule 21 reads it as a line reference the grammar could not place
        self.assertEqual(rules(result), {(2, "uncited"), (2, "not_a_fact_line"), (4, "missing"), (21, "unparsed_reference")})
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
        # rules 13-15 (round 9): the prompt says a file and line go only in the trailing [path:line] of a fact line, and its example, with a
        # real file and line standing in for the placeholder, is a reply the checker accepts whole (the example's own path:line is canonical)
        self.assertIn("only as the trailing [path:line] of a fact line, never in any other shape or place", prompt)
        self.assertIn("fails the whole reply", prompt)
        ex = {"required": [["returns the defaults"]], "forbidden": [], "cite_files": ["src/config.py"], "support_lines": [{"src/config.py": ["41-43"]}]}
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "src").mkdir()
            (Path(root) / "src/config.py").write_text("\n".join("line %d" % n for n in range(1, 60)))
            result = summarize_check.check(example.group(1), ex, Path(root))
        self.assertTrue(result["pass"], result["failures"])
        self.assertEqual((result["failures"], result["bad_citations"], result["uncited"]), ([], [], []))


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
        self.assertEqual((summary["unchecked_citations"], summary["unparsed_references"]), (0, 0))
        self.assertEqual(summary["s2_needs_zero"], ["fabricated_citations", "unparsed_references"])  # round 10: S2 needs both counts at 0
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
        # rule 14: a fabricated reference in any other shape is counted too: split over lines, and a bare path:N; a valid one is not counted
        for extra, bad in (("Also [\nsrc/nonexistent.rs:999\n]", ["src/nonexistent.rs:999 (unknown_file)"]),
                           ("Also [src/nonexistent.rs:\n999]", ["src/nonexistent.rs: 999 (unknown_file)"]),
                           ("See src/nonexistent.rs:999 for more.", ["src/nonexistent.rs:999 (unknown_file)"]),
                           ("src/nonexistent.rs line 999", ["src/nonexistent.rs line 999 (unknown_file)"]),
                           ("(src/cache.py#L9999)", ["src/cache.py#L9999 (range)"]),
                           ("See src/cache.py:6 for more.", [])):
            self.reply.write_text(self.example() + "\n" + extra)
            _, records, summary = self.run_main()
            self.assertEqual((records[0]["pass"], records[0]["bad_citations"]), (False, bad), extra)
            self.assertEqual((summary["passed"], summary["fabricated_citations"]), (0, len(bad)), extra)
            self.assertIn((15, "noncanonical_citation"), {(f["rule"], f["kind"]) for f in records[0]["failures"]}, extra)
        # rule 21: a number after a line word or a file that no grammar placed is no fabricated citation but its own count, and a failure class
        self.reply.write_text(self.example() + "\nAlso see line 6 of the file.\nAnd src/cache.py;7 too.")
        _, records, summary = self.run_main()
        self.assertEqual((records[0]["pass"], records[0]["bad_citations"], records[0]["unparsed_references"]), (False, [], ["line 6", "src/cache.py;7"]))
        self.assertEqual({(f["rule"], f["kind"]) for f in records[0]["failures"]}, {(21, "unparsed_reference")})
        self.assertEqual((summary["passed"], summary["fabricated_citations"], summary["unparsed_references"]), (0, 0, 2))
        self.assertEqual(summary["failure_classes"], {"unparsed_references": 1, "uncited": 1})
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
