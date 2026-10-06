"""The deterministic resolver (lib/route_resolve.py) against the committed model catalog, and against copies of it
edited as data only. Ported from the architect's 31-test prototype and extended for the shipped behaviour."""
import copy
import json
import sys
import unittest

import route_fixture as fx
from route_fixture import ASTRA, HAIKU, LUNA, OPUS, QWEN, SOL, SONNET

import route_resolve as rr  # noqa: E402  (route_fixture put lib on sys.path)
import router_catalog as rc  # noqa: E402

CAT = fx.fixture_catalog((HAIKU, LUNA, QWEN))  # explicit chain order; the shipped order does not matter here
ROSTER = json.loads((fx.ROOT / "config" / "router.json").read_text())["models"]


def D(key, effort="medium", review="optional", shape=None, cat=CAT):
    """A router reply asking for a level, numbered the way the router sees the shown levels."""
    return {"level": rc.shown_keys(cat).index(key) + 1, "effort": effort, "review": review, "shape": shape, "reason": "t"}


def K(key, effort="medium", review="optional", shape=None):
    """An internal request by level key (works for a hidden level too)."""
    return {"level_key": key, "effort": effort, "review": review, "shape": shape, "reason": "t"}


def R(decision, **ctx):
    health = ctx.pop("health", None)
    return rr.resolve(decision, CAT, health, ctx)


class RosterTests(unittest.TestCase):
    def test_rostered_catalog_models_match_router_config(self):
        roster = {m["model"]: m for m in ROSTER}
        catalog = {m["model"]: m for lv in CAT["levels"] for m in lv["models"]}
        for name, entry in catalog.items():
            if entry["in_roster"]:
                self.assertIn(name, roster)
                self.assertEqual((roster[name]["worker"], roster[name]["thinking"]), (entry["worker"], entry["efforts"]), name)
        self.assertEqual(sorted(set(roster) - set(catalog)), [])
        self.assertFalse(catalog["openai-codex/gpt-6.1-sol"]["in_roster"])


