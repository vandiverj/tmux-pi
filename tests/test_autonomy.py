"""Autonomy recovery gate (Phase D1): lib/autonomy.py through bin/tp-spawn, bin/tp-agent (dispatch, tick, autonomy on|off|status,
work recovered) and the index's `autonomy` entry. Repositories, the work store and the state directories are temporary; tmux is the
private fixture server of tests/lane_fixture.py, and no model runs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from lane_fixture import MODEL, ROOT  # noqa: E402
from test_lane_writer import LaneCase, git  # noqa: E402
import autonomy  # noqa: E402
import work_checkpoint  # noqa: E402

CLAUDE = {"TP_MAIN_HARNESS": "claude", "TP_MODE": "orchestrator"}
REFUSED = "autonomy is on and recovery is pending"


class AutonomyCase(LaneCase):
    def setUp(self):
        super().setUp()
        config = self.root / "work-config.json"
        config.write_text("{}")
        self.env["TP_WORK_CONFIG"] = str(config)
        for key in ("TP_MAIN_HARNESS", "TP_MODE", "TP_TASK_DIR"):
            self.env.pop(key, None)
        self.store = self.root / "work-store"
        self.cli("work", "new", "Demo", "--id", "demo")

    # ---- running things
    def run_cli(self, *args, env=None, script="tp-agent"):
        return subprocess.run([str(ROOT / "bin" / script), *args], env={**self.env, **(env or {})}, text=True, capture_output=True, timeout=90)

    def ok(self, *args, env=None):
        run = self.run_cli(*args, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout) if run.stdout.strip().startswith(("{", "[")) else run.stdout

    def on(self, reason="test"):
        return self.ok("autonomy", "on", "--workspace", "demo", "--reason", reason, env=CLAUDE)

    def recovered(self, *more):
        return self.run_cli("work", "recovered", "--evidence", "checked the live records", *more)

    def clear(self):
        run = self.recovered("--no-checkpoint")
        self.assertEqual(run.returncode, 0, run.stderr)

    def marker(self, state=None):
        return work_checkpoint.marker(state or self.state)

    def spawn(self, *args, workdir=None, env=None, worker="pi"):
        return self.run_cli("--worker", worker, "--model", MODEL, "--thinking", "medium", "--workdir", str(workdir or self.project), *args,
                            env=env, script="tp-spawn")

    def dispatch_role(self, role="coder", deliverable="Report", extra=(), env=None):
        args = ["dispatch", "--role", role, "--task", "Make a focused change", "--worker", "pi", "--model", MODEL, "--thinking", "medium",
                "--workdir", str(self.project), *extra]
        if deliverable:
            args += ["--deliverable", deliverable]
        return self.run_cli(*args, env=env)

    def nothing_started(self):
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(list((self.state / "queue").glob("*.json")) if (self.state / "queue").is_dir() else [], [])

    def refuses(self, run):
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertIn(REFUSED, run.stderr)
        self.assertIn("tp-agent work recovered", run.stderr)  # the refusal names the command that clears it

    def finished_author(self, role="coder"):
        item, task = self.dispatch_ok(role)
        self.settle(task)
        return item["id"], task

    def dispatch_ok(self, role="coder"):
        run = self.dispatch_role(role)
        self.assertEqual(run.returncode, 0, run.stderr)
        item = json.loads(run.stdout)
        return item, self.state / "tasks" / item["id"]

    def queue(self, ident="coder-queued0001"):
        item = {"id": ident, "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.project), "worker": "pi",
                "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [], "context": [],
                "notes": [], "tests": "none", "queue_reason": "waiting"}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue" / f"{ident}.json").write_text(json.dumps(item))
        return ident


class OffAndOnTests(AutonomyCase):
    def test_with_autonomy_off_nothing_is_refused_even_while_recovery_is_pending(self):
        self.ok("work", "compacted", "--reason", "precompact:auto")
        self.assertTrue(self.marker()["pending"])
        self.assertFalse((self.state / "session-autonomy.json").exists())
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.assertEqual(self.spawn("--", "plain task").returncode, 0)
        self.assertEqual(self.ok("autonomy", "status"), {"autonomy": "off"})

    def test_on_attaches_the_workspace_arms_recovery_and_reports_what_is_blocked(self):
        value = self.on("overnight run")
        self.assertEqual((value["autonomy"], value["workspace"], value["attached"], value["reason"]), ("on", "demo", "demo", "overnight run"))
        self.assertTrue(value["recovery"]["pending"])
        self.assertEqual(value["blocked"]["by"], "recovery")
        self.assertEqual(json.loads((self.state / "session-work.json").read_text())["attached"], ["demo"])
        self.assertTrue(self.marker()["pending"])
        self.assertIn("autonomy on", (self.store / "workspaces" / "demo.md").read_text())  # the audit line

    def test_every_dispatch_class_is_refused_while_pending_and_nothing_is_written(self):
        self.on()
        for role, deliverable in (("researcher", ""), ("architect", ""), ("coder", "Report"), ("coder", ""), ("code-reviewer", "")):
            with self.subTest(role=role, deliverable=deliverable):
                self.refuses(self.dispatch_role(role, deliverable))
        self.refuses(self.dispatch_role("coder", "Report", extra=["--after", "coder-missing"]))
        self.nothing_started()

    def test_a_role_less_start_with_or_without_a_deliverable_or_lane_is_refused_by_tp_spawn_itself(self):
        self.on()
        for extra in ((), ("--deliverable", "Report"), ("--role", "coder"), ("--role", "researcher", "--deliverable", "Report")):
            with self.subTest(extra=extra):
                self.refuses(self.spawn(*extra, "--", "work"))
        self.refuses(self.spawn("--lane", "alpha", "--lane-step", "s1", "--", "work", workdir=self.repo))
        self.nothing_started()

    def test_a_lane_dispatch_is_refused_before_any_lane_branch_or_worktree_exists(self):
        self.on()
        self.refuses(self.lane_dispatch())
        self.no_lane()
        self.assertFalse(self.lane_dir().exists())
        self.nothing_started()
        self.clear()
        self.lane_ok()  # and once recovery is done the same dispatch creates the lane
        self.assertTrue(self.worktree.is_dir())

    def test_recovered_lets_every_class_start_again(self):
        self.on()
        self.refuses(self.dispatch_role())
        self.clear()
        self.assertFalse(self.marker()["pending"])
        for role in ("researcher", "architect", "coder"):
            self.assertEqual(self.dispatch_role(role, "Report" if role == "coder" else "").returncode, 0)
        self.assertEqual(self.spawn("--", "plain").returncode, 0)
        self.assertEqual(self.ok("autonomy", "status")["blocked"], None)

    def test_a_later_compaction_arms_the_gate_again(self):
        self.on()
        self.clear()
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.ok("work", "compacted", "--reason", "precompact:auto")
        self.refuses(self.dispatch_role())

    def test_the_gate_has_no_time_expiry(self):
        self.on()
        event = sorted((self.state / work_checkpoint.RECOVERY_DIR).glob("*.json"))[0]  # the oldest pending event names `since`
        event.write_text(json.dumps({**json.loads(event.read_text()), "since": "2001-01-01T00:00:00Z"}))
        self.refuses(self.dispatch_role())
        with mock.patch("time.time", return_value=4_000_000_000.0):  # far in the future: the answer does not move
            self.assertIn(REFUSED, autonomy.gate(self.state, role="coder"))
        self.assertIn("since 2001-01-01", autonomy.gate(self.state, role="coder"))

    def test_the_off_switch_admits_again_and_leaves_running_workers_and_the_marker_alone(self):
        (self.state / "live-window").write_text("")
        item, task, pane, pid = self.live_task(state="working", result=None)
        self.on()
        self.refuses(self.dispatch_role())
        self.assertTrue(self.alive(pane))  # turning it on never stops what runs
        off = self.ok("autonomy", "off", "--reason", "back at the keyboard")
        self.assertEqual((off["autonomy"], off["was_on"], off["workspace"]), ("off", True, "demo"))
        self.assertFalse((self.state / "session-autonomy.json").exists())
        self.assertTrue(self.marker()["pending"])  # the marker is a record, not the switch
        self.assertTrue(self.alive(pane))
        self.assertEqual(self.dispatch_role().returncode, 0)
        self.assertIn("autonomy off: back at the keyboard", (self.store / "workspaces" / "demo.md").read_text())
        self.assertEqual(self.ok("autonomy", "off")["was_on"], False)  # idempotent

    def test_another_run_is_unaffected(self):
        self.on()
        state2 = self.root / "session2" / ".tmux-pi"
        (state2 / "tasks").mkdir(parents=True)
        env2 = {**self.env, "TP_STATE_DIR": str(state2)}
        run = subprocess.run([str(ROOT / "bin/tp-agent"), "dispatch", "--role", "coder", "--task", "x", "--worker", "pi", "--model", MODEL,
                              "--thinking", "medium", "--workdir", str(self.project), "--deliverable", "Report"],
                             env=env2, text=True, capture_output=True, timeout=90)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.refuses(self.dispatch_role())

    def test_a_marker_or_autonomy_record_that_cannot_be_read_refuses_and_a_missing_one_is_off_or_clear(self):
        self.on()
        (self.state / "session-recovery.json").write_text("{not json")
        run = self.dispatch_role()
        self.refuses(run)
        self.assertIn("unreadable", run.stderr)
        self.assertEqual(self.recovered("--no-checkpoint").returncode, 0)  # an unreadable marker is cleared like a pending one
        self.assertEqual(self.dispatch_role().returncode, 0)
        (self.state / "session-autonomy.json").write_text("[]")  # present but not usable: fail closed, `autonomy off` is the way out
        run = self.dispatch_role()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("autonomy state cannot be read", run.stderr)
        self.assertEqual(self.spawn("--", "x").returncode, 1)
        self.assertEqual(self.ok("autonomy", "status")["autonomy"], "unreadable")
        self.ok("autonomy", "off")
        self.assertEqual(self.dispatch_role().returncode, 0)

    def test_a_failed_checkpoint_or_missing_workspace_still_reads_as_pending(self):
        self.on()
        (self.store / "workspaces" / "demo.md").unlink()
        self.refuses(self.dispatch_role())
        run = self.recovered()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("nothing was cleared", run.stderr)
        self.assertTrue(self.marker()["pending"])
        self.refuses(self.spawn("--", "x"))


class RecoveryEventTests(AutonomyCase):
    """`since`/`reason` are the oldest uncleared recovery event's; `events_pending` and `latest_*` say there are more and what the newest is."""

    def test_autonomy_on_is_two_events_and_the_status_names_the_first(self):
        self.on("one run")  # `autonomy on` arms recovery, then attaches the workspace (a second event)
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual((value["pending"], value["reason"], value["events_pending"], value["latest_reason"]), (True, "autonomy on", 2, "attach"))
        self.assertIn("(autonomy on); 2 events are pending, the first shown, the newest attach at " + value["latest_since"], self.dispatch_role().stderr)

    def test_one_event_is_its_own_latest_and_the_refusal_stays_as_it_was(self):
        self.ok("work", "compacted", "--reason", "precompact:auto")
        self.ok("autonomy", "on", "--workspace", "demo", "--reason", "x", env=CLAUDE)
        self.clear()
        self.ok("work", "compacted", "--reason", "precompact:auto")
        value = self.marker()
        self.assertEqual((value["reason"], value["events_pending"], value["latest_reason"], value["latest_since"]),
                         ("precompact:auto", 1, "precompact:auto", value["since"]))
        self.assertNotIn("events are pending", autonomy.refusal_text(autonomy.recovery(self.state)))

    def test_status_names_the_oldest_event_and_says_there_are_more(self):
        self.on("long run")
        self.ok("work", "compacted", "--reason", "precompact:auto")
        self.ok("work", "compacted", "--reason", "session-start:compact")
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual((value["pending"], value["reason"], value["events_pending"], value["latest_reason"]),
                         (True, "autonomy on", 4, "session-start:compact"))
        self.assertTrue(value["since"] and value["latest_since"] and value["since"] <= value["latest_since"])
        marker = self.marker()
        self.assertEqual((marker["reason"], marker["events_pending"], marker["latest_reason"], marker["latest_since"]),
                         ("autonomy on", 4, "session-start:compact", value["latest_since"]))
        run = self.dispatch_role()
        self.refuses(run)
        self.assertIn("(autonomy on); 4 events are pending, the first shown, the newest session-start:compact at " + value["latest_since"], run.stderr)

    def test_recovered_clears_every_event_and_the_fields_with_them(self):
        self.on()
        self.ok("work", "compacted", "--reason", "precompact:auto")
        self.clear()
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual((value["pending"], value["events_pending"], value["since"], value["reason"], value["latest_since"], value["latest_reason"]),
                         (False, 0, "", "", "", ""))

    def test_an_event_after_a_clear_counts_from_one(self):
        self.on()
        self.clear()
        self.ok("work", "compacted", "--reason", "precompact:manual")
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual((value["reason"], value["events_pending"], value["latest_reason"]), ("precompact:manual", 1, "precompact:manual"))

    def test_a_damaged_record_still_shows_the_latest_readable_event_and_reports_the_error_apart(self):
        self.on()
        self.ok("work", "compacted", "--reason", "session-start:compact")
        (self.state / work_checkpoint.RECOVERY_FILE).write_text("{not json")
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual((value["pending"], value["latest_reason"], value["events_pending"]), (True, "session-start:compact", 3))
        self.assertTrue(value["latest_since"])
        self.assertIn("the recovery record is unreadable", value["problem"])
        self.assertNotIn("unreadable", value["latest_reason"])

    def test_with_no_readable_event_the_problem_stands_in_for_the_latest_reason(self):
        self.on()
        for path in (self.state / work_checkpoint.RECOVERY_DIR).iterdir():
            path.write_text("{not json")
        value = self.ok("autonomy", "status")["recovery"]
        self.assertEqual(value["latest_reason"], "an unreadable recovery event")

    def test_the_fields_the_status_always_had_are_still_there(self):
        self.on()
        self.assertTrue({"pending", "since", "reason"} <= set(self.ok("autonomy", "status")["recovery"]))
        self.assertTrue({"pending", "generation", "cleared_generation", "since", "reason", "problem", "evidence"} <= set(self.marker()))

    def test_the_marker_of_a_state_that_never_compacted_has_the_new_fields_empty(self):
        marker = self.marker()
        self.assertEqual((marker["pending"], marker["events_pending"], marker["latest_since"], marker["latest_reason"]), (False, 0, "", ""))


