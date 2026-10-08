"""Regression tests for the two router follow-ups: the reviewer minimum bounds availability, and a redirect after an
explicit low level is checked against the route's effective bound (not the role's full floor)."""
import copy
import unittest

import route_fixture as fx
from route_fixture import HAIKU, LUNA, QWEN, SOL, SONNET

import route_resolve as rr  # noqa: E402  (route_fixture put lib on sys.path)

CAT = fx.fixture_catalog((HAIKU, LUNA, QWEN))
BOTH_MID_OUT = {"anthropic": {"status": "exhausted"}, "openai": {"status": "exhausted"}}


def K(key, source="requested"):
    return {"level_key": key, "effort": "medium", "review": "optional", "shape": None, "reason": "t", "source": source}


MID_OUT = {"anthropic:" + SONNET: {"status": "exhausted"}, "openai:" + SOL: {"status": "exhausted"}}


class ReviewerMinimumTests(unittest.TestCase):
    def test_no_reviewer_falls_below_mid_when_both_mid_models_are_unavailable(self):
        for review in ("required", "optional"):
            for author in ("anthropic", "openai"):
                with self.subTest(review=review, author=author):
                    out = rr.pick_reviewer(author, "mid", CAT, MID_OUT, review, {})
                    self.assertEqual(out["source"], "stop_and_ask", out)
                    self.assertIn("mid minimum for the role", out["reason"])

    def test_the_small_models_are_usable_so_only_the_minimum_stops_it(self):
        self.assertEqual(rr.candidates_at(CAT, CAT["levels"][1], {"is_review": True}, MID_OUT) != [], True)

    def test_a_mid_reviewer_is_still_picked_when_one_mid_model_is_available(self):
        out = rr.pick_reviewer("anthropic", "mid", CAT, {}, "required", {})
        self.assertEqual((out["level_key"], out["model"]), ("mid", SOL))
        out = rr.pick_reviewer("anthropic", "mid", CAT, {"openai:" + SOL: {"status": "exhausted"}}, "optional", {})
        self.assertEqual((out["level_key"], out["model"], out["independence"]), ("mid", SONNET, "same_provider"))


class ExplicitReviewerLevelTests(unittest.TestCase):
    """Orchestrator decision (not the owner's): an explicit reviewer level lowers the reviewer minimum only as far as
    it asked, as an explicit --level does for a role floor; availability never goes below it. No level: mid stands."""

    def test_explicit_small_with_both_mid_models_out_selects_small_and_records_the_lowered_bound(self):
        out = rr.pick_reviewer("anthropic", "mid", CAT, MID_OUT, "required", {"level_key": "small"})
        self.assertNotEqual(out["source"], "stop_and_ask", out)
        self.assertEqual(out["level_key"], "small")
        self.assertEqual(out["route_bound"][0], 1)
        self.assertNotIn("mid minimum", out["route_bound"][1] or "")

    def test_explicit_local_with_no_local_model_moves_up_never_down(self):
        health = {"local:" + m["model"]: {"status": "exhausted"} for m in CAT["levels"][0]["models"]}
        out = rr.pick_reviewer("anthropic", "mid", CAT, health, "optional", {"level_key": "local", "local_only": False})
        self.assertNotEqual(out["source"], "stop_and_ask", out)
        self.assertEqual(out["level_key"], "small")
        self.assertEqual(out["route_bound"][0], 1)  # local needs a qualified shape: the level entered is small, as for a role

    def test_explicit_small_never_moves_below_small(self):
        health = dict(MID_OUT, **{"openai:" + LUNA: {"status": "exhausted"}, "anthropic:" + HAIKU: {"status": "exhausted"}})
        out = rr.pick_reviewer("anthropic", "mid", CAT, health, "required", {"level_key": "small"})
        self.assertNotEqual(out.get("level_key"), "local", out)

    def test_no_explicit_level_still_stops(self):
        out = rr.pick_reviewer("anthropic", "mid", CAT, MID_OUT, "required", {})
        self.assertEqual(out["source"], "stop_and_ask")


def _out(level_key):
    """Health marking every model of one level exhausted."""
    return {"{}:{}".format(m["provider"], m["model"]): {"status": "exhausted"}
            for lv in CAT["levels"] if lv["key"] == level_key for m in lv["models"]}


def _out_in(cat, level_key):
    return {"{}:{}".format(m["provider"], m["model"]): {"status": "exhausted"}
            for lv in cat["levels"] if lv["key"] == level_key for m in lv["models"]}


def _shape(rec):
    return (rec["source"] == "stop_and_ask", rec.get("level_key"), rec.get("degraded"), (rec.get("route_bound") or (None,))[0])


def _strong_catalog():
    """The fixture catalog with the strong level offered and its (openai-only) model rostered, so it is really usable."""
    cat = copy.deepcopy(CAT)
    for lv in cat["levels"]:
        if lv["key"] == "strong":
            lv["shown"] = True
            for m in lv["models"]:
                m["in_roster"] = True
    return cat


STRONG_CAT = _strong_catalog()


def _role(level, cat, health, author, shape=None):
    request = dict(K(level, "explicit_level"), shape=shape)
    return rr.resolve(request, cat, health,
                      {"role": "coder", "skip_caps": True, "is_review": True, "exclude_providers": (author,)})