class ChoiceTests(unittest.TestCase):
    def test_authoring_prefers_claude_and_review_prefers_openai(self):
        r = R(D("mid"), role="coder")
        self.assertEqual((r["provider"], r["model"], r["effort"]), ("anthropic", SONNET, "medium"))
        self.assertEqual(R(D("mid"), role="coder", is_review=True)["provider"], "openai")

    def test_provider_out_uses_the_equivalent_on_another_provider(self):
        r = R(D("mid"), role="coder", health={"anthropic": {"status": "exhausted"}})
        self.assertEqual((r["level_key"], r["model"], r["steps"]), ("mid", SOL, []))
        r = R(D("top", "high"), role="architect", health={"anthropic": {"status": "limited"}})
        self.assertEqual((r["level_key"], r["model"]), ("top", ASTRA))

    def test_router_may_choose_the_top_level(self):
        r = R(D("top", "high"), role="architect")
        self.assertEqual((r["level_key"], r["model"], r["source"]), ("top", OPUS, "router"))

    def test_hidden_level_is_never_offered_or_resolved_to(self):
        self.assertEqual(rc.shown_keys(CAT), ["local", "small", "mid", "top"])
        self.assertNotIn("gpt-6.1", rc.build_system(CAT))
        with self.assertRaises(ValueError):
            rc.parse_decision(json.dumps(dict(D("top"), level=5)), CAT)
        # the router's 4th level is the top level, never the hidden one
        self.assertEqual(R(dict(D("top"), level=4), role="coder")["level_key"], "top")
        # an internal request for the hidden level is moved to a shown one, even with unrostered models allowed
        r = R(K("strong", "high"), role="coder", allow_unrostered=True)
        self.assertEqual(r["level_key"], "mid")
        self.assertIn("level is not offered: used level mid", r["steps"])
        # and an availability fallback never walks into it
        health = {"anthropic:" + SONNET: {"status": "exhausted"}, "openai:" + SOL: {"status": "exhausted"}}
        for allow in (False, True):
            r = R(D("mid"), role="coder", health=health, allow_unrostered=allow)
            self.assertNotEqual(r["level_key"], "strong")

    def test_availability_never_moves_work_up_into_the_restricted_top_level(self):
        health = {"anthropic:" + SONNET: {"status": "exhausted"}, "openai:" + SOL: {"status": "exhausted"}}
        r = R(D("mid"), role="coder", health=health)
        self.assertEqual((r["level_key"], r["degraded"], r["review"]), ("small", "down", "required"))
        self.assertIn("moved down: review forced to required", r["steps"])

    def test_top_request_falls_down_with_review_when_top_is_out(self):
        health = {"anthropic:" + OPUS: {"status": "exhausted"}, "openai:" + ASTRA: {"status": "exhausted"}}
        r = R(D("top", "high"), role="architect", health=health)
        self.assertEqual((r["level_key"], r["degraded"], r["review"]), ("mid", "down", "required"))

    def test_nothing_in_the_request_raises_a_level(self):
        # the resolver sees only the router's level, the role and facts; security roles get a floor, never the top
        r = R(D("small", "low", "none"), role="vulnerability-reviewer")
        self.assertEqual((r["level_key"], r["effort"]), ("mid", "high"))
        r = R(D("mid", "medium", "required"), role="coder")
        self.assertEqual((r["level_key"], r["review"]), ("mid", "required"))

    def test_all_cloud_down_stops_above_light_work(self):
        health = {"anthropic": {"status": "exhausted"}, "openai": {"status": "exhausted"}}
        self.assertEqual(R(D("mid"), role="coder", health=health)["source"], "stop_and_ask")
        r = R(D("local", "low", "none", "locate_quote"), role="researcher", health=health)
        self.assertEqual(r["provider"], "local")

    def test_lowest_level_needs_a_qualified_shape(self):
        for shape in (None, "small_edit"):
            r = R(D("local", "low", "none", shape), role="tester")
            self.assertEqual(r["level_key"], "small")
            self.assertIn("shape not qualified for the lowest level: moved to level small", r["steps"])
        r = R(D("local", "high", "none", "locate_quote"), role="researcher")
        self.assertEqual((r["provider"], r["model"], r["effort"]), ("local", QWEN, "off"))

    def test_local_down_or_too_large_moves_up_never_down_into_local(self):
        r = R(D("local", "low", "none", "locate_quote"), role="researcher", health={"local": {"status": "exhausted"}})
        self.assertEqual(r["level_key"], "small")
        r = R(D("local", "low", "none", "locate_quote"), role="researcher", est_tokens=100000)
        self.assertEqual(r["level_key"], "small")

    def test_small_context_model_only_for_small_packets(self):
        health = {"local:" + QWEN: {"status": "limited"}}
        small = R(D("local", "low", "none", "locate_quote"), role="researcher", health=health, est_tokens=2000)
        self.assertEqual(small["model"], "slotstream/qwen3.8-flash-next:4bit")
        big = R(D("local", "low", "none", "locate_quote"), role="researcher", health=health, est_tokens=12000)
        self.assertEqual(big["level_key"], "small")

    def test_worker_is_a_real_constraint(self):
        r = R(D("mid"), role="coder", require_worker="pi")
        self.assertEqual((r["worker"], r["model"]), ("pi", SOL))
        r = R(D("mid"), role="coder", require_worker="claude", health={"anthropic": {"status": "exhausted"}})
        self.assertEqual(r["source"], "stop_and_ask")

    def test_no_router_reply_defaults_to_the_catalog_default_not_a_premium_model(self):
        r = R(None, role="coder")
        self.assertEqual((r["source"], r["level_key"], r["model"], r["effort"]), ("router_failed_default", "mid", SONNET, "medium"))

    def test_local_only_work_needs_a_local_shape(self):
        self.assertEqual(R(D("mid"), role="coder", local_only=True)["source"], "stop_and_ask")
        r = R(D("local", "low", "none", "count_enumerate"), role="researcher", local_only=True)
        self.assertEqual(r["provider"], "local")
        self.assertEqual(R(None, role="coder", local_only=True)["source"], "stop_and_ask")

    def test_router_chain_order_health_and_local_only(self):
        pick = lambda **kw: (rr.choose_router(CAT, **kw) or {}).get("model")
        self.assertEqual(pick(), HAIKU)
        self.assertEqual(pick(health={"anthropic": {"status": "limited"}}), LUNA)
        self.assertEqual(pick(health={"anthropic": {"status": "limited"}, "openai": {"status": "exhausted"}}), QWEN)
        self.assertEqual(pick(local_only=True), QWEN)
        self.assertIsNone(pick(health={"local": {"status": "exhausted"}}, local_only=True))
        self.assertEqual(rc.lowest_effort(CAT, rr.choose_router(CAT, {"anthropic": {"status": "limited"}})), "off")
        self.assertEqual(rc.lowest_effort(CAT, rr.choose_router(CAT)), "low")

    def test_decided_cap_limits_a_local_routers_pick(self):
        r = rr.resolve(D("top", "high"), CAT, ctx={"role": "coder", "decided_cap": "mid"})
        self.assertEqual(r["level_key"], "mid")
        self.assertIn("decided by a capped router: level mid", r["steps"])


