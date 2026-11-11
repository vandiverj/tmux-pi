"""lib/summarize_structured.py (numbered files, one structured call, validated and rendered facts) and the runner's `--path structured`
(tools/qual/summarize_run.py). The endpoint is a stub HTTP server on 127.0.0.1 (port 0); no model runs and nothing else is reached."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tools" / "qual"))
import summarize_check
import summarize_run
import summarize_structured as ss

DATA = ROOT / "tests" / "calibration" / "summarize"
SETTINGS = {"system": "SYSTEM TEXT", "schema": {"type": "object", "required": ["facts"], "properties": {"facts": {"type": "array"}}}}


class Stub:
    """A loopback server that answers every POST with the next canned reply and records what it was sent."""

    def __init__(self):
        self.status, self.body, self.requests = 200, b"{}", []
        self.release = threading.Event()  # a reply that must hang waits on this; the test releases it at the end
        self.hang = False
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                stub.requests.append({"path": self.path, "body": json.loads(self.rfile.read(length))})
                if stub.hang:
                    stub.release.wait(30)
                    return
                self.send_response(stub.status)
                self.send_header("Content-Length", str(len(stub.body)))
                self.end_headers()
                self.wfile.write(stub.body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:{}/v1".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, content=None, reasoning=None, status=200):
        message = {"role": "assistant", "content": content}
        if reasoning is not None:
            message["reasoning_content"] = reasoning
        self.status, self.body = status, json.dumps({"choices": [{"message": message}]}).encode()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


def fact(claim="f does x", path="a.py", a=1, b=1):
    return {"claim": claim, "path": path, "line_start": a, "line_end": b}


class StubCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub = Stub()
        cls.addClassCleanup(cls.stub.close)

    def setUp(self):
        self.stub.hang, self.stub.requests[:] = False, []
        self.stub.release.clear()
        self.stub.reply("{}")


class PrepareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        base = Path(os.path.realpath(temp.name))
        cls.outside = base / "outside"
        cls.root = base / "root"
        cls.outside.mkdir()
        (cls.root / "src").mkdir(parents=True)
        (cls.root / "config").mkdir()
        (cls.root / "a.py").write_text("one\ntwo\n\nfour")
        (cls.root / "src" / "b.py").write_text("x = 1\n")
        for name in (".env", ".env.local", "config/local.json", "id_rsa", "notes.pem"):
            (cls.root / name).write_text("SECRET\n")
        (cls.outside / "o.py").write_text("o = 1\n")
        (cls.outside / ".env").write_text("SECRET\n")
        (cls.root / "link_out.py").symlink_to(cls.outside / "o.py")
        (cls.root / "link_dir").symlink_to(cls.outside)
        (cls.root / "link_env.py").symlink_to(cls.root / ".env")
        (cls.root / "link_in.py").symlink_to(cls.root / "a.py")
        (cls.root / "binary.bin").write_bytes(b"\xff\xfe\x00bad")

    def test_numbers_each_line_per_file(self):
        files = ss.prepare(self.root, ["a.py", "./src/b.py"], 1000)
        self.assertEqual([f["path"] for f in files], ["a.py", "src/b.py"])
        self.assertEqual(files[0]["text"], "   1\tone\n   2\ttwo\n   3\t\n   4\tfour")
        self.assertEqual((files[0]["lines"], files[0]["chars"], files[1]["text"]), (4, len(files[0]["text"]), "   1\tx = 1"))
        self.assertEqual(ss.prepare(self.root, ["link_in.py"], 1000)[0]["lines"], 4)  # a symlink that stays inside is fine

    def test_refusals(self):
        cases = {
            "../outside/o.py": "outside the root", str(Path("/etc/hosts")): "outside the root",
            "link_out.py": "symlink that leaves", "link_dir/o.py": "symlink that leaves",
            ".env": "environment file", ".env.local": "environment file", "link_env.py": "refused",
            "config/local.json": "withheld", "id_rsa": "withheld", "notes.pem": "withheld",
            "": "empty", "missing.py": "cannot read", "src": "cannot read", "binary.bin": "cannot read",
        }
        for path, why in cases.items():
            with self.subTest(path=path), self.assertRaises(ss.PrepareError) as caught:
                ss.prepare(self.root, [path], 1000)
            self.assertIn(why, str(caught.exception))
            self.assertNotIn("SECRET", str(caught.exception))

    def test_trust_store_is_refused(self):
        store = self.root / "storedir"
        store.mkdir(exist_ok=True)
        (store / "grant.json").write_text("{}")
        with mock.patch.object(ss.trust, "trust_dir", return_value=str(store)):
            with self.assertRaises(ss.PrepareError) as caught:
                ss.prepare(self.root, ["storedir/grant.json"], 1000)
        self.assertIn("trust store", str(caught.exception))

    def test_one_refused_path_refuses_all(self):
        with self.assertRaises(ss.PrepareError):
            ss.prepare(self.root, ["a.py", ".env"], 1000)

    def test_budget_names_the_sizes(self):
        a, b = len(ss.prepare(self.root, ["a.py"], 1000)[0]["text"]), len(ss.prepare(self.root, ["src/b.py"], 1000)[0]["text"])
        self.assertEqual(len(ss.prepare(self.root, ["a.py", "src/b.py"], a + b)), 2)  # exactly the budget is allowed
        with self.assertRaises(ss.PrepareError) as caught:
            ss.prepare(self.root, ["a.py", "src/b.py"], a + b - 1)
        message = str(caught.exception)
        for part in ("total {} chars".format(a + b), "budget of {}".format(a + b - 1), "a.py {} chars".format(a), "src/b.py {} chars".format(b)):
            self.assertIn(part, message)


class AskTests(StubCase):
    def ask(self, timeout=5):
        return ss.ask(self.stub.url, "m/1", "SYS", "USER", SETTINGS["schema"], timeout)

    def test_content_then_reasoning_content(self):
        self.stub.reply('{"facts": []}')
        self.assertEqual(self.ask(), '{"facts": []}')
        self.stub.reply("", reasoning='{"facts": [1]}')  # LM Studio: the JSON is in reasoning_content while reasoning is on
        self.assertEqual(self.ask(), '{"facts": [1]}')
        self.stub.reply(None, reasoning='{"facts": [2]}')
        self.assertEqual(self.ask(), '{"facts": [2]}')
        self.stub.reply('{"facts": [3]}', reasoning='{"facts": [4]}')  # content wins when both are there
        self.assertEqual(self.ask(), '{"facts": [3]}')

    def test_lmstudio_cap_is_an_ask_error_and_sends_nothing(self):
        """The router's cap on LM Studio requests reaches summarize_structured as its normal failure (an AskError), never a hang."""
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "count"
            counter.write_text("0")
            port = self.stub.url.split(":")[2].split("/")[0]
            env = {"TP_LMSTUDIO_ENDPOINT": "127.0.0.1:" + port, "TP_LMSTUDIO_BUDGET": "0", "TP_LMSTUDIO_BUDGET_FILE": str(counter)}
            with mock.patch.dict(os.environ, env), self.assertRaises(ss.AskError) as caught:
                self.ask()
        self.assertIn("cap is 0", str(caught.exception))
        self.assertIn("TP_LMSTUDIO_BUDGET", str(caught.exception))
        self.assertEqual(self.stub.requests, [])

    def test_request_shape(self):
        self.ask()
        (request,) = self.stub.requests
        self.assertEqual(request["path"], "/v1/chat/completions")
        body = request["body"]
        self.assertEqual((body["model"], body["temperature"]), ("m/1", 0))
        self.assertEqual(body["messages"], [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}])
        self.assertEqual(body["response_format"], {"type": "json_schema", "json_schema": {"name": "facts", "strict": True, "schema": SETTINGS["schema"]}})
        ss.ask(self.stub.url + "/", "m", "s", "u", {}, 5)  # a trailing slash still posts to one /chat/completions
        self.assertEqual(self.stub.requests[-1]["path"], "/v1/chat/completions")

    def test_http_error_names_endpoint_and_status_not_the_body(self):
        self.stub.body, self.stub.status = b"the file said SECRET-TOKEN", 500
        with self.assertRaises(ss.AskError) as caught:
            self.ask()
        self.assertEqual((caught.exception.status, caught.exception.endpoint), (500, self.stub.url + "/chat/completions"))
        self.assertIn("500", str(caught.exception))
        self.assertIn(self.stub.url, str(caught.exception))
        self.assertNotIn("SECRET-TOKEN", str(caught.exception))

    def test_not_json(self):
        for body in (b"SECRET-TOKEN not json", b'{"choices": []}', b'{"choices": [{"message": {"content": "SECRET-TOKEN prose"}}]}',
                     b'{"choices": [{"message": {"content": ""}}]}'):
            self.stub.body = body
            with self.subTest(body=body), self.assertRaises(ss.AskError) as caught:
                self.ask()
            self.assertIn("not JSON", str(caught.exception))
            self.assertNotIn("SECRET-TOKEN", str(caught.exception))

    def test_timeout(self):
        self.stub.hang = True  # the stub holds the reply until the test ends; only the client's timeout can end the call
        with self.assertRaises(ss.AskError) as caught:
            self.ask(timeout=0.3)
        self.assertIn("timeout", str(caught.exception))
        self.assertIsNone(caught.exception.status)

    def test_unreachable(self):
        dead = Stub()
        dead.close()
        with self.assertRaises(ss.AskError) as caught:
            ss.ask(dead.url, "m", "s", "u", {}, 2)
        self.assertIn("cannot reach", str(caught.exception))


