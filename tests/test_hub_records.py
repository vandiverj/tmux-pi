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


class AgentCase(unittest.TestCase):
    """tp-agent in-process (main(argv, env)) with pane_send.send faked, on one task w1 in a temporary state directory."""

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


class TpAgentRequestTests(AgentCase):
    """`tp-agent request` and `tp-agent message --resolves`."""

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

    def test_the_capability_file_mirrors_the_newest_request_and_an_older_open_one_still_blocks(self):
        # Rule 5 (F6): capability-request.json is the newest request's own id, what and status; aggregate readers use requests/.
        (self.task / "state.json").write_text(json.dumps({"state": "working", "harness": "pi"}))  # at work, so asking shows
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        first = self.legacy()["id"]
        self.run_agent("request", "--kind", "context", "--what", "api", "--reason", "fetch", worker=True)
        second = self.legacy()["id"]
        self.run_agent("message", "w1", "--resolves", second, "--text", "api: yes")
        self.assertEqual({k: self.legacy()[k] for k in ("id", "what", "status")}, {"id": second, "what": "api", "status": "answered"})
        self.assertEqual(self.requests()[first]["status"], "open")
        import contract
        self.assertEqual(contract.task_lifecycle(self.task)[0], "needs_input")  # the older request still asks
        self.assertEqual(self.review_gate(), "1")  # tp-review's own gate text: one capability request open
        self.run_agent("message", "w1", "--resolves", first, "--text", "browser: yes")
        self.assertEqual(self.legacy()["status"], "answered")  # it names the second request, which stays answered
        self.assertNotEqual(contract.task_lifecycle(self.task)[0], "needs_input")
        self.assertEqual(self.review_gate(), "0")

    def review_gate(self):
        """Runs the exact `python3 -c` text bin/tp-review gates a review on, in-process, for this task."""
        text = (ROOT / "bin/tp-review").read_text()
        code = re.search(r"open_caps=\"\$\(python3 -c '([^']*)'", text).group(1)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["-c", str(ROOT / "lib"), str(self.task)]), contextlib.redirect_stdout(out):
            exec(compile(code, "tp-review-gate", "exec"), {})
        return out.getvalue().strip()

    def test_more_than_twenty_resolved_never_hide_an_open_request(self):
        # Rule 2 (F4): only resolved history is bounded.
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        oldest = self.legacy()["id"]
        for n in range(21):
            req, _ = hub_records.open_request(self.task, "capability", "tool: t%d" % n, "t%d" % n, "worker", "file")
            hub_records.resolve(self.task, req["id"], "main", "message", None, "main", "file")
        shown = self.tp.record(self.task)["requests"]
        self.assertEqual(len(shown), 21)
        self.assertEqual([r["id"] for r in shown if r["status"] == "open"], [oldest])
        self.assertEqual(shown[0]["id"], oldest)