class EffortTests(unittest.TestCase):
    def test_effort_above_high_is_cut_without_approval(self):
        r = R(D("mid", "high"), role="coder", require_worker="pi", explicit_effort="max")
        self.assertEqual(r["effort"], "high")
        self.assertIn("effort max capped at high (no approval recorded)", r["steps"])
        self.assertFalse(r["approvals"]["effort_above_high"])

    def test_approval_allows_effort_above_high_on_models_that_have_it(self):
        ctx = dict(role="coder", require_worker="pi", explicit_effort="max", approvals={"effort_above_high": True})
        r = R(D("mid", "high"), **ctx)
        self.assertEqual((r["model"], r["effort"], r["steps"]), (SOL, "max", []))
        r = R(D("mid", "high"), **dict(ctx, require_worker="claude"))
        self.assertEqual((r["model"], r["effort"]), (SONNET, "high"))  # Sonnet has nothing above high
        self.assertTrue(any("not allowed" in s for s in r["steps"]))

    def test_effort_is_mapped_onto_the_models_allowed_list(self):
        r = R(D("local", "high", "none", "locate_quote"), role="researcher")
        self.assertEqual(r["effort"], "off")
        self.assertEqual(R(D("small", "low", "none"), role="coder")["effort"], "low")

    def test_explicit_choice_wins_but_effort_is_still_clamped(self):
        ctx = dict(explicit=dict(model=OPUS, worker="claude", thinking="high"))
        r = R(D("local", "low", "none", "locate_quote"), **ctx)
        self.assertEqual((r["source"], r["model"], r["effort"], r["level_key"]), ("explicit", OPUS, "high", "top"))
        r = R(None, explicit=dict(model=SOL, worker="pi", thinking="xhigh"))
        self.assertEqual(r["effort"], "high")
        r = R(None, explicit=dict(model=SOL, worker="pi", thinking="xhigh"), approvals={"effort_above_high": True})
        self.assertEqual((r["effort"], r["steps"]), ("xhigh", []))

    def test_role_floors_and_caps(self):
        r = R(D("small", "low"), role="vulnerability-reviewer")
        self.assertEqual((r["level_key"], r["effort"]), ("mid", "high"))
        # a cap on the hidden level resolves to the highest shown level below it, so the top level stays out of reach
        r = R(D("top", "high"), role="code-reviewer")
        self.assertEqual(r["level_key"], "mid")
        self.assertIn("role code-reviewer cap: level mid", r["steps"])
        r = R(D("top", "high"), role="tester")
        self.assertEqual(r["level_key"], "mid")
        # an explicit level or an escalation skips caps
        r = rr.resolve(K("top", "high"), CAT, ctx={"role": "tester", "skip_caps": True})
        self.assertEqual(r["level_key"], "top")

    def test_code_and_design_roles_stay_at_mid_or_above(self):
        # A tidy task description is not a sign of easy work: these roles do not go to a small or local model.
        for role in ("architect", "infrastructure", "coder"):
            for asked in ("small", "local"):
                with self.subTest(role=role, asked=asked):
                    r = R(D(asked, "low", shape="locate_quote" if asked == "local" else None), role=role)
                    self.assertEqual(r["level_key"], "mid")
                    self.assertIn("role {} floor: level mid".format(role), r["steps"])
        # The one waiver: a coder's small edit to a named file with a check command.
        r = R(D("small", "low", shape="small_edit"), role="coder")
        self.assertEqual(r["level_key"], "small")
        self.assertFalse(any("floor" in step for step in r["steps"]))
        # The waiver is the coder's, not every role's, and other shapes do not earn it.
        self.assertEqual(R(D("small", "low", shape="small_edit"), role="architect")["level_key"], "mid")
        self.assertEqual(R(D("small", "low", shape="run_named_checks"), role="coder")["level_key"], "mid")
        # Roles without a floor still go down: research and test work.
        self.assertEqual(R(D("small", "low"), role="researcher")["level_key"], "small")
        self.assertEqual(R(D("small", "low"), role="tester")["level_key"], "small")

    def test_protected_paths_keep_guard_code_at_mid_with_high_effort(self):
        guard = ["lib/sensitive_read.py"]
        # The small-edit waiver does not apply: the path floor comes after the role floor.
        r = R(D("small", "low", shape="small_edit"), role="coder", paths=guard)
        self.assertEqual((r["level_key"], r["effort"]), ("mid", "high"))
        self.assertIn("protected path lib/sensitive_read.py: level mid", r["steps"])
        self.assertIn("protected path lib/sensitive_read.py: effort high", r["steps"])
        # An absolute path, a glob pattern and a hooks file all match.
        for path in ("/repo/bin/tp-sensitive-read", "lib/redact_hook.py", "claude/plugins/tmux-pi/hooks/hooks.json"):
            with self.subTest(path=path):
                self.assertEqual(R(D("small", "low", shape="small_edit"), role="coder", paths=[path])["level_key"], "mid")
        # Other files keep the waiver, and a similar name is not a match.
        for path in ("bin/tp-list", "lib/sensitive_reader_notes.py", "docs/redact.md"):
            with self.subTest(path=path):
                self.assertEqual(R(D("small", "low", shape="small_edit"), role="coder", paths=[path])["level_key"], "small")
        # The rule is for roles that change the code: reading it for a lookup is not held to it.
        r = R(D("local", "low", "none", "locate_quote"), role="researcher", paths=guard)
        self.assertEqual(r["level_key"], "local")
        # It never raises above what was asked, and never into the top level.
        r = R(D("mid", "high"), role="coder", paths=guard)
        self.assertEqual((r["level_key"], r["effort"]), ("mid", "high"))
        self.assertFalse(any("protected path" in step for step in r["steps"]))

    def test_review_is_the_larger_of_router_and_policy(self):
        self.assertEqual(R(D("mid", "medium", "none"), role="coder")["review"], "optional")
        self.assertEqual(R(D("mid", "medium", "required"), role="researcher")["review"], "required")
        self.assertEqual(R(D("mid", "medium", "none"), role="researcher")["review"], "none")
        self.assertEqual(R(D("mid", "high", "none"), role="coder")["review"], "required")
        self.assertEqual(R(D("small", "low", "none"), role="coder", files_touched=5)["review"], "required")

    def test_review_policy_facts(self):
        pol = lambda role, level, effort="medium", files=None: rr.review_policy(CAT, role, level, effort, files)
        # the need is the independent maximum of role, level, effort and files: a read-only role does not skip the others
        self.assertEqual(pol("researcher", "mid", "medium"), "none")
        self.assertEqual(pol("researcher", "mid", "high"), "required")
        self.assertEqual(pol("researcher", "small", "high"), "none")  # below the level the effort rule starts at
        self.assertEqual(pol("researcher", "mid", "medium", 1), "optional")
        self.assertEqual(pol("coder", "small"), "optional")
        self.assertEqual(pol("coder", "small", files=4), "required")
        self.assertEqual(pol("coder", "top"), "required")
        self.assertEqual(pol("coder", "mid", "high"), "required")
        self.assertEqual(pol("tester", "top", files=None), "required")
        self.assertEqual(pol("researcher", "top", "low"), "required")
        self.assertEqual(pol("tester", "mid", "high"), "required")
        self.assertEqual(pol("code-reviewer", "mid", "medium"), "none")
        self.assertEqual(pol("tester", "small", "low", 4), "required")
        self.assertEqual(pol("tester", "small", "low", 1), "optional")


