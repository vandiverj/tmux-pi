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

    def test_a_name_with_no_single_value_or_definition_states_nothing(self):
        # bound twice, augmented, declared global: the source does not show which value or def is the file's
        self.assertEqual(self.diff("X = 1\nX = 2", "X = 2"), [])
        self.assertEqual(self.diff("X = 1\nX += 1", "X = 2"), [])
        self.assertEqual(self.diff("X = 1\nif c:\n    X = 2", "X = 2"), [])
        self.assertEqual(self.diff("X = 1\ndef f():\n    global X\n    X = 5\n", "X = 5\ndef f():\n    pass\n"), [])
        self.assertEqual(self.diff("def f():\n    a()\ndef f():\n    b()", "def f():\n    b()"), [])
        prop = "class C:\n    @property\n    def x(self):\n        return {0}()\n    @x.setter\n    def x(self, v):\n        pass\n"
        self.assertEqual(self.diff(prop.format("old"), prop.format("new")), [])
        # a function the other file defines under an if or try is not dropped or added; one bound once still is
        self.assertEqual(self.diff("def f():\n    pass\n", "if 1:\n    def f():\n        pass\n"), [])
        self.assertEqual(self.diff("try:\n    from m import g\nexcept ImportError:\n    g = None\n", "def g():\n    pass\n"), [])
        self.assertEqual(self.diff("X = 1", "X = 2\ndef f():\n    pass\n"),
                         ["- a2.py adds the function f [a2.py:2]", "- a2.py changes X from 1 to 2 [a.py:1, a2.py:1]"])


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

    def test_star_import_attributes_nothing_and_dunder_methods(self):
        # a star import proves nothing about what Q is (a later write or another star import could replace it)
        self.assertEqual(self.scan("from t import *\nQ()\n"), [])
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

    def test_a_rebound_name_keeps_only_the_import(self):
        imp = "- u.py imports Q [u.py:1]"
        for label, user, lines in (("assigned after the import", "from t import Q\nQ = other\nQ()\n", [imp]),
                                   ("an import fallback", "try:\n    from t import Q\nexcept ImportError:\n    Q = None\nQ()\n",
                                    ["- u.py imports Q [u.py:2]"]),
                                   ("a second import of the name", "from t import Q\nfrom mid import Q\nQ()\n", [imp]),
                                   ("a star import of another module after it", "from t import Q\nfrom mid import *\nQ()\n", [imp]),
                                   ("two star imports", "from t import *\nfrom mid import *\nQ()\n", []),
                                   ("a star import, then assigned", "from t import *\nQ = 3\nQ()\n", []),
                                   ("the module rebound", "import t\nt = other\nt.Q()\n", [])):
            with self.subTest(label):
                self.assertEqual(self.scan(user), lines)

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


# ---- LS1c S1c round 3: a fact is stated only when the code proves the binding is single and simple -----------------------------
# Each row is one more way a file can bind or write a name ({n} is the name): the forms of the brief, one by one, so a missing form shows.
# The last field says the form needs `match` (Python 3.10+; older parsers read the file as not parseable and say nothing anyway).
MATCH = sys.version_info >= (3, 10)
FORMS = [
    ("assignment", "{n} = 7", False),
    ("attribute write to it", "{n}.field = 7", False),
    ("attribute with its name", "obj.{n} = 7", False),
    ("subscript write to it", "{n}['k'] = 7", False),
    ("augmented assignment", "{n} += 1", False),
    ("annotated assignment", "{n}: int = 7", False),
    ("annotation only", "{n}: int", False),
    ("tuple target", "{n}, other = 7, 8", False),
    ("starred target", "first, *{n} = [7, 8]", False),
    ("walrus", "({n} := 7)", False),
    ("for target", "for {n} in [1]:\n    pass", False),
    ("with as", "with ctx() as {n}:\n    pass", False),
    ("except as", "try:\n    pass\nexcept Exception as {n}:\n    pass", False),
    ("match capture", "match 1:\n    case {n}:\n        pass", True),
    ("match star pattern", "match 1:\n    case [1, *{n}]:\n        pass", True),
    ("match mapping rest", "match 1:\n    case {{'k': 1, **{n}}}:\n        pass", True),
    ("match as", "match 1:\n    case int() as {n}:\n        pass", True),
    ("import", "import {n}", False),
    ("import as", "import other as {n}", False),
    ("from import", "from m import {n}", False),
    ("dotted import", "import {n}.child", False),
    ("del", "del {n}", False),
    ("def", "def {n}():\n    pass", False),
    ("async def", "async def {n}():\n    pass", False),
    ("class", "class {n}:\n    pass", False),
    ("under if", "if cond:\n    {n} = 7", False),
    ("under try", "try:\n    {n} = 7\nexcept ValueError:\n    pass", False),
]
# a global or nonlocal in a function reaches the module's name; a star import is not about a name at all
MODULE_FORMS = FORMS + [
    ("global declaration", "def g():\n    global {n}", False),
    ("nonlocal declaration", "def g():\n    {n} = 1\n    def h():\n        nonlocal {n}", False),
    ("star import", "from m import *", False),
]
# a class member is bound in the class body: a declaration there, or a write through self or the class, reaches it
CLASS_FORMS = FORMS + [
    ("global declaration in the class body", "global {n}", False),
    ("self attribute in another method", "def other(self):\n    self.{n} = 1", False),
]
COMPOUND = {"if": ("if cond:", ""), "try": ("try:", "except ImportError:\n    pass"), "with": ("with ctx():", ""),
            "for": ("for i in items:", ""), "while": ("while cond:", ""), "match": ("match cond:\n    case 1:", "")}