class ExplicitLevelTableTests(unittest.TestCase):
    """Every explicit level, with its models available and unavailable, for both author providers: the reviewer route
    and the coder role route (mid strict floor) must agree, so the two cannot drift. Columns: level asked, level
    available, expected (stopped, level used, degraded, recorded bound index). An unavailable explicit top moves down
    to the floor in both routes (marked degraded down, review forced); whether it should stop is the orchestrator's call.
    `local` and `strong` have their own tests below: the fixture cannot make them usable here."""

    TABLE = [
        ("small", True, (False, "small", None, 1)), ("small", False, (False, "mid", "up", 1)),
        ("mid", True, (False, "mid", None, 2)), ("mid", False, (True, None, None, 2)),
        ("top", True, (False, "top", None, 2)), ("top", False, (False, "mid", "down", 2)),
    ]

    def test_reviewer_and_role_routes_agree_for_every_explicit_level(self):
        for author in ("anthropic", "openai"):
            for level, available, expected in self.TABLE:
                with self.subTest(author=author, level=level, available=available):
                    health = {} if available else _out(level)
                    review = rr.pick_reviewer(author, "mid", CAT, health, "required", {"level_key": level})
                    role = _role(level, CAT, health, author)
                    self.assertEqual(_shape(review), expected, review)
                    self.assertEqual(_shape(role), expected, role)
                    if not expected[0]:
                        self.assertNotEqual(review["provider"], author)

    def test_strong_available_and_unavailable_with_the_level_offered(self):
        # strong has one model, on openai. For an anthropic author it is usable; for an openai author it is the
        # author's own provider, so it is excluded in both routes and the level behaves as unavailable.
        for author, available, expected in [
            ("anthropic", True, (False, "strong", None, 2)), ("anthropic", False, (False, "mid", "down", 2)),
            ("openai", True, (False, "mid", "down", 2)), ("openai", False, (False, "mid", "down", 2)),
        ]:
            with self.subTest(author=author, available=available):
                health = {} if available else _out_in(STRONG_CAT, "strong")
                review = rr.pick_reviewer(author, "mid", STRONG_CAT, health, "required", {"level_key": "strong"})
                role = _role("strong", STRONG_CAT, health, author)
                self.assertEqual(_shape(review), expected, review)
                self.assertEqual(_shape(role), expected, role)

    def test_strong_is_not_offered_in_the_shipped_catalog_so_it_resolves_to_mid(self):
        for author in ("anthropic", "openai"):
            review = rr.pick_reviewer(author, "mid", CAT, {}, "required", {"level_key": "strong"})
            self.assertEqual(_shape(review), (False, "mid", None, 2), review)

    def test_local_with_a_qualified_shape_is_usable_on_the_role_route_and_moves_up_when_unavailable(self):
        # local is entered only with a qualified shape, which pick_reviewer never passes (shape None): an explicit
        # reviewer level local therefore always enters at small, available or not (covered by the next test).
        self.assertEqual(_shape(_role("local", CAT, {}, "anthropic", "locate_quote")), (False, "local", None, 0))
        self.assertEqual(_shape(_role("local", CAT, _out("local"), "anthropic", "locate_quote")), (False, "small", "up", 0))

    def test_explicit_reviewer_local_enters_at_small_whether_or_not_local_models_are_available(self):
        for author in ("anthropic", "openai"):
            for health in ({}, _out("local")):
                with self.subTest(author=author, local_out=bool(health)):
                    review = rr.pick_reviewer(author, "mid", CAT, health, "required", {"level_key": "local"})
                    self.assertEqual(_shape(review), (False, "small", None, 1), review)
                    self.assertEqual(_shape(_role("local", CAT, health, author)), _shape(review))


class RedirectAfterExplicitLevelTests(unittest.TestCase):
    ctx = {"role": "coder", "skip_caps": True}

    def test_resolve_records_the_lowered_bound_and_a_same_level_redirect_is_allowed(self):
        rec = rr.resolve(K("small", "explicit_level"), CAT, {}, dict(self.ctx))
        self.assertEqual((rec["level_key"], rec["model"]), ("small", HAIKU))
        self.assertEqual(rec["route_bound"][0], 1)  # small, not the coder's mid floor
        target = {"worker": "pi", "model": LUNA, "thinking": "medium"}
        problems, _ = rr.redirect_problems(CAT, target, self.ctx, {}, True, rec["route_bound"])
        self.assertEqual(problems, [])

    def test_without_the_effective_bound_the_full_role_floor_still_refuses(self):
        target = {"worker": "pi", "model": LUNA, "thinking": "medium"}
        problems, _ = rr.redirect_problems(CAT, target, self.ctx, {}, True)
        self.assertTrue(any("below" in p for p in problems), problems)

    def test_the_router_own_small_pick_keeps_the_floor_and_a_lower_redirect_is_refused(self):
        rec = rr.resolve(K("small"), CAT, {}, dict(self.ctx))
        self.assertEqual(rec["level_key"], "mid")
        target = {"worker": "pi", "model": LUNA, "thinking": "medium"}
        problems, _ = rr.redirect_problems(CAT, target, self.ctx, {}, False, rec["route_bound"])
        self.assertTrue(any("below" in p for p in problems), problems)

    def test_a_protected_path_still_bounds_a_redirect_after_an_explicit_level(self):
        ctx = dict(self.ctx, paths=["bin/tp-redact"], role="architect")
        rec = rr.resolve(K("small", "explicit_level"), CAT, {}, ctx)
        target = {"worker": "pi", "model": LUNA, "thinking": "medium"}
        problems, _ = rr.redirect_problems(CAT, target, ctx, {}, True, rec["route_bound"])
        self.assertEqual(problems == [], rec["level_key"] == "small", (problems, rec["level_key"]))


if __name__ == "__main__":
    unittest.main()
