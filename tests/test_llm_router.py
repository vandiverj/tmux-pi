"""The router call chain (lib/llm_router.py via lib/model_router.select) with stub `claude` and `pi` on PATH.
No live model is called. Covers the chain order, failover, the invalid-reply retry, the Qwen cap, local-only work,
the all-fail fallback and what each router is (and is not) given."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import route_fixture as fx
from route_fixture import ASTRA, HAIKU, LUNA, OPUS, QWEN, SOL, SONNET, reply

import model_router  # noqa: E402
import llm_router  # noqa: E402
import router_catalog as rc  # noqa: E402

CAT = fx.catalog()
TASK = ("Add pagination to the /orders endpoint with tests, following the existing list endpoints. "
        "Do not edit the billing module. A fourth sentence that the card leaves out. A fifth sentence about colours.")


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.stubs = fx.Stubs(self.base, {})
        self.addCleanup(self.stubs.close)
        fx.block_real_models(self)
        patch = mock.patch.dict(os.environ, self.stubs.env())
        patch.start()
        self.addCleanup(patch.stop)

    def script(self, **steps):
        self.stubs.set({k.replace("__", "/"): v for k, v in steps.items()})
        self.stubs.log.write_text("")

    def select(self, log=False, base=None, **req):
        return model_router.select({"task": TASK, "role": "coder", "deliverable": "Report paths and checks", **req}, log=log, base=base)

    def models_called(self):
        return [c["key"] for c in self.stubs.calls()]


def text(*levels):
    return [{"text": reply(*lv) if isinstance(lv, tuple) else lv} for lv in levels]


class ChainTests(ChainTestCase):
    def test_haiku_decides_with_no_thinking_no_tools_and_only_the_card(self):
        self.script(claude=text((3, "medium", "optional", None, "ordinary feature")))
        r = self.select()
        self.assertEqual((r["worker"], r["model"], r["thinking"], r["level"]), ("claude", SONNET, "medium", "mid"))
        self.assertEqual((r["decided_by"], r["source"], r["reason"]), (HAIKU, "router", "ordinary feature"))
        self.assertEqual(self.models_called(), ["claude"])
        call = self.stubs.calls("claude")[0]
        argv = call["argv"]
        self.assertEqual(call["max_thinking_tokens"], "0")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertEqual(argv[argv.index("--model") + 1], HAIKU)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("--strict-mcp-config", "--no-session-persistence", "-p"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        # no repository context: a temporary directory, and the system prompt is exactly the generated one
        self.assertFalse(Path(call["cwd"]).resolve().is_relative_to(fx.ROOT.resolve()))
        system = argv[argv.index("--system-prompt") + 1]
        self.assertEqual(system, rc.build_system(CAT))
        self.assertEqual(r["prompt_sha256"], rc.prompt_sha(system))
        self.assertEqual(r["catalog_sha"], rc.catalog_sha(CAT))
        self.assertEqual(r["catalog_version"], CAT["catalog_version"])
        # the user message is the fixed-form card: role, first sentence, a constraint line, the deliverable
        user = argv[-1]
        self.assertIn("Role: coder", user)
        self.assertIn("Task: Add pagination to the /orders endpoint with tests, following the existing list endpoints.", user)
        self.assertIn("Constraints: Do not edit the billing module.", user)
        self.assertIn("Deliverable: Report paths and checks", user)
        self.assertNotIn("fourth sentence", user)
        self.assertNotIn("colours", user)

    def test_haiku_fails_luna_answers(self):
        self.script(claude=[{"exit": 1, "text": "rate limited"}], **{"pi/" + LUNA: text((3, "high"))})
        r = self.select()
        self.assertEqual((r["decided_by"], r["model"], r["effort"]), (LUNA, SONNET, "high"))
        self.assertEqual(self.models_called(), ["claude", "pi/" + LUNA])
        argv = self.stubs.calls("pi/" + LUNA)[0]["argv"]
        self.assertEqual(argv[argv.index("--thinking") + 1], "off")
        for flag in ("--no-tools", "--no-session", "--no-context-files", "--no-extensions", "--no-skills", "--no-mcp"):
            self.assertIn(flag, argv)

    def test_both_cloud_routers_fail_qwen_answers_and_is_capped_at_level_3(self):
        self.script(claude=[{"exit": 1}], **{"pi/" + LUNA: [{"exit": 1}], "chat": text((4, "high", "required", None, "big"))})
        r = self.select()
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (QWEN, "mid", SONNET))
        self.assertTrue(any("capped router" in s for s in r["steps"]), r["steps"])
        self.assertTrue(r["substituted"])
        self.assertEqual(self.models_called(), ["claude", "pi/" + LUNA, "chat"])

    def test_all_routers_fail_falls_back_to_the_default_and_records_why(self):
        self.script(claude=[{"exit": 1, "text": "quota"}], **{"pi/" + LUNA: [{"exit": 2, "text": "auth"}],
                                                              "chat": [{"sleep": 0, "text": "garbage"}]})
        r = self.select(base=self.base, log=True)
        self.assertEqual((r["model"], r["thinking"], r["level"], r["source"], r["decided_by"]),
                         (SONNET, "medium", "mid", "router_failed_default", "fallback"))
        for needle in (HAIKU, LUNA, QWEN, "quota", "auth"):
            self.assertIn(needle, r["reason"])
        row = json.loads((self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])
        self.assertEqual((row["decided_by"], row["source"], row["resolved"]["model"]), ("fallback", "router_failed_default", SONNET))
        self.assertIn("quota", row["reason"])
        self.assertEqual([a["model"] for a in row["attempts"]], [HAIKU, LUNA, QWEN, QWEN])

    def test_invalid_reply_is_retried_once_on_the_same_router(self):
        self.script(claude=[{"text": "I think level three."}, {"text": reply(2, "low", "none", "small_edit", "small")}])
        r = self.select(role="scribe")
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (HAIKU, "small", HAIKU))
        self.assertEqual(self.models_called(), ["claude", "claude"])

    def test_invalid_twice_moves_to_the_next_router(self):
        bad = {"text": '{"level": 9, "effort": "high", "review": "none", "shape": null, "reason": "x"}'}
        self.script(claude=[bad], **{"pi/" + LUNA: text((3, "medium"))})
        r = self.select(base=self.base, log=True)
        self.assertEqual(r["decided_by"], LUNA)
        self.assertEqual(self.models_called(), ["claude", "claude", "pi/" + LUNA])
        attempts = json.loads((self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])["attempts"]
        self.assertTrue(attempts[0]["error"].startswith("invalid reply"))

    def test_a_timeout_moves_to_the_next_router_without_retry(self):
        cat = copy.deepcopy(CAT)
        cat["router"]["chain"][0]["timeout_seconds"] = 1
        path = self.base / "catalog.json"
        path.write_text(json.dumps(cat))
        self.script(claude=[{"sleep": 4, "text": reply()}], **{"pi/" + LUNA: text((3, "medium"))})
        r = model_router.select({"task": TASK, "role": "coder"}, log=False, catalog_path=path)
        self.assertEqual(r["decided_by"], LUNA)
        self.assertEqual(self.models_called(), ["claude", "pi/" + LUNA])

    def test_a_provider_marked_out_is_not_called(self):
        (self.base / "provider-health.json").write_text(json.dumps({"anthropic": {"status": "exhausted"}}))
        self.script(claude=text((3, "medium")), **{"pi/" + LUNA: text((3, "medium"))})
        r = self.select(base=self.base)
        self.assertEqual((r["decided_by"], r["model"]), (LUNA, SOL))  # authoring also moved to the equivalent on another provider
        self.assertEqual(self.models_called(), ["pi/" + LUNA])

    def test_top_level_by_router_choice_only_for_breadth(self):
        self.script(claude=text((4, "high", "required", None, "architecture across many subsystems")))
        r = self.select(role="architect")
        self.assertEqual((r["level"], r["model"], r["effort"]), ("top", OPUS, "high"))
        # a Qwen-decided route never reaches it
        self.script(claude=[{"exit": 1}], **{"pi/" + LUNA: [{"exit": 1}], "chat": text((4, "high"))})
        self.assertEqual(self.select(role="architect")["level"], "mid")


class ThinkingTests(ChainTestCase):
    """The routers that must not think (catalog thinking_off) are checked on what they report."""

    def rows(self):
        return [json.loads(line) for line in (self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()]

    def test_thinking_tokens_fail_the_attempt_retry_once_then_fail_over(self):
        self.script(claude=[{"text": reply(3, "medium", "optional", None, "x"), "thinking": 1500}], **{"pi/" + LUNA: text((3, "high"))})
        r = self.select(base=self.base, log=True)
        self.assertEqual((r["decided_by"], r["effort"]), (LUNA, "high"))
        self.assertEqual(self.models_called(), ["claude", "claude", "pi/" + LUNA])
        attempts = self.rows()[-1]["attempts"]
        self.assertEqual([(a["model"], a["ok"], a["thinking_tokens"]) for a in attempts], [(HAIKU, False, 1500), (HAIKU, False, 1500), (LUNA, True, 0)])
        self.assertIn("thinking is meant to be off", attempts[0]["error"])

    def test_a_retry_that_reports_zero_thinking_is_accepted(self):
        self.script(claude=[{"text": reply(3), "thinking": 40}, {"text": reply(3, "high", "optional", None, "ok"), "thinking": 0}])
        r = self.select(base=self.base, log=True)
        self.assertEqual((r["decided_by"], r["effort"]), (HAIKU, "high"))
        self.assertEqual(self.models_called(), ["claude", "claude"])

    def test_zero_thinking_tokens_are_the_normal_case(self):
        self.script(claude=text((3, "medium")))
        self.select(base=self.base, log=True)
        attempt = self.rows()[-1]["attempts"][0]
        self.assertEqual((attempt["thinking_tokens"], attempt["ok"], attempt.get("note")), (0, True, None))

    def test_a_cli_that_reports_no_count_is_accepted_and_the_note_is_recorded(self):
        self.script(claude=[{"text": reply(3, "medium", "optional", None, "x"), "no_count": True}])
        r = self.select(base=self.base, log=True)
        self.assertEqual(r["decided_by"], HAIKU)
        self.assertEqual(self.models_called(), ["claude"])
        attempt = self.rows()[-1]["attempts"][0]
        self.assertIsNone(attempt["thinking_tokens"])
        self.assertIn("not reported", attempt["note"])
        self.assertTrue(attempt["ok"])

    def test_a_router_not_marked_thinking_off_is_not_checked(self):
        cat = copy.deepcopy(CAT)
        cat["router"]["chain"][0].pop("thinking_off")
        path = self.base / "catalog.json"
        path.write_text(json.dumps(cat))
        self.script(claude=[{"text": reply(3), "thinking": 900}])
        r = model_router.select({"task": TASK, "role": "coder"}, log=False, catalog_path=path)
        self.assertEqual(r["decided_by"], HAIKU)

    def test_the_local_router_reporting_reasoning_tokens_fails_and_a_missing_count_is_accepted(self):
        self.script(chat=[{"text": reply(1, "low", "none", "locate_quote", "find"), "reasoning_tokens": 120}])
        with self.assertRaises(model_router.RouteStop):
            self.select(local_only=True, role="scribe", base=self.base, log=True)
        self.assertEqual(self.models_called(), ["chat", "chat"])
        self.assertIn("thinking is meant to be off", self.rows()[-1]["attempts"][0]["error"])
        self.script(chat=[{"text": reply(1, "low", "none", "locate_quote", "find"), "no_usage": True}])
        r = self.select(local_only=True, role="scribe", base=self.base, log=True)
        self.assertEqual(r["decided_by"], QWEN)
        self.assertIn("not reported", self.rows()[-1]["attempts"][0]["note"])


class DirectChatTests(ChainTestCase):
    """The local router is called straight on the local server: temperature 0, a fixed seed, reasoning off, no pi."""

    def ask_local(self, **req):
        self.script(chat=text((1, "low", "none", "locate_quote", "find")))
        return self.select(local_only=True, role="scribe", base=self.base, log=True, **req)

    def test_request_is_deterministic_and_not_through_the_cli(self):
        r = self.ask_local()
        self.assertEqual((r["decided_by"], r["model"], r["thinking"]), (QWEN, QWEN, "off"))
        self.assertEqual(self.models_called(), ["chat"])
        self.assertEqual(self.stubs.calls("pi"), [])
        call = self.stubs.calls("chat")[0]
        body = call["body"]
        self.assertEqual(call["path"], "/v1/chat/completions")
        self.assertEqual((body["temperature"], body["seed"], body["reasoning_effort"], body["stream"]), (0, 7, "none", False))
        self.assertEqual(body["model"], "qwen/qwen3.6-35b-a3b")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], rc.build_system(CAT))
        self.assertIn("Role: scribe", body["messages"][1]["content"])
        self.assertLessEqual(body["max_tokens"], 400)

    def test_the_same_card_gives_the_same_request_every_time(self):
        self.ask_local()
        first = self.stubs.calls("chat")[0]["body"]
        self.script(chat=text((1, "low", "none", "locate_quote", "find")))
        self.select(local_only=True, role="scribe")
        self.assertEqual(self.stubs.calls("chat")[0]["body"], first)

    def test_temperature_and_seed_are_catalog_data(self):
        cat = copy.deepcopy(CAT)
        cat["router"]["chain"][2].update(temperature=0.5, seed=99)
        path = self.base / "catalog.json"
        path.write_text(json.dumps(cat))
        self.script(chat=text((1, "low", "none", "locate_quote", "find")))
        model_router.select({"task": TASK, "role": "scribe", "local_only": True}, log=False, catalog_path=path)
        body = self.stubs.calls("chat")[0]["body"]
        self.assertEqual((body["temperature"], body["seed"]), (0.5, 99))

    def test_base_url_and_model_come_from_the_followup_local_configuration(self):
        local = self.base / "local.json"
        local.write_text(json.dumps({"followup": {"base_url": self.stubs.chat.url, "model": "configured/model", "ask_between": [0.25, 0.9],
                                                  "extra_body": {"reasoning_effort": "high", "top_p": 0.3}}}))
        env = dict(os.environ, TP_LOCAL_CONFIG=str(local))
        env.pop("TP_ROUTER_LLM_URL")
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.ask_local()
        self.assertEqual(r["decided_by"], QWEN)
        body = self.stubs.calls("chat")[0]["body"]
        self.assertEqual((body["model"], body["top_p"], body["temperature"], body["seed"]), ("configured/model", 0.3, 0, 7))
        # The follow-up judge's own reasoning effort never reaches a router that must run with thinking off.
        self.assertEqual(body["reasoning_effort"], "none")

    def test_the_configuration_cannot_override_the_sampling_fields(self):
        entry = next(e for e in CAT["router"]["chain"] if e.get("transport") == "direct_chat")
        config = {"local": {"followup": {"base_url": self.stubs.chat.url, "model": "m",
                                         "extra_body": {"temperature": 9, "seed": 1, "stream": True, "top_p": 0.3}}}}
        with mock.patch.dict(os.environ):
            os.environ.pop("TP_ROUTER_LLM_URL")
            self.assertEqual(llm_router.local_endpoint(entry, config)[2], {"top_p": 0.3})

    def test_without_a_followup_configuration_the_local_servers_own_entry_is_used(self):
        entry = next(e for e in CAT["router"]["chain"] if e.get("transport") == "direct_chat")
        with mock.patch.dict(os.environ):
            os.environ.pop("TP_ROUTER_LLM_URL")
            url, model, extra = llm_router.local_endpoint(entry, None)
        provider = json.loads((fx.ROOT / "agent" / "models.json").read_text())["providers"]["lmstudio"]
        self.assertEqual((url, model, extra), (provider["baseUrl"].rstrip("/"), "qwen/qwen3.6-35b-a3b", {}))

    def test_a_non_loopback_url_is_refused_and_the_router_fails(self):
        with mock.patch.dict(os.environ, {"TP_ROUTER_LLM_URL": "http://203.0.113.9:1234/v1"}):
            with self.assertRaises(model_router.RouteStop):
                self.ask_local()
        self.assertIn("loopback", self.stubs.calls() and "" or json.loads(
            (self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])["attempts"][0]["error"])
        self.assertEqual(self.models_called(), [])

    def test_a_server_error_and_a_cut_off_reply_fail_the_router(self):
        self.script(chat=[{"status": 500}])
        with self.assertRaises(model_router.RouteStop):
            self.select(local_only=True, role="scribe")
        self.script(chat=[{"text": "{", "finish": "length"}])
        with self.assertRaises(model_router.RouteStop):
            self.select(local_only=True, role="scribe")

    def test_no_router_call_is_made_by_a_cli_when_the_chain_is_the_local_router_alone(self):
        self.ask_local()
        self.assertEqual(self.stubs.calls("claude") + self.stubs.calls("pi"), [])


class ChainDataTests(ChainTestCase):
    """Order, per-router input limits and the confirm rule are catalog data: editing the catalog changes the behaviour."""

    def catalog_file(self, edit, order=fx.DEFAULT_ORDER):
        return fx.write_catalog(self.base / "catalog.json", order, edit)

    def route(self, path, **req):
        return model_router.select({"task": TASK, "role": "coder", **req}, log=False, catalog_path=path)

    def test_the_chain_order_is_the_catalog_order(self):
        path = self.catalog_file(lambda c: None, order=(LUNA, HAIKU, QWEN))
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((3, "high"))})
        r = self.route(path)
        self.assertEqual((r["decided_by"], r["effort"]), (LUNA, "high"))
        self.assertEqual(self.models_called(), ["pi/" + LUNA])
        # the local router first, then the cloud ones; its pick is still capped at mid
        path = self.catalog_file(lambda c: None, order=(QWEN, HAIKU, LUNA))
        self.script(chat=text((4, "high", "required", None, "big")), claude=text((3,)))
        r = self.route(path)
        self.assertEqual((r["decided_by"], r["level"]), (QWEN, "mid"))
        self.assertEqual(self.models_called(), ["chat"])

    def test_a_router_whose_maximum_input_is_too_small_is_skipped(self):
        path = self.catalog_file(lambda c: c["router"]["chain"][0].update(max_input_tokens=50))
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((3, "high"))})
        r = self.route(path)
        self.assertEqual(r["decided_by"], LUNA)
        self.assertEqual(self.models_called(), ["pi/" + LUNA])

    def test_the_skip_is_recorded_and_every_router_too_small_falls_back_to_the_default(self):
        def tiny(c):
            for entry in c["router"]["chain"]:
                entry["max_input_tokens"] = 50
        path = self.catalog_file(tiny)
        r = model_router.select({"task": TASK, "role": "coder"}, log=True, base=self.base, catalog_path=path)
        self.assertEqual((r["source"], r["model"]), ("router_failed_default", SONNET))
        self.assertEqual(self.models_called(), [])
        row = json.loads((self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])
        self.assertEqual([(a["model"], a["skipped"]) for a in row["attempts"]], [(HAIKU, True), (LUNA, True), (QWEN, True)])
        self.assertIn("maximum of 50", row["reason"])

    def test_confirm_above_a_level_hands_the_pick_to_the_next_router(self):
        path = self.catalog_file(lambda c: c["router"]["chain"][0].update(confirm={"above_level": "mid"}))
        self.script(claude=text((4, "high", "required", None, "big")), **{"pi/" + LUNA: text((3, "medium", "optional", None, "ordinary"))})
        r = model_router.select({"task": TASK, "role": "architect"}, log=True, base=self.base, catalog_path=path)
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (LUNA, "mid", SONNET))
        self.assertEqual(self.models_called(), ["claude", "pi/" + LUNA])
        row = json.loads((self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])
        self.assertEqual(row["confirmation"]["asked"], HAIKU)
        self.assertEqual((row["confirmation"]["confirmed_by"], row["confirmation"]["first_level"], row["confirmation"]["level"]),
                         (LUNA, 4, 3))
        # a pick at or below the level is not confirmed
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((2,))})
        self.assertEqual(self.route(path)["decided_by"], HAIKU)
        self.assertEqual(self.models_called(), ["claude"])

    def test_the_first_pick_stands_when_nobody_can_confirm_it(self):
        path = self.catalog_file(lambda c: c["router"]["chain"][0].update(confirm={"above_level": "mid"}))
        self.script(claude=text((4, "high", "required", None, "big")), **{"pi/" + LUNA: [{"exit": 1}], "chat": [{"status": 500}]})
        r = model_router.select({"task": TASK, "role": "architect"}, log=True, base=self.base, catalog_path=path)
        self.assertEqual((r["decided_by"], r["level"]), (HAIKU, "top"))
        row = json.loads((self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()[-1])
        self.assertIsNone(row["confirmation"]["confirmed_by"])
        self.assertIn("pick stands", row["confirmation"]["note"])

    def test_confirm_when_the_local_level_is_picked_for_an_unqualified_shape(self):
        path = self.catalog_file(lambda c: c["router"]["chain"][0].update(confirm={"unqualified_local": True}), order=(QWEN, HAIKU, LUNA))
        self.script(chat=text((1, "low", "none", None, "lookup")), claude=text((2, "low", "none", "small_edit", "small")))
        r = self.route(path, role="scribe")
        self.assertEqual((r["decided_by"], r["level"]), (HAIKU, "small"))
        self.assertEqual(self.models_called(), ["chat", "claude"])
        # a qualified shape is not confirmed
        self.script(chat=text((1, "low", "none", "locate_quote", "lookup")), claude=text((2,)))
        self.assertEqual(self.route(path)["decided_by"], QWEN)
        self.assertEqual(self.models_called(), ["chat"])


class ShippedChainTests(ChainTestCase):
    """The flow of the shipped chain end to end, with every router stubbed: Qwen first (capped at mid, confirm rule), then
    Haiku, then Luna. These follow the shipped order on purpose; the mechanics tests above run on a fixture catalog."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, {"TP_MODEL_CATALOG": str(fx.CATALOG_PATH)})
        patch.start()
        self.addCleanup(patch.stop)
        self.assertEqual([e["model"] for e in rc.load_catalog()["router"]["chain"]][:2], [QWEN, HAIKU])

    def rows(self):
        return [json.loads(line) for line in (self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()]

    def test_qwen_picks_small_or_mid_and_decides_alone(self):
        for pick, level, shape in ((2, "small", "small_edit"), (3, "mid", None)):
            self.script(chat=text((pick, "medium", "optional", shape, "qwen")), claude=text((4, "high")))
            r = self.select(role="scribe")
            self.assertEqual((r["decided_by"], r["level"], r["source"]), (QWEN, level, "router"))
            self.assertEqual(self.models_called(), ["chat"])

    def test_qwen_picks_above_mid_and_haikus_answer_stands(self):
        self.script(chat=text((4, "high", "required", None, "big")), claude=text((3, "medium", "optional", None, "ordinary")))
        r = self.select(base=self.base, log=True, role="architect")
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (HAIKU, "mid", SONNET))
        self.assertEqual(self.models_called(), ["chat", "claude"])
        confirmation = self.rows()[-1]["confirmation"]
        self.assertEqual((confirmation["asked"], confirmation["confirmed_by"], confirmation["first_level"], confirmation["level"]),
                         (QWEN, HAIKU, 4, 3))
        # Haiku's pick stands even when it is the one that goes up
        self.script(chat=text((4, "high")), claude=text((4, "high", "required", None, "breadth")))
        r = self.select(role="architect")
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (HAIKU, "top", OPUS))

    def test_qwen_picks_the_local_level_for_an_unqualified_shape_and_haiku_is_asked(self):
        self.script(chat=text((1, "low", "none", None, "lookup")), claude=text((2, "low", "none", "small_edit", "small")))
        r = self.select(role="scribe")
        self.assertEqual((r["decided_by"], r["level"]), (HAIKU, "small"))
        self.assertEqual(self.models_called(), ["chat", "claude"])
        # a qualified shape is not asked about
        self.script(chat=text((1, "low", "none", "locate_quote", "lookup")), claude=text((3,)))
        r = self.select(role="scribe")
        self.assertEqual((r["decided_by"], r["level"]), (QWEN, "local"))
        self.assertEqual(self.models_called(), ["chat"])

    def test_qwen_unavailable_or_over_its_input_limit_and_haiku_decides(self):
        self.script(chat=[{"status": 500}], claude=text((3, "medium", "optional", None, "ordinary")))
        r = self.select()
        self.assertEqual((r["decided_by"], r["level"]), (HAIKU, "mid"))
        self.assertEqual(self.models_called(), ["chat", "claude"])
        self.script(chat=text((3,)), claude=text((3, "medium", "optional", None, "ordinary")))
        padded = rc.build_system(rc.load_catalog()) + "x" * 60000  # about 15k tokens: over Qwen's 12000, under Haiku's 50000
        with mock.patch.object(rc, "build_system", return_value=padded):
            r = self.select(base=self.base, log=True)
        self.assertEqual((r["decided_by"], r["level"]), (HAIKU, "mid"))
        self.assertEqual(self.models_called(), ["claude"])
        self.assertTrue(self.rows()[-1]["attempts"][0]["skipped"])

    def test_both_cloud_routers_down_and_qwens_pick_is_capped_at_mid(self):
        self.script(chat=text((4, "high", "required", None, "big")), claude=[{"exit": 1}], **{"pi/" + LUNA: [{"exit": 1}]})
        r = self.select(role="architect")
        self.assertEqual((r["decided_by"], r["level"], r["model"]), (QWEN, "mid", SONNET))
        self.assertTrue(any("capped router" in s for s in r["steps"]), r["steps"])
        self.assertEqual(self.models_called(), ["chat", "claude", "pi/" + LUNA])


