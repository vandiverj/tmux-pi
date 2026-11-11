"""Rule 20 of the summarize_files checker (tools/qual/summarize_check.py): a megabyte of any shape is read in linear time. Split from
tests/test_qual_summarize.py so that each file runs in well under 60 s. No model is run.

The bound is a COUNT, not a time (round 2 of LS1b: a CPU-second bound failed on a loaded machine, since the CPU seconds of this Mac grow under load).
steps() reads one reply with the checker's own counters switched on, from OUTSIDE the checker (the checker has no counter and returns what it
always returned; the same count can be taken of any version of the file, which is how the bound was shown to fail on the quadratic reader of
commit 8560cf6). What is counted, in basic steps:
  - every character of the scan (the NFKC copy of the reply that rules 17 to 30 read) that is looked at: an index is 1, a slice is its length + 1,
    rfind is the length of the range it searches + 1 (a walk over the text, in Python or in C, is a walk over the scan);
  - every item of the table of backtick runs that is looked at (rule 8);
  - every probe of a bisect table (the boundary tables of rule 20);
  - every match that a regular expression of the checker yields, and every other call of one.
A count is the same on every run, load and machine (and on py3.9 and py3.14, up to the Unicode database), because it depends on the text and on
the algorithm only. BUDGET holds, for each shape, the steps per character allowed: 1.25 times the count of the current code, so a reader that
walks anything for each of k candidates fails at once. A wall-clock timer (BACKSTOP seconds) is only a guard against a hang; it is never the
pass criterion."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import re
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_qual_summarize as base  # noqa: E402  (names only: no test class is imported, so no test runs twice)

summarize_check = base.summarize_check
FIXTURES, rules = base.FIXTURES, base.rules
BACKSTOP = 60  # seconds of wall clock: a guard against a hang, never the pass criterion


class StepLimit(AssertionError):
    pass


class Steps:
    """The count of one reading. add() is the one place that counts; past the limit it stops the reading where it is (a quadratic reader is
    stopped in the first second, not after minutes)."""
    def __init__(self, limit=float("inf")):
        self.n, self.limit = 0, limit

    def add(self, cost):
        self.n += cost
        if self.n > self.limit:
            raise StepLimit("more than {:,.0f} steps".format(self.limit))


def counted_string(steps):
    """A str class whose character accesses add to steps: str.__getitem__ (an index is 1, a slice is its length + 1) and rfind (the range + 1)."""
    class Counted(str):
        __slots__ = ()

        def __getitem__(self, key):
            if type(key) is slice:
                a, b, _ = key.indices(len(self))
                steps.add(b - a + 1 if b > a else 1)
            else:
                steps.n += 1
                if steps.n > steps.limit:
                    steps.add(0)
            return str.__getitem__(self, key)

        def rfind(self, sub, a=0, b=None):
            lo, hi, _ = slice(a, b).indices(len(self))
            steps.add(hi - lo + 1 if hi > lo else 1)
            return str.rfind(self, sub, a, b)
    return Counted


class CountedBisect(object):
    """The bisect module of the checker with each probe counted."""
    def __init__(self, real, steps):
        self.real, self.steps = real, steps

    def bisect_left(self, *args):
        self.steps.add(1)
        return self.real.bisect_left(*args)

    def bisect_right(self, *args):
        self.steps.add(1)
        return self.real.bisect_right(*args)


class CountedPattern(object):
    """A compiled pattern of the checker with each match it yields, and each other call, counted."""
    def __init__(self, real, steps):
        self.real, self.steps = real, steps

    def finditer(self, *args):
        for m in self.real.finditer(*args):
            self.steps.add(1)
            yield m

    def search(self, *args):
        self.steps.add(1)
        return self.real.search(*args)

    def match(self, *args):
        self.steps.add(1)
        return self.real.match(*args)

    def fullmatch(self, *args):
        self.steps.add(1)
        return self.real.fullmatch(*args)

    def sub(self, *args):
        self.steps.add(1)
        return self.real.sub(*args)

    def __getattr__(self, name):
        return getattr(self.real, name)


class CountedSpan(tuple):
    """The (start, end) of a backtick run: each item looked at is a step (the table that rule 8 reads for the closer of a run)."""
    steps = None

    def __getitem__(self, key):
        self.steps.add(1)
        return tuple.__getitem__(self, key)


class CountedRuns(CountedPattern):
    """BACKTICK_RUN, whose match spans are counted when looked at."""
    def finditer(self, *args):
        span = type("Span", (CountedSpan,), {"steps": self.steps})

        class Match(object):
            __slots__ = ("m",)

            def __init__(self, m):
                self.m = m

            def span(self):
                return span(self.m.span())
        for m in self.real.finditer(*args):
            self.steps.add(1)
            yield Match(m)


@contextlib.contextmanager
def counting(steps, module=None):
    """Turns the counters on for summarize_check (or module, another copy of the file) and off again, whatever happens."""
    module = module or summarize_check
    patched = {"_normalize": module._normalize, "bisect": module.bisect}
    string = counted_string(steps)

    def normalize(text):
        scan, origin = patched["_normalize"](text)
        return string(scan), origin
    for name, value in list(vars(module).items()):
        if isinstance(value, re.Pattern):
            patched[name] = value
    module._normalize, module.bisect = normalize, CountedBisect(patched["bisect"], steps)
    for name, value in patched.items():
        if isinstance(value, re.Pattern):
            setattr(module, name, (CountedRuns if name == "BACKTICK_RUN" else CountedPattern)(value, steps))
    module._citation_items.cache_clear()  # a line read before is not read again: the count must not depend on what ran earlier
    try:
        yield
    finally:
        for name, value in patched.items():
            setattr(module, name, value)
        module._citation_items.cache_clear()


@contextlib.contextmanager
def backstop(name, seconds=BACKSTOP):
    """Stops a reading that has run for `seconds` of wall clock (a hang). It is no criterion: a slow machine takes longer and still passes."""
    def stop(signum, frame):
        raise AssertionError("{}: still running after {} seconds (a hang)".format(name, seconds))
    previous = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def steps_of(reply, answer, limit=float("inf"), module=None):
    """(steps, result) of one reading of reply by summarize_check.check (or by module's copy of it)."""
    steps = Steps(limit)
    with counting(steps, module):
        result = (module or summarize_check).check(reply, answer, FIXTURES)
    return steps.n, result


# Steps per character allowed for each shape: 1.25 times what the current code takes (rounded up to 0.01), the larger of py3.9 and py3.14.
BUDGET = {
    "forward in ( )": 0.09,
    "forward in < >": 0.09,
    "forward in [ ]": 0.13,
    "forward in backticks": 0.23,
    "reverse in backticks": 0.51,
    "reverse in < >": 0.65,
    "reverse in [ ]": 0.65,
    "reverse in quotes": 0.81,
    "alternating bracket types": 0.86,
    "reverse then forward": 1.04,
    "reverse in ( )": 1.07,
    "reverse in ( ), path with spaces": 1.07,
    "forward and reverse alternating": 1.1,
    "forward in ( ), no path": 1.3,
    "bracket items, many lines": 1.85,
    "floor": 2.16,
    "line breaks": 2.51,
    "open brackets": 2.51,
    "short fact lines": 2.51,
    "words": 2.73,
    "dense references": 3.01,
    "reverse in ( ), no path": 3.03,
    "compatibility forms": 3.13,
    "line of": 3.21,
    "forward in ( ), failed line parts after a word": 4.38,
    "bracket-with-hash lines": 5.18,
    "forward in ( ), failed line parts after spaces": 5.7,
    "unmatched backtick runs in a claim": 5.85,
    "hash items one line": 8.2,
    "unknown-name items one line": 9.66,
    "numbers": 10.0,
    "unicode digits": 10.0,
    "comma lists one line": 13.58,
}
DEPTH_BUDGET = 1.1  # steps per character, at both depths (the ratio of the two is what the depth test bounds)


class LinearTimeTests(unittest.TestCase):
    ANSWER = base.CheckerTests.ANSWER
    C = base.CheckerTests.C  # [src/cache.py:6]
    # TQ1 Q2: the shapes were 1 MB each; every size is now a tenth of that. The count of steps per character is the same at both sizes (to
    # three digits, for every shape, so the budgets below did not change), and a reader that walks the text for each of k candidates takes k
    # times the steps: the four tests that failed on the quadratic reader of 8560cf6 at 1 MB fail on it at this size too, with the same
    # message. "MB" below is the size of one input: 100,000 characters.
    SCALE = 10
    MB = 1000000 // SCALE
    NEST = 16000 // SCALE  # nested pairs of one shape
    RUNS = 120  # the longest backtick run (the runs 1 to RUNS add up to RUNS squared / 2 characters); 600 at 1 MB
    MEASURED = {}  # shape name -> steps per character of the last reading (for whoever sets the budgets)

    def reading(self, name, body, budget=None):
        """The steps of reading a reply made of the two fact lines, body and a reference to a missing file at its end (found whatever
        comes before it). It fails when they pass budget (BUDGET[name]) steps for each character, and when the missing file is not found."""
        reply = "- alpha {c}\n- beta {c}\n{body}\nsrc/nonexistent.rs:999".format(c=self.C, body=body)
        allowed = BUDGET[name] if budget is None else budget
        try:
            with backstop(name):
                steps, result = steps_of(reply, self.ANSWER, allowed * len(reply))
        except StepLimit as e:
            self.fail("{}: {} for {:,} characters (more than {} a character)".format(name, e, len(reply), allowed))
        self.MEASURED[name] = steps / len(reply)
        self.assertIn((3, "unknown_file"), rules(result), name)
        return steps

    def megabyte(self, unit):
        """A value is a piece repeated up to 1 MB, or a function that returns the 1 MB body."""
        for name, piece in unit.items():
            self.reading(name, piece() if callable(piece) else piece * (self.MB // len(piece)))

    def test_the_counter_counts_what_is_looked_at_and_is_the_same_every_time(self):
        steps = Steps()
        text = counted_string(steps)("a b c d")
        text[0], text[2:5], text.rfind(" ", 0, 4), text[3:3]
        self.assertEqual(steps.n, 1 + 4 + 5 + 1)
        reply = "- alpha {c}\n- beta {c}\nsee (src/cache.py:6) and line 7 of [src/nonexistent.rs] `x` [a:1]\nsrc/nonexistent.rs:999".format(c=self.C)
        first, again = steps_of(reply, self.ANSWER)[0], steps_of(reply, self.ANSWER)[0]
        self.assertGreater(first, 100)
        self.assertEqual(first, again)
        self.assertEqual(summarize_check.check(reply, self.ANSWER, FIXTURES), steps_of(reply, self.ANSWER)[1])  # counting changes no result
        self.assertIs(summarize_check.bisect, __import__("bisect"))  # and leaves nothing patched
        self.assertFalse(any(isinstance(v, (CountedPattern, CountedBisect)) for v in vars(summarize_check).values()))

    def test_the_counter_stops_a_reader_that_walks_the_text_for_each_candidate(self):
        # a stand-in for the round-13 reader: each of 3000 candidates walks the 3000 characters after it (a million steps), against a limit of
        # 20 steps a character; and the walk of the tail of the backtick table, which the checker's rule 8 used to make for each run
        steps = Steps(20 * 3000)
        with counting(steps):
            scan, _ = summarize_check._normalize("x" * 3000)
            with self.assertRaises(StepLimit):
                for i in range(len(scan)):
                    for j in range(i, len(scan)):
                        scan[j]
        steps = Steps(20 * 3000)
        with counting(steps):
            runs = [m.span() for m in summarize_check.BACKTICK_RUN.finditer("`x" * 3000)]
            with self.assertRaises(StepLimit):
                for i in range(len(runs)):
                    next((j for j in range(i + 1, len(runs)) if runs[j][1] - runs[j][0] == 7), None)

    # ---- the shapes: the same 1 MB inputs as before, every one of them ----
    def test_rule_20_a_megabyte_of_words_references_and_brackets_is_read_in_linear_time(self):
        self.megabyte({"words": "The queue evicts after 128 entries and retries 3 times. ", "line breaks": "\n", "open brackets": "[",
                       "dense references": "src/cache.py:6 ", "line of": "line 6 of src/cache.py ", "floor": "src/a.py;7 ", "compatibility forms": "ﬁ "})

    def test_rule_20_a_megabyte_of_numbers_is_read_in_linear_time(self):
        # the shapes with the most numbers (one for each two characters): the floor reads each, ASCII and fullwidth
        self.megabyte({"numbers": "7 ", "unicode digits": "９ "})

    def test_rule_20_a_megabyte_of_bracket_items_on_one_line_is_read_in_linear_time(self):
        # rule 10 reads the items of a line through the one path reader (references), so a line of bracket items costs a second reading of the line;
        # the comma lists are the worst shape (three items for 14 characters)
        self.megabyte({"comma lists one line": "[a:1,b:2,c:3] ", "hash items one line": "[src/cache.py#:6] ", "unknown-name items one line": "[nofile:9] "})

    def test_rule_20_a_megabyte_of_bracket_items_on_many_lines_is_read_in_linear_time(self):
        self.megabyte({"short fact lines": "- x [y:1]\n", "bracket-with-hash lines": "[x#:1]\n", "bracket items, many lines": "- x [src/cache.py:6]\n"})

    # ---- round 14 (review-coder-b69bae86e7-032459 F1): nested pairs, read in linear time at any depth ----
    def reverse_nest(self, opener, closer, k=None, tail=".py"):
        """1 MB: k nested "line 1 of <opener>" (reverse order), one long word, tail, k closers. The k candidates share one tail."""
        k = k or self.NEST
        lead = "line 1 of " + opener
        return lead * k + "x" * (self.MB - (len(lead) + len(closer)) * k - len(tail)) + tail + closer * k

    def forward_nest(self, opener, closer, k=None, tail=".py:6"):
        """1 MB: k nested openers, one long word, tail (path and line part), k closers."""
        k = k or self.NEST
        return opener * k + "x" * (self.MB - (len(opener) + len(closer)) * k - len(tail)) + tail + closer * k

    def backtick_nest(self, lead, tail, depth=None):
        """1 MB: backtick runs of length 1 to depth (the only way backticks nest), each after lead, one long word, tail, the closers from depth to 1."""
        depth = depth or self.RUNS
        opens = "".join(lead + "`" * i for i in range(1, depth + 1))
        closes = " ".join("`" * i for i in range(depth, 0, -1))
        return opens + "x" * (self.MB - len(opens) - len(closes) - len(tail)) + tail + closes

    def test_rule_20_a_megabyte_of_nested_reverse_pairs_is_read_in_linear_time(self):
        # the reviewer's shape (k = 8000 took 4.4 s CPU in round 13, 13.4 s at 16000): each of k nested candidates walked the same tail again.
        # The last shape has no path: every candidate fails, and the old reader walked the 900000 letters of its tail for each (88 s at k = 1000).
        self.megabyte({"reverse in ( )": lambda: self.reverse_nest("(", ")"), "reverse in [ ]": lambda: self.reverse_nest("[", "]"),
                       "reverse in < >": lambda: self.reverse_nest("<", ">"), "reverse in quotes": lambda: self.reverse_nest('"', '"'),
                       "reverse in backticks": lambda: self.backtick_nest("line 1 of ", ".py"),
                       "reverse in ( ), no path": lambda: self.reverse_nest("(", ")", tail=""),
                       "reverse in ( ), path with spaces": lambda: self.reverse_nest("(", ")", tail=" end.py")})

    def test_rule_20_a_megabyte_of_nested_forward_pairs_is_read_in_linear_time(self):
        # the forward loop: nested pairs around one path and line part, and many line parts that each fail inside one pair (a long word, or a long
        # run of spaces, in front of them: each failed candidate must not read it again)
        self.megabyte({"forward in ( )": lambda: self.forward_nest("(", ")"), "forward in [ ]": lambda: self.forward_nest("[", "]"),
                       "forward in < >": lambda: self.forward_nest("<", ">"), "forward in backticks": lambda: self.backtick_nest("", ".py:6"),
                       "forward in ( ), no path": lambda: self.forward_nest("(", ")", tail=":6"),
                       "forward in ( ), failed line parts after a word": lambda: "(" + "x" * (self.MB // 2) + ":6" * (250000 // self.SCALE),
                       "forward in ( ), failed line parts after spaces": lambda: "(" + " " * (self.MB // 2) + "x" + ":6" * (200000 // self.SCALE) + ")"})

    def test_rule_20_a_megabyte_of_mixed_nested_pairs_and_code_spans_is_read_in_linear_time(self):
        def padded(head, tail):
            return head + "x" * (self.MB - len(head) - len(tail)) + tail
        self.megabyte({"reverse then forward": lambda: padded("line 1 of (" * (self.NEST // 2) + "(" * (self.NEST // 2), ".py:6" + ")" * self.NEST),
                       "alternating bracket types": lambda: padded("line 1 of (line 1 of [" * (self.NEST // 2), ".py" + "])" * (self.NEST // 2)),
                       "forward and reverse alternating": lambda: padded("(line 1 of [" * (self.NEST // 2), ".py:6" + "])" * (self.NEST // 2)),
                       # a claim: unmatched backtick runs of every length from 600 down to 2 each looked for a closer through all the runs after it
                       "unmatched backtick runs in a claim": lambda: "- " + "".join("`" * i + "x" for i in range(self.RUNS, 1, -1)) + "`x" * (400000 // self.SCALE) + " " + self.C})

    def test_rule_20_nesting_depth_costs_the_same_for_each_pair(self):
        """The reviewer's shape at the same 1 MB with 1000 pairs and with 16000: the steps are the same for each pair (a linear reader takes 15.9
        times as many for 16 times the pairs: each pair is a match of the forward grammar and two delimiters); the round-13 reader took 36 times
        the CPU. The bound is 20 times: 1.25 times the linear ratio and a thirteenth of the quadratic one."""
        few, many = (self.reading("depth {}".format(k), self.reverse_nest("(", ")", k), budget=DEPTH_BUDGET) for k in (self.NEST // 16, self.NEST))
        self.assertLess(many, 20 * few, (few, many))


if __name__ == "__main__":
    unittest.main()
