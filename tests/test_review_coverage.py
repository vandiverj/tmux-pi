"""A review without a Coverage section is incomplete: its stated verdict is kept, and no consumer reads it as an approval.

One test per consumer of a verdict: lib/review_verdict.py itself, tp-agent (record / inspect / collect, the injected index,
escalation evidence), lib/task_record.py (the approval gate), lib/routing_evals.py, the dispatcher (tests/test_dispatcher_runtime.py,
scenarios no-coverage and empty-coverage), the Claude MCP collect_result and bin/tp-review's instructions to the reviewer."""
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import review_verdict  # noqa: E402
import routing_evals  # noqa: E402
import task_record  # noqa: E402

COMPLETE = "Coverage\nreviewed src/a.py; checked its callers.\n"
CASES_INCOMPLETE = {
    "no section": "APPROVED\nRESULT_STATUS: complete\nLooks fine.",
    "empty section": "APPROVED\nRESULT_STATUS: complete\nCoverage\n",
    "empty section, blank lines": "APPROVED\nCoverage:\n\n\n",
    "empty section before another heading": "APPROVED\n## Coverage\n\n## Notes\nx",
    "later empty section after a full one": "APPROVED\nCoverage\nreviewed a.\nRound 2\nCoverage\n",
    "heading word inside a sentence": "APPROVED\nThe coverage of the tests is thin.",
    "only the verdict line": "APPROVED\n",
    "heading inside a code fence": "APPROVED\nFindings:\n```markdown\nCoverage\nReviewed all changed files\n```\n",
    "heading inside a tilde fence": "APPROVED\n~~~\nCoverage\nreviewed a.py and checked its callers\n~~~\n",
    "heading inside an unclosed fence": "APPROVED\n```\nCoverage\nreviewed a.py and checked its callers\n",
    "heading inside a quotation": "APPROVED\n> Coverage\n> reviewed a.py\n",
    "only RESULT_STATUS in the body": "APPROVED\nCoverage\nRESULT_STATUS: complete\n",
    "boilerplate body": "APPROVED\nCoverage\nN/A\n",
    "boilerplate on the heading line": "APPROVED\nCoverage: none\n",
    "same line as the heading": "APPROVED\nCoverage: reviewed a.py; nothing unreviewed",
    "metadata in list form": "APPROVED\nCoverage\n- RESULT_STATUS: complete\n",
    "metadata with emphasis and bullets": "APPROVED\nCoverage\n* **RESULT_STATUS: complete**\n",
    "metadata, long value": "APPROVED\nCoverage\nVERDICT: approved and nothing else here\n",
    "two-word line": "APPROVED\nCoverage\nreviewed a.py\n",
    "heading and body in an HTML comment": "APPROVED\n<!--\nCoverage\nreviewed a.py and checked its callers\n-->\n",
    "comment around the body": "APPROVED\nCoverage\n<!-- reviewed a.py and checked its callers -->\n",
    "unterminated comment": "APPROVED\nCoverage\nreviewed a.py and checked its callers\n<!-- later\n",
    "unterminated fence after a real section": "APPROVED\nCoverage\nreviewed a.py and checked its callers\n```\nx\n",
    "indented fence": "APPROVED\n  ```\n  Coverage\n  reviewed a.py and checked its callers\n  ```\n",
    "longer fence closed by a shorter one stays open": "APPROVED\n````\nCoverage\nreviewed a.py and checked its callers\n```\n",
    "tilde fence inside backticks stays quoted": "APPROVED\n```\n~~~\nCoverage\nreviewed a.py and checked its callers\n~~~\n```\n",
    "nested quotations": "APPROVED\n> > Coverage\n> > reviewed a.py and checked its callers\n",
    "quotation with spaces between markers": "APPROVED\n> > > Coverage\n>> reviewed a.py and checked its callers\n",
    "CRLF empty section": "APPROVED\r\nCoverage\r\n\r\n",
    "CRLF fenced": "APPROVED\r\n```\r\nCoverage\r\nreviewed a.py and checked its callers\r\n```\r\n",
    "table separator only": "APPROVED\nCoverage\n|---|---|\n",
    "placeholder repeated with words": "APPROVED\nCoverage\nsame as above\n",
    "quotation-only body": "APPROVED\nCoverage\n> reviewed a.py\n",
}
# every reader of a review is exercised with each of these; none may present them as a complete approval
REFUSED = {
    "contradictory statuses": "APPROVED\nRESULT_STATUS: REQUEST_CHANGES\nCoverage\nreviewed src/a.py and checked its callers\n",
    "contradicted after coverage": "APPROVED\nRESULT_STATUS: complete\nCoverage\nreviewed a.py and checked its callers\nRESULT_STATUS: REQUEST_CHANGES",
    "unparseable": "Looks good to me.\nCoverage\nreviewed a.py and checked its callers\n",
}
CASES_COMPLETE = {
    "plain heading": "APPROVED\n" + COMPLETE,
    "markdown heading": "APPROVED\n## Coverage\nreviewed a.py and checked its callers\n",
    "bold heading": "APPROVED\n**Coverage**\nreviewed a.py and checked its callers\n",
    "colon then body": "APPROVED\nCoverage:\n- a.py reviewed along with its callers\n",
    "lowercase": "APPROVED\ncoverage\nreviewed a.py and checked its callers",
    "bold with colon inside": "APPROVED\n**Coverage:**\nreviewed a.py and checked its callers\n",
    "heading with trailing hashes": "APPROVED\n## Coverage ##\nreviewed a.py and checked its callers\n",
    "list body": "APPROVED\nCoverage\n- Changed files: all reviewed, none skipped at all\n- RESULT_STATUS: complete\n",
    "metadata plus one statement": "APPROVED\nCoverage\nRESULT_STATUS: complete\nI read every changed file today\n",
    "numbered body": "APPROVED\nCoverage\n1. reviewed a.py and its callers\n",
    "closed comment elsewhere": "APPROVED\n<!-- note -->\nCoverage\nreviewed a.py and checked its callers\n",
    "fenced quote earlier, real section later": "APPROVED\n```\nCoverage\n```\nCoverage\nreviewed a.py and checked its callers\n",
}


