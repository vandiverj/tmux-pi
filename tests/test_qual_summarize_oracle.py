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
# orders of rule 19, over all 24 chores ("characters"); and, between the file and its line number, every separator that can sit just before the colon
# ("separators"). Each case is a line of prose and, in the fact-line form, a fact line "- ok [..]" that the checker may or may not accept.
ORACLE_DELIMITERS = {"none": ("", ""), "brackets": ("[", "]"), "parens": ("(", ")"), "angle": ("<", ">"), "backticks": ("`", "`"),
                     "double quotes": ('"', '"'), "single quotes": ("'", "'"), "curly double": ("“", "”"), "curly single": ("‘", "’")}
ORACLE_PLACES = ("first", "last", "directory", "name")
ORACLE_BEFORE = ("letter", "space", "nothing")
ORACLE_BETWEEN = ["", " ", "  ", " ", "\t", "#", "(", "·", ",", ":", "#:", " #", "# ", ": ", ",,"]  # what may stand between the path and ":N"
ORACLE_TRAILING = ".,;:!?)]}>'\"`*”’"  # rule 18, written out again
ORACLE_SEPARATORS = " :#,(·"  # what rule 19 allows between a path and its line number: a separator at the end of the path is one of them
ORACLE_CLOSES = {"]": "[", ")": "(", ">": "<", "”": "“", "’": "‘"}
ORACLE_ITEM = re.compile(r"[^\s,:\[\]]+:[0-9]+(?:-[0-9]+)?\Z")
ORACLE_TAIL = re.compile(r"\[([^\[\]]*)\]\Z")
ORACLE_FULL_EVERY = 32  # every 32nd case is also checked inside the whole reference reply (about 6x the cost of the line alone)
CHARACTERS = list(base.ASCII_PUNCTUATION) + base.NON_ASCII


def oracle_pairs(w):
    """{opener index: (index just after the opener, closer index)}: the matched pairs of w by a plain stack (rules 23 and 25). A bracket or a backtick
    run (N of them close at the next N) is a delimiter wherever it stands; a quote opens only at the start of w or after a space or an opening
    bracket, and closes only before the end, a space or rule-18 trailing punctuation; any other quote is an apostrophe."""
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
            ends = i + 1 == len(w) or w[i + 1] == " " or w[i + 1] in ORACLE_TRAILING
            starts = i == 0 or w[i - 1] == " " or w[i - 1] in "[(<"
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


def oracle_path(w, files, where, reverse):
    """The path a reader of the rules takes from the line w that holds one reference, or None when it takes none. Forward: where is the index of the
    separator run before the line number. Reverse: where is the index just after "of ". Inside a delimiter the path is exactly the text from just
    after the opener, trimmed of white space and nothing else (rule 24); outside it is the run of non-space characters minus the trailing
    punctuation (rule 18); a chore file written whole wins (rule 18); the path is always the text as written (rule 26), whatever delimiter, however
    it is written: [src/cli.py#:29] and (src/cli.py#:29) both have the file src/cli.py, since rule 19 starts the line part at the "#"."""
    names = set(files) | {f.rpartition("/")[2] for f in files}
    pairs = oracle_pairs(w)
    if reverse:
        stop = w.find(" ", where)
        stop = len(w) if stop < 0 else stop
        inner = sorted(i for i in pairs if where <= i < stop)  # the first delimiter that opens in the word, wherever it stands
        if inner:
            candidate = w[pairs[inner[0]][0]:pairs[inner[0]][1]].strip()
            if oracle_shaped(candidate, names):
                return candidate
        head, end = where, stop
    else:
        head, end = w.rfind(" ", 0, where) + 1, where
    while end > head and w[end - 1] in ORACLE_TRAILING:
        end -= 1
    bare = w[head:end]
    if not reverse:
        if bare in files:
            return bare
        inside = [i for i, (_, j) in pairs.items() if i < where < j]
        if inside:  # the innermost pair around the line number
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


def oracle_expect(line, files, where, reverse, target, fact):
    """(path the grammar reads or None, the paths of the exact items, the rules the model expects of the line alone)."""
    grammar = oracle_path(line, files, where, reverse)
    items = oracle_items(line, grammar)
    tail = ORACLE_TAIL.search(line) if fact else None
    pieces = tail.group(1).split(",") if tail else []
    cited = bool(pieces) and all(ORACLE_ITEM.match(p.strip()) and p.strip().rpartition(":")[0] in items for p in pieces)
    if cited:  # a fact line whose whole citation list is exact items: nothing but the file can be wrong
        return grammar, items, ({UNKNOWN} if any(p != target for p in items) else set())
    if items:
        rules_ = {NONCANON} | ({UNKNOWN} if any(p != target for p in items) else set())
    elif grammar is None:
        rules_ = {UNPARSED}
    else:
        rules_ = {NONCANON} | ({UNKNOWN} if grammar != target else set())
    return grammar, items, (rules_ | {UNCITED} if fact else rules_)


