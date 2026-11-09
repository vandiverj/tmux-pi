"""The path oracle of the summarize_files checker (rules 10, 24, 25 and 26), split from tests/test_qual_summarize.py so that each file runs in
well under 60 s. No model is run. The checker's reported path is compared with the path a plain model of the rules reads from the text a
generator wrote. The model is written here, not read from the checker: its own pair matcher, its own list of trailing punctuation, its own idea of
an exact [path:N] item. The helpers of the data (jsonl, split_line, the character lists, the failure kinds) come from test_qual_summarize."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_qual_summarize as base  # noqa: E402  (names only: no test class is imported, so no test runs twice)

summarize_check = base.summarize_check
NONCANON, UNPARSED, UNCITED, STRAY = base.NONCANON, base.UNPARSED, base.UNCITED, base.STRAY
UNKNOWN = (3, "unknown_file")
WATCHED = {NONCANON, UNPARSED, UNCITED, UNKNOWN}

# For every delimiter (each bracket pair, backticks, each quote pair, none), every printable ASCII punctuation character and the 20 non-ASCII ones
# placed first, last and inside a chore's cited file (in its directory and in its name), a letter, a space or nothing just before the opener, both
# orders of rule 19, over every chore of every split ("characters"); between the file and its line number, every separator that can sit just before the colon
# ("separators"); and (round 13, rule 27) extra path text in front of the file, inside every delimiter and with none ("prefixes"): a word, a path
# fragment or a "." and one of five white spaces between it and the file, and nothing, "." or "," just before ":N". Each case is a line of prose and,
# in the fact-line form, a fact line "- ok [..]" that the checker may or may not accept.
ORACLE_DELIMITERS = {"none": ("", ""), "brackets": ("[", "]"), "parens": ("(", ")"), "angle": ("<", ">"), "backticks": ("`", "`"),
                     "double quotes": ('"', '"'), "single quotes": ("'", "'"), "curly double": ("“", "”"), "curly single": ("‘", "’")}
ORACLE_PLACES = ("first", "last", "directory", "name")
ORACLE_BEFORE = ("letter", "space", "nothing")
ORACLE_BETWEEN = ["", " ", "  ", " ", "\t", "#", "(", "·", ",", ":", "#:", " #", "# ", ": ", ",,"]  # what may stand between the path and ":N"
ORACLE_TRAILING = ".,;:!?)]}>'\"`*”’"  # rule 18, written out again
ORACLE_PREFIXES = ("junk", "a/", ".")  # extra path text in front of the file (rule 27)
ORACLE_GAPS = (" ", "  ", "\t", "\n", "\u00a0")  # what stands between the extra text and the file: five kinds of white space
ORACLE_LEADINS = ("see", "see also", "a/")  # a lead-in in front of the file, after which ONE list separator may stand (rule 29): a word, two words, a path fragment
ORACLE_LEAD_SEPARATORS = (",", ";", ",,", "")  # the list separator after the lead-in: one, the other, a doubled one (malformed: no split), none
ORACLE_LEAD_WHITE = ("", " ", "\n")  # what stands after that separator before the file
ORACLE_BARE_ENDINGS = ("", ".", ",", ";")  # a bare file name (no slash), and with sentence punctuation at its end (rule 30)
ORACLE_SUFFIXES = ("", ".", ",")  # what stands just before ":N" (or, in the reverse order, just before the closer)
ORACLE_SEPARATORS = " :#,(·"  # what rule 19 allows between a path and its line number: a separator at the end of the path is one of them
ORACLE_CLOSES = {"]": "[", ")": "(", ">": "<", "”": "“", "’": "‘"}
ORACLE_ITEM = re.compile(r"[^\s,:\[\]]+:[0-9]+(?:-[0-9]+)?\Z")
ORACLE_TAIL = re.compile(r"\[([^\[\]]*)\]\Z")
ORACLE_FULL_EVERY = 32  # every 32nd case is also checked inside the whole reference reply (about 6x the cost of the line alone)
CHARACTERS = list(base.ASCII_PUNCTUATION) + base.NON_ASCII


def oracle_pairs(w):
    """{opener index: (index just after the opener, closer index)}: the matched pairs of w by a plain stack (rules 23 and 25). A bracket or a backtick
    run (N of them close at the next N) is a delimiter wherever it stands; a quote opens only at the start of w or after white space or an opening
    bracket, and closes only before the end, white space or rule-18 trailing punctuation; any other quote is an apostrophe."""
    pairs, stack, i = {}, [], 0

    def close(kind, at):
        while True:
            top, start, opener = stack.pop()
            if top == kind:
                pairs[opener] = (start, at)
                return

    while i < len(w):
        ch = w[i]
        if ch == "`":
            j = i
            while j < len(w) and w[j] == "`":
                j += 1
            if any(k == w[i:j] for k, _, _ in stack):
                close(w[i:j], i)
            else:
                stack.append((w[i:j], j, i))
            i = j
            continue
        if ch in "\"'“”‘’":
            kind = ORACLE_CLOSES.get(ch, ch)
            ends = i + 1 == len(w) or w[i + 1].isspace() or w[i + 1] in ORACLE_TRAILING
            starts = i == 0 or w[i - 1].isspace() or w[i - 1] in "[(<"
            if ch in "\"'”’" and ends and any(k == kind for k, _, _ in stack):
                close(kind, i)
            elif ch in "\"'“‘" and starts:
                stack.append((kind, i + 1, i))
        elif ch in "[(<":
            stack.append((ch, i + 1, i))
        elif ch in ORACLE_CLOSES and any(k == ORACLE_CLOSES[ch] for k, _, _ in stack):
            close(ORACLE_CLOSES[ch], i)
        i += 1
    return pairs


def oracle_shaped(x, names):
    """Rule 18: x counts as a path when it holds / or \\, ends in .<letters> or is a chore file's path or name."""
    return bool(x) and ("/" in x or "\\" in x or re.search(r"\.[^\W\d_]+\Z", x) is not None or x in names)