def indented(text, pad):
    return "\n".join(pad + line for line in text.split("\n"))


def form_text(snippet, name, pad=""):
    return indented(snippet.format(n=name), pad)


def under(head, text):
    """text as the body of a compound statement."""
    opening, closing = COMPOUND[head]
    return opening + "\n" + indented(text, " " * (8 if head == "match" else 4)) + ("\n" + closing if closing else "")


def mentions(line, name):
    return re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", line) is not None


# the clean pair: constants X (changed), ADD (added), GONE (dropped); functions f (calls changed), add, gone; methods put (calls
# changed), take, give. {cls} is the end of the class body, {mod} the end of the module.
PAIR = {
    "old": "X = 1\nGONE = 1\n\n\ndef f():\n    a()\n\n\ndef gone():\n    pass\n\n\nclass C:\n    def put(self):\n        a()\n\n    def give(self):\n        pass\n{cls}\n{mod}\n",
    "new": "X = 2\nADD = 1\n\n\ndef f():\n    b()\n\n\ndef add():\n    pass\n\n\nclass C:\n    def put(self):\n        b()\n\n    def take(self):\n        pass\n{cls}\n{mod}\n",
}
PAIR_NAMES = ("X", "ADD", "GONE", "f", "add", "gone", "put", "take", "give")


def pair_diff(side=None, cls="", mod=""):
    """version_diff of the clean pair, with the text cls added to the class body and mod to the module of one side ("old" or "new")."""
    texts = {s: PAIR[s].format(cls=cls if s == side else "", mod=mod if s == side else "") for s in PAIR}
    for text in texts.values():
        ast.parse(text)  # a form that is a syntax error would test nothing
    return ss.version_diff(num("a.py", texts["old"]), num("a2.py", texts["new"]))