def X(model, worker=None, thinking=None, **ctx):
    """An explicit model choice through resolve."""
    health = ctx.pop("health", None)
    return rr.resolve(None, CAT, health, dict(ctx, explicit={"model": model, "worker": worker, "thinking": thinking}))


class ExplicitValidationTests(unittest.TestCase):
    def rejected(self, needle, *args, **kw):
        with self.assertRaises(rr.ExplicitError) as caught:
            X(*args, **kw)
        self.assertIn(needle, str(caught.exception))

    def test_a_valid_pair_wins(self):
        for model, worker in ((SONNET, "claude"), (SOL, "pi"), (OPUS, "claude"), (ASTRA, "pi"), (QWEN, "pi"), (HAIKU, None)):
            r = X(model, worker)
            self.assertEqual((r["model"], r["source"]), (model, "explicit"))
            self.assertIn(r["worker"], ("pi", "claude"))

    def test_unknown_model_is_rejected_with_the_catalog_list(self):
        self.rejected("not in the model catalog", "madeup/nonexistent-model")
        self.rejected(SONNET, "madeup/nonexistent-model")  # the message lists what is valid

    def test_hidden_and_unrostered_models_are_rejected(self):
        self.rejected("not offered", "openai-codex/gpt-6.1-sol")
        cat = copy.deepcopy(CAT)
        for lv in cat["levels"]:
            for m in lv["models"]:
                if m["model"] == SOL:
                    m["in_roster"] = False
        with self.assertRaises(rr.ExplicitError) as caught:
            rr.resolve(None, cat, None, {"explicit": {"model": SOL, "worker": "pi"}})
        self.assertIn("not in the roster", str(caught.exception))

    def test_effort_the_model_does_not_allow_is_rejected(self):
        self.rejected("does not allow effort 'off'", HAIKU, None, "off")
        self.rejected("does not allow effort 'high'", QWEN, "pi", "high")
        self.rejected("does not allow effort 'minimal'", SONNET, "claude", "minimal")
        self.assertEqual(X(SOL, "pi", "off")["effort"], "off")
        self.assertEqual(X(QWEN, "pi", "off")["effort"], "off")

    def test_default_effort_is_mapped_onto_what_the_model_allows(self):
        r = X(QWEN, "pi")
        self.assertEqual(r["effort"], "off")  # medium is not allowed on Qwen; its level maps it to off
        self.assertEqual(X(SONNET, "claude")["effort"], "medium")

    def test_a_packet_that_does_not_fit_is_rejected(self):
        self.rejected("does not fit", HAIKU, "claude", est_tokens=150000)
        self.rejected("over", "slotstream/qwen3.8-flash-next:4bit", "pi", est_tokens=9000)
        self.assertEqual(X(SONNET, "claude", est_tokens=150000)["model"], SONNET)

    def test_worker_model_mismatch_is_rejected(self):
        self.rejected("belongs to claude, not pi", SONNET, "pi")
        self.rejected("belongs to pi", SOL, "claude")

    def test_a_provider_marked_out_is_rejected(self):
        self.rejected("marked unavailable", SONNET, "claude", health={"anthropic": {"status": "exhausted"}})
        self.rejected("marked unavailable", SONNET, "claude", health={"anthropic:" + SONNET: {"status": "down"}})

    def test_local_only_rejects_every_cloud_model_and_keeps_local_ones(self):
        for model, worker in ((SONNET, "claude"), (SOL, "pi"), (OPUS, "claude"), (HAIKU, None)):
            self.rejected("local-only", model, worker, local_only=True)
        self.assertEqual(X(QWEN, "pi", local_only=True)["model"], QWEN)

    def test_explicit_effort_is_still_capped_at_the_ceiling_without_approval(self):
        r = X(SOL, "pi", "max")
        self.assertEqual((r["effort"], any("capped" in s for s in r["steps"])), ("high", True))
        self.assertEqual(X(SOL, "pi", "max", approvals={"effort_above_high": True})["effort"], "max")

    def test_explicit_review_need_follows_level_and_effort_for_a_read_only_role(self):
        self.assertEqual(X(OPUS, "claude", "low", role="tester")["review"], "required")
        self.assertEqual(X(SONNET, "claude", "high", role="researcher")["review"], "required")
        self.assertEqual(X(SONNET, "claude", "medium", role="researcher")["review"], "none")


