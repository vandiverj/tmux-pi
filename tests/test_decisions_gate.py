"""Autonomy gate condition 2 (Phase D2): an open `blocking` decision of an attached workspace refuses the dispatch its `Affects` covers,
and only a `--by user` answer (or a withdrawal) closes it. Same fixtures as tests/test_autonomy.py: temporary repositories, work store
and state directories, no model."""
import json
from pathlib import Path
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_autonomy import AutonomyCase  # noqa: E402
import autonomy  # noqa: E402

DECISION = "blocking decision demo/D1 is open"


class DecisionCase(AutonomyCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {key: self.env[key] for key in ("TP_WORK_CONFIG", "TP_WORK_DIR")})  # the in-process gate reads this run's store
        patch.start()
        self.addCleanup(patch.stop)
        self.on()
        self.clear()  # recovery is done: only a decision can gate now

    def decide(self, workspace="demo", *affects, blocking=True):
        args = ["work", "decision", "add", workspace, "--question", "Ship it?"]
        for target in affects:
            args += ["--affects", target]
        return self.ok(*args, *(["--blocking"] if blocking else []))

    def answer(self, ident="demo/D1", by="agent", *more):
        return self.ok("work", "decision", "answer", ident, "--text", "go", "--by", by, *more)

    def gated(self, run):
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn(DECISION, run.stderr)
        self.assertIn("work decision answer demo/D1", run.stderr)  # names how to answer it
        self.assertIn("--by user", run.stderr)

    def admitted(self, **kw):
        self.assertIsNone(autonomy.gate(self.state, role="coder", deliverable="Report", **kw))


class CoverageTests(DecisionCase):
    def test_a_workspace_decision_refuses_dispatch_and_spawn_and_writes_nothing(self):
        self.decide()
        self.gated(self.dispatch_role())
        self.gated(self.dispatch_role("researcher", ""))
        self.gated(self.spawn("--", "plain"))
        self.nothing_started()
        self.assertFalse(self.marker()["pending"])  # condition 2 only: recovery is clear

    def test_lane_and_task_targets_cover_only_what_they_name(self):
        self.decide("demo", "lane:beta")
        self.decide("demo", "task:coder-x1")
        self.admitted()
        self.admitted(lane="alpha", parent="coder-other")
        self.assertIn("demo/D1", autonomy.gate(self.state, role="coder", lane="beta", lane_step="s1"))
        self.assertIn("demo/D2", autonomy.gate(self.state, role="coder", parent="coder-x1"))
        self.assertIn("demo/D2", self.dispatch_role(extra=["--parent", "coder-x1"]).stderr)
        self.assertEqual(self.dispatch_role().returncode, 0)  # a dispatch the decisions do not name proceeds

    def test_an_entry_with_no_recognised_target_blocks_as_the_workspace(self):
        self.decide("demo", "next:2")
        self.gated(self.dispatch_role())

    def test_a_decision_with_several_targets_covers_any_of_them(self):
        self.decide("demo", "lane:beta", "task:coder-x1")
        self.assertIsNotNone(autonomy.gate(self.state, role="coder", lane="beta"))
        self.assertIsNotNone(autonomy.gate(self.state, role="coder", parent="coder-x1"))
        self.admitted()

    def test_a_review_of_a_finished_task_is_admitted_by_the_same_rule(self):
        author, _ = self.finished_author()
        self.decide()
        self.assertIsNone(autonomy.gate(self.state, preamble_role="code-reviewer", review_of=author))
        self.assertIn(DECISION, autonomy.gate(self.state, preamble_role="code-reviewer", review_of="coder-missing"))
        self.gated(self.dispatch_role())


class WhatDoesNotGateTests(DecisionCase):
    def test_a_non_blocking_decision_never_gates(self):
        self.decide(blocking=False)
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.assertEqual(self.spawn("--", "plain").returncode, 0)

    def test_an_answered_or_withdrawn_decision_does_not_gate(self):
        self.decide()
        self.decide()
        self.gated(self.dispatch_role())
        self.assertEqual(self.answer("demo/D1", "user", "--quote", "yes, ship it")["status"], "answered")
        self.ok("work", "decision", "withdraw", "demo/D2", "--reason", "moot")
        self.assertEqual(self.dispatch_role().returncode, 0)

    def test_a_decision_in_a_workspace_not_attached_to_this_run_does_not_gate(self):
        self.ok("work", "new", "Other", "--id", "other")
        self.decide("other")
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.assertEqual(self.spawn("--", "plain").returncode, 0)

    def test_another_run_in_the_same_repo_is_unaffected(self):
        self.decide()
        state2 = self.root / "session2" / ".tmux-pi"
        (state2 / "tasks").mkdir(parents=True)
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "dispatch", "--role", "coder", "--task", "x", "--worker", "pi", "--model", MODEL,
                              "--thinking", "medium", "--workdir", str(self.project), "--deliverable", "Report"],
                             env={**self.env, "TP_STATE_DIR": str(state2)}, text=True, capture_output=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.gated(self.dispatch_role())

    def test_autonomy_off_is_unaffected(self):
        self.decide()
        self.gated(self.dispatch_role())
        self.ok("autonomy", "off")
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.assertEqual(self.spawn("--", "plain").returncode, 0)


