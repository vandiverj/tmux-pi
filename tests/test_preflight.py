import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from preflight import create_preflight_record, merge_main_contract, select_preflight, strip_mode_marker  # noqa: E402


class PreflightTests(unittest.TestCase):
    def decision(self, **kw):
        data = {"worker": "pi", "model": "slotstream/qwen", "thinking": "low", "route": "standard",
                "intent": "communication_or_documentation", "review": "none", "scope": "brief", "placement": "main"}
        data.update(kw)
        return data

    def test_direct_low_risk_can_remain_tier0(self):
        pf = select_preflight("Explain what tp-view does", self.decision())
        self.assertEqual((pf["mode"], pf["artifact_tier"], pf["durable"], pf["stop"]), ("direct", 0, False, False))
        self.assertIn("answer compactly", "; ".join(pf["proof_path"]))

    def test_plan_and_prototype_are_durable_stop_no_edit_modes(self):
        for mode in ("plan", "prototype"):
            with self.subTest(mode=mode):
                pf = select_preflight("Add SAML SSO", mode=mode)
                self.assertEqual(pf["mode"], mode)
                self.assertGreaterEqual(pf["artifact_tier"], 1)
                self.assertTrue(pf["durable"])
                self.assertTrue(pf["stop"])
                self.assertTrue(pf["no_edits"])
                self.assertIn("no edit", "; ".join(pf["allowed"]["tools"]))

    def test_investigate_is_stop_mode_but_only_durable_when_material(self):
        brief = select_preflight("What is UUID v7?", mode="investigate")
        self.assertEqual((brief["mode"], brief["stop"], brief["artifact_tier"], brief["durable"]), ("investigate", True, 0, False))
        deep = select_preflight("Investigate why production customer imports duplicate records and report evidence", mode="investigate")
        self.assertTrue(deep["stop"])
        self.assertTrue(deep["no_edits"])
        self.assertGreaterEqual(deep["artifact_tier"], 1)
        self.assertTrue(deep["durable"])

    def test_structural_shape_promotes_to_tier2(self):
        pf = select_preflight("Design an event bus so billing and analytics stop calling each other", self.decision(intent="planning", scope="sustained", placement="subagent"))
        self.assertGreaterEqual(pf["artifact_tier"], 2)
        self.assertIn("structural shape", pf["triggers"])
        self.assertIn("Promoted", pf["promotion_notice"])

    def test_pii_security_high_risk_promotes_to_tier3(self):
        pf = select_preflight("Plan GDPR deletion for production customer billing data", self.decision(intent="planning", scope="multi_step", review="required", sensitive=True, high_risk=True))
        self.assertEqual(pf["artifact_tier"], 3)
        self.assertIn("sensitive/security/data shape", pf["triggers"])
        self.assertIn("high-risk", "; ".join(pf["triggers"]))

    def test_main_contract_intersects_scaffold_edit_authority_in_worker_packet(self):
        request = ("+delegate Read only fixture.txt in this scratch workspace and answer: what color is cedar? "
                   "Do not edit files, add MCPs, or request credentials. If delegation is not authorized, "
                   "report that and do not spawn.")
        # Reproduce the live smoke's overclassified configuration route, not a
        # corrected router prediction. Main's explicit read-only frame narrows it.
        decision = self.decision(intent="configuration_or_operations", route="coding", scope="multi_step",
                                 placement="subagent", review="required", task=request)
        with tempfile.TemporaryDirectory() as directory:
            for label, scaffold_request, main_no_edits, expected_no_edits in (
                ("live-read-only", request, True, True),
                ("scaffold-denies", "Investigate the issue and report evidence", False, True),
                ("authorized-edit", "Fix a typo in README.md", False, False),
            ):
                with self.subTest(case=label):
                    pf = select_preflight(scaffold_request, decision=decision if label == "live-read-only" else
                                          self.decision(intent="research", placement="subagent") if label == "scaffold-denies" else
                                          self.decision(intent="implementation", placement="subagent"))
                    self.assertEqual(pf["no_edits"], label == "scaffold-denies")
                    sha = hashlib.sha256(scaffold_request.encode()).hexdigest()
                    contract = {
                        "handshake_id": "r5-" + label, "request_sha256": sha, "action": "delegate",
                        "mode": "delegated", "scope": "multi_step", "task_summary": scaffold_request,
                        "frame": "Read only; do not edit files" if main_no_edits else "Stay within scope",
                        "assumptions": [], "no_edits": main_no_edits, "risk_notes": [],
                        "proof_path": ["report evidence"], "done_when": ["answer provided"],
                    }
                    result = subprocess.run(
                        [str(ROOT / "bin/tp-task"), "--state-dir", directory, "main-contract", "--json",
                         "--handshake-id", contract["handshake_id"], "--request-sha256", sha,
                         "--preflight-json", json.dumps(pf), "--decision-json", json.dumps(decision)],
                        input=json.dumps(contract), text=True, capture_output=True, check=True,
                    )
                    packet = json.loads(result.stdout)
                    enriched = packet["enriched_preflight"]
                    tools = enriched["allowed"]["tools"]
                    self.assertEqual(enriched["no_edits"], expected_no_edits)
                    self.assertEqual(enriched["stop"], pf["stop"])
                    self.assertEqual(enriched["mode"], pf["mode"])
                    self.assertEqual(packet["policy_input"]["preflight"], enriched)
                    self.assertIn("no_edits={}".format(expected_no_edits), packet["note"])
                    self.assertIn("read/search", tools)
                    if expected_no_edits:
                        self.assertIn("no edit/apply/write tools", tools)
                        self.assertNotIn("file edits within task scope", tools)
                        self.assertNotIn("shell/test/verification commands", tools)
                        self.assertIn("no edit/apply/write tools", packet["note"])
                    else:
                        self.assertIn("file edits within task scope", tools)
                    if label == "live-read-only":
                        self.assertIn("non-destructive verification commands only if needed for the answer", tools)
                        self.assertFalse(pf["no_edits"])  # original scaffold remains unchanged
                        self.assertIn("file edits within task scope", pf["allowed"]["tools"])

    def test_main_false_cannot_relax_stop_scaffold(self):
        pf = select_preflight("+plan Outline read-only steps", mode="plan")
        merged = merge_main_contract(pf, {"no_edits": False, "frame": "Plan only"})
        self.assertTrue(merged["no_edits"])
        self.assertTrue(merged["stop"])
        self.assertNotIn("non-destructive verification commands only if needed for the answer", merged["allowed"]["tools"])

    def test_create_preflight_record_writes_task_contract_section(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            pf = select_preflight("+plan ABC-12 Replace auth provider", mode="plan")
            rec = create_preflight_record(state_dir, strip_mode_marker("+plan ABC-12 Replace auth provider"), pf, name="plan")
            task = Path(rec["record"]).read_text()
            meta = json.loads((Path(rec["dir"]) / "meta.json").read_text())
            self.assertIn("## Task Contract", task)
            self.assertIn("Artifact Tier:** `Tier", task)
            self.assertIn("No Edits:** yes", task)
            self.assertEqual(meta["preflight"]["mode"], "plan")
            self.assertTrue((state_dir / "tasks" / rec["id"]).exists())


if __name__ == "__main__":
    unittest.main()
