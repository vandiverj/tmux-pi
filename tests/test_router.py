import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import copy
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import router
from router import (QUESTIONS, REVIEWS, RouterError, apply_rules, dispatch, feature_names, follow_up, laya_decision,
                    laya_route,
                    parse_markers, placement_policy,
                    load_config, load_local, load_model, model_digest, pick_files, review_policy, review_task, route_openai,
                    route_task, validate_decision, workdir_files)  # noqa: E402


class Response:
    def __init__(self, value):
        self.value = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.value


class RawResponse(Response):
    def __init__(self, raw):
        self.value = raw


class ResponseError(Response):
    def __init__(self, status):
        self.value = b"{}"
        self.status = status


class RouterTests(unittest.TestCase):
    def setUp(self):
        # Tests use the committed config only; a machine's config/local.json must not change them.
        self.config = load_config(ROOT / "config/router.json", local_path=False)

    def reply(self, decision, finish_reason="stop"):
        return Response({"choices": [{"message": {"content": json.dumps(decision)},
                                       "finish_reason": finish_reason}]})

    def test_valid_completion_maps_to_spawn_arguments(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return self.reply({"model": "claude-sonnet-5-5", "thinking": "high"})

        result = route_openai("Review this change", self.config, opener=open_request)
        self.assertEqual(result, {"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high"})
        self.assertEqual(captured["url"], "http://127.0.0.1:11435/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "qwen3.8-flash-next:4bit")
        self.assertEqual(captured["body"]["messages"][1]["content"], "Review this change")
        self.assertEqual(captured["timeout"], 60)

    def test_rejects_unlisted_model_and_thinking(self):
        with self.assertRaisesRegex(RouterError, "configured roster"):
            validate_decision({"model": "other", "thinking": "low"}, self.config)
        with self.assertRaisesRegex(RouterError, "not allowed"):
            validate_decision({"model": "claude-sonnet-5-5", "thinking": "off"}, self.config)
        with self.assertRaisesRegex(RouterError, "exactly model and thinking"):
            validate_decision({"model": "claude-sonnet-5-5", "thinking": "low", "worker": "pi"}, self.config)

    def test_rejects_invalid_or_truncated_completion(self):
        with self.assertRaisesRegex(RouterError, "invalid JSON completion"):
            route_openai("task", self.config, opener=lambda *_args, **_kwargs: Response(
                {"choices": [{"message": {"content": "```json"}, "finish_reason": "stop"}]}))
        with self.assertRaisesRegex(RouterError, "incomplete"):
            route_openai("task", self.config, opener=lambda *_args, **_kwargs:
                         self.reply({"model": "claude-sonnet-5-5", "thinking": "low"}, "length"))

    def test_unavailable_endpoint_is_clear(self):
        def unavailable(*_args, **_kwargs):
            raise urllib.error.URLError("connection refused")

        with self.assertRaisesRegex(RouterError, "endpoint unavailable"):
            route_openai("task", self.config, opener=unavailable)

    def test_local_override_and_invalid_config(self):
        def open_request(request, timeout):
            self.assertEqual(request.full_url, "http://localhost:1234/v1/chat/completions")
            self.assertEqual(json.loads(request.data)["model"], "loaded-local-model")
            return self.reply({"model": "slotstream/qwen3.8-flash-next:4bit", "thinking": "low"})

        route_openai("task", self.config, "http://localhost:1234/v1", "loaded-local-model", open_request)
        with self.assertRaisesRegex(RouterError, "loopback"):
            route_openai("task", self.config, "http://example.com:1234/v1", opener=open_request)
        bad = json.loads((ROOT / "config/router.json").read_text())
        bad["models"][0]["thinking"] = [{"bad": "level"}]
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router.json"
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(RouterError, "thinking must"):
                load_config(path)

    def laya_reply(self, intent="implementation", difficulty=1, tools=0.5, sensitive=0.1, risk=0.1, changes=0.5,
                   scope="brief"):
        intents = {key: (0.95 if key == intent else 0.01) for key in QUESTIONS["task_intent"]["criteria"]}
        return {"routing": {"model": "typed-decisions"}, "answers": {
            "task_intent": {"choice": intent, "probabilities": intents},
            "difficulty": {"score": difficulty, "confidence": 0.8,
                           "probabilities": {"0": 0.1, "1": 0.8, "2": 0.05, "3": 0.05}},
            "needs_tools": {"noul": tools},
            "is_sensitive": {"noul": sensitive},
            "is_high_risk": {"noul": risk},
            "changes": {"noul": changes},
            "execution_scope": {"choice": scope, "probabilities": {
                key: (0.9 if key == scope else 0.05) for key in QUESTIONS["execution_scope"]["criteria"]}},
        }}

    def toy_model(self, gate=0.5, level=0.5):
        """Route follows is_sensitive (simple below 0.5, expert above); review follows changes."""
        names = feature_names()
        width = len(names)

        def head(labels, feature, slopes):
            weights = [[0.0] * len(labels) for _ in names]
            weights[names.index(feature)] = slopes
            return {"labels": labels, "mean": [0.5] * width, "scale": [1.0] * width,
                    "weights": weights, "bias": [0.0] * len(labels)}

        intents = list(QUESTIONS["task_intent"]["criteria"])
        intent = head(intents, "changes", [0.0] * len(intents))
        for index, key in enumerate(intents):  # the learned intent follows Laya's own choice
            intent["weights"][names.index("task_intent:" + key)][index] = 20.0
        return {"questions": model_digest(), "features": names, "intent": intent,
                "route": head(["simple", "expert"], "is_sensitive", [-20.0, 20.0]),
                "gate": {**head(["none", "review"], "changes", [-20.0, 20.0]), "threshold": gate},
                "level": {**head(["optional", "required"], "needs_tools", [-20.0, 20.0]), "threshold": level},
                "placement": {**self.placement_head(), "threshold": 0.5}}

    def placement_head(self):
        """Placement follows the probability of a brief scope: brief stays in main."""
        width = len(feature_names()) + 3
        weights = [[0.0, 0.0] for _ in range(width)]
        weights[len(feature_names())] = [20.0, -20.0]  # execution_scope:brief
        return {"labels": ["main", "subagent"], "mean": [0.5] * width, "scale": [1.0] * width,
                "weights": weights, "bias": [0.0, 0.0]}

    def with_model(self, model):
        from tempfile import TemporaryDirectory
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        raw = json.loads((ROOT / "config/router.json").read_text())
        (Path(directory.name) / "router.json").write_text(json.dumps(raw))
        (Path(directory.name) / "laya-model.json").write_text(json.dumps(model))
        return load_config(Path(directory.name) / "router.json", local_path=False)

    def test_shipped_model_matches_questions(self):
        self.assertEqual(self.config["model"]["questions"], model_digest())
        self.assertEqual(self.config["model"]["features"], feature_names())
        self.assertIn(laya_route(self.laya_reply(), self.config["model"]), self.config["routes"])

    def test_laya_request_and_model_mapping(self):
        captured = {}
        config = self.with_model(self.toy_model())

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response(self.laya_reply(sensitive=0.1))

        result = route_task("Summarize a note", config, opener=open_request)
        self.assertEqual(result, {"worker": "pi", "model": "slotstream/qwen3.8-flash-next:4bit", "thinking": "off"})
        self.assertEqual(captured["url"], "http://127.0.0.1:11436/v1/systemone")
        self.assertEqual(captured["body"], {"model": "typed-decisions",
                                             "state": {"request": "Summarize a note"}, "questions": QUESTIONS})
        self.assertEqual(captured["timeout"], 60)
        self.assertEqual(laya_decision(self.laya_reply(sensitive=0.9), config),
                         {"worker": "claude", "model": "claude-opus-5-5", "thinking": "high"})

    def test_laya_malformed_and_numeric_failures(self):
        model = self.toy_model()
        with self.assertRaisesRegex(RouterError, "missing required answers"):
            laya_route({"routing": {"model": "typed-decisions"},
                        "answers": {"difficulty": {"score": 1}}}, model)
        with self.assertRaisesRegex(RouterError, "not routed through typed-decisions"):
            laya_route({"routing": {"model": "english"}, "answers": self.laya_reply()["answers"]}, model)
        incomplete = self.laya_reply()
        del incomplete["answers"]["difficulty"]["probabilities"]["3"]
        with self.assertRaisesRegex(RouterError, "probabilities is incomplete"):
            laya_route(incomplete, model)
        no_probs = self.laya_reply()
        del no_probs["answers"]["task_intent"]["probabilities"]
        with self.assertRaisesRegex(RouterError, "needs probabilities"):
            laya_route(no_probs, model)
        bad_choice = self.laya_reply(intent="deploy")
        with self.assertRaisesRegex(RouterError, "invalid choice"):
            laya_route(bad_choice, model)
        wrong_type = self.laya_reply()
        wrong_type["answers"]["needs_tools"]["type"] = "score"
        with self.assertRaisesRegex(RouterError, "wrong type"):
            laya_route(wrong_type, model)
        for value in (None, "0.8", True, math.nan, math.inf, -0.1, 1.1):
            reply = self.laya_reply()
            reply["answers"]["changes"]["noul"] = value
            with self.subTest(value=value), self.assertRaises(RouterError):
                laya_route(reply, model)
        for value in ("2", True, math.nan, -1, 4):
            reply = self.laya_reply(difficulty=value)
            with self.subTest(difficulty=value), self.assertRaises(RouterError):
                laya_route(reply, model)

    def test_model_validation(self):
        good = self.toy_model()
        cases = [
            ({**good, "questions": "0" * 64}, "different questions"),
            ({**good, "features": good["features"][:-1]}, "features do not match"),
            ({**good, "route": {**good["route"], "labels": ["simple", "fallback"]}}, "labels must be among"),
            ({**good, "route": {**good["route"], "bias": [0.0]}}, "wrong shape"),
            ({**good, "route": {**good["route"], "scale": [0.0] * len(good["features"])}}, "wrong shape"),
            ({**good, "gate": {**good["gate"], "labels": ["review", "none"]}}, "gate labels must be"),
            ({**good, "level": {**good["level"], "threshold": 1}}, "threshold must be between"),
            ({k: v for k, v in good.items() if k != "level"}, "level needs"),
            ({**good, "intent": {**good["intent"], "labels": good["intent"]["labels"][::-1]}}, "intent labels"),
            ({**good, "placement": {**good["placement"], "weights": good["route"]["weights"]}}, "wrong shape"),
        ]
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            for model, message in cases:
                with self.subTest(message=message):
                    path.write_text(json.dumps(model))
                    with self.assertRaisesRegex(RouterError, message):
                        load_model(path)

    def test_review_policy(self):
        model = self.toy_model()
        docs = "communication_or_documentation"
        # The gate follows `changes`; the level head follows `needs_tools`.
        self.assertEqual(review_policy(self.laya_reply(intent=docs, changes=0.1), model), "none")
        self.assertEqual(review_policy(self.laya_reply(intent=docs, changes=0.9, tools=0.9), model), "required")
        self.assertEqual(review_policy(self.laya_reply(intent=docs, changes=0.9, tools=0.1), model), "optional")
        # Research is never reviewed, whatever the model says.
        self.assertEqual(review_policy(self.laya_reply(intent="research", changes=0.9, sensitive=0.9), model), "none")
        self.assertEqual(review_policy(self.laya_reply(intent="research"), model, force=True), "required")
        with self.assertRaisesRegex(RouterError, "invalid choice"):
            review_policy(self.laya_reply(intent="deploy"), model)

    def test_review_guardrails(self):
        model = self.toy_model()
        # The gate alone would skip these (changes 0.1); intent or sensitivity forces a review.
        for intent in ("implementation", "configuration_or_operations"):
            with self.subTest(intent=intent):
                self.assertNotEqual(review_policy(self.laya_reply(intent=intent, changes=0.1), model), "none")
        self.assertNotEqual(review_policy(self.laya_reply(intent="planning", changes=0.1, sensitive=0.5), model), "none")
        self.assertEqual(review_policy(self.laya_reply(intent="planning", changes=0.1, sensitive=0.49), model), "none")

    def test_review_thresholds(self):
        reply = self.laya_reply(intent="planning", changes=0.45, tools=0.45)  # P(review) = P(required) ≈ 0.12
        self.assertEqual(review_policy(reply, self.toy_model(gate=0.5)), "none")
        self.assertEqual(review_policy(reply, self.toy_model(gate=0.1, level=0.5)), "optional")
        self.assertEqual(review_policy(reply, self.toy_model(gate=0.1, level=0.1)), "required")

    def test_review_task(self):
        captured = {}
        config = self.with_model(self.toy_model())

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            return Response(self.laya_reply(intent="research"))

        self.assertEqual(review_task("Explain tp-view", config, opener=open_request), "none")
        self.assertEqual(captured["url"], "http://127.0.0.1:11436/v1/systemone")
        self.assertEqual(captured["body"]["questions"], QUESTIONS)

        def never(*_args, **_kwargs):
            raise AssertionError("force must not contact Laya")

        self.assertEqual(review_task("Explain tp-view", config, opener=never, force=True), "required")
        with self.assertRaisesRegex(RouterError, "task must contain text"):
            review_task(" ", config, opener=never)

    def test_laya_transport_and_config_failures(self):
        def unavailable(*_args, **_kwargs):
            raise urllib.error.URLError("connection refused")

        with self.assertRaisesRegex(RouterError, "endpoint unavailable"):
            route_task("task", self.config, opener=unavailable)
        with self.assertRaisesRegex(RouterError, "HTTP 503"):
            route_task("task", self.config, opener=lambda *_args, **_kwargs: ResponseError(503))
        with self.assertRaisesRegex(RouterError, "malformed JSON"):
            route_task("task", self.config, opener=lambda *_args, **_kwargs: RawResponse(b"{"))
        with self.assertRaisesRegex(RouterError, "loopback"):
            route_task("task", self.config, base_url="http://example.com:11436", opener=lambda *_: None)
        with self.assertRaisesRegex(RouterError, "typed-decisions"):
            route_task("task", self.config, router_model="english", opener=lambda *_: None)
        bad = json.loads((ROOT / "config/router.json").read_text())
        bad["routes"]["coding"]["model"] = "unknown"
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router.json"
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(RouterError, "invalid coding route"):
                load_config(path)
            fine = json.loads((ROOT / "config/router.json").read_text())
            path.write_text(json.dumps(fine))
            with self.assertRaisesRegex(RouterError, "cannot read Laya model"):
                load_config(path)

    def write_local(self, local):
        from tempfile import TemporaryDirectory
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "local.json"
        path.write_text(json.dumps(local))
        return path

    def test_example_local_config_is_valid(self):
        local = load_local(ROOT / "config/local.example.json", self.config)
        self.assertIn("standard", local["routes"])
        self.assertIn("lmstudio/qwen/qwen3.6-35b-a3b", local["prompts"])

    def test_local_routes_override_and_validate(self):
        qwen = "lmstudio/qwen/qwen3.6-35b-a3b"
        path = self.write_local({"_comment": "x", "routes": {"standard": {"model": qwen, "thinking": "off"}}})
        config = load_config(ROOT / "config/router.json", local_path=path)
        self.assertEqual(config["routes"]["standard"]["model"], qwen)
        self.assertEqual(config["routes"]["coding"], self.config["routes"]["coding"])
        cases = [
            ({"routes": {"fallback": {"model": qwen, "thinking": "off"}}}, "local routes must be among"),
            ({"routes": {"standard": {"model": "local/unknown", "thinking": "off"}}}, "invalid local standard route"),
            ({"hosts": {}}, "local config keys"),
            ({"escalation": {"models": [qwen], "after_consecutive_errors": 0,
                             "to": {"model": "claude-haiku-5-5", "thinking": "low"}}}, "after_consecutive_errors"),
            ({"escalation": {"models": ["local/unknown"], "after_consecutive_errors": 3,
                             "to": {"model": "claude-haiku-5-5", "thinking": "low"}}}, "roster models"),
            ({"mlx": {"model": "x", "port": 80}}, "mlx port"),
            ({"rules": [{"pattern": "(", "from": [qwen], "to": {"model": qwen, "thinking": "off"}}]}, "valid regular expression"),
            ({"rules": [{"pattern": "x", "from": [qwen]}]}, "needs exactly pattern, from and to"),
            ({"prompts": {"local/unknown": "count with a command"}}, "local prompts must map roster models"),
            ({"prompts": {qwen: "  "}}, "nonempty text"),
        ]
        for local, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(RouterError, message):
                load_config(ROOT / "config/router.json", local_path=self.write_local(local))

    def test_rules_redirect_matching_tasks(self):
        qwen = "lmstudio/qwen/qwen3.6-35b-a3b"
        slot = "slotstream/qwen3.8-flash-next:4bit"
        path = self.write_local({"rules": [{"_why": "counting", "pattern": "(?i)\\bhow many\\b",
                                            "from": [qwen], "to": {"model": slot, "thinking": "low"}}]})
        config = load_config(ROOT / "config/router.json", local_path=path)
        picked = {"worker": "pi", "model": qwen, "thinking": "off"}
        self.assertEqual(apply_rules("How many tests are there?", picked, config)["model"], slot)
        self.assertEqual(apply_rules("Where is load_model defined?", picked, config), picked)
        other = {"worker": "claude", "model": "claude-opus-5-5", "thinking": "high"}
        self.assertEqual(apply_rules("How many tests are there?", other, config), other)

    def test_markers(self):
        self.assertEqual(parse_markers("+here +review  fix the bug"), ({"placement": "main", "review": "required"}, "fix the bug"))
        self.assertEqual(parse_markers("+Delegate write docs"), ({"placement": "subagent"}, "write docs"))
        self.assertEqual(parse_markers("email me +here later"), ({}, "email me +here later"))

    def test_placement_policy(self):
        model = self.toy_model()
        brief, long = self.laya_reply(scope="brief"), self.laya_reply(scope="sustained")
        self.assertEqual(placement_policy(brief, model, "research", "none"), "main")
        self.assertEqual(placement_policy(long, model, "research", "none"), "subagent")
        self.assertEqual(placement_policy(brief, model, "communication_or_documentation", "optional"), "main")
        # Required review and change-making intents always delegate, whatever the head says.
        self.assertEqual(placement_policy(brief, model, "planning", "required"), "subagent")
        for intent in ("implementation", "configuration_or_operations"):
            self.assertEqual(placement_policy(brief, model, intent, "none"), "subagent")

    def test_dispatch(self):
        config = self.with_model(self.toy_model())
        docs = "communication_or_documentation"
        reply = lambda **kw: (lambda *_args, **_kwargs: Response(self.laya_reply(**kw)))
        brief = dispatch("Explain tp-view", config, opener=reply(intent=docs, changes=0.1))
        self.assertEqual((brief["placement"], brief["review"], brief["scope"]), ("main", "none", "brief"))
        self.assertEqual(brief["model"], "slotstream/qwen3.8-flash-next:4bit")
        long = dispatch("Write the onboarding guide", config, opener=reply(intent=docs, changes=0.1, scope="sustained"))
        self.assertEqual(long["placement"], "subagent")
        forced = dispatch("+here +review Explain tp-view", config, opener=reply(intent=docs, changes=0.1))
        self.assertEqual((forced["placement"], forced["review"], forced["task"]), ("main", "required", "Explain tp-view"))
        with self.assertRaisesRegex(RouterError, "task must contain text"):
            dispatch("+here", config, opener=reply())

    def test_follow_up_with_local_judge(self):
        config = dict(self.config, local={"followup": {"base_url": "http://127.0.0.1:11437/v1", "model": "qwen",
                                                       "ask_between": [0.25, 0.9]}})
        worker = {"a": 0.99, "b": 0.02}  # probability the message is for each task's worker
        sent = []

        def judge(request, timeout):
            body = json.loads(request.data)
            sent.append(body)
            p = worker["a" if "json flag" in body["messages"][1]["content"] else "b"]
            top = [{"token": "worker", "logprob": math.log(p)}, {"token": "se", "logprob": math.log(1 - p)}]
            return Response({"choices": [{"logprobs": {"content": [{"top_logprobs": top}]}}]})

        tasks = [{"id": "a", "task": "Add a json flag to tp-list", "reply": "Include killed tasks?"},
                 {"id": "b", "task": "Write a haiku", "reply": "Done."}]
        self.assertEqual(follow_up("yes include them", tasks, config, opener=judge), {"target": "a", "ask": []})
        self.assertTrue(sent[0]["logprobs"])
        self.assertNotIn("reasoning_effort", sent[0])
        config["local"]["followup"]["extra_body"] = {"reasoning_effort": "none"}
        follow_up("yes include them", tasks, config, opener=judge)
        self.assertEqual((sent[-1]["reasoning_effort"], sent[-1]["max_tokens"]), ("none", 3))
        del config["local"]["followup"]["extra_body"]
        worker.update(a=0.02)
        self.assertEqual(follow_up("what is 2+2", tasks, config, opener=judge), {"target": None, "ask": []})
        worker.update(a=0.6)  # unsure: the user picks
        self.assertEqual(follow_up("undo that", tasks, config, opener=judge), {"target": None, "ask": ["a"]})
        worker.update(a=0.99, b=0.95)  # two tasks claim it
        self.assertEqual(follow_up("do it", tasks, config, opener=judge), {"target": None, "ask": ["a", "b"]})
        calls = len(sent)
        self.assertEqual(follow_up("+delegate do it", tasks, config, opener=judge), {"target": None, "ask": []})
        self.assertEqual(follow_up("do it", [], config, opener=judge), {"target": None, "ask": []})
        self.assertEqual(len(sent), calls)

    def test_follow_up_with_laya(self):
        def laya(score):
            return lambda request, timeout: Response({"answers": {"follow_up": {"type": "score", "score": score}}})

        tasks = [{"id": "a", "task": "Add a json flag to tp-list", "reply": "Include killed tasks?"}]
        self.assertEqual(follow_up("yes", tasks, self.config, opener=laya(1.6)), {"target": "a", "ask": []})
        self.assertEqual(follow_up("yes", tasks, self.config, opener=laya(1.0)), {"target": None, "ask": ["a"]})
        self.assertEqual(follow_up("hi", tasks, self.config, opener=laya(0.2)), {"target": None, "ask": []})
        with self.assertRaisesRegex(RouterError, "missing its score"):
            follow_up("yes", tasks, self.config, opener=lambda *_a, **_k: Response({"answers": {}}))

    def test_followup_config_validation(self):
        good = {"base_url": "http://127.0.0.1:11437/v1", "model": "qwen", "ask_between": [0.25, 0.9]}
        self.assertEqual(load_local(self.write_local({"followup": good}), self.config)["followup"], good)
        extra = {**good, "extra_body": {"reasoning_effort": "none"}}
        self.assertEqual(load_local(self.write_local({"followup": extra}), self.config)["followup"], extra)
        for bad in ({**good, "ask_between": [0.9, 0.25]}, {**good, "base_url": "http://example.com:1/v1"},
                    {"model": "qwen"}, {**good, "ask_between": [0, 2]}, {**good, "extra_body": {"max_tokens": 50}},
                    {**good, "extra_body": "none"}, {**good, "other": 1}):
            with self.assertRaises(RouterError):
                load_local(self.write_local({"followup": bad}), self.config)

    def test_packet_files_config_validation(self):
        qwen = "lmstudio/qwen/qwen3.6-35b-a3b"
        good = {"models": [qwen], "selector": "claude-sonnet-5-5"}
        self.assertEqual(load_local(self.write_local({"packet_files": good}), self.config)["packet_files"], good)
        for bad in ({"models": [], "selector": "claude-sonnet-5-5"}, {"models": ["nope/model"], "selector": "claude-sonnet-5-5"},
                    {"models": [qwen], "selector": "gpt-5.5"}, {"models": [qwen]}, {**good, "extra": 1}):
            with self.assertRaises(RouterError):
                load_local(self.write_local({"packet_files": bad}), self.config)

    def test_summarize_structured_config_validation(self):
        good = {"system": "Summarize.", "schema": {"type": "object"}}
        self.assertEqual(load_local(self.write_local({"summarize_structured": good}), self.config)["summarize_structured"], good)
        with_budget = {**good, "budget_chars": 1000}
        self.assertEqual(load_local(self.write_local({"summarize_structured": with_budget}), self.config)["summarize_structured"], with_budget)
        for bad in ("text", [], {"system": "x"}, {"schema": {"a": 1}}, {**good, "system": " "}, {**good, "system": 3},
                    {**good, "schema": {}}, {**good, "schema": "{}"}, {**good, "extra": 1}, {**good, "budget_chars": 999},
                    {**good, "budget_chars": 2000001}, {**good, "budget_chars": True}, {**good, "budget_chars": "5000"},
                    {**good, "budget_chars": 5000.5}):
            with self.subTest(bad=bad), self.assertRaises(RouterError) as caught:
                load_local(self.write_local({"summarize_structured": bad}), self.config)
            self.assertIn("summarize_structured", str(caught.exception))

    def pick_workdir(self):
        from tempfile import TemporaryDirectory
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "lib").mkdir()
        (root / "lib/small.py").write_text("x = 1\n")
        (root / "lib/big.py").write_text("line of text\n" * 3000)
        (root / "lib/blob.bin").write_bytes(b"\xff\xfe\x00\x01" * 10)
        (root / ".env").write_text("TOKEN=1\n")
        (root / "db_secret.json").write_text("{}\n")
        (root / "node_modules").mkdir()
        (root / "node_modules/dep.js").write_text("1\n")
        return root

    def test_workdir_files_skip_credentials_and_vendored_dirs(self):
        root = self.pick_workdir()
        no_git = lambda *a, **k: type("R", (), {"returncode": 128, "stdout": "", "stderr": ""})()
        self.assertEqual(workdir_files(str(root), no_git), ["lib/big.py", "lib/blob.bin", "lib/small.py"])

    def test_pick_files(self):
        root = self.pick_workdir()
        qwen = "lmstudio/qwen/qwen3.6-35b-a3b"
        config = dict(self.config, local={"packet_files": {"models": [qwen], "selector": "claude-sonnet-5-5"}})
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if cmd[0] == "git":
                return type("R", (), {"returncode": 0, "stdout": "lib/small.py\nlib/big.py\nlib/blob.bin\n.env\n", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stderr": "",
                                  "stdout": "- `lib/big.py`\n./lib/small.py\nlib/blob.bin\n.env\nmissing.py\nlib/small.py\n"})()

        specs = pick_files("fix the thing", str(root), qwen, config, runner)
        self.assertEqual(specs[1], "lib/small.py")
        self.assertRegex(specs[0], r"^lib/big\.py:1-\d+$")
        self.assertEqual(len(specs), 2)  # blob.bin is not text; .env was never listed
        self.assertIn("claude-sonnet-5-5", calls[1])
        self.assertNotIn(".env", calls[1][-1])
        self.assertEqual(pick_files("task", str(root), "slotstream/qwen3.8-flash-next:4bit", config, runner), [])
        self.assertEqual(pick_files("task", str(root), qwen, self.config, runner), [])

        failing = lambda cmd, **k: runner(cmd) if cmd[0] == "git" else type("R", (), {"returncode": 1, "stdout": "", "stderr": "no auth"})()
        with self.assertRaises(RouterError):
            pick_files("task", str(root), qwen, config, failing)

    def test_explicit_openai_backend(self):
        result = route_task("task", self.config, backend="openai", opener=lambda *_args, **_kwargs:
                            self.reply({"model": "claude-sonnet-5-5", "thinking": "high"}))
        self.assertEqual(result, {"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "high"})


class LmStudioBudgetTests(unittest.TestCase):
    """The one LM Studio guard in local_open (TP_LMSTUDIO_BUDGET / TP_LMSTUDIO_BUDGET_FILE). "LM Studio" here is a stub on a free
    loopback port that a row names as LM Studio (TP_LMSTUDIO_ENDPOINT or a temp config root); nothing touches 127.0.0.1:1234: rows
    about port 1234 ask the predicate (is_lmstudio) only, and every send with a budget set is refused before a socket opens."""

    @classmethod
    def setUpClass(cls):
        cls.hits, cls.active, cls.peak = [], [0], [0]
        cls.lock = threading.Lock()
        cls.events = queue.Queue()  # ("hit", n) from the stub, ("A"/"B", line) from the contention test's children
        cls.hold, cls.release = [False], threading.Event()  # hold: the first hit waits for release
        hits, active, peak, lock, events, hold, release = cls.hits, cls.active, cls.peak, cls.lock, cls.events, cls.hold, cls.release

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with lock:
                    hits.append(self.server.server_address[1])
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                    n = len(hits)
                events.put(("hit", n))
                if hold[0] and n == 1:
                    release.wait(30)  # bound only: the test sets release as soon as the second request is seen waiting
                with lock:
                    active[0] -= 1
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        cls.servers = []
        for _ in range(2):  # [0] is "LM Studio", [1] is a stub on another port
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
            cls.servers.append(server)
            cls.addClassCleanup(server.server_close)
            cls.addClassCleanup(server.shutdown)
        cls.lm, cls.other = (s.server_address[1] for s in cls.servers)

    def setUp(self):
        self.hits[:] = []
        self.peak[0] = 0
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.dir, True)
        self.counter = self.dir / "count"
        # The guard reads LM Studio's configured address under this root: an empty temp root, never this machine's config.
        self.root = self.dir / "root"
        patch = mock.patch.object(router, "LMSTUDIO_CONFIG_ROOT", self.root)
        patch.start()
        self.addCleanup(patch.stop)

    def configure(self, local=None, models=None):
        """Write a temp config root: config/local.json followup.base_url and/or agent/models.json providers.lmstudio.baseUrl."""
        for rel, value, body in (("config/local.json", local, lambda v: {"followup": {"base_url": v}}),
                                 ("agent/models.json", models, lambda v: {"providers": {"lmstudio": {"baseUrl": v}}})):
            if value is not None:
                (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.root / rel).write_text(json.dumps(body(value)))

    def env(self, budget="0", content="0", endpoint=True):
        env = {"TP_LMSTUDIO_ENDPOINT": "127.0.0.1:{}".format(self.lm)} if endpoint else {}
        if budget is not None:
            env["TP_LMSTUDIO_BUDGET"] = budget
            env["TP_LMSTUDIO_BUDGET_FILE"] = str(self.counter)
        if content is not None:
            self.counter.write_text(content)
        return env

    def send(self, env, port=None, host="127.0.0.1"):
        """One local_open to the stub; returns "ok" or the refusal text."""
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TP_LMSTUDIO")}
        request = router.urllib.request.Request("http://{}:{}/v1/chat/completions".format(host, port or self.lm), data=b"{}")
        with mock.patch.dict(os.environ, dict(clean, **env), clear=True):
            try:
                with router.local_open(request, 5) as response:
                    response.read()
                return "ok"
            except router.LmStudioBudgetError as exc:
                return str(exc)

    def test_the_cap_table(self):
        lm = "http://localhost:{}/v1"
        cases = [  # name, env (made after the counter is cleared), sends, expected results, stub hits on the LM Studio port, host
            ("no budget: unchanged, no counter", lambda: self.env(None, None), 3, ["ok"] * 3, 3, "127.0.0.1"),
            ("budget 0: refused, nothing sent", lambda: self.env("0"), 1, ["refused"], 0, "127.0.0.1"),
            ("budget 2: third refused", lambda: self.env("2"), 3, ["ok", "ok", "refused"], 2, "127.0.0.1"),
            ("counter file missing", lambda: self.env("5", None), 1, ["refused"], 0, "127.0.0.1"),
            ("counter file empty", lambda: self.env("5", ""), 1, ["refused"], 0, "127.0.0.1"),
            ("counter file not a number", lambda: self.env("5", "lots"), 1, ["refused"], 0, "127.0.0.1"),
            ("counter file negative", lambda: self.env("5", "-1"), 1, ["refused"], 0, "127.0.0.1"),
            ("budget not a number", lambda: self.env("many"), 1, ["refused"], 0, "127.0.0.1"),
            ("budget negative", lambda: self.env("-1"), 1, ["refused"], 0, "127.0.0.1"),
            ("budget without a file", lambda: {"TP_LMSTUDIO_ENDPOINT": "127.0.0.1:{}".format(self.lm), "TP_LMSTUDIO_BUDGET": "5"}, 1, ["refused"], 0, "127.0.0.1"),
            # where LM Studio is (F2): the configured base URLs, with no TP_LMSTUDIO_ENDPOINT
            ("config/local.json followup.base_url", lambda: (self.configure(local=lm.format(self.lm)), self.env("0", endpoint=False))[1], 1, ["refused"], 0, "127.0.0.1"),
            ("agent/models.json lmstudio baseUrl", lambda: (self.configure(models=lm.format(self.lm)), self.env("0", endpoint=False))[1], 1, ["refused"], 0, "127.0.0.1"),
            ("TP_LMSTUDIO_ENDPOINT as a base URL", lambda: dict(self.env("0"), TP_LMSTUDIO_ENDPOINT=lm.format(self.lm)), 1, ["refused"], 0, "127.0.0.1"),
            ("TP_LMSTUDIO_ENDPOINT as a bare port", lambda: dict(self.env("0"), TP_LMSTUDIO_ENDPOINT=str(self.lm)), 1, ["refused"], 0, "127.0.0.1"),
            # loopback names are one host: refused before a socket opens, so the stub need not listen there
            ("sent to localhost", lambda: self.env("0"), 1, ["refused"], 0, "localhost"),
            ("sent to [::1]", lambda: self.env("0"), 1, ["refused"], 0, "[::1]"),
            ("sent to 127.0.0.2", lambda: self.env("0"), 1, ["refused"], 0, "127.0.0.2"),
            ("sent to 0.0.0.0", lambda: self.env("0"), 1, ["refused"], 0, "0.0.0.0"),
            ("sent to 127.1", lambda: self.env("0"), 1, ["refused"], 0, "127.1"),
            ("sent to [::ffff:127.0.0.1]", lambda: self.env("0"), 1, ["refused"], 0, "[::ffff:127.0.0.1]"),
            # a per-run redirect is not where LM Studio is: TP_LOCAL_CONFIG naming the stub leaves it a stub (tests rely on this)
            ("TP_LOCAL_CONFIG followup on the stub", lambda: dict(self.env("0", endpoint=False), TP_LOCAL_CONFIG=self.write_redirect()), 2, ["ok"] * 2, 2, "127.0.0.1"),
        ]
        for name, make_env, sends, expected, hits, host in cases:
            with self.subTest(name):
                self.hits[:] = []
                self.counter.unlink(missing_ok=True)
                __import__("shutil").rmtree(self.root, True)
                env = make_env()
                got = [self.send(env, host=host) for _ in range(sends)]
                self.assertEqual(["ok" if r == "ok" else "refused" for r in got], expected)
                self.assertEqual(self.hits.count(self.lm), hits)
                if name.startswith("no budget"):
                    self.assertFalse(self.counter.exists())  # production: no counter is read or written
                for r in got:
                    if r != "ok":
                        self.assertIn("TP_LMSTUDIO_BUDGET", r)
                        if name == "budget 2: third refused":
                            self.assertIn("cap is 2", r)
        with self.subTest("a stub on another port is never counted or limited"):
            self.hits[:] = []
            env = self.env("0")
            self.assertEqual([self.send(env, port=self.other) for _ in range(3)], ["ok"] * 3)
            self.assertEqual(self.counter.read_text(), "0")

    def write_redirect(self):
        """A local config elsewhere whose follow-up judge is the stub, as tests name one through TP_LOCAL_CONFIG."""
        path = self.dir / "redirect-local.json"
        path.write_text(json.dumps({"followup": {"base_url": "http://127.0.0.1:{}/v1".format(self.lm)}}))
        return str(path)

    def test_where_lm_studio_is(self):
        """is_lmstudio over the address forms, without sending: port 1234 is LM Studio's default and is never contacted here."""
        self.configure(local="http://localhost:7001/v1", models="http://127.0.0.1:7002/v1")
        cases = [  # url, TP_LMSTUDIO_ENDPOINT, LM Studio?
            ("http://127.0.0.1:1234/v1/chat/completions", None, True),
            ("http://localhost:1234/v1", None, True),
            ("http://LOCALHOST.:1234/v1", None, True),
            ("http://[::1]:1234/v1", None, True),
            ("http://2130706433:1234/v1", None, True),  # 127.0.0.1 as one number
            ("http://127.0.0.1:7001/v1", None, True),  # config/local.json followup on localhost: one host
            ("http://localhost:7002/v1", None, True),  # agent/models.json lmstudio on 127.0.0.1: one host
            ("http://127.0.0.1:7003/v1", "localhost:7003", True),
            ("http://127.0.0.1:7003/v1", "http://[::1]:7003/v1", True),
            ("http://127.0.0.1:7003/v1", None, False),  # another loopback port: a stub
            ("http://127.0.0.1:7003/v1", "not a port", False),  # an unreadable override adds nothing, the defaults stay
            ("http://127.0.0.1:1234/v1", "not a port", True),
            ("http://192.0.2.7:1234/v1", None, False),  # another host on port 1234
            ("http://127.0.0.1/v1", None, False),  # port 80
        ]
        for url, endpoint, expected in cases:
            with self.subTest(url=url, endpoint=endpoint):
                env = {k: v for k, v in os.environ.items() if k != "TP_LMSTUDIO_ENDPOINT"}
                if endpoint is not None:
                    env["TP_LMSTUDIO_ENDPOINT"] = endpoint
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertIs(router.is_lmstudio(url), expected)

    def test_two_processes_share_one_counter_and_run_one_request_at_a_time(self):
        """A's first request is held at the stub until B is seen waiting for the run's lock (a non-blocking try failed, so B
        then blocks); only then is it released. Without the lock, B's request reaches the stub while A's is held, and the test
        fails on that event. Each child sends two; the budget of 3 refuses one."""
        env = dict(os.environ, **self.env("3"))
        code = ("import fcntl, sys, urllib.request; sys.path.insert(0, {!r}); import router\n"
                "real = fcntl.flock\n"
                "def flock(fd, op):\n"
                "    try:\n"
                "        return real(fd, op | fcntl.LOCK_NB)\n"
                "    except BlockingIOError:\n"
                "        print('waiting', flush=True)\n"
                "        return real(fd, op)\n"
                "router.fcntl.flock = flock\n"
                "for _ in range(2):\n"
                "    try:\n"
                "        with router.local_open(urllib.request.Request('http://127.0.0.1:{}/x', data=b'{{}}'), 30) as r: r.read()\n"
                "        print('ok', flush=True)\n"
                "    except router.LmStudioBudgetError: print('refused', flush=True)\n").format(str(ROOT / "lib"), self.lm)
        while not self.events.empty():
            self.events.get_nowait()
        self.release.clear()
        self.hold[0] = True
        self.addCleanup(self.hold.__setitem__, 0, False)
        self.addCleanup(self.release.set)
        procs, out = {}, {"A": [], "B": []}

        def start(name):
            procs[name] = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, text=True)
            self.addCleanup(procs[name].stdout.close)  # runs after the kill below (cleanups run last first)
            self.addCleanup(lambda p=procs[name]: p.poll() is None and p.kill())

            def pump(name=name):
                for line in procs[name].stdout:
                    self.events.put((name, line.strip()))
            threading.Thread(target=pump, daemon=True).start()

        def next_event():
            event = self.events.get(timeout=30)  # a bound on a hang, not a wait for time to pass
            if event[0] in out:
                out[event[0]].append(event[1])
            return event

        start("A")
        while next_event() != ("hit", 1):  # A's first request is at the stub and held: A holds the run's lock
            pass
        start("B")
        first = next_event()
        self.assertEqual(first, ("B", "waiting"), "B's request must wait for A's to finish, not reach LM Studio beside it")
        self.release.set()
        for p in procs.values():
            p.wait(timeout=30)
        while not self.events.empty():
            next_event()
        lines = [line for name in out for line in out[name] if line != "waiting"]
        self.assertEqual((lines.count("ok"), lines.count("refused")), (3, 1))
        self.assertEqual((self.counter.read_text(), self.hits.count(self.lm), self.peak[0]), ("3", 3, 1))


if __name__ == "__main__":
    unittest.main()
