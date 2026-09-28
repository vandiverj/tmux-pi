import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from automation import automation_policy, review_requirement  # noqa: E402
from preflight import select_preflight  # noqa: E402


class AutomationPolicyTests(unittest.TestCase):
    def decision(self, **kw):
        data = {"worker": "pi", "model": "slotstream/qwen", "thinking": "low", "route": "standard",
                "intent": "research", "review": "none", "scope": "brief", "placement": "main", "overrides": []}
        data.update(kw)
        return data

    def test_low_risk_read_only_investigation_cell_stays_pending(self):
        request = "Investigate where tp-review is documented; do not edit files"
        decision = self.decision(intent="research", scope="brief", placement="subagent")
        pf = select_preflight(request, decision, mode="investigate")
        policy = automation_policy(request, decision, pf)
        self.assertEqual(policy["action"], "recommend_manual")
        self.assertEqual(policy["candidate_cell"], "read_only_investigation")
        self.assertIsNone(policy["qualified_cell"])
        self.assertEqual(policy["qualification"]["state"], "pending")
        self.assertFalse(policy["review"]["required"])

    def test_small_scoped_edit_cell_stays_pending_until_dispatcher_fixtures(self):
        request = "Fix the README typo in the tp-task cleanup example"
        decision = self.decision(intent="implementation", scope="brief", route="coding", placement="subagent")
        pf = select_preflight(request, decision)
        policy = automation_policy(request, decision, pf)
        self.assertEqual(policy["action"], "recommend_manual")
        self.assertEqual(policy["candidate_cell"], "small_scoped_edit")
        self.assertIsNone(policy["qualified_cell"])
        self.assertEqual(policy["qualification"]["state"], "pending")

    def test_structural_medium_risk_requires_report_only_review_and_manual_recommendation(self):
        request = "Add a new persistence service boundary for invoices and change the public API contract"
        decision = self.decision(intent="implementation", scope="sustained", route="coding", placement="subagent")
        pf = select_preflight(request, decision)
        review = review_requirement(request, decision, pf)
        self.assertTrue(review["required"])
        self.assertIn("structural + medium risk trigger", review["reasons"])
        policy = automation_policy(request, decision, pf)
        self.assertEqual(policy["action"], "recommend_manual")
        self.assertIn("review required", policy["notice"])

    def test_sensitive_and_explicit_review_raise_review_independently(self):
        sensitive = "Review logs for PII and customer tokens"
        self.assertTrue(review_requirement(sensitive, self.decision(intent="review"), select_preflight(sensitive))["required"])
        explicit = self.decision(intent="communication_or_documentation", review="required", overrides=["review"])
        self.assertTrue(review_requirement("Explain tp-view", explicit, select_preflight("Explain tp-view", explicit))["required"])

    def test_unqualified_risky_or_broad_work_stays_manual_unless_explicit_placement(self):
        request = "Deploy the migration to production"
        decision = self.decision(intent="configuration_or_operations", scope="multi_step", route="expert", placement="subagent")
        pf = select_preflight(request, decision)
        self.assertEqual(automation_policy(request, decision, pf)["action"], "recommend_manual")
        review_only = dict(decision, overrides=["review"], review="required")
        self.assertEqual(automation_policy(request, review_only, pf)["action"], "recommend_manual")
        explicit = dict(decision, overrides=["placement"])
        policy = automation_policy(request, explicit, pf)
        self.assertEqual(policy["action"], "auto_dispatch")
        self.assertEqual(policy["mode"], "explicit_human_request")
        self.assertTrue(policy["confirmation_required"])

    def test_explicit_sensitive_high_risk_requires_separate_confirmation(self):
        request = "Update password hashing in auth.py"
        decision = self.decision(intent="implementation", placement="subagent", overrides=["placement"])
        pf = select_preflight(request, decision)
        policy = automation_policy(request, decision, pf)
        self.assertEqual(policy["review"]["risk"], "high")
        self.assertTrue(policy["review"]["required"])
        self.assertTrue(policy["confirmation_required"])
        self.assertEqual(policy["action"], "auto_dispatch")
        self.assertIn("high-risk", policy["notice"])
        # +review alone cannot authorize placement or a dispatch confirmation.
        review_only = automation_policy(request, dict(decision, overrides=["review"]), pf)
        self.assertFalse(review_only["confirmation_required"])
        self.assertEqual(review_only["action"], "recommend_manual")


if __name__ == "__main__":
    unittest.main()
