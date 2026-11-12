"""HB4a: lib/hub_records.py (request ids, events, the one tool-name table) and the tp-agent paths that write them:
`tp-agent request` opens a capability request by id, and `tp-agent message` records its delivery id and resolves a request
only with --resolves and a confirmed delivery. No tmux and no child process: tp-agent runs in-process with the pane send faked."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
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

    def run_agent(self, *argv, submitted=True, worker=False, during=None):
        """during: run inside the faked send, as another writer acting while the text is typed (the worker's own state
        change closing a request the message answers)."""
        sent = []

        def send(pane, text, task_dir=None, token=None):
            sent.append((pane, text, token))
            if during:
                during()
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
        # HB6b1 rule 2: the owner decided, Main relays: by owner, with the owner's words on the resolved event
        self.run_agent("request", "--kind", "context", "--what", "api", "--reason", "fetch", worker=True)
        second = self.legacy()["id"]
        value, _, error, _ = self.run_agent("message", "w1", "--resolves", second, "--by", "owner", "--quote", " yes, use it ",
                                            "--text", "the owner says yes")
        self.assertIsNone(error)
        self.assertEqual(self.requests()[second]["resolution"], {"by": "owner", "how": "message", "ref": value["delivery"]})
        resolved = [e for e in hub_records.read_events(self.task) if e["kind"] == "request.resolved" and e["ref"] == second]
        self.assertEqual([(e["detail"]["by"], e["detail"]["quote"], e["actor"]) for e in resolved], [("owner", "yes, use it", "main")])
        # HB6b1 round 2 (F1): who answered survives another writer closing the request first. Each row: how the request
        # opens, the writer acting while the message is typed, confirmed?, then the record and request_log it leaves.
        hook = lambda **f: hub_records.claude_hook(self.task, f, f.pop("prev"), f.pop("new"), calls=f.pop("calls", None))  # noqa: E731
        opens = {"input": lambda: hub_records.pi_state(self.task, "waiting", "which file?")["opened"]["id"],
                 "permission": lambda: hook(hook_event_name="Notification", message="Claude needs your permission to use Bash",
                                            prev="working", new="permission", calls=[{"id": "tu1", "name": "Bash"}])["opened"]["id"],
                 "capability": lambda: (self.run_agent("request", "--kind", "tool", "--what", "x", "--reason", "y", worker=True),
                                        self.legacy()["id"])[1]}
        pi_working = lambda: hub_records.pi_state(self.task, "working")  # noqa: E731  reporter.ts:217-227 agent_start
        ran = lambda: hook(hook_event_name="PostToolUse", tool_use_id="tu1", prev="permission", new="working")  # noqa: E731
        races = [
            # (name, kind, during, confirmed, resolution without answer, answer by or None, resolved?, closes in request_log)
            ("pi input closed by agent_start while typed", "input", pi_working, True, ("worker", "left-state"), "owner", True, 1),
            ("claude permission closed by PostToolUse while typed", "permission", ran, True, ("hook", "approved"), "owner", True, 1),
            ("unconfirmed delivery invents no answer", "input", pi_working, False, ("worker", "left-state"), None, False, 1),
            ("capability answered by Main meanwhile: an answer never replaces an answer", "capability",
             lambda: hub_records.resolve(self.task, current[0], "main", "message", "[tp:other]", "main", "file"), True,
             ("main", "message"), None, False, 1),
            ("no race: the owner's answer closes it; a state change after never overwrites it", "input", None, True,
             ("owner", "message"), None, True, 1),
        ]
        for name, kind, during, confirmed, closed, answered, resolved, closes in races:
            with self.subTest(name):
                for r in hub_records.open_requests(self.task):
                    hub_records.resolve(self.task, r["id"], "main", "withdrawn", None, "main", "file", status="withdrawn")
                current = [opens[kind]()]
                value, _, error, _ = self.run_agent("message", "w1", "--resolves", current[0], "--by", "owner", "--quote", "go",
                                                    "--text", "the owner says go", submitted=confirmed, during=during)
                if during is None:
                    pi_working()
                record = self.requests()[current[0]]
                resolution = record["resolution"]
                self.assertEqual((record["status"], resolution["by"], resolution["how"]), ("resolved",) + closed)
                given = resolution.get("answer")
                self.assertEqual(given and (given["by"], given["how"], given["ref"], given["quote"]),
                                 answered and (answered, "message", value["delivery"], "go"))
                self.assertEqual(value["resolves"]["resolved"], resolved)
                log = [e for e in hub_records.request_log(self.task) if e["id"] == current[0] and e["change"] == "closed"]
                self.assertEqual(len(log), closes)
                if answered:
                    self.assertIn("the answer is recorded on it", value["resolves"]["why"])
                if kind == "capability":
                    self.assertEqual(self.legacy()["status"], "answered")

    def test_an_unknown_request_id_is_refused_before_anything_is_typed(self):
        _, sent, error, _ = self.run_agent("message", "w1", "--resolves", "req-01JABCDEFGHJKMNPQRSTVWXYZ0", "--text", "yes")
        self.assertIn("has no request", error)
        self.assertEqual(sent, [])
        self.assertFalse((self.task / "messages.jsonl").exists())
        # HB6b1 rule 2: who answered is never inferred; an owner answer without the owner's words is refused, untyped
        self.run_agent("request", "--kind", "tool", "--what", "browser", "--reason", "render", worker=True)
        req = self.legacy()["id"]
        for argv, refusal in ((("--resolves", req, "--by", "owner"), "needs --quote"),
                              (("--resolves", req, "--by", "owner", "--quote", "  "), "needs --quote"),
                              (("--by", "owner", "--quote", "yes"), "needs --resolves"),
                              (("--resolves", req, "--quote", "yes"), "needs --by owner")):
            _, sent, error, _ = self.run_agent("message", "w1", *argv, "--text", "yes")
            self.assertIn(refusal, error or "", argv)
            self.assertEqual(sent, [], argv)
        self.assertFalse((self.task / "messages.jsonl").exists())
        self.assertEqual(self.requests()[req]["status"], "open")

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
        # Round 3 rule 1 (F3): the old request closes after 21 newer closed ones. The display window keeps the most recently
        # closed (by close time), so it shows; and the transition log, which notices come from, has every transition in order.
        hub_records.resolve(self.task, oldest, "main", "message", None, "main", "file")
        row = self.tp.record(self.task)
        self.assertIn(oldest, [r["id"] for r in row["requests"]])
        self.assertEqual(len(row["requests"]), 20)
        log = row["request_log"]
        self.assertEqual([e["seq"] for e in log], list(range(1, 45)))
        self.assertEqual((log[0]["id"], log[0]["change"], log[-1]["id"], log[-1]["change"], log[-1]["how"]),
                         (oldest, "opened", oldest, "closed", "message"))
        self.assertEqual(log[-1]["summary"], log[0]["summary"])


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

    def make_task(self, name, tree=None, pane="%9"):
        """A working pi task with its own working tree (`tree` shares another task's), as tp-spawn records one."""
        task = self.state / "tasks" / name
        task.mkdir(parents=True)
        workdir = self.state.parent / "trees" / (tree or name)
        workdir.mkdir(parents=True, exist_ok=True)
        (task / "meta.json").write_text(json.dumps({"id": name, "worker": "pi", "pane": pane, "workdir": str(workdir),
                                                    "created": "2026-01-01T00:00:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": "working", "harness": "pi"}))
        (task / "request.md").write_text("Fix the thing.\n")
        (task / "dispatch.json").write_text(json.dumps({"context": [], "notes": [], "tests": "affected"}))
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
        # More than 20 resolved and one open (F4): the open one is announced.
        t = self.make_task("many")
        ids["many"] = [self.ask(t, "browser")]
        for n in range(21):
            req, _ = hub_records.open_request(t, "capability", "tool: t%d" % n, "t%d" % n, "worker", "file")
            hub_records.resolve(t, req["id"], "main", "message", None, "main", "file")
        sc["many"] = [{"rows": [self.row(t)]}]
        for steps in sc.values():  # each session starts as session.start does: its startup list read is the first rows
            steps[0].setdefault("start", steps[0]["rows"])

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
        for each in ids["start"]:
            self.assertIn(f"has an open capability request {each}", said["start"][0])
        self.assertIn(f"done1 has an open capability request {ids['done'][0]}", said["done"][1])
        self.assertIn(f"done2 has an open capability request {ids['done'][1]}", said["done"][1])
        (a,) = ids["lost"]
        self.assertIn(f"--resolves {a} --text", said["lost"][1])  # tried; the submit failed
        self.assertIn(f"{a} (tool: browser) was opened", said["lost"][2])
        self.assertIn(f"{a} (tool: browser) is closed (message)", said["lost"][2])
        self.assertIn(f"has an open capability request {ids['many'][0]}", said["many"][0])
        self.assertEqual(said["many"][0].count("is closed"), 0)

        # Rule 6 (F7): every command a notice suggests is accepted by tp-agent's own argparse.
        commands = set()
        for text in "\n".join(t for texts in said.values() for t in texts).split("\n"):
            commands.update(re.findall(r"(?:env (?:-u \S+ )+TP_STATE_DIR=(?:'[^']*'|\S+) )?tp-agent [a-z-]+ [A-Za-z0-9._-]+(?: --resolves \S+ --text \"[^\"]*\")?", text))
        self.assertTrue(any("--resolves" in c for c in commands))
        self.assertTrue(any(" inspect " in c for c in commands))
        for command in sorted(commands):
            self.assertEqual(self.parses(command), command.split("tp-agent ")[1].split()[0], command)

    def test_each_transition_is_delivered_once_from_the_log_past_the_startup_cursor(self):
        # Round 3 rule 1: notices come from the transition log (request_log), with a cursor per task taken at startup. Round 4
        # invariant 2: the cutoff is the log position at the main's view on every start path (a successful startup read, a
        # failed one, a reload of main's shipped value); with no read at that view every task starts at 0.
        sc, ids = {}, {}
        # Opened and resolved between two polls (F2, folded in from the table above): both transitions, by id.
        t = self.make_task("quick")
        sc["quick"] = [{"start": [self.row(t)], "rows": [self.row(t)]}]
        ids["quick"] = [self.ask(t, "browser")]
        self.answer(t, ids["quick"][0])
        sc["quick"].append({"rows": [self.row(t)]})
        # Round 4: the startup list read failed, the index was injected with A open, A resolves before the first poll. At
        # ec566e4 the first poll took the snapshot then, with A already closed: neither its opening nor its closing was told.
        t = self.make_task("failstart")
        ids["failstart"] = [self.ask(t, "browser")]
        index = [{"id": "failstart", "status": "needs_input", "open_requests": ids["failstart"]}]
        self.answer(t, ids["failstart"][0])
        sc["failstart"] = [{"start": "failed", "index": index, "rows": [self.row(t)]}, {"rows": [self.row(t)]}]
        # Round 4: a reload takes up main's shipped value (epoch, isIndexed, `delivered` of id:status keys, no cursors). A still
        # open: its opening is told once (main's notifier never rendered a request id). A closed meanwhile: its closing is told.
        shipped = {"epoch": 3, "isIndexed": True, "delivered": ["mainopen:needs_input", "mainclosed:needs_input"], "pending": []}
        t = self.make_task("mainopen")
        ids["mainopen"] = [self.ask(t, "browser")]
        sc["mainopen"] = [{"stored": shipped, "start": [self.row(t)], "rows": [self.row(t, "needs_input")]},
                          {"rows": [self.row(t, "needs_input")]}]
        t = self.make_task("mainclosed")
        ids["mainclosed"] = [self.ask(t, "browser")]
        self.answer(t, ids["mainclosed"][0])
        sc["mainclosed"] = [{"stored": dict(shipped), "start": [self.row(t)], "rows": [self.row(t)]}, {"rows": [self.row(t)]}]
        # F2 (the review's probe): open at startup, resolved before the first poll. At cb5353a: no notice at all.
        t = self.make_task("race")
        ids["race"] = [self.ask(t, "browser")]
        start = self.row(t)
        self.answer(t, ids["race"][0])
        sc["race"] = [{"start": [start], "rows": [self.row(t)]}, {"rows": [self.row(t)]}]
        # Opened and resolved after the startup snapshot, before the first poll: both transitions, once.
        t = self.make_task("between")
        start = self.row(t)
        ids["between"] = [self.ask(t, "browser")]
        self.answer(t, ids["between"][0])
        sc["between"] = [{"start": [start], "rows": [self.row(t)]}, {"rows": [self.row(t)]}]
        # Closed before the snapshot: history, never replayed, even when it is the only thing on the task.
        t = self.make_task("before")
        ids["before"] = [self.ask(t, "browser")]
        self.answer(t, ids["before"][0])
        sc["before"] = [{"start": [self.row(t)], "rows": [self.row(t)]}]
        # F3 (the review's probe): an old open request closes after 21 newer closed records. At cb5353a its closing is lost.
        t = self.make_task("window")
        ids["window"] = [self.ask(t, "browser")]
        sc["window"] = [{"start": [self.row(t)], "rows": [self.row(t)]}]
        for n in range(21):
            req, _ = hub_records.open_request(t, "capability", "tool: t%d" % n, "t%d" % n, "worker", "file")
            hub_records.resolve(t, req["id"], "main", "message", None, "main", "file")
            ids["window"].append(req["id"])
        self.answer(t, ids["window"][0])
        sc["window"] += [{"rows": [self.row(t)]}, {"rows": [self.row(t)]}]
        # The display window is not read: a row whose `requests` is emptied still delivers from its log, and a row whose
        # `requests` lists an open request its log does not have announces nothing.
        t = self.make_task("display")
        ids["display"] = [self.ask(t, "browser")]
        fake = dict(self.row(t), requests=[{"id": "req-01JABCDEFGHJKMNPQRSTVWXYZ0", "kind": "capability", "status": "open",
                                            "summary": "fake", "opened": "2026-01-01T00:00:00Z"}])
        sc["display"] = [{"start": [dict(self.row(t), request_log=[], requests=[])], "rows": [dict(fake, requests=[])]},
                         {"rows": [fake]}]

        got = self.notifier(sc)
        said = {name: [s["submitted"] or "" for s in steps] for name, steps in got.items()}

        def told(text):  # the request each line of a submitted text is about, in order
            return [found[0] for found in (re.findall(r"req-[0-9A-Z]{26}", line) for line in text.split("\n")) if found]
        (a,) = ids["quick"]
        self.assertEqual(said["quick"][0], "")
        self.assertEqual(said["quick"][1].split("\n")[1:], [f"quick: capability request {a} (tool: browser) was opened",
                                                            f"quick: capability request {a} (tool: browser) is closed (message)"])
        (a,) = ids["failstart"]
        self.assertEqual(said["failstart"][0].split("\n")[1:], [
            f"failstart: capability request {a} (tool: browser) was opened",
            f"failstart: capability request {a} (tool: browser) is closed (message)"])
        self.assertEqual(said["failstart"][1], "")
        (a,) = ids["mainopen"]
        self.assertEqual(told(said["mainopen"][0]), [a])
        self.assertIn(f"mainopen has an open capability request {a}", said["mainopen"][0])
        self.assertEqual(said["mainopen"][1], "")  # told once
        (a,) = ids["mainclosed"]
        self.assertEqual(told(said["mainclosed"][0]), [a, a])
        self.assertIn(f"mainclosed: capability request {a} (tool: browser) is closed (message)", said["mainclosed"][0])
        self.assertEqual(said["mainclosed"][1], "")

        (a,) = ids["race"]
        self.assertEqual(said["race"][0].split("\n")[1:], [f"race: capability request {a} (tool: browser) was opened",
                                                          f"race: capability request {a} (tool: browser) is closed (message)"])
        self.assertEqual(got["race"][0]["toasts"][0], f"race has an open capability request {a} (tool: browser): answer with "
                         f"tp-agent message race --resolves {a} --text \"...\", or tp-agent inspect race")
        self.assertEqual(said["race"][1], "")  # delivered once
        self.assertEqual({k.split("#")[1]: v for k, v in got["race"][1]["cursors"].items()}, {"race": 2})
        (a,) = ids["between"]
        self.assertEqual(said["between"][0].split("\n")[1:], [f"between: capability request {a} (tool: browser) was opened",
                                                             f"between: capability request {a} (tool: browser) is closed (message)"])
        self.assertEqual(said["between"][1], "")
        self.assertEqual(said["before"], [""])
        oldest, newer = ids["window"][0], ids["window"][1:]
        self.assertIn(f"has an open capability request {oldest}", said["window"][0])
        lines = said["window"][1].split("\n")[1:]
        self.assertEqual(len(lines), 43)  # 21 openings, 21 closings, and the old request's closing: each once
        self.assertEqual(lines[-1], f"window: capability request {oldest} (tool: browser) is closed (message)")
        self.assertEqual([sum(req in line for line in lines) for req in newer], [2] * 21)
        self.assertEqual(said["window"][2], "")
        (a,) = ids["display"]
        self.assertIn(f"has an open capability request {a}", said["display"][0])
        self.assertNotIn("req-01JABCDEFGHJKMNPQRSTVWXYZ0", said["display"][0] + said["display"][1])
        self.assertEqual(said["display"][1], "")

    # The task states a request can be left open in, and the lifecycle each reads as. Each is made from a working task with
    # one open capability request.
    STATES = {
        "waiting": ({"state.json": {"state": "waiting", "harness": "pi"}}, "message"),
        "working": ({}, "message"),
        "settled": ({"state.json": {"state": "waiting", "harness": "pi"}, "result.md": "Done.\nRESULT_STATUS: complete\n"}, "message"),
        "collected": ({"state.json": {"state": "waiting", "harness": "pi"}, "result.md": "Done.\nRESULT_STATUS: complete\n",
                       "collected.json": {}, "reaped.json": {}}, "retry"),
        "reaped": ({"state.json": {"state": "waiting", "harness": "pi"}, "result.md": "Done.\nRESULT_STATUS: complete\n",
                    "reaped.json": {}}, "retry"),
        "reaped-working": ({"reaped.json": {}}, "none"),
        "failed": ({"state.json": {"state": "error", "harness": "pi"}}, "retry"),
        "stopped": ({"stopped.json": {}}, "retry"),
        "nopane": ({"meta.json": {"id": "nopane", "worker": "pi", "created": "2026-01-01T00:00:00Z"}}, "none"),
        "nopane-done": ({"meta.json": {"id": "nopane-done", "worker": "pi", "created": "2026-01-01T00:00:00Z"},
                         "state.json": {"state": "waiting", "harness": "pi"}, "result.md": "Done.\nRESULT_STATUS: complete\n"},
                        "retry"),
        # Round 4 invariant 3 (the review's probes): retry's own admission refuses, so the notice names what it needs first.
        # A failed pi worker whose process is still alive in its pane (%41): guard_retry finds it on its own tree.
        "failed-alive": ({"meta.json": {"id": "failed-alive", "worker": "pi", "pane": "%41", "created": "2026-01-01T00:00:00Z"},
                          "state.json": {"state": "error", "harness": "pi"}}, "none"),
        # A collected author with a live peer (task "peer", pane %42) on its working tree.
        "collected-peer": ({"state.json": {"state": "waiting", "harness": "pi"}, "result.md": "Done.\nRESULT_STATUS: complete\n",
                            "collected.json": {}, "reaped.json": {}}, "none"),
    }
    # The pane table (tmux list-panes) as the guards read it: only the two live panes above. Everything else is real.
    PANES = {"%41": (True, 4241), "%42": (True, 4242)}

    def test_a_notice_suggests_only_a_command_that_works_on_the_task_in_its_state(self):
        # Round 3 rule 3 (F4): `tp-agent message --resolves` only while the worker can take a message (tp-agent's own check);
        # a finished task's request is left open, with a command that works there (retry), or none. Each suggested command is
        # then run against the task in that state. The review's probe is "collected": at cb5353a the notice suggested the
        # message, which the command refused (reaped.json).
        panes = mock.patch.object(sys.modules["worker_process"], "pane_table", return_value=dict(self.PANES))
        panes.start()
        self.addCleanup(panes.stop)
        peer = self.make_task("peer", tree="collected-peer", pane="%42")  # a writer alive on collected-peer's tree
        (peer / "meta.json").write_text(json.dumps(dict(json.loads((peer / "meta.json").read_text()), role="coder")))
        tasks, reqs = {}, {}
        for name, (files, _) in self.STATES.items():
            t = self.make_task(name)
            reqs[name] = self.ask(t, "browser")
            for file, body in files.items():
                if file == "meta.json":  # a recorded working tree, as every spawned task has
                    body = dict(body, workdir=json.loads((t / "meta.json").read_text())["workdir"])
                (t / file).write_text(body if isinstance(body, str) else json.dumps(body))
            tasks[name] = t
        got = self.notifier({name: [{"start": [self.row(t)], "rows": [self.row(t)]}] for name, t in tasks.items()})
        for name, t in tasks.items():
            route = self.STATES[name][1]
            with self.subTest(state=name, life=self.tp.lifecycle(t)):
                seen = (self.row(t).get("answer_route") or {}).get("by")
                text = got[name][0]["submitted"]
                self.assertIn(reqs[name], text)
                commands = re.findall(r"tp-agent (?:message|retry|inspect) [A-Za-z0-9._-]+(?: --resolves \S+)?(?: --(?:text|reason) \"[^\"]*\")?", text)
                # First: every command the notice suggests works on the task as it is (the review's probe, at cb5353a: the
                # collected task was told `tp-agent message`, which refused).
                for command in commands:
                    self.assertIsNone(self.runs(t, command.replace('"..."', '"answer"')), command)
                verbs = sorted({c.split()[1] for c in commands})
                self.assertEqual(verbs, {"message": ["inspect", "message"], "retry": ["inspect", "retry"], "none": ["inspect"]}[route], text)
                self.assertEqual(seen, route)
                if route == "retry":
                    self.assertIn("left open by a finished task", text)
                if route == "none":
                    self.assertIn("no command can answer it now", text)
                    # The prerequisite the notice names is the retry command's own refusal, word for word (the real guard).
                    # needs is data (HB4b rule 3): its text is the command's refusal, and the notice prints that text.
                    from worker_process import needs_text
                    needs = needs_text(self.row(t)["answer_route"]["needs"])
                    refused = self.runs(t, f'tp-agent retry {name} --reason "answer"')
                    self.assertEqual(needs, refused)
                    self.assertIn(f"First: {needs}", text)
                if name == "failed-alive":
                    self.assertIn("a process is alive on the same working tree: failed-alive", text)
                if name == "collected-peer":
                    self.assertIn("a process is alive on the same working tree: peer (writer", text)
                if route == "message":
                    self.assertEqual(hub_records.requests(t)[0]["status"], "resolved")  # the suggested message answered it

        # Round 5 rule 3 (F3), the table's external-session row: "failed-alive" again (retry refused, so `needs` names a
        # command), dispatched by main "sess-1" into another session of the workspace ("remote one": a path shlex must quote),
        # with a same-named task in main's own session ("local"). Its row comes from main's own list (main_rows: state_dir).
        # Every command the notice suggests, the notifier's (agent()) and those in `needs` (tp-agent's own refusal, printed
        # unedited), carries the task's session in the form tp-agent's refusals print (lib/lane_lease.py in_session); run as
        # typed in main's pane (TMUX, TMUX_PANE, TP_SESSION and main's TP_STATE_DIR set), tp-agent takes it and it acts on
        # that task, never the local one. At b317e77 `needs` said a bare `tp-agent stop failed-alive`: main's own task.
        # HB4b rule 3 (K3), the hard-path row: a session path with two spaces and a quote ("remote  it's"). `needs` is data
        # (prose and tp-agent argument lists); the notice quotes each command once with the row's session, so shlex.split of
        # every suggested command gives back that exact path. At main the notice collapsed the two spaces: another directory.
        import lane_lease
        from worker_process import needs_text
        sessions = self.state.parent / "workspace" / ".tmux-pi" / "sessions"
        local = sessions / "local"
        for name in ("remote one", "remote  it's"):
            remote = sessions / name
            made = {}
            for session, pane, state in ((remote, "%41", "error"), (local, "%43", "working")):
                t = session / "tasks" / "failed-alive"
                made[session] = t
                if t.exists():  # main's own same-named task, made by the first row
                    continue
                t.mkdir(parents=True)
                tree = sessions.parent.parent / "trees" / session.name
                tree.mkdir(parents=True)
                (t / "meta.json").write_text(json.dumps({"id": "failed-alive", "worker": "pi", "pane": pane, "workdir": str(tree),
                                                         "created": "2026-01-01T00:00:00Z"}))
                (t / "state.json").write_text(json.dumps({"state": state, "harness": "pi"}))
                (t / "request.md").write_text("Fix the thing.\n")
                (t / "dispatch.json").write_text(json.dumps({"context": [], "notes": [], "tests": "affected", "main": "sess-1"}))
            env = self.env
            self.env = {**env, "TP_STATE_DIR": str(remote)}
            try:
                req = self.ask(made[remote], "browser")
            finally:
                self.env = env
            rows, _ = self.tp.main_rows(local, "sess-1")
            (row,) = [json.loads(json.dumps(r, default=str)) for r in rows if r.get("state_dir") == str(remote)]
            self.assertEqual([row["id"], row["state_dir"], row["answer_route"]["by"]], ["failed-alive", str(remote), "none"])
            prefix = lane_lease.in_session({"session": str(remote)}, local)
            self.assertEqual(prefix, f"env -u TMUX -u TMUX_PANE -u TP_SESSION TP_STATE_DIR={shlex.quote(str(remote))} ")
            needs = row["answer_route"]["needs"]
            self.assertIn(["stop", "failed-alive"], needs)  # a command is an argument list, never text
            text = self.notifier({"external": [{"start": [row], "rows": [row]}]})["external"][0]["submitted"]
            self.assertIn(req, text)
            self.assertIn(f"First: {needs_text(needs, prefix)}", text)  # the command's own words, with the task's session
            commands = re.findall(r"(?:env (?:-u \S+ )+TP_STATE_DIR=(?:(?:'[^']*'|\"'\")+|\S+) )?tp-agent [a-z-]+ [A-Za-z0-9._-]+",
                                  text)
            self.assertEqual(sorted({c.split("tp-agent ")[1].split()[0] for c in commands}), ["inspect", "stop"], text)
            typed_in_main = {**self.env, "TP_STATE_DIR": str(local), "TMUX": "/private/tmp/tmux-0/default,1,0",
                             "TMUX_PANE": "%1", "TP_SESSION": "main"}
            for command in commands:
                with self.subTest(external=command, session=name):
                    self.assertTrue(command.startswith(prefix + "tp-agent "), command)
                    self.assertEqual(shlex.split(command)[7], f"TP_STATE_DIR={remote}")  # the exact path, two spaces and all
                    self.assertEqual(self.acts_on(command, typed_in_main), made[remote].resolve())

    def acts_on(self, command, env):
        """Runs a suggested command as a shell would in a pane with environment `env` (its `env -u ... VAR=value` prefix
        applied), in-process; returns the task directory it acted on (inspect: the row it printed; stop: the session tp-kill
        was started in). tp-kill is recorded, not run; any other child process fails the test."""
        words = shlex.split(command)
        env = dict(env)
        if words[0] == "env":
            words = words[1:]
            while words[0] == "-u":
                env.pop(words[1], None)
                words = words[2:]
            while "=" in words[0]:
                name, value = words[0].split("=", 1)
                env[name] = value
                words = words[1:]
        self.assertEqual(words[0], "tp-agent")
        killed = []

        def run(argv, *args, **kwargs):
            self.assertTrue(str(argv[0]).endswith("bin/tp-kill"), argv)
            killed.append(Path(os.environ["TP_STATE_DIR"]).resolve() / "tasks" / argv[1])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        out = io.StringIO()
        with mock.patch.object(self.tp.subprocess, "run", side_effect=run), \
                mock.patch.object(self.tp, "write_index", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.tp.main(words[1:], env=env)
        if words[1] == "stop":
            (target,) = killed
            return target
        return Path(json.loads(out.getvalue())["task_dir"]).resolve()

    def runs(self, task, command):
        """Runs a suggested command in-process against `task` (pane_send.send faked; retry's new attempt is not started:
        dispatch is replaced by a recorder; its live-process guard is the real one, over the faked pane table). None when
        tp-agent took it, else its error."""
        argv = shlex.split(command)[1:]
        started = []
        with mock.patch.object(self.tp, "dispatch", side_effect=lambda args, base: started.append(args) or {"id": "next"}), \
                mock.patch.object(self.tp, "write_index", return_value=None):
            value, _, error, err = self.run_agent(*argv)
        if error is None and argv[0] == "retry" and not started:
            return "retry did not start a new attempt: " + err
        return error

    def parses(self, command):
        """The verb tp-agent's real parser makes of `command` (an `env -u ... VAR=value` prefix is the shell's, not argv)."""
        import argparse
        import shlex
        words = shlex.split(command)
        argv = words[words.index("tp-agent") + 1:]

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
