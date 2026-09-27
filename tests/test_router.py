import copy
import json
import math
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from router import (QUESTIONS, REVIEWS, RouterError, feature_names, laya_decision, laya_route, load_config,
                    load_model, questions_digest, review_policy, review_task, route_openai, route_task,
                    validate_decision)  # noqa: E402


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
        self.config = load_config(ROOT / "config/router.json")

    def reply(self, decision, finish_reason="stop"):
        return Response({"choices": [{"message": {"content": json.dumps(decision)},
                                       "finish_reason": finish_reason}]})

    def test_valid_completion_maps_to_spawn_arguments(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return self.reply({"model": "claude-sonnet-5", "thinking": "high"})

        result = route_openai("Review this change", self.config, opener=open_request)
        self.assertEqual(result, {"worker": "claude", "model": "claude-sonnet-5", "thinking": "high"})
        self.assertEqual(captured["url"], "http://127.0.0.1:11435/v1/chat/completions")
        self.assertEqual(captured["body"]["model"], "qwen3.8-flash-next:4bit")
        self.assertEqual(captured["body"]["messages"][1]["content"], "Review this change")
        self.assertEqual(captured["timeout"], 60)

    def test_rejects_unlisted_model_and_thinking(self):
        with self.assertRaisesRegex(RouterError, "configured roster"):
            validate_decision({"model": "other", "thinking": "low"}, self.config)
        with self.assertRaisesRegex(RouterError, "not allowed"):
            validate_decision({"model": "claude-sonnet-5", "thinking": "off"}, self.config)
        with self.assertRaisesRegex(RouterError, "exactly model and thinking"):
            validate_decision({"model": "claude-sonnet-5", "thinking": "low", "worker": "pi"}, self.config)

    def test_rejects_invalid_or_truncated_completion(self):
        with self.assertRaisesRegex(RouterError, "invalid JSON completion"):
            route_openai("task", self.config, opener=lambda *_args, **_kwargs: Response(
                {"choices": [{"message": {"content": "```json"}, "finish_reason": "stop"}]}))
        with self.assertRaisesRegex(RouterError, "incomplete"):
            route_openai("task", self.config, opener=lambda *_args, **_kwargs:
                         self.reply({"model": "claude-sonnet-5", "thinking": "low"}, "length"))

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

    def laya_reply(self, intent="implementation", difficulty=1, tools=0.5, sensitive=0.1, risk=0.1, changes=0.5):
        intents = {key: (0.95 if key == intent else 0.01) for key in QUESTIONS["task_intent"]["criteria"]}
        return {"routing": {"model": "typed-decisions"}, "answers": {
            "task_intent": {"choice": intent, "probabilities": intents},
            "difficulty": {"score": difficulty, "confidence": 0.8,
                           "probabilities": {"0": 0.1, "1": 0.8, "2": 0.05, "3": 0.05}},
            "needs_tools": {"noul": tools},
            "is_sensitive": {"noul": sensitive},
            "is_high_risk": {"noul": risk},
            "changes": {"noul": changes},
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
        return {"questions": questions_digest(), "features": names, "intent": intent,
                "route": head(["simple", "expert"], "is_sensitive", [-20.0, 20.0]),
                "gate": {**head(["none", "review"], "changes", [-20.0, 20.0]), "threshold": gate},
                "level": {**head(["optional", "required"], "needs_tools", [-20.0, 20.0]), "threshold": level}}

    def with_model(self, model):
        from tempfile import TemporaryDirectory
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        raw = json.loads((ROOT / "config/router.json").read_text())
        (Path(directory.name) / "router.json").write_text(json.dumps(raw))
        (Path(directory.name) / "laya-model.json").write_text(json.dumps(model))
        return load_config(Path(directory.name) / "router.json")

    def test_shipped_model_matches_questions(self):
        self.assertEqual(self.config["model"]["questions"], questions_digest())
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

    def test_explicit_openai_backend(self):
        result = route_task("task", self.config, backend="openai", opener=lambda *_args, **_kwargs:
                            self.reply({"model": "claude-sonnet-5", "thinking": "high"}))
        self.assertEqual(result, {"worker": "claude", "model": "claude-sonnet-5", "thinking": "high"})


if __name__ == "__main__":
    unittest.main()