class QueueTests(AutonomyCase):
    def test_a_queued_item_waits_with_the_reason_and_starts_after_recovered(self):
        ident = self.queue()
        self.on()
        tick = self.ok("tick")
        self.assertEqual(tick["started"], [])
        item = json.loads((self.state / "queue" / f"{ident}.json").read_text())
        self.assertIn(REFUSED, item["queue_reason"])
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.clear()
        self.assertEqual(self.ok("tick")["started"], [ident])
        self.assertTrue((self.state / "tasks" / ident).is_dir())
        self.assertFalse((self.state / "queue" / f"{ident}.json").exists())

    def test_a_queued_item_with_a_lane_waits_too(self):
        item, _ = self.lane_ok()
        queued = {"id": "coder-queued0002", "created": "2026-10-08T00:00:00Z", "role": "coder", "task": "t", "workdir": str(self.worktree),
                  "worker": "pi", "model": MODEL, "thinking": "medium", "route_info": {}, "deliverable": "", "parent": "", "after": [],
                  "context": [], "notes": [], "tests": "none", "lane": "alpha", "lane_step": "s2", "preamble_from": str(self.repo),
                  "queue_reason": "waiting"}
        (self.state / "queue").mkdir(exist_ok=True)
        (self.state / "queue" / "coder-queued0002.json").write_text(json.dumps(queued))
        self.on()
        self.assertEqual(self.ok("tick")["started"], [])
        self.assertIn(REFUSED, json.loads((self.state / "queue/coder-queued0002.json").read_text())["queue_reason"])