class LocalOnlyResolveTests(unittest.TestCase):
    def test_an_explicit_level_without_a_local_model_is_rejected(self):
        with self.assertRaises(rr.ExplicitError) as caught:
            rr.resolve(K("top", "high") | {"source": "explicit_level"}, CAT, None, {"local_only": True, "skip_caps": True})
        self.assertIn("local-only", str(caught.exception))
        r = rr.resolve(K("local", "low") | {"source": "explicit_level"}, CAT, None, {"local_only": True, "skip_caps": True})
        self.assertEqual(r["model"], QWEN)

    def test_escalation_of_a_local_only_task_never_reaches_a_cloud_model(self):
        prior = {"level_key": "local", "effort_abstract": "medium", "model": QWEN, "review": "optional", "decided_by": None}
        e1 = rr.escalate(prior, CAT, ctx={"local_only": True})
        self.assertEqual((e1["source"], e1["provider"]), ("escalation_effort_up", "local"))
        e2 = rr.escalate(dict(prior, effort_abstract="high"), CAT, ctx={"local_only": True})
        self.assertEqual(e2["source"], "stop_and_ask")
        self.assertIn("local-only", e2["reason"])


class RedirectTests(unittest.TestCase):
    def check(self, model, worker, thinking, may_enter_restricted=False, **ctx):
        return rr.redirect_problems(CAT, {"worker": worker, "model": model, "thinking": thinking}, ctx, ctx.pop("health", {}),
                                    may_enter_restricted)

    def test_an_allowed_redirect_has_no_problem(self):
        self.assertEqual(self.check(SOL, "pi", "medium")[0], [])

    def test_each_constraint_is_re_applied(self):
        cases = [
            (OPUS, "claude", "high", {}, "restricted"),
            (SONNET, "claude", "medium", {"local_only": True}, "local-only"),
            ("madeup/model", "pi", "low", {}, "not in the model catalog"),
            ("openai-codex/gpt-6.1-sol", "pi", "high", {}, "not offered"),
            (SONNET, "claude", "medium", {"health": {"anthropic": {"status": "exhausted"}}}, "marked unavailable"),
            (HAIKU, "claude", "high", {"est_tokens": 150000}, "does not fit"),
            (QWEN, "pi", "high", {}, "not allowed"),
        ]
        for model, worker, thinking, ctx, needle in cases:
            with self.subTest(model=model):
                problems, _ = self.check(model, worker, thinking, **ctx)
                self.assertTrue(any(needle in p for p in problems), problems)

    def test_restricted_is_allowed_only_when_the_route_was_already_there(self):
        self.assertEqual(self.check(OPUS, "claude", "high", True)[0], [])

    def test_effort_is_clamped_unless_approved(self):
        self.assertEqual(self.check(SOL, "pi", "max")[1], "high")
        self.assertEqual(self.check(SOL, "pi", "max", approvals={"effort_above_high": True})[1], "max")


