"""Score one reply to a summarize chore. Pure and deterministic: no model, no network, only reads the fixture files.

    check(reply, answer, root) -> {"pass", "missing", "missing_citation", "unsupported", "order", "forbidden_hit",
                                   "bad_citations", "unchecked", "uncited", "identifier_only", "failures"}

The reply format (round 6; rounds 2 to 5 graded free prose with regexes and found a new prose shape each time). A reply is a list
of FACT LINES, and only fact lines state facts:

    - <claim text> [<path>:<line>[-<line>]][, <path>:<line>...]

  Rule 1  One fact per line. A fact line starts with "- " and ends with ONE bracketed citation list; the claim holds no other
          bracketed citation list, and a line cannot be what two required facts are met by.
  Rule 2  A line that is no list item (a heading, a sentence, a blank line) is ignored for facts: it never supplies a file, a line,
          or a claim, and a line with no bracketed citation in it is ignored altogether. A list item that is not a valid fact line fails
          the reply (rules 9 and 11): no partial credit and no borrowing from the lines around it. A line that is no list item but holds
          a bracketed citation is `stray_citation` (rule 10).
  Rule 3  A citation is valid only when its path is one of the chore's files (answer["cite_files"], written in full as the chore
          names it), its lines are in the file, and it spans at most MAX_RANGE (5) lines: otherwise it is a bad citation
          (`<citation> (unknown_file | range | range_too_wide)`). The citations of the fact line must also cover one of the
          fact's support_lines, or the fact is `unsupported`; a matching fact line with no valid citation is `missing_citation`.
  Rule 4  A required fact is met only by ONE fact line whose claim matches the fact AND whose citations support it, both on the
          same line. No other line, heading or neighbour counts. Facts are met by different lines. A fact no fact line states
          is `missing`; a line that states two facts meets one of them and the other is `missing` (shared_line, rule 1).
  Rule 5  A fact about what code does (answer["behaviour"]) is matched on the claim with every code span, every
          `path:line` and every identifier of the chore's code removed (names_in_code below, computed from the fixture): a claim of
          names and signature text has nothing left to match, however it is cited, so it never meets such a fact (the fact is
          `missing` or `unsupported`, by the other lines). A fact line with no word left once the names, the chore's file names and
          Python keywords are removed (`Job(name, payload, priority=0)`, `class LRUCache:`) that no fact needs is a failed line,
          `identifier_only` (rule 5): it is not a claim.
  Rule 6  Subject. A behaviour fact has a subject, answer["behaviour"][k]["name"] (a symbol or a file name). The claim's subject is
          the FIRST token of the claim (backtick characters removed for this step) that is a name of the chore's code
          (names_in_code) or one of the chore's file names (written in full or bare). The fact is met only by a line whose subject
          is that name: the names are removed before the words are matched (rule 5), so without this a claim about another
          function would meet the fact. No such token: the line has no subject and meets no behaviour fact. The line is then
          `wrong_subject` or `no_subject` (rule 6), and the fact is `missing`.
  Rule 7  Order. In a chore with an order a fact line whose claim matches two or more of the ordered facts is `several_in_one_line`
          (rule 1) and meets none of them. Every other fact line that states an ordered fact counts (rule 12).
  Rule 8  Code spans. Every code span is removed before rule 5, as CommonMark has it: a run of N backticks up to the next run of
          exactly N backticks. A backtick with no closing run is removed as a character (the text after it stays).
  Rule 9  List items. A line is classified as it is written, before anything is stripped. A list item that is not a fact line is
          `not_a_fact_line` (rule 2); a line "- ..." with no valid trailing citation list is `uncited` (rule 2). Rule 11 says what a
          list item is.
  Rule 10 Every citation is checked, wherever it is. Any bracket of the reply with an item of the CITATION shape (path:N or path:N-M)
          is checked by rule 3: on any line, list or not, a heading, a quote, prose, inside or outside a code span or block. A bad
          one is in bad_citations (so in a run's fabricated count). A line that holds a citation and is no valid fact line fails the
          reply: `stray_citation` (rule 2) when it is no list item, and what rules 9 and 11 say when it is one.
  Rule 11 List items by any whitespace and any bullet. A line is unprefixed first: leading whitespace as str.isspace has it (U+00A0,
          U+2003, U+3000 ...), control and format characters (U+0000, U+200B, U+FEFF ...) and any number of blockquote markers ">". It is a list item
          when it then starts with - * + a bullet (U+2022, U+2023, U+25E6, U+2043, U+2013, U+2014, U+00B7) or digits and . or ),
          followed by whitespace (any Unicode space) or the end of the line. Only a line that is exactly "- ", a claim and a citation
          list, at column 0, is a fact line; every other list item is `not_a_fact_line` (rule 2).
  Rule 12 Order counts every line. In a chore with an order, every fact line that states an ordered fact is read in reply order, and
          their ordered-fact indexes must be strictly increasing. A step stated again is `repeated_step` (rule 7), also when the
          first line is not the supported one; a step that comes before one it must follow is `order`. Reference + reversed copy,
          reversed copy + reference and reference + reference all fail. Without an order a repeated line is allowed.

answer (a line of tests/calibration/summarize/answers.jsonl):
  required     list of facts; a fact is a list of alternative regexes for ONE thing (case-insensitive, matched against ONE claim),
               present when any alternative matches. A list of items (steps, states, exports) is one fact per item. A behaviour
               fact is matched on words that say it (returns, stores, evicts ...), never on a name.
  support_lines  one entry per required fact (same index): {"src/x.py": ["14", "20-22"]}, the fixture lines that are evidence of
               that fact: the lines that do it (body, return, raise, a docstring that states it), the signature only when the
               signature alone shows it; for a fact about a name existing, the definition line.
  anchors      one entry per required fact: {"src/x.py": {"14": "return payload", ...}}, a short exact substring of EACH line in
               support_lines, so a fixture edit cannot silently move the evidence (tests/test_qual_summarize.py asserts it; the
               checker does not read it).
  behaviour    optional {"<index into required>": {"name": ..., "sig": "path:line"}} the facts that are about what code does (rules 5
               and 6). name is the subject the claim must start with; sig (the line that declares it) is for the tests.
  order        optional list of indexes into required: facts a chore wants in a given order (states, steps). Every line that states
               one must come after the lines of the one before, and none twice (rule 12); a fact that comes too early or twice is
               listed in "order".
  forbidden    list of regexes for plausible fabrications; none may match the reply (any line of it).
  cite_files   the files (relative to root) a citation may name.

The result names, per failed line, which rule it broke: "failures" is a list of {"rule": 1-7 or None, "kind", "line" (1-based
line of the reply, or None), "text" (the line, or the fact for a fact no line carries)}. kind is one of uncited (2),
not_a_fact_line (2), stray_citation (2), two_citation_lists (1), several_in_one_line (1), unknown_file / range / range_too_wide (3),
missing_citation (3), unsupported (3), missing (4), shared_line (1), identifier_only (5), wrong_subject / no_subject (6),
repeated_step (7), order (rule None). "order" lists the facts of both order kinds.
"uncited" lists the lines of rules 1, 2 and 10. A valid citation of a fact
line that no fact the line states vouches for is `unchecked`: counted, not a failure. The reply passes when nothing is missing,
uncited, identifier_only, missing_citation, unsupported or out of order, no forbidden regex matches and no citation is bad.
"identifier_only" lists the lines of rule 5.
"""
import ast
import functools
import keyword
import re
import unicodedata
from pathlib import Path