class NoLayaTests(ChainTestCase):
    def test_the_selection_path_never_calls_laya_or_loads_the_learned_head(self):
        import router
        self.script(claude=text((3, "medium")))
        boom = AssertionError("Laya or the learned head was used on the dispatch path")
        with mock.patch.object(router, "classify_laya", side_effect=boom), mock.patch.object(router, "load_model", side_effect=boom), \
                mock.patch.object(router, "route_laya", side_effect=boom), mock.patch.object(router, "review_task", side_effect=boom), \
                mock.patch.object(router, "dispatch", side_effect=boom), mock.patch.object(router, "follow_up_judge", side_effect=boom):
            self.assertEqual(self.select()["model"], SONNET)
            self.assertEqual(self.select(model=OPUS)["model"], OPUS)
            self.assertEqual(self.select(level="top")["model"], OPUS)

    def test_the_commands_on_the_path_name_neither_the_service_nor_its_port(self):
        for path in ("bin/tp-agent", "bin/tp-review", "lib/model_router.py", "lib/llm_router.py", "lib/route_resolve.py",
                     "lib/router_catalog.py", "lib/route_card.py"):
            body = (fx.ROOT / path).read_text()
            for needle in ("11436", "classify_laya", "typed-decisions", "load_model("):
                self.assertFalse(needle in body, "{} mentions {}".format(path, needle))