def oracle_pair_path(text, names):
    """The path a pair gives for the text from just after its opener to the line part (reverse: to its closer), or None: rules 24, 27, 29 and 30.
    ONE list separator, with non-empty text before it in the same item that does not end in a separator, non-empty text after it, and not followed by another, splits a
    lead-in off (the path starts after it); the path is then the item as written, white space trimmed, whatever it holds, and it is a path when it,
    or its last word, is path-shaped once the sentence punctuation at its end is not counted (the text stays as written). No other reading."""
    item = 0
    for i, c in enumerate(text):
        if c in (",", ";"):
            before = text[item:i].strip()
            if before and before[-1] not in (",", ";") and text[i + 1:i + 2] not in (",", ";") and text[i + 1:].strip():
                item = i + 1
    candidate = text[item:].strip()
    end = len(candidate)
    while end and candidate[end - 1] in ORACLE_TRAILING:
        end -= 1
    shown = candidate[:end]
    words = shown.split()
    return candidate if oracle_shaped(shown, names) or (words and oracle_shaped(words[-1], names)) else None


def oracle_path(w, files, where, reverse, legacy=False):
    """legacy=True is the reading BEFORE rules 29 and 30 (the round-12 checker, c8f96f5): a chore file written whole wins, a pair is a path when its whole
    text is path-shaped, and a forward pair that is not falls back to the token outside it (rule 18). It exists for ONE purpose, the product-wide
    condition of Main's ruling 4 (see oracle_mismatches): what each case did before rules 29 and 30. The current reading follows.

    The path a reader of the rules takes from the line w that holds one reference, or None when it takes none. Forward: where is the index of the
    separator run before the line number. Reverse: where is the index just after "of ". Inside a matched pair the path is exactly the text from just
    after the opener, trimmed of white space and nothing else (rule 24), and that is the path (rule 27) whatever it holds: "(junk src/cli.py:6)" is
    the file "junk src/cli.py", and there is no shortcut for a chore file, in either order. Rule 29: ONE list separator splits a lead-in off
    ("[see, src/cli.py:6]" is the file "src/cli.py"). Rule 30: a forward line part inside a pair is read by that pair alone, no rule-18 fallback
    ("( cli.py.:6)" is the file "cli.py."); a reverse reference whose pair holds no path falls back to the token, as before. Outside a pair it is the run of non-white-space characters minus the trailing punctuation (rule 18); a chore file's
    name is path-shaped, and that is all a chore file does here. The path is always the text as written (rule 26), whatever delimiter, however it
    is written: [src/cli.py#:29] and (src/cli.py#:29) both have the file src/cli.py, since rule 19 starts the line part at the "#"."""
    names = set(files) | {f.rpartition("/")[2] for f in files}
    pairs = oracle_pairs(w)
    if reverse:
        stop = next((i for i in range(where, len(w)) if w[i].isspace()), len(w))
        inner = sorted(i for i in pairs if where <= i < stop)  # the first delimiter that opens in the word, wherever it stands
        if inner:
            if legacy:
                candidate = w[pairs[inner[0]][0]:pairs[inner[0]][1]].strip()
                candidate = candidate if oracle_shaped(candidate, names) else None
            else:
                candidate = oracle_pair_path(w[pairs[inner[0]][0]:pairs[inner[0]][1]], names)
            if candidate is not None:
                return candidate
        head, end = where, stop
    else:
        head, end = max((i for i in range(where) if w[i].isspace()), default=-1) + 1, where
        inside = [i for i, (_, j) in pairs.items() if i < where < j]
        if inside and not legacy:  # the innermost pair around the line number reads it, and only that pair (rule 30)
            return oracle_pair_path(w[pairs[max(inside)][0]:where], names)
    while end > head and w[end - 1] in ORACLE_TRAILING:
        end -= 1
    bare = w[head:end]
    if legacy and not reverse:
        if bare in files:
            return bare
        if inside:
            candidate = w[pairs[max(inside)][0]:where].strip()
            if oracle_shaped(candidate, names):
                return candidate
    return bare if oracle_shaped(bare, names) else None


def oracle_items(w, grammar):
    """The paths of the exact items of w (rule 10): an item of a [bracket] with no bracket in it, split at commas, that is exactly path:N or path:N-M
    (a path of no white space, comma, colon or bracket) AND whose path is the one the grammar reads there (rule 26: one path reader). grammar is the
    path oracle_path reads from w, or None when the grammar reads none, in which case an item is read as written."""
    paths = [item.strip().rpartition(":")[0] for i, (start, j) in oracle_pairs(w).items() if w[i] == "[" and not set("[]") & set(w[start:j])
             for item in w[start:j].split(",") if ORACLE_ITEM.match(item.strip())]
    return [p for p in paths if grammar is None or p == grammar]