FACT_LINE = re.compile(r"^- (?P<claim>.*?\S)[ \t]*\[(?P<cites>[^\[\]]*)\]$")
CITATION = re.compile(r"^(?P<path>[^\s,:\[\]]+):(?P<a>\d+)(?:-(?P<b>\d+))?$")
BRACKETS = re.compile(r"\[[^\[\]]*\]")
BACKTICK_RUN = re.compile(r"`+")
BULLETS = "-*+\u2022\u2023\u25e6\u2043\u2013\u2014\u00b7"  # - * + bullet triangular-bullet white-bullet hyphen-bullet en-dash em-dash middle-dot
LIST_ITEM = re.compile(r"(?:[{}]|\d+[.)])(?:\s|\Z)".format(re.escape(BULLETS)))
PATH_LINE = re.compile(r"(?<![\w./-])[\w./-]+:\d+(?:-\d+)?")
IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
MAX_RANGE = 5
SUPPORT_LINES = re.compile(r"^(\d+)(?:-(\d+))?$")
_LINES, _NAMES = {}, {}


def support_sets(entry):
    """{"src/x.py": ["14", "20-22"]} -> {"src/x.py": {14, 20, 21, 22}}."""
    sets = {}
    for name, specs in entry.items():
        lines = set()
        for spec in specs:
            m = SUPPORT_LINES.match(spec)
            if m is None or int(m.group(2) or m.group(1)) < int(m.group(1)):
                raise ValueError("bad support_lines entry {!r} for {}".format(spec, name))
            lines.update(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
        sets[name] = lines
    return sets


def _read(root, name):
    """The lines of a file under root, or None. Cached by (path, mtime, size), so an edit is seen."""
    path = Path(root) / name
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if key not in _LINES:
            _LINES[key] = path.read_text(encoding="utf-8").splitlines()
        return _LINES[key]
    except (OSError, UnicodeDecodeError):
        return None


def names_in_code(root, files):
    """Every name that appears in the Python files (relative to root) of a chore, computed from them: functions, classes, parameters,
    assigned names and attributes, imports, the names and attributes the code uses (`time.sleep`, `heappush`), keyword arguments,
    and strings that are one identifier (dict keys, handler names). A claim cannot be met by saying them (rule 5)."""
    sources = tuple((name, _read(root, name) if name.endswith(".py") else None) for name in files)
    key = tuple((name, id(lines)) for name, lines in sources)
    if key not in _NAMES:
        found = set()
        for name, lines in sources:
            try:
                tree = ast.parse("\n".join(lines)) if lines else None
            except SyntaxError:
                tree = None
            for node in ast.walk(tree) if tree else ():
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    found.add(node.name)
                elif isinstance(node, ast.arg):
                    found.add(node.arg)
                elif isinstance(node, ast.Name):
                    found.add(node.id)
                elif isinstance(node, ast.Attribute):
                    found.add(node.attr)
                elif isinstance(node, ast.keyword) and node.arg:
                    found.add(node.arg)
                elif isinstance(node, ast.alias):
                    found.update(part for part in (node.name + "." + (node.asname or "")).split(".") if part)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    found.update(node.module.split("."))
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    found.add(node.name)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str) and IDENTIFIER.fullmatch(node.value):
                    found.add(node.value)
        _NAMES[key] = frozenset(found)
    return _NAMES[key]