class ValidateTests(unittest.TestCase):
    FILES = {"a.py": 10, "src/b.py": 3}

    def test_path_normalization(self):
        for given in ("a.py", " a.py ", "`a.py`", "./a.py", "././a.py", "a.py:3", "a.py:3-4", "`./a.py:3-4`", " ./a.py:3 "):
            kept, drops = ss.validate([fact(path=given, a=3, b=4)], self.FILES)
            self.assertEqual((kept, drops), ([fact(path="a.py", a=3, b=4)], []), given)

    def test_drop_reasons(self):
        table = [
            (fact(path="c.py"), "unnamed_file"), (fact(path="src"), "unnamed_file"), (fact(path="../a.py"), "unnamed_file"),
            (fact(a=0, b=1), "range_outside_file"), (fact(a=10, b=11), "range_outside_file"), (fact(a=11, b=11), "range_outside_file"),
            (fact(path="src/b.py", a=3, b=4), "range_outside_file"), (fact(a=-1, b=2), "range_outside_file"),
            (fact(a=5, b=4), "range_reversed"), (fact(a=1, b=6), "range_too_long"), (fact(a=2, b=10), "range_too_long"),
            (fact(claim="  "), "empty_claim"), ({"claim": "x"}, "malformed"), (fact(a="1"), "malformed"), (fact(a=True), "malformed"),
            (fact(claim=None), "malformed"), ("not a dict", "malformed"),
        ]
        for item, reason in table:
            kept, drops = ss.validate([item], self.FILES)
            self.assertEqual((kept, drops), ([], [{"fact": item, "reason": reason}]), item)

    def test_five_lines_kept_six_dropped(self):
        kept, drops = ss.validate([fact(a=1, b=5), fact(a=6, b=10), fact(a=1, b=6), fact(a=4, b=10)], self.FILES)
        self.assertEqual([(f["line_start"], f["line_end"]) for f in kept], [(1, 5), (6, 10)])
        self.assertEqual([(d["fact"]["line_start"], d["reason"]) for d in drops], [(1, "range_too_long"), (4, "range_too_long")])

    def test_keeps_order_and_never_changes_a_line(self):
        facts = [fact("one", "a.py:9", 7, 9), fact("bad", "zz.py"), fact("two", "src/b.py", 1, 3)]
        kept, drops = ss.validate(facts, self.FILES)
        self.assertEqual(kept, [fact("one", "a.py", 7, 9), fact("two", "src/b.py", 1, 3)])
        self.assertEqual(drops, [{"fact": facts[1], "reason": "unnamed_file"}])  # the path's :N suffix does not replace line_start


