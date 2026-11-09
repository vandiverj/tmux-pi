"""Score one reply to a summarize chore. Pure and deterministic: no model, no network, only reads the fixture files.

    check(reply, answer, root) -> {"pass", "missing", "missing_citation", "unsupported", "order", "forbidden_hit",
                                   "bad_citations", "unparsed_references", "unchecked", "uncited", "identifier_only", "failures"}

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
  Rule 13 A line reference is found in any shape, anywhere in the reply, across line breaks and any Unicode whitespace (\\n, \\r\\n,
          U+2028, U+00A0 ...): PATH:N, PATH:N-M (spaces allowed around ":" and "-"), PATH#LN, PATH#LN-LM, PATH#LN-M, PATH line N,
          PATH, line N, PATH (line N), PATH lines N-M (any case), also inside [], (), backticks, a code block, an HTML comment or
          nested brackets. PATH is a chore file (in full or its bare name) or a token with a "/" or ending in ".<letters>"; inside
          a bracket or backticks it may hold spaces. A version (v1.2.3:4), a time (10:30) or localhost:8080 is no reference; a URL
          port after a dotted host (example.com:8080) is, as is "word.word line N" (see references()).
  Rule 14 Every line reference is graded by rule 3. A bad one is in bad_citations (so in a run's fabricated count) and fails the reply
          with the kind it has (unknown_file, range, range_too_wide). A basename is not a chore file as rule 3 has it (unknown_file).
  Rule 15 Only the canonical form earns credit: the exact trailing "[path:line]" of a fact line. Any other line reference fails the reply,
          `noncanonical_citation` (rule 15), even when its target is good; a good one is not counted as fabricated. The canonical
          bracket on a line that is no fact line stays `stray_citation` (rule 2, from rule 10).
  Rule 16 Numbers by value. Leading zeros (any Unicode decimal zero) are stripped before the cap of NUMBER_CAP (18) significant digits,
          so 000000000000000029 is line 29 at any length of padding, and a number of 18 or more significant digits is capped, not
          converted: it is a line no file has (`range`), and a 5000-digit one cannot make int() refuse it.
  Rule 17 Normalize, then scan. The scan reads a copy of the reply in NFKC form, every Unicode decimal digit its value and every
          isspace character (line breaks too) one space. Credit is judged on the text as written: a fullwidth colon or digit in a
          trailing citation is not the canonical form (`noncanonical_citation`, and the line is `uncited`).
  Rule 18 A path has no character list. Outside a delimiter PATH is a maximal run of non-whitespace characters less trailing sentence
          punctuation; it is a path when it holds "/" or "\\", ends in ".<letters>" or is a chore file's path or bare name. Inside a
          delimiter ([] () <> backticks quotes) PATH is everything from the opener to the line part, whitespace and line breaks
          included; when that is not path-shaped, the token outside the delimiter is used.
  Rule 19 A line part comes in both orders. Forward: a run of separators (": # , (" and "·", whitespace, line breaks), optionally
          L, l., ln, line or lines (any case), then a number or a range. Reverse: line, lines or L + number or range, then of, in or
          at, then PATH.
  Rule 20 No window, linear time. Detection does not depend on the distance between the parts or on the length of the reply (a
          megabyte takes well under 2 s); every scan starts at the start of a separator run and never looks back over a gap.
  Rule 21 Table-free floor. After the canonical citations and the references of rules 17 to 19 are removed, a number (any Unicode
          digits) directly preceded by a path token or a line word (line, lines, L, l., ln, #), with only separators between,
          fails the reply: `unparsed_reference`, listed in "unparsed_references" (S2 needs it, with fabricated citations, at zero).

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

The result names, per failed line, which rule it broke: "failures" is a list of {"rule": 1-16 or None, "kind", "line" (1-based
line of the reply, or None), "text" (the line, or the fact for a fact no line carries)}. kind is one of uncited (2),
not_a_fact_line (2), stray_citation (2), noncanonical_citation (15), two_citation_lists (1), several_in_one_line (1),
unknown_file / range / range_too_wide (3),
missing_citation (3), unsupported (3), missing (4), shared_line (1), identifier_only (5), wrong_subject / no_subject (6),
repeated_step (7), order (rule None). "order" lists the facts of both order kinds.
"uncited" lists the lines of rules 1, 2 and 10. A valid citation of a fact
line that no fact the line states vouches for is `unchecked`: counted, not a failure. The reply passes when nothing is missing,
uncited, identifier_only, missing_citation, unsupported or out of order, no forbidden regex matches and no citation is bad.
"identifier_only" lists the lines of rule 5.
"""
import ast
import bisect
import functools
import keyword
import re
import unicodedata
from pathlib import Path

