"""Isolated adaptive-goal ledger tests. No tmux session or worker is launched."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from goal import GoalError, command, plan, gate_attempt  # noqa: E402


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name).resolve()
        # A worker shell carries its own TP_STATE_DIR; these tests use the legacy project root.
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("TP_STATE_DIR", None)
        self.spec = {"id": "repair", "objective": "Repair all parser behaviors and regression tests",
                     "done": "All original parser requirements verified; required reviews approved",
                     "scope": "Only this project parser and tests; no external writes",
                     "workdir": str(self.project),
                     "budget": {"seconds": 3600, "turns": 10, "tokens": 10000, "cost_micros": 100000, "attempts": 6}}

    def act(self, action, **kw):
        return command(self.project, "repair", action, **kw)

    def started(self):
        s = plan(self.project, self.spec)
        return self.act("start", confirm="START-GOAL", approve_sha256=s["spec_sha256"])

    def admit(self):
        s = self.act("admit")
        return s["inflight"]["ticket"]

    def meter(self, ticket, event_id="m1", turns=1, tokens=50, cost_micros=10):
        return self.act("usage", ticket=ticket, event_id=event_id, turns=turns, tokens=tokens, cost_micros=cost_micros)

    def settle(self, ticket, outcome="progress", evidence="Inspected parser and ran the matching tests", blocker=None):
        return self.act("settle", ticket=ticket, outcome=outcome, evidence=evidence, blocker=blocker)

    def test_plan_requires_distinct_human_start_bound_to_scope(self):
        s = plan(self.project, self.spec)
        self.assertEqual(s["status"], "proposed")
        self.assertEqual(plan(self.project, self.spec), s)
        with self.assertRaises(GoalError):
            self.act("admit")
        with self.assertRaises(GoalError):
            self.act("start", confirm="START-GOAL", approve_sha256="0" * 64)
        tampered = copy.deepcopy(self.spec)
        tampered["objective"] = "Deploy production"
        with self.assertRaises(GoalError):
            plan(self.project, tampered)
        self.assertEqual(self.act("start", confirm="START-GOAL", approve_sha256=s["spec_sha256"])["status"], "active")
        with self.assertRaises(GoalError):
            self.act("start", confirm="START-GOAL", approve_sha256=s["spec_sha256"])

    def test_adaptive_turns_actual_meter_and_idempotent_event(self):
        self.started()
        ticket = self.admit()
        with self.assertRaises(GoalError):
            self.act("admit")
        self.meter(ticket)
        self.meter(ticket)
        with self.assertRaises(GoalError):
            self.meter(ticket, tokens=100)
        s = self.settle(ticket)
        self.assertEqual(s["usage"], {"turns": 1, "tokens": 50, "cost_micros": 10})
        self.assertEqual(s["attempts"], 1)
        self.assertIsNone(s["inflight"])
        self.assertEqual(s["status"], "active")
        self.assertNotEqual(self.admit(), ticket)

    def test_concurrent_admission_has_single_durable_ticket(self):
        self.started()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self._admit_or_error(), range(2)))
        self.assertEqual(sum(isinstance(x, str) for x in results), 1)
        self.assertEqual(sum(isinstance(x, GoalError) for x in results), 1)
        self.assertEqual(self.act("status")["attempts"], 1)

    def _admit_or_error(self):
        try:
            return self.admit()
        except GoalError as exc:
            return exc

    def test_repeated_ordinary_blocker_and_immediate_safety_block(self):
        self.started()
        for n in range(3):
            t = self.admit()
            self.meter(t, event_id=f"m{n}")
            s = self.settle(t, outcome="no_progress", blocker="missing-fixture")
            self.assertEqual(s["status"], "blocked" if n == 2 else "active")
        with self.assertRaises(GoalError):
            self.act("admit")
        self.act("resume", confirm="RESUME-GOAL", diagnosis="Human supplied fixture")
        t = self.admit()
        self.meter(t, event_id="m4")
        self.assertEqual(self.settle(t, "safety_block", "Trust prompt requires a human")["status"], "suspended")

    def test_restart_unknown_turn_never_replays(self):
        self.started()
        t = self.admit()
        s = self.act("reconcile")
        self.assertEqual(s["status"], "suspended")
        self.assertEqual(s["inflight"]["ticket"], t)
        with self.assertRaises(GoalError):
            self.act("admit")
        with self.assertRaises(GoalError):
            self.act("resume", confirm="RESUME-GOAL", diagnosis="unknown")
        with self.assertRaises(GoalError):
            self.meter(t)  # Old runtime cannot assert ownership after restart.
        self.assertEqual(self.act("status")["status"], "suspended")
        with self.assertRaises(GoalError):
            self.act("resolve", confirm="wrong", evidence="Verified no worker pane")
        self.act("resolve", confirm="RESOLVE-GOAL-TURN", evidence="Human inspected session: no worker pane or turn")
        self.act("resume", confirm="RESUME-GOAL", diagnosis="Previous turn cancelled and inspected")
        self.assertNotEqual(self.admit(), t)

    def test_pause_stop_prevent_admissions_and_allow_final_accounting(self):
        self.started()
        t = self.admit()
        self.assertEqual(self.act("pause", reason="Human pause")["status"], "paused")
        with self.assertRaises(GoalError):
            self.act("admit")
        self.meter(t)
        self.settle(t)
        self.assertEqual(self.act("status")["status"], "paused")
        self.act("resume", confirm="RESUME-GOAL", diagnosis="Human chose to continue")
        t = self.admit()
        self.act("stop")
        self.meter(t, event_id="after-stop")
        self.settle(t)
        self.assertEqual(self.act("status")["status"], "stopped")
        with self.assertRaises(GoalError):
            self.act("admit")

    def test_budgets_charge_current_turn_before_stopping_future_turns(self):
        self.spec["budget"]["tokens"] = 15
        self.started()
        t = self.admit()
        self.meter(t, tokens=15)
        s = self.settle(t)
        self.assertEqual(s["status"], "budget_limited")
        self.assertEqual(s["usage"]["tokens"], 15)
        with self.assertRaises(GoalError):
            self.act("admit")

    def test_full_scope_audit_can_complete_only_after_gates(self):
        self.started()
        t = self.admit()
        self.meter(t)
        s = self.settle(t, "complete", "Read every parser requirement and inspected the current parser and its regressions; checked each expected behavior against the actual files and test output. No edit task was created.")
        self.assertEqual(s["status"], "complete")
        self.assertIn("reviewed", s["reason"])

    def test_preexisting_live_worker_cannot_claim_synthetic_goal_input(self):
        self.started()
        d = self.project / ".tmux-pi" / "tasks" / "other"
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"id": "other", "workdir": str(self.project), "created": "2000-01-01T00:00:00Z", "pane": "%1"}))
        (d / "state.json").write_text('{"state":"waiting"}')
        with patch("goal.subprocess.run") as run:
            run.return_value.stdout = "%1 0\n"
            s = self.act("admit")
        self.assertEqual(s["status"], "suspended")
        self.assertIn("follow-up", s["reason"])
        self.assertEqual(s["attempts"], 0)

    def test_unlinked_new_worker_suspends_no_assumed_review_or_usage(self):
        s = self.started()
        t = self.admit()
        task = self.project / ".tmux-pi" / "tasks" / "author"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "author", "workdir": str(self.project), "created": s["started"]}))
        with self.assertRaises(GoalError):
            self.act("link-task", ticket=t, task_id="author")
        self.meter(t)
        s = self.settle(t)
        self.assertEqual(s["status"], "suspended")
        self.assertIn("not linked", s["reason"])

    def test_task_evidence_follows_the_run_state_dir(self):
        s = self.started()
        run = self.project / ".tmux-pi" / "sessions" / "run"
        os.environ["TP_STATE_DIR"] = str(run)
        t = self.admit()
        task = run / "tasks" / "author"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "author", "workdir": str(self.project), "created": s["started"], "pane": "%1", **self.provenance()}))
        (task / "state.json").write_text(json.dumps({"state": "working", "turns": 1, "tokens": {"input": 3, "output": 2}, "cost": 0.00001}))
        self.assertEqual(self.act("link-task", ticket=t, task_id="author")["tasks"]["author"]["ticket"], t)
        self.assertEqual(self.act("status")["usage"]["tokens"], 5)
        # Another project's state dir, or a path that is not a run directory, is never read.
        outside = self.project.parent / (self.project.name + "-other")
        for foreign in (outside / ".tmux-pi" / "sessions" / "run", run / "tasks"):
            os.environ["TP_STATE_DIR"] = str(foreign)
            with self.assertRaises(GoalError):
                self.act("status")
        # Without TP_STATE_DIR only the legacy root can be meant; the linked task is not found there.
        del os.environ["TP_STATE_DIR"]
        with self.assertRaises(GoalError):
            self.act("link-reviewer", author_id="author", reviewer_id="review-author")

    def test_wait_needs_authoritative_live_pane(self):
        self.started()
        t = self.admit()
        self.meter(t)
        s = self.settle(t, "wait", "Task may be running", blocker="absent")
        self.assertEqual(s["status"], "suspended")
        self.assertIn("missing/ambiguous", s["reason"])

    @staticmethod
    def provenance():
        return {"contract": {"kind": "task"}, "main_contract": {}, "policy_decision": {}, "preflight": {}, "decision": {"review": "required"}}

    def test_verified_live_handle_waits_no_new_attempt_then_suspends_on_review_gap(self):
        s = self.started()
        t = self.admit()
        task = self.project / ".tmux-pi" / "tasks" / "author"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "author", "workdir": str(self.project), "created": s["started"], "pane": "%1", **self.provenance()}))
        (task / "state.json").write_text(json.dumps({"state": "working", "turns": 1, "tokens": {"input": 3, "output": 2}, "cost": 0.00001}))
        self.act("link-task", ticket=t, task_id="author")
        self.meter(t)
        with patch("goal._live_task", return_value=True):
            s = self.settle(t, "wait", "Observed live worker", blocker="author")
            self.assertEqual(s["status"], "waiting")
            self.assertEqual(self.act("poll")["status"], "waiting")
        with self.assertRaises(GoalError):
            self.act("admit")
        # The handle is gone now. Say so; the real tmux would be asked otherwise, and any live pane %1 on the machine would keep it waiting.
        with patch("goal._live_task", return_value=False):
            s = self.act("poll")
        self.assertEqual(s["status"], "suspended")
        self.assertIn("review", s["reason"])

    def test_linked_task_requires_real_opposite_provider_review(self):
        s = self.started()
        t = self.admit()
        tasks = self.project / ".tmux-pi" / "tasks"
        author, reviewer = tasks / "author", tasks / "review-author"
        author.mkdir(parents=True)
        reviewer.mkdir()
        meta = {"id": "author", "workdir": str(self.project), "created": s["started"], "worker": "pi", "review": "review-author", **self.provenance()}
        (author / "meta.json").write_text(json.dumps(meta))
        (author / "result.md").write_text("Work")
        (author / "completed").write_text("1\n")
        (author / "state.json").write_text(json.dumps({"state": "waiting", "turns": 2, "tokens": {"input": 10, "output": 4}, "cost": 0.0001}))
        (reviewer / "meta.json").write_text(json.dumps({"id": "review-author", "worker": "claude", "workdir": str(self.project), "created": s["started"]}))
        (reviewer / "result.md").write_text("APPROVED\nIndependent check\nCoverage\nreviewed the changed files.")
        (reviewer / "state.json").write_text(json.dumps({"state": "waiting", "turns": 1, "tokens": {"input": 5, "output": 3}, "cost": 0.00005}))
        (reviewer / "review-of").write_text("author")
        marker = {"required": True, "status": "approved", "task_id": "author", "round_nonce": "nonce",
                  "approval": {"round_nonce": "nonce", "reviewer_id": "review-author", "author_sha256": hashlib.sha256(b"Work").hexdigest(),
                               "evidence_sha256": hashlib.sha256(b"APPROVED\nIndependent check\nCoverage\nreviewed the changed files.").hexdigest()}}
        (author / "review-required.json").write_text(json.dumps(marker))
        self.act("link-task", ticket=t, task_id="author")
        self.act("link-task", ticket=t, task_id="review-author")
        self.act("link-task", ticket=t, task_id="author")  # idempotent; no meter reset
        self.meter(t)
        s = self.settle(t)
        self.assertEqual(s["status"], "active")
        self.assertEqual(s["usage"], {"turns": 4, "tokens": 72, "cost_micros": 160})
        (reviewer / "result.md").write_text("REQUEST_CHANGES\nNot approved")
        s = self.act("admit")
        self.assertEqual(s["status"], "suspended")
        self.assertIn("review", s["reason"])

    def test_goal_policy_intersects_approved_paths_and_human_boundaries(self):
        self.spec["allowed_paths"] = ["src", "tests"]
        s = self.started()
        ticket = self.admit()
        main = {"action": "delegate", "mode": "delegated", "no_edits": False, "task_summary": "Fix parser in src/parser.py"}
        route = {"placement": "subagent", "worker": "pi", "model": "local/one", "task": main["task_summary"], "review": "none"}
        preflight = {"stop": False, "no_edits": False}
        policy = {"action": "recommend_manual", "review": {"risk": "low"}}
        args = (self.project, "repair", ticket, s["spec_sha256"])
        allowed = gate_attempt(*args, main, route, preflight, policy)
        self.assertEqual(allowed["review"], "required")
        for summary in ("Fix parser in other.py", "Deploy parser to production", "Fix parser in ../src/parser.py"):
            with self.assertRaises(GoalError, msg=summary):
                gate_attempt(*args, {**main, "task_summary": summary}, route, preflight, policy)
        with self.assertRaises(GoalError):
            gate_attempt(*args, main, route, preflight, {"review": {"risk": "high"}, "confirmation_required": True})
        self.act("pause", reason="Human pause")
        with self.assertRaises(GoalError):
            gate_attempt(*args, main, route, preflight, policy)

    def test_dispatcher_goal_contract_review_collect_and_next_handshake(self):
        harness = Path(__file__).resolve().parent / "dispatcher_runtime.mjs"
        result = subprocess.run(["node", "--experimental-strip-types", str(harness), "goal"], check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        self.assertTrue(data["terminate"])
        self.assertEqual(data["committed"]["policy"]["mode"], "approved_goal")
        self.assertEqual(data["taskReview"], "approved")
        self.assertTrue(data["goalNextHandshake"])
        calls = [c for c in data["calls"] if c["name"] in ("tp-goal", "tp-spawn", "tp-review", "tp-contract")]
        names = [c["name"] for c in calls]
        self.assertLess(names.index("tp-spawn"), names.index("tp-review"))
        self.assertLess(names.index("tp-review"), names.index("tp-contract"))
        self.assertEqual(sum(c["name"] == "tp-goal" and "link-task" in c["args"] for c in calls), 1)
        self.assertEqual(sum(c["name"] == "tp-goal" and "link-reviewer" in c["args"] for c in calls), 1)
        self.assertEqual(data["decisionAtContract"]["review"], "required")

    def test_corrupt_state_and_symlink_lock_fail_closed(self):
        self.started()
        path = self.project / ".tmux-pi" / "goals" / "repair.json"
        s = json.loads(path.read_text())
        s["spec"]["scope"] = "Everything"
        path.write_text(json.dumps(s))
        with self.assertRaises(GoalError):
            self.act("status")
        path.unlink()
        (self.project / ".tmux-pi" / "goals" / "repair.lock").unlink()
        (self.project / ".tmux-pi" / "goals" / "repair.lock").symlink_to(self.project / "outside")
        with self.assertRaises(OSError):
            self.act("status")


if __name__ == "__main__":
    unittest.main()
