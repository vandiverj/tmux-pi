"""Experimental Claude preference and routing eval logging (fake Laya; no live model or frozen set)."""

import copy
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
import routing_evals  # noqa: E402
from automation import automation_policy  # noqa: E402
from router import (QUESTIONS, RouterError, claude_preference, dispatch, feature_names, laya_answers,  # noqa: E402
                    load_config, route_task)
import test_router  # noqa: E402

Response = test_router.Response

TASK = "Refactor the private payroll importer for ACME-SECRET-CUSTOMER"


def helper():
    """RouterTests' fake Laya replies and toy model, without collecting its tests here."""
    h = test_router.RouterTests("test_dispatch")
    h.setUp()
    return h


def model_routing_on_tools(h, labels=("simple", "expert")):
    """Toy model whose route follows needs_tools (low: labels[0], high: labels[1])."""
    model = h.toy_model()
    names = feature_names()
    weights = [[0.0, 0.0] for _ in names]
    weights[names.index("needs_tools")] = [-20.0, 20.0]
    model["route"] = {"labels": list(labels), "mean": [0.5] * len(names), "scale": [1.0] * len(names),
                      "weights": weights, "bias": [0.0, 0.0]}
    return model


def write_config(directory, model, preference=True):
    raw = json.loads((ROOT / "config/router.json").read_text())
    if not preference:
        raw.pop("claude_preference", None)
    (Path(directory) / "router.json").write_text(json.dumps(raw))
    (Path(directory) / "laya-model.json").write_text(json.dumps(model))
    return Path(directory) / "router.json"


def opener_for(reply):
    return lambda *_a, **_k: Response(reply)


class ClaudePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.h = helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write_config(self.tmp.name, model_routing_on_tools(self.h))
        self.config = load_config(self.path, local_path=False)

    def run_dispatch(self, config=None, text=TASK, **reply):
        trace = {}
        decision = dispatch(text, config or self.config, opener=opener_for(self.h.laya_reply(**reply)), trace=trace)
        return decision, trace

    def test_shipped_config(self):
        config = load_config(ROOT / "config/router.json", local_path=False)
        pref = config["claude_preference"]
        self.assertEqual((pref["ordinary"]["model"], pref["ordinary"]["thinking"]), ("claude-sonnet-5-5", "medium"))
        self.assertEqual((pref["heavy"]["model"], pref["heavy"]["thinking"]), ("claude-opus-5-5", "high"))
        # The shared route table is unchanged: GPT-6 routes stay GPT-6, expert stays Opus/high.
        self.assertEqual(config["routes"]["coding"]["model"], "openai/gpt-6-sol")
        self.assertEqual(config["routes"]["complex"]["model"], "openai/gpt-6-sol")
        self.assertEqual(config["routes"]["expert"], {"model": "claude-opus-5-5", "thinking": "high"})

    def test_ordinary_claude_recommendation_prefers_sonnet(self):
        decision, trace = self.run_dispatch(tools=0.9, difficulty=1, risk=0.1, sensitive=0.1, scope="sustained")
        self.assertEqual((decision["worker"], decision["model"], decision["thinking"]),
                         ("claude", "claude-sonnet-5-5", "medium"))
        self.assertEqual(decision["route"], "expert")
        self.assertEqual(trace["laya_target"]["model"], "claude-opus-5-5")
        self.assertEqual(trace["preference"]["tier"], "ordinary")
        self.assertTrue(trace["preference"]["changed"])

    def test_heavy_traits_escalate_to_opus_high(self):
        for traits, reason in ((dict(difficulty=3), "difficulty"), (dict(risk=0.8), "high_risk"),
                               (dict(sensitive=0.7), "sensitive")):
            reply = dict(tools=0.9, difficulty=1, risk=0.1, sensitive=0.1, scope="sustained")
            reply.update(traits)
            decision, trace = self.run_dispatch(**reply)
            self.assertEqual((decision["model"], decision["thinking"]), ("claude-opus-5-5", "high"), reason)
            self.assertIn(reason, trace["preference"]["reasons"])
            self.assertEqual(trace["preference"]["tier"], "heavy")

    def test_multistep_alone_is_not_heavy_reasoning(self):
        decision, trace = self.run_dispatch(tools=0.9, difficulty=1, risk=0.1, sensitive=0.1, scope="multi_step")
        self.assertEqual(decision["model"], "claude-sonnet-5-5")
        self.assertEqual(trace["preference"]["tier"], "ordinary")

    def test_non_claude_routes_are_never_moved_to_claude(self):
        decision, trace = self.run_dispatch(tools=0.1, difficulty=3, risk=0.9)
        self.assertEqual(decision["model"], "slotstream/qwen3.8-flash-next:4bit")
        self.assertIsNone(trace["preference"])
        path = write_config(self.tmp.name, model_routing_on_tools(self.h, ("simple", "coding")))
        config = load_config(path, local_path=False)
        for tools in (0.1, 0.9):
            decision, trace = self.run_dispatch(config, tools=tools, difficulty=3, risk=0.9, sensitive=0.9)
            self.assertEqual(decision["worker"], "pi")
            self.assertIsNone(trace["preference"])
        self.assertEqual(decision["model"], "openai/gpt-6-sol")

    def test_preference_changes_only_model_and_effort(self):
        with tempfile.TemporaryDirectory() as other:
            plain = load_config(write_config(other, model_routing_on_tools(self.h), preference=False), local_path=False)
            for reply in (dict(tools=0.9), dict(tools=0.9, intent="research", changes=0.1),
                          dict(tools=0.9, intent="communication_or_documentation", changes=0.9, risk=0.9)):
                with_pref, _ = self.run_dispatch(**reply)
                without, _ = self.run_dispatch(plain, **reply)
                strip = lambda d: {k: v for k, v in d.items() if k not in ("model", "thinking")}
                self.assertEqual(strip(with_pref), strip(without))
                self.assertEqual(automation_policy(TASK, with_pref)["review"], automation_policy(TASK, without)["review"])

    def test_route_task_applies_preference_and_rules_still_win(self):
        reply = self.h.laya_reply(tools=0.9, difficulty=1, risk=0.1, sensitive=0.1)
        self.assertEqual(route_task(TASK, self.config, opener=opener_for(reply))["model"], "claude-sonnet-5-5")
        config = dict(self.config, local={"rules": [{"pattern": "payroll", "from": ["claude-sonnet-5-5"],
                                                      "to": {"model": "openai/gpt-6-sol", "thinking": "high"}}]})
        trace = {}
        self.assertEqual(route_task(TASK, config, opener=opener_for(reply), trace=trace)["model"], "openai/gpt-6-sol")
        self.assertTrue(trace["rule_applied"])

    def test_invalid_preference_is_rejected(self):
        base = json.loads((ROOT / "config/router.json").read_text())
        bad = [
            {"ordinary": {"model": "openai/gpt-6-sol", "thinking": "high"}},
            {"heavy": {"model": "claude-opus-5-5", "thinking": "max"}},
            {"ordinary": {"model": "claude-sonnet-9", "thinking": "medium"}},
            {"heavy_when": {"difficulty_at_least": 4, "high_risk_at_least": 0.5, "sensitive_at_least": 0.5, "scopes": []}},
            {"heavy_when": {"difficulty_at_least": 2, "high_risk_at_least": 0.5, "sensitive_at_least": 0.5, "scopes": ["huge"]}},
            {"extra": 1},
        ]
        for change in bad:
            raw = copy.deepcopy(base)
            raw["claude_preference"].update(change)
            (Path(self.tmp.name) / "router.json").write_text(json.dumps(raw))
            with self.assertRaises(RouterError, msg=change):
                load_config(Path(self.tmp.name) / "router.json", local_path=False)

    def test_preference_is_a_pure_function_of_bounded_traits(self):
        answers = laya_answers(self.h.laya_reply(difficulty=2, risk=0.49, sensitive=0.49, scope="brief"))
        target = {"worker": "claude", "model": "claude-haiku-5-5", "thinking": "low"}
        chosen, record = claude_preference(target, answers, self.config)
        self.assertEqual(chosen["model"], "claude-sonnet-5-5")
        self.assertEqual(record["reasons"], [])
        pi = {"worker": "pi", "model": "openai/gpt-6-astra", "thinking": "high"}
        self.assertEqual(claude_preference(pi, answers, self.config), (pi, None))