class OnRefusalTests(AutonomyCase):
    def writes_nothing(self):
        for name in ("session-autonomy.json", "session-recovery.json", "session-recovery", "session-work.json"):
            self.assertFalse((self.state / name).exists(), name)

    def test_on_needs_a_claude_orchestrator_main_and_writes_nothing_otherwise(self):
        cases = {"pi main": {"TP_MAIN_HARNESS": "pi", "TP_MODE": "orchestrator"},
                 "harness unset": {"TP_MODE": "orchestrator"},
                 "legacy mode": {"TP_MAIN_HARNESS": "claude", "TP_MODE": "legacy"},
                 "mode unset": {"TP_MAIN_HARNESS": "claude"},
                 "nothing set": {},
                 "a worker": {**CLAUDE, "TP_TASK_DIR": str(self.state / "tasks" / "x")},
                 "TP_TASK_DIR set but empty": {**CLAUDE, "TP_TASK_DIR": ""}}
        for name, env in cases.items():
            with self.subTest(name):
                run = self.run_cli("autonomy", "on", "--workspace", "demo", env=env)
                self.assertEqual(run.returncode, 1)
                self.assertIn("autonomy is not available under Pi: Pi has no verified compaction event, so recovery cannot be tracked; "
                              "run Claude main (tp-main with TP_MAIN_HARNESS=claude) for autonomous work", run.stderr)
                self.writes_nothing()
        self.assertEqual(self.dispatch_role().returncode, 0)

    def test_env_problem_treats_a_set_but_empty_task_dir_as_a_worker(self):
        self.assertIsNone(autonomy.env_problem(CLAUDE))
        self.assertIn("TP_TASK_DIR is set", autonomy.env_problem({**CLAUDE, "TP_TASK_DIR": ""}))

    def test_on_for_a_workspace_that_does_not_exist_writes_nothing(self):
        run = self.run_cli("autonomy", "on", "--workspace", "nope", env=CLAUDE)
        self.assertEqual(run.returncode, 1)
        self.writes_nothing()


