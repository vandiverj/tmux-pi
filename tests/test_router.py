import copy
import json
import math
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from router import (QUESTIONS, RouterError, laya_decision, load_config, route_openai,
                    route_task, validate_decision)  # noqa: E402


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
        bad = copy.deepcopy(self.config)
        bad["models"][0]["thinking"] = [{"bad": "level"}]
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router.json"
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(RouterError, "thinking must"):
                load_config(path)

    def laya_reply(self, difficulty=1, tools=0.1, private=0.1, sensitive=0.1, risk=0.1):
        return {"routing": {"model": "typed-decisions"}, "answers": {
            "difficulty": {"score": difficulty, "confidence": 0.8,
                           "probabilities": {"0": 0.1, "1": 0.8, "2": 0.05, "3": 0.05}},
            "needs_tools": {"noul": tools},
            "needs_private_context": {"noul": private},
            "is_sensitive": {"noul": sensitive},
            "is_high_risk": {"noul": risk},
        }}

    def test_laya_request_and_default_mapping(self):
        captured = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response(self.laya_reply(difficulty=0.8, tools=0.2, private=0.2))

        result = route_task("Summarize a note", self.config, opener=open_request)
        self.assertEqual(result, {"worker": "pi", "model": "slotstream/qwen3.8-flash-next:4bit", "thinking": "off"})
        self.assertEqual(captured["url"], "http://127.0.0.1:11436/v1/systemone")
        self.assertEqual(captured["body"], {"model": "typed-decisions",
                                             "state": {"request": "Summarize a note"}, "questions": QUESTIONS})
        self.assertEqual(captured["timeout"], 60)

    def test_laya_policy_tiers(self):
        cases = [
            ("simple", self.laya_reply(difficulty=0.8, tools=0.2, private=0.2),
             "slotstream/qwen3.8-flash-next:4bit", "off"),
            ("standard", self.laya_reply(difficulty=0.8, tools=0.8, private=0.2),
             "slotstream/qwen3.8-flash-next:4bit", "low"),
            ("coding", self.laya_reply(difficulty=1.5, tools=0.2, private=0.2),
             "openai-codex/gpt-5.6-terra", "medium"),
            ("complex", self.laya_reply(difficulty=2.5, tools=0.2, private=0.2),
             "openai-codex/gpt-6-sol", "high"),
            ("expert", self.laya_reply(difficulty=2.8, tools=0.2, private=0.2),
             "claude-opus-5-5", "high"),
            ("complex high risk", self.laya_reply(difficulty=2.5, risk=0.8, tools=0.2, private=0.2),
             "claude-opus-5-5", "high"),
            ("sensitive", self.laya_reply(sensitive=0.8, tools=0.2, private=0.2),
             "claude-opus-5-5", "high"),
            ("uncertain", self.laya_reply(tools=0.5, private=0.2), "claude-sonnet-5", "medium"),
        ]
        for name, reply, model, thinking in cases:
            with self.subTest(name=name):
                result = laya_decision(reply, self.config)
                self.assertEqual(result["model"], model)
                self.assertEqual(result["thinking"], thinking)

    def test_low_difficulty_confidence_does_not_gate_route(self):
        reply = self.laya_reply(difficulty=0.8, tools=0.2, private=0.2)
        reply["answers"]["difficulty"]["confidence"] = 0.1
        result = laya_decision(reply, self.config)
        self.assertEqual(result, {"worker": "pi", "model": "slotstream/qwen3.8-flash-next:4bit", "thinking": "off"})
        reply["answers"]["difficulty"]["probabilities"]["1"] = 0.1
        self.assertEqual(laya_decision(reply, self.config)["thinking"], "medium")

    def test_laya_malformed_and_numeric_failures(self):
        with self.assertRaisesRegex(RouterError, "missing required answers"):
            laya_decision({"routing": {"model": "typed-decisions"},
                           "answers": {"difficulty": {"score": 1}}}, self.config)
        with self.assertRaisesRegex(RouterError, "not routed through typed-decisions"):
            laya_decision({"routing": {"model": "english"}, "answers": self.laya_reply()["answers"]}, self.config)
        incomplete = self.laya_reply()
        del incomplete["answers"]["difficulty"]["probabilities"]["3"]
        with self.assertRaisesRegex(RouterError, "probabilities is incomplete"):
            laya_decision(incomplete, self.config)
        wrong_type = self.laya_reply()
        wrong_type["answers"]["needs_tools"]["type"] = "score"
        with self.assertRaisesRegex(RouterError, "wrong type"):
            laya_decision(wrong_type, self.config)
        for value in (None, "0.8", True, math.nan, math.inf, -0.1, 1.1):
            reply = self.laya_reply()
            reply["answers"]["needs_tools"]["noul"] = value
            with self.subTest(value=value), self.assertRaises(RouterError):
                laya_decision(reply, self.config)
        for value in ("2", True, math.nan, -1, 4):
            reply = self.laya_reply(difficulty=value)
            with self.subTest(difficulty=value), self.assertRaises(RouterError):
                laya_decision(reply, self.config)

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
        bad = copy.deepcopy(self.config)
        bad["routes"]["fallback"]["model"] = "unknown"
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router.json"
            path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(RouterError, "invalid fallback route"):
                load_config(path)

    def test_explicit_openai_backend(self):
        result = route_task("task", self.config, backend="openai", opener=lambda *_args, **_kwargs:
                            self.reply({"model": "claude-sonnet-5", "thinking": "high"}))
        self.assertEqual(result, {"worker": "claude", "model": "claude-sonnet-5", "thinking": "high"})


if __name__ == "__main__":
    unittest.main()