def agent_module():
    loader = importlib.machinery.SourceFileLoader("tp_agent_under_test", str(ROOT / "bin" / "tp-agent"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SharedFunctionTests(unittest.TestCase):
    def test_missing_or_empty_coverage_is_incomplete_but_keeps_the_stated_verdict(self):
        for label, text in CASES_INCOMPLETE.items():
            with self.subTest(label):
                verdict, _, incomplete = review_verdict.assess(text)
                self.assertEqual(verdict, "APPROVED")
                self.assertIn("Coverage", incomplete)
                self.assertFalse(review_verdict.approves(text))

    def test_a_non_empty_coverage_section_is_complete(self):
        for label, text in CASES_COMPLETE.items():
            with self.subTest(label):
                self.assertIsNone(review_verdict.assess(text)[2])
                self.assertTrue(review_verdict.approves(text))

    def test_notes_and_request_changes_follow_the_same_rule(self):
        self.assertFalse(review_verdict.approves("APPROVED_WITH_NOTES\nN1 gap."))
        self.assertTrue(review_verdict.approves("APPROVED_WITH_NOTES\nN1 gap.\n" + COMPLETE))
        verdict, _, incomplete = review_verdict.assess("REQUEST_CHANGES\nF1 defect.")
        self.assertEqual(verdict, "REQUEST_CHANGES")
        self.assertIsNotNone(incomplete)

    def test_the_command_reports_completeness_and_refuses_an_unreadable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text, complete in (("APPROVED\nx", False), ("APPROVED\n" + COMPLETE, True)):
                Path(tmp, "r.md").write_text(text)
                out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), f"{tmp}/r.md"], capture_output=True, text=True)
                value = json.loads(out.stdout)
                self.assertEqual((out.returncode, value["verdict"], value["complete"]), (0, "APPROVED", complete))
                self.assertEqual(value["incomplete_reason"] is None, complete)
            missing = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), f"{tmp}/nope.md"], capture_output=True, text=True)
            self.assertEqual(missing.returncode, 1)
            self.assertEqual(missing.stdout, "")


