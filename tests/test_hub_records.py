"""HB4a: lib/hub_records.py (request ids, events, the one tool-name table) and the tp-agent paths that write them:
`tp-agent request` opens a capability request by id, and `tp-agent message` records its delivery id and resolves a request
only with --resolves and a confirmed delivery. No tmux and no child process: tp-agent runs in-process with the pane send faked."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_records  # noqa: E402
from session_env import outside_session  # noqa: E402

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def load_tp_agent():
    loader = importlib.machinery.SourceFileLoader("tp_agent_for_hub_records", str(ROOT / "bin/tp-agent"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


class HubRecordsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / ".tmux-pi"
        self.task = self.state / "tasks" / "w1"
        self.task.mkdir(parents=True)
        (self.task / "meta.json").write_text(json.dumps({"id": "w1", "worker": "claude", "pane": "%9"}))

    def test_one_tool_name_table_keeps_the_raw_name(self):
        expect = {"Bash": ("shell", None), "bash": ("shell", None), "Read": ("read", False), "read": ("read", False),
                  "safe_read": ("read", True), "mcp__tmux-pi-safe__safe_read": ("read", True), "Grep": ("grep", False),
                  "safe_grep": ("grep", True), "mcp__tmux-pi-safe__safe_grep": ("grep", True), "Edit": ("edit", None),
                  "MultiEdit": ("edit", None), "Write": ("write", None), "Glob": ("find", None), "find": ("find", None)}
        for raw, (tool, safe) in expect.items():
            got = hub_records.normalize_tool(raw)
            self.assertEqual((got["tool"], got["raw_tool"], got.get("safe")), (tool, raw, safe), raw)
        self.assertEqual(hub_records.normalize_tool("WebFetch"), {"tool": "WebFetch", "raw_tool": "WebFetch"})  # unknown: as is

    def test_ids_are_ulids_and_increase_within_a_process(self):
        ids = [hub_records.new_id("req") for _ in range(50)]
        self.assertTrue(all(i.startswith("req-") and ULID.match(i[4:]) for i in ids))
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 50)

    def test_the_same_open_request_is_one_id_and_a_new_one_after_resolution(self):
        first, created = hub_records.open_request(self.task, "capability", "browser", "tool: browser", "worker", "file")
        again, created_again = hub_records.open_request(self.task, "capability", "browser", "tool: browser", "worker", "file")
        self.assertEqual((first["id"], created, created_again), (again["id"], True, False))
        other, _ = hub_records.open_request(self.task, "capability", "network", "tool: network", "worker", "file")
        self.assertNotEqual(other["id"], first["id"])  # distinct requests stay distinct (no supersede for capabilities)
        record, changed = hub_records.resolve(self.task, first["id"], "main", "message", "[tp:0123456789/4]", "main", "file")
        self.assertEqual((record["status"], record["resolution"], changed),
                         ("resolved", {"by": "main", "how": "message", "ref": "[tp:0123456789/4]"}, True))
        self.assertFalse(hub_records.resolve(self.task, first["id"], "main", "message", "x", "main", "file")[1])  # once only
        later, created = hub_records.open_request(self.task, "capability", "browser", "tool: browser", "worker", "file")
        self.assertTrue(created)
        self.assertNotEqual(later["id"], first["id"])
        on_disk = json.loads((self.task / "requests" / f"{first['id']}.json").read_text())
        self.assertEqual(set(on_disk) - {"resolved", "resolution"},
                         {"id", "task", "kind", "opened", "text_sha256", "summary", "status"})
        self.assertEqual([(e["kind"], e["ref"]) for e in hub_records.read_events(self.task)],
                         [("request.opened", first["id"]), ("request.opened", other["id"]),
                          ("request.resolved", first["id"]), ("request.opened", later["id"])])
        self.assertEqual([r["status"] for r in hub_records.row_requests(self.task)], ["resolved", "open", "open"])

    def test_events_are_checked_against_the_closed_lists(self):
        line = hub_records.event(self.task, "pause.unverified", "main", "pane", "derived", detail={"tool": "Bash"})
        self.assertEqual({k: line[k] for k in ("v", "task", "actor", "kind", "source", "confidence")},
                         {"v": 1, "task": "w1", "actor": "main", "kind": "pause.unverified", "source": "pane", "confidence": "derived"})
        self.assertTrue(line["id"].startswith("ev-"))
        self.assertEqual((line["detail"]["tool"], line["detail"]["raw_tool"]), ("shell", "Bash"))
        for bad in (("no.such.kind", "main", "pane", "exact"), ("state", "nobody", "pane", "exact"),
                    ("state", "main", "smoke", "exact"), ("state", "main", "pane", "sure")):
            with self.assertRaises(ValueError):
                hub_records.event(self.task, *bad)
        self.assertIsNone(hub_records.row_requests(self.state / "tasks" / "none"))


class TpAgentRequestTests(unittest.TestCase):
    """`tp-agent request` and `tp-agent message --resolves`, in-process (main(argv, env)) with pane_send.send faked."""

    @classmethod
    def setUpClass(cls):
        cls.tp = load_tp_agent()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / ".tmux-pi"
        self.task = self.state / "tasks" / "w1"
        self.task.mkdir(parents=True)
        (self.task / "meta.json").write_text(json.dumps({"id": "w1", "worker": "pi", "pane": "%9", "created": "2026-01-01T00:00:00Z"}))
        (self.task / "state.json").write_text(json.dumps({"state": "waiting", "harness": "pi"}))
        self.env = {**outside_session(), "TP_STATE_DIR": str(self.state)}
        self.env.pop("TMUX", None)

    def run_agent(self, *argv, submitted=True, worker=False):
        sent = []

        def send(pane, text, task_dir=None, token=None):
            sent.append((pane, text, token))
            return {"submitted": submitted, "attempts": 1 if submitted else 3}
        out, err = io.StringIO(), io.StringIO()
        env = {**self.env, **({"TP_TASK_DIR": str(self.task)} if worker else {})}
        with mock.patch.object(self.tp.pane_send, "send", side_effect=send), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                self.tp.main(list(argv), env=env)
                error = None
            except (ValueError, RuntimeError) as caught:
                error = str(caught)
        return (json.loads(out.getvalue()) if out.getvalue().strip() else None), sent, error, err.getvalue()

    def legacy(self):
        return json.loads((self.task / "capability-request.json").read_text())

    def requests(self):
        return {r["id"]: r for r in hub_records.requests(self.task)}

    def test_a_request_gets_an_id_and_two_distinct_requests_are_two_ids(self):
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        first = self.legacy()["id"]
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        self.assertEqual(self.legacy()["id"], first)  # asked again while open: the same request
        self.run_agent("request", "--kind", "context", "--what", "api", "--reason", "fetch", worker=True)
        second = self.legacy()["id"]
        self.assertNotEqual(second, first)
        self.assertEqual({i: r["status"] for i, r in self.requests().items()}, {first: "open", second: "open"})
        self.assertEqual(self.legacy()["status"], "pending")  # older readers still see a pending request

    def test_a_plain_message_resolves_nothing_and_records_its_delivery_id(self):
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        req = self.legacy()["id"]
        value, sent, error, _ = self.run_agent("message", "w1", "--text", "use the renderer")
        self.assertIsNone(error)
        self.assertRegex(value["delivery"], r"^\[tp:[0-9a-f]{10}/\d+\]$")
        self.assertEqual(sent[0][2], value["delivery"])  # the id typed is the id recorded
        line = json.loads((self.task / "messages.jsonl").read_text().splitlines()[-1])
        self.assertEqual((line["delivery"], "resolves" in line), (value["delivery"], False))
        self.assertEqual((self.requests()[req]["status"], self.legacy()["status"]), ("open", "pending"))
        delivered = [e for e in hub_records.read_events(self.task) if e["kind"].startswith("correction.")]
        self.assertEqual([(e["kind"], e["detail"]["delivery"]) for e in delivered], [("correction.delivered", value["delivery"])])

    def test_resolves_closes_the_request_only_when_the_delivery_is_confirmed(self):
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        req = self.legacy()["id"]
        value, _, error, err = self.run_agent("message", "w1", "--resolves", req, "--text", "yes", submitted=False)
        self.assertIn("not confirmed submitted", error)
        self.assertEqual(value["resolves"], {"request": req, "resolved": False, "status": "open",
                                             "why": "the delivery was not confirmed; the request stays open"})
        self.assertIn(f"request {req} stays open", err)
        self.assertEqual((self.requests()[req]["status"], self.legacy()["status"]), ("open", "pending"))
        self.assertEqual(hub_records.read_events(self.task)[-1]["kind"], "correction.unverified")
        value, _, error, _ = self.run_agent("message", "w1", "--resolves", req, "--text", "yes")
        self.assertIsNone(error)
        self.assertEqual(self.requests()[req]["resolution"], {"by": "main", "how": "message", "ref": value["delivery"]})
        self.assertEqual(self.legacy()["status"], "answered")
        value, _, _, _ = self.run_agent("message", "w1", "--resolves", req, "--text", "again")  # already resolved: unchanged
        self.assertEqual(value["resolves"]["resolved"], False)

    def test_an_unknown_request_id_is_refused_before_anything_is_typed(self):
        _, sent, error, _ = self.run_agent("message", "w1", "--resolves", "req-01JABCDEFGHJKMNPQRSTVWXYZ0", "--text", "yes")
        self.assertIn("has no request", error)
        self.assertEqual(sent, [])
        self.assertFalse((self.task / "messages.jsonl").exists())

    def test_the_capability_file_stays_pending_while_another_capability_request_is_open(self):
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        first = self.legacy()["id"]
        self.run_agent("request", "--kind", "context", "--what", "api", "--reason", "fetch", worker=True)
        self.run_agent("message", "w1", "--resolves", first, "--text", "browser: yes")
        self.assertEqual(self.legacy()["status"], "pending")  # the newer one is still open
        row = self.tp.record(self.task)
        self.assertEqual([r["status"] for r in row["requests"]], ["resolved", "open"])


if __name__ == "__main__":
    unittest.main()