class SelectionRecordTests(unittest.TestCase):
    def setUp(self):
        self.h = helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write_config(self.tmp.name, model_routing_on_tools(self.h))
        self.config = load_config(self.path, local_path=False)
        self.trace = {}
        self.decision = dispatch("+review " + TASK, self.config, trace=self.trace,
                                 opener=opener_for(self.h.laya_reply(tools=0.9, intent="research", changes=0.1)))

    def record(self, **kw):
        return routing_evals.selection_record("+review " + TASK, self.config, self.path, decision=self.decision,
                                              trace=self.trace, include_text=kw.pop("include_text", False), **kw)

    def test_record_is_sanitized_and_versioned(self):
        rec = self.record()
        dumped = json.dumps(rec)
        self.assertNotIn("ACME-SECRET-CUSTOMER", dumped)
        self.assertNotIn("payroll", dumped)
        self.assertEqual(rec["request"]["sha256"], routing_evals.sha256_text("+review " + TASK))
        self.assertEqual(rec["request"]["task_sha256"], routing_evals.sha256_text(TASK))
        self.assertEqual(rec["request"]["markers"], ["review"])
        self.assertRegex(rec["selection_id"], routing_evals.SELECTION_ID)
        self.assertEqual((rec["authority"], rec["experimental"], rec["status"]), ("none", True, "ok"))
        v = rec["versions"]
        for key in ("questions_sha256", "feature_questions_sha256", "classifier_request_sha256", "router_config_sha256",
                    "laya_model_sha256", "claude_preference_sha256", "router_code_sha256"):
            self.assertRegex(v[key], r"^[0-9a-f]{64}$", key)
        self.assertEqual(set(v["heads_sha256"]), {"route", "intent", "gate", "level", "placement"})
        scores = rec["classifier"]["scores"]
        self.assertEqual(set(scores), set(QUESTIONS))
        self.assertTrue(all(0 <= p <= 1 for p in scores["task_intent"]["probabilities"].values()))
        self.assertAlmostEqual(sum(rec["classifier"]["heads"]["route"].values()), 1, places=3)
        r = rec["recommendation"]
        self.assertEqual((r["route"], r["laya_target"]["model"], r["final"]["model"], r["claude_preference"]["tier"]),
                         ("expert", "claude-opus-5-5", "claude-sonnet-5-5", "ordinary"))
        self.assertEqual(r["review"], "required")
        self.assertFalse(rec["actual"]["main_model_switched"])

    def test_text_is_opt_in_and_bounded(self):
        rec = self.record(include_text=True)
        self.assertEqual(rec["request"]["text"], TASK)
        long = "x " * 10000
        rec = routing_evals.selection_record(long, self.config, self.path, include_text=True, error=RouterError("down"))
        self.assertEqual(len(rec["request"]["text"]), routing_evals.MAX_TEXT)
        self.assertEqual((rec["status"], rec["error"]["type"]), ("router_error", "RouterError"))
        self.assertNotIn("recommendation", rec)

    def test_main_placed_pi_route_is_honest_about_main_model(self):
        trace = {}
        decision = dispatch("+here explain tp-view", self.config, trace=trace,
                            opener=opener_for(self.h.laya_reply(tools=0.1, intent="communication_or_documentation", changes=0.1)))
        rec = routing_evals.selection_record("+here explain tp-view", self.config, self.path, decision=decision, trace=trace)
        self.assertEqual((decision["placement"], decision["worker"]), ("main", "pi"))
        self.assertEqual(rec["actual"]["main_model"], "unknown")
        self.assertIn("does not switch the main model", rec["actual"]["note"])

    def test_oversized_record_drops_optional_parts(self):
        old = routing_evals.MAX_RECORD_BYTES
        routing_evals.MAX_RECORD_BYTES = 2500
        try:
            rec = self.record(include_text=True)
        finally:
            routing_evals.MAX_RECORD_BYTES = old
        self.assertIn("request.text", rec["truncated"])
        self.assertNotIn("text", rec["request"])


class AppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "evals"

    def test_concurrent_appends_are_whole_lines(self):
        record = {"schema": "x", "pad": "y" * 3000}
        threads = [threading.Thread(target=lambda i=i: [routing_evals.append_jsonl(self.dir, "t.jsonl", dict(record, i=i, j=j))
                                                        for j in range(20)]) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows, bad = routing_evals.read_jsonl(self.dir / "t.jsonl")
        self.assertEqual((len(rows), bad), (160, 0))
        self.assertEqual(oct((self.dir / "t.jsonl").stat().st_mode & 0o777), "0o600")

    def test_failures_raise_eval_log_error(self):
        target = Path(self.tmp.name) / "elsewhere"
        target.mkdir()
        os.symlink(target, self.dir)
        with self.assertRaisesRegex(routing_evals.EvalLogError, "symlink"):
            routing_evals.append_jsonl(self.dir, "t.jsonl", {"a": 1})
        os.unlink(self.dir)
        self.dir.mkdir()
        os.symlink(target / "victim", self.dir / "t.jsonl")
        with self.assertRaises(routing_evals.EvalLogError):
            routing_evals.append_jsonl(self.dir, "t.jsonl", {"a": 1})
        self.assertFalse((target / "victim").exists())
        os.unlink(self.dir / "t.jsonl")
        old = routing_evals.MAX_LOG_BYTES
        routing_evals.MAX_LOG_BYTES = 10
        try:
            with self.assertRaisesRegex(routing_evals.EvalLogError, "full"):
                routing_evals.append_jsonl(self.dir, "t.jsonl", {"a": "long enough"})
        finally:
            routing_evals.MAX_LOG_BYTES = old
        with self.assertRaisesRegex(routing_evals.EvalLogError, "larger"):
            routing_evals.append_jsonl(self.dir, "t.jsonl", {"a": "z" * (routing_evals.MAX_RECORD_BYTES + 10)})
        with self.assertRaisesRegex(routing_evals.EvalLogError, "no tmux-pi state dir"):
            env = {k: v for k, v in os.environ.items() if k not in ("TP_ROUTING_EVAL_DIR", "TP_STATE_DIR", "TMUX")}
            with unittest.mock.patch.dict(os.environ, env, clear=True):
                routing_evals.log_selection({"a": 1})

    def test_torn_line_is_counted_not_fatal(self):
        self.dir.mkdir()
        (self.dir / "t.jsonl").write_text('{"a":1}\n{"b":\n[1]\n')
        self.assertEqual(routing_evals.read_jsonl(self.dir / "t.jsonl"), ([{"a": 1}], 2))


class FakeLaya:
    """A loopback typed-decisions endpoint that returns a fixed reply (or HTTP 503)."""

    def __init__(self, reply, status=200):
        body = json.dumps(reply).encode()
        self.hits = hits = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.server.server_port)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class CliTests(unittest.TestCase):
    def setUp(self):
        self.h = helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = write_config(self.tmp.name, model_routing_on_tools(self.h))
        self.laya = FakeLaya(self.h.laya_reply(tools=0.9, intent="research", changes=0.1))
        self.addCleanup(self.laya.close)
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if k not in ("TMUX", "TP_STATE_DIR", "TP_ROUTING_EVAL_DIR", "TP_ROUTING_EVALS", "TP_ROUTING_EVALS_TEXT",
                            "TP_LOCAL_CONFIG", "TP_ROUTER_BACKEND", "TP_ROUTER_BASE_URL", "TP_ROUTER_MODEL")}
        env.update(extra)
        return env

    def route(self, *args, url=None, **env):
        return subprocess.run([sys.executable, str(ROOT / "bin/tp-route"), "--config", str(self.config),
                               "--base-url", url or self.laya.url, *args], input=TASK, capture_output=True,
                              text=True, env=self.env(**env), timeout=60)

    def test_dispatch_logs_and_links_selection(self):
        r = self.route("--dispatch", TP_STATE_DIR=str(self.state))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        rows, bad = routing_evals.read_jsonl(self.state / "evals" / routing_evals.SELECTIONS)
        self.assertEqual((len(rows), bad), (1, 0))
        self.assertEqual(rows[0]["selection_id"], out["selection_id"])
        self.assertEqual(rows[0]["recommendation"]["final"]["model"], out["model"])
        self.assertNotIn("ACME-SECRET-CUSTOMER", (self.state / "evals" / routing_evals.SELECTIONS).read_text())

    def test_logging_failure_changes_nothing_and_grants_nothing(self):
        ok = self.route("--dispatch", TP_STATE_DIR=str(self.state))
        off = self.route("--dispatch", TP_ROUTING_EVALS="0")
        blocked = Path(self.tmp.name) / "blocked"
        blocked.write_text("not a directory")
        failed = self.route("--dispatch", TP_ROUTING_EVAL_DIR=str(blocked / "evals"))
        self.assertEqual((ok.returncode, off.returncode, failed.returncode), (0, 0, 0))
        self.assertIn("routing eval not logged", failed.stderr)
        base = {k: v for k, v in json.loads(ok.stdout).items() if k != "selection_id"}
        self.assertEqual(json.loads(off.stdout), base)
        self.assertEqual(json.loads(failed.stdout), base)
        self.assertNotIn("selection_id", json.loads(failed.stdout))
        # Authority is decided by policy from the decision; the log leaves it identical.
        strip = lambda p: {k: v for k, v in p.items() if k != "created"}
        self.assertEqual(strip(automation_policy(TASK, json.loads(ok.stdout))),
                         strip(automation_policy(TASK, json.loads(failed.stdout))))
        self.assertEqual(automation_policy(TASK, json.loads(ok.stdout))["action"], "recommend_manual")

    def test_router_failure_is_logged_and_still_fails_closed(self):
        down = FakeLaya({}, status=503)
        self.addCleanup(down.close)
        r = self.route("--dispatch", url=down.url, TP_STATE_DIR=str(self.state))
        self.assertEqual((r.returncode, r.stdout), (1, ""))
        rows, _ = routing_evals.read_jsonl(self.state / "evals" / routing_evals.SELECTIONS)
        self.assertEqual((rows[0]["status"], rows[0]["error"]["type"]), ("router_error", "RouterError"))

    def test_plain_route_is_the_catalog_router_and_never_asks_laya(self):
        # Changed from test_plain_route_output_is_unchanged: the plain route is no longer a Laya decision. It keeps
        # worker, model and thinking first, adds the router's record, and writes a model-selection record instead.
        import route_fixture
        stubs = route_fixture.Stubs(self.tmp.name, {"claude": [{"text": route_fixture.reply(3, "medium", "optional", None, "stub")}]})
        self.addCleanup(stubs.close)
        # The fixture catalog (Haiku first) and the stubbed chat endpoint: no model is reached whatever the shipped order is,
        # and the guard fails the test if the Laya port or the real LM Studio port is contacted.
        guard, hits = route_fixture.laya_guard(self.tmp.name)
        r = self.route(TP_STATE_DIR=str(self.state), **{k: v for k, v in stubs.env().items() if k in
                                                       ("PATH", "STUB_LOG", "STUB_SCRIPT", "TP_LOCAL_CONFIG", "TP_CLAUDE_CONFIG_DIR",
                                                        "TP_ROUTER_LLM_URL", "TP_MODEL_CATALOG")}, **guard)
        self.assertFalse(hits.exists(), "a real model endpoint was contacted")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(list(out)[:3], ["worker", "model", "thinking"])
        self.assertEqual((out["decided_by"], out["source"]), ("claude-haiku-5-5", "router"))
        self.assertEqual(self.laya.hits, [])
        rows, _ = routing_evals.read_jsonl(self.state / "evals" / "model-selections.jsonl")
        self.assertEqual([row["kind"] for row in rows], ["route"])
        self.assertFalse((self.state / "evals" / routing_evals.SELECTIONS).exists())


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.h = helper()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write_config(self.tmp.name, model_routing_on_tools(self.h))
        self.config = load_config(self.path, local_path=False)
        self.state = Path(self.tmp.name) / "state"
        self.evals = self.state / "evals"

    def select(self, text=TASK, include_text=False, **reply):
        trace = {}
        decision = dispatch(text, self.config, trace=trace, opener=opener_for(self.h.laya_reply(**reply)))
        rec = routing_evals.selection_record(text, self.config, self.path, decision=decision, trace=trace,
                                             include_text=include_text)
        routing_evals.log_selection(rec, self.evals)
        return dict(decision, selection_id=rec["selection_id"]), rec

    def task(self, task_id, meta, state=None, files=None):
        d = self.state / "tasks" / task_id
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps(dict({"id": task_id, "created": "2026-09-29T10:00:00Z"}, **meta)))
        if state:
            (d / "state.json").write_text(json.dumps(state))
        for name, text in (files or {}).items():
            (d / name).write_text(text)
        return d

    def test_export_joins_tasks_review_escalation_and_outcomes(self):
        decision, rec = self.select(include_text=True, tools=0.9, intent="implementation")
        self.task("auto-1", {"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "medium", "decision": decision,
                             "review": "review-auto-1", "escalated_to": {"id": "auto-2", "model": "claude-opus-5-5"},
                             "handshake_id": "r5-x"},
                  {"state": "waiting", "tokens": {"input": 100, "output": 40, "cacheRead": 0, "cacheWrite": 0},
                   "cost": 0.25, "updated": "2026-09-29T10:05:00Z"},
                  {"result.md": "done", "review-required.json": json.dumps({"required": True, "status": "pending_review"})})
        self.task("review-auto-1", {"worker": "pi", "model": "openai/gpt-6-sol", "thinking": "medium"},
                  files={"result.md": "APPROVED\nlooks right\nCoverage\nreviewed the changed files."})
        self.task("auto-2", {"worker": "claude", "model": "claude-opus-5-5", "thinking": "high",
                             "escalated_from": {"id": "auto-1"}},
                  {"state": "waiting", "cost": 1.0, "tokens": {"output": 10}, "updated": "2026-09-29T10:09:00Z"})
        hand = self.state / "handoffs" / "r5-x"
        hand.mkdir(parents=True)
        (hand / "scaffold.json").write_text(json.dumps({"handshake_id": "r5-x", "decision": decision,
                                                        "original_request": TASK}))
        rows, summary = routing_evals.export_rows(self.evals, self.state)
        self.assertEqual((summary["selections"], summary["linked"]), (1, 1))
        row = rows[0]
        dumped = json.dumps(row)
        self.assertNotIn("ACME-SECRET-CUSTOMER", dumped)  # opt-in text and scaffold requests stay out of exports
        self.assertEqual(row["link"], "selection_id")
        self.assertEqual(row["handshakes"], ["r5-x"])
        self.assertEqual([t["task_id"] for t in row["tasks"]], ["auto-1", "auto-2"])
        first = row["tasks"][0]
        self.assertEqual((first["duration_seconds"], first["cost"], first["review"]["verdict"]), (300, 0.25, "APPROVED"))
        self.assertEqual(first["review_required"], {"status": "pending_review"})
        self.assertEqual(row["actual"]["worker"]["model"], "claude-sonnet-5-5")
        self.assertTrue(row["actual"]["matches_recommendation"])
        self.assertTrue(row["actual"]["escalated"])
        self.assertEqual(row["totals"]["cost"], 1.25)
        labels = row["labels"]
        # A review verdict and an escalation are evidence, not acceptance or a correctness label.
        self.assertEqual((labels["acceptance"], labels["sizing"], labels["review_verdicts"]), ("unknown", "unknown", ["APPROVED"]))
        self.assertIn("not by itself a correctness label", labels["escalation_note"])

        out = routing_evals.outcome_record(self.config, selection_id=rec["selection_id"], rating="good",
                                           acceptance="accepted_with_changes", sizing="oversized",
                                           correct_model="claude-sonnet-5-5", correct_thinking="low", note="fine")
        routing_evals.append_jsonl(self.evals, routing_evals.OUTCOMES, out)
        labels = routing_evals.export_rows(self.evals, self.state)[0][0]["labels"]
        self.assertEqual((labels["acceptance"], labels["acceptance_source"], labels["sizing"], labels["rating"]),
                         ("accepted_with_changes", "human", "oversized", "good"))
        self.assertEqual(labels["correction"]["model"], "claude-sonnet-5-5")

    def test_export_classifies_a_request_changes_with_no_code_finding_as_unusable_not_as_a_rejection(self):
        coverage = "\nCoverage\nreviewed the changed files."
        replies = {"check": "REQUEST_CHANGES\nF1 [note] no count in the report. Closes by: check" + coverage,
                   "code": "REQUEST_CHANGES\nF1 [defect] wrong value. Closes by: code" + coverage,
                   "bare": "REQUEST_CHANGES\nF1 [defect] wrong value." + coverage,
                   "quoted": "REQUEST_CHANGES\nF1 [defect] wrong value.\nCoverage\nreviewed the files, quoting `Closes by: check` from the rule."}
        dirs = {}
        for name, reply in replies.items():
            dirs[f"review-{name}"] = self.task(f"review-{name}", {"worker": "pi"}, files={"result.md": reply})
            dirs[f"auto-{name}"] = self.task(f"auto-{name}", {"worker": "pi", "review": f"review-{name}"})
        review = {name: routing_evals.task_summary(dirs[f"auto-{name}"], dirs)["review"] for name in replies}
        self.assertEqual(review["check"]["verdict"], "UNUSABLE")
        self.assertEqual(review["check"]["stated_verdict"], "REQUEST_CHANGES")
        self.assertIn("Closes by: code", review["check"]["unusable_reason"])
        for name in ("code", "bare", "quoted"):
            self.assertEqual(review[name]["verdict"], "REQUEST_CHANGES", name)

    def test_legacy_tasks_join_by_hash_only_when_unambiguous(self):
        decision, _ = self.select(tools=0.9)
        legacy = {k: v for k, v in decision.items() if k != "selection_id"}
        self.task("old-1", {"worker": "claude", "model": "claude-opus-5-5", "thinking": "high", "decision": legacy})
        rows, _ = routing_evals.export_rows(self.evals, self.state)
        self.assertEqual((rows[0]["link"], rows[0]["actual"]["overridden"]), ("task_sha256", True))
        self.select(tools=0.9)
        rows, _ = routing_evals.export_rows(self.evals, self.state)
        self.assertTrue(all(r["tasks"] == [] for r in rows))

    def test_outcome_validation(self):
        good = dict(selection_id="sel-" + "a" * 32, rating="good")
        for bad in (dict(good, selection_id="sel-1"), dict(good, selection_id=None), dict(good, rating="great"),
                    dict(good, task_id="../x"), dict(good, correct_model="claude-opus-5-5"),
                    dict(good, correct_model="nope", correct_thinking="low"), dict(good, note="n" * 1001),
                    dict(selection_id=good["selection_id"]), dict(good, rater="a b")):
            with self.assertRaises(routing_evals.EvalLogError, msg=bad):
                routing_evals.outcome_record(self.config, **bad)
        self.assertEqual(routing_evals.outcome_record(self.config, **good)["authority"], "none")

    def test_tp_evals_cli(self):
        _, rec = self.select(tools=0.9)
        run = lambda *a: subprocess.run([sys.executable, str(ROOT / "bin/tp-evals"), "--state-dir", str(self.state),
                                         "--config", str(self.path), *a], capture_output=True, text=True, timeout=60)
        r = run("rate", "--selection", rec["selection_id"], "--acceptance", "rejected", "--note", "wrong model")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r"^out-[0-9a-f]{32}$")
        self.assertEqual(run("rate", "--selection", "bogus", "--rating", "good").returncode, 1)
        listed = run("list")
        self.assertIn(rec["selection_id"], listed.stdout)
        self.assertIn("claude:ordinary", listed.stdout)
        out = Path(self.tmp.name) / "rows.jsonl"
        r = run("export", "--out", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        row = json.loads(out.read_text())
        self.assertEqual(row["labels"]["acceptance"], "rejected")
        self.assertEqual(json.loads(r.stderr)["outcomes"], 1)


if __name__ == "__main__":
    unittest.main()