class RenderTests(unittest.TestCase):
    def test_exact_text(self):
        facts = [fact("f   returns\n x.", "a.py", 3, 3), fact("g is a flag...", "src/b.py", 2, 4), fact("h", "a.py", 7, 7)]
        text = ss.render(facts)
        self.assertEqual(text, "- f returns x [a.py:3]\n- g is a flag [src/b.py:2-4]\n- h [a.py:7]")
        self.assertEqual(ss.render([]), "")

    def test_parses_with_the_checker(self):
        text = ss.render([fact("f returns x.", "a.py", 3, 3), fact("g is a flag", "src/b.py", 2, 4)])
        found = [summarize_check.FACT_LINE.match(line).groupdict() for line in text.splitlines()]
        self.assertEqual(found, [{"claim": "f returns x", "cites": "a.py:3"}, {"claim": "g is a flag", "cites": "src/b.py:2-4"}])


class SummarizeTests(StubCase):
    def test_record_and_text(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("def f():\n    return 1\n")
            self.stub.reply(json.dumps({"facts": [fact("f returns 1.", "./a.py:2", 2, 2), fact("ghost", "z.py"), fact("long", "a.py", 1, 9)]}))
            text, record = ss.summarize(root, ["a.py"], "TASK", endpoint=self.stub.url, model="m", settings=SETTINGS)
        self.assertEqual(text, "- a.py defines the function f [a.py:1]\n- f returns 1 [a.py:2]")
        self.assertEqual(record["indexed"], 1)
        self.assertEqual((record["model"], record["kept"], record["chars"]), ("m", 1, len("   1\tdef f():\n   2\t    return 1")))
        self.assertEqual(record["files"], [{"path": "a.py", "lines": 2, "chars": record["chars"]}])
        self.assertEqual([d["reason"] for d in record["drops"]], ["unnamed_file", "range_outside_file"])
        self.assertIsInstance(record["seconds"], float)
        sent = self.stub.requests[0]["body"]
        self.assertEqual(sent["messages"][0]["content"], "SYSTEM TEXT")
        self.assertIn("# Task\n\nTASK\n", sent["messages"][1]["content"])
        self.assertIn("   2\t    return 1", sent["messages"][1]["content"])

    def test_reply_without_facts_list(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("x\n")
            for body in ('{"nope": 1}', "[]", '{"facts": "x"}'):
                self.stub.reply(body)
                with self.subTest(body=body), self.assertRaises(ss.AskError):
                    ss.summarize(root, ["a.py"], "T", endpoint=self.stub.url, model="m", settings=SETTINGS)

    def test_refused_path_sends_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ss.PrepareError):
                ss.summarize(root, [".env"], "T", endpoint=self.stub.url, model="m", settings=SETTINGS)
        self.assertEqual(self.stub.requests, [])


class SettingsTests(StubCase):
    EXAMPLE = ROOT / "config" / "local.example.json"

    def test_real_example_loads_through_the_router_loader(self):
        want = json.loads(self.EXAMPLE.read_text())["summarize_structured"]
        settings = ss.load_settings(ROOT, self.EXAMPLE)
        self.assertEqual(settings, want)
        self.assertTrue(settings["system"].strip() and settings["schema"]["required"] == ["facts"])
        self.assertEqual(ss.default_endpoint(ROOT, self.EXAMPLE), "http://127.0.0.1:1234/v1")

    def test_machine_config_without_the_key_falls_back_to_the_example(self):
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp, "local.json")
            local.write_text(json.dumps({"redaction": {"mode": "shadow"}}))
            self.assertEqual(ss.load_settings(ROOT, local), json.loads(self.EXAMPLE.read_text())["summarize_structured"])
            local.write_text(json.dumps({"summarize_structured": {"system": "MINE", "schema": {"type": "object"}}}))
            self.assertEqual(ss.load_settings(ROOT, local)["system"], "MINE")  # the machine's own text wins
            with mock.patch.dict(os.environ, {"TP_LOCAL_CONFIG": "/nonexistent"}):
                self.assertEqual(ss.load_settings(ROOT)["system"], json.loads(self.EXAMPLE.read_text())["summarize_structured"]["system"])

    def test_example_settings_drive_summarize(self):
        settings = ss.load_settings(ROOT, self.EXAMPLE)
        self.stub.reply(json.dumps({"facts": [fact("f returns 1", "a.py", 1, 1)]}))
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("return 1\n")
            text, _ = ss.summarize(root, ["a.py"], "T", endpoint=self.stub.url, model="m", settings=settings)
        body = self.stub.requests[0]["body"]
        self.assertEqual(text, "- f returns 1 [a.py:1]")
        self.assertEqual(body["messages"][0]["content"], settings["system"])
        self.assertEqual(body["response_format"]["json_schema"]["schema"], settings["schema"])