class NoticeRulesTests(AgentCase):
    """The real request writers (tp-agent request/message, hub_records) feed the plugin notifier's own TypeScript rules,
    run under node from claude/plugins/tmux-pi/hooks/register.tsx (tests/notice_rules.mjs): one node process per test."""

    def notifier(self, scenarios):
        import subprocess
        payload = json.dumps({"source": str(ROOT / "claude/plugins/tmux-pi/hooks/register.tsx"), "scenarios": scenarios})
        ran = subprocess.run(["node", str(ROOT / "tests/notice_rules.mjs")], input=payload, capture_output=True, text=True,
                             timeout=20, check=False)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        return json.loads(ran.stdout)

    def make_task(self, name):
        task = self.state / "tasks" / name
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": name, "worker": "pi", "pane": "%9", "created": "2026-01-01T00:00:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": "working", "harness": "pi"}))
        return task

    def row(self, task, status=None):
        row = json.loads(json.dumps(self.tp.record(task), default=str))
        return dict(row, status=status) if status else row

    def ask(self, task, what):
        env = self.env
        self.env = {**env, "TP_TASK_DIR": str(task)}
        try:
            self.run_agent("request", "--kind", "tool", "--what", what, "--reason", "need it", worker=False)
        finally:
            self.env = env
        return json.loads((task / "capability-request.json").read_text())["id"]

    def answer(self, task, req):
        _, _, error, _ = self.run_agent("message", task.name, "--resolves", req, "--text", "yes")
        self.assertIsNone(error)

    def test_every_transition_is_heard_by_its_id_and_every_suggested_command_parses(self):
        sc, ids = {}, {}
        # Two distinct requests between polls (F2): two openings, each by its id.
        t = self.make_task("two")
        sc["two"] = [{"rows": [self.row(t)]}]
        ids["two"] = [self.ask(t, "browser"), self.ask(t, "network")]
        sc["two"].append({"rows": [self.row(t)]})
        # Opened, then superseded by another, between polls (F2): A opened, B open, A closed (superseded), in order.
        t = self.make_task("sup")
        sc["sup"] = [{"rows": [self.row(t)]}]
        a, _ = hub_records.open_request(t, "permission", "allow Bash 1", "Bash 1", "hook", "hook", supersede=True)
        b, _ = hub_records.open_request(t, "permission", "allow Bash 2", "Bash 2", "hook", "hook", supersede=True)
        ids["sup"] = [a["id"], b["id"]]
        sc["sup"].append({"rows": [self.row(t, "needs_input")]})
        # Opened and resolved between polls (F2): the resolution, with its id.
        t = self.make_task("quick")
        sc["quick"] = [{"rows": [self.row(t)]}]
        ids["quick"] = [self.ask(t, "browser")]
        self.answer(t, ids["quick"][0])
        sc["quick"].append({"rows": [self.row(t)]})
        # Startup with N open, the index listing them (F3): each id rendered by the first poll.
        t = self.make_task("start")
        ids["start"] = [self.ask(t, w) for w in ("browser", "network", "gpu")]
        row = self.row(t, "needs_input")
        sc["start"] = [{"rows": [row], "index": [{"id": "start", "status": "needs_input", "open_requests": ids["start"]}]}]
        # Settled and completed rows with an open request (F5): announced.
        t, u = self.make_task("done1"), self.make_task("done2")
        sc["done"] = [{"rows": [self.row(t), self.row(u)]}]
        ids["done"] = [self.ask(t, "browser"), self.ask(u, "network")]
        sc["done"].append({"rows": [self.row(t, "settled"), self.row(u, "completed")]})
        # A submit that failed rendered nothing (F3): the opening is told again, now with its closing.
        t = self.make_task("lost")
        sc["lost"] = [{"rows": [self.row(t)]}]
        ids["lost"] = [self.ask(t, "browser")]
        sc["lost"].append({"rows": [self.row(t)], "submit": False})
        self.answer(t, ids["lost"][0])
        sc["lost"].append({"rows": [self.row(t)]})
        # History: resolved before the notifier started, never replayed.
        t = self.make_task("old")
        ids["old"] = [self.ask(t, "browser")]
        self.answer(t, ids["old"][0])
        sc["old"] = [{"rows": [self.row(t)]}]
        # More than 20 resolved and one open (F4): the open one is announced.
        t = self.make_task("many")
        ids["many"] = [self.ask(t, "browser")]
        for n in range(21):
            req, _ = hub_records.open_request(t, "capability", "tool: t%d" % n, "t%d" % n, "worker", "file")
            hub_records.resolve(t, req["id"], "main", "message", None, "main", "file")
        sc["many"] = [{"rows": [self.row(t)]}]

        got = self.notifier(sc)
        said = {name: [s["submitted"] or "" for s in steps] for name, steps in got.items()}

        a, b = ids["two"]
        self.assertEqual(said["two"][0], "")
        self.assertIn(f"has an open capability request {a}", said["two"][1])
        self.assertIn(f"has an open capability request {b}", said["two"][1])
        a, b = ids["sup"]
        self.assertEqual(said["sup"][1].split("\n")[1:4], [
            f"sup: permission request {a} (Bash 1) was opened",
            f"sup has an open permission request {b} (Bash 2): answer it in its pane, or tp-agent inspect sup",
            f"sup: permission request {a} (Bash 1) is closed (superseded)"])
        (a,) = ids["quick"]
        self.assertIn(f"capability request {a} (tool: browser) was opened", said["quick"][1])
        self.assertIn(f"capability request {a} (tool: browser) is closed (message)", said["quick"][1])
        for each in ids["start"]:
            self.assertIn(f"has an open capability request {each}", said["start"][0])
        self.assertIn(f"done1 has an open capability request {ids['done'][0]}", said["done"][1])
        self.assertIn(f"done2 has an open capability request {ids['done'][1]}", said["done"][1])
        (a,) = ids["lost"]
        self.assertIn(f"--resolves {a} --text", said["lost"][1])  # tried; the submit failed
        self.assertIn(f"{a} (tool: browser) was opened", said["lost"][2])
        self.assertIn(f"{a} (tool: browser) is closed (message)", said["lost"][2])
        self.assertNotIn(ids["old"][0], said["old"][0])
        self.assertIn(f"has an open capability request {ids['many'][0]}", said["many"][0])
        self.assertEqual(said["many"][0].count("is closed"), 0)

        # Rule 6 (F7): every command a notice suggests is accepted by tp-agent's own argparse.
        commands = set()
        for text in "\n".join(t for texts in said.values() for t in texts).split("\n"):
            commands.update(re.findall(r"(?:TP_STATE_DIR='[^']*' )?tp-agent [a-z-]+ [A-Za-z0-9._-]+(?: --resolves \S+ --text \"[^\"]*\")?", text))
        self.assertTrue(any("--resolves" in c for c in commands))
        self.assertTrue(any(" inspect " in c for c in commands))
        for command in sorted(commands):
            self.assertEqual(self.parses(command), command.split("tp-agent ")[1].split()[0], command)

    def parses(self, command):
        """The verb tp-agent's real parser makes of `command` (an env assignment in front is the shell's, not argv)."""
        import argparse
        import shlex
        argv = [w for w in shlex.split(command) if "=" not in w.split(" ")[0] or w.startswith("-")][1:]

        class Parsed(Exception):
            pass
        real = argparse.ArgumentParser.parse_args

        def capture(parser, args=None, namespace=None):
            raise Parsed(real(parser, args, namespace))
        with mock.patch.object(argparse.ArgumentParser, "parse_args", capture), contextlib.redirect_stderr(io.StringIO()):
            try:
                self.tp.main(argv, env=self.env)
            except Parsed as parsed:
                return parsed.args[0].command
            except SystemExit:
                return None
        return None


if __name__ == "__main__":
    unittest.main()