def without_code_spans(text):
    """text with every code span removed (rule 8), as CommonMark reads them: a run of N backticks up to the next run of exactly N
    backticks is a span, the runs and what they hold go. A run with no such closer is literal: its backticks are removed as
    characters and the text after them stays (and may itself open a span)."""
    runs = [m.span() for m in BACKTICK_RUN.finditer(text)]
    out, at, i = [], 0, 0
    while i < len(runs):
        start, end = runs[i]
        closer = next((j for j in range(i + 1, len(runs)) if runs[j][1] - runs[j][0] == end - start), None)
        out.append(text[at:start])
        if closer is None:
            at, i = end, i + 1
        else:
            at, i = runs[closer][1], closer + 1
        out.append(" ")
    out.append(text[at:])
    return "".join(out)


def without_names(claim, names):
    """The claim with every code span, every path:line and every name in names removed (rule 5)."""
    text = PATH_LINE.sub(" ", without_code_spans(claim))
    return IDENTIFIER.sub(lambda m: " " if m.group(0) in names else m.group(0), text)


def without_files(text, files):
    """text with the chore's file names (written in full, or the bare name) and Python's keywords (def, class, return ...) removed:
    what is left is the words of the claim. A signature is names, file names and keywords."""
    for name in sorted(files, key=len, reverse=True):
        text = text.replace(name, " ").replace(Path(name).name, " ")
    return IDENTIFIER.sub(lambda m: " " if keyword.iskeyword(m.group(0)) else m.group(0), text)


@functools.lru_cache(maxsize=None)
def _token_pattern(files):
    """Scans a claim for its tokens: a file name of the chore (in full or bare) or an identifier."""
    variants = sorted({name for f in files for name in (f, Path(f).name)}, key=len, reverse=True)
    return re.compile(r"(?<![\w./-])(?:{})(?!\w|\.\w)|[A-Za-z_]\w*".format("|".join(re.escape(v) for v in variants)))


def subject_of(claim, names, files):
    """The subject of a claim (rule 6): its first token (backtick characters removed) that is a name of the chore's code or one of
    the chore's file names, a file name as its bare name; None when there is none."""
    bare = {Path(f).name: f for f in files}
    for m in _token_pattern(tuple(files)).finditer(claim.replace("`", " ")):
        token = m.group(0)
        if token in files or token in bare:
            return Path(token).name
        if token in names:
            return token
    return None