class LocalOnlyTests(ChainTestCase):
    def test_local_only_uses_only_the_local_router_and_a_local_model(self):
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((3,)), "chat": text((1, "low", "none", "locate_quote", "find"))})
        r = self.select(local_only=True, role="scribe")
        self.assertEqual((r["decided_by"], r["provider"], r["model"], r["thinking"]), (QWEN, "local", QWEN, "off"))
        self.assertEqual(self.models_called(), ["chat"])

    def test_local_only_stops_when_the_local_router_fails_and_never_calls_the_cloud(self):
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((3,)), "chat": [{"status": 500}]})
        with self.assertRaises(model_router.RouteStop) as caught:
            self.select(local_only=True, role="scribe")
        self.assertIn("no local router answered", str(caught.exception))
        self.assertEqual(self.models_called(), ["chat"])

    def test_local_only_stops_when_the_task_is_outside_the_local_shapes(self):
        self.script(**{"chat": text((3, "medium"))})
        with self.assertRaises(model_router.RouteStop):
            self.select(local_only=True)
        self.assertEqual(self.models_called(), ["chat"])


class ExplicitChoiceTests(ChainTestCase):
    def test_explicit_model_level_and_worker_choices_behave_as_constraints(self):
        self.script(claude=text((3,)), **{"pi/" + LUNA: text((3,))})
        r = self.select(model=OPUS, thinking="high", base=self.base, log=True)
        self.assertEqual((r["source"], r["model"], r["thinking"], r["decided_by"]), ("explicit", OPUS, "high", "explicit"))
        r = self.select(level="top")
        self.assertEqual((r["source"], r["model"], r["level"], r["decided_by"]), ("explicit_level", OPUS, "top", "explicit"))
        r = self.select(level="top", worker="pi", role="tester")  # an explicit level skips role caps
        self.assertEqual((r["model"], r["worker"]), (ASTRA, "pi"))
        self.assertEqual(self.models_called(), [])  # no router was asked for any of them
        rows = [json.loads(line) for line in (self.base / "evals" / model_router.SELECTIONS).read_text().splitlines()]
        self.assertEqual([x["source"] for x in rows], ["explicit"])
        self.assertEqual(rows[0]["constraints"]["model"], OPUS)


    def test_an_explicit_level_overrides_the_role_floor_but_the_routers_pick_does_not(self):
        for role in ("coder", "tester", "researcher"):
            with self.subTest(role=role):
                r = self.select(level="small", role=role)
                self.assertEqual((r["source"], r["level"], r["model"]), ("explicit_level", "small", HAIKU))
                self.assertFalse(any("floor" in step for step in r["steps"]), r["steps"])
                self.script(claude=text((2, "low", "none", "small_edit", "small")))
                self.assertEqual(self.select(role=role)["level"], "mid")  # the router's own pick is raised to the floor
                self.script(chat=text((1, "low", "none", "locate_quote", "find")))
                with self.assertRaises(model_router.RouteStop):
                    self.select(role=role, local_only=True)  # and --local-only stops rather than run a local model
    def test_explicit_effort_above_high_needs_approval(self):
        r = self.select(model=SOL, thinking="xhigh")
        self.assertEqual((r["thinking"], r["approvals"]), ("high", {"effort_above_high": False}))
        self.assertTrue(any("capped at high" in s for s in r["steps"]))
        r = self.select(model=SOL, thinking="xhigh", approve_effort=True)
        self.assertEqual((r["thinking"], r["approvals"]), ("xhigh", {"effort_above_high": True}))
        self.script(claude=text((3, "high")))
        r = self.select(thinking="max", worker="pi", approve_effort=True)
        self.assertEqual((r["model"], r["thinking"]), (SOL, "max"))

    def test_bad_explicit_requests(self):
        for req in ({"model": SONNET, "worker": "pi"}, {"model": "gpt-x", "worker": "pi"}, {"level": "strong"}, {"level": "nope"},
                    {"model": SONNET, "level": "top"}):
            with self.subTest(req=req), self.assertRaises(ValueError):
                self.select(**req)
        self.assertEqual(self.models_called(), [])

    def test_worker_pi_is_honoured_without_a_conflict_fallback(self):
        self.script(claude=text((3, "medium")))
        r = self.select(worker="pi")
        self.assertEqual((r["worker"], r["model"], r["source"], r["steps"]), ("pi", SOL, "router", []))
        self.assertEqual(r["decided_by"], HAIKU)


if __name__ == "__main__":
    unittest.main()