class ReadReviewTests(unittest.TestCase):
    """The one strict reader: nothing is replaced, escaped or defaulted."""

    def read(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "result.md")
            if data is not None:
                path.write_bytes(data)
            return review_verdict.read_review(path)

    def test_a_good_file_is_read(self):
        self.assertEqual(self.read(b"APPROVED\n" + COMPLETE.encode()), "APPROVED\n" + COMPLETE)

    def test_every_unreadable_shape_refuses_with_a_reason(self):
        for label, data, expected in (("undecodable", b"APPROVED\nCoverage\nreviewed a.py and its callers\xff\n", "UTF-8"),
                                      ("empty", b"", "empty"), ("blank", b" \n\n", "empty"),
                                      ("oversized", b"A" * (review_verdict.MAX_REVIEW_BYTES + 1), "larger"),
                                      ("missing", None, "does not exist")):
            with self.subTest(label), self.assertRaises(ValueError) as caught:
                self.read(data)
            self.assertIn(expected, str(caught.exception))
        self.assertTrue(issubclass(review_verdict.ReviewMissing, ValueError))

    def test_the_command_refuses_an_undecodable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "r.md").write_bytes(b"APPROVED\nCoverage\nreviewed a.py and its callers\xff\n")
            out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), f"{tmp}/r.md"], capture_output=True, text=True)
            self.assertEqual((out.returncode, out.stdout), (1, ""))
            self.assertIn("UTF-8", out.stderr)

    def test_genuine_reviews_from_this_work_are_complete(self):
        for name in ("genuine_review_a.md", "genuine_review_b.md"):
            with self.subTest(name):
                text = review_verdict.read_review(ROOT / "tests" / "fixtures" / name)
                self.assertEqual(review_verdict.assess(text), ("REQUEST_CHANGES", review_verdict.assess(text)[1], None))