class AnswerRuleTests(DecisionCase):
    def test_an_agent_answer_leaves_it_gating_and_a_user_answer_with_a_quote_clears_it(self):
        self.decide()
        value = self.answer("demo/D1", "agent", "--quote", "looks fine to me")
        self.assertFalse(value["clears_blocking"])
        self.assertEqual(value["status"], "open")
        self.gated(self.dispatch_role())
        text = (self.store / "workspaces" / "demo.md").read_text()
        self.assertIn("By: agent", text)
        self.assertIn("Quote: looks fine to me", text)  # recorded, though it cleared nothing
        value = self.answer("demo/D1", "user", "--quote", "Ship it, go ahead.")
        self.assertTrue(value["clears_blocking"])
        text = (self.store / "workspaces" / "demo.md").read_text()
        self.assertIn("By: user", text)
        self.assertIn("Quote: Ship it, go ahead.", text)
        self.assertEqual(self.dispatch_role().returncode, 0)

    def test_a_user_answer_without_a_quote_leaves_it_gating(self):
        self.decide()
        for extra in ([], ["--quote", "   "]):
            value = self.answer("demo/D1", "user", *extra)
            self.assertFalse(value["clears_blocking"])
            self.assertEqual(value["status"], "open")
            self.gated(self.dispatch_role())

    def test_the_help_says_this_is_policy_not_authentication(self):
        run = self.run_cli("work", "decision", "answer", "--help")
        self.assertIn("not authentication", " ".join(run.stdout.split()))


class QueueTests(DecisionCase):
    def test_queued_work_waits_with_the_decision_and_starts_after_the_user_answers(self):
        ident = self.queue()
        self.decide()
        self.assertEqual(self.ok("tick")["started"], [])
        reason = json.loads((self.state / "queue" / f"{ident}.json").read_text())["queue_reason"]
        self.assertIn(DECISION, reason)
        self.answer("demo/D1", "agent")
        self.assertEqual(self.ok("tick")["started"], [])
        self.answer("demo/D1", "user", "--quote", "go")
        self.assertEqual(self.ok("tick")["started"], [ident])

    def test_a_queued_item_the_decision_does_not_cover_starts(self):
        ident = self.queue()
        self.decide("demo", "task:coder-x1")
        self.assertEqual(self.ok("tick")["started"], [ident])


class FailClosedTests(DecisionCase):
    def test_an_attached_workspace_that_cannot_be_read_refuses(self):
        (self.store / "workspaces" / "demo.md").unlink()
        self.assertIn("autonomy state cannot be read", autonomy.gate(self.state, role="coder"))

    def test_an_unreadable_attachment_record_refuses_and_a_missing_one_admits(self):
        self.decide()
        record = self.state / "session-work.json"
        for broken in ("{broken", "{}", "[]", '{"attached": {}}', '{"attached": [123]}', '{"attached": ["demo", ""]}'):
            record.write_text(broken)
            self.assertIn("attachment record", autonomy.gate(self.state, role="coder") or "", broken)
        record.unlink()
        self.admitted()  # nothing attached: no decision in scope


class StatusTests(DecisionCase):
    def test_status_and_index_name_the_decision_not_dispatch_admitted(self):
        self.assertIsNone(autonomy.status(self.state)["blocked"])
        self.assertIn("dispatch admitted", autonomy.index_entry(self.state)["line"])
        self.decide("demo", "lane:beta")
        value = autonomy.status(self.state)
        self.assertEqual(value["blocked"]["decision"], "demo/D1")
        entry = autonomy.index_entry(self.state)
        self.assertTrue(entry["blocked"])
        self.assertIn("demo/D1", entry["line"])
        self.assertIn("refused", entry["line"])
        self.assertNotIn("dispatch admitted", entry["line"])
        self.assertNotIn("Ship it", json.dumps(entry))  # no question text in the index
        self.answer("demo/D1", "user", "--quote", "go")
        self.assertIsNone(autonomy.status(self.state)["blocked"])


if __name__ == "__main__":
    unittest.main()
