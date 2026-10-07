"""The model catalog: strict validation, the generated router prompt, the top-level wording, and the parser for the
router's reply."""
import copy
import json
import re
import unittest

import route_fixture as fx

import router_catalog as rc  # noqa: E402

CAT = fx.fixture_catalog((fx.HAIKU, fx.LUNA, fx.QWEN))  # chain order the mutation tests below index into; not the shipped order
SHIPPED = fx.shipped_catalog()
LIB = fx.ROOT / "lib"


def level(cat, key):
    return next(lv for lv in cat["levels"] if lv["key"] == key)


class ValidationTests(unittest.TestCase):
    def bad(self, edit, message=None):
        cat = copy.deepcopy(CAT)
        edit(cat)
        with self.assertRaises(rc.CatalogError) as caught:
            rc.validate_catalog(cat)
        if message:
            self.assertIn(message, str(caught.exception))

    def test_committed_catalog_is_valid_and_shaped_as_designed(self):
        # Whatever the router chain's order: the shipped file is valid and every chain entry is complete.
        rc.load_catalog(fx.CATALOG_PATH)
        self.assertEqual([lv["key"] for lv in SHIPPED["levels"]], ["local", "small", "mid", "strong", "top"])
        self.assertEqual(rc.shown_keys(SHIPPED), ["local", "small", "mid", "top"])
        self.assertTrue(level(SHIPPED, "top").get("restricted"))
        self.assertIs(level(SHIPPED, "strong")["shown"], False)
        chain = SHIPPED["router"]["chain"]
        self.assertEqual(sorted(c["model"] for c in chain), sorted([fx.HAIKU, fx.LUNA, fx.QWEN]))
        for entry in chain:
            for key in ("provider", "worker", "model", "effort", "timeout_seconds", "max_input_tokens"):
                self.assertIn(key, entry, "{} lacks {}".format(entry.get("model"), key))
        by_model = {c["model"]: c for c in chain}
        self.assertEqual(by_model[fx.HAIKU]["env"], {"MAX_THINKING_TOKENS": "0"})
        self.assertEqual(by_model[fx.QWEN]["max_level"], "mid")

    def test_opus_is_reachable_only_through_the_restricted_top_level(self):
        homes = [lv["key"] for lv in SHIPPED["levels"] for m in lv["models"] if m["model"] == fx.OPUS]
        self.assertEqual(homes, ["top"])
        self.assertTrue(level(SHIPPED, "top").get("restricted"))
        self.assertNotIn(fx.OPUS, [c["model"] for c in SHIPPED["router"]["chain"]])
        # no router may be told to pick the top level on its own authority unless it is uncapped (a cloud router)
        for entry in SHIPPED["router"]["chain"]:
            if entry["provider"] == "local":
                self.assertNotIn(entry.get("max_level"), (None, "top", "strong"))

    def test_the_shipped_chain_is_qwen_first_with_the_confirm_rule_and_a_mid_cap(self):
        chain = SHIPPED["router"]["chain"]
        self.assertEqual([c["model"] for c in chain], [fx.QWEN, fx.HAIKU, fx.LUNA])
        qwen = chain[0]
        self.assertEqual(qwen["max_level"], "mid")
        self.assertEqual(qwen["confirm"], {"above_level": "mid", "unqualified_local": True})
        self.assertEqual(qwen["transport"], "direct_chat")
        self.assertTrue(chain[1]["thinking_off"])

    def test_levels_out_of_order(self):
        self.bad(lambda c: c["levels"].__setitem__(slice(1, 3), [c["levels"][2], c["levels"][1]]), "out of order")
        self.bad(lambda c: level(c, "mid").update(rank=20), "out of order")

    def test_effort_map_must_not_decrease(self):
        self.bad(lambda c: level(c, "mid")["effort_map"].update(low="high", medium="low"), "must not decrease")

    def test_vocabulary_must_follow_the_effort_order(self):
        self.bad(lambda c: c["effort"].update(vocabulary=["high", "medium", "low"]), "follow effort.order")

    def test_unknown_effort(self):
        self.bad(lambda c: level(c, "mid")["models"][0]["efforts"].append("ludicrous"), "unknown entries")
        self.bad(lambda c: level(c, "mid")["effort_map"].update(high="ludicrous"), "unknown effort")
        self.bad(lambda c: level(c, "mid")["anchors"][0].update(effort="max"), "not in the vocabulary")
        self.bad(lambda c: c["effort"].update(ceiling="max"), "vocabulary")
        self.bad(lambda c: level(c, "mid")["models"][0].update(efforts=["high", "low"]), "follow effort.order")

    def test_missing_model(self):
        self.bad(lambda c: level(c, "mid").update(models=[]), "has no models")
        self.bad(lambda c: c["router"]["chain"][0].update(model="claude-nonexistent-1"), "not in the catalog")
        self.bad(lambda c: c["router"].update(chain=[e for e in c["router"]["chain"] if e["provider"] != "local"]), "local router")
        self.bad(lambda c: level(c, "mid").pop("models"), "missing models")

    def test_other_inconsistencies(self):
        self.bad(lambda c: level(c, "mid")["models"][0].update(provider="nobody"), "unknown provider")
        self.bad(lambda c: level(c, "mid")["models"][0].update(worker="pi"), "does not match provider")
        self.bad(lambda c: level(c, "small")["models"].append(copy.deepcopy(level(c, "mid")["models"][0])), "listed twice")
        self.bad(lambda c: c.update(default_level="strong"), "default_level")
        self.bad(lambda c: c.update(default_level="nope"), "default_level")
        self.bad(lambda c: level(c, "mid").update(colour="red"), "unknown keys")
        self.bad(lambda c: c["levels"][0].update(restricted=True), "cannot be restricted")
        self.bad(lambda c: c["roles"]["tester"].update(max_level="nope"), "not a level key")
        self.bad(lambda c: c["roles"]["tester"].update(min_level="top", max_level="local"), "above max_level")
        self.bad(lambda c: c["roles"]["coder"].update(below_floor_shapes=["nope"]), "below_floor_shapes")
        self.bad(lambda c: c["roles"].update(scribe={"max_level": "mid", "strict_floor": True}), "strict_floor")
        self.bad(lambda c: c["roles"]["coder"].update(strict_floor="yes"), "strict_floor")
        self.bad(lambda c: c["roles"]["coder"].pop("min_level"), "strict_floor")
        self.bad(lambda c: c["protected_paths"][0].update(min_level="nope"), "not a level key")
        self.bad(lambda c: c["protected_paths"][0].update(min_effort="extreme"), "min_effort")
        self.bad(lambda c: c["protected_paths"][0].update(roles=[]), "roles")
        self.bad(lambda c: c["protected_paths"][0].pop("pattern"), "pattern")
        self.bad(lambda c: c["roles"]["tester"].update(below_floor_shapes=["small_edit"]), "below_floor_shapes")
        self.bad(lambda c: c["principles"].append("{level:strong} is hidden"), "cannot be generated")
        self.bad(lambda c: c["router"]["chain"][2].update(max_level="strong"), "shown level")
        self.bad(lambda c: c["review_policy"].update(files_required_at=0), "files_required_at")
        self.bad(lambda c: level(c, "mid")["anchors"][0].update(shape="nope"), "shape")

    def test_router_chain_options(self):
        chain = lambda c, i=0: c["router"]["chain"][i]
        self.bad(lambda c: chain(c).update(transport="smoke"), "transport")
        self.bad(lambda c: chain(c).update(temperature=0), "no such option")  # a CLI router cannot be given a temperature
        self.bad(lambda c: chain(c).update(seed=1), "no such option")
        self.bad(lambda c: chain(c, 2).update(temperature=-1), "temperature")
        self.bad(lambda c: chain(c, 2).update(seed=-3), "seed")
        self.bad(lambda c: chain(c, 2).update(seed="7"), "seed")
        self.bad(lambda c: chain(c, 2).update(env={"A": "1"}), "env applies to a CLI")
        self.bad(lambda c: chain(c).update(transport="direct_chat"), "local providers only")
        self.bad(lambda c: chain(c).update(thinking_off="yes"), "thinking_off")
        self.bad(lambda c: chain(c).update(max_input_tokens=0), "max_input_tokens")
        self.bad(lambda c: chain(c).update(confirm={}), "above_level or unqualified_local")
        self.bad(lambda c: chain(c).update(confirm={"above_level": "strong"}), "shown level")
        self.bad(lambda c: chain(c).update(confirm={"colour": 1}), "unknown keys")
        self.bad(lambda c: chain(c).update(confirm={"unqualified_local": "yes"}), "unqualified_local")

        def no_shape_level(c):
            level(c, "local").pop("requires_shape")
            chain(c).update(confirm={"unqualified_local": True})
        self.bad(no_shape_level, "requires_shape")
        good = copy.deepcopy(CAT)
        chain(good).update(confirm={"above_level": "mid", "unqualified_local": True})
        chain(good, 2).update(temperature=0.2, seed=3)
        rc.validate_catalog(good)

    def test_the_shipped_local_router_is_deterministic_and_thinks_not(self):
        chain = SHIPPED["router"]["chain"]
        local = next(e for e in chain if e.get("transport") == "direct_chat")
        self.assertEqual((local["temperature"], local["thinking_off"], local["max_level"]), (0, True, "mid"))
        self.assertIsInstance(local["seed"], int)
        self.assertEqual(local["provider"], "local")
        self.assertTrue(next(e for e in chain if e["worker"] == "claude")["thinking_off"])
        self.assertEqual(sorted(e["provider"] for e in chain), ["anthropic", "local", "openai"])
        for entry in chain:
            self.assertIn("max_input_tokens", entry)

    def test_unreadable_catalog_is_an_error(self):
        with self.assertRaises(rc.CatalogError):
            rc.load_catalog(fx.ROOT / "config" / "nonexistent.json")