def oracle_cases(answer, delimiter, family="characters"):
    """(label, reply, line, expected path, expected exact items, expected rules, reverse, fact) for every case of one delimiter over one chore (the
    generator of the oracle). family "characters": a character in the path, as a line of prose in both orders and as a forward fact line;
    "separators": a separator between the path and its line number, as a line of prose and as a fact line."""
    texts = [s["text"] for s in answer["reference"]]
    first = base.split_line(texts[0])[1].split(",")[0].strip()
    target, lines = first.rsplit(":", 1)
    number = int(lines.partition("-")[0])
    opener, closer = ORACLE_DELIMITERS[delimiter]
    files = answer["cite_files"]
    dot = target.rindex(".")

    def case(label, line, where, reverse, fact):
        grammar, items, rules_ = oracle_expect(line, files, where, reverse, target, fact)
        reply = "\n".join(texts) + "\n" + line if (fact or "nothing" not in label) else line + "\n" + "\n".join(texts)
        return label, reply, line, grammar, items, rules_, reverse, fact

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
    else:
        for between in ORACLE_BETWEEN:
            for before in ORACLE_BEFORE:
                lead = {"letter": "x", "space": "See ", "nothing": ""}[before]
                line = lead + opener + target + between + ":{}".format(number) + closer + " ok"
                yield case("{}/between {}/{}/line".format(delimiter, repr(between), before), line, len(lead) + len(opener) + len(target), False, False)
            line = "- ok " + opener + target + between + ":{}".format(number) + closer
            yield case("{}/between {}/fact".format(delimiter, repr(between)), line, len("- ok ") + len(opener) + len(target), False, True)


def oracle_mismatches(answer, delimiter, family="characters", limit=None):
    """(cases, full, mismatches): every oracle case of one delimiter over one chore, how many of them were also checked as the last or first line of the
    whole reference reply, and the ones where the checker's path, exact items or failures differ from the model's. The failures compared are rules 2
    (uncited), 3 (unknown_file), 15 (a stray bracket, rule 10, counts as 15) and 21 of the line alone; a mismatch is (label, line, expected path,
    reported paths, expected items, reported items, expected rules, reported rules)."""
    cases, full, bad = 0, 0, []
    files = answer["cite_files"]
    for label, reply, line, expected, expected_items, expected_rules, reverse, fact in oracle_cases(answer, delimiter, family):
        cases += 1
        reported = [ref[2] for ref in summarize_check.references(line, files)[0]]
        items = [c[1] for c, _ in summarize_check.citation_items(line, files)]
        found = {NONCANON if failure == STRAY else failure for failure in base.rules(summarize_check.check(line, answer, base.FIXTURES))} & WATCHED
        if cases % ORACLE_FULL_EVERY == 0:
            full += 1
            whole = {NONCANON if failure == STRAY else failure for failure in base.rules(summarize_check.check(reply, answer, base.FIXTURES))} & WATCHED
            if whole != found:
                reported = reported + ["(whole reply: {})".format(sorted(whole, key=str))]
        if reported != ([] if expected is None else [expected]) or items != expected_items or found != expected_rules:
            bad.append((label, line, expected, reported, expected_items, items, sorted(expected_rules, key=str), sorted(found, key=str)))
            if limit and len(bad) >= limit:
                break
    return cases, full, bad


class PathOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chores = base.jsonl("chores.jsonl")
        cls.answers = {a["id"]: a for a in base.jsonl("answers.jsonl")}

    def path_oracle(self, delimiter):
        """Rules 24 to 26: the path the checker reads equals the path the generator wrote, and the failures are the model's, for every printable ASCII
        punctuation character and the 20 non-ASCII ones first, last and inside the cited file of each of the 24 chores (in its directory and in its
        name), with a letter, a space or nothing before the opener, in both orders of rule 19, inside one kind of delimiter (or none); and, forward,
        as a fact line."""
        cases = full = 0
        for chore in self.chores:
            n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "characters", limit=5)
            self.assertEqual(bad, [], chore["id"])
            cases, full = cases + n, full + f
        per_chore = len(CHARACTERS) * len(ORACLE_PLACES) * (len(ORACLE_BEFORE) * 2 + 1)
        self.assertEqual(cases, 24 * per_chore)
        self.assertEqual(full, 24 * (per_chore // ORACLE_FULL_EVERY))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("path oracle {:<14} cases {} (whole-reply checks {}) on python {}".format(delimiter, cases, full, sys.version.split()[0]), file=sys.stderr)

    def separator_oracle(self, delimiter):
        """Rules 19 and 26: whatever stands between the path and ":N" (a space, a tab, a no-break space, "#", "(", "·", ",", ":", and runs of them),
        the one path reader gives the same path, on a line of prose and on a fact line, whichever the delimiter: [src/cli.py#:29] and
        (src/cli.py#:29) both read src/cli.py."""
        cases = full = 0
        for chore in self.chores:
            n, f, bad = oracle_mismatches(self.answers[chore["id"]], delimiter, "separators", limit=5)
            self.assertEqual(bad, [], chore["id"])
            cases, full = cases + n, full + f
        self.assertEqual(cases, 24 * len(ORACLE_BETWEEN) * (len(ORACLE_BEFORE) + 1))
        if os.environ.get("QUAL_ATTACK_COUNTS"):
            print("separator oracle {:<10} cases {} (whole-reply checks {}) on python {}".format(delimiter, cases, full, sys.version.split()[0]), file=sys.stderr)

    def test_generated_path_oracle_without_a_delimiter(self):
        self.path_oracle("none")

    def test_generated_path_oracle_in_brackets(self):
        self.path_oracle("brackets")

    def test_generated_path_oracle_in_parentheses(self):
        self.path_oracle("parens")

    def test_generated_path_oracle_in_angle_brackets(self):
        self.path_oracle("angle")

    def test_generated_path_oracle_in_backticks(self):
        self.path_oracle("backticks")

    def test_generated_path_oracle_in_double_quotes(self):
        self.path_oracle("double quotes")

    def test_generated_path_oracle_in_single_quotes(self):
        self.path_oracle("single quotes")

    def test_generated_path_oracle_in_curly_double_quotes(self):
        self.path_oracle("curly double")

    def test_generated_path_oracle_in_curly_single_quotes(self):
        self.path_oracle("curly single")

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


if __name__ == "__main__":
    unittest.main()