class NormaliseTests(unittest.TestCase):
    def test_table(self):
        for label, text, expected in (
                ("CRLF", "a\r\nb\rc", "a\nb\nc"),
                ("backtick fence", "a\n```py\nx\n```\nb", "a\nb"),
                ("tilde fence", "a\n~~~\nx\n~~~\nb", "a\nb"),
                ("indented fence", "a\n   ```\nx\n   ```\nb", "a\nb"),
                ("longer closing fence", "a\n```\nx\n`````\nb", "a\nb"),
                ("html comment", "a<!-- x\ny -->b", "ab"),
                ("blockquote", "a\n> x\n> > y\n  >z\nb", "a\nb")):
            with self.subTest(label):
                self.assertEqual(review_verdict.normalise(text), expected)

    def test_unterminated_fence_or_comment_refuses(self):
        for text in ("a\n```\nx", "a\n~~~\nx\n```\n", "a <!-- x", "a\n````\nx\n```"):
            with self.subTest(text), self.assertRaises(ValueError):
                review_verdict.normalise(text)


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        (self.state / "tasks").mkdir()
        self.agent = agent_module()

    def task(self, ident, reply, review_of=None, meta=None):
        directory = self.state / "tasks" / ident
        directory.mkdir()
        (directory / "meta.json").write_text(json.dumps({"id": ident, "worker": "claude", "created": "2026-10-08T10:00:00Z", **(meta or {})}))
        (directory / "result.md").write_text(reply)
        (directory / "exit").write_text("0")
        if review_of:
            (directory / "review-of").write_text(review_of)
        return directory

    # tp-agent record / inspect / collect
    def test_tp_agent_record_marks_the_review_incomplete_and_keeps_the_verdict(self):
        d = self.task("rv", "APPROVED\nRESULT_STATUS: complete\nfine", review_of="author")
        rec = self.agent.record(d, include_result=True)
        self.assertEqual((rec["verdict"], rec["review_complete"], rec["result_status"], rec["stated_result_status"]),
                         ("APPROVED", False, "incomplete", "complete"))
        self.assertIn("no Coverage section", rec["review_incomplete_reason"])
        self.assertIn("does not count as an approval", rec["review_notice"])
        done = self.task("rv2", "APPROVED\nRESULT_STATUS: complete\n" + COMPLETE, review_of="author")
        ok = self.agent.record(done)
        self.assertEqual((ok["verdict"], ok["review_complete"], ok["result_status"]), ("APPROVED", True, "complete"))
        self.assertNotIn("review_notice", ok)

    def test_tp_agent_collect_and_inspect_say_so_in_words(self):
        self.task("rv", "REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 defect", review_of="author")
        self.task("rv-ok", "APPROVED\nRESULT_STATUS: complete\n" + COMPLETE, review_of="author")
        env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TMUX": "", "TMUX_PANE": ""}
        for command in ("inspect", "collect"):
            out = subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), command, "rv"], capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            value = json.loads(out.stdout)
            self.assertIn("review is incomplete", value["notice"], command)
            self.assertEqual((value["verdict"], value["review_complete"]), ("REQUEST_CHANGES", False), command)
            clean = subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), command, "rv-ok"], capture_output=True, text=True, env=env, timeout=60)
            self.assertNotIn("notice", json.loads(clean.stdout), command)
        self.assertIn("incomplete", out.stderr or subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), "collect", "rv"],
                                                                 capture_output=True, text=True, env=env).stderr)

    # the injected index
    def test_the_session_index_shows_an_incomplete_review_as_incomplete(self):
        self.task("rv", "APPROVED\nRESULT_STATUS: complete\nfine", review_of="author")
        self.task("rv-ok", "APPROVED\nRESULT_STATUS: complete\n" + COMPLETE, review_of="author")
        index = self.agent.write_index(self.state)
        rows = {row["id"]: row for row in index["recent"]}
        self.assertEqual((rows["rv"]["verdict"], rows["rv"]["review_complete"], rows["rv"]["result_status"]), ("APPROVED", False, "incomplete"))
        self.assertIn("Coverage", rows["rv"]["review_incomplete_reason"])
        self.assertEqual((rows["rv-ok"]["verdict"], rows["rv-ok"]["review_complete"]), ("APPROVED", True))
        author = self.task("author", "done\nRESULT_STATUS: complete\n")
        self.assertNotIn("verdict", {row["id"]: row for row in self.agent.write_index(self.state)["recent"]}["author"])

    # escalation evidence
    def test_escalation_counts_a_stated_request_changes_and_never_an_incomplete_approval(self):
        for ident, reply, expected in (("a", "REQUEST_CHANGES\nF1 defect", 1), ("b", "REQUEST_CHANGES\nF1\n" + COMPLETE, 1),
                                       ("c", "APPROVED\nfine", 0), ("d", "APPROVED_WITH_NOTES\nN1", 0), ("e", "APPROVED\n" + COMPLETE, 0)):
            review = self.task(f"rv-{ident}", reply, review_of=ident)
            author = self.task(ident, "x\nRESULT_STATUS: complete\n", meta={"review": review.name})
            self.assertEqual(self.agent.escalation_evidence(self.state, author)[1], expected, ident)

    # the approval gate
    def test_the_approval_gate_refuses_an_incomplete_approval(self):
        for reply, expected in (("APPROVED\nfine", False), ("APPROVED\n" + COMPLETE, True), ("APPROVED_WITH_NOTES\nN1\nCoverage\n", False)):
            with self.subTest(reply=reply):
                tasks = self.state / "tasks"
                shutil.rmtree(tasks); tasks.mkdir()
                author = self.task("author", "Work", meta={"review": "rv", "worker": "pi", "id": "author"})
                reviewer = self.task("rv", reply, review_of="author", meta={"worker": "claude"})
                text = reply.strip()
                approval = {"required": True, "status": "approved", "task_id": "author", "round_nonce": "n", "approval": {
                    "round_nonce": "n", "reviewer_id": "rv", "evidence_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "author_sha256": hashlib.sha256(b"Work").hexdigest()}}
                (author / "review-required.json").write_text(json.dumps(approval))
                meta = json.loads((author / "meta.json").read_text())
                self.assertEqual(task_record.approved_delegated_review(author, meta, approval), expected)

    # routing evals
    def test_routing_evals_count_an_incomplete_review_as_incomplete(self):
        for reply, expected, stated in (("APPROVED\nlooks right", "incomplete", "APPROVED"),
                                        ("APPROVED\nlooks right\n" + COMPLETE, "APPROVED", None),
                                        ("REQUEST_CHANGES\nx", "incomplete", "REQUEST_CHANGES")):
            tasks = self.state / "tasks"
            shutil.rmtree(tasks); tasks.mkdir()
            self.task("rv", reply, review_of="auto-1")
            self.task("auto-1", "done", meta={"review": "rv"})
            dirs = {p.name: p for p in tasks.iterdir()}
            summary = routing_evals.task_summary(dirs["auto-1"], dirs)
            self.assertEqual(summary["review"]["verdict"], expected, reply)
            self.assertEqual(summary["review"].get("stated_verdict"), stated, reply)

    # the Claude MCP collect_result
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_mcp_collect_result_says_a_review_is_not_an_approval(self):
        self.task("rv", "APPROVED\nRESULT_STATUS: complete\nfine", review_of="author")
        self.task("rv-ok", "APPROVED\nRESULT_STATUS: complete\n" + COMPLETE, review_of="author")
        self.task("plain", "APPROVED\nRESULT_STATUS: complete\nfine")
        requests = [{"id": 1, "method": "initialize", "params": {}}, {"method": "notifications/initialized"}]
        for n, ident in enumerate(("rv", "rv-ok", "plain"), start=2):
            requests.append({"id": n, "method": "tools/call", "params": {"name": "collect_result", "arguments": {"id": ident}}})
        fake = Path(self.tmp.name, "fakebin")
        fake.mkdir()
        (fake / "tmux").write_text('#!/bin/sh\ncase "$1" in\n display-message) printf \'%s\\n\' "$TP_STATE_DIR" ;;\n *) exit 0 ;;\nesac\n')
        (fake / "tmux").chmod(0o755)
        env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "TMUX": "/nonexistent/tmux-pi-test.sock,1,0", "TP_MAIN": "claude",
               "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture"}
        out = subprocess.run(["node", str(ROOT / "claude/mcp/subagents.mjs")], input="".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests),
                             text=True, capture_output=True, timeout=60, cwd=ROOT, env=env)
        replies = {m["id"]: m for m in map(json.loads, out.stdout.splitlines())}
        body = lambda n: json.dumps(replies[n])
        self.assertIn("NOT AN APPROVAL", body(2), out.stderr)
        self.assertIn("no Coverage section", body(2))
        self.assertNotIn("NOT AN APPROVAL", body(3))
        self.assertNotIn("NOT AN APPROVAL", body(4))

    # the instruction to the reviewer
    def test_tp_review_still_tells_every_reviewer_to_end_with_coverage(self):
        text = (ROOT / "bin/tp-review").read_text()
        self.assertEqual(text.count("section headed Coverage"), 2)  # code review and research-report review