def oracle_expect(line, files, where, reverse, target, fact, legacy=False):
    """(path the grammar reads or None, the paths of the exact items, the rules the model expects of the line alone). legacy: see oracle_path."""
    grammar = oracle_path(line, files, where, reverse, legacy)
    items = oracle_items(line, grammar)
    # the failures are graded line by line (a bracket that a line break cuts in two holds no exact item, and a fact line whose citation list is cut is uncited)
    graded = [p for part in line.split("\n") for p in oracle_items(part, grammar)]
    tail = ORACLE_TAIL.search(line) if fact and "\n" not in line else None
    pieces = tail.group(1).split(",") if tail else []
    cited = bool(pieces) and all(ORACLE_ITEM.match(p.strip()) and p.strip().rpartition(":")[0] in items for p in pieces)
    if cited:  # a fact line whose whole citation list is exact items: nothing but the file can be wrong
        return grammar, items, ({UNKNOWN} if any(p != target for p in items) else set())
    if graded:  # exact items that are no fact line's citation list (a lead-in such as "see, " in front of them, rule 29): stray ones are graded (rule 10)
        rules_ = {UNCITED if fact else NONCANON} | ({UNKNOWN} if any(p != target for p in graded) else set())
        return grammar, items, rules_
    elif grammar is None:
        rules_ = {UNPARSED}
    else:
        rules_ = {NONCANON} | ({UNKNOWN} if grammar != target else set())
    return grammar, items, (rules_ | {UNCITED} if fact else rules_)


def oracle_name_at(target):
    """The index in a cited path of the place "name" of the characters family: where a character goes inside the file's own name. A name with an
    extension takes it just before the extension (the last "." of the LAST path segment: a "." in a directory name is no extension). A name with no
    extension (a bin/ script such as bin/tp-spawn) has no such place, and "just before the extension" would be the end of the path, which the place
    "last" already covers; the place is then just before the last character of the name, which is inside the name and in no other place."""
    start = target.rfind("/") + 1
    dot = target.rfind(".", start)
    return dot if dot >= 0 else len(target) - 1


def oracle_target(answer, with_lines=False):
    """The file the first fact line of the reference reply cites (with_lines: and its line part as written, "29" or "12-14")."""
    first = base.split_line(answer["reference"][0]["text"])[1].split(",")[0].strip()
    cited, lines = first.rsplit(":", 1)
    return (cited, lines) if with_lines else cited


def oracle_cases(answer, delimiter, family="characters", with_legacy=False, target=None):
    """(label, reply, line, expected path, expected exact items, expected rules, reverse, fact) for every case of one delimiter over one chore (the
    generator of the oracle). family "characters": a character in the path, as a line of prose in both orders and as a forward fact line;
    "separators": a separator between the path and its line number, as a line of prose and as a fact line; "prefixes": extra text and white space in
    front of the file and a suffix punctuation after it (rule 27), as a line of prose in both orders of rule 19 and as a fact line; "leadins" (rule 29)
    and "bare" (rule 30) are the lead-in and bare-name families. with_legacy adds a ninth field: (path, exact items, rules) as the reading before rules 29 and 30 expected them.
    target: the cited file the cases are written around; by default the first file the reference reply cites (any other entry of cite_files may be given)."""
    texts = [s["text"] for s in answer["reference"]]
    cited, lines = oracle_target(answer, with_lines=True)
    target = cited if target is None else target
    number = int(lines.partition("-")[0])
    opener, closer = ORACLE_DELIMITERS[delimiter]
    files = answer["cite_files"]
    dot = oracle_name_at(target)

    def case(label, line, where, reverse, fact):
        grammar, items, rules_ = oracle_expect(line, files, where, reverse, target, fact)
        # an ignored line (rule 2) between the reference and the line: rule 13 reads "PATH line N" across line breaks, so a reference claim that ends in a
        # path-shaped word ("... moved to lanes/archive/NAME-TIMESTAMP [..]") would otherwise take a following "line 8 of ..." as its own line part
        reply = "\n".join(texts) + "\nNotes\n" + line if (fact or "nothing" not in label) else line + "\nNotes\n" + "\n".join(texts)
        row = (label, reply, line, grammar, items, rules_, reverse, fact)
        return row + (oracle_expect(line, files, where, reverse, target, fact, True),) if with_legacy else row

    if family == "characters":
        for char in CHARACTERS:
            for place, path in zip(ORACLE_PLACES, (char + target, target + char, target[:2] + char + target[2:], target[:dot] + char + target[dot:])):
                for before in ORACLE_BEFORE:
                    for reverse in (False, True):
                        if reverse:
                            lead = "line {} of ".format(number)
                            line = lead + ("x" if before == "letter" else "") + opener + path + closer + " ok"
                            where = len(lead)
                        else:
                            lead = {"letter": "x", "space": "See ", "nothing": ""}[before]
                            line = lead + opener + path + ":{}".format(number) + closer + " ok"
                            where = len(lead) + len(opener) + len(path)
                            while line[where - 1] in ORACLE_SEPARATORS:  # a separator that ends the path belongs to the line part
                                where -= 1
                        yield case("{}/{}/{}/{}/{}".format(delimiter, repr(char), place, before, "reverse" if reverse else "forward"), line, where, reverse, False)
        for char in CHARACTERS:  # the same forward, as a fact line "- ok <opener>path:N<closer>" with nothing after the closer
            for place, path in zip(ORACLE_PLACES, (char + target, target + char, target[:2] + char + target[2:], target[:dot] + char + target[dot:])):
                line = "- ok " + opener + path + ":{}".format(number) + closer
                where = len("- ok ") + len(opener) + len(path)
                while line[where - 1] in ORACLE_SEPARATORS:
                    where -= 1
                yield case("{}/{}/{}/fact".format(delimiter, repr(char), place), line, where, False, True)
    elif family == "prefixes":
        for gap in ORACLE_GAPS:
            for prefix in ORACLE_PREFIXES:
                for suffix in ORACLE_SUFFIXES:
                    path = prefix + gap + target + suffix
                    name = "{}/{}/{}/{}".format(delimiter, repr(prefix), repr(gap), repr(suffix))
                    line = "See " + opener + path + ":{}".format(number) + closer + " ok"
                    where = len("See ") + len(opener) + len(path)
                    while line[where - 1] in ORACLE_SEPARATORS:  # a separator that ends the path belongs to the line part
                        where -= 1
                    yield case(name + "/forward", line, where, False, False)
                    lead = "line {} of ".format(number)
                    yield case(name + "/reverse", lead + opener + path + closer + " ok", len(lead), True, False)
                    line = "- ok " + opener + path + ":{}".format(number) + closer
                    where = len("- ok ") + len(opener) + len(path)
                    while line[where - 1] in ORACLE_SEPARATORS:
                        where -= 1
                    yield case(name + "/fact", line, where, False, True)
    elif family in ("leadins", "bare"):
        if family == "leadins":  # rule 29: lead-in x separator x white space after it (the path is lead-in + separator + white space + file)
            paths = [(repr(lead) + "/" + repr(sep) + "/" + repr(white), lead + sep + white + target) for lead in ORACLE_LEADINS
                     for sep in ORACLE_LEAD_SEPARATORS for white in ORACLE_LEAD_WHITE]
        else:  # rule 30: a bare file name, alone and with a trailing ".", "," or ";" (a name is no chore file's path: unknown_file)
            paths = [("name" + repr(end), target.rpartition("/")[2] + end) for end in ORACLE_BARE_ENDINGS]
        for name, path in paths:
            name = "{}/{}".format(delimiter, name)
            line = "See " + opener + path + ":{}".format(number) + closer + " ok"
            where = len("See ") + len(opener) + len(path)
            while line[where - 1] in ORACLE_SEPARATORS:  # a separator that ends the path belongs to the line part
                where -= 1
            yield case(name + "/forward", line, where, False, False)
            lead = "line {} of ".format(number)
            yield case(name + "/reverse", lead + opener + path + closer + " ok", len(lead), True, False)
            line = "- ok " + opener + path + ":{}".format(number) + closer
            where = len("- ok ") + len(opener) + len(path)
            while line[where - 1] in ORACLE_SEPARATORS:
                where -= 1
            yield case(name + "/fact", line, where, False, True)
    else:
        assert family == "separators", family
        for between in ORACLE_BETWEEN:
            for before in ORACLE_BEFORE:
                lead = {"letter": "x", "space": "See ", "nothing": ""}[before]
                line = lead + opener + target + between + ":{}".format(number) + closer + " ok"
                yield case("{}/between {}/{}/line".format(delimiter, repr(between), before), line, len(lead) + len(opener) + len(target), False, False)
            line = "- ok " + opener + target + between + ":{}".format(number) + closer
            yield case("{}/between {}/fact".format(delimiter, repr(between)), line, len("- ok ") + len(opener) + len(target), False, True)


