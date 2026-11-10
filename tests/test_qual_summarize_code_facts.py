"""LS1c S1c on lib/summarize_structured.py: version_pairs, version_diff, use_scan, the false_absence drop, the user message blocks and
summarize's answer order. The fixtures here are written for the tests; the calibration files are only read by the guard. Stub endpoint."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import ast
import importlib.machinery
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
            "- in m_v2.py, keep now calls new_call [new/m_v2.py:16]",
            "- in m_v2.py, keep no longer calls old_call [old/m.py:15]",
        ])

    def test_non_literal_and_unchanged_not_compared(self):
        text = "\n".join(self.lines)
        # DEFAULTS is a dict: mutable, so it is not compared, key by key or otherwise
        for absent in ("COMPUTED", "BOTH", "NAME", "helper", "mode", "DEFAULTS", "workers"):
            self.assertNotIn(absent, text)

    def diff(self, old, new):
        return ss.version_diff(num("a.py", old), num("a2.py", new))

    def test_equal_values_give_no_change(self):
        self.assertEqual(self.diff("T = (1, 'a')", 'T = (1, "a")'), [])
        self.assertEqual(self.diff("T = ((1, 2), 'a')", "T = ((1, 2), 'a')"), [])
        # a change is still one, also where == holds (1 and True), or the value is nested in a tuple
        self.assertEqual(self.diff("X = 1", "X = True"), ["- a2.py changes X from 1 to True [a.py:1, a2.py:1]"])
        self.assertEqual(self.diff("T = (1, (2, 'a'))", "T = (1, (2, 'b'))"), ["- a2.py changes T from (1, (2, 'a')) to (1, (2, 'b')) [a.py:1, a2.py:1]"])
        self.assertEqual(self.diff("T = (1, 2)", "T = (2, 1)"), ["- a2.py changes T from (1, 2) to (2, 1) [a.py:1, a2.py:1]"])

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
        # user.py imports from tgt (it spells no run_it, but an import that resolves to the target is no proof of absence): no line
        self.assertEqual(ss.use_scan("Who calls run_it?", self.FILES), [
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
        # round 5 F2: no emitter of a "uses" line (a parameter, a variable bound from Q(), a call) goes past a star import, whatever it is for
        self.assertEqual(self.scan("from other import *\ndef f(q):\n    q.pop()\n"), [])
        self.assertEqual(self.scan("from other import *\ndef f(q):\n    q.pop()\n", self.TARGET.replace("Q", "W"), "W"), [])
        self.assertEqual(self.scan("def f(q):\n    q.pop()\nfrom other import *\n"), [])
        self.assertEqual(self.scan("from t import Q\nfrom other import *\nx = Q()\nx.pop()\ndef f(q):\n    q.pop()\n"), ["- u.py imports Q [u.py:1]"])
        self.assertEqual(self.scan("import t\nfrom other import *\nx = t.Q()\nx.pop()\n"), [])
        self.assertEqual(self.scan("def f(q):\n    q.pop()\n"), ["- u.py uses Q.pop on q [u.py:2]"])  # without the star import, the same parameter is a use

    def test_undecided_gives_no_absence_line(self):
        for label, user in (("a bare Name no import explains", "class Sub(Q):\n    pass\n"),
                            ("from another module", "from pkg import Q\n"),
                            ("a star import of another module", "from pkg import *\nQ()\n"),
                            ("a star import of the module, name only referenced", "from t import *\nclass Sub(Q):\n    pass\n"),
                            ("a module attribute no import explains", "x = cfg.Q\n"),
                            ("the name as a string", "x = getattr(t, 'Q')\n")):
            with self.subTest(label):
                self.assertEqual([l for l in self.scan(user) if "does not use" in l], [])
        # decided: no star import, no dynamic form, no import of the target's module or that cannot be resolved, and the name spelled nowhere
        for user in ("x = 1\n", "import os\nfrom pkg import helper\nx = os.sep\n", "from other.t import W\nx = W\n"):
            self.assertEqual(self.scan(user), ["- u.py does not use Q [u.py:1]"], user)

    def test_absence_needs_positive_proof(self):
        """Round 4 F2: "does not use" only when the file has no path to the name; each row is one way it might, and gives no line."""
        for label, user in (("a star import of the target", "from t import *\n"),
                            ("a star import of another module", "from pkg import *\nx = 1\n"),
                            ("a relative star import", "from . import *\nx = 1\n"),
                            ("an import of the target module, a name that is not Q", "from t import W\nx = W\n"),
                            ("an import of the target module as a whole", "import t\nx = 1\n"),
                            ("an unresolvable relative import", "from .t import W\nx = W\n"),
                            ("a dynamic form", "globals()\nx = 1\n"),
                            ("setattr", "x = 1\nsetattr(x, 'a', 2)\n"),
                            ("the name as an attribute", "x = cfg.Q\n"),
                            ("the name as a keyword", "f(Q=1)\n"),
                            ("the name inside a string", "x = 'make a Q now'\n"),
                            ("the name as a parameter", "def f(Q):\n    pass\n"),
                            ("an alias of the name", "from pkg import X as Q\n"),
                            # round 5 F3: absence is a test on the raw text, so any spelling of the name blocks it
                            ("the name in a comment", "x = 1  # TODO move Q\n"),
                            ("the name in a docstring", '"""Uses Q."""\nx = 1\n'),
                            ("the name in bytes", 'x = b"Q"\n'),
                            ("the name in a longer bytes", "x = b'make Q now'\n"),
                            ("the name in an f-string", "x = f'{1} Q'\n"),
                            ("the name in a dotted path", "x = 'pkg.t.Q'\n"),
                            ("the name in a type comment", "x = []  # type: list[Q]\n")):
            with self.subTest(label):
                self.assertEqual(self.scan(user), [])
        if MATCH:  # the keyword of a class pattern is stored in MatchClass.kwd_attrs, which no walk of names reaches
            self.assertEqual(self.scan("match obj:\n    case C(Q=x):\n        pass\n"), [])
        # a different identifier is not the name: the line stays
        for user in ("x = Q2\n", "x = QQ\n", "x = _Q\n", "x = qQ\n", "x = b'Q2'\n", "x = 1  # q\n", "def f(q):\n    pass\n"):
            self.assertEqual(self.scan(user), ["- u.py does not use Q [u.py:1]"], user)
        # the review's second reproduction, and a file with nothing that reaches the target still gets its line
        self.assertEqual(self.scan("from t import *\n"), [])
        self.assertEqual(self.scan("import json\nx = json.dumps(1)\n"), ["- u.py does not use Q [u.py:1]"])

    def test_relative_imports_resolve_only_as_python_does(self):
        """Round 4 F1: a relative import resolves by the importer's package; a module at the run root has none, and no level climbs
        above the root. Otherwise it resolves to nothing: no attribution and no absence line."""
        target = num("t.py", "class Q:\n    pass\n")
        for label, path, user in (("from .t at the run root", "u.py", "from .t import Q\nQ()\n"),
                                  ("from ..t from a package of one level", "pkg/u.py", "from ..t import Q\nQ()\n"),
                                  ("from . import t at the run root", "u.py", "from . import t\nt.Q()\n"),
                                  ("from .. import t from a package of one level", "pkg/u.py", "from .. import t\nt.Q()\n"),
                                  ("no name used, from .t at the run root", "u.py", "from .t import W\n"),
                                  ("no name used, above the root", "pkg/u.py", "from ..t import W\n")):
            with self.subTest(label):
                self.assertEqual(ss.use_scan("Who uses Q?", [target, num(path, user)]), [])
        # the same imports one level down resolve
        self.assertEqual(ss.use_scan("Who uses Q?", [num("pkg/t.py", "class Q:\n    pass\n"), num("pkg/sub/u.py", "from ..t import Q\nQ()\n")]),
                         ["- u.py imports Q [pkg/sub/u.py:1]", "- u.py creates a Q [pkg/sub/u.py:2]"])

    def test_each_dynamic_access_word_blocks_the_absence_line(self):
        """Round 5: a file whose text holds one of these words can reach a name it does not spell. One row per word, inserted into an
        otherwise clean file; the table must equal ss._DYNAMIC_ACCESS, so a word added there needs its row here."""
        words = ("getattr", "__getattribute__", "__import__", "importlib", "import_module", "sys.modules", "eval", "exec", "compile",
                 "globals", "locals", "vars", "__dict__", "attrgetter", "methodcaller", "runpy", "builtins", "__builtins__",
                 "pkgutil", "pickle", "marshal", "setattr", "delattr", "inspect", "getmembers", "__subclasses__", "gc")
        self.assertEqual(set(words), set(ss._DYNAMIC_ACCESS))
        self.assertEqual(self.scan("x = 1\n"), ["- u.py does not use Q [u.py:1]"])
        for word in words:
            with self.subTest(word):
                self.assertEqual(self.scan("x = 1\ny = {}\n".format(word)), [])
                self.assertEqual(self.scan("x = 1  # {}\n".format(word)), [])  # the raw text: a comment holds it too
        # the word is matched on identifier boundaries: a longer identifier is not the word
        for user in ("x = execute\n", "y = evaluate(1)\n", "z = my_vars\n", "g = gcd(1, 2)\n", "i = inspector\n"):
            self.assertEqual(self.scan(user), ["- u.py does not use Q [u.py:1]"], user)
        # the review's reproduction (F3): the name is built, never spelled
        files = [num("pkg/sub/t.py", "class Q:\n    pass\n"), num("pkg/sub/__init__.py", "from . import t\n"), num("pkg/__init__.py", ""),
                 num("u.py", "from pkg import sub\ngetattr(sub.t, chr(81))()\n")]
        self.assertEqual([l for l in ss.use_scan("Who uses Q?", files) if l.startswith("- u.py")], [])  # (the empty pkg/__init__.py does say it)

    def test_absence_needs_no_path_through_the_import_graph(self):
        """Round 5: the run root's import graph, with full dotted paths and relative imports resolved as Python does. An import that is
        the target module, an ancestor package of it or a submodule of it blocks the line, and so does any run-root module that reaches
        it (transitively); an ancestor package's __init__ reaches its submodules only if it imports them."""
        target = num("pkg/sub/t.py", "class Q:\n    pass\n")
        none = ["- u.py does not use Q [u.py:1]"]

        def scan(user, *others, at="u.py", target=target):
            files = [target] + [num(path, text) for path, text in others] + [num(at, user)]
            return [line for line in ss.use_scan("Who uses Q?", files) if line.startswith("- u.py")]

        h_pkg, h_mod = ("h/__init__.py", "x = 1\n"), ("h.py", "x = 1\n")

        for label, user, others in (
                ("the target by full path", "import pkg.sub.t\n", ()),
                ("the target from its package", "from pkg.sub import t\n", ()),
                ("an ancestor, from-import (review F3)", "from pkg import sub\nx = sub.t.make()\n", ()),
                ("an ancestor package", "import pkg\n", ()),
                ("an ancestor, dotted", "import pkg.sub\n", ()),
                ("a submodule of the target", "import pkg.sub.t.deeper\n", ()),
                ("another module of the ancestor", "import pkg.other\n", ()),
                ("two hops: u, h, t", "import h\n", (("h.py", "from pkg.sub import t\n"),)),
                ("three hops: u, h, g, t", "import h\n", (("h.py", "import g\n"), ("g.py", "import pkg.sub.t\n"))),
                ("through a from-import of a module", "from h import x\n", (("h.py", "import pkg.sub.t\nx = 1\n"),)),
                ("through a star import in h", "import h\n", (("h.py", "from pkg.sub.t import *\n"),)),
                ("through an import inside a function of h", "import h\n", (("h.py", "def f():\n    import pkg.sub.t\n"),)),
                ("through the __init__ of a package, which imports it", "import lib.x\n",
                 (("lib/__init__.py", "from pkg.sub import t\n"), ("lib/x.py", "y = 1\n"))),
                ("by the path below a directory on sys.path", "import worker\n", (("src/worker.py", "import pkg.sub.t\n"),)),
                ("a bare name that can be the target", "import t\n", ()),
                ("a relative import that cannot be resolved, in u", "from . import t\n", ()),
                ("a relative import above the root, in h", "import h\n", (("h.py", "from .. import z\n"),)),
                ("a module that does not parse", "import h\n", (("h.py", "def (:\n"),)),
                ("two files of one module name, the later harmless", "import h\n", (("h/__init__.py", "import pkg.sub.t\n"), ("h.py", "import os\n"))),
                ("a name no module accounts for, in a run-root package", "from pkg import zzz\n", ()),
                # round 6 F1: a name whose prefix is a run-root package or module but which is no module there cannot be resolved; a
                # known ancestor does not resolve it. The same at every hop.
                ("a missing submodule of a known package", "import h.missing\n", (h_pkg,)),
                ("a missing submodule of a known module", "import h.missing\n", (h_mod,)),
                ("a missing member of a known package (from-import)", "from h import missing\n", (h_pkg,)),
                ("a missing member of a known module (from-import)", "from h import missing\n", (h_mod,)),
                ("a missing dotted module of a known package, from-import", "from h.missing import f\n", (h_pkg,)),
                ("a member of a module that uses a dynamic form", "from h import x\n", (("h/__init__.py", "x = 1\nsetattr(a, 'b', 1)\n"),)),
                ("transitive: u, helper, a missing submodule", "import helper\n", (h_pkg, ("helper.py", "import h.missing\n"))),
                ("transitive: u, helper, a missing member", "import helper\n", (h_pkg, ("helper.py", "from h import missing\n"))),
                ("transitive: u, helper, helper2, a missing submodule", "from helper import f\n",
                 (h_pkg, ("helper.py", "import helper2\nf = 1\n"), ("helper2.py", "import h.missing\n"))),
                ("a missing submodule below a path on sys.path", "import worker.missing\n", (("src/worker.py", "x = 1\n"),)),
                # round 6 (Main): a module the walk visits is held to the same closed checks as the file: a star import or a dynamic word
                ("a module it imports uses importlib", "import h\n", (("h.py", "import importlib\nx = 1\n"),)),
                ("a module it imports uses getattr", "import h\n", (("h.py", "x = getattr(a, 'b')\n"),)),
                ("a member that a star import may bring", "from h import missing\n", (("h/__init__.py", "from os import *\n"),)),
                ("a module it imports has a star import", "import h\n", (("h.py", "from os import *\n"),)),
                ("a dynamic word in a comment of a module it imports", "import h\n", (("h.py", "x = 1  # exec\n"),)),
                ("a module it imports from, which uses importlib", "from h import x\n", (("h.py", "import importlib\nx = 1\n"),)),
                ("the package __init__ it loads has a star import", "import h.real\n", (("h/__init__.py", "from os import *\n"), ("h/real.py", "y = 1\n"))),
                ("two hops: the second module uses getattr", "import h\n", (("h.py", "import g\n"), ("g.py", "x = getattr(a, 'b')\n"))),
                ("two hops: the second module has a star import", "import h\n", (("h.py", "import g\n"), ("g.py", "from os import *\n"))),
                # round 7 F1: a member import is resolved only when the module's own source proves the member is bound, once, simply
                # (_single_binding); a possible write, an annotation, a delete, a condition or a write to another object proves nothing
                ("a member that is only annotated", "from h import missing\n", (("h.py", "missing: int\n"),)),
                ("a member that is deleted", "from h import missing\n", (("h.py", "missing = 1\ndel missing\n"),)),
                ("a member bound under a condition", "from h import missing\n", (("h.py", "if False:\n    missing = 1\n"),)),
                ("a member that only another object is given", "from h import missing\n",
                 (("h.py", "class C: pass\nobj = C()\nobj.missing = 1\n"),)),
                ("a member bound twice", "from h import x\n", (("h.py", "x = 1\nx = 2\n"),)),
                # round 8 F2: a member's value is h's business: h is in the graph, so what x holds is walked and every module visited is
                # opaque-checked. A value that reaches the target is a path.
                ("a member that h takes from the target", "from h import x\n", (("h.py", "from t import Q as x\n"),)),
                ("a member that h takes from the target's module", "from h import x\n", (("h.py", "import pkg.sub.t as x\n"),)),
                ("a member read from sys.modules", "from h import x\n", (("h.py", "import sys\nx = sys.modules['t'].Q\n"),)),
                ("a member bound in a try", "from h import x\n", (("h.py", "try:\n    x = 1\nexcept Exception:\n    pass\n"),)),
                # round 7b: only a Del, a write whose attribute is the name, or another binding can unbind the name
                ("a member whose item is deleted", "from h import x\n", (("h.py", "x = {}\nx['a'] = 1\ndel x['a']\n"),)),
                ("a member whose item is deleted (no write)", "from h import x\n", (("h.py", "x = {}\ndel x['a']\n"),)),
                ("a member whose attribute is deleted", "from h import x\n", (("h.py", "x = C()\ndel x.a\n"),)),
                ("a member that another object in the file is given as an attribute", "from h import x\n", (("h.py", "x = 1\nobj.x = 2\n"),)),
                ("a member that another file sets as an attribute of h", "from h import x\n", (("h.py", "x = {}\n"), ("g.py", "import h\nh.x = 2\n"))),
                ("a member rebound by an augmented assignment", "from h import x\n", (("h.py", "x = {}\nx |= {}\n"),)),
                ("a member that another module deletes", "from h import x\n", (("h.py", "x = 1\n"), ("g.py", "import h\ndel h.x\n"))),
                # round 8 attack: another file of the run root can rewrite or delete any module attribute by a dynamic form
                ("a member that another module deletes by delattr", "from h import x\n", (("h.py", "x = 1\n"), ("g.py", "import h\ndelattr(h, 'x')\n"))),
                ("a member that another module pops from vars", "from h import x\n", (("h.py", "x = 1\n"), ("g.py", "import h\nvars(h).pop('x')\n"))),
                ("a member that another module pops from __dict__", "from h import x\n", (("h.py", "x = 1\n"), ("g.py", "import h\nh.__dict__.pop('x')\n"))),
                ("a member that another module replaces in sys.modules", "from h import x\n",
                 (("h.py", "x = 1\n"), ("g.py", "import sys\nimport h\nsys.modules['h'] = object()\n"))),
                ("a member that another module replaces, from sys import modules", "from h import x\n",
                 (("h.py", "x = 1\n"), ("g.py", "from sys import modules\nmodules['h'] = object()\n"))),
                ("a member of one of two files of one module name, only one proves it", "from h import y\n",
                 (h_pkg, ("h.py", "y = 2\n"))),
                # round 8b: a name in a string is a reference. Any string constant, split on . : /, with a token equal to the target's name or to
                # the last component of its module's names, blocks; the set of string-built loaders is open, the text they need is not
                ("pydoc.locate of a string built from constants", "import pydoc\nresult = pydoc.locate('pkg.sub.t.' + chr(81))\n", ()),
                ("unittest.mock.patch of such a string", "import unittest.mock as mock\nmock.patch('pkg.sub.t.' + chr(81)).start()\n", ()),
                ("logging.config with such a string", "import logging.config\nlogging.config.dictConfig({'handlers': {'h': {'()': 'pkg.sub.t.' + chr(81)}}})\n", ()),
                ("an f-string with a constant part t.", "import pydoc\nname = chr(81)\nresult = pydoc.locate(f'pkg.sub.t.{name}')\n", ()),
                ("an entry-point string t:", "import pkgutil\nresult = pkgutil.resolve_name('pkg.sub.t:' + chr(81))\n", ()),
                ("a path string pkg/sub/t.py", "import pydoc\nresult = pydoc.importfile('pkg/sub/t.py')\n", ()),
                ("constants joined by +", "import pydoc\nresult = pydoc.locate('pkg.' + 'sub.' + 't.' + chr(81))\n", ()),
                ("the module name as bytes", "import pydoc\nresult = pydoc.locate(b'pkg.sub.t.'.decode() + chr(81))\n", ()),
                ("the module name as a bare string", "x = 't'\n", ()),
                # round 7 F2: any directory may be on sys.path, so a module is known by every dotted suffix of its path and a directory
                # above it by its own suffixes, with or without an __init__.py
                ("a missing submodule of a namespace package under src", "import h.missing\n", (("src/h/real.py", "x = 1\n"),)),
                ("a missing member of a namespace package under src", "from h import missing\n", (("src/h/real.py", "x = 1\n"),)),
                ("a missing module of a namespace package under src, from-import", "from h.missing import f\n", (("src/h/real.py", "x = 1\n"),)),
                ("a missing submodule below src", "import src.h.missing\n", (("src/h/real.py", "x = 1\n"),)),
                ("transitive: a missing submodule of a namespace package", "import helper\n",
                 (("src/h/real.py", "x = 1\n"), ("helper.py", "import h.missing\n"))),
                ("a member of a namespace package that is no module", "from h import real2\n", (("src/h/real.py", "x = 1\n"),))):
            with self.subTest(label):
                self.assertEqual(scan(user, *others), [])
        # round 7 F2: the target's own names are every suffix of its path, and the directories above it are ancestor packages
        for label, user in (("an ancestor below src (the review's reproduction)", "import h\n"),
                            ("an ancestor above the sys.path directory", "import src\n"),
                            ("the ancestor and its submodule", "import src.h\n"),
                            ("the target by the path below src", "import h.t\n"),
                            ("the target by the path below its directory", "import t\n"),
                            ("the target by its full path", "import src.h.t\n"),
                            ("a member that is the target module", "from h import t\n"),
                            ("a member that is the target module, from src", "from src.h import t\n"),
                            ("a submodule of the target by a suffix", "import h.t.deeper\n")):
            with self.subTest(label):
                self.assertEqual(scan(user, target=num("src/h/t.py", "class Q:\n    pass\n")), [])
        # the relative form: a module of a package that imports a sibling the package does not have
        for label, user, others in (("a missing relative module", "from .missing import x\n", (("pkg2/__init__.py", ""),)),
                                    ("a missing relative member", "from . import missing\n", (("pkg2/__init__.py", "x = 1\n"),)),
                                    ("transitive: a relative missing module in a sibling", "from . import helper\n",
                                     (("pkg2/__init__.py", ""), ("pkg2/helper.py", "from .missing import y\n")))):
            with self.subTest(label):
                self.assertEqual(scan(user, *others, at="pkg2/u.py"), [])
        # round 9: a module the file can load is part of what it runs, so every per-file test (_refers: the bare name, a string
        # reference, a _DYNAMIC_ACCESS word, a star import) is made on every module the walk visits (review F1: h loads Q by pydoc.locate).
        # The rows of the file itself are above (round 8b); a trigger in a module the file does not import keeps the line.
        triggers = (("the bare name", "def f():\n    return Q\n"),
                     ("pydoc.locate of constants + chr (review F1)", "import pydoc\nresult = pydoc.locate('pkg.sub.t.' + chr(81))\n"),
                     ("a constant naming the target", "x = 'pkg.sub.t.Q'\n"),
                     ("constants joined by +", "x = 'pkg.' + 'sub.' + 't'\n"),
                     ("an f-string with a constant part", "n = chr(81)\nx = f'pkg.sub.t.{n}'\n"),
                     ("an entry point", "x = 'pkg.sub.t:' + chr(81)\n"),
                     ("a path string", "x = 'pkg/sub/t.py'\n"),
                     ("bytes", "x = b'pkg.sub.t'\n"),
                     ("the module name as a bare string", "x = 't'\n"),
                     ("a dynamic word", "x = getattr(a, 'b')\n"),
                     ("a star import", "from os import *\n"))
        for form, text in triggers:
            for where, user, others in (
                    ("a module it imports", "import h\n", (("h.py", text),)),
                    ("two imports away", "import h\n", (("h.py", "import g\n"), ("g.py", text))),
                    ("the __init__ of an ancestor package", "import lib.x\n", (("lib/__init__.py", text), ("lib/x.py", "y = 1\n"))),
                    ("a module reached by a member import", "from lib import x\n", (("lib/__init__.py", ""), ("lib/x.py", text))),
                    ("a module in an import cycle", "import h\n", (("h.py", "import g\n"), ("g.py", "import h\n" + text)))):
                with self.subTest("{} in {}".format(form, where)):
                    self.assertEqual(scan(user, *others), [])
            with self.subTest("{} in a module the file does not import".format(form)):
                self.assertEqual(scan("import h\n", ("h.py", "x = 1\n"), ("g.py", text)), none)
        for label, user, others in (
                ("a module in the closure with no trigger", "import h\n", (("h.py", "import g\nx = 'pkg.sub.other'\n"), ("g.py", "y = 'QUEUE'\n"))),
                ("nothing imported", "x = 1\n", ()),
                # round 8b: strings that name neither the target nor its module's last component keep the line
                ("an unrelated string", "x = 'hello world'\n", ()),
                ("Q as a substring of a longer token", "x = 'QUEUE'\n", ()),
                ("a dotted string without the module name", "x = 'pkg.sub.other'\n", ()),
                ("a path string of another file", "x = 'pkg/sub/other.py'\n", ()),
                ("the standard library", "import os\nfrom collections import OrderedDict\n", ()),
                ("a package that only looks alike", "import pkg2\nfrom other.pkg import sub\n", ()),
                ("a module of its own", "import h\n", (("h.py", "import os\n"),)),
                ("two hops that end elsewhere", "import h\n", (("h.py", "import g\n"), ("g.py", "import json\n"))),
                ("a package whose __init__ does not import the target", "import lib.x\n",
                 (("lib/__init__.py", "import os\n"), ("lib/x.py", "y = 1\n"))),
                ("an import cycle that ends nowhere", "import h\n", (("h.py", "import u\n"),)),
                ("by the path below a directory on sys.path, ending elsewhere", "import worker\n", (("src/worker.py", "import os\n"),)),
                # round 6 F1: an import that resolves to a run-root module keeps the line, whatever hops it takes
                ("a clean module it imports, words that only look alike", "import h\n", (("h.py", "x = execute(evaluate(my_vars))\n"),)),
                ("two clean hops", "import h\n", (("h.py", "import g\n"), ("g.py", "x = 1\n"))),
                ("a submodule that exists", "import h.real\n", (h_pkg, ("h/real.py", "y = 1\n"))),
                ("a member that is a submodule", "from h import real\n", (h_pkg, ("h/real.py", "y = 1\n"))),
                ("a member the package binds", "from h import x\n", (h_pkg,)),
                ("a member a module binds", "from h import x\n", (h_mod,)),
                ("a member from a module of a package", "from h.real import y\n", (h_pkg, ("h/real.py", "y = 1\n"))),
                # round 7: the line stays when the import is proved
                # round 7b: a write into the object a name holds does not unbind the name (the calibration cli.py: HANDLERS[name] = fn)
                ("a member whose item is written", "from h import x\n", (("h.py", "x = {}\nx['a'] = 1\n"),)),
                ("a member whose item is written in a function", "from h import x\n",
                 (("h.py", "x = {}\n\n\ndef reg(name):\n    def deco(fn):\n        x[name] = fn\n        return fn\n    return deco\n"),)),
                ("a member whose item is added with +=", "from h import x\n", (("h.py", "x = {'a': [1]}\nx['a'] += [2]\n"),)),
                ("a function that carries an attribute", "from h import f\n", (("h.py", "def f():\n    pass\n\n\nf.cache = {}\n"),)),
                ("a member whose item another file deletes", "from h import x\n", (("h.py", "x = {}\n"), ("g.py", "import h\ndel h.x['a']\n"))),
                ("a member another file writes an item of", "from h import x\n", (("h.py", "x = {}\n"), ("g.py", "import h\nh.x['a'] = 1\n"))),
                ("a member defined by one plain def", "from h import f\n", (("h.py", "def f():\n    pass\n"),)),
                # round 8 F2: one unconditional binding of any value proves the member exists (the import takes the attribute, not a
                # submodule from outside the run root); a right-hand side that raises makes h, and so the importer, fail to import
                ("a member bound to a call", "from h import x\n", (("h.py", "def unknown():\n    pass\n\n\nx = unknown()\n"),)),
                ("a member bound to an import alias", "from h import x\n", (("h.py", "import os as x\n"),)),
                ("a member bound to an undefined name", "from h import x\n", (("h.py", "x = nonexistent\n"),)),
                ("a member defined by one plain class", "from h import C\n", (("h.py", "class C:\n    pass\n"),)),
                ("a member that a package binds, in a package", "from h import f\n", (("h/__init__.py", "def f():\n    pass\n"),)),
                ("a module of a namespace package under src", "import h.real\n", (("src/h/real.py", "y = 1\n"),)),
                ("a module of a namespace package under src, by the full path", "import src.h.real\n", (("src/h/real.py", "y = 1\n"),)),
                ("a member that is a module of a namespace package", "from h import real\n", (("src/h/real.py", "y = 1\n"),)),
                ("a member from a module of a namespace package", "from h.real import y\n", (("src/h/real.py", "y = 1\n"),)),
                ("a name that does not look like a run-root directory", "import hh.real\n", (("src/h/real.py", "y = 1\n"),)),
                ("transitive: a submodule that exists", "import helper\n", (h_pkg, ("h/real.py", "y = 1\n"), ("helper.py", "import h.real\n"))),
                ("transitive: a member that exists", "import helper\n", (h_pkg, ("helper.py", "from h import x\n"))),
                ("a relative import that resolves, to a file that imports elsewhere", "import pkg.o\n",
                 (("pkg/o.py", "from . import p\n"), ("pkg/p.py", "y = 1\n")))):
            with self.subTest(label):
                expected = [] if label.startswith("a relative import that resolves") else none  # pkg is an ancestor of pkg.sub.t
                self.assertEqual(scan(user, *others), expected)
        # round 11: with the run root on disk, an import for which Python's finder may load a file the scan was not given (an
        # __init__.py, a module, a .pyc or an extension module on disk only) is a path; a namespace directory runs nothing
        reach = "from pkg.sub.t import Q\nQ()\n"
        with tempfile.TemporaryDirectory() as tmp:
            for n, (label, user, others, disk, expected) in enumerate((
                    ("h/__init__.py on disk beside a supplied h.py", "import h\n", (h_mod,), {"h/__init__.py": reach}, []),
                    ("h.py on disk only", "import h\n", (), {"h.py": reach}, []),
                    ("h.pyc on disk beside a supplied h.py", "import h\n", (h_mod,), {"h.pyc": ""}, []),
                    ("an extension module h on disk beside a supplied h.py", "import h\n", (h_mod,),
                     {"h" + importlib.machinery.EXTENSION_SUFFIXES[0]: ""}, []),
                    ("a submodule on disk only, from a supplied package", "from h import real\n", (h_pkg,), {"h/real.py": reach}, []),
                    ("on disk too, nothing else there", "import h\n", (h_mod,), {}, none),
                    ("a namespace directory on disk only", "import ns\n", (), {"ns/notes.txt": ""}, none))):
                root = Path(tmp) / str(n)
                for p, t in [(target["path"], "class Q:\n    pass\n"), ("u.py", user)] + list(others) + list(disk.items()):
                    (root / p).parent.mkdir(parents=True, exist_ok=True)
                    (root / p).write_text(t)
                with self.subTest(label, root="on disk"):
                    files = [target] + [num(p, t) for p, t in others] + [num("u.py", user)]
                    self.assertEqual([line for line in ss.use_scan("Who uses Q?", files, str(root)) if line.startswith("- u.py")], expected)

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
            "- z.py does not use t.Q [z.py:1]",  # w, param2 and u spell Q (imported from the other definer): the absence is not proven
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
# round 3b: a file that names one of these anywhere can bind, rewrite or delete a name without a binding the source shows
DYNAMIC_FORMS = [
    ("globals", "globals()['{n}'] = 7"),
    ("vars", "vars()['{n}'] = 7"),
    ("locals", "locals()['{n}'] = 7"),
    ("setattr", "setattr(sys.modules[__name__], '{n}', 7)"),
    ("delattr", "delattr(mod, '{n}')"),
    ("exec", "exec('{n} = 7')"),
    ("eval", "eval('{n}')"),
    ("__import__", "__import__('os')"),
    ("import importlib", "import importlib"),
    ("import importlib.util", "import importlib.util"),
    ("from importlib import", "from importlib import import_module"),
    ("importlib attribute", "importlib.import_module('m')"),
    ("__dict__ write", "mod.__dict__['{n}'] = 7"),
    ("__dict__ update", "mod.__dict__.update(d)"),
    ("__dict__ name", "__dict__"),
    ("attribute named like a builtin", "builtins.exec('x = 1')"),
]
# the same, in a module and inside a function body (the use is anywhere in the file)
DYNAMIC_PLACES = (("module level", lambda text: text), ("in a function", lambda text: "def dyn():\n" + indented(text, "    ")))
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
                    if label in ("assignment", "annotated assignment") and (name, side) in (("ADD", "old"), ("GONE", "new")):
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

    def test_a_mutable_literal_is_never_compared(self):
        """A dict, list or set (or a tuple holding one) can be changed by `D.update(...)`, `D.append(...)` or `D[k] = v` in a function,
        so the file does not show its value: no line, with or without such a write."""
        values = (("{'a': 1}", "{'a': 2}"), ("[1, 2]", "[2, 1]"), ("{1, 2}", "{1, 3}"), ("(1, [2])", "(1, [3])"), ("(1, {'a': 1})", "(1, {'a': 2})"),
)
        writes = (("no write", ""), ("update call", "def mutate():\n    D.update({'a': 3})"), ("append call", "def mutate():\n    D.append(3)"),
                  ("subscript write", "def mutate():\n    D['k'] = 3"), ("update call at module level", "D.update({'a': 3})"),
                  ("append call at module level", "D.append(3)"), ("subscript write at module level", "D['k'] = 3"))
        for old, new in values:
            for label, write in writes:
                for side in ("old", "new", "both"):
                    with self.subTest(old=old, new=new, write=label, side=side):
                        texts = ["D = " + old + "\n" + (write if side in ("old", "both") else ""),
                                 "D = " + new + "\n" + (write if side in ("new", "both") else "")]
                        for text in texts:
                            ast.parse(text)
                        out = ss.version_diff(num("a.py", texts[0]), num("a2.py", texts[1]))
                        self.assertEqual([line for line in out if mentions(line, "D")], [])
        # annotated too
        self.assertEqual(ss.version_diff(num("a.py", "D: dict = {'a': 1}"), num("a2.py", "D: dict = {'a': 2}")), [])

    def test_an_immutable_literal_is_compared(self):
        for old, new, shown in (("1", "2", "1 to 2"), ("-1", "-2", "-1 to -2"), ("'a'", "'b'", "'a' to 'b'"), ("b'a'", "b'b'", "b'a' to b'b'"),
                                ("None", "'n'", "None to 'n'"), ("True", "False", "True to False"), ("1.5", "2.5", "1.5 to 2.5"),
                                ("(1, 'a')", "(1, 'b')", "(1, 'a') to (1, 'b')"), ("()", "(1,)", "() to (1,)")):
            for annotation in ("", ": object"):
                with self.subTest(old=old, new=new, annotation=annotation):
                    out = ss.version_diff(num("a.py", "X{} = {}".format(annotation, old)), num("a2.py", "X{} = {}".format(annotation, new)))
                    self.assertEqual(out, ["- a2.py changes X from {} [a.py:1, a2.py:1]".format(shown)])
        # a method call on it, or a read, does not change a number
        self.assertEqual(ss.version_diff(num("a.py", "X = 1\nY = X.bit_length()"), num("a2.py", "X = 2\nY = X.bit_length()")),
                         ["- a2.py changes X from 1 to 2 [a.py:1, a2.py:1]"])

    def test_a_file_that_uses_a_dynamic_form_states_nothing(self):
        clean = pair_diff()
        self.assertTrue(len(clean) >= 9, clean)
        for label, snippet in DYNAMIC_FORMS:
            for place, put in DYNAMIC_PLACES:
                for side in ("old", "new"):
                    with self.subTest(form=label, place=place, side=side):
                        self.assertEqual(pair_diff(side, mod=put(form_text(snippet, "X"))), [])
        # in the class body too (a class attribute is a name of the file)
        for label, snippet in DYNAMIC_FORMS:
            with self.subTest(form=label, place="class body"):
                self.assertEqual(pair_diff("new", cls=form_text(snippet, "take", "    ")), [])

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

    def scan(self, text, path="app/u.py", *others):
        return ss.use_scan("Who uses Q?", self.FILES + [num(p, t) for p, t in others] + [num(path, text)])

    def test_resolution_by_full_dotted_path(self):
        both = ["- u.py imports Q [{p}:{i}]", "- u.py creates a Q [{p}:{c}]"]
        for label, path, text, imp, call in (
                ("from pkg.t import Q", "app/u.py", "from pkg.t import Q\nQ()\n", 1, 2),
                ("import pkg.t", "app/u.py", "import pkg.t\npkg.t.Q()\n", 2, 2),
                ("import pkg.t as t", "app/u.py", "import pkg.t as t\nt.Q()\n", 2, 2),
                ("from pkg import t", "app/u.py", "from pkg import t\nt.Q()\n", 2, 2),
                ("relative from .t", "pkg/u.py", "from .t import Q\nQ()\n", 1, 2),
                ("relative from . import t", "pkg/u.py", "from . import t\nt.Q()\n", 2, 2),
                ("relative from ..t in a subpackage", "pkg/sub/u.py", "from ..t import Q\nQ()\n", 1, 2)):
            with self.subTest(label):
                self.assertEqual(self.scan(text, path), [line.format(p=path, i=imp, c=call) for line in both])
        # a module that only ends in t is not pkg.t (F4); neither is a bare t, a relative one from another package, one above the root
        for label, text in (("unrelated.t", "from unrelated.t import Q\nQ()\n"), ("unrelated t attribute", "from unrelated import t\nt.Q()\n"),
                            ("bare from t", "from t import Q\nQ()\n"), ("bare import t", "import t\nt.Q()\n"),
                            ("relative in another package", "from .t import Q\nQ()\n"), ("relative above the root", "from ... import t\nt.Q()\n"),
                            ("relative above the package", "from ..pkg.t import Q\nQ()\n"),
                            # the dotted import binds pkg: a second import of pkg may be another module
                            ("pkg bound twice", "import pkg.t\nimport pkg.other\npkg.t.Q()\n")):
            with self.subTest(label):
                self.assertEqual(self.scan(text), [])
        # Round 11 (Main): a module is credited to a file only when Python's finder can load nothing else for that name from the run
        # root (_module_identity), at every step: a competing candidate, supplied or on disk (the root), leaves the import line of
        # `from pkg.t import Q` at most, and no file gets "does not use". Rows from the round-11 attack (729 cases on both interpreters,
        # 0 unsound after); the first is the review's repro. Extension suffixes are the running interpreter's.
        other = "Q = lambda: 'other'\n"
        rows = [("review: pkg/t/__init__.py supplied beside pkg/t.py", {"pkg/t/__init__.py": other}, {}),
                ("pkg/t/__init__.py on disk only", {}, {"pkg/t/__init__.py": other}),
                ("pkg/t/ with no __init__, on disk", {}, {"pkg/t/notes.txt": ""}),
                ("pkg.py supplied beside the package", {"pkg.py": other}, {}),
                ("pkg.py on disk beside the package", {}, {"pkg.py": other}),
                # an __init__ on disk only runs unseen on the way to pkg.t: here it rebinds t.Q
                ("pkg/__init__.py on disk only", "drop", {"pkg/__init__.py": "from . import t\nt.Q = lambda: 'other'\n"})]
        for ext in [s for s in importlib.machinery.all_suffixes() if s != ".py"]:
            rows += [("pkg/t{} on disk".format(ext), {}, {"pkg/t" + ext: ""}), ("pkg{} on disk".format(ext), {}, {"pkg" + ext: ""}),
                     ("pkg/__init__{} on disk".format(ext), {}, {"pkg/__init__" + ext: ""})]
        forms = (("import pkg.t\nobj = pkg.t.Q()\n", 2), ("import pkg.t as t\nobj = t.Q()\n", 2), ("from pkg import t\nobj = t.Q()\n", 2),
                 ("from pkg.t import Q\nobj = Q()\n", 1))
        with tempfile.TemporaryDirectory() as tmp:
            for n, (label, supplied, disk) in enumerate([("control: nothing competes, on disk", {}, {})] + rows):
                for kind, target, verb in (("class", "class Q:\n    pass\n", "creates a"), ("function", "def Q():\n    pass\n", "calls")):
                    for text, imp in forms:
                        files = {"pkg/__init__.py": "", "pkg/t.py": target, "app/u.py": text, "v.py": "x = 1\n"}
                        if supplied == "drop":
                            del files["pkg/__init__.py"]
                        else:
                            files.update(supplied)
                        root = Path(tmp) / str(n) / kind / str(imp) / text.split("\n")[0].replace(" ", "_")
                        for p, t in list(files.items()) + list(disk.items()):
                            (root / p).parent.mkdir(parents=True, exist_ok=True)
                            (root / p).write_text(t)
                        with self.subTest(label, kind=kind, form=text.split("\n")[0]):
                            out = ss.use_scan("Who uses Q?", [num(p, t) for p, t in files.items()], str(root))
                            credited = ["- u.py imports Q [app/u.py:{}]".format(imp), "- u.py {} Q [app/u.py:2]".format(verb)]
                            expected = credited if n == 0 else ["- u.py imports Q [app/u.py:1]"] if imp == 1 else []
                            self.assertEqual([line for line in out if line.startswith("- u.py")], expected)
                            self.assertEqual("- v.py does not use Q [v.py:1]" in out, n == 0)
        # the top-level name of the module is a builtin or standard-library name: Python may answer it without the run root
        for path, module in (("json.py", "json"), ("os/t.py", "os.t"), ("sys/__init__.py", "sys"), ("collections/abc.py", "collections.abc")):
            with self.subTest("a standard-library top-level name", module=module):
                out = ss.use_scan("Who uses Q?", [num(path, "class Q:\n    pass\n"), num("app/u.py", "from {} import Q\nQ()\n".format(module)),
                                                  num("v.py", "x = 1\n")])
                self.assertEqual(out, ["- u.py imports Q [app/u.py:1]"])

    def test_the_review_reproductions(self):
        # F1: the module overwritten by a star import, the name by a dotted import, the attribute by a write
        self.assertEqual(self.scan("import pkg.t as t\nfrom mid import *\nt.Q()\n"), [])
        self.assertEqual(self.scan("from pkg.t import Q\nimport Q.child\nQ()\n"), ["- u.py imports Q [app/u.py:1]"])
        self.assertEqual(self.scan("import pkg.t as t\nt.Q = other\nt.Q()\n"), [])
        self.assertEqual(self.scan("import pkg.t\npkg.t.Q = other\npkg.t.Q()\n"), [])
        self.assertEqual(self.scan("from pkg.t import Q\nQ.pop = other\nQ()\n"), ["- u.py imports Q [app/u.py:1]"])

    def test_an_attribute_write_anywhere_in_the_run_root_unproves_the_module_member(self):
        """Round 8 F1: a call, creation or use through `m.Q` or a `from m import Q` name holds only if no file of the run root writes or
        deletes an attribute named Q (`x.Q = v`, `x.Q += v`, `del x.Q`, any x): the module attribute may be another object by then. No
        alias analysis: the write of the attribute name is the block. The direct import line stays (the import itself is a fact).
        Round 10: the same holds for every link a module-path name goes through (the package attributes on the way to the module), and
        a package __init__ that binds a link by anything but an import of that very submodule unproves it too."""
        module_form, direct_form = "import pkg.t as t\n{}t.Q()\n", "from pkg.t import Q\n{}Q()\n"
        for label, write in (("the review's alias write", "a = t\na.Q = other\n"), ("an augmented write", "a = t\na.Q += 1\n"),
                             ("a delete", "a = t\ndel a.Q\n"), ("a write in a function", "def f(a):\n    a.Q = other\n\n\n")):
            with self.subTest(label, form="module attribute"):
                self.assertEqual(self.scan(module_form.format(write)), [])
            with self.subTest(label, form="module attribute, and an instance"):
                self.assertEqual(self.scan("import pkg.t as t\n" + write + "x = t.Q()\nx.pop()\n"), [])
        for label, other in (("a write", "def f(x):\n    x.Q = 1\n"), ("an augmented write", "def f(x):\n    x.Q += 1\n"),
                             ("a delete", "def f(x):\n    del x.Q\n"), ("a write at module level", "import os\nos.Q = 1\n")):
            with self.subTest(label, form="module attribute, the write in another file"):
                self.assertEqual(self.scan(module_form.format(""), "app/u.py", ("app/g.py", other)), [])
            with self.subTest(label, form="direct import, the write in another file"):
                self.assertEqual(self.scan(direct_form.format(""), "app/u.py", ("app/g.py", other)), ["- u.py imports Q [app/u.py:1]"])
        # round 8 attack: a dynamic form in another file can rewrite or delete any module attribute (`_ANY` in _written_names), and so can
        # a `sys.modules` write (a dynamic file); neither names the attribute Q
        for label, other in (("setattr", "import pkg.t as p\nsetattr(p, 'Q', other)\n"), ("delattr", "import pkg.t as p\ndelattr(p, 'Q')\n"),
                             ("vars", "import pkg.t as p\nvars(p)['Q'] = other\n"), ("__dict__", "import pkg.t as p\np.__dict__['Q'] = other\n"),
                             ("sys.modules", "import sys\nsys.modules['pkg.t'] = other\n"),
                             ("from sys import modules", "from sys import modules\nmodules['pkg.t'] = other\n")):
            with self.subTest(label, form="module attribute, a dynamic write in another file"):
                self.assertEqual(self.scan(module_form.format(""), "app/u.py", ("app/g.py", other)), [])
            with self.subTest(label, form="direct import, a dynamic write in another file"):
                self.assertEqual(self.scan(direct_form.format(""), "app/u.py", ("app/g.py", other)), ["- u.py imports Q [app/u.py:1]"])
        # keep the line: an unrelated attribute, or a write into the object the name holds, is not a write of the attribute Q
        for label, text, others in (("an unrelated attribute, same file", "import pkg.t as t\na = t\na.R = other\nt.Q()\n", ()),
                                    ("an unrelated attribute, another file", "import pkg.t as t\nt.Q()\n", (("app/g.py", "def f(x):\n    x.R = 1\n"),)),
                                    ("an item of Q, another file", "import pkg.t as t\nt.Q()\n", (("app/g.py", "Q = {}\nQ['a'] = 1\n"),))):
            with self.subTest(label):
                call = text.count("\n")
                self.assertEqual([line for line in self.scan(text, "app/u.py", *others) if line.startswith("- u.py")],
                                 ["- u.py imports Q [app/u.py:{}]".format(call), "- u.py creates a Q [app/u.py:{}]".format(call)])
        # Round 10: a module-path name (`import pkg.sub.t`, `as x`, `from pkg.sub import t`, relative) goes through the package
        # attributes sub and t too; each link must be proved: no run-root write or delete of an attribute of its name, and the
        # package's __init__ binds it only by importing exactly that submodule. `from pkg.sub.t import Q` reads sys.modules and keeps
        # its facts. Rows come from the round-10 attack (600 cases run through use_scan and executed, 0 unsound after).
        deep = {"pkg/__init__.py": "", "pkg/sub/__init__.py": "", "pkg/sub/t.py": "class Q:\n    pass\n", "pkg/sub/alt.py": "Q = 1\n",
                "other.py": "Q = lambda: 'other'\n\n\nclass t:\n    Q = staticmethod(Q)\n"}
        module_forms = (("u.py", "import pkg.sub.t\nobj = pkg.sub.t.Q()\n"), ("u.py", "import pkg.sub.t as x\nobj = x.Q()\n"),
                        ("u.py", "from pkg.sub import t\nobj = t.Q()\n"), ("pkg/sub/u.py", "from . import t\nobj = t.Q()\n"),
                        ("pkg/u.py", "from .sub import t\nobj = t.Q()\n"))
        direct_forms = (("u.py", "from pkg.sub.t import Q\nobj = Q()\n"), ("pkg/sub/u.py", "from .t import Q\nobj = Q()\n"))

        def deep_scan(path, text, changes):
            files = dict(deep, **changes)
            head = "import h\n" if "h.py" in changes else ""
            files[path] = head + text
            last = files[path].count("\n")
            out = ss.use_scan("Who uses Q?", [num(p, t) for p, t in files.items()])
            return [line for line in out if "[{}:".format(path) in line], last

        # the review's repro, verbatim: A (h rebinds the package attribute t through an alias), B (the __init__ binds t to another module)
        helper = ("app/h.py", "import pkg.t\nimport other\na = pkg\na.t = other\n")
        for text in ("import h\nimport pkg.t\nobj = pkg.t.Q()\n", "import h\nimport pkg.t as t\nobj = t.Q()\n", "import h\nfrom pkg import t\nobj = t.Q()\n"):
            with self.subTest("review A", form=text.split("\n")[1]):
                self.assertEqual([line for line in self.scan(text, "app/u.py", helper) if line.startswith("- u.py")], [])
        self.assertEqual(self.scan("import h\nfrom pkg.t import Q\nobj = Q()\n", "app/u.py", helper),
                         ["- u.py imports Q [app/u.py:2]", "- u.py creates a Q [app/u.py:3]"])
        with self.subTest("review B"):
            self.assertEqual(self.scan("from pkg import t\nobj = t.Q()\n", "app/u.py", ("pkg/__init__.py", "from . import other as t\n"),
                                       ("pkg/other.py", "Q = lambda: 'other'\n")), [])
        breaker = "import pkg.sub.t\nimport other\n"
        for label, changes in (
                ("review A: t written through an alias, in an imported module", {"h.py": breaker + "a = pkg.sub\na.t = other\n"}),
                ("sub written through the package name, in an imported module", {"h.py": breaker + "pkg.sub = other\n"}),
                ("t deleted, in a module nobody imports", {"g.py": breaker + "del pkg.sub.t\n"}),
                ("sub written through an alias, in the package __init__", {"pkg/__init__.py": breaker + "a = pkg\na.sub = other\n"}),
                ("review B: __init__ binds t by a relative as import", {"pkg/sub/__init__.py": "from . import alt as t\n"}),
                ("__init__ binds t by an as import", {"pkg/sub/__init__.py": "import other as t\n"}),
                ("__init__ binds t by an assignment", {"pkg/sub/__init__.py": "import other\nt = other\n"}),
                ("__init__ binds t by a def", {"pkg/sub/__init__.py": "def t():\n    pass\n"}),
                ("__init__ binds t by a class", {"pkg/sub/__init__.py": "class t:\n    Q = None\n"}),
                ("__init__ binds t by a for target", {"pkg/sub/__init__.py": "import other\nfor t in [other]:\n    pass\n"}),
                ("__init__ binds t by a with target", {"pkg/sub/__init__.py": "import contextlib\nwith contextlib.nullcontext(1) as t:\n    pass\n"}),
                ("__init__ binds t by a global", {"pkg/sub/__init__.py": "def f():\n    global t\n    t = 1\n\n\nf()\n"}),
                ("__init__ binds t by a star import", {"pkg/sub/__init__.py": "from other import *\n"}),
                ("__init__ answers any name by __getattr__", {"pkg/sub/__init__.py": "def __getattr__(name):\n    return None\n"}),
                ("__init__ binds sub by an assignment", {"pkg/__init__.py": "sub = None\n"})):
            for path, text in module_forms:
                with self.subTest(label, form=text.split("\n")[0], where=path):
                    self.assertEqual(deep_scan(path, text, changes)[0], [])
            for path, text in direct_forms:
                with self.subTest(label, form=text.split("\n")[0], where=path):
                    lines, last = deep_scan(path, text, changes)
                    self.assertEqual(lines, ["- {} imports Q [{}:{}]".format(Path(path).name, path, last - 1),
                                             "- {} creates a Q [{}:{}]".format(Path(path).name, path, last)])
        # keep the line: no break, or an __init__ that binds the link only by importing exactly that submodule
        for label, changes in (("no break", {}), ("__init__: from . import t", {"pkg/sub/__init__.py": "from . import t\n"}),
                               ("__init__: import pkg.sub.t as t", {"pkg/sub/__init__.py": "import pkg.sub.t as t\n"}),
                               ("__init__: from . import sub", {"pkg/__init__.py": "from . import sub\n"}),
                               ("__init__: a method named __getattr__",
                                {"pkg/sub/__init__.py": "class C:\n    def __getattr__(self, n):\n        return 1\n"})):
            for path, text in module_forms:
                with self.subTest(label, form=text.split("\n")[0], where=path):
                    lines, last = deep_scan(path, text, changes)
                    self.assertEqual(lines, ["- {} imports Q [{}:{}]".format(Path(path).name, path, last),
                                             "- {} creates a Q [{}:{}]".format(Path(path).name, path, last)])

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

    def test_a_type_parameter_binds_its_name_like_a_parameter(self):
        """Round 8 attack: `def f[t]()`, `class C[t]` and `type X[t] = ...` (Python 3.12+) bind t in their own scope; a use of t there is
        the type parameter, not the import. The rows are skipped where the syntax does not parse."""
        for label, form in (("a def", "def f[{n}]():\n    return {c}\n"), ("an async def", "async def f[{n}]():\n    return {c}\n"),
                            ("a class", "class C[{n}]:\n    x = {c}\n"), ("a type alias", "type X[{n}] = {c}\n"),
                            ("a ParamSpec", "def f[**{n}]():\n    return {c}\n"), ("a TypeVarTuple", "def f[*{n}]():\n    return {c}\n")):
            for name, imp, call in (("t", "import pkg.t as t\n", "t.Q()"), ("Q", "from pkg.t import Q\n", "Q()")):
                text = imp + form.format(n=name, c=call)
                try:
                    ast.parse(text)
                except SyntaxError:
                    continue
                with self.subTest(label, name=name):
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

    def test_a_file_that_uses_a_dynamic_form_attributes_nothing(self):
        """The importer, or the file that defines the target, may rebind a name by globals(), setattr, exec, importlib or __dict__: the
        import line is not stated either, and there is no `does not use` line."""
        calls = "x = Q()\nx.pop()\n"
        self.assertEqual(len(self.scan("from pkg.t import Q\n" + calls)), 3)
        self.assertEqual(self.scan("x = 1\n"), ["- u.py does not use Q [app/u.py:1]"])
        for label, snippet in DYNAMIC_FORMS:
            for place, put in DYNAMIC_PLACES:
                form = put(form_text(snippet, "Q"))
                with self.subTest(form=label, place=place, file="the importer"):
                    self.assertEqual(self.scan("from pkg.t import Q\n" + form + "\n" + calls), [])
                    self.assertEqual(self.scan("import pkg.t as t\n" + form + "\ny = t.Q()\n"), [])
                with self.subTest(form=label, place=place, file="a file with no use"):
                    self.assertEqual(self.scan("x = 1\n" + form + "\n"), [])
                with self.subTest(form=label, place=place, file="the target"):
                    target = "class Q:\n    def pop(self):\n        return 1\n\n" + form + "\n"
                    ast.parse(target)
                    self.assertEqual(ss.use_scan("Who uses Q?", [num("pkg/t.py", target), num("app/u.py", "from pkg.t import Q\n" + calls)]), [])

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