class PromptTests(unittest.TestCase):
    FORBIDDEN = ("haiku", "sonnet", "opus", "gpt", "qwen", "luna", "astra", "anthropic", "openai", "openrouter", "slotstream", "lmstudio")
    LEVEL_KEYS = ("small", "mid", "strong", "top")

    def test_generator_code_names_no_model_provider_or_level(self):
        for name in ("router_catalog.py", "route_resolve.py", "route_card.py", "llm_router.py", "model_router.py"):
            text = (LIB / name).read_text().lower()
            for word in self.FORBIDDEN:
                self.assertFalse(word in text, "{} names {}".format(name, word))
            for key in self.LEVEL_KEYS:
                self.assertFalse(re.search(r"[\"']%s[\"']" % key, text), "{} names level {}".format(name, key))

    def test_prompt_follows_a_renamed_catalog(self):
        """Rename every level, model and provider and blank the catalog's prose: nothing of the originals is left,
        so the generator adds no name of its own."""
        models = {"claude-haiku-4-5-20251001": "zed-one", "claude-sonnet-5-5": "zed-two", "claude-opus-5-5": "zed-three",
                  "openai-codex/gpt-6-luna": "vend/zed-four", "openai-codex/gpt-6-sol": "vend/zed-five",
                  "openai-codex/gpt-6-astra": "vend/zed-six", "openai-codex/gpt-6.1-sol": "vend/zed-seven",
                  "lmstudio/qwen/qwen3.6-35b-a3b": "box/zed-eight", "slotstream/qwen3.8-flash-next:4bit": "box/zed-nine"}
        providers = {"anthropic": "vendor_a", "openai": "vendor_b", "local": "vendor_c", "openrouter": "vendor_d"}
        text = json.dumps(CAT)
        for old, new in models.items():
            text = text.replace(old, new)
        cat = json.loads(text)
        cat["providers"] = {providers[k]: v for k, v in cat["providers"].items()}
        for order in cat["provider_order"].values():
            order[:] = [providers[p] for p in order]
        for item in [m for lv in cat["levels"] for m in lv["models"]] + cat["router"]["chain"]:
            item["provider"] = providers[item["provider"]]
        for i, lv in enumerate(cat["levels"]):
            lv.update(name="Tier-%d" % i)
            lv["for"] = lv["not_for"] = "prose"
            for anchor in lv["anchors"]:
                anchor["task"] = "an anchor task"
        cat["principles"] = ["a principle"]
        for key in cat["effort"]["meaning"]:
            cat["effort"]["meaning"][key] = "meaning"
        cat["effort"]["ceiling_rule"] = "a rule"
        for shape in cat["shapes"].values():
            shape["text"] = "a shape"
        for key in cat["review"]:
            cat["review"][key] = "a review meaning"
        prompt = rc.build_system(rc.validate_catalog(cat)).lower()
        for word in self.FORBIDDEN:
            self.assertFalse(word in prompt, "the prompt still names " + word)
        for expected in ("zed-one", "zed-two", "tier-0", "tier-4", "vendor_a", "vendor_b"):
            self.assertTrue(expected in prompt, "the prompt lacks " + expected)

    def test_prompt_lists_only_shown_levels_in_order(self):
        prompt = rc.build_system(CAT)
        self.assertEqual(re.findall(r"^Level (\d+) - ", prompt, re.M), ["1", "2", "3", "4"])
        self.assertNotIn("gpt-6.1-sol", prompt)
        self.assertIn('<integer 1-4>', prompt)
        self.assertIn("Level 1 requires a listed shape", prompt)  # {level:local} placeholder rendered as a number
        self.assertNotIn("{level:", prompt)
        self.assertIn("level 4 (Opus / GPT-6 astra) 0-3%", prompt)

    def test_prompt_reflects_catalog_edits(self):
        cat = copy.deepcopy(CAT)
        level(cat, "mid")["name"] = "Workhorse"
        level(cat, "mid")["for"] = "Whatever the catalog says."
        prompt = rc.build_system(cat)
        self.assertIn("Level 3 - Workhorse", prompt)
        self.assertIn("For: Whatever the catalog says.", prompt)

    def test_hashes_follow_the_content(self):
        cat = copy.deepcopy(CAT)
        before = (rc.catalog_sha(cat), rc.prompt_sha(rc.build_system(cat)))
        level(cat, "mid")["name"] = "Other"
        self.assertNotEqual(before[0], rc.catalog_sha(cat))
        self.assertNotEqual(before[1], rc.prompt_sha(rc.build_system(cat)))


