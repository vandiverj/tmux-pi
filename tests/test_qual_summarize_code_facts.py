"""LS1c S1c on lib/summarize_structured.py: version_pairs, version_diff, use_scan, the false_absence drop, the user message blocks and
summarize's answer order. The fixtures here are written for the tests; the calibration files are only read by the guard. Stub endpoint."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import ast
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_qual_summarize_structured as base  # noqa: E402  (names only: no test class is imported, so no test runs twice)

ss, summarize_check = base.ss, base.summarize_check


def num(path, text):
    return {"path": path, "text": "\n".join("{:>4}\t{}".format(n, line) for n, line in enumerate(text.split("\n"), 1))}


OLD = '''"""old"""
LIMIT = 5
NAME = "a"
DEFAULTS = {
    "workers": 2,
    "mode": "x",
    "gone": 1,
}
COMPUTED = len("ab")
BOTH = len("ab")


def keep(a):
    helper(a)
    return old_call(a)


def removed():
    pass


class Box:
    def put(self, x):
        return x

OLDONLY = 7
'''
NEW = '''"""new"""
LIMIT = 9
NAME = "a"
DEFAULTS = {
    "workers": 4,
    "mode": "x",
    "extra": 3,
}
COMPUTED = len("abc")
BOTH = len("ab")
ADDED = 1


def keep(a):
    helper(a)
    return new_call(a)


def fresh():
    pass


class Box:
    def put(self, x):
        return x

    def take(self):
        pass
'''


class VersionPairsTests(unittest.TestCase):
    def pairs(self, *paths):
        return [(a["path"], b["path"]) for a, b in ss.version_pairs([num(p, "x = 1") for p in paths])]

    def test_rows(self):
        self.assertEqual(self.pairs("src/settings.py", "src/settings_v2.py"), [("src/settings.py", "src/settings_v2.py")])
        self.assertEqual(self.pairs("a2.py", "a.py"), [("a.py", "a2.py")])
        self.assertEqual(self.pairs("a_v3.py", "a_v2.py"), [("a_v2.py", "a_v3.py")])
        self.assertEqual(self.pairs("cache.py", "jobq.py", "src/cache.py"), [])
        self.assertEqual(self.pairs("a.py", "a2.md", "a2.txt"), [])
        self.assertEqual(self.pairs("a2.py", "a2.py"), [])

    def test_directories_and_a_zero_suffix(self):
        self.assertEqual(self.pairs("old/a.py", "new/a2.py"), [("old/a.py", "new/a2.py")])
        self.assertEqual(self.pairs("proj/after/a.py", "proj/before/a.py"), [("proj/before/a.py", "proj/after/a.py")])
        self.assertEqual(self.pairs("v2/a.py", "v1/a.py"), [("v1/a.py", "v2/a.py")])
        self.assertEqual(self.pairs("a_v0.py", "a.py"), [("a.py", "a_v0.py")])
        self.assertEqual(self.pairs("a.py", "a0.py"), [("a.py", "a0.py")])
        # unpaired: another stem, orders that disagree, a version directory against a plain one, other directories, two readings
        self.assertEqual(self.pairs("old/a.py", "new/b.py"), [])
        self.assertEqual(self.pairs("old/a2.py", "new/a.py"), [])
        self.assertEqual(self.pairs("old/a.py", "a2.py"), [])
        self.assertEqual(self.pairs("lib/a.py", "src/a2.py"), [])
        self.assertEqual(self.pairs("a.py", "a_v0.py", "a_v1.py"), [("a_v0.py", "a_v1.py")])


class VersionDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = ss.version_diff(num("old/m.py", OLD), num("new/m_v2.py", NEW))

    def test_exact_lines_in_order(self):
        self.assertEqual(self.lines, [
            "- m_v2.py adds the constant ADDED [new/m_v2.py:11]",
            "- m_v2.py adds the function fresh [new/m_v2.py:19]",
            "- m_v2.py adds the method take [new/m_v2.py:27]",
            "- m_v2.py drops the function removed that m.py defines [old/m.py:18]",
            "- m_v2.py drops the constant OLDONLY that m.py defines [old/m.py:26]",
            "- m_v2.py changes LIMIT from 5 to 9 [old/m.py:2, new/m_v2.py:2]",
            '- m_v2.py changes DEFAULTS["workers"] from 2 to 4 [old/m.py:5, new/m_v2.py:5]',
            '- m_v2.py adds the key DEFAULTS["extra"] [new/m_v2.py:7]',
            '- m_v2.py drops the key DEFAULTS["gone"] that m.py defines [old/m.py:7]',
            "- in m_v2.py, keep now calls new_call [new/m_v2.py:16]",
            "- in m_v2.py, keep no longer calls old_call [old/m.py:15]",
        ])

    def test_non_literal_and_unchanged_not_compared(self):
        text = "\n".join(self.lines)
        for absent in ("COMPUTED", "BOTH", "NAME", "helper", "mode"):
            self.assertNotIn(absent, text)

    def diff(self, old, new):
        return ss.version_diff(num("a.py", old), num("a2.py", new))

    def test_equal_values_give_no_change(self):
        self.assertEqual(self.diff("D = {'x': {'a': 1, 'b': 2}}", "D = {'x': {'b': 2, 'a': 1}}"), [])
        self.assertEqual(self.diff("D = {'a': 1, 'b': [1, 2]}", "D = {'b': [1, 2], 'a': 1}"), [])
        self.assertEqual(self.diff("S = {1, 2}", "S = {2, 1}"), [])
        self.assertEqual(self.diff("T = (1, 'a')", 'T = (1, "a")'), [])
        # a change is still one, also where == holds (1 and True), or the value is nested, or a list changes order
        self.assertEqual(self.diff("X = 1", "X = True"), ["- a2.py changes X from 1 to True [a.py:1, a2.py:1]"])
        self.assertEqual(self.diff("D = {'x': {'a': 1}}", "D = {'x': {'a': 2}}"),
                         ['- a2.py changes D["x"] from {\'a\': 1} to {\'a\': 2} [a.py:1, a2.py:1]'])
        self.assertEqual(self.diff("L = [1, 2]", "L = [2, 1]"), ["- a2.py changes L from [1, 2] to [2, 1] [a.py:1, a2.py:1]"])

    def test_calls_in_a_nested_scope_are_not_the_outer_functions(self):
        nested = "def outer():\n    def inner():\n        {0}()\n    key = lambda: {0}()\n    class C:\n        x = {0}()\n    return inner\n"
        self.assertEqual(self.diff(nested.format("old"), nested.format("new")), [])
        # what runs in the outer scope still counts: a decorator, a default value, a call after the nested def
        outer = "def outer():\n    @{0}1(1)\n    def inner(a={0}2()):\n        pass\n    {0}3()\n"
        self.assertEqual(self.diff(outer.format("old"), outer.format("new")), [
            "- in a2.py, outer now calls new1 [a2.py:2]", "- in a2.py, outer now calls new2 [a2.py:3]",
            "- in a2.py, outer now calls new3 [a2.py:5]", "- in a2.py, outer no longer calls old1 [a.py:2]",
            "- in a2.py, outer no longer calls old2 [a.py:3]", "- in a2.py, outer no longer calls old3 [a.py:5]"])


class UseScanTests(unittest.TestCase):
    TARGET = "class Q:\n    def __init__(self):\n        self.items = []\n\n    def pop(self):\n        return 1\n\n\ndef run_it():\n    pass\n"
    USER = ("from tgt import Q\n\n\ndef go(q):\n    own = Q()\n    own.pop()\n    q.pop()\n    q.nope()\n    q.items\n")
    MODULE = "import tgt\n\nx = tgt.Q()\ny = tgt.run_it()\n"
    OTHER = "x = 1\n"
    FILES = [num("tgt.py", TARGET), num("user.py", USER), num("mod.py", MODULE), num("other.py", OTHER)]

    def test_class_lines(self):
        self.assertEqual(ss.use_scan("Who uses Q?", self.FILES), [
            "- user.py imports Q [user.py:1]",
            "- user.py creates a Q [user.py:5]",
            "- user.py uses Q.pop on own [user.py:6]",
            "- user.py uses Q.pop on q [user.py:7]",
            "- user.py uses Q.items on q [user.py:9]",
            "- mod.py imports Q [mod.py:3]",
            "- mod.py creates a Q [mod.py:3]",
            "- other.py does not use Q [other.py:1]",
        ])

    def test_function_target(self):
        self.assertEqual(ss.use_scan("Who calls run_it?", self.FILES), [
            "- user.py does not use run_it [user.py:1]",
            "- mod.py imports run_it [mod.py:4]",
            "- mod.py calls run_it [mod.py:4]",
            "- other.py does not use run_it [other.py:1]",
        ])

    def test_nothing_without_a_defined_name_in_the_task(self):
        self.assertEqual(ss.use_scan("Who uses Queue or q? Q2 and pop are not classes", self.FILES), [])
        self.assertEqual(ss.use_scan("", self.FILES), [])

    def scan(self, user, target=TARGET, name="Q"):
        return ss.use_scan("Who uses {}?".format(name), [num("t.py", target), num("u.py", user)])

    def test_star_import_and_dunder_methods(self):
        self.assertEqual(self.scan("from t import *\nQ()\n"), ["- u.py creates a Q [u.py:2]"])
        self.assertEqual(self.scan("def use(q):\n    q.__init__()\n"), ["- u.py uses Q.__init__ on q [u.py:2]"])
        enter = "class Q:\n    def __enter__(self):\n        return self\n"
        self.assertEqual(self.scan("from t import Q\nc = Q()\nc.__enter__()\n", enter),
                         ["- u.py imports Q [u.py:1]", "- u.py creates a Q [u.py:2]", "- u.py uses Q.__enter__ on c [u.py:3]"])
        # a star import that does not export the name is not a use of it
        hidden = self.TARGET + '__all__ = ["run_it"]\n'
        self.assertEqual(self.scan("from t import *\nQ()\n", hidden), [])

    def test_undecided_gives_no_absence_line(self):
        for label, user in (("a bare Name no import explains", "class Sub(Q):\n    pass\n"),
                            ("from another module", "from pkg import Q\n"),
                            ("a star import of another module", "from pkg import *\nQ()\n"),
                            ("a star import of the module, name only referenced", "from t import *\nclass Sub(Q):\n    pass\n"),
                            ("a module attribute no import explains", "x = cfg.Q\n"),
                            ("the name as a string", "x = getattr(t, 'Q')\n")):
            with self.subTest(label):
                self.assertEqual([l for l in self.scan(user) if "does not use" in l], [])
        # decided: no mention of the name at all, even with a star import of another module
        self.assertEqual(self.scan("from pkg import *\nx = 1\n"), ["- u.py does not use Q [u.py:1]"])

    def test_duplicate_definitions_keep_the_unambiguous_files(self):
        files = [num("t.py", "class Q:\n    def pop(self):\n        pass\n"),
                 num("t_v2.py", "class Q:\n    def pop(self):\n        pass\n\n    def push(self):\n        pass\n"),
                 num("u.py", "from t import Q\nQ()\n"),
                 num("w.py", "import t_v2\nx = t_v2.Q()\nx.push()\n"),
                 num("z.py", "x = 1\n"),
                 num("both.py", "from t import Q\nfrom t_v2 import Q\nQ()\n"),
                 num("param.py", "def f(q):\n    q.pop()\n"),
                 num("param2.py", "from t_v2 import Q\n\n\ndef f(q):\n    q.push()\n")]
        self.assertEqual(ss.use_scan("Who uses Q?", files), [
            "- u.py imports t.Q [u.py:1]", "- u.py creates a t.Q [u.py:2]",
            "- w.py does not use t.Q [w.py:1]", "- z.py does not use t.Q [z.py:1]", "- param2.py does not use t.Q [param2.py:1]",
            "- u.py does not use t_v2.Q [u.py:1]",
            "- w.py imports t_v2.Q [w.py:2]", "- w.py creates a t_v2.Q [w.py:2]", "- w.py uses t_v2.Q.push on x [w.py:3]",
            "- z.py does not use t_v2.Q [z.py:1]",
            "- param2.py imports t_v2.Q [param2.py:1]", "- param2.py uses t_v2.Q.push on q [param2.py:5]"])


class FalseAbsenceTests(unittest.TestCase):
    FILES = {"src/jobq.py": 50, "src/cli.py": 40}
    DEFINED = {"src/jobq.py": {"JobQueue", "pop", "backoff_for", "attempt", "Q", "use", "ab", "file"}, "src/cli.py": {"main"}}

    def run_fact(self, subject, claim):
        return ss.validate([{"subject": subject, "claim": claim, "path": "src/cli.py", "lines": [3]}], self.FILES, self.DEFINED)

    def test_each_phrase_drops_a_defined_name(self):
        for claim in ("is not in the files", "is NOT IN THE FILES", "is not defined", "does not define it", "does not exist", "is absent",
                      "is not in jobq.py", "is not in src/jobq.py"):
            with self.subTest(claim):
                kept, drops = self.run_fact("JobQueue", claim)
                self.assertEqual((kept, [d["reason"] for d in drops]), ([], ["false_absence"]))

    def reasons(self, subject, claim):
        return [d["reason"] for d in self.run_fact(subject, claim)[1]]

    def test_name_in_claim_token_and_backticks(self):
        self.assertEqual(self.reasons("x", "backoff_for is absent"), ["false_absence"])
        self.assertEqual(self.reasons("x", "does not define `pop`"), ["false_absence"])

    def test_a_file_subject_scopes_the_claim_to_that_file(self):
        self.assertEqual(self.reasons("cli.py", "does not define `pop`"), [])  # cli.py defines only main: true
        self.assertEqual(self.reasons("src/cli.py", "does not define pop"), [])
        self.assertEqual(self.reasons("cli.py", "does not define `main`"), ["false_absence"])
        self.assertEqual(self.reasons("jobq.py", "does not define `pop`"), ["false_absence"])
        self.assertEqual(self.reasons("src/jobq.py", "does not define `pop`"), ["false_absence"])

    def test_bare_words_are_names_unless_ordinary(self):
        for subject, claim in (("x", "Q does not exist"), ("x", "attempt is absent"), ("x", "the attempt is absent"),
                               ("a retry loop", "Q is not defined"), ("x", "`ab` is absent"), ("x", "the `use` is absent"),
                               ("x", "the `file` is absent")):
            with self.subTest(claim):
                self.assertEqual(self.reasons(subject, claim), ["false_absence"])
        # defined names that are also stop words or short lowercase words, bare: not names; a word nothing defines: not a name
        for subject, claim in (("x", "the file is absent"), ("x", "use is absent"), ("a retry loop", "ab is absent"),
                               ("x", "the queue is absent")):
            with self.subTest(claim):
                self.assertEqual(self.reasons(subject, claim), [])

    def test_kept(self):
        for label, subject, claim in (("true absence", "RetryPolicy", "is not in the files"), ("no phrase", "JobQueue", "holds the heap"),
                                      ("named file lacks it", "JobQueue", "is not in cli.py")):
            with self.subTest(label):
                kept, drops = self.run_fact(subject, claim)
                self.assertEqual((len(kept), drops), (1, []))

    def test_older_fact_form(self):
        old = {"claim": "JobQueue is not in the files", "path": "src/cli.py", "line_start": 1, "line_end": 1}
        self.assertEqual([d["reason"] for d in ss.validate([old], self.FILES, self.DEFINED)[1]], ["false_absence"])
        self.assertEqual(len(ss.validate([old], self.FILES)[0]), 1)  # no names given: no drop


# code_facts is computed by code and sends nothing to a model, so the prepare budget does not apply to it. The guard walks every
# row, the real split included; the biggest row numbers to about 123000 chars (real-lane-lock-users), so the bound is far above.
GUARD_BUDGET_CHARS = 1000000


class GuardTests(unittest.TestCase):
    """Every diff and use line of the calibration chores cites a line that shows it (rule 3 and the line text)."""
    STOP = {"adds", "drops", "changes", "from", "that", "defines", "the", "now", "calls", "creates", "uses", "imports", "key",
            "function", "method", "constant", "class", "no", "longer", "does", "not", "use", "and", "for", "in", "to"}

    def check_lines(self, root, paths, task):
        """Run code_facts on the files and check every line it returns; the number of lines checked."""
        files = ss.prepare(root, paths, GUARD_BUDGET_CHARS)
        texts = {f["path"]: _plain(f) for f in files}
        diff, uses = ss.code_facts(task, files)
        for line in diff + uses:
            match = summarize_check.FACT_LINE.match(line)
            self.assertTrue(match, line)
            cites = [c.strip().rsplit(":", 1) for c in match.group("cites").split(",")]
            absent = " does not use " in line
            words = {w for w in re.findall(r"[A-Za-z_]\w{2,}", re.sub(r"^\S+ ", "", match.group("claim"))) if w not in self.STOP}
            shown = False
            for path, n in cites:
                self.assertIsNone(summarize_check._cite_problem(root, paths, path, int(n), int(n)), line)
                shown = shown or any(w in texts[path][int(n) - 1] for w in words)
            self.assertTrue(absent and cites == [[cites[0][0], "1"]] or shown, line)
        return len(diff) + len(uses)

    def test_calibration_lines_cite_their_line(self):
        root = base.DATA / "fixtures"
        checked = {}
        rows = [json.loads(l) for l in (base.DATA / "chores.jsonl").read_text().splitlines()]
        self.assertEqual({r["split"] for r in rows}, {"tune", "holdout", "real"})
        for row in rows:
            n = self.check_lines(root, row["files"], row["task"])
            checked[row["split"]] = checked.get(row["split"], 0) + n
        print("guard lines checked per split:", dict(sorted(checked.items())))
        self.assertGreater(sum(checked.values()), 10)

    def test_real_files_cite_their_line_when_their_names_are_asked_about(self):
        """The real rows ask no question about a name, so code_facts says nothing for them; ask about every top-level name."""
        root = base.DATA / "fixtures"
        checked = 0
        for row in map(json.loads, (base.DATA / "chores.jsonl").read_text().splitlines()):
            if row["split"] != "real":
                continue
            names = set()
            for f in ss.prepare(root, row["files"], GUARD_BUDGET_CHARS):
                if f["path"].endswith(".py"):
                    tree = ast.parse("\n".join(_plain(f)))
                    names.update(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
            if names:
                checked += self.check_lines(root, row["files"], "Who uses " + ", ".join("`{}`".format(n) for n in sorted(names)) + "?")
        self.assertGreater(checked, 0)


def _plain(f):
    return [l.split("\t", 1)[1] if "\t" in l else "" for l in f["text"].split("\n")]


class SummarizeStubTests(base.StubCase):
    def test_answer_order_dedup_code_facts_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "m.py").write_text(OLD)
            (root / "m_v2.py").write_text(NEW)
            (root / "gad.py").write_text("class Gadget:\n    def put(self, x):\n        return x\n")
            (root / "use.py").write_text("from gad import Gadget\nb = Gadget()\nb.put(1)\n")
            diff_line = '- m_v2.py changes LIMIT from 5 to 9 [m.py:2, m_v2.py:2]'
            facts = [{"subject": "LIMIT", "claim": "is raised", "path": "m_v2.py", "lines": [2]},
                     {"subject": "Gadget", "claim": "is not defined", "path": "m.py", "lines": [1]}]
            self.stub.reply(json.dumps({"facts": facts}))
            settings = {"system": "s", "schema": {"type": "object"}}
            text, record = ss.summarize(root, ["m.py", "m_v2.py", "gad.py", "use.py"], "What changed, and who uses Gadget?",
                                        endpoint=self.stub.url, model="m", settings=settings)
            expected_diff, expected_uses = ss.code_facts("What changed, and who uses Gadget?", ss.prepare(root, ["m.py", "m_v2.py", "gad.py", "use.py"]))
        self.assertTrue(expected_diff and expected_uses)
        lines = text.split("\n")
        first = {k: next(i for i, l in enumerate(lines) if v in l) for k, v in
                 (("index", "defines the class Gadget"), ("diff", diff_line), ("use", "use.py creates a Gadget"), ("fact", "LIMIT is raised"))}
        self.assertEqual(sorted(first, key=first.get), ["index", "diff", "use", "fact"])
        self.assertEqual(len(lines), len(set(lines)))
        self.assertEqual(record["code_facts"], len(expected_diff) + len(expected_uses))
        self.assertIn("- use.py uses Gadget.put on b [use.py:3]", lines)
        self.assertEqual([d["reason"] for d in record["drops"]], ["false_absence"])
        sent = self.stub.requests[0]["body"]["messages"][1]["content"]
        self.assertIn("Differences found by code:\n- m_v2.py adds", sent)
        self.assertIn("Uses found by code:\n- m.py does not use Gadget", sent)
        self.assertIn("Explain what each difference or use means for the task; these lines are added to your answer for you.", sent)

    def test_dedup_of_a_model_fact_equal_to_a_code_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "t.py").write_text("class A:\n    pass\n")
            (root / "u.py").write_text("x = 1\n")
            self.stub.reply(json.dumps({"facts": [{"subject": "u.py", "claim": "does not use A", "path": "u.py", "lines": [1]}]}))
            text, record = ss.summarize(root, ["t.py", "u.py"], "Who uses A?", endpoint=self.stub.url, model="m",
                                        settings={"system": "s", "schema": {"type": "object"}})
        self.assertEqual(text.split("\n").count("- u.py does not use A [u.py:1]"), 1)
        self.assertEqual((record["code_facts"], record["kept"]), (1, 1))

    def test_no_blocks_without_facts(self):
        files = [num("a.py", "x = 1")]
        self.assertNotIn("found by code", ss.user_message("anything", files))


if __name__ == "__main__":
    unittest.main()