class ConfirmTests(unittest.TestCase):
    def entry(self, **rule):
        return {"model": "m", "confirm": rule}

    def test_above_level_and_unqualified_local_rules(self):
        above = self.entry(above_level="mid")
        self.assertIsNone(rr.confirm_reason(CAT, above, D("mid")))
        self.assertIn("above mid", rr.confirm_reason(CAT, above, D("top")))
        local = self.entry(unqualified_local=True)
        self.assertIsNone(rr.confirm_reason(CAT, local, D("local", shape="locate_quote")))
        self.assertIn("not qualified", rr.confirm_reason(CAT, local, D("local", shape="small_edit")))
        self.assertIn("not qualified", rr.confirm_reason(CAT, local, D("local")))
        self.assertIsNone(rr.confirm_reason(CAT, local, D("mid")))
        self.assertIsNone(rr.confirm_reason(CAT, {"model": "m"}, D("top")))


class EscalationTests(unittest.TestCase):
    def step(self, rec, **kw):
        return rr.escalate(rec, CAT, ctx=dict(role="coder"), approvals=kw.get("approvals"))

    def test_effort_steps_before_level_then_stop_and_never_xhigh_or_max(self):
        r = R(D("mid", "medium"), role="coder")
        e1 = self.step(r)
        self.assertEqual((e1["level_key"], e1["effort"], e1["source"]), ("mid", "high", "escalation_effort_up"))
        e2 = self.step(e1)
        self.assertEqual((e2["level_key"], e2["effort_abstract"], e2["source"]), ("top", "high", "escalation_level_up"))
        e3 = self.step(e2)
        self.assertEqual(e3["source"], "stop_and_ask")
        for e in (e1, e2):
            self.assertNotIn(e["effort"], ("xhigh", "max"))

    def test_escalation_skips_the_hidden_level_and_role_caps(self):
        r = R(D("mid", "high"), role="tester")
        e = rr.escalate(r, CAT, ctx={"role": "tester"})
        self.assertEqual(e["level_key"], "top")

    def test_approval_lets_effort_rise_above_high_before_the_level(self):
        r = R(D("mid", "high"), role="coder", require_worker="pi")
        ctx = dict(role="coder", require_worker="pi")
        e1 = rr.escalate(r, CAT, ctx=ctx, approvals={"effort_above_high": True})
        self.assertEqual((e1["level_key"], e1["effort"], e1["source"]), ("mid", "xhigh", "escalation_effort_up"))
        e2 = rr.escalate(e1, CAT, ctx=ctx, approvals={"effort_above_high": True})
        self.assertEqual((e2["level_key"], e2["effort"]), ("mid", "max"))
        e3 = rr.escalate(e2, CAT, ctx=ctx, approvals={"effort_above_high": True})
        self.assertEqual((e3["level_key"], e3["source"]), ("top", "escalation_level_up"))

    def test_top_level_stops_and_asks(self):
        r = R(D("top", "high"), role="architect")
        self.assertEqual(self.step(r)["source"], "stop_and_ask")


