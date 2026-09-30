import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run_json(*args, input_text=None):
    p = subprocess.run(args, input=input_text, text=True, cwd=ROOT, capture_output=True, check=True)
    return json.loads(p.stdout)


class DispatcherR2PathTests(unittest.TestCase):
    def decision(self, **kw):
        data = {
            "worker": "pi",
            "model": "slotstream/qwen",
            "thinking": "low",
            "route": "standard",
            "intent": "research",
            "review": "none",
            "scope": "brief",
            "placement": "subagent",
            "overrides": [],
            "task": "Find where retries are configured",
        }
        data.update(kw)
        return data

    def preflight(self, request, decision):
        return run_json(str(ROOT / "bin/tp-task"), "preflight", "--json", "--decision-json", json.dumps(decision), request)["preflight"]

    def automation(self, request, decision, preflight):
        return run_json(
            str(ROOT / "bin/tp-task"),
            "automation",
            "--json",
            "--decision-json",
            json.dumps(decision),
            "--preflight-json",
            json.dumps(preflight),
            request,
        )

    def test_real_dispatcher_read_only_shape_auto_dispatches(self):
        request = "Find where retries are configured"
        decision = self.decision(task=request, intent="research", placement="subagent")
        pf = self.preflight(request, decision)
        self.assertTrue(pf["no_edits"])
        self.assertNotIn("file edits within task scope", pf["allowed"]["tools"])
        policy = self.automation(request, decision, pf)
        self.assertEqual(policy["candidate_cell"], "read_only_investigation")
        self.assertEqual(policy["qualification"]["state"], "enabled")
        self.assertEqual(policy["action"], "auto_dispatch")

    def test_real_dispatcher_small_edit_shape_is_pending_not_auto(self):
        request = "Fix typo in README.md"
        decision = self.decision(task=request, intent="implementation", route="coding", placement="subagent")
        pf = self.preflight(request, decision)
        self.assertFalse(pf["no_edits"])
        policy = self.automation(request, decision, pf)
        self.assertEqual(policy["candidate_cell"], "small_scoped_edit")
        self.assertEqual(policy["action"], "recommend_manual")

    def test_review_marker_alone_does_not_dispatch_high_risk_task(self):
        request = "Rotate the production database credentials and delete old backups"
        decision = self.decision(
            task=request,
            intent="configuration_or_operations",
            route="expert",
            review="required",
            scope="multi_step",
            placement="subagent",
            overrides=["review"],
        )
        pf = self.preflight(request, decision)
        policy = self.automation(request, decision, pf)
        self.assertEqual(policy["action"], "recommend_manual")
        self.assertTrue(policy["review"]["required"])

    def test_delegate_marker_is_explicit_placement_not_qualification(self):
        request = "Fix typo in README.md"
        decision = self.decision(task=request, intent="implementation", route="coding", placement="subagent", overrides=["placement"])
        pf = self.preflight(request, decision)
        policy = self.automation(request, decision, pf)
        self.assertEqual(policy["action"], "auto_dispatch")
        self.assertEqual(policy["mode"], "explicit_human_request")
        self.assertIsNone(policy["qualified_cell"])

    def test_main_direct_tiny_request_is_read_only_not_auto_dispatched(self):
        request = "What is tp-view?"
        decision = self.decision(task=request, intent="communication_or_documentation", placement="main")
        pf = self.preflight(request, decision)
        policy = self.automation(request, decision, pf)
        self.assertEqual(pf["mode"], "direct")
        self.assertTrue(pf["no_edits"])
        self.assertEqual(policy["candidate_cell"], "read_only_investigation")
        self.assertIsNone(policy["qualified_cell"])
        self.assertEqual(policy["action"], "recommend_manual")
        self.assertFalse(policy["review"]["required"])


if __name__ == "__main__":
    unittest.main()