class RecoveredTests(AutonomyCase):
    def test_evidence_is_required_and_a_blank_one_is_not_evidence(self):
        self.on()
        self.assertNotEqual(self.run_cli("work", "recovered", "--no-checkpoint").returncode, 0)
        run = self.run_cli("work", "recovered", "--evidence", "  ", "--no-checkpoint")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--evidence", run.stderr)
        self.assertTrue(self.marker()["pending"])

    def test_with_no_checkpoint_of_this_run_it_needs_no_checkpoint_to_say_so(self):
        self.on()
        for path in (self.store / "checkpoints" / "demo").glob("*"):
            path.unlink()
        run = self.recovered()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--no-checkpoint", run.stderr)
        self.assertTrue(self.marker()["pending"])
        done = self.ok("work", "recovered", "--evidence", "read list and the workspace", "--no-checkpoint")
        self.assertEqual((done["recovered"], done["changed"], done["workspaces"]), (True, True, ["demo"]))
        marker = self.marker()
        self.assertEqual((marker["pending"], marker["evidence"]), (False, "read list and the workspace"))
        self.assertEqual(self.ok("work", "recovered", "--evidence", "again", "--no-checkpoint")["changed"], False)

    def test_with_a_checkpoint_the_changed_tasks_must_each_be_accepted(self):
        (self.state / "live-window").write_text("")
        item, task, pane, pid = self.live_task(state="working", result=None)
        self.on()
        self.ok("work", "checkpoint", "demo")
        self.settle(task, state="waiting", result="RESULT_STATUS: complete\nDone.\n")
        self.assertIn(item["id"], self.ok("work", "recover", "demo"))  # the diff names it
        run = self.recovered()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(item["id"], run.stderr)
        self.assertIn(f"--accept {item['id']}", run.stderr)
        self.assertTrue(self.marker()["pending"])
        self.assertEqual(self.recovered("--accept", "other-task").returncode, 1)
        done = self.ok("work", "recovered", "--evidence", "checked the result", "--accept", item["id"])
        self.assertEqual(done["accepted"], [item["id"]])
        self.assertFalse(self.marker()["pending"])

    def test_with_a_checkpoint_and_no_change_it_clears_without_accept(self):
        self.on()
        self.ok("work", "checkpoint", "demo")
        done = self.ok("work", "recovered", "--evidence", "nothing moved")
        self.assertEqual((done["recovered"], done["accepted"]), (True, []))

    def test_nothing_attached_needs_no_checkpoint_to_say_so(self):
        self.ok("work", "compacted", "--reason", "precompact:auto")
        run = self.recovered()
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("--no-checkpoint", run.stderr)
        self.assertEqual(self.recovered("--no-checkpoint").returncode, 0)