class RunnerStructuredTests(StubCase):
    CHORE = "use-lrucache"

    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.work = self.dir / "work"  # tempfile's directory: nothing may be left in it
        self.work.mkdir()
        patch = mock.patch.object(tempfile, "tempdir", str(self.work))
        patch.start()
        self.addCleanup(patch.stop)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name in ("tmux", "git", "pi"):  # a call to any of them lands in tools.log
            script = bin_dir / name
            script.write_text('#!/bin/sh\necho "$0 $*" >> {}\n'.format(self.dir / "tools.log"))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.env = {"PATH": "{}:{}".format(bin_dir, os.environ["PATH"]), "TP_LOCAL_CONFIG": "/nonexistent"}
        patch = mock.patch.object(ss, "load_settings", return_value=SETTINGS)
        patch.start()
        self.addCleanup(patch.stop)
        answer = next(a for a in summarize_run.read_jsonl(DATA / "answers.jsonl") if a["id"] == self.CHORE)
        self.good = []
        for line in answer["reference"]:
            m = summarize_check.FACT_LINE.match(line["text"])
            path, a, b = re.match(r"(.+):(\d+)(?:-(\d+))?$", m["cites"]).groups()
            self.good.append(fact(m["claim"], path, int(a), int(b or a)))

    def run_main(self, *extra):
        out = self.dir / "out.jsonl"
        argv = ["--path", "structured", "--model", "lmstudio/qwen/q", "--split", "tune", "--only", self.CHORE, "--out", str(out),
                "--endpoint", self.stub.url, *extra]
        with mock.patch("builtins.print"):
            rc = summarize_run.main(argv, env=self.env)
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        return rc, lines[:-1], lines[-1]

    def assert_clean(self):
        self.assertEqual(list(self.work.iterdir()), [], "temp dir left behind")
        self.assertFalse((self.dir / "tools.log").exists(), "tmux, git or pi was called")

    def test_records_summary_and_drops(self):
        self.stub.reply(json.dumps({"facts": self.good + [fact("ghost", "nowhere.py", 1, 1)]}))
        rc, records, summary = self.run_main("--runs", "2")
        self.assertEqual(rc, 0)
        self.assertEqual([(r["chore"], r["run"], r["pass"], r["error"], len(r["drops"])) for r in records],
                         [(self.CHORE, 1, True, None, 1), (self.CHORE, 2, True, None, 1)])
        self.assertEqual(records[0]["drops"][0]["reason"], "unnamed_file")
        self.assertEqual(records[0]["model"], "qwen/q")  # the server's name: the provider prefix is gone
        self.assertEqual(self.stub.requests[0]["body"]["model"], "qwen/q")
        self.assertEqual((summary["total"], summary["passed"], summary["fabricated_citations"], summary["dropped_facts"],
                          summary["path"]), (2, 2, 0, 2, "structured"))
        self.assertEqual(len(self.stub.requests), 2)
        self.assert_clean()

    def test_endpoint_failure_is_an_error_record(self):
        self.stub.body, self.stub.status = b"x", 500
        rc, records, summary = self.run_main()
        self.assertEqual(rc, 0)
        self.assertFalse(records[0]["pass"])
        self.assertIn("AskError", records[0]["error"])
        self.assertIn("500", records[0]["error"])
        self.assertEqual((summary["total"], summary["passed"]), (1, 0))
        self.assert_clean()

    def test_default_path_is_still_pi(self):
        with mock.patch.object(summarize_run, "run_structured") as structured, mock.patch.object(summarize_run, "run_chores",
                                                                                                  return_value=([], {})) as chores, \
                mock.patch("builtins.print"):
            summarize_run.main(["--model", "m", "--only", self.CHORE, "--split", "tune", "--out", str(self.dir / "o.jsonl")], env=self.env)
        self.assertEqual((structured.called, chores.called), (False, True))