FACT_LINE = re.compile(r"^- (?P<claim>.*?\S)[ \t]*\[(?P<cites>[^\[\]]*)\]$")
CITATION = re.compile(r"^(?P<path>[^\s,:\[\]]+):(?P<a>[0-9]+)(?:-(?P<b>[0-9]+))?$")  # ASCII digits: a fullwidth one is no canonical citation (rule 17)
BRACKETS = re.compile(r"\[[^\[\]]*\]")
BACKTICK_RUN = re.compile(r"`+")
BULLETS = "-*+•‣◦⁃–—·"  # - * + bullet triangular-bullet white-bullet hyphen-bullet en-dash em-dash middle-dot
LIST_ITEM = re.compile(r"(?:[{}]|\d+[.)])(?:\s|\Z)".format(re.escape(BULLETS)))
PATH_LINE = re.compile(r"(?<![\w./-])[\w./-]+:\d+(?:-\d+)?")
# Rules 18 and 19, on the scan (a reply in NFKC form, every Unicode space one space, every decimal digit ASCII). The separators of a line part and
# the words of a line; the patterns start only where a run of separators starts, so a long run of spaces is read once (rule 20: no window, linear).
_SEP = r"[ :#,(·]"
_LINE_WORD = r"(?:lines?|ln|l)"
_RANGE = r"(?:[ ]*[-‐-―−][ ]*(?:l[ ]*)?(?P<b>\d+))?"
FORWARD = re.compile(r"(?<!{s}){s}+(?:{w}\.?[ :#,(·=]*)?(?P<a>\d+){r}".format(s=_SEP, w=_LINE_WORD, r=_RANGE), re.I)
REVERSE = re.compile(r"(?<!\w){w}\.?[ :#,(·=]*(?P<a>\d+){r}[ ,)\]]*(?:of|in|at)(?!\w)[ ]+".format(w=_LINE_WORD, r=_RANGE), re.I)
NON_SPACE = re.compile(r"\S+")
SLASHES = re.compile(r"[/\\]")
DELIMITERS = re.compile(r"`+|[\[\]()<>\"']")
OPENERS = frozenset("[(<\"'`")
LEADING = frozenset("([{<'\"`*,;")  # what may stand in front of a path outside a delimiter
TRAILING = frozenset(".,;:!?)]}>'\"`*")  # sentence punctuation: not part of a path at its end (rule 18)
# rule 21: a number preceded, with only separators between, by a path token or a line word. A separator is a space or any character that is
# no letter, digit, "_", ".", "/", "\" or "-" (those are what paths and numbers are made of)
FLOOR = re.compile(r"(?<![^\w./\\-])(?P<seps>[^\w./\\-]*)(?P<n>\d+)")
LINE_WORD_END = re.compile(r"(?:^|[^A-Za-z0-9_])(?:lines?|ln|l\.?)\Z", re.I)
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


NUMBER_CAP = 18  # significant digits; a number with this many or more is capped at 10 ** NUMBER_CAP, which no file reaches


def _number(digits):
    """The value of a string of decimal digits (any Unicode decimal digit), rule 16: leading zeros do not count, so 000000000000000029
    is 29. A number of NUMBER_CAP or more significant digits is capped, so a reply cannot make int() refuse a 5000-digit number."""
    zeros = 0
    while zeros < len(digits) and unicodedata.decimal(digits[zeros], -1) == 0:
        zeros += 1
    digits = digits[zeros:]
    if not digits:
        return 0
    return int(digits) if len(digits) < NUMBER_CAP else 10 ** NUMBER_CAP


def _citation(item):
    """(text, path, first, last) when the bracket item has the CITATION shape (path:N or path:N-M), else None."""
    text = item.strip()
    c = CITATION.match(text)
    return None if c is None else (text, c.group("path"), _number(c.group("a")), _number(c.group("b") or c.group("a")))


def citation_items(text):
    """[(citation, offset of its end in text)] for every citation-shaped item of every bracket of a line, wherever it is on it (rule 10):
    in the claim, in prose, in a heading, a quote, a code span. A bracket that holds one such item among others is read for that item."""
    found = []
    for bracket in BRACKETS.finditer(text):
        at = bracket.start() + 1
        for item in bracket.group(0)[1:-1].split(","):
            c = _citation(item)
            if c:
                found.append((c, at + len(item.rstrip())))
            at += len(item) + 1
    return found