class RecoveredScopeTests(AutonomyCase):
    """`work recovered` clears a run-wide marker, so it checks the workspaces this run is attached to, whatever id is named."""

    def changed_author_with_two_checkpointed_workspaces(self):
        (self.state / "live-window").write_text("")
        item, task, pane, pid = self.live_task(state="working", result=None)
        self.on()
        self.ok("work", "checkpoint", "demo")
        self.settle(task, state="waiting", result="RESULT_STATUS: complete\nDone.\n")
        self.ok("work", "new", "Other", "--id", "other")
        self.ok("work", "checkpoint", "other")  # taken after the author settled: nothing differs for it
        return item["id"]

    def test_an_unrelated_workspace_id_cannot_clear_the_run_marker(self):
        author = self.changed_author_with_two_checkpointed_workspaces()
        run = self.recovered("other")
        self.assertEqual(run.returncode, 1)
        self.assertIn("not a workspace of this run", run.stderr)
        self.assertTrue(self.marker()["pending"])
        run = self.recovered()  # and the attached workspace still refuses on its changed task
        self.assertEqual(run.returncode, 1)
        self.assertIn(author, run.stderr)
        self.assertTrue(self.marker()["pending"])

    def test_naming_the_attached_workspace_still_reconciles_it(self):
        author = self.changed_author_with_two_checkpointed_workspaces()
        run = self.recovered("demo")
        self.assertEqual(run.returncode, 1)
        self.assertIn(author, run.stderr)
        done = self.ok("work", "recovered", "demo", "--evidence", "checked it", "--accept", author)
        self.assertEqual((done["workspaces"], done["accepted"]), (["demo"], [author]))
        self.assertFalse(self.marker()["pending"])

    def test_every_attached_workspace_is_reconciled_whichever_one_is_named(self):
        self.on()
        self.ok("work", "new", "Other", "--id", "other")
        self.ok("work", "attach", "other")
        self.ok("work", "checkpoint", "demo")  # `other` has none, and it is attached
        run = self.recovered("demo")
        self.assertEqual(run.returncode, 1)
        self.assertIn("other: no valid checkpoint", run.stderr)
        self.assertTrue(self.marker()["pending"])
        self.assertEqual(self.recovered("demo", "--no-checkpoint").returncode, 0)

    def test_an_unreadable_task_list_does_not_let_no_checkpoint_skip_an_existing_checkpoint(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        self.on()
        self.ok("work", "checkpoint", "demo")
        tasks = self.state / "tasks"
        tasks.chmod(0)
        self.addCleanup(tasks.chmod, 0o755)
        for extra in ((), ("--no-checkpoint",)):
            with self.subTest(extra=extra):
                run = self.recovered(*extra)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("cannot be read", run.stderr)
                self.assertTrue(self.marker()["pending"])
        tasks.chmod(0o755)
        self.assertEqual(self.recovered().returncode, 0)

    def test_an_unreadable_task_list_refuses_no_checkpoint_when_this_run_has_no_checkpoint_either(self):
        # Review F1: with no checkpoint of this run, --no-checkpoint used to skip the unreadable live records and clear.
        if os.geteuid() == 0:
            self.skipTest("root reads any directory")
        self.on()
        self.assertIsNone(work_checkpoint.latest("demo", self.state.name)["valid"])
        tasks = self.state / "tasks"
        tasks.chmod(0)
        self.addCleanup(tasks.chmod, 0o755)
        for extra in ((), ("--no-checkpoint",)):
            with self.subTest(extra=extra):
                run = self.recovered(*extra)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("cannot be read", run.stderr)
                self.assertTrue(self.marker()["pending"])
        tasks.chmod(0o755)
        self.assertEqual(self.recovered("--no-checkpoint").returncode, 0)

    def test_recovery_problems_refuse_unreadable_records_with_or_without_a_checkpoint(self):
        import work_commands
        for no_checkpoint in (False, True):
            with self.subTest(no_checkpoint=no_checkpoint):
                args = mock.Mock(no_checkpoint=no_checkpoint)
                problems = work_commands._recovery_problems(args, ["demo"], self.state.name, None, PermissionError("tasks unreadable"),
                                                            set(), [])
                self.assertEqual(len(problems), 1)
                self.assertIn("cannot be read", problems[0])
        args = mock.Mock(no_checkpoint=True)
        self.assertEqual(work_commands._recovery_problems(args, [], "run", None, OSError("x"), set(), [])[0][:38],
                         "the live task records cannot be read (")

    def test_a_newer_checkpoint_of_another_run_does_not_hide_this_runs_checkpoint(self):
        (self.state / "live-window").write_text("")
        item, task, pane, pid = self.live_task(state="working", result=None)
        self.on()
        self.ok("work", "checkpoint", "demo")  # this run's: the author is working
        self.settle(task, state="waiting", result="RESULT_STATUS: complete\nDone.\n")
        state2 = self.root / "sessions" / "run-b"  # a run is named by its state directory, as bin/tp's .tmux-pi/sessions/<run>
        (state2 / "tasks").mkdir(parents=True)
        self.ok("work", "checkpoint", "demo", env={"TP_STATE_DIR": str(state2)})  # newer, another run's, without the author
        self.assertIn("another run's", self.ok("work", "recover", "demo"))
        for extra in ((), ("--no-checkpoint",)):
            with self.subTest(extra=extra):
                run = self.recovered(*extra)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn(f"--accept {item['id']}", run.stderr)
                self.assertTrue(self.marker()["pending"])
        self.assertEqual(self.recovered("--accept", item["id"]).returncode, 0)
        self.assertFalse(self.marker()["pending"])


class RecoveredRaceTests(AutonomyCase):
    """Interleavings, made deterministic by running the competing step from inside the step it races with."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {key: self.env[key] for key in ("TP_WORK_DIR", "TP_WORK_CONFIG")})
        patch.start()
        self.addCleanup(patch.stop)

    def recovered_with(self, during):
        import work_commands
        args = mock.Mock(id=None, evidence="checked the live records", accept="", no_checkpoint=True)

        def tasks():
            during()
            return []
        return work_commands._recovered(args, self.state, tasks)

    @staticmethod
    def long_wait():
        """Every look at the clock is 6 s after the last: any lock wait gives up at once, as after more than the old 5 s."""
        import itertools
        return mock.patch("time.monotonic", side_effect=itertools.count(0.0, 6.0))

    def test_a_compaction_during_the_check_is_not_erased_by_the_clear(self):
        self.on()
        for start_pending in (True, False):  # pending from `autonomy on`, then clear (each round ends cleared)
            with self.subTest(start_pending=start_pending):
                self.assertEqual(self.marker()["pending"], start_pending)
                value = self.recovered_with(lambda: work_checkpoint.compacted(self.state, "precompact:auto"))
                self.assertTrue(value["recovered"])
                self.assertTrue(self.marker()["pending"])
                self.assertEqual(self.marker()["reason"], "precompact:auto")
                self.assertIn(REFUSED, autonomy.gate(self.state, role="coder"))
                self.clear()

    def test_an_attach_after_the_workspaces_were_selected_stays_pending(self):
        # Review F1: the workspaces were selected before the generation snapshot, so an attach landing in between was cleared unchecked.
        import work_commands
        self.on()
        self.ok("work", "new", "Other", "--id", "other")
        real = work_commands._recovery_targets
        seen = {}

        def select_then_attach(args, session_dir):
            seen["selected"] = real(args, session_dir)
            self.ok("work", "attach", "other")  # publishes `other` and arms, after the selection and before the check compares
            return seen["selected"]

        with mock.patch.object(work_commands, "_recovery_targets", select_then_attach):
            value = self.recovered_with(lambda: None)
        self.assertNotIn("other", seen["selected"])
        self.assertEqual(value["workspaces"], seen["selected"])
        self.assertTrue(self.marker()["pending"])
        self.assertEqual(self.marker()["reason"], "attach")
        self.refuses(self.dispatch_role())

    def test_a_compaction_while_recovered_holds_the_lock_past_the_old_timeout_is_kept(self):
        # Review F3: the compaction used to wait for this lock and give up after 5 s, and the clear then erased it.
        self.on()
        seen = {}

        def compaction_after_a_long_hold():
            with self.long_wait():
                with self.assertRaisesRegex(work_checkpoint.WorkError, "locked"):  # the lock really is held, and waiting gives up
                    with work_checkpoint.state_lock(self.state):
                        pass
                seen["armed"] = work_checkpoint.compacted(self.state, "precompact:auto")  # takes no lock: never waits, never fails

        self.recovered_with(compaction_after_a_long_hold)
        self.assertGreater(seen["armed"]["armed"], self.marker()["cleared_generation"])
        self.assertTrue(self.marker()["pending"])
        self.assertEqual(self.marker()["reason"], "precompact:auto")
        self.refuses(self.dispatch_role())

    def test_autonomy_on_cannot_run_inside_recovered_and_runs_after_it(self):
        self.ok("work", "attach", "demo")  # pending, autonomy off
        before = self.marker()["generation"]
        seen = {}

        def enable():
            with self.long_wait(), self.assertRaisesRegex(work_checkpoint.WorkError, "locked") as caught:
                autonomy.enable(self.state, CLAUDE, "demo", "race")
            seen["refused"] = str(caught.exception)

        self.recovered_with(enable)
        self.assertIn("locked", seen["refused"])
        self.assertFalse((self.state / autonomy.FILE).exists())  # refused inside the clear: nothing written, nothing armed
        self.assertEqual((self.marker()["generation"], self.marker()["pending"]), (before, False))
        autonomy.enable(self.state, CLAUDE, "demo", "after")
        self.assertTrue(self.marker()["pending"])
        self.assertIn(REFUSED, autonomy.gate(self.state, role="coder"))

    def test_recovered_cannot_clear_between_the_arm_and_the_record_of_autonomy_on(self):
        # Review F2: a `work recovered` landing after `autonomy on` armed and before it wrote its record left autonomy on and clear.
        self.ok("work", "attach", "demo")
        real = autonomy.work.atomic_write
        seen = {}

        def write(path, text, **kw):
            if Path(path).name == autonomy.FILE:
                with self.long_wait(), self.assertRaisesRegex(work_checkpoint.WorkError, "locked") as caught:
                    self.recovered_with(lambda: None)
                seen["refused"] = str(caught.exception)
            real(path, text, **kw)

        with mock.patch.object(autonomy.work, "atomic_write", write):
            autonomy.enable(self.state, CLAUDE, "demo", "race")
        self.assertIn("locked", seen["refused"])
        self.assertTrue(self.marker()["pending"])
        self.assertFalse((self.state / work_checkpoint.RECOVERY_FILE).exists())  # no clear was written
        self.assertIn(REFUSED, autonomy.gate(self.state, role="coder"))
        self.recovered_with(lambda: None)  # once on is done, recovery clears as usual
        self.assertIsNone(autonomy.gate(self.state, role="coder"))

    def test_missing_or_unreadable_generation_state_with_autonomy_on_is_pending(self):
        self.on()
        self.clear()
        self.assertIsNone(autonomy.gate(self.state, role="coder"))
        folder = self.state / work_checkpoint.RECOVERY_DIR
        if os.geteuid() != 0:
            folder.chmod(0)
            self.addCleanup(folder.chmod, 0o755)
            self.assertIn("unreadable", autonomy.gate(self.state, role="coder"))
            folder.chmod(0o755)
        import shutil
        shutil.rmtree(folder)
        refusal = autonomy.gate(self.state, role="coder")
        self.assertIn(REFUSED, refusal)
        self.assertIn("missing", refusal)
        self.recovered_with(lambda: None)  # reconciled: the directory comes back empty, which is clear
        self.assertIsNone(autonomy.gate(self.state, role="coder"))

    def test_autonomy_on_arms_the_marker_before_its_record_can_admit_a_start(self):
        seen = []
        real = autonomy.work.atomic_write

        def write(path, text, **kw):
            if Path(path).name == autonomy.FILE:
                seen.append(("before", autonomy.gate(self.state, role="coder")))
            real(path, text, **kw)
            if Path(path).name == autonomy.FILE:
                seen.append(("after", autonomy.gate(self.state, role="coder")))

        with mock.patch.object(autonomy.work, "atomic_write", write):
            autonomy.enable(self.state, CLAUDE, "demo", "race")
        self.assertEqual([moment for moment, _ in seen], ["before", "after"])
        self.assertIsNone(seen[0][1])  # autonomy not on yet: nothing changed for anyone
        self.assertIsNotNone(seen[1][1], "a start was admitted with autonomy on and recovery not yet armed")
        self.assertIn(REFUSED, seen[1][1])  # the instant the record exists, the marker is already pending


class InvestigateGateTests(AutonomyCase):
    def test_an_investigation_is_refused_before_any_file_or_worktree_is_written(self):
        author, _ = self.finished_author()
        self.on()
        before = git(self.repo, "worktree", "list")
        run = self.run_cli("dispatch", "--investigate-failure", author, "--failing-test", "test_x: python3 -m unittest", "--base", "HEAD",
                           "--worker", "pi", "--model", MODEL, "--thinking", "medium")
        self.refuses(run)
        self.assertFalse((self.state / "investigations").exists())
        self.assertEqual(git(self.repo, "worktree", "list"), before)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 1)  # only the author


class ReviewAdmissionTests(AutonomyCase):
    def review(self, author, *extra, workdir=None, preamble="code-reviewer", review_of=True):
        args = ["--name", f"review-{author}", "--preamble-role", preamble]
        if review_of:
            args += ["--review-of", author]
        return self.spawn(*args, *extra, "--", "Review it", workdir=workdir)

    def test_a_review_of_a_finished_task_is_admitted_while_recovery_is_pending(self):
        author, _ = self.finished_author()
        self.on()
        run = self.review(author)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 2)

    def test_a_review_of_a_lane_task_is_admitted_with_the_authors_lane_and_step(self):
        item, task = self.lane_ok()
        self.settle(task)
        self.on()
        run = self.review(item["id"], "--lane", "alpha", "--lane-step", "s1", "--preamble-from", str(self.repo), workdir=self.worktree)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_what_is_not_a_review_of_a_finished_task_is_refused(self):
        author, task = self.finished_author()
        item, lane_task = self.lane_ok()
        self.settle(lane_task)
        unfinished = self.dispatch_ok()[0]["id"]  # a task with no result yet
        self.on()
        cases = {
            "preamble-role without --review-of": self.review(author, review_of=False),
            "a task that does not exist": self.review("coder-nothere0001"),
            "a task without a result": self.review(unfinished),
            "a wrong preamble role": self.review(author, preamble="coder"),
            "a --role beside --review-of": self.review(author, "--role", "coder"),
            "a --deliverable beside --review-of": self.review(author, "--deliverable", "Report"),
            "a lane the author does not have": self.review(author, "--lane", "alpha", "--lane-step", "s1", workdir=self.worktree),
            "no lane for a lane author": self.review(item["id"]),
            "another step": self.review(item["id"], "--lane", "alpha", "--lane-step", "s9", workdir=self.worktree),
        }
        for name, run in cases.items():
            with self.subTest(name):
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertIn(REFUSED, run.stderr)
        self.assertIn("[not admitted as a review: coder-%s has no result yet]" % unfinished.split("-")[1], cases["a task without a result"].stderr)
        self.assertIn("is not a task of this run", cases["a task that does not exist"].stderr)
        self.assertIn("is not the lane of", cases["no lane for a lane author"].stderr)
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 3)  # only the authors made earlier

    def test_a_task_of_another_run_and_a_malformed_id_are_not_admitted(self):
        state2 = self.root / "session2" / ".tmux-pi"
        (state2 / "tasks" / "coder-elsewhere01").mkdir(parents=True)
        (state2 / "tasks" / "coder-elsewhere01" / "meta.json").write_text("{}")
        (state2 / "tasks" / "coder-elsewhere01" / "result.md").write_text("done\n")
        self.on()
        self.refuses(self.review("coder-elsewhere01"))
        run = self.review("../session2/.tmux-pi/tasks/coder-elsewhere01")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("bad --review-of id", run.stderr)

    def test_a_review_may_not_be_used_to_start_other_work_through_a_finished_task(self):
        author, _ = self.finished_author()
        self.on()
        self.assertEqual(self.review(author).returncode, 0)
        self.refuses(self.dispatch_role())  # the admitted review opened nothing else

    def test_review_of_changes_nothing_while_no_recovery_is_pending(self):
        author, _ = self.finished_author()
        self.on()
        self.clear()
        self.assertEqual(self.review("coder-nothere0001").returncode, 0)  # not pending: the gate does not look at --review-of

    def test_the_real_tp_review_of_a_finished_task_is_admitted_while_pending_and_other_starts_are_not(self):
        author, _ = self.finished_author()
        self.on()
        self.refuses(self.dispatch_role())
        self.refuses(self.spawn("--", "plain task"))
        run = self.run_cli("--force", author, script="tp-review")
        self.assertEqual(run.returncode, 0, run.stderr)
        review = run.stdout.strip()
        self.assertTrue(review.startswith(f"review-{author}"), review)
        meta = json.loads((self.state / "tasks" / review / "meta.json").read_text())
        self.assertEqual(meta.get("review_of", author), author)
        self.assertTrue((self.state / "tasks" / review / "review-of").is_file())
        self.assertTrue(self.marker()["pending"])  # the review did not clear anything
        self.refuses(self.dispatch_role())  # and it opened no other door
        self.assertEqual(len(list((self.state / "tasks").iterdir())), 2)

    def test_tp_review_passes_review_of_to_tp_spawn(self):
        self.assertIn('--review-of "$id"', (ROOT / "bin" / "tp-review").read_text())


class IndexTests(AutonomyCase):
    def block(self):
        self.ok("index")
        value = json.loads((self.state / "session-index.json").read_text())
        return value["work"]

    def test_the_index_says_what_is_blocked_and_since_when_and_only_while_autonomy_is_on(self):
        self.assertNotIn("autonomy", self.block())
        self.on()
        entry = self.block()["autonomy"]
        self.assertEqual((entry["on"], entry["blocked"], entry["workspace"]), (True, True, "demo"))
        self.assertEqual(entry["blocked_since"], self.marker()["since"])
        self.assertIn("dispatch blocked since", entry["line"])
        self.assertIn("tp-agent work recovered", entry["line"])
        self.clear()
        entry = self.block()["autonomy"]
        self.assertEqual((entry["blocked"], entry["blocked_since"]), (False, None))
        self.assertIn("recovery clear", entry["line"])
        self.ok("autonomy", "off")
        self.assertNotIn("autonomy", self.block())


if __name__ == "__main__":
    unittest.main()