class ReviewerTests(unittest.TestCase):
    def pick(self, provider, level, health=None, review="required", **ctx):
        return rr.pick_reviewer(provider, level, CAT, health, review, ctx)

    def test_reviewer_is_on_another_provider_gpt_first_for_claude_and_qwen_work(self):
        r = self.pick("anthropic", "mid")
        self.assertEqual((r["provider"], r["model"], r["independence"]), ("openai", SOL, "different_provider"))
        self.assertEqual(self.pick("local", "local")["provider"], "openai")
        r = self.pick("openai", "mid")
        self.assertEqual((r["provider"], r["model"]), ("anthropic", SONNET))

    def test_reviewer_level_is_at_least_mid_and_never_the_restricted_level(self):
        self.assertEqual(self.pick("anthropic", "small")["level_key"], "mid")
        self.assertEqual(self.pick("local", "local")["level_key"], "mid")
        r = self.pick("anthropic", "top")
        self.assertEqual(r["level_key"], "mid")
        r = self.pick("anthropic", "mid", level_key="top")  # an explicit request may ask for it
        self.assertEqual(r["level_key"], "top")

    def test_stop_and_ask_when_a_required_review_has_no_independent_provider(self):
        health = {"openai": {"status": "exhausted"}}
        r = self.pick("anthropic", "mid", health, "required")
        self.assertEqual(r["source"], "stop_and_ask")
        self.assertIn("no independent reviewer", r["reason"])
        r = self.pick("anthropic", "mid", health, "optional")
        self.assertEqual((r["independence"], r["provider"]), ("same_provider", "anthropic"))

    def test_local_only_work_is_never_sent_to_a_cloud_reviewer(self):
        r = self.pick("local", "local", None, "optional", local_only=True)
        self.assertEqual(r["source"], "stop_and_ask")