@unittest.skipUnless(os.environ.get("TP_LIVE_LMSTUDIO") == "1", "live LM Studio request: set TP_LIVE_LMSTUDIO=1 (budget 20, 300 s per request)")
class LiveLmStudioTests(unittest.TestCase):
    """One full-size (60k characters, the default limit) summarizer request to the real LM Studio, to determine the limit and wait
    for the longer response. Opt-in; the budget is 20 requests, one at a time (tests/__init__.py)."""
    TIMEOUT = 300

    def test_full_size_request_returns_well_formed_facts(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        lines = ["def f{n}(x):\n    return x + {n}\n".format(n=n) for n in range(4000)]
        (root / "big.py").write_text("".join(lines)[:ss.DEFAULT_BUDGET_CHARS])
        text, record = ss.summarize(root, ["big.py"], "what do the functions return", endpoint=ss.default_endpoint(ROOT),
                                    model=os.environ.get("TP_LIVE_LMSTUDIO_MODEL", "qwen/qwen3.6-35b-a3b"),
                                    settings=ss.load_settings(ROOT), timeout=self.TIMEOUT)
        self.assertIsInstance(text, str)
        self.assertGreater(record["chars"], 50000)
        self.assertGreaterEqual(record["kept"] + record["indexed"], 1)


if __name__ == "__main__":
    unittest.main()