def _number(digits):
    """int(digits), capped: a reply cannot crash the scorer with a 5000-digit line number (int() refuses those)."""
    return int(digits) if len(digits) < 18 else 10 ** 18


def _citation(item):
    """(text, path, first, last) when the bracket item has the CITATION shape (path:N or path:N-M), else None."""
    text = item.strip()
    c = CITATION.match(text)
    return None if c is None else (text, c.group("path"), _number(c.group("a")), _number(c.group("b") or c.group("a")))


def citations_in(text):
    """Every citation-shaped item of every bracket of a line, wherever it is on it (rule 10): in the claim, in prose, in a heading, a
    quote, a code span. A bracket that holds one such item among others is read for that item."""
    return [c for bracket in BRACKETS.findall(text) for c in map(_citation, bracket[1:-1].split(",")) if c]


def unprefixed(line):
    """The line without what may precede a list marker (rule 11): every leading character that is whitespace as str.isspace has it
    (U+00A0, U+2003, U+3000 ...) or a control or format character (U+0000, U+001B, U+200B, U+FEFF ...), and any number of blockquote
    markers ">"."""
    i = 0
    while i < len(line) and (line[i] == ">" or line[i].isspace() or unicodedata.category(line[i]) in ("Cc", "Cf")):
        i += 1
    return line[i:]


def is_list_item(line):
    """True for a line whose first characters, once unprefixed, are a bullet (BULLETS) or digits and . or ), then whitespace or the end."""
    return bool(LIST_ITEM.match(unprefixed(line)))


def parse_fact_line(line):
    """(claim, [(text, path, first, last)], None) for a fact line; (None, None, kind) when "- " has no valid trailing citation list
    (uncited, rule 2) or the claim holds a second one (two_citation_lists, rule 1)."""
    m = FACT_LINE.match(line.rstrip())
    cites = []
    for item in (m.group("cites").split(",") if m else ()):
        c = _citation(item)
        if c is None:
            m = None
            break
        cites.append(c)
    if m is None or not cites:
        return None, None, "uncited"
    if citations_in(m.group("claim")):
        return None, None, "two_citation_lists"
    return m.group("claim"), cites, None


def _cite_problem(root, files, path, first, last):
    """None for a good citation, else unknown_file, range or range_too_wide (rule 3)."""
    if path not in files:
        return "unknown_file"
    lines = _read(root, path)
    if lines is None or not 1 <= first <= last <= len(lines):
        return "range"
    return "range_too_wide" if last - first + 1 > MAX_RANGE else None