class DataOnlyChangeTests(unittest.TestCase):
    def test_adding_a_level_and_a_model_changes_behaviour_without_code(self):
        cat = copy.deepcopy(CAT)
        small = next(lv for lv in cat["levels"] if lv["key"] == "small")
        small["models"].append({"provider": "openrouter", "worker": "pi", "model": "openrouter/acme/flash-9", "efforts": ["low", "medium"],
                                "context_window": 64000, "max_output": 4000, "in_roster": True})
        mid = next(lv for lv in cat["levels"] if lv["key"] == "mid")
        plus = copy.deepcopy(mid)
        plus.update(key="mid_plus", name="Acme XL", rank=35,
                    models=[{"provider": "openrouter", "worker": "pi", "model": "openrouter/acme/coder-xl-9", "efforts": ["low", "medium", "high"],
                             "context_window": 200000, "max_output": 8000, "in_roster": True}])
        cat["levels"].insert(cat["levels"].index(mid) + 1, plus)
        rc.validate_catalog(cat)
        self.assertEqual(rc.shown_keys(cat), ["local", "small", "mid", "mid_plus", "top"])
        prompt = rc.build_system(cat)
        self.assertIn("Level 4 - Acme XL", prompt)
        self.assertIn("level 5 (Opus / GPT-6 astra)", prompt)
        self.assertIn("openrouter: coder-xl-9", prompt)
        d = {"level": 4, "effort": "medium", "review": "optional", "shape": None, "reason": "t"}
        r = rr.resolve(d, cat, ctx={"role": "coder"})
        self.assertEqual((r["level_key"], r["model"]), ("mid_plus", "openrouter/acme/coder-xl-9"))
        # the same reply number meant the top level before, and still resolves with the unchanged code
        self.assertEqual(rr.resolve(dict(d, level=5), cat, ctx={"role": "coder"})["level_key"], "top")
        # a new model at an existing level is used when it is the one available
        health = {"anthropic": {"status": "exhausted"}, "openai": {"status": "exhausted"}}
        r = rr.resolve(dict(d, level=2, effort="low"), cat, health, {"role": "tester"})
        self.assertEqual(r["model"], "openrouter/acme/flash-9")
        # the original catalog's behaviour is unchanged
        self.assertEqual(rr.resolve(D("mid"), CAT, ctx={"role": "coder"})["model"], SONNET)

    def test_unhiding_a_level_offers_it_and_renumbers(self):
        cat = copy.deepcopy(CAT)
        next(lv for lv in cat["levels"] if lv["key"] == "strong").pop("shown")
        self.assertEqual(rc.shown_keys(cat), ["local", "small", "mid", "strong", "top"])
        r = rr.resolve({"level": 4, "effort": "high", "review": "none", "shape": None, "reason": "t"}, cat,
                       ctx={"role": "coder", "allow_unrostered": True})
        self.assertEqual(r["model"], "openai-codex/gpt-6.1-sol")
        # unrostered, the level has no model: the request moves up only if the next level is not restricted, else down
        r = rr.resolve({"level": 4, "effort": "high", "review": "none", "shape": None, "reason": "t"}, cat, ctx={"role": "coder"})
        self.assertEqual((r["level_key"], r["degraded"]), ("mid", "down"))


if __name__ == "__main__":
    unittest.main()