def citations_in(text):
    return [c for c, _ in citation_items(text)]


class _Normal(dict):
    """str.translate table for rule 17: each character in NFKC form, every Unicode space (str.isspace) as one space, every Unicode decimal
    digit as the ASCII digit of its value. A control or format character that is no space stays (a path may hold one). `odd` holds the
    characters that give more or fewer than one character, which the offset map must follow."""
    def __init__(self):
        super().__init__()
        self.odd = set()

    def __missing__(self, code):
        out = []
        for char in unicodedata.normalize("NFKC", chr(code)):
            digit = unicodedata.decimal(char, -1)
            out.append(" " if char.isspace() else str(digit) if digit >= 0 else char)
        self[code] = "".join(out)
        if len(self[code]) != 1:
            self.odd.add(code)
        return self[code]


_NORMAL = _Normal()


def _normalize(text):
    """(scan, origin): text in the form rules 17 to 21 read. origin[i] is the index in text of the character scan[i] came from, or None when the
    two have the same length and character for character (almost always: all of ASCII)."""
    scan = text.translate(_NORMAL)
    if len(scan) == len(text) and (text.isascii() or not any(chr(c) in text for c in _NORMAL.odd)):
        return scan, None
    origin = []
    for i, char in enumerate(text):
        origin.extend([i] * len(_NORMAL[ord(char)]))
    return "".join(_NORMAL[ord(char)] for char in text), origin


def _blank(text, spans):
    """text with each (start, end) span replaced by spaces."""
    pieces, at = [], 0
    for start, end in spans:
        pieces.extend((text[at:start], " " * (end - start)))
        at = end
    pieces.append(text[at:])
    return "".join(pieces)


def _delimiters(scan):
    """What rule 18 calls a delimiter: [] () <> backticks (a run of N closes at the next run of N) and quotes. Returns (positions, tops, closer):
    tops[i] is the text start of the innermost delimiter still open after the one at positions[i] (-1 for none), and closer[p] is
    (text start, position of the closer) for the delimiter opened at p. One pass; a closer that matches nothing open is literal."""
    positions, tops, closer, stack, count = [], [], {}, [], {}
    n = len(scan)
    for m in DELIMITERS.finditer(scan):
        p, token = m.start(), m.group()
        char = token[0]
        if char == "`":
            kind = token
        elif char in "[(<":
            if char == "<" and (p + 1 >= n or scan[p + 1] in " =\t"):
                continue  # "a < b", "a <= b"
            kind = char
        elif char in "])>":
            kind = {"]": "[", ")": "(", ">": "<"}[char]
            if not count.get(kind):
                continue
        elif char == '"':
            kind = char
        elif count.get(char):  # "'" closes unless a letter or digit follows it (an apostrophe); it opens only after a non-word character
            if p + 1 < n and scan[p + 1].isalnum():
                continue
            kind = char
        elif p == 0 or not scan[p - 1].isalnum():
            kind = char
        else:
            continue
        if count.get(kind):
            while True:
                entry = stack.pop()
                count[entry[0]] -= 1
                if entry[0] == kind:
                    closer[entry[2]] = (entry[1], p)
                    break
        else:
            stack.append((kind, p + len(token), p))
            count[kind] = count.get(kind, 0) + 1
        positions.append(p)
        tops.append(stack[-1][1] if stack else -1)
    return positions, tops, closer


def _shown(text, start, end):
    """The reference text[start:end] as a failure shows it: white space folded to single spaces, at most about 200 characters."""
    written = " ".join(text[start:end].split())
    return written if len(written) <= 200 else written[:100] + " ... " + written[-95:]