def check(reply, answer, root):
    required, files = answer["required"], answer["cite_files"]
    support = [support_sets(entry) for entry in answer["support_lines"]]
    if len(support) != len(required):
        raise ValueError("support_lines has {} entries for {} required facts".format(len(support), len(required)))
    behaviour = {int(k): Path(v["name"]).name for k, v in answer.get("behaviour", {}).items()}  # fact -> its subject (rule 6)
    ordered = list(answer.get("order", []))
    names = names_in_code(root, files)
    failures, bad, uncited = [], [], []

    def fail(rule, kind, line, text):
        failures.append({"rule": rule, "kind": kind, "line": line, "text": text})

    def grade(cites, number):
        """Rule 3 for each citation: the good ones. A bad one is counted in bad, and so in a run's fabricated count."""
        good = []
        for text, path, first, last in cites:
            problem = _cite_problem(root, files, path, first, last)
            if problem:
                bad.append("{} ({})".format(text, problem))
                fail(3, problem, number, text)
            else:
                good.append((text, path, first, last))
        return good

    facts = []  # one dict per fact line: n (reply line number), line, claim, stripped (names removed), good (valid citations)
    for number, raw in enumerate(reply.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.startswith("- "):
            claim, cites, kind = parse_fact_line(line)
        else:  # rule 11: a list item by any whitespace and bullet is graded and is no fact line; any other line is ignored
            claim, cites, kind = None, None, "not_a_fact_line" if is_list_item(line) else "ignored"
        if kind:
            stray = citations_in(line)
            grade(stray, number)  # rule 10: a line that is no fact line vouches for nothing, but a bad citation on it is still counted
            if kind == "ignored" and not stray:
                continue
            uncited.append(line)
            fail(1 if kind == "two_citation_lists" else 2, "stray_citation" if kind == "ignored" else kind, number, line)
            continue
        good = grade(cites, number)
        stripped = without_names(claim, names)
        # a claim with no word left once names and the chore's file names are gone says nothing (rule 5)
        bare = not re.search(r"[A-Za-z]{2,}", without_names(without_files(claim, files), names))
        facts.append({"n": number, "line": line, "claim": claim, "stripped": stripped, "good": good, "names_only": bare,
                      "subject": subject_of(claim, names, files)})
    bad = list(dict.fromkeys(bad))

    def says(f, k):
        return any(re.search(a, f["stripped" if k in behaviour else "claim"], re.I) for a in required[k])

    if ordered:  # rule 7: a line that states two of the ordered facts is not the line of either
        for f in facts:
            if sum(says(f, k) for k in ordered) >= 2:
                f["several"] = True
                uncited.append(f["line"])
                fail(1, "several_in_one_line", f["n"], f["line"])
        facts = [f for f in facts if not f.get("several")]

    def covers(fact, k):
        return [c for c in fact["good"] if support[k].get(c[1], set()).intersection(range(c[2], c[3] + 1))]

    missing, missing_citation, unsupported = [], [], []
    ok, vouched, wrong_subject = {}, set(), set()  # wrong_subject: (line number, subject, line)
    stated = {}  # ordered fact -> every fact line that states it (rule 12), in reply order
    for k, alternatives in enumerate(required):
        label = alternatives[0]
        said = [f for f in facts if says(f, k)]
        matches = [f for f in said if f["subject"] == behaviour[k]] if k in behaviour else said
        if k in ordered:
            stated[k] = matches
        if not matches:
            missing.append(label)
            fail(4, "missing", None, label)
            wrong_subject.update((f["n"], f["subject"], f["line"]) for f in said)  # words of this fact, about something else (rule 6)
            continue
        for f in matches:
            vouched.update((f["n"], c) for c in covers(f, k))
        ok[k] = sorted((f for f in matches if covers(f, k)), key=lambda f: f["names_only"])  # a names-only line is the last to be used
        if not ok[k]:
            kind, into = ("unsupported", unsupported) if any(f["good"] for f in matches) else ("missing_citation", missing_citation)
            into.append(label)
            fail(3, kind, matches[0]["n"], label)
    owner = {}  # reply line number -> the fact it meets

    def assign(k, seen):
        for f in ok[k]:
            if f["n"] not in seen:
                seen.add(f["n"])
                if f["n"] not in owner or assign(owner[f["n"]], seen):
                    owner[f["n"]] = k
                    return True
        return False

    for k in ok:
        if ok[k] and not assign(k, set()):
            missing.append(required[k][0])
            fail(1, "shared_line", ok[k][0]["n"], required[k][0])
    for n, subject, line in sorted(wrong_subject, key=lambda x: x[0]):
        if n not in owner:  # a line that meets another fact is not wrong; one that meets none is, for the fact it was taken to say
            fail(6, "wrong_subject" if subject else "no_subject", n, line)
    identifier_only = []  # rule 5: a line of names, signature text and keywords that no fact needed
    for f in facts:
        if f["names_only"] and f["n"] not in owner:
            identifier_only.append(f["line"])
            fail(5, "identifier_only", f["n"], f["line"])
    out_of_order, last_at = [], -1  # rule 12: every line that states an ordered fact, in reply order
    for k in ordered:
        lines = stated.get(k, [])
        for f in lines[1:]:  # a step stated twice, whichever line is the first
            out_of_order.append(required[k][0])
            fail(7, "repeated_step", f["n"], f["line"])
        if lines:
            if lines[0]["n"] < last_at:  # a step that comes before one that must come first
                out_of_order.append(required[k][0])
                fail(None, "order", lines[0]["n"], required[k][0])
            last_at = max(last_at, lines[0]["n"])
    forbidden_hit = [pattern for pattern in answer["forbidden"] if re.search(pattern, reply, re.I)]
    unchecked = sum(1 for f in facts for c in f["good"] if (f["n"], c) not in vouched)
    return {"pass": not (missing or missing_citation or unsupported or out_of_order or forbidden_hit or bad or uncited or identifier_only),
            "missing": missing, "missing_citation": missing_citation, "unsupported": unsupported, "order": out_of_order,
            "forbidden_hit": forbidden_hit, "bad_citations": bad, "unchecked": unchecked, "uncited": uncited, "identifier_only": identifier_only,
            "failures": failures}