class BindingProofDiffTests(unittest.TestCase):
    """A name is compared across two versions only when each file proves it bound once, simply (_single_binding), and added or
    dropped only when one file proves it bound once and the other proves it not bound at all."""

    def test_the_clean_pair_states_every_claim(self):
        clean = pair_diff()
        for name in PAIR_NAMES:
            with self.subTest(name):
                self.assertTrue(any(mentions(line, name) for line in clean), clean)

    def test_each_extra_binding_of_a_module_name_silences_it(self):
        for name in ("X", "ADD", "GONE", "f", "add", "gone", "C"):
            about = ("put", "take", "give") if name == "C" else (name,)  # a class that is rebound says nothing about its methods
            for label, snippet, needs_match in MODULE_FORMS:
                for side in ("old", "new"):
                    if needs_match and not MATCH:
                        continue
                    if label == "assignment" and (name, side) in (("ADD", "old"), ("GONE", "new")):
                        continue  # one plain `NAME = 7` in the file that lacked it makes the name single in both: a real change
                    with self.subTest(name=name, form=label, side=side):
                        out = pair_diff(side, mod=form_text(snippet, name))
                        self.assertEqual([line for line in out if any(mentions(line, w) for w in about)], [])

    def test_each_extra_binding_of_a_class_member_silences_it(self):
        for name in ("put", "take", "give"):
            for label, snippet, needs_match in CLASS_FORMS + [("write through the class", "C.{n} = other", False)]:
                for side in ("old", "new"):
                    if needs_match and not MATCH:
                        continue
                    with self.subTest(name=name, form=label, side=side):
                        if label == "write through the class":
                            out = pair_diff(side, mod=form_text(snippet, name))
                        else:
                            out = pair_diff(side, cls=form_text(snippet, name, "    "))
                        self.assertEqual([line for line in out if mentions(line, name)], [])

    def test_a_binding_under_a_compound_statement_is_not_single_and_simple(self):
        plain = {"X": ("X = 1", "X = 2"), "f": ("def f():\n    a()", "def f():\n    b()")}
        for head in COMPOUND:
            if head == "match" and not MATCH:
                continue
            for name, (old, new) in plain.items():
                for side in ("old", "new"):
                    with self.subTest(name=name, head=head, side=side):
                        texts = (under(head, old), new) if side == "old" else (old, under(head, new))
                        for text in texts:
                            ast.parse(text)
                        out = ss.version_diff(num("a.py", texts[0]), num("a2.py", texts[1]))
                        self.assertEqual([line for line in out if mentions(line, name)], [])
            with self.subTest(name="put", head=head):  # a method under a compound statement of its class body
                method = "def put(self):\n    {}()"
                wrapped = "class C:\n" + indented(under(head, method.format("b")), "    ")
                plain_old = "class C:\n" + indented(method.format("a"), "    ")
                ast.parse(wrapped)
                self.assertEqual(ss.version_diff(num("a.py", plain_old), num("a2.py", wrapped)), [])
                self.assertEqual(ss.version_diff(num("a.py", wrapped), num("a2.py", plain_old)), [])
        # a definition under an if in the new file only is not an addition either; the same definition at module level is
        self.assertEqual(ss.version_diff(num("a.py", "pass"), num("a2.py", "if cond:\n    def f():\n        pass")), [])
        self.assertEqual(ss.version_diff(num("a.py", "pass"), num("a2.py", "def f():\n    pass")), ["- a2.py adds the function f [a2.py:1]"])

    def test_the_review_reproductions(self):
        def diff(old, new):
            return ss.version_diff(num("a.py", old), num("a2.py", new))
        # F2: a method rewritten by a class attribute, and a method moved under a class-level if
        self.assertEqual(diff("class C:\n    def f(self):\n        old()\n    f = None", "class C:\n    def f(self):\n        new()\n    f = None"), [])
        self.assertEqual(diff("class C:\n    def f(self):\n        pass", "class C:\n    if True:\n        def f(self):\n            pass"), [])
        # F3: a star import, a match capture and a subscript write change what the constant is
        self.assertEqual(diff("X = 1\nfrom m import *", "X = 2\nfrom m import *"), [])
        self.assertEqual(diff('D = {"x": 1}\nD["x"] = 2', 'D = {"x": 2}'), [])
        if MATCH:
            self.assertEqual(diff("X = 1\nmatch 2:\n    case X:\n        pass", "X = 2"), [])
        # the clean cases still give their lines
        self.assertEqual(diff("X = 1", "X = 2"), ["- a2.py changes X from 1 to 2 [a.py:1, a2.py:1]"])
        self.assertEqual(diff("def f():\n    a()", "def f():\n    b()"),
                         ["- in a2.py, f now calls b [a2.py:2]", "- in a2.py, f no longer calls a [a.py:2]"])
        self.assertEqual(diff("class C:\n    def f(self):\n        a()", "class C:\n    def f(self):\n        b()"),
                         ["- in a2.py, f now calls b [a2.py:3]", "- in a2.py, f no longer calls a [a.py:3]"])