class EveryInputEverySiteTests(unittest.TestCase):
    """The same inputs through each reader of a review: approved with coverage, and every input that must not approve."""
    BAD = {**CASES_INCOMPLETE, **REFUSED}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.tasks = self.state / "tasks"
        self.tasks.mkdir()
        self.agent = agent_module()

    def task(self, ident, reply, review_of=None, meta=None):
        d = self.tasks / ident
        d.mkdir()
        (d / "meta.json").write_text(json.dumps({"id": ident, "worker": "claude", "created": "2026-10-08T10:00:00Z", **(meta or {})}))
        if reply is not None:
            (d / "result.md").write_bytes(reply if isinstance(reply, bytes) else reply.encode())
        (d / "exit").write_text("0")
        if review_of:
            (d / "review-of").write_text(review_of)
        return d

    def inputs(self):
        return {**{k: (v, False) for k, v in self.BAD.items()}, "complete": ("APPROVED\nRESULT_STATUS: complete\n" + COMPLETE, True),
                "missing file": (None, False), "empty file": ("", False),
                "undecodable bytes": (b"APPROVED\nCoverage\nreviewed a.py and checked its callers\xff\n", False)}

    def test_tp_agent_record_and_index(self):
        for label, (reply, complete) in self.inputs().items():
            with self.subTest(label):
                d = self.task("rv", reply, review_of="author")
                rec = self.agent.record(d)
                self.assertEqual(rec["review_complete"], complete)
                if not complete:
                    self.assertEqual(rec["result_status"], "incomplete")
                    self.assertTrue(rec["review_incomplete_reason"])
                    self.assertIn("does not count as an approval", rec["review_notice"])
                row = {r["id"]: r for r in self.agent.write_index(self.state)["recent"]}["rv"]
                self.assertEqual(row["review_complete"], complete)
                self.assertEqual(row["result_status"] == "incomplete", not complete)
                shutil.rmtree(d)

    def test_tp_agent_collect_and_inspect(self):
        env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TMUX": "", "TMUX_PANE": ""}
        for label, (reply, complete) in self.inputs().items():
            d = self.task("rv", reply, review_of="author")
            for command in ("inspect", "collect"):
                with self.subTest(label, command=command):
                    out = subprocess.run([sys.executable, str(ROOT / "bin/tp-agent"), command, "rv"], capture_output=True, text=True, env=env, timeout=60)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    value = json.loads(out.stdout)
                    self.assertEqual("notice" in value, not complete)
                    self.assertEqual(value["review_complete"], complete)
                    if not complete:
                        self.assertIn("review is incomplete", value["notice"])
                        self.assertEqual(value["result_status"], "incomplete")
            shutil.rmtree(d)

    def test_escalation_never_counts_a_bad_review_as_approval_or_crashes(self):
        for label, (reply, _) in self.inputs().items():
            with self.subTest(label):
                review = self.task("rv", reply, review_of="a")
                author = self.task("a", "x\nRESULT_STATUS: complete\n", meta={"review": "rv"})
                self.assertEqual(self.agent.escalation_evidence(self.state, author)[1], 0)
                shutil.rmtree(review); shutil.rmtree(author)

    def test_approval_gate(self):
        for label, (reply, complete) in self.inputs().items():
            if reply is None:
                continue
            with self.subTest(label):
                author = self.task("author", "Work", meta={"review": "rv", "worker": "pi"})
                self.task("rv", reply, review_of="author")
                approval = {"required": True, "status": "approved", "task_id": "author", "round_nonce": "n", "approval": {
                    "round_nonce": "n", "reviewer_id": "rv", "evidence_sha256": hashlib.sha256(reply.strip() if isinstance(reply, bytes) else reply.strip().encode()).hexdigest(),
                    "author_sha256": hashlib.sha256(b"Work").hexdigest()}}
                (author / "review-required.json").write_text(json.dumps(approval))
                meta = json.loads((author / "meta.json").read_text())
                self.assertEqual(task_record.approved_delegated_review(author, meta, approval), complete)
                shutil.rmtree(author); shutil.rmtree(self.tasks / "rv")

    def test_routing_evals(self):
        for label, (reply, complete) in self.inputs().items():
            with self.subTest(label):
                self.task("rv", reply, review_of="auto-1")
                self.task("auto-1", "done", meta={"review": "rv"})
                dirs = {p.name: p for p in self.tasks.iterdir()}
                review = routing_evals.task_summary(dirs["auto-1"], dirs)["review"]
                if complete:
                    self.assertEqual(review["verdict"], "APPROVED")
                else:
                    self.assertNotIn(review["verdict"], ("APPROVED", "APPROVED_WITH_NOTES"))
                    if reply is not None:
                        self.assertTrue(review["incomplete_reason"])
                shutil.rmtree(dirs["rv"]); shutil.rmtree(dirs["auto-1"])

    def test_the_command_used_by_the_dispatcher_and_collect_tools(self):
        for label, (reply, complete) in self.inputs().items():
            with self.subTest(label):
                path = Path(self.tmp.name, "r.md")
                path.unlink(missing_ok=True)
                if reply is not None:
                    path.write_bytes(reply if isinstance(reply, bytes) else reply.encode())
                out = subprocess.run([sys.executable, str(ROOT / "lib/review_verdict.py"), str(path)], capture_output=True, text=True)
                if label in REFUSED or not reply or isinstance(reply, bytes):
                    self.assertEqual((out.returncode, out.stdout), (1, ""))
                    self.assertTrue(out.stderr)
                else:
                    self.assertEqual(json.loads(out.stdout)["complete"], complete)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_mcp_collect_result(self):
        inputs = self.inputs()
        for ident, (reply, _) in zip(range(len(inputs)), inputs.values()):
            self.task(f"rv{ident}", reply, review_of="author")
        requests = [{"id": 1, "method": "initialize", "params": {}}, {"method": "notifications/initialized"}]
        for n in range(len(inputs)):
            requests.append({"id": n + 2, "method": "tools/call", "params": {"name": "collect_result", "arguments": {"id": f"rv{n}"}}})
        fake = Path(self.tmp.name, "fakebin")
        fake.mkdir()
        (fake / "tmux").write_text('#!/bin/sh\ncase "$1" in\n display-message) printf \'%s\\n\' "$TP_STATE_DIR" ;;\n *) exit 0 ;;\nesac\n')
        (fake / "tmux").chmod(0o755)
        env = {"PATH": f"{fake}{os.pathsep}{os.environ['PATH']}", "TMUX": "/nonexistent/tmux-pi-test.sock,1,0", "TP_MAIN": "claude",
               "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture"}
        out = subprocess.run(["node", str(ROOT / "claude/mcp/subagents.mjs")], input="".join(json.dumps({"jsonrpc": "2.0", **r}) + "\n" for r in requests),
                             text=True, capture_output=True, timeout=60, cwd=ROOT, env=env)
        replies = {m["id"]: json.dumps(m) for m in map(json.loads, out.stdout.splitlines())}
        for n, (label, (_, complete)) in enumerate(inputs.items()):
            with self.subTest(label):
                self.assertEqual("NOT AN APPROVAL" in replies[n + 2], not complete, out.stderr)


if __name__ == "__main__":
    unittest.main()
