"""LS1c S1b on lib/summarize_structured.py: fact form v2 (subject, claim, path, lines), render, outline, the names block of the user
message, the code-made name index, and the v2 config key. The endpoint is the stub of test_qual_summarize_structured; no model runs."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_qual_summarize_structured as base  # noqa: E402  (names only: no test class is imported, so no test runs twice)

ss, summarize_check, ROOT = base.ss, base.summarize_check, base.ROOT
FIXTURES = base.DATA / "fixtures"

CODE = '''"""doc"""
TIMEOUT = 5
LIMIT: int = 7
A = B = 1
x.attr = 2


class Cache:
    SIZE = 3

    def __init__(self, capacity, ttl=None, *, clock=None):
        self.capacity = capacity

    def get(self, key):
        def inner(hidden):
            return hidden
        return inner(key)

    def _private(self):
        pass

    @classmethod
    def make(cls, size):
        return cls(size)


def top(a, /, b):
    return a


async def later():
    pass
'''


def v2(subject="f", claim="does x", path="a.py", lines=(1,)):
    return {"subject": subject, "claim": claim, "path": path, "lines": list(lines)}


class ValidateV2Tests(unittest.TestCase):
    FILES = {"a.py": 10, "src/b.py": 4}

    def test_drop_reasons(self):
        rows = [
            ("empty lines", v2(lines=()), "malformed"),
            ("four lines", v2(lines=(1, 2, 3, 4)), "malformed"),
            ("a string line", v2(lines=(1, "2")), "malformed"),
            ("a float line", v2(lines=(1.0,)), "malformed"),
            ("a bool line", v2(lines=(True,)), "malformed"),
            ("lines not a list", dict(v2(), lines=3), "malformed"),
            ("claim not a string", dict(v2(), claim=5), "malformed"),
            ("no subject", {"claim": "c", "path": "a.py", "lines": [1]}, "malformed"),
            ("unnamed file", v2(path="z.py"), "unnamed_file"),
            ("past the end", v2(lines=(1, 11)), "range_outside_file"),
            ("line zero", v2(lines=(0,)), "range_outside_file"),
            ("negative line", v2(lines=(-1,)), "range_outside_file"),
            ("empty claim", v2(claim="  "), "empty_claim"),
        ]
        for label, fact, reason in rows:
            with self.subTest(label):
                kept, drops = ss.validate([fact], self.FILES)
                self.assertEqual((kept, drops), ([], [{"fact": fact, "reason": reason}]))

    def test_three_lines_kept_sorted_and_never_changed(self):
        kept, drops = ss.validate([v2(lines=(9, 2, 9))], self.FILES)
        self.assertEqual((drops, [f["lines"] for f in kept]), ([], [[2, 9]]))  # 3 given, one repeated: de-duplicated, sorted, not moved
        kept, _ = ss.validate([v2(lines=(1, 2, 3), path="./src/b.py:2")], {"src/b.py": 4})
        self.assertEqual((kept[0]["path"], kept[0]["lines"]), ("src/b.py", [1, 2, 3]))

    def test_subject_normalization(self):
        rows = [("  `load`  ", "load"), ("Store.load", "load"), ("Store.load()", "load"), ("`A.b.c`", "c"),
                ("src/b.py", "src/b.py"), ("b.py", "b.py"), ("a.py", "a.py"), ("load()", "load()"), ("two words", "two words")]
        for given, want in rows:
            with self.subTest(given):
                kept, drops = ss.validate([v2(subject=given, claim=" returns 1 ".strip())], self.FILES)
                self.assertEqual((drops, kept[0]["claim"]), ([], want + " returns 1"))

    def test_old_form_is_still_accepted(self):
        old = base.fact("f returns 1", "./a.py:2", 2, 4)
        kept, drops = ss.validate([old, base.fact("long", "a.py", 1, 6), base.fact("past", "a.py", 1, 11)], self.FILES)
        self.assertEqual(kept, [{"claim": "f returns 1", "path": "a.py", "line_start": 2, "line_end": 4}])
        self.assertEqual([d["reason"] for d in drops], ["range_too_long", "range_outside_file"])


class RenderV2Tests(unittest.TestCase):
    def test_exact_text_and_one_citation_per_line(self):
        kept, _ = ss.validate([v2("get", "returns the value.", "src/b.py", (3, 1)), v2("a.py", "makes a cache", "a.py", (7,))],
                              {"a.py": 10, "src/b.py": 4})
        text = ss.render(kept + [base.fact("old form", "a.py", 2, 4)])
        self.assertEqual(text, "- get returns the value [src/b.py:1, src/b.py:3]\n- a.py makes a cache [a.py:7]\n- old form [a.py:2-4]")
        found = [summarize_check.FACT_LINE.match(line).groupdict() for line in text.splitlines()]
        self.assertEqual([f["cites"] for f in found], ["src/b.py:1, src/b.py:3", "a.py:7", "a.py:2-4"])
        cited = [summarize_check.CITATION.match(c.strip()).group("path", "a") for c in found[0]["cites"].split(",")]
        self.assertEqual(cited, [("src/b.py", "1"), ("src/b.py", "3")])


class OutlineTests(unittest.TestCase):
    @staticmethod
    def numbered(code):
        return "\n".join("{:>4}\t{}".format(n, line) for n, line in enumerate(code.splitlines(), 1))

    def test_every_kind_and_what_is_not_walked(self):
        got = ss.outline("pkg/c.py", self.numbered(CODE))
        self.assertEqual(got, [
            (2, "constant", "TIMEOUT", None), (3, "constant", "LIMIT", None), (4, "constant", "A", None), (4, "constant", "B", None),
            (8, "class", "Cache", None),
            (11, "method", "__init__", "Cache"), (11, "parameter", "capacity", "__init__"), (11, "parameter", "ttl", "__init__"),
            (11, "parameter", "clock", "__init__"),
            (14, "method", "get", "Cache"), (14, "parameter", "key", "get"),
            (19, "method", "_private", "Cache"),
            (23, "method", "make", "Cache"), (23, "parameter", "size", "make"),
            (27, "function", "top", None), (27, "parameter", "a", "top"), (27, "parameter", "b", "top"),
            (31, "function", "later", None)])
        names = [name for _, _, name, _ in got]
        self.assertNotIn("inner", names)   # a function inside a function
        self.assertNotIn("hidden", names)  # and its parameters
        self.assertNotIn("SIZE", names)    # only module-level constants
        self.assertNotIn("attr", names)    # not a plain name

    def test_every_parameter_kind_in_signature_order(self):
        code = ("def collect(first, /, second, *items, key=None, **options):\n    pass\n\n\n"
                "class K:\n    def m(self, *args, flag, **kw):\n        pass\n\n    @classmethod\n    def n(cls, *rest):\n        pass\n")
        want = [(1, "function", "collect", None), (1, "parameter", "first", "collect"), (1, "parameter", "second", "collect"),
                (1, "parameter", "items", "collect"), (1, "parameter", "key", "collect"), (1, "parameter", "options", "collect"),
                (5, "class", "K", None),
                (6, "method", "m", "K"), (6, "parameter", "args", "m"), (6, "parameter", "flag", "m"), (6, "parameter", "kw", "m"),
                (10, "method", "n", "K"), (10, "parameter", "rest", "n")]
        self.assertEqual(ss.outline("k.py", self.numbered(code)), want)
        names = "\n".join("- line 1: parameter {} of collect".format(n) for n in ("first", "second", "items", "key", "options"))
        message = ss.user_message("TASK", [{"path": "k.py", "text": self.numbered(code)}])
        self.assertIn("- line 1: function collect\n" + names + "\n- line 5: class K\n- line 6: method m\n"
                      "- line 6: parameter args of m\n- line 6: parameter flag of m\n- line 6: parameter kw of m\n"
                      "- line 10: method n\n- line 10: parameter rest of n", message)

    def test_no_list(self):
        self.assertEqual(ss.outline("notes.txt", self.numbered(CODE)), [])
        self.assertEqual(ss.outline("a.py", self.numbered("def broken(:\n    pass")), [])
        self.assertEqual(ss.outline("a.py", ""), [])

    def test_user_message_names_block(self):
        files = [{"path": "c.py", "text": self.numbered("K = 1\n\n\nclass C:\n    def m(self, ttl):\n        pass")},
                 {"path": "n.txt", "text": self.numbered("K = 1")}]
        message = ss.user_message("TASK", files)
        block = ("Names defined in c.py (cover each one the task is about):\n- line 1: constant K\n- line 4: class C\n"
                 "- line 5: method m\n- line 5: parameter ttl of m")
        self.assertIn("```\n" + block + "\n\n## n.txt", message)
        self.assertEqual(message.count("Names defined in"), 1)  # none for a file with no outline


class NameIndexTests(unittest.TestCase):
    def test_exact_lines_and_skips(self):
        files = [{"path": "pkg/c.py", "text": OutlineTests.numbered(CODE)}, {"path": "n.txt", "text": "   1\tK = 1"}]
        self.assertEqual(ss.name_index(files), [
            "- c.py defines the constant TIMEOUT [pkg/c.py:2]",
            "- c.py defines the constant LIMIT [pkg/c.py:3]",
            "- c.py defines the constant A [pkg/c.py:4]",
            "- c.py defines the constant B [pkg/c.py:4]",
            "- c.py defines the class Cache [pkg/c.py:8]",
            "- c.py defines the method get [pkg/c.py:14]",
            "- c.py defines the method make [pkg/c.py:23]",
            "- c.py defines the function top [pkg/c.py:27]",
            "- c.py defines the function later [pkg/c.py:31]"])

    def test_every_line_on_cache_py_passes_rule_3(self):
        answers = [json.loads(line) for line in (base.DATA / "answers.jsonl").read_text().splitlines()]
        cite_files = next(a["cite_files"] for a in answers if "src/cache.py" in a["cite_files"])
        files = ss.prepare(FIXTURES, ["src/cache.py"])
        index = ss.name_index(files)
        self.assertEqual([l.split(" defines the ")[1].split(" [")[0] for l in index],  # no __init__, no parameters
                         ["class LRUCache", "method get", "method put", "method clear", "method __len__"])
        source = (FIXTURES / "src" / "cache.py").read_text().splitlines()
        for line in index:
            with self.subTest(line):
                fact = summarize_check.FACT_LINE.match(line)
                self.assertTrue(fact, line)
                path, number = summarize_check.CITATION.match(fact["cites"]).group("path", "a")
                self.assertIsNone(summarize_check._cite_problem(FIXTURES, cite_files, path, int(number), int(number)))
                name = line.split(" defines the ")[1].split(" ")[1]
                self.assertRegex(source[int(number) - 1], r"^\s*(def|class) {}\b|^{}\b".format(name, name))  # the def line itself


class SummarizeV2Tests(base.StubCase):
    def test_index_first_then_facts_and_the_record(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("LIMIT = 1\n\n\ndef f(x):\n    return x + LIMIT\n")
            self.stub.reply(json.dumps({"facts": [v2("Mod.f", "adds the limit", "./a.py", (4, 5, 5)), v2("f", "bad", "a.py", (1, 2, 3, 4))]}))
            text, record = ss.summarize(root, ["a.py"], "TASK", endpoint=self.stub.url, model="m", settings=base.SETTINGS)
        self.assertEqual(text, "- a.py defines the constant LIMIT [a.py:1]\n- a.py defines the function f [a.py:4]\n"
                               "- f adds the limit [a.py:4, a.py:5]")
        self.assertEqual((record["indexed"], record["kept"]), (2, 1))
        self.assertEqual([d["reason"] for d in record["drops"]], ["malformed"])
        sent = self.stub.requests[0]["body"]["messages"][1]["content"]
        self.assertIn("Names defined in a.py (cover each one the task is about):\n- line 1: constant LIMIT\n- line 4: function f\n"
                      "- line 4: parameter x of f", sent)

    def test_index_alone_when_no_fact_is_kept(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("def f():\n    pass\n")
            Path(root, "n.txt").write_text("hello\n")
            self.stub.reply(json.dumps({"facts": []}))
            text, record = ss.summarize(root, ["a.py", "n.txt"], "T", endpoint=self.stub.url, model="m", settings=base.SETTINGS)
        self.assertEqual((text, record["indexed"], record["kept"]), ("- a.py defines the function f [a.py:1]", 1, 0))


class ConfigV2Tests(unittest.TestCase):
    def test_example_key_loads_through_the_router_and_is_v2(self):
        settings = ss.load_settings(ROOT, ROOT / "config" / "local.example.json")
        item = settings["schema"]["properties"]["facts"]["items"]
        self.assertEqual(item["required"], ["subject", "claim", "path", "lines"])
        self.assertIs(item["additionalProperties"], False)
        self.assertEqual(sorted(item["properties"]), ["claim", "lines", "path", "subject"])
        self.assertEqual(item["properties"]["lines"], {"type": "array", "items": {"type": "integer"}, "minItems": 1, "maxItems": 3})
        self.assertIn("subject:", settings["system"])
        self.assertNotIn("line_start", json.dumps(settings))
        note = json.loads((ROOT / "config" / "local.example.json").read_text())["_summarize_structured"]
        self.assertIn("lines", note)
        self.assertNotIn("EXPERIMENT", (ROOT / "lib" / "summarize_structured.py").read_text())


if __name__ == "__main__":
    unittest.main()