def oracle_mismatches(answer, delimiter, family="characters", limit=None, stats=None, target=None, first=None):
    """(cases, full, mismatches): every oracle case of one delimiter over one chore, how many of them were also checked as the last or first line of the
    whole reference reply, and the ones where the checker's path, exact items or failures differ from the model's. The failures compared are rules 2
    (uncited), 3 (unknown_file), 15 (a stray bracket, rule 10, counts as 15) and 21 of the line alone; a mismatch is (label, line, expected path,
    reported paths, expected items, reported items, expected rules, reported rules).

    stats, a dict, is Main's ruling 4 as an assertion over the whole product: for each case it adds to stats["cases"], and puts in stats["broken"] every case that
    (a) failed before and now passes (the line alone adds a failure to the reply in all but stats["line_clean"] cases, and those are checked as whole replies;
    every 32nd case is also checked as a whole reply, and a line-level failure always made a reply fail there), or (b) had a bad citation before rules 29 and 30 (the reading of the
    legacy model: unknown_file expected) and now has no bad citation although the reply fails neither on rule 21 (unparsed) nor on rule 15. The counts
    stats["old_failed"] (cases the legacy model expected to fail), stats["old_bad"] (cases it expected a bad citation of) and stats["changed"] (cases whose
    path, exact items or expected rules changed) are what the report quotes. The lead-in and bare-name families have no legacy reading: only (a) applies."""
    cases, full, bad = 0, 0, []
    files = answer["cite_files"]
    legacy = stats is not None and family in ("characters", "separators", "prefixes")
    for row in oracle_cases(answer, delimiter, family, with_legacy=legacy, target=target):
        if first is not None and cases >= first:  # first: compare only the first cases (the guard over every cited file; the product tests compare all)
            break
        label, reply, line, expected, expected_items, expected_rules, reverse, fact = row[:8]
        cases += 1
        reported = [ref[2] for ref in summarize_check.references(line, files)[0]]
        items = [c[1] for c, _ in summarize_check.citation_items(line, files)]
        result = summarize_check.check(line, answer, base.FIXTURES)
        raw = base.rules(result)
        found = {NONCANON if failure == STRAY else failure for failure in raw} & WATCHED
        if stats is not None:
            stats["cases"] = stats.get("cases", 0) + 1
            failed_before = not legacy or bool(row[8][2])  # the lead-in and bare-name families are new: every reply of them is a bad one
            if failed_before and not found:  # (a) the line alone adds no failure: then the whole reply is looked at, and must not pass
                stats["line_clean"] = stats.get("line_clean", 0) + 1
                if summarize_check.check(reply, answer, base.FIXTURES)["pass"]:
                    stats.setdefault("broken", []).append((label, line, "passes"))
            if legacy:
                old = row[8][2]
                stats["old_failed"] = stats.get("old_failed", 0) + bool(old)
                stats["old_bad"] = stats.get("old_bad", 0) + (UNKNOWN in old)
                stats["changed"] = stats.get("changed", 0) + (row[8] != (expected, expected_items, expected_rules))
                if UNKNOWN in old and not result["bad_citations"] and not raw & {UNPARSED, NONCANON, STRAY}:  # (b)
                    stats.setdefault("broken", []).append((label, line, "bad citation gone"))
        if cases % ORACLE_FULL_EVERY == 0:
            full += 1
            checked = summarize_check.check(reply, answer, base.FIXTURES)
            whole = {NONCANON if failure == STRAY else failure for failure in base.rules(checked)} & WATCHED
            if stats is not None and checked["pass"] and (not legacy or row[8][2]):  # (a), also over the whole reply of every 32nd case
                stats.setdefault("broken", []).append((label, line, "passes"))
            if whole != found:
                reported = reported + ["(whole reply: {})".format(sorted(whole, key=str))]
        if reported != ([] if expected is None else [expected]) or items != expected_items or found != expected_rules:
            bad.append((label, line, expected, reported, expected_items, items, sorted(expected_rules, key=str), sorted(found, key=str)))
            if limit and len(bad) >= limit:
                break
    return cases, full, bad


