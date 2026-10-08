"""T9: the small orchestration faults, one test each (see report in the task directory)."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestration  # noqa: E402  (the module, so its own tests are not collected twice)
from task_record import parse_record  # noqa: E402


def load_agent():
    loader = importlib.machinery.SourceFileLoader("tp_agent_under_test", str(ROOT / "bin/tp-agent"))
    spec = importlib.util.spec_from_loader("tp_agent_under_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class T9Tests(unittest.TestCase):
    # the orchestration fixture (fake tmux, state dir, cli helpers) without running its own tests again
    for _name in ("setUp", "raw", "cli", "task_dir", "settle", "dispatch", "retry"):
        locals()[_name] = test_orchestration.OrchestrationTests.__dict__[_name]
    del _name

    def archive(self, item, name=None):
        archive = self.state / "archive"
        archive.mkdir(exist_ok=True)
        (self.state / "tasks" / item["id"]).rename(archive / (name or item["id"]))
        return archive / (name or item["id"])

    # (a) retry and review of an archived task
    def test_retry_and_review_of_an_archived_task_name_where_it_is(self):
        for name in (None, "{id}-ended-20261008010101"):
            item = self.dispatch()
            self.settle(item, "RESULT_STATUS: complete\n")
            moved = self.archive(item, name.format(id=item["id"]) if name else None)
            for command in (("retry", item["id"], "--reason", "again"), ("review", item["id"])):
                with self.subTest(command=command[0], archived_as=moved.name):
                    run = self.raw(*command)
                    self.assertEqual(run.returncode, 1)
                    self.assertIn("session archive at " + str(moved.resolve()), run.stderr)
                    self.assertNotIn("unknown task", run.stderr)
        run = self.raw("retry", "no-such-task", "--reason", "again")
        self.assertIn("unknown task: no-such-task", run.stderr)

    def test_an_archived_dependency_or_ancestor_is_named_by_place(self):
        first = self.dispatch()
        waiting = self.dispatch(task="b", after=first["id"])    # queued behind the running first
        self.settle(first, "RESULT_STATUS: complete\n")
        moved = self.archive(first, first["id"] + "-ended-20261008010101")
        self.cli("tick")
        reason = json.loads((self.state / "queue" / f"{waiting['id']}.json").read_text()).get("queue_reason", "")
        self.assertIn("session archive at " + str(moved.resolve()), reason)
        self.assertNotIn("has not started", reason)
        agent = load_agent()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            child = self.lineage(base, 2)
            (base / "archive").mkdir()
            (base / "tasks" / "t1").rename(base / "archive" / "t1-ended-20261008010101")
            with self.assertRaisesRegex(ValueError, "passes through an archived task.*session archive at"):
                agent.escalation_evidence(base, child)

    # (h) unknown cost stays unknown in sums
    def test_routing_totals_and_ledger_keep_unknown_cost_unknown(self):
        import routing_evals
        from contract import ledger_row
        claude = {"cost": None, "tokens": None}
        pi = {"cost": 0.5, "tokens": {"output": 7}}
        self.assertEqual(routing_evals.totals([]), {"cost": None, "output_tokens": None})
        only = routing_evals.totals([claude, claude])
        self.assertEqual((only["cost"], only["cost_partial"], only["cost_unknown_tasks"], only["output_tokens"]), (None, False, 2, None))
        mixed = routing_evals.totals([claude, pi])
        self.assertEqual((mixed["cost"], mixed["cost_partial"], mixed["cost_unknown_tasks"]), (0.5, True, 1))
        self.assertEqual((mixed["output_tokens"], mixed["output_tokens_partial"]), (7, True))
        full = routing_evals.totals([pi, pi])
        self.assertEqual((full["cost"], full["cost_partial"]), (1.0, False))
        task = self.state / "tasks" / "w"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "w", "worker": "claude"}))
        (task / "state.json").write_text(json.dumps({"state": "working", "tokens": {}, "cost": 0, "costKnown": False}))
        self.assertIsNone(ledger_row(task, 0)["cost"])
        (task / "state.json").write_text(json.dumps({"state": "working", "tokens": {}, "cost": 0.25}))
        self.assertEqual(ledger_row(task, 0)["cost"], 0.25)

    # (c) a queue entry's reason is the current one
    def test_a_waiting_queue_entry_gets_the_current_reason(self):
        first = self.dispatch()
        waiting = self.dispatch(task="b", after=first["id"])
        path = self.state / "queue" / f"{waiting['id']}.json"
        path.write_text(json.dumps(dict(json.loads(path.read_text()), queue_reason="start failed: an earlier tick")))
        self.cli("tick")
        self.assertEqual(json.loads(path.read_text())["queue_reason"], f"waiting for {first['id']}")

    # (f) the lineage walk has review_rounds' limits
    def lineage(self, state, count, loop=False):
        tasks = state / "tasks"
        for number in range(count):
            directory = tasks / f"t{number}"
            directory.mkdir(parents=True)
            parent = f"t{number + 1}" if number + 1 < count else ("t0" if loop else "")
            (directory / "meta.json").write_text(json.dumps({"id": f"t{number}", "parent": parent}))
            (directory / "exit").write_text("1")
        return tasks / "t0"

    def test_escalation_evidence_follows_the_whole_lineage_and_refuses_past_the_limits(self):
        agent = load_agent()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            start = self.lineage(base, 30)
            self.assertEqual(agent.escalation_evidence(base, start)[0], 30)    # past the old 12-step stop
        import review_rounds
        for count, loop, message in ((review_rounds.MAX_ANCESTORS + 1, False, "longer than"), (3, True, "loops back")):
            with self.subTest(message), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                start = self.lineage(base, count, loop)
                with self.assertRaisesRegex(ValueError, message):
                    agent.escalation_evidence(base, start)
        with tempfile.TemporaryDirectory() as tmp:    # exactly MAX_ANCESTORS tasks is within the limit
            base = Path(tmp)
            start = self.lineage(base, review_rounds.MAX_ANCESTORS)
            self.assertEqual(agent.escalation_evidence(base, start)[0], review_rounds.MAX_ANCESTORS)

    # (g) capability-resolve writes the derived lifecycle
    def test_capability_resolve_records_the_derived_state_not_working(self):
        from capabilities import select_capabilities
        task = self.state / "tasks" / "worker-1"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "worker-1", "worker": "pi", "pane": "%1"}))
        (task / "request.md").write_text("Plan complex tradeoffs\n")
        (task / "state.json").write_text('{"state":"waiting"}')
        (task / "result.md").write_text("done\nRESULT_STATUS: complete\n")
        manifest = select_capabilities("Plan complex tradeoffs")
        manifest["selected"], manifest["selected_categories"], manifest["disclosures"] = [], [], []
        pending = {"request": "Sequential Thinking | reason=tradeoffs", "event": {"id": "sequential_thinking", "decision": "human_required"}}
        manifest["events"].append({"type": "capability_request", **pending["event"], "request": pending["request"]})
        (task / "context-manifest.json").write_text(json.dumps({"capability_manifest": manifest}))
        (task / "pending-capability.json").write_text(json.dumps(pending))
        fake = self.root / "tmux-bin"
        fake.mkdir()
        (fake / "tmux").write_text("#!/bin/sh\nexit 0\n")
        (fake / "tmux").chmod(0o755)
        env = dict(self.env, PATH=str(fake) + os.pathsep + self.env["PATH"])
        for key in ("TP_TASK_DIR", "TP_TASK_ID"):
            env.pop(key, None)
        run = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.state), "capability-resolve", "worker-1", "--approve"],
                             env=env, capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(parse_record(task)["state"], "settled")    # the files say a reply with a status; not "working"

    # (h) usage in the task record
    def test_the_record_reports_tokens_only_when_counted_and_no_cost_for_a_harness_that_has_none(self):
        agent = load_agent()
        counted = {"input": 5, "output": 7, "cacheRead": 0, "cacheWrite": 0}
        zero = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        self.assertEqual(agent.usage_fields({"harness": "claude", "tokens": counted, "cost": 0, "costKnown": False}),
                         {"tokens": counted, "cost_known": False})
        self.assertEqual(agent.usage_fields({"harness": "claude", "tokens": zero, "cost": 0, "costKnown": False}),
                         {"tokens": None, "cost_known": False})
        self.assertEqual(agent.usage_fields({"harness": "pi", "tokens": counted, "cost": 0.25}), {"tokens": counted, "cost": 0.25})
        self.assertEqual(agent.usage_fields({}), {"tokens": None, "cost": None})
        item = self.dispatch()
        (self.task_dir(item) / "state.json").write_text(json.dumps({"state": "working", "harness": "claude", "tokens": counted,
                                                                      "cost": 0, "costKnown": False}))
        row = self.cli("inspect", item["id"])
        self.assertEqual((row["tokens"], row["cost_known"], "cost" in row), (counted, False, False))

    # (i) a --context spec over the cap
    def test_a_context_over_the_cap_is_refused_at_dispatch_with_the_cap_the_size_and_the_largest_range(self):
        big = self.project / "big.txt"
        lines = ["x" * 29 for _ in range(3000)]    # 30 characters a line with its newline
        big.write_text("\n".join(lines) + "\n")
        fit = 60000 // 30    # n lines hold 30n - 1 characters
        for spec, start in (("big.txt", 1), ("big.txt:10-3000", 10)):
            with self.subTest(spec):
                run = self.raw("dispatch", "--role", "coder", "--task", "t", "--worker", "pi", "--model", "openai-codex/gpt-6-sol",
                               "--thinking", "medium", "--workdir", str(self.project), "--context", spec)
                self.assertEqual(run.returncode, 1)
                last = start + fit - 1
                self.assertIn("the limit is 60000", run.stderr)
                self.assertIn("chars", run.stderr)
                self.assertIn(f"big.txt:{start}-{last}", run.stderr)
                self.assertFalse((self.state / "queue").exists() and list((self.state / "queue").iterdir()))
                self.assertEqual(list((self.state / "tasks").iterdir()), [])
        big.write_text("ok\n")
        self.assertEqual(self.dispatch(context="big.txt")["status"], "starting")


if __name__ == "__main__":
    unittest.main()