def references(text, files, canonical=()):
    """Rules 17 to 21. Returns (refs, unparsed). Nothing in it depends on a character list for paths or on the distance between the parts of
    a reference; every pass is linear.

    The scan is text in NFKC form (rule 17) with the spans in canonical (a fact line's own citation list) blanked. refs is every line
    reference of it (rules 18 and 19), [(start, end, path, first, last, written)] in text's offsets and in order, written being the
    reference as text has it: a PATH tied to a line number by a separator run from : # , ( · and spaces, then optionally L, l., ln, line
    or lines, then a number or a range, or the reverse (line / lines / L, a number, then of, in or at, then a PATH). Outside a delimiter a
    PATH is the run of non-space characters before the line part (minus sentence punctuation at its end, and a delimiter in front of
    it), and it counts as a path when it holds / or \\, ends in ".<letters>" or is a chore file's path or name. Inside a delimiter it is
    everything from the opening one to the line part, spaces and line breaks included. A chore file, written whole, wins over that.

    unparsed is [(start, end, written)]: every number left once the references are removed that is directly preceded by a path token or
    a line word (line, lines, L, l., ln, #) with only separators between them (rule 21)."""
    scan, origin = _normalize(_blank(text, canonical))
    n = len(scan)
    if not any(char.isdigit() for char in scan):  # a reference needs a number
        return [], []

    def at(i):
        return i if origin is None else origin[i]

    def after(i):
        return i if origin is None else (origin[i - 1] + 1 if i else 0)

    spans = [m.span() for m in NON_SPACE.finditer(scan)]
    run_starts = [a for a, _ in spans]
    run_ends = [b for _, b in spans]
    slashes = [m.start() for m in SLASHES.finditer(scan)]
    names = set(files) | {Path(f).name for f in files}
    longest = max((len(name) for name in names), default=0)
    positions, tops, closer = _delimiters(scan)
    lead_from = {}

    def lead(a, b):
        """The first index in [a, b) that does not hold a LEADING character (b when there is none). Memoized per a."""
        k = lead_from.get(a)
        if k is None:
            k = a
            while k < n and scan[k] in LEADING:
                k += 1
            lead_from[a] = k
        return min(k, b)

    def trail(a, b):
        while b > a and scan[b - 1] in TRAILING:
            b -= 1
        return b

    def shaped(a, b):
        """scan[a:b] is a path (rule 18): it holds / or \\, ends in ".<letters>" or is a chore file's path or name."""
        if a >= b:
            return False
        if bisect.bisect_left(slashes, b) > bisect.bisect_left(slashes, a):
            return True
        j = b
        while j > a and scan[j - 1].isalpha():
            j -= 1
        if a < j < b and scan[j - 1] == ".":
            return True
        return b - a <= longest and scan[a:b] in names

    found = []  # (start, end, path start, path end, delimited, first, last), in scan offsets
    last_end = 0
    for m in FORWARD.finditer(scan):
        s = m.start()
        if s < last_end:
            continue
        head = last_end
        if s and scan[s - 1] != " ":
            head = max(head, run_starts[bisect.bisect_right(run_starts, s - 1) - 1])
        ce = trail(head, s)
        cs = lead(head, ce)
        chosen = None
        if cs < ce and ce - cs <= longest and scan[cs:ce] in files:
            chosen = (cs, ce, False)  # a chore file, written whole
        else:
            k = bisect.bisect_left(positions, s) - 1
            top = tops[k] if k >= 0 else -1
            if top >= 0:  # inside a delimiter: everything from it to the line part
                low = max(top, last_end)
                high = trail(low, s)
                if shaped(low, high):
                    chosen = (low, high, True)
            if chosen is None and shaped(cs, ce):
                chosen = (cs, ce, False)
        if chosen:
            found.append((chosen[0], m.end(), chosen[0], chosen[1], chosen[2], m.group("a"), m.group("b") or m.group("a")))
            last_end = m.end()
    for m in REVERSE.finditer(scan):
        e = m.end()
        if e >= n:
            continue
        chosen = None
        if scan[e] in OPENERS and e in closer:
            low, high = closer[e]
            if shaped(low, trail(low, high)):
                chosen = (low, trail(low, high), True)
        if chosen is None:
            stop = run_ends[bisect.bisect_right(run_starts, e) - 1]
            ce = trail(e, stop)
            cs = lead(e, ce)
            if shaped(cs, ce):
                chosen = (cs, ce, False)
        if chosen:
            found.append((m.start(), chosen[1], chosen[0], chosen[1], chosen[2], m.group("a"), m.group("b") or m.group("a")))
    found.sort(key=lambda r: (r[0], -r[1]))
    kept, end = [], 0
    for ref in found:
        if ref[0] >= end:
            kept.append(ref)
            end = ref[1]
    refs = []
    for start, stop, ps, pe, delimited, a, b in kept:
        written = _shown(text, at(start), after(stop))
        while delimited and ps < pe and scan[ps] in " ,;":
            ps += 1
        refs.append((at(start), after(stop), " ".join(scan[ps:pe].split()), _number(a), _number(b), written))
    return refs, _floor(text, scan, kept, at, after, files)