class BindingProofUseTests(unittest.TestCase):
    """A call, creation or use is attributed to the target only through an import that resolves to it by its full dotted path and a
    name nothing else binds, writes or shadows."""
    FILES = [num("pkg/t.py", "class Q:\n    def pop(self):\n        return 1\n")]

    def scan(self, text, path="app/u.py"):
        return ss.use_scan("Who uses Q?", self.FILES + [num(path, text)])

    def test_resolution_by_full_dotted_path(self):
        both = ["- u.py imports Q [{p}:{i}]", "- u.py creates a Q [{p}:{c}]"]
        for label, path, text, imp, call in (
                ("from pkg.t import Q", "app/u.py", "from pkg.t import Q\nQ()\n", 1, 2),
                ("import pkg.t", "app/u.py", "import pkg.t\npkg.t.Q()\n", 2, 2),
                ("import pkg.t as t", "app/u.py", "import pkg.t as t\nt.Q()\n", 2, 2),
                ("from pkg import t", "app/u.py", "from pkg import t\nt.Q()\n", 2, 2),
                ("relative from .t", "pkg/u.py", "from .t import Q\nQ()\n", 1, 2),
                ("relative from . import t", "pkg/u.py", "from . import t\nt.Q()\n", 2, 2)):
            with self.subTest(label):
                self.assertEqual(self.scan(text, path), [line.format(p=path, i=imp, c=call) for line in both])
        # a module that only ends in t is not pkg.t (F4); neither is a bare t, a relative one from another package, one above the root
        for label, text in (("unrelated.t", "from unrelated.t import Q\nQ()\n"), ("unrelated t attribute", "from unrelated import t\nt.Q()\n"),
                            ("bare from t", "from t import Q\nQ()\n"), ("bare import t", "import t\nt.Q()\n"),
                            ("relative in another package", "from .t import Q\nQ()\n"), ("relative above the root", "from ... import t\nt.Q()\n"),
                            # the dotted import binds pkg: a second import of pkg may be another module
                            ("pkg bound twice", "import pkg.t\nimport pkg.other\npkg.t.Q()\n")):
            with self.subTest(label):
                self.assertEqual(self.scan(text), [])

    def test_the_review_reproductions(self):
        # F1: the module overwritten by a star import, the name by a dotted import, the attribute by a write
        self.assertEqual(self.scan("import pkg.t as t\nfrom mid import *\nt.Q()\n"), [])
        self.assertEqual(self.scan("from pkg.t import Q\nimport Q.child\nQ()\n"), ["- u.py imports Q [app/u.py:1]"])
        self.assertEqual(self.scan("import pkg.t as t\nt.Q = other\nt.Q()\n"), [])
        self.assertEqual(self.scan("import pkg.t\npkg.t.Q = other\npkg.t.Q()\n"), [])
        self.assertEqual(self.scan("from pkg.t import Q\nQ.pop = other\nQ()\n"), ["- u.py imports Q [app/u.py:1]"])

    def test_an_import_under_a_compound_statement_or_shadowed_in_a_function_attributes_no_call(self):
        for head in COMPOUND:
            if head == "match" and not MATCH:
                continue
            with self.subTest(head):
                text = under(head, "from pkg.t import Q") + "\nQ()\n"
                ast.parse(text)
                out = self.scan(text)
                self.assertEqual([line for line in out if "imports" not in line], [])
        for label, text in (("a parameter", "from pkg.t import Q\n\n\ndef f(Q):\n    Q()\n"),
                            ("a lambda parameter", "from pkg.t import Q\ng = lambda Q: Q()\n"),
                            ("a def in a function", "from pkg.t import Q\n\n\ndef f():\n    def Q():\n        pass\n    Q()\n")):
            with self.subTest(label):
                self.assertEqual([line for line in self.scan(text) if "imports" not in line], [])

    def test_each_extra_binding_silences_the_calls_through_a_name(self):
        # with no extra binding every line is there
        self.assertEqual(self.scan("from pkg.t import Q\nx = Q()\nx.pop()\n"), [
            "- u.py imports Q [app/u.py:1]", "- u.py creates a Q [app/u.py:2]", "- u.py uses Q.pop on x [app/u.py:3]"])
        self.assertEqual(self.scan("import pkg.t as t\ny = t.Q()\n"), ["- u.py imports Q [app/u.py:2]", "- u.py creates a Q [app/u.py:2]"])
        for label, snippet, needs_match in MODULE_FORMS:
            if needs_match and not MATCH:
                continue
            with self.subTest(form=label, name="Q, from an import"):  # the import line stays: the import itself is proven
                out = self.scan("from pkg.t import Q\n" + form_text(snippet, "Q") + "\nx = Q()\nx.pop()\n")
                self.assertEqual([line for line in out if "imports" not in line], [])
            with self.subTest(form=label, name="t, a module alias"):
                self.assertEqual(self.scan("import pkg.t as t\n" + form_text(snippet, "t") + "\ny = t.Q()\n"), [])
            if label != "star import":  # which already silences Q, and with it the use through x
                with self.subTest(form=label, name="x, a variable bound from Q()"):
                    out = self.scan("from pkg.t import Q\n" + form_text(snippet, "x") + "\nx = Q()\nx.pop()\n")
                    self.assertEqual([line for line in out if "uses" in line], [])

    def test_a_target_bound_twice_is_not_scanned(self):
        for target in ("class Q:\n    pass\nQ = other\n", "if cond:\n    class Q:\n        pass\n"):
            with self.subTest(target):
                self.assertEqual(ss.use_scan("Who uses Q?", [num("pkg/t.py", target), num("app/u.py", "from pkg.t import Q\nQ()\n")]), [])


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