class TopLevelWordingTests(unittest.TestCase):
    def test_top_level_is_rare_and_reached_only_by_the_three_paths(self):
        top = level(SHIPPED, "top")
        text = (top["for"] + " " + top["not_for"]).lower()
        for needle in ("rare", "breadth of scope", "architecture", "inference", "explicit request", "escalation after two failed attempts"):
            self.assertIn(needle, text)
        self.assertIn("security wording, length and risk never raise the level", text)
        self.assertTrue(top["restricted"])
        self.assertTrue(all("security" not in a["task"].lower() for a in top["anchors"]))
        self.assertTrue(all("architecture" in a["task"].lower() for a in top["anchors"]))
        self.assertLessEqual(top["expected_share"][1], 0.03)
        principle = next(p for p in CAT["principles"] if "top level" in p)
        self.assertIn("rare", principle)
        self.assertIn("security-related", principle)


class ReplyParserTests(unittest.TestCase):
    OK = '{"level": 3, "effort": "medium", "review": "none", "shape": null, "reason": "x"}'

    def test_fenced_and_plain_replies_parse(self):
        self.assertEqual(rc.parse_decision("```json\n" + self.OK + "\n```", CAT)["level"], 3)
        self.assertEqual(rc.parse_decision(self.OK, CAT)["effort"], "medium")
        self.assertEqual(rc.parse_decision("Sure: " + self.OK, CAT)["review"], "none")

    def test_strict_rejections(self):
        bad = ['{"level": 5, "effort": "medium", "review": "none", "shape": null, "reason": "x"}',
               '{"level": 3, "effort": "max", "review": "none", "shape": null, "reason": "x"}',
               '{"level": "3", "effort": "medium", "review": "none", "shape": null, "reason": "x"}',
               '{"level": true, "effort": "medium", "review": "none", "shape": null, "reason": "x"}',
               '{"level": 3, "effort": "medium", "review": "maybe", "shape": null, "reason": "x"}',
               '{"level": 3, "effort": "medium", "review": "none", "shape": "nope", "reason": "x"}',
               '{"level": 3, "effort": "medium", "review": "none", "shape": null}',
               '{"level": 3, "effort": "medium", "review": "none", "shape": null, "reason": "x", "extra": 1}',
               '[1, 2]', 'no json here', '{"level": 3,']
        for text in bad:
            with self.subTest(text=text):
                with self.assertRaises((ValueError, TypeError)):
                    rc.parse_decision(text, CAT)

    def test_schema_matches_the_parser(self):
        schema = rc.reply_schema(CAT)
        self.assertEqual(schema["properties"]["level"]["maximum"], 4)
        self.assertEqual(schema["properties"]["effort"]["enum"], ["low", "medium", "high"])


if __name__ == "__main__":
    unittest.main()