def _floor(text, scan, kept, at, after, files):
    """Rule 21: [(start, end, written)] for each number of scan, outside the references in kept, that follows a path token or a line word."""
    rest = _blank(scan, [(r[0], r[1]) for r in kept])
    spans = [m.span() for m in NON_SPACE.finditer(rest)]
    run_starts = [a for a, _ in spans]
    out, previous = [], 0
    for m in FLOOR.finditer(rest):
        q, p = m.start(), m.start("n")
        window = rest[max(0, q - 8):q]
        if "#" in m.group("seps") or LINE_WORD_END.search(window):
            word = LINE_WORD_END.search(window)
            start = q - (len(word.group(0)) - (0 if word.group(0)[0] in "lLnN" else 1)) if word else q + len(m.group("seps")) - len(m.group("seps").lstrip())
            out.append((at(start), after(m.end()), _shown(text, at(start), after(m.end()))))
        elif p > q and q:
            k = bisect.bisect_right(run_starts, q - 1) - 1
            if k >= 0 and spans[k][1] >= q:
                token = rest[max(spans[k][0], previous):q]
                core = token.lstrip("".join(LEADING)).rstrip("".join(TRAILING))
                if core and not core.isdigit() and (SLASHES.search(core) or re.search(r"\.[^\W\d_]+\Z", core) or core in files or any(core == Path(f).name for f in files)):
                    start = q - len(token)
                    out.append((at(start), after(m.end()), _shown(text, at(start), after(m.end()))))
        previous = m.end()
    return out


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
    failures, bad, uncited, unread = [], [], [], []

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

    rows, canonical, at = [], [], 0  # rows: (number, line, claim, cites, kind, offset of the line in the reply)
    for number, raw in enumerate(reply.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.startswith("- "):
            claim, cites, kind = parse_fact_line(line)
        else:  # rule 11: a list item by any whitespace and bullet is graded and is no fact line; any other line is ignored
            claim, cites, kind = None, None, "not_a_fact_line" if is_list_item(line) else "ignored"
        if not kind:  # a fact line's own citation list is the one canonical reference (rule 15); it is graded by parse_fact_line's cites
            canonical.append((at + line.rstrip().rindex("["), at + len(line.rstrip())))
        rows.append((number, line, claim, cites, kind, at))
        at += len(raw) + 1
    # rule 13: the exact bracketed citation items of lines that are no fact line are read by rule 10; every other line reference of
    # the reply is noncanonical (rule 15), and every number the grammar cannot place is unparsed (rule 21)
    exact = {row[5] + end for row in rows if row[4] for _, end in citation_items(row[1])}
    line_at = [row[5] for row in rows]
    noncanonical, unparsed = {}, {}  # reply line number -> its references / its unparsed numbers
    found, left = references(reply, files, canonical)
    for ref in found:
        if ref[1] not in exact:
            noncanonical.setdefault(bisect.bisect_right(line_at, ref[0]), []).append(ref)
    for ref in left:
        if ref[1] not in exact:
            unparsed.setdefault(bisect.bisect_right(line_at, ref[0]), []).append(ref)

    facts = []  # one dict per fact line: n (reply line number), line, claim, stripped (names removed), good (valid citations)
    for number, line, claim, cites, kind, _ in rows:
        flagged = False
        if kind:
            stray = citations_in(line)
            grade(stray, number)  # rule 10: a line that is no fact line vouches for nothing, but a bad citation on it is still counted
            if kind != "ignored" or stray:
                flagged = True
                uncited.append(line)
                fail(1 if kind == "two_citation_lists" else 2, "stray_citation" if kind == "ignored" else kind, number, line)
        for start, end, path, first, last, written in noncanonical.get(number, ()):
            grade([(written, path, first, last)], number)  # rule 14: a bad one is counted in bad_citations
            fail(15, "noncanonical_citation", number, written)  # rule 15: and, however good its target, it is no credit
            if not flagged:
                flagged = True
                uncited.append(line)
        for start, end, written in unparsed.get(number, ()):
            unread.append(written)
            fail(21, "unparsed_reference", number, written)  # rule 21: a number the grammar could not place after a path or a line word
            if not flagged:
                flagged = True
                uncited.append(line)
        if kind:
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
    return {"pass": not (missing or missing_citation or unsupported or out_of_order or forbidden_hit or bad or unread or uncited or identifier_only),
            "missing": missing, "missing_citation": missing_citation, "unsupported": unsupported, "order": out_of_order,
            "forbidden_hit": forbidden_hit, "bad_citations": bad, "unparsed_references": unread, "unchecked": unchecked, "uncited": uncited, "identifier_only": identifier_only,
            "failures": failures}