class OracleCases:
    """The data and the checks the oracle test classes share (a plain mixin, not a TestCase: the modules that import it run no test twice). The path
    (characters) oracle is split over tests/test_qual_summarize_oracle_characters_{a,b,c}.py, three delimiters each, so that each file runs in well
    under 60 s alone; this file holds the other families, the reproducers and the guard over every cited file."""

    @classmethod
    def setUpClass(cls):
        cls.chores = base.jsonl("chores.jsonl")
        cls.answers = {a["id"]: a for a in base.jsonl("answers.jsonl")}

    # characters family: the cases rules 29 and 30 read differently from the round-12 reading, by delimiter (Main's ruling 4: accepted, on the condition below)
    # per chore whose first cited file has an extension (the 24 chores of Main's ruling 4 had 4800 in all, "," ";" "[" "<" in the path, 200 per chore)
    CHANGED_BY_RULES_29_30 = {"none": 0, "brackets": 32, "parens": 28, "angle": 32, "backticks": 28, "double quotes": 20, "single quotes": 20,
                              "curly double": 20, "curly single": 20}
    # a first cited file with no extension (bin/tp-spawn) has 4 more such cases in the pair delimiters that read a "," or ";" inside the name as a list
    # separator ("[tp-spaw,n:29]": a lead-in "tp-spaw", rule 29); its place "name" is before the last character, see oracle_name_at
    CHANGED_EXTRA_WITHOUT_EXTENSION = {"brackets": 4, "parens": 4, "angle": 4}

    def product_condition(self, stats, cases, where, family):
        """Main's ruling 4, over the whole product of a family (every case of every chore, in the delimiters the test runs): (a) no case now passes (every
        generated reply is a bad one), and (b) no case that had a bad citation before rules 29 and 30 (the legacy model: unknown_file expected) now has
        none unless the reply still fails on rule 21 or rule 15. The set of failure kinds may change; these two may not. The counts are what the report quotes."""
        self.assertEqual(stats["cases"], cases)
        self.assertEqual(stats.get("broken", [])[:5], [], "a case that failed before passes now, or lost its bad citation: " + str(where))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("product condition {} / {}: cases {}, expected to fail before {}, bad citation before {}, read differently now {}, broken {}".format(
                family, where, stats["cases"], stats.get("old_failed"), stats.get("old_bad"), stats.get("changed"), len(stats.get("broken", []))), file=sys.stderr)

    def path_oracle(self, delimiter):
        """Rules 24 to 26: the path the checker reads equals the path the generator wrote, and the failures are the model's, for every printable ASCII
        punctuation character and the 20 non-ASCII ones first, last and inside the cited file of each chore of every split (in its directory and in its
        name), with a letter, a space or nothing before the opener, in both orders of rule 19, inside one kind of delimiter (or none); and, forward,
        as a fact line."""
        cases = full = 0
        stats = {}
        for chore in self.chores:
            n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "characters", limit=5, stats=stats)
            self.assertEqual(bad, [], chore["id"])
            cases, full = cases + n, full + f
        self.product_condition(stats, cases, delimiter, "characters")
        bare = sum(1 for chore in self.chores if "." not in oracle_target(self.answers[chore["id"]]).rpartition("/")[2])
        self.assertEqual(stats["changed"], self.CHANGED_BY_RULES_29_30[delimiter] * len(self.chores) + self.CHANGED_EXTRA_WITHOUT_EXTENSION.get(delimiter, 0) * bare,
                         "cases read differently from the round-12 reading: " + delimiter)
        per_chore = len(CHARACTERS) * len(ORACLE_PLACES) * (len(ORACLE_BEFORE) * 2 + 1)
        self.assertEqual(cases, len(self.chores) * per_chore)
        self.assertEqual(full, len(self.chores) * (per_chore // ORACLE_FULL_EVERY))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("path oracle {:<14} cases {} (whole-reply checks {}) on python {}".format(delimiter, cases, full, sys.version.split()[0]), file=sys.stderr)

    def separator_oracle(self, delimiter):
        """Rules 19 and 26: whatever stands between the path and ":N" (a space, a tab, a no-break space, "#", "(", "·", ",", ":", and runs of them),
        the one path reader gives the same path, on a line of prose and on a fact line, whichever the delimiter: [src/cli.py#:29] and
        (src/cli.py#:29) both read src/cli.py."""
        cases = full = 0
        stats = {}
        for chore in self.chores:
            n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "separators", limit=5, stats=stats)
            self.assertEqual(bad, [], chore["id"])
            cases, full = cases + n, full + f
        self.product_condition(stats, cases, delimiter, "separators")
        self.assertEqual(stats["changed"], 0, "rules 29 and 30 change no separator case: " + delimiter)
        self.assertEqual(cases, len(self.chores) * len(ORACLE_BETWEEN) * (len(ORACLE_BEFORE) + 1))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("separator oracle {:<10} cases {} (whole-reply checks {}) on python {}".format(delimiter, cases, full, sys.version.split()[0]), file=sys.stderr)


class PathOracleTests(OracleCases, unittest.TestCase):
    def test_generated_prefix_oracle_for_every_delimiter_and_none(self):
        """Rule 27: extra text in front of the file inside a delimiter is part of the path ("(junk src/cli.py:6)" is the file "junk src/cli.py"), and
        outside one the run of non-white-space characters is the path; the model has no shortcut for a chore file. Every delimiter and none x five
        white spaces x three prefixes x three suffixes x both orders of rule 19 (forward also as a fact line) x every chore of every split."""
        cases = full = 0
        stats = {}
        for delimiter in ORACLE_DELIMITERS:
            for chore in self.chores:
                n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "prefixes", limit=5, stats=stats)
                self.assertEqual(bad, [], (delimiter, chore["id"]))
                cases, full = cases + n, full + f
        self.product_condition(stats, cases, "every delimiter and none", "prefixes")
        self.assertEqual(cases, len(ORACLE_DELIMITERS) * len(self.chores) * len(ORACLE_GAPS) * len(ORACLE_PREFIXES) * len(ORACLE_SUFFIXES) * 3)
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("prefix oracle cases {} (whole-reply checks {}) on python {}".format(cases, full, sys.version.split()[0]), file=sys.stderr)

    def test_generated_leadin_oracle_for_every_delimiter_and_none(self):
        """Rule 29: inside a pair ONE list separator splits a lead-in off ("[see, src/cli.py:6]" is the file "src/cli.py"; a doubled or no separator is
        no split: "[see,, src/cli.py:6]" and "(see src/cli.py:6)" are the files as written). A lead-in of a word, of two words and of a path fragment x
        the separators "," ";" ",," and none x no white space, a space and a line break after it x every delimiter and none x both orders of rule 19
        (forward also as a fact line) x every chore of every split."""
        cases = full = 0
        stats = {}
        for delimiter in ORACLE_DELIMITERS:
            for chore in self.chores:
                n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "leadins", limit=5, stats=stats)
                self.assertEqual(bad, [], (delimiter, chore["id"]))
                cases, full = cases + n, full + f
        self.product_condition(stats, cases, "every delimiter and none", "leadins")
        self.assertEqual(cases, len(ORACLE_DELIMITERS) * len(self.chores) * len(ORACLE_LEADINS) * len(ORACLE_LEAD_SEPARATORS) * len(ORACLE_LEAD_WHITE) * 3)
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("lead-in oracle cases {} (whole-reply checks {}) on python {}".format(cases, full, sys.version.split()[0]), file=sys.stderr)

    def test_generated_bare_name_oracle_for_every_delimiter_and_none(self):
        """Rule 30: a line part inside a pair is read by that pair alone, no rule-18 fallback: "( cli.py.:6)" is the file "cli.py." (unknown_file). A bare
        file name, alone and with a trailing ".", "," or ";" x every delimiter and none x both orders of rule 19 (forward also as a fact line) x every chore of every split
        chores."""
        cases = full = 0
        stats = {}
        for delimiter in ORACLE_DELIMITERS:
            for chore in self.chores:
                n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "bare", limit=5, stats=stats)
                self.assertEqual(bad, [], (delimiter, chore["id"]))
                cases, full = cases + n, full + f
        self.product_condition(stats, cases, "every delimiter and none", "bare")
        self.assertEqual(cases, len(ORACLE_DELIMITERS) * len(self.chores) * len(ORACLE_BARE_ENDINGS) * 3)
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("bare-name oracle cases {} (whole-reply checks {}) on python {}".format(cases, full, sys.version.split()[0]), file=sys.stderr)

    def test_generated_separator_oracle_without_a_delimiter(self):
        self.separator_oracle("none")

    def test_generated_separator_oracle_in_brackets(self):
        self.separator_oracle("brackets")

    def test_generated_separator_oracle_in_parentheses(self):
        self.separator_oracle("parens")

    def test_generated_separator_oracle_in_angle_brackets(self):
        self.separator_oracle("angle")

    def test_generated_separator_oracle_in_backticks(self):
        self.separator_oracle("backticks")

    def test_generated_separator_oracle_in_double_quotes(self):
        self.separator_oracle("double quotes")

    def test_generated_separator_oracle_in_single_quotes(self):
        self.separator_oracle("single quotes")

    def test_generated_separator_oracle_in_curly_double_quotes(self):
        self.separator_oracle("curly double")

    def test_generated_separator_oracle_in_curly_single_quotes(self):
        self.separator_oracle("curly single")

    def test_every_cited_file_of_every_split_is_handled_by_the_oracle_and_the_checker(self):
        """Round 3 guard. The generator once assumed that a cited file has an extension (target.rindex(".")) and raised on bin/tp-spawn, which the real
        split cites. Every file any answer of any split names in cite_files, as the file the cases are written around (not only the first citation of
        the reference reply), in every family and every delimiter: the generator yields cases, and the checker and the model agree on the first of
        them. A file whose last segment has no extension, and one with a "." only in a directory, are among them."""
        owner = {}
        for answer in self.answers.values():
            for name in answer["cite_files"]:
                owner.setdefault(name, answer)
        self.assertEqual(len({a["id"] for a in self.answers.values()}), len(self.chores))
        self.assertTrue(any("." not in name.rpartition("/")[2] for name in owner), "the data holds no extensionless cited file: the guard checks nothing")
        for name, answer in sorted(owner.items()):
            for family in ("characters", "separators", "prefixes", "leadins", "bare"):
                for delimiter in ORACLE_DELIMITERS:
                    cases, _, bad = oracle_mismatches(answer, delimiter, family, limit=3, target=name, first=48)
                    self.assertEqual(bad, [], (name, family, delimiter))
                    self.assertGreater(cases, 0, (name, family, delimiter))

    def test_path_oracle_generator_holds_the_reproducers_and_reads_them_as_the_rules_do(self):
        # review 11's F1 is a case of the oracle ("(;src/cli.py:29)": the file ";src/cli.py"), and so is F2's apostrophe: a quote after a letter
        # opens nothing, so the path is "x'src/cli.py" with the letter and the quote in it; round 12's row 28 is "src/cli.py#" in brackets,
        # which the one path reader (rule 26) gives as src/cli.py, as it does in parentheses, and which is no exact item then
        self.assertEqual(len(ORACLE_DELIMITERS), 9)
        answer = self.answers["use-jobqueue"]
        by_label = {c[0]: c for c in oracle_cases(answer, "parens")}
        _, _, line, expected, _, expected_rules, _, _ = by_label["parens/';'/first/space/forward"]
        self.assertEqual((line, expected, expected_rules), ("See (;src/cli.py:29) ok", ";src/cli.py", {NONCANON, UNKNOWN}))
        line, expected = next((c[2], c[3]) for c in oracle_cases(answer, "single quotes") if c[0] == "single quotes/'é'/first/letter/forward")
        self.assertEqual((line, expected), ("x'ésrc/cli.py:29' ok", "x'ésrc/cli.py"))
        by_label = {c[0]: c for c in oracle_cases(answer, "brackets")}
        _, _, line, expected, items, expected_rules, _, _ = by_label["brackets/'#'/last/space/forward"]
        self.assertEqual((line, expected, items, expected_rules), ("See [src/cli.py#:29] ok", "src/cli.py", [], {NONCANON}))
        _, _, line, expected, items, expected_rules, _, fact = by_label["brackets/'#'/last/fact"]
        self.assertEqual((line, expected, items, expected_rules, fact), ("- ok [src/cli.py#:29]", "src/cli.py", [], {NONCANON, UNCITED}, True))
        separators = {c[0]: c for c in oracle_cases(answer, "parens", "separators")}
        self.assertEqual(separators["parens/between '#'/space/line"][2:6], ("See (src/cli.py#:29) ok", "src/cli.py", [], {NONCANON}))
        # round 13 (review 12's F1, rule 27): extra text in front of the file inside a pair is part of the path, in both orders and as a fact line, and
        # the model has no shortcut for a chore file: "(junk src/cli.py:29)" is the file "junk src/cli.py", the same with a line break for the space
        prefixes = {c[0]: c for c in oracle_cases(answer, "parens", "prefixes")}
        self.assertEqual(len(prefixes), 5 * 3 * 3 * 3)
        for label, line, path, rules_ in (("parens/'junk'/' '/''/forward", "See (junk src/cli.py:29) ok", "junk src/cli.py", {NONCANON, UNKNOWN}),
                                          ("parens/'junk'/' '/''/reverse", "line 29 of (junk src/cli.py) ok", "junk src/cli.py", {NONCANON, UNKNOWN}),
                                          ("parens/'junk'/' '/''/fact", "- ok (junk src/cli.py:29)", "junk src/cli.py", {NONCANON, UNKNOWN, UNCITED}),
                                          ("parens/'.'/' '/'.'/forward", "See (. src/cli.py.:29) ok", ". src/cli.py.", {NONCANON, UNKNOWN}),
                                          ("parens/'junk'/'\\n'/''/forward", "See (junk\nsrc/cli.py:29) ok", "junk\nsrc/cli.py", {NONCANON, UNKNOWN}),
                                          ("parens/'a/'/'\\xa0'/','/forward", "See (a/\u00a0src/cli.py,:29) ok", "a/\u00a0src/cli.py", {NONCANON, UNKNOWN})):
            _, _, got, expected, _, expected_rules, _, _ = prefixes[label]
            self.assertEqual((got, expected, expected_rules), (line, path, rules_), label)
        none = {c[0]: c for c in oracle_cases(answer, "none", "prefixes")}  # outside a pair the run is the path (rule 18): only the file
        self.assertEqual(none["none/'junk'/' '/''/forward"][3], "src/cli.py")
        self.assertEqual(none["none/'junk'/' '/'.'/forward"][3], "src/cli.py")
        self.assertEqual(none["none/'junk'/' '/''/reverse"][3], None)  # "line 29 of junk": no path, rule 21's
        # round 13, Main's ruling 2 (rule 29): ONE list separator in a pair splits off a lead-in; a doubled one, none, or nothing in front of it does not
        leadins = {d: {c[0]: c for c in oracle_cases(answer, d, "leadins")} for d in ("brackets", "parens")}
        self.assertEqual(len(leadins["parens"]), 3 * 4 * 3 * 3)
        for delimiter, label, line, path, items, rules_ in (
                ("brackets", "'see'/','/' '/forward", "See [see, src/cli.py:29] ok", "src/cli.py", ["src/cli.py"], {NONCANON}),
                ("brackets", "'see'/','/''/fact", "- ok [see,src/cli.py:29]", "src/cli.py", ["src/cli.py"], {UNCITED}),
                ("brackets", "'a/'/','/' '/reverse", "line 29 of [a/, src/cli.py] ok", "src/cli.py", [], {NONCANON}),
                ("brackets", "'see'/',,'/' '/forward", "See [see,, src/cli.py:29] ok", "see,, src/cli.py", [], {NONCANON, UNKNOWN}),
                ("brackets", "'see'/''/' '/forward", "See [see src/cli.py:29] ok", "see src/cli.py", [], {NONCANON, UNKNOWN}),
                ("parens", "'see also'/';'/'\\n'/forward", "See (see also;\nsrc/cli.py:29) ok", "src/cli.py", [], {NONCANON}),
                ("parens", "'see'/','/' '/fact", "- ok (see, src/cli.py:29)", "src/cli.py", [], {NONCANON, UNCITED})):
            _, _, got, expected, got_items, expected_rules, _, _ = leadins[delimiter][delimiter + "/" + label]
            self.assertEqual((got, expected, got_items, expected_rules), (line, path, items, rules_), label)
        # rule 30: a bare file name in a pair is that name as written, with a trailing "." or ";" and in either order; "," ends a forward path (line part)
        bare = {c[0]: c for c in oracle_cases(answer, "parens", "bare")}
        self.assertEqual(len(bare), 4 * 3)
        for label, line, path, rules_ in (("parens/name'.'/forward", "See (cli.py.:29) ok", "cli.py.", {NONCANON, UNKNOWN}),
                                          ("parens/name';'/forward", "See (cli.py;:29) ok", "cli.py;", {NONCANON, UNKNOWN}),
                                          ("parens/name','/forward", "See (cli.py,:29) ok", "cli.py", {NONCANON, UNKNOWN}),
                                          ("parens/name','/reverse", "line 29 of (cli.py,) ok", "cli.py,", {NONCANON, UNKNOWN}),
                                          ("parens/name''/fact", "- ok (cli.py:29)", "cli.py", {NONCANON, UNKNOWN, UNCITED})):
            _, _, got, expected, _, expected_rules, _, _ = bare[label]
            self.assertEqual((got, expected, expected_rules), (line, path, rules_), label)
        outside = {c[0]: c for c in oracle_cases(answer, "none", "bare")}  # outside a pair rule 18 takes the sentence punctuation off the end
        self.assertEqual(outside["none/name'.'/forward"][3], "cli.py")
        # round 3: the place "name" of the characters family. With an extension it is just before the extension, the last "." of the LAST segment (a "."
        # in a directory is none); with none it is before the last character of the name, which "last" ("src/cli.py!") and "directory" do not cover
        for target, at in (("src/cli.py", 7), ("real/bin/tp-spawn", 16), ("real/v1.2/tool", 13), ("real/v1.2/x.tar.gz", 15), ("a/.gitignore", 2), ("tp-spawn", 7)):
            self.assertEqual(oracle_name_at(target), at, target)
        spawn = dict(answer, cite_files=["real/bin/tp-spawn"], reference=[{"text": "- tp-spawn runs it [real/bin/tp-spawn:29]"}])
        by_label = {c[0]: c for c in oracle_cases(spawn, "none")}
        self.assertEqual(by_label["none/'!'/name/space/forward"][2], "See real/bin/tp-spaw!n:29 ok")
        self.assertEqual(by_label["none/'!'/last/space/forward"][2], "See real/bin/tp-spawn!:29 ok")
        self.assertEqual(by_label["none/'!'/directory/space/forward"][2], "See re!al/bin/tp-spawn:29 ok")
        # rule 13 reads "PATH line N" across line breaks: a reference claim that ends in a path-shaped word, then a line "line 8 of ...", is a reference
        # to that word at line 8 (noncanonical) in the checker, and so the generator puts an ignored line between the reference and its own line
        joined = "- it is moved to lanes/archive/NAME-TIMESTAMP [src/cli.py:29]\nline 8 of x ok"
        self.assertIn(NONCANON, base.rules(summarize_check.check(joined, answer, base.FIXTURES)))
        self.assertNotIn(NONCANON, base.rules(summarize_check.check(joined.replace("]\n", "]\nNotes\n"), answer, base.FIXTURES)))
        row = next(c for c in oracle_cases(spawn, "none") if c[0] == "none/'!'/name/space/forward")
        self.assertIn("\nNotes\n", row[1])


if __name__ == "__main__":
    unittest.main()
