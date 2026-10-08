"""The model router through its commands: `tp-route`, `tp-agent dispatch / retry --escalate / review`, and review
planning (who reviews, and whether). Stub `claude` and `pi` stand in for the routers; tmux is faked as in
test_orchestration. Nothing here may contact the Laya endpoint (127.0.0.1:11436)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import route_fixture as fx
from route_fixture import ASTRA, HAIKU, LUNA, OPUS, QWEN, SOL, SONNET, reply

import model_router  # noqa: E402

ROOT = fx.ROOT
TASK = "Add pagination to the /orders endpoint with tests. Do not edit the billing module."


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        fake = self.root / "fakebin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('''#!/bin/sh
case "$1" in
 has-session) exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%fake\\n' ;;
 *) exit 0 ;;
esac
''')
        tmux.chmod(0o755)
        self.stubs = fx.Stubs(self.root, {"claude": [{"text": reply(3, "medium", "optional", None, "ordinary")}]})
        self.addCleanup(self.stubs.close)
        guard, self.hits = fx.laya_guard(self.root)
        self.addCleanup(self.assert_no_laya)  # also covers the real LM Studio port
        self.env = self.stubs.env(TP_STATE_DIR=str(self.state), TP_SESSION="fixture", **guard)
        self.env["PATH"] = str(fake) + os.pathsep + self.env["PATH"]

    def run_cmd(self, script, *args, stdin=None, env=None):
        return subprocess.run([sys.executable, str(ROOT / "bin" / script), *args], input=stdin, text=True, capture_output=True,
                              env=env or self.env, timeout=60)

    def tp_route(self, *args, stdin=TASK):
        return self.run_cmd("tp-route", "--no-log", *args, stdin=stdin)

    def agent(self, *args, check=True):
        run = self.run_cmd("tp-agent", *args)
        if check:
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)
        return run

    def dispatch(self, *extra, role="coder", task=TASK, **kw):
        return self.agent("dispatch", "--role", role, "--task", task, "--workdir", str(self.project), "--deliverable",
                          "Report paths and checks", *extra, **kw)

    def meta(self, item):
        return json.loads((self.state / "tasks" / item["id"] / "meta.json").read_text())

    def selections(self):
        path = self.state / "evals" / model_router.SELECTIONS
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_no_laya(self):
        self.assertFalse(self.hits.exists(), "the Laya endpoint was contacted: " + (self.hits.read_text() if self.hits.exists() else ""))


class TpRouteTests(Base):
    def test_plain_route_prints_a_valid_pair_and_more(self):
        run = self.tp_route("--role", "coder")
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual((out["worker"], out["model"], out["thinking"]), ("claude", SONNET, "medium"))
        self.assertEqual((out["level"], out["decided_by"], out["source"]), ("mid", HAIKU, "router"))
        self.assertEqual(list(out)[:3], ["worker", "model", "thinking"])
        self.assert_no_laya()

    def test_payload_form_from_the_old_dispatch_path_is_understood(self):
        self.stubs.set({"claude": [{"text": reply(3, "high")}]})
        payload = f"Worker role: vulnerability-reviewer\nAssignment: {TASK}\nExpected deliverable: Findings\nSelected context paths: a.py, b.py"
        out = json.loads(self.tp_route(stdin=payload).stdout)
        self.assertEqual((out["level"], out["effort"]), ("mid", "high"))
        user = self.stubs.calls("claude")[0]["argv"][-1]
        self.assertIn("Role: vulnerability-reviewer", user)
        self.assertIn("Deliverable: Findings", user)
        self.assertNotIn("Selected context paths", user)

    def test_every_router_down_still_prints_sonnet_medium_with_the_reason(self):
        self.stubs.set({"claude": [{"exit": 1, "text": "down"}], "pi/" + LUNA: [{"exit": 1, "text": "down"}], "chat": [{"status": 500}]})
        run = self.tp_route()
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual((out["model"], out["thinking"], out["source"]), (SONNET, "medium", "router_failed_default"))
        self.assertIn("down", out["reason"])

    def test_stop_and_ask_exits_2(self):
        run = self.tp_route("--local-only")
        self.assertEqual(run.returncode, 2)
        self.assertIn("stop and ask", run.stderr)
        self.assertEqual(run.stdout, "")

    def test_bad_requests_exit_1(self):
        for args in (("--level", "strong"), ("--model", SONNET, "--level", "top"), ("--model", SONNET, "--worker", "pi")):
            with self.subTest(args=args):
                run = self.tp_route(*args)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("tp-route:", run.stderr)

    def test_explicit_level_and_approval_flags(self):
        out = json.loads(self.tp_route("--level", "top").stdout)
        self.assertEqual((out["model"], out["source"]), (OPUS, "explicit_level"))
        out = json.loads(self.tp_route("--model", SOL, "--thinking", "max").stdout)
        self.assertEqual(out["thinking"], "high")
        out = json.loads(self.tp_route("--model", SOL, "--thinking", "max", "--approve-effort").stdout)
        self.assertEqual(out["thinking"], "max")
        self.assertEqual(self.stubs.calls(), [])

    def test_provider_health_in_the_state_dir_moves_the_router_and_the_choice(self):
        (self.state / "provider-health.json").write_text(json.dumps({"anthropic": {"status": "exhausted"}}))
        self.stubs.set({"pi/" + LUNA: [{"text": reply(3, "medium")}]})
        out = json.loads(self.tp_route().stdout)
        self.assertEqual((out["decided_by"], out["model"]), (LUNA, SOL))
        self.assertEqual([c["key"] for c in self.stubs.calls()], ["pi/" + LUNA])

    def test_stop_and_ask_reports_the_resolvers_reason_not_the_routers(self):
        (self.state / "provider-health.json").write_text(json.dumps({"anthropic": {"status": "exhausted"}, "openai": {"status": "exhausted"}}))
        self.stubs.set({"chat": [{"text": reply(3, "medium", "optional", None, "ROUTERS OWN WORDS")}]})
        run = self.tp_route()
        self.assertEqual(run.returncode, 2)
        self.assertIn("no available model at any level", run.stderr)
        self.assertNotIn("ROUTERS OWN WORDS", run.stderr)

    def test_check_validates_config_and_catalog(self):
        run = self.run_cmd("tp-route", "--check")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("catalog valid: 5 levels (4 shown)", run.stdout)

    def test_selection_record_for_a_routed_choice(self):
        run = self.run_cmd("tp-route", "--role", "coder", stdin=TASK, env={**self.env, "TP_ROUTING_EVAL_DIR": str(self.root / "ev")})
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = [json.loads(line) for line in (self.root / "ev" / model_router.SELECTIONS).read_text().splitlines()]
        row = rows[0]
        self.assertEqual((row["role"], row["level"], row["effort"], row["decided_by"], row["source"]), ("coder", "mid", "medium", HAIKU, "router"))
        self.assertEqual(row["resolved"], {"worker": "claude", "model": SONNET, "provider": "anthropic", "effort": "medium"})
        self.assertEqual(len(row["card_sha256"]), 64)
        self.assertEqual(row["substitution"], [])
        self.assertNotIn(TASK, json.dumps(row))  # hashes, never the text
        self.assert_no_laya()

    def test_local_rule_from_local_config_still_applies(self):
        local = self.root / "local.json"
        local.write_text(json.dumps({"rules": [{"pattern": "pagination", "from": [SONNET],
                                                "to": {"model": SOL, "thinking": "medium"}}]}))
        out = json.loads(self.run_cmd("tp-route", "--no-log", "--role", "coder", stdin=TASK, env={**self.env, "TP_LOCAL_CONFIG": str(local)}).stdout)
        self.assertEqual((out["worker"], out["model"]), ("pi", SOL))
        self.assertIn(f"local rule redirected {SONNET} to {SOL}", out["steps"])
        # with --worker claude the rule may not move the work to another harness
        out = json.loads(self.run_cmd("tp-route", "--no-log", "--worker", "claude", stdin=TASK, env={**self.env, "TP_LOCAL_CONFIG": str(local)}).stdout)
        self.assertEqual(out["model"], SONNET)
        self.assertTrue(any("dropped" in step and "--worker claude" in step for step in out["steps"]), out["steps"])

    def test_a_task_that_names_guard_code_is_held_at_mid_and_high_effort(self):
        import model_router
        self.assertEqual(model_router.task_paths("Fix lib/sensitive_read.py; see note.", "tests in tests/test_x.py", ["/r/bin/tp-redact:1-40"]),
                         ["/r/bin/tp-redact", "lib/sensitive_read.py", "tests/test_x.py"])
        self.stubs.set({"claude": [{"text": reply(2, "low", "none", "small_edit", "small")}]})
        out = json.loads(self.tp_route("--role", "coder", stdin="Close one hole in lib/sensitive_read.py and run its tests.").stdout)
        self.assertEqual((out["level"], out["effort"], out["model"]), ("mid", "high", SONNET))
        self.assertTrue(any(step.startswith("protected path lib/sensitive_read.py") for step in out["steps"]), out["steps"])
        # the same small edit elsewhere is held at mid too: the coder floor has no waiver
        self.stubs.set({"claude": [{"text": reply(2, "low", "none", "small_edit", "small")}]})
        out = json.loads(self.tp_route("--role", "coder", stdin="Fix the usage text in bin/tp-list and run its test.").stdout)
        self.assertEqual((out["level"], out["model"]), ("mid", SONNET))

    def test_explicit_model_is_refused_when_the_catalog_cannot_be_read(self):
        for model in ("madeup/nonexistent-model", SONNET):
            with self.subTest(model=model):
                run = self.tp_route("--catalog", str(self.root / "no-such-catalog.json"), "--model", model)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertIn("cannot be validated", run.stderr)
                self.assertEqual(run.stdout.strip(), "")


class DispatchTests(Base):
    def test_routed_dispatch_records_the_decision_and_never_calls_laya(self):
        item = self.dispatch()
        meta = self.meta(item)
        self.assertEqual((meta["worker"], meta["model"], meta["thinking"]), ("claude", SONNET, "medium"))
        info = meta["route_info"]
        self.assertEqual((info["source"], info["level"], info["decided_by"], info["reason"]), ("router", "mid", HAIKU, "ordinary"))
        self.assertEqual((info["review"], info["router_review"]), ("optional", "optional"))
        for key in ("catalog_version", "prompt_sha256", "card_sha256", "selection_id", "steps", "approvals"):
            self.assertIn(key, info)
        rows = self.selections()
        self.assertEqual([r["selection_id"] for r in rows], [info["selection_id"]])
        self.assert_no_laya()

    def test_worker_pi_is_a_real_constraint(self):
        item = self.dispatch("--worker", "pi")
        meta = self.meta(item)
        self.assertEqual((meta["worker"], meta["model"]), ("pi", SOL))
        self.assertEqual(meta["route_info"]["source"], "router")
        self.assertEqual(self.selections()[0]["constraints"]["worker"], "pi")
        self.assert_no_laya()

    def test_explicit_choices_get_a_selection_record_too(self):
        self.dispatch("--model", OPUS, "--thinking", "high")
        self.dispatch("--level", "top", "--worker", "pi")
        rows = self.selections()
        self.assertEqual([(r["source"], r["decided_by"], r["resolved"]["model"]) for r in rows],
                         [("explicit", "explicit", OPUS), ("explicit_level", "explicit", ASTRA)])
        self.assertEqual(self.stubs.calls(), [])
        for row in rows:
            for key in ("role", "card_sha256", "level", "effort", "decided_by", "reason", "resolved", "substitution"):
                self.assertIn(key, row)
        self.assert_no_laya()

    def test_effort_above_high_is_cut_unless_approved(self):
        item = self.dispatch("--model", SOL, "--thinking", "xhigh")
        self.assertEqual(self.meta(item)["thinking"], "high")
        item = self.dispatch("--model", SOL, "--thinking", "xhigh", "--approve-effort")
        self.assertEqual((self.meta(item)["thinking"], self.meta(item)["route_info"]["approvals"]), ("xhigh", {"effort_above_high": True}))

    def test_the_pair_is_valid_even_when_every_router_fails(self):
        self.stubs.set({})
        item = self.dispatch()
        meta = self.meta(item)
        self.assertEqual((meta["worker"], meta["model"], meta["thinking"]), ("claude", SONNET, "medium"))
        self.assertEqual(meta["route_info"]["source"], "router_failed_default")
        self.assertIn("router unavailable", meta["route_info"]["reason"])

    def test_stop_and_ask_is_an_error_not_a_guess(self):
        run = self.agent("dispatch", "--role", "coder", "--task", TASK, "--local-only", "--workdir", str(self.project), check=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("tp-agent:", run.stderr)
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(self.selections()[-1]["status"], "stop_and_ask")

    def test_level_hidden_is_rejected(self):
        run = self.agent("dispatch", "--role", "coder", "--task", TASK, "--level", "strong", "--workdir", str(self.project), check=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("not offered", run.stderr)

    def test_the_router_may_choose_the_top_level_but_a_tester_is_capped(self):
        self.stubs.set({"claude": [{"text": reply(4, "high", "required", None, "architecture")}]})
        self.assertEqual(self.meta(self.dispatch(role="architect"))["model"], OPUS)
        self.assertEqual(self.meta(self.dispatch(role="tester"))["model"], SONNET)


class EscalationTests(Base):
    def fail(self, item):
        (self.state / "tasks" / item["id"] / "exit").write_text("1")

    def retry(self, item, *extra, check=True):
        return self.agent("retry", item["id"], "--reason", "failed", *extra, check=check)

    def test_effort_steps_up_before_the_level_after_two_failures_then_stops(self):
        first = self.dispatch("--model", SONNET, "--thinking", "medium")
        self.fail(first)
        early = self.retry(first, "--escalate", check=False)
        self.assertEqual(early.returncode, 1)
        self.assertIn("needs 2 failed attempts", early.stderr)
        second = self.retry(first, "--model", SONNET, "--thinking", "medium")
        self.fail(second)
        third = self.retry(second, "--escalate")
        self.assertEqual((self.meta(third)["model"], self.meta(third)["thinking"]), (SONNET, "high"))
        self.assertEqual(self.meta(third)["route_info"]["source"], "escalation_effort_up")
        self.fail(third)
        fourth = self.retry(third, "--escalate")
        self.assertEqual((self.meta(fourth)["model"], self.meta(fourth)["thinking"]), (OPUS, "high"))
        self.assertEqual(self.meta(fourth)["route_info"]["source"], "escalation_level_up")
        self.fail(fourth)
        stop = self.retry(fourth, "--escalate", check=False)
        self.assertEqual(stop.returncode, 1)
        self.assertIn("approval needed", stop.stderr)
        self.assertEqual([r["source"] for r in self.selections()][-3:], ["escalation_effort_up", "escalation_level_up", "stop_and_ask"])
        self.assert_no_laya()

    def test_escalate_cannot_be_combined_with_an_explicit_model(self):
        first = self.dispatch("--model", SONNET)
        self.fail(first)
        run = self.retry(first, "--escalate", "--model", OPUS, check=False)
        self.assertEqual(run.returncode, 1)

    def test_request_changes_reviews_count_as_failures(self):
        first = self.dispatch("--model", SONNET, "--thinking", "medium")
        task = self.state / "tasks" / first["id"]
        (task / "exit").write_text("0")
        (task / "result.md").write_text("RESULT_STATUS: complete\n")
        for n in (1, 2):
            review = self.state / "tasks" / f"review-{n}"
            review.mkdir()
            (review / "meta.json").write_text(json.dumps({"id": f"review-{n}"}))
            (review / "result.md").write_text("REQUEST_CHANGES\nRESULT_STATUS: complete\nfix it")
        # only one review is linked per attempt, so link the first attempt to one and retry once more
        meta = json.loads((task / "meta.json").read_text())
        (task / "meta.json").write_text(json.dumps(dict(meta, review="review-1")))
        second = self.retry(first, "--model", SONNET, "--thinking", "medium")
        task2 = self.state / "tasks" / second["id"]
        (task2 / "exit").write_text("0")
        (task2 / "result.md").write_text("RESULT_STATUS: complete\n")
        (task2 / "meta.json").write_text(json.dumps(dict(json.loads((task2 / "meta.json").read_text()), review="review-2")))
        third = self.retry(second, "--escalate")
        self.assertEqual(self.meta(third)["thinking"], "high")


class LocalOnlyCommandTests(Base):
    """Local-only is a hard constraint through every command: an explicit non-local choice is rejected and an
    escalation never leaves the local models."""
    CLOUD_CHOICES = (("--model", SONNET, "--worker", "claude"), ("--model", SOL), ("--model", HAIKU), ("--worker", "claude"),
                     ("--level", "top"), ("--level", "mid"), ("--model", OPUS, "--thinking", "high"))

    def test_tp_route_rejects_an_explicit_choice_that_is_not_local(self):
        for args in self.CLOUD_CHOICES:
            with self.subTest(args=args):
                run = self.tp_route("--local-only", *args)
                self.assertEqual((run.returncode, run.stdout), (1, ""), run.stderr)
                self.assertIn("local", run.stderr)
        self.assertEqual(self.stubs.calls(), [])

    def test_dispatch_rejects_an_explicit_choice_that_is_not_local_and_starts_nothing(self):
        for args in self.CLOUD_CHOICES:
            with self.subTest(args=args):
                run = self.agent("dispatch", "--role", "coder", "--task", TASK, "--local-only", "--workdir", str(self.project), *args, check=False)
                self.assertEqual(run.returncode, 1, run.stdout)
                self.assertIn("local", run.stderr)
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(self.stubs.calls(), [])

    def test_an_explicit_local_choice_is_accepted(self):
        out = json.loads(self.tp_route("--local-only", "--model", QWEN).stdout)
        self.assertEqual((out["worker"], out["model"], out["local_only"], out["source"]), ("pi", QWEN, True, "explicit"))
        out = json.loads(self.tp_route("--local-only", "--worker", "pi", "--level", "local").stdout)
        self.assertEqual((out["model"], out["local_only"]), (QWEN, True))

    def local_task(self, *extra):
        # an explicit --level overrides the researcher's floor; the router's pick and escalation would not
        return self.dispatch("--local-only", "--level", "local", *extra, role="researcher")

    def fail(self, item):
        (self.state / "tasks" / item["id"] / "exit").write_text("1")

    def test_escalation_stays_local_or_stops_and_never_picks_a_cloud_model(self):
        first = self.local_task()
        self.assertEqual(self.meta(first)["model"], QWEN)
        self.fail(first)
        second = self.agent("retry", first["id"], "--reason", "again", "--level", "local")  # a plain retry would be routed, and held to the floor
        self.assertTrue(self.meta(second)["route_info"]["local_only"])  # the constraint survives a plain retry
        self.fail(second)
        before = sorted(p.name for p in (self.state / "tasks").iterdir())
        run = self.agent("retry", second["id"], "--reason", "failed", "--escalate", check=False)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("local-only", run.stderr)
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)  # nothing was started
        row = self.selections()[-1]
        self.assertEqual((row["kind"], row["status"], row["constraints"]["local_only"]), ("escalation", "stop_and_ask", True))
        self.assertEqual(self.stubs.calls("claude") + self.stubs.calls("pi"), [])
        # naming a cloud worker does not get around it either
        run = self.agent("retry", second["id"], "--reason", "failed", "--escalate", "--worker", "claude", check=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("local-only", run.stderr)

    def test_escalation_is_local_only_when_only_the_recorded_route_says_so(self):
        # the flag is read from the failed attempt's route, so a retry that forgets --local-only cannot drop it
        first = self.local_task()
        self.fail(first)
        second = self.agent("retry", first["id"], "--reason", "again", "--level", "local")  # a plain retry would be routed, and held to the floor
        self.fail(second)
        sys.path.insert(0, str(ROOT / "lib"))
        import router_catalog
        meta = json.loads((self.state / "tasks" / second["id"] / "meta.json").read_text())
        self.assertTrue(meta["route_info"]["local_only"])
        with self.assertRaises(model_router.RouteStop):
            model_router.escalate_route(meta, router_catalog.load_catalog(), role="researcher", base=self.state, log=False)


class ExplicitValidationCommandTests(Base):
    def test_tp_route_and_dispatch_reject_invalid_explicit_choices(self):
        big = self.project / "big.txt"
        big.write_text("x" * 40000)  # about 10000 tokens: over the 32k Slotstream model's packet limit
        slot = "slotstream/qwen3.8-flash-next:4bit"
        cases = [(("--model", "madeup/nonexistent-model"), "not in the model catalog"),
                 (("--model", "openai-codex/gpt-6.1-sol"), "not offered"),
                 (("--model", "claude-opus-9"), "not in the model catalog"),
                 (("--model", HAIKU, "--thinking", "off"), "does not allow effort"),
                 (("--model", QWEN, "--thinking", "high"), "does not allow effort"),
                 (("--model", SONNET, "--thinking", "nonsense"), "not one of"),
                 (("--model", slot, "--workdir", str(self.project), "--context", "big.txt"), "over")]
        for args, needle in cases:
            with self.subTest(args=args):
                run = self.tp_route(*args)
                self.assertEqual((run.returncode, run.stdout), (1, ""), run.stderr)
                self.assertIn(needle, run.stderr)
                run = self.agent("dispatch", "--role", "coder", "--task", TASK, "--workdir", str(self.project),
                                 *[a for a in args if a != "--workdir" and a != str(self.project)], check=False) if "--context" not in args else None
                if run:
                    self.assertEqual(run.returncode, 1, run.stdout)
        self.assertEqual(list((self.state / "tasks").iterdir()), [])
        self.assertEqual(self.stubs.calls(), [])

    def test_a_provider_marked_out_rejects_its_explicit_models(self):
        (self.state / "provider-health.json").write_text(json.dumps({"anthropic": {"status": "exhausted"}}))
        run = self.tp_route("--model", SONNET)
        self.assertEqual(run.returncode, 1)
        self.assertIn("marked unavailable", run.stderr)
        self.assertEqual(json.loads(self.tp_route("--model", SOL).stdout)["model"], SOL)

    def test_valid_explicit_pairs_still_work_and_map_the_default_effort(self):
        out = json.loads(self.tp_route("--model", QWEN).stdout)
        self.assertEqual((out["model"], out["thinking"]), (QWEN, "off"))
        out = json.loads(self.tp_route("--worker", "pi", "--model", SOL, "--thinking", "low").stdout)
        self.assertEqual((out["worker"], out["thinking"]), ("pi", "low"))


class CardHashTests(Base):
    def test_every_selection_record_carries_the_card_hash(self):
        run = self.run_cmd("tp-route", "--role", "coder", "--model", OPUS, "--thinking", "high", stdin=TASK)
        self.assertEqual(run.returncode, 0, run.stderr)
        first = self.dispatch("--model", SONNET, "--thinking", "medium")
        self.dispatch("--level", "top")
        (self.state / "tasks" / first["id"] / "exit").write_text("1")
        second = self.agent("retry", first["id"], "--reason", "again", "--model", SONNET, "--thinking", "medium")
        (self.state / "tasks" / second["id"] / "exit").write_text("1")
        self.agent("retry", second["id"], "--reason", "failed", "--escalate")
        rows = self.selections()
        self.assertEqual([r["source"] for r in rows if r["kind"] == "escalation"], ["escalation_effort_up"])
        self.assertGreaterEqual(len(rows), 5)
        for row in rows:
            self.assertRegex(row["card_sha256"] or "", r"^[0-9a-f]{64}$", row["source"])
        # the explicit dispatch, its retry and the escalation of the same task share one card
        self.assertEqual(rows[1]["card_sha256"], rows[3]["card_sha256"])
        self.assertEqual(rows[1]["card_sha256"], rows[4]["card_sha256"])

    def test_the_card_hash_does_not_depend_on_how_the_model_was_chosen(self):
        a, b, c = (self.dispatch(*extra) for extra in (("--model", SONNET), (), ("--level", "top")))
        hashes = {self.meta(x)["route_info"]["card_sha256"] for x in (a, b, c)}
        self.assertEqual(len(hashes), 1)


class LocalRuleConstraintTests(Base):
    def floorless_catalog(self):
        cat = json.loads((ROOT / "config" / "model-catalog.json").read_text())
        cat["roles"]["scribe"] = {"max_level": "mid"}  # no min_level: nothing to confirm
        path = self.root / "floorless-catalog.json"
        path.write_text(json.dumps(cat))
        return path

    def rule(self, source, target, thinking="medium", pattern="pagination"):
        path = self.root / "local.json"
        path.write_text(json.dumps({"rules": [{"pattern": pattern, "from": [source], "to": {"model": target, "thinking": thinking}}]}))
        return {**self.env, "TP_LOCAL_CONFIG": str(path)}

    def route(self, env, *args):
        run = self.run_cmd("tp-route", "--no-log", "--role", "coder", *args, stdin=TASK, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def test_a_redirect_to_the_restricted_top_level_is_dropped_with_the_reason(self):
        out = self.route(self.rule(SONNET, OPUS, "high"))
        self.assertEqual((out["model"], out["level"]), (SONNET, "mid"))
        self.assertTrue(any("redirect" in s and "dropped" in s and "restricted" in s for s in out["steps"]), out["steps"])

    def test_a_redirect_that_breaks_local_only_is_dropped(self):
        env = self.rule(QWEN, SONNET, "medium", pattern=".")
        self.stubs.set({"chat": [{"text": reply(1, "low", "none", "locate_quote", "find")}]})
        run = self.run_cmd("tp-route", "--no-log", "--role", "scribe", "--local-only", stdin="Find where the timeout is set in net.py.", env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual((out["model"], out["provider"]), (QWEN, "local"))
        self.assertTrue(any("dropped" in s and "local-only" in s for s in out["steps"]), out["steps"])

    def test_a_redirect_to_a_provider_marked_out_is_dropped(self):
        (self.state / "provider-health.json").write_text(json.dumps({"openai": {"status": "exhausted"}}))
        out = self.route(self.rule(SONNET, SOL))
        self.assertEqual(out["model"], SONNET)
        self.assertTrue(any("dropped" in s and "marked unavailable" in s for s in out["steps"]), out["steps"])

    def test_an_allowed_redirect_still_applies_and_its_effort_is_clamped(self):
        out = self.route(self.rule(SONNET, SOL, "max"))
        self.assertEqual((out["model"], out["thinking"]), (SOL, "high"))
        self.assertIn(f"local rule redirected {SONNET} to {SOL}", out["steps"])
        out = self.route(self.rule(SONNET, SOL, "max"), "--approve-effort")
        self.assertEqual(out["thinking"], "max")

    def test_a_coder_redirect_below_the_floor_is_dropped(self):
        for target in (HAIKU, QWEN, LUNA):
            with self.subTest(target=target):
                out = self.route(self.rule(SONNET, target, "low"))
                self.assertEqual((out["model"], out["level"]), (SONNET, "mid"))
                # a local-model target may be refused earlier still (steps empty); the model must stay at the floor either way
                self.assertTrue(not out["steps"] or any("dropped" in s for s in out["steps"]), out["steps"])
                if target == HAIKU:
                    self.assertTrue(any("floor for role coder" in s for s in out["steps"]), out["steps"])

    def test_a_redirect_for_a_role_without_a_floor_still_applies(self):
        env = self.rule(SONNET, HAIKU, "low", pattern=".")
        run = self.run_cmd("tp-route", "--no-log", "--role", "scribe", stdin=TASK, env=env)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual((out["model"], out["level"]), (HAIKU, "small"))

    def test_the_emergency_fallback_never_substitutes_below_a_strict_floor(self):
        config = {"claude_preference": {"ordinary": {"model": "nope"}}, "routes": {}, "models": [
            {"model": HAIKU, "worker": "claude", "thinking": ["low"]}]}
        self.assertEqual(model_router._emergency(config, "claude", "scribe", self.floorless_catalog())[1], HAIKU)
        for role in ("coder", "tester", "researcher"):
            with self.subTest(role=role), self.assertRaises(model_router.RouteStop):
                model_router._emergency(config, "claude", role, None)

    def test_the_emergency_fallback_for_a_coder_is_the_configured_pair_only_when_it_is_at_the_floor(self):
        config = json.loads((ROOT / "config" / "router.json").read_text())
        self.assertEqual(model_router._emergency(config, "claude", "coder")[:2], ("claude", SONNET))
        self.assertEqual(model_router._emergency(config, "pi", "coder")[:2], ("pi", SOL))
        config["claude_preference"]["ordinary"]["model"] = HAIKU
        config["routes"]["coding"] = {"model": QWEN, "thinking": "off"}
        for worker in ("claude", "pi"):
            with self.subTest(worker=worker), self.assertRaises(model_router.RouteStop):
                model_router._emergency(config, worker, "coder")
        # the same configuration still serves a role that has no strict floor
        self.assertEqual(model_router._emergency(config, "claude", "scribe", self.floorless_catalog())[1], HAIKU)
        for role in ("tester", "researcher"):
            with self.subTest(role=role), self.assertRaises(model_router.RouteStop):
                model_router._emergency(config, "claude", role)

    def test_a_redirect_that_does_not_fit_the_packet_is_dropped(self):
        (self.project / "big.txt").write_text("x" * 40000)
        env = self.rule(SONNET, "slotstream/qwen3.8-flash-next:4bit", "low")
        out = self.route(env, "--workdir", str(self.project), "--context", "big.txt")
        self.assertEqual(out["model"], SONNET)
        self.assertTrue(any("dropped" in s and "over" in s for s in out["steps"]), out["steps"])


class InvalidCatalogCoderFloorTests(Base):
    """A catalog that fails validation is not trusted, but the coder still must not land on Haiku or a local model."""

    def setUp(self):
        super().setUp()
        cat = json.loads((ROOT / "config" / "model-catalog.json").read_text())
        cat["unexpected_key"] = True  # valid JSON, fails strict validation, roles and levels intact
        self.bad_catalog = self.root / "bad-catalog.json"
        self.bad_catalog.write_text(json.dumps(cat))
        self.config = json.loads((ROOT / "config" / "router.json").read_text())

    def route(self, role, *args, config=None, catalog=None):
        cfg = self.root / "router.json"
        cfg.write_text(json.dumps(config or self.config))
        return self.run_cmd("tp-route", "--no-log", "--role", role, "--catalog", str(catalog or self.bad_catalog), "--config", str(cfg),
                            *args, stdin=TASK)

    def test_the_configured_mid_pairs_are_used(self):
        for worker, model in (("claude", SONNET), ("pi", SOL)):
            with self.subTest(worker=worker):
                run = self.route("coder", "--worker", worker)
                self.assertEqual(run.returncode, 0, run.stderr)
                out = json.loads(run.stdout)
                self.assertEqual((out["source"], out["worker"], out["model"]), ("catalog_error_fallback", worker, model))

    def test_a_configured_pair_below_the_floor_stops_instead_of_routing_a_coder_to_it(self):
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        self.config["routes"]["coding"] = {"model": QWEN, "thinking": "off"}
        for worker in ("claude", "pi", None):
            with self.subTest(worker=worker):
                run = self.route("coder", *(("--worker", worker) if worker else ()))
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertIn("cannot be confirmed", run.stderr)
                self.assertNotIn(HAIKU, run.stdout)
                self.assertNotIn(QWEN, run.stdout)

    def test_a_configured_pair_missing_from_the_roster_stops_a_coder_without_substituting(self):
        self.config["claude_preference"]["ordinary"]["model"] = "no-such-model"
        run = self.route("coder", "--worker", "claude")
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertNotIn(HAIKU, run.stdout)

    def test_a_malformed_catalog_file_still_takes_the_floor_from_the_shipped_catalog(self):
        broken = self.root / "broken.json"
        broken.write_text("{ not json")
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        run = self.route("coder", "--worker", "claude", catalog=broken)
        self.assertNotEqual(run.returncode, 0, run.stdout)
        self.assertNotIn(HAIKU, run.stdout)
        run = self.route("coder", "--worker", "claude", config=json.loads((ROOT / "config" / "router.json").read_text()), catalog=broken)
        self.assertEqual(json.loads(run.stdout)["model"], SONNET)

    def test_a_catalog_whose_coder_rule_was_dropped_cannot_lower_the_floor(self):
        cat = json.loads(self.bad_catalog.read_text())
        del cat["roles"]["coder"]
        self.bad_catalog.write_text(json.dumps(cat))
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        run = self.route("coder", "--worker", "claude")
        self.assertNotEqual(run.returncode, 0, run.stdout)

    def test_a_configured_pair_the_catalog_places_below_mid_is_not_confirmed(self):
        # the model is in the roster, but the lenient read of the catalog puts it at the small level
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        run = self.route("coder", "--worker", "claude")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("floor", run.stderr)

    def test_a_role_without_a_min_level_keeps_the_configured_pair_even_below_mid(self):
        cat = json.loads(self.bad_catalog.read_text())
        cat["roles"]["scribe"] = {"max_level": "mid"}
        self.bad_catalog.write_text(json.dumps(cat))
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        run = self.route("scribe", "--worker", "claude")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout)["model"], HAIKU)

    def test_tester_and_researcher_are_held_to_the_floor_too(self):
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        for role in ("tester", "researcher"):
            with self.subTest(role=role):
                run = self.route(role, "--worker", "claude")
                self.assertNotEqual(run.returncode, 0, run.stdout)
                self.assertNotIn(HAIKU, run.stdout)

    def test_a_coder_rule_that_lost_strict_floor_but_kept_min_level_still_stops_a_coder(self):
        from unittest import mock
        cat = json.loads(self.bad_catalog.read_text())
        del cat["roles"]["coder"]["strict_floor"]
        self.bad_catalog.write_text(json.dumps(cat))
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        self.config["routes"]["coding"] = {"model": QWEN, "thinking": "off"}
        cfg = self.root / "router.json"
        cfg.write_text(json.dumps(self.config))
        with mock.patch.object(model_router.rc, "CATALOG_PATH", self.bad_catalog):
            for worker in ("claude", "pi", None):
                with self.subTest(worker=worker), self.assertRaises(model_router.RouteStop):
                    model_router.select({"role": "coder", "worker": worker, "task": "fix named code"},
                                        catalog_path=self.bad_catalog, config_path=cfg, log=False)

    def test_an_invalid_shipped_catalog_that_lost_the_coder_rule_stops_a_coder(self):
        # the shipped catalog itself is the invalid one, so no valid copy can restore the rule
        from unittest import mock
        cat = json.loads(self.bad_catalog.read_text())
        del cat["roles"]["coder"]
        self.bad_catalog.write_text(json.dumps(cat))
        self.config["claude_preference"]["ordinary"]["model"] = HAIKU
        cfg = self.root / "router.json"
        cfg.write_text(json.dumps(self.config))
        with mock.patch.object(model_router.rc, "CATALOG_PATH", self.bad_catalog):
            for worker in ("claude", "pi", None):
                with self.subTest(worker=worker), self.assertRaises(model_router.RouteStop):
                    model_router.select({"role": "coder", "worker": worker, "task": "fix named code"},
                                        catalog_path=self.bad_catalog, config_path=cfg, log=False)
            # a role the broken catalog still names with a floor is held to it
            with self.assertRaises(model_router.RouteStop):
                model_router.select({"role": "tester", "worker": "claude", "task": "t"},
                                    catalog_path=self.bad_catalog, config_path=cfg, log=False)
            # a role it does not name cannot be confirmed either
            with self.assertRaises(model_router.RouteStop):
                model_router.select({"role": "unlisted", "worker": "claude", "task": "t"},
                                    catalog_path=self.bad_catalog, config_path=cfg, log=False)


class ReviewPlanTests(Base):
    def author(self, worker, model, role="coder", level=None, effort="medium", router_review=None, **extra):
        path = self.state / "tasks" / f"a-{len(list((self.state / 'tasks').iterdir()))}"
        path.mkdir()
        info = {"level": level, "effort_abstract": effort, "router_review": router_review, "local_only": extra.pop("local_only", False)}
        (path / "meta.json").write_text(json.dumps({"id": path.name, "role": role, "worker": worker, "model": model, "thinking": effort,
                                                    "route_info": {k: v for k, v in info.items() if v is not None}, **extra}))
        return path

    def plan(self, path, **kw):
        return model_router.review_plan(path, base=self.state, log=False, files_touched=kw.pop("files_touched", 1), **kw)

    def test_claude_authored_work_goes_to_gpt_at_mid(self):
        plan = self.plan(self.author("claude", SONNET))
        self.assertEqual(plan["reviewer"]["model"], SOL)
        self.assertEqual((plan["reviewer"]["worker"], plan["reviewer"]["level"], plan["reviewer"]["independence"]), ("pi", "mid", "different_provider"))

    def test_gpt_authored_work_goes_to_claude(self):
        plan = self.plan(self.author("pi", SOL))
        self.assertEqual((plan["reviewer"]["worker"], plan["reviewer"]["model"]), ("claude", SONNET))

    def test_qwen_authored_work_goes_to_gpt_at_least_mid(self):
        plan = self.plan(self.author("pi", QWEN, level="local"))
        self.assertEqual((plan["reviewer"]["model"], plan["reviewer"]["level"]), (SOL, "mid"))
        plan = self.plan(self.author("claude", HAIKU))  # a small author still gets a mid reviewer
        self.assertEqual(plan["reviewer"]["level"], "mid")

    def test_author_started_without_a_model_is_placed_by_its_harness(self):
        for worker, expected in (("claude", ("pi", SOL)), ("pi", ("claude", SONNET))):
            plan = self.plan(self.author(worker, ""))
            self.assertEqual((plan["reviewer"]["worker"], plan["reviewer"]["model"]), expected)
            self.assertEqual(plan["author"]["model"], None)

    def test_top_level_author_does_not_pull_the_reviewer_to_the_top_level(self):
        plan = self.plan(self.author("claude", OPUS, level="top", effort="high"))
        self.assertEqual((plan["reviewer"]["level"], plan["reviewer"]["thinking"]), ("mid", "high"))

    def test_explicit_reviewer_level_and_effort(self):
        plan = self.plan(self.author("claude", SONNET), level="top", thinking="high")
        self.assertEqual((plan["reviewer"]["model"], plan["reviewer"]["thinking"]), (ASTRA, "high"))
        plan = self.plan(self.author("claude", SONNET), thinking="max")
        self.assertEqual(plan["reviewer"]["thinking"], "high")
        plan = self.plan(self.author("claude", SONNET), thinking="max", approve_effort=True)
        self.assertEqual(plan["reviewer"]["thinking"], "max")
        with self.assertRaises(ValueError):
            self.plan(self.author("claude", SONNET), level="strong")

    def test_stop_and_ask_when_a_required_review_has_no_independent_provider(self):
        (self.state / "provider-health.json").write_text(json.dumps({"openai": {"status": "exhausted"}}))
        path = self.author("claude", SONNET, effort="high")
        with self.assertRaises(model_router.RouteStop) as caught:
            self.plan(path)
        self.assertIn("no independent reviewer", str(caught.exception))
        # an optional review may use the author's provider, and says so
        plan = self.plan(self.author("claude", SONNET, effort="medium"))
        self.assertEqual((plan["review"], plan["reviewer"]["independence"], plan["reviewer"]["worker"]), ("optional", "same_provider", "claude"))

    def test_local_only_work_is_not_sent_to_a_cloud_reviewer(self):
        with self.assertRaises(model_router.RouteStop):
            self.plan(self.author("pi", QWEN, level="local", local_only=True))

    def test_a_read_only_role_still_needs_review_for_a_high_level_or_effort(self):
        for role in ("researcher", "tester"):
            plan = self.plan(self.author("claude", OPUS, role=role, level="top", effort="low"), files_touched=None)
            self.assertEqual((plan["review"], plan["policy"]), ("required", "required"), role)
            self.assertEqual(plan["reviewer"]["model"], SOL)
            plan = self.plan(self.author("claude", SONNET, role=role, level="mid", effort="high"), files_touched=None)
            self.assertEqual(plan["review"], "required", role)
            plan = self.plan(self.author("claude", SONNET, role=role, level="mid", effort="medium"), files_touched=None)
            self.assertEqual(plan["review"], "none", role)

    def test_the_review_record_carries_the_card_hash_of_the_reviewed_task(self):
        path = self.author("claude", SONNET, role="coder")
        (path / "request.md").write_text(TASK + "\n")
        model_router.review_plan(path, base=self.state, log=True, files_touched=1)
        row = [r for r in self.selections() if r["kind"] == "review"][-1]
        self.assertRegex(row["card_sha256"], r"^[0-9a-f]{64}$")

    def test_need_is_the_larger_of_policy_and_the_routers_field(self):
        self.assertEqual(self.plan(self.author("claude", SONNET, role="researcher"), files_touched=0)["review"], "none")
        self.assertEqual(self.plan(self.author("claude", SONNET, role="researcher", router_review="required"), files_touched=0)["review"], "required")
        self.assertEqual(self.plan(self.author("claude", SONNET, role="coder", router_review="none"))["review"], "optional")
        self.assertEqual(self.plan(self.author("claude", SONNET, role="coder", router_review="required"))["review"], "required")
        self.assertEqual(self.plan(self.author("claude", SONNET, role="coder"), files_touched=6)["review"], "required")
        self.assertEqual(self.plan(self.author("claude", SONNET, role="coder", effort="high"))["review"], "required")
        none = self.plan(self.author("claude", SONNET, role="researcher"), files_touched=0)
        self.assertNotIn("reviewer", none)
        forced = self.plan(self.author("claude", SONNET, role="researcher"), files_touched=0, force=True)
        self.assertEqual((forced["review"], forced["reviewer"]["model"]), ("forced", SOL))

    def test_plan_cli_exit_codes(self):
        run = lambda path, *a: self.run_cmd("tp-route", "--no-log", "--review-plan", str(path), *a)
        self.assertEqual(run(self.author("claude", SONNET, role="researcher")).returncode, 3)
        ok = run(self.author("claude", SONNET, role="coder"))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["reviewer"]["model"], SOL)
        (self.state / "provider-health.json").write_text(json.dumps({"openai": {"status": "exhausted"}}))
        self.assertEqual(run(self.author("claude", SONNET, role="coder", effort="high")).returncode, 4)
        self.assertEqual(run(self.author("claude", SONNET), "--level", "strong").returncode, 1)
        self.assert_no_laya()


class ReviewCommandTests(Base):
    def settle(self, item, result="RESULT_STATUS: complete\nDone.\n"):
        task = self.state / "tasks" / item["id"]
        (task / "state.json").write_text('{"state":"waiting"}')
        (task / "result.md").write_text(result)

    def test_agent_review_starts_a_reviewer_on_another_provider(self):
        item = self.dispatch()
        self.settle(item)
        value = self.agent("review", item["id"])
        self.assertEqual((value["id"], value["status"]), (item["id"], "started"))
        review = json.loads((self.state / "tasks" / value["review"] / "meta.json").read_text())
        self.assertEqual((review["worker"], review["model"]), ("pi", SOL))
        author = self.meta(item)
        self.assertEqual((author["review"], author["reviewer"]["model"], author["reviewer"]["independence"]),
                         (value["review"], SOL, "different_provider"))
        self.assertEqual(self.selections()[-1]["kind"], "review")
        self.assertEqual(self.selections()[-1]["resolved"]["model"], SOL)
        self.assert_no_laya()

    def test_agent_review_reports_when_no_review_is_needed(self):
        item = self.dispatch("--model", SONNET, "--thinking", "medium", role="researcher")
        self.settle(item)
        value = self.agent("review", item["id"])
        self.assertEqual((value["status"], value["review"]), ("not_needed", None))
        forced = self.agent("review", item["id"], "--force")
        self.assertEqual(forced["status"], "started")

    def test_agent_review_stops_when_no_independent_provider_is_available(self):
        (self.state / "provider-health.json").write_text(json.dumps({"openai": {"status": "exhausted"}}))
        item = self.dispatch("--model", SONNET, "--thinking", "high")
        self.settle(item)
        run = self.agent("review", item["id"], check=False)
        self.assertEqual(run.returncode, 1)
        self.assertIn("no independent reviewer", run.stderr)
        self.assertIn("ask the human", run.stderr)


if __name__ == "__main__":
    unittest.main()
