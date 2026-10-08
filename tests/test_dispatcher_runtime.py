"""Run the real Pi extension with fake events, UI and child-process boundary."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests/dispatcher_runtime.mjs"


def scenario(name):
    result = subprocess.run(["node", "--experimental-strip-types", str(HARNESS), name],
                            cwd=ROOT, text=True, capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


class DispatcherRuntime(unittest.TestCase):
    def test_gate_and_low_risk_direct_contract(self):
        d = scenario("direct")
        self.assertTrue(d["mismatch"])
        self.assertTrue(d["duplicate"])
        for key in ("send", "resolve", "reviewResolve", "globalResolve", "globalReviewResolve", "absolute", "variable", "tmux", "shellwrite", "edit", "read", "gitOutput", "gitShowOutput", "rgPre", "rgPreEq", "extDiff"):
            self.assertTrue(d["guards"][key], key)
        self.assertFalse(d["editAfter"])
        self.assertTrue(d["postContractResolve"])
        self.assertTrue(d["postContractReviewResolve"])
        self.assertFalse(d["afterEndBeforeSettle"]["edit"])
        self.assertTrue(all(d["afterEnd"][k] for k in ("edit", "bash", "powershell", "unknown")))
        self.assertEqual(d["decisionAtContract"]["intent"], "communication_or_documentation")
        self.assertEqual(d["decisionAtContract"]["placement"], "main")
        self.assertEqual(d["scaffold"] and json.loads(d["scaffold"])["original_request"], "+here Explain tp-view")
        self.assertEqual(d["mainContract"]["schema"], "tmux-pi-main-contract:v1")
        self.assertEqual(d["mainContract"]["request_sha256"], json.loads(d["scaffold"])["request_sha256"])
        self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]))
        names = [c["name"] for c in d["calls"]]
        operations = [c["args"][0] for c in d["calls"] if c["name"] == "tp-task"]
        self.assertLess(operations.index("main-contract"), operations.index("automation"))
        self.assertTrue(any(c["name"] == "tp-task" and c["args"][0] == "main-contract" for c in d["calls"]))

    def test_explicit_delegate_omitted_contract_is_visible_without_dispatch(self):
        d = scenario("delegate-omitted")
        hidden = d["hidden"]
        self.assertFalse(hidden["display"])
        self.assertIn("original user explicitly requested +delegate", hidden["content"])
        self.assertIn("MUST call tmux_pi_commit_contract with action=delegate", hidden["content"])
        self.assertIn("Before answering the task", hidden["content"])
        self.assertIn("Policy may refuse dispatch or require separate human confirmation", hidden["content"])
        self.assertIn("No worker starts without persisted contract and policy approval", hidden["content"])
        self.assertTrue(d["preContractTools"]["edit"])
        self.assertTrue(d["after"]["edit"])
        self.assertEqual(len(d["messages"]), 1)
        notice = d["messages"][0]
        self.assertEqual(notice["details"]["kind"], "delegate-contract-omitted")
        self.assertIn("not delegated; no contract committed", notice["content"])
        self.assertNotIn("authorization denied", notice["content"].lower())
        self.assertFalse(any(c["name"] in ("tp-spawn", "tp-review") or
                             c["name"] == "tp-task" and c["args"][0] in ("main-contract", "automation")
                             for c in d["calls"]))

    def test_policy_review_propagates_to_worker_and_runs(self):
        d = scenario("review")
        self.assertTrue(d["terminate"])
        spawn = next(c for c in d["calls"] if c["name"] == "tp-spawn")
        self.assertIn("A reviewer on another provider", ' '.join(spawn["args"]))
        self.assertEqual(len([c for c in d["calls"] if c["name"] == "tp-review"]), 1)

    def test_required_review_is_durable_before_review_and_collects_only_after_independent_approval(self):
        d = scenario("review-gate")
        gate = d["reviewGate"]
        self.assertEqual(d["decisionAtContract"]["review"], "required")
        self.assertEqual(gate["marker"]["status"], "pending_review")
        self.assertTrue(gate["preReviewCollectDenied"])
        self.assertTrue(gate["restartCollectDenied"])
        self.assertEqual(gate["approval"]["status"], "approved")
        self.assertEqual(gate["approval"]["approval"]["reviewer_id"], "review-1")
        self.assertTrue(gate["approvedCollect"])
        self.assertEqual([m["kind"] for m in d["messages"]][-1], "result")

    def test_research_dispatch_persists_review_and_decision_before_result(self):
        d = scenario("research-gate")
        self.assertEqual(d["researchGate"]["marker"]["status"], "pending_review")
        self.assertEqual(d["researchGate"]["decision"]["review"], "required")
        self.assertEqual(d["researchGate"]["decision"]["intent"], "research")
        self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_no_hand_slash_extension_and_queued_are_closed(self):
        self.assertEqual(scenario("queued")["queued"]["action"], "handled")
        for name in ("no-hand", "queued"):
            tools = scenario(name)["guards"]
            self.assertTrue(all(tools[k] for k in ("edit", "bash", "powershell", "unknown")))
            self.assertFalse(tools["read"])
            self.assertFalse(tools["contract"])
        for name in ("direct", "stop"):
            tools = scenario(name)["preContractTools"]
            self.assertTrue(all(tools[k] for k in ("edit", "bash", "powershell", "unknown")))
            self.assertFalse(tools["read"])
            self.assertFalse(tools["contract"])
        for name in ("slash", "extension", "slash-off", "extension-off"):
            d = scenario(name)
            self.assertTrue(d["hidden"], name)
            self.assertTrue(d["guards"]["edit"], name)
            self.assertTrue(d["guards"]["shellwrite"], name)

    def test_expanded_templates_never_inherit_raw_policy_or_contract_hash(self):
        for name in ("expanded", "expanded-marker"):
            d = scenario(name)
            self.assertTrue(d["staleHashRejected"], name)
            self.assertTrue(d["editAfter"], name)
            self.assertFalse(d["terminate"], name)
            self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]))
            scaffold = json.loads(d["scaffold"])
            self.assertEqual(scaffold["effective_request"], "Update password hashing in auth.py")
            self.assertTrue(scaffold["unverified_expansion"])
            self.assertEqual(scaffold["decision"], {})
            self.assertEqual(scaffold["request_sha256"], d["mainContract"]["request_sha256"])
            self.assertIn("unverified-expansion", [m["kind"] for m in d["messages"]])
            self.assertNotIn("review-needs-human", [m["kind"] for m in d["messages"]])

    def test_direct_contract_on_subagent_route_cannot_edit(self):
        self.assertTrue(scenario("direct-subagent")["editAfter"])

    def test_capability_deletion_without_resolution_cannot_complete(self):
        d = scenario("capability-deleted")
        self.assertIn("capability", [m["kind"] for m in d["messages"]])
        self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))
        self.assertFalse(any(c["name"] == "tp-review" for c in d["calls"]))

    def test_capability_resolution_rejoins_review_and_collection(self):
        d = scenario("capability-resolved")
        self.assertTrue(d["capabilityResolution"])
        self.assertEqual([m["kind"] for m in d["messages"]][-2:], ["capability", "result"])
        self.assertTrue(any(c["name"] == "tp-review" for c in d["calls"]))
        self.assertTrue(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_failed_delegated_review_is_durable_and_inherited(self):
        d = scenario("review-failed")
        self.assertTrue(d["taskReviewMarker"])
        self.assertEqual(d["taskReview"], "needs_human")
        self.assertTrue(d["inheritedTaskReview"])
        self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_reload_during_pending_review_stops_old_watcher_without_collect(self):
        d = scenario("midreview-reload")
        self.assertEqual(d["reviewer"], 1)
        self.assertEqual(d["marker"]["status"], "pending_review")
        self.assertEqual(d["collected"], 0)
        self.assertNotIn("review-needs-human", d["messages"])

    def test_task_and_archived_blockers_inherit_review_without_policy_risk(self):
        for name in ("task-blocker", "archived-blocker", "archived-renamed", "unknown-marker", "wrong-id-resolved"):
            d = scenario(name)
            self.assertEqual(d["inheritedReview"], "required", name)
            self.assertTrue(d["editAfter"], name)
            self.assertEqual(d["newReviewMarkers"], [], name)
            self.assertEqual(json.loads(d["scaffold"])["blocked_by"], ["task:earlier-task"], name)

    def test_invalid_review_and_cancel_retain_durable_human_gate(self):
        for name in ("review-invalid", "kill"):
            d = scenario(name)
            self.assertTrue(d["taskReviewMarker"], name)
            self.assertEqual(d["taskReview"], "needs_human", name)
            self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]), name)

    def test_approval_with_notes_clears_the_gate_and_lists_the_notes(self):
        d = scenario("notes-verdict")
        done = next(m for m in d["messages"] if m["kind"] == "result")
        self.assertIn("approved it with notes", done["content"])
        self.assertIn("N1 test gap: no test for the empty case.", done["content"])
        self.assertIn("N2 wording", done["content"])
        self.assertNotIn("RESULT_STATUS", done["content"].split("Notes (")[1].split("Full review:")[0])
        self.assertTrue(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_notes_quoting_an_earlier_rounds_verdict_still_approve_with_notes(self):
        d = scenario("notes-quote")
        done = next(m for m in d["messages"] if m["kind"] == "result")
        self.assertIn("approved it with notes", done["content"])
        self.assertIn("Round 1 REQUEST_CHANGES F1 was fixed.", done["content"])
        self.assertTrue(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_unknown_or_note_less_verdicts_keep_the_human_gate(self):
        for name in ("unknown-verdict", "notes-empty", "notes-suffix", "approved-suffix"):
            d = scenario(name)
            self.assertEqual(d["taskReview"], "needs_human", name)
            self.assertIn("unparseable", d["reviewReasons"][-1], name)
            self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]), name)

    def test_final_review_round_requests_changes_without_collecting(self):
        d = scenario("review-changes")
        self.assertEqual(len([c for c in d["calls"] if c["name"] == "tp-review"]), 2)
        self.assertEqual(d["reviewRoundMarkers"], ["pending_review", "pending_review"])
        self.assertTrue(d["taskReviewMarker"])
        self.assertEqual(d["taskReview"], "needs_human")
        self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "collect" for c in d["calls"]))

    def test_malformed_and_forged_resolved_markers_never_unblock(self):
        malformed = scenario("malformed-marker")
        self.assertEqual(malformed["inputResult"]["action"], "handled")
        self.assertTrue(malformed["blocked"]["edit"])
        self.assertTrue(malformed["blocked"]["powershell"])
        self.assertNotIn("tp-spawn", malformed["calls"])
        forged = scenario("forged-resolved")
        self.assertTrue(forged["editAfter"])
        self.assertEqual(json.loads(forged["scaffold"])["blocked_by"], ["earlier"])

    def test_followup_retains_review_requirement(self):
        d = scenario("followup-blocked")
        self.assertEqual(d["decisionAtContract"]["review"], "none")
        self.assertTrue(any(c["name"] == "tp-review" for c in d["calls"]))
        self.assertEqual(d["upgradeAtSend"]["marker"]["status"], "pending_review")
        self.assertEqual(d["upgradeAtSend"]["decision"]["review"], "required")
        self.assertEqual(d["newReviewMarkers"], ["earlier"])

    def test_review_resolve_cli_requires_human_and_audits_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "handoffs" / "r5-review-fixture" / "review-required.json"
            marker.parent.mkdir(parents=True)
            marker.write_text(json.dumps({"handshake_id": "r5-review-fixture", "required": True,
                                          "status": "needs_human"}))
            cmd = [str(ROOT / "bin/tp-task"), "--state-dir", tmp, "review-resolve", "r5-review-fixture",
                   "--outcome", "reviewed", "--review-reference", "report.md", "--reason", "independent report checked",
                   "--confirm", "RESOLVE-REVIEW-REQUIRED"]
            self.assertNotEqual(subprocess.run(cmd, cwd=ROOT, env={"TP_TASK_DIR": "worker"},
                                               capture_output=True).returncode, 0)
            self.assertEqual(json.loads(marker.read_text())["status"], "needs_human")
            human_env = os.environ.copy()
            human_env.pop("TP_TASK_DIR", None)
            subprocess.run(cmd, cwd=ROOT, env=human_env, check=True, capture_output=True)
            self.assertEqual(json.loads(marker.read_text())["status"], "resolved")
            self.assertTrue((Path(tmp) / "review-resolutions.log").exists())

    def test_review_blocker_survives_new_turn_and_main_pi_branch(self):
        for name in ("review-turn", "main-pi"):
            d = scenario(name)
            self.assertEqual(d["decisionAtContract"]["review"], "required")
            self.assertTrue(d["reviewBlocker"])
            if name == "review-turn":
                self.assertEqual(d["newReviewMarkers"], ["earlier"])
                self.assertEqual(json.loads(d["scaffold"])["blocked_by"], ["earlier"])
            self.assertTrue(d["editAfter"])
            self.assertIn("review-needs-human", [m["kind"] for m in d["messages"]])
        resolved = scenario("resolved-turn")
        self.assertEqual(resolved["decisionAtContract"]["review"], "none")
        self.assertFalse(resolved["editAfter"])

    def test_capability_request_takes_priority_over_question(self):
        d = scenario("capability")
        self.assertIn("capability", [m["kind"] for m in d["messages"]])
        self.assertNotIn("question", [m["kind"] for m in d["messages"]])

    def test_kill_does_not_escalate(self):
        d = scenario("kill")
        self.assertIn("cancelled", [m["kind"] for m in d["messages"]])
        self.assertFalse(any(c["name"] == "tp-contract" and c["args"][0] == "escalate" for c in d["calls"]))
        self.assertEqual(len([c for c in d["calls"] if c["name"] == "tp-spawn"]), 1)

    def test_confirmation_and_auto_off_are_fail_closed(self):
        self.assertTrue(scenario("confirm")["terminate"])
        for name in ("decline", "no-ui", "auto-off"):
            d = scenario(name)
            self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]), name)
        self.assertIn("r5-policy", [m["kind"] for m in scenario("auto-off")["messages"]])
        self.assertTrue(scenario("direct-off")["editAfter"])

    def test_alignment_answer_starts_new_handshake_without_authority(self):
        d = scenario("alignment")
        self.assertEqual(d["continuation"]["action"], "continue")
        self.assertEqual(d["handoffs"], 2)
        self.assertTrue(d["editAfter"])
        self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]))

    def test_direct_review_retained_as_blocker(self):
        d = scenario("direct-review")
        self.assertTrue(d["reviewBlocker"])
        self.assertTrue(d["editAfter"])
        self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]))

    def test_stop_record_captures_even_without_contract_call(self):
        d = scenario("stop-no-commit")
        self.assertEqual(d["captured"], "Plan without tool call.")
        self.assertEqual(d["contractCalls"], 0)

    def test_retry_can_commit_after_agent_end_before_settlement(self):
        d = scenario("retry-contract")
        self.assertTrue(d["duplicate"])
        self.assertEqual(d["mainContract"]["schema"], "tmux-pi-main-contract:v1")
        self.assertFalse(any(c["name"] == "tp-spawn" for c in d["calls"]))

    def test_agent_end_can_retry_before_settlement_and_stop_capture(self):
        d = scenario("stop-retry")
        self.assertFalse(d["stopCapturedBeforeSettle"])
        self.assertEqual(d["capturedOutput"], "Retried final plan.")
        self.assertTrue(d["afterEndBeforeSettle"]["edit"])

    def test_stop_record_precedes_contract_and_captures_output(self):
        d = scenario("stop")
        self.assertTrue(d["wrongStop"])
        self.assertTrue(d["stopCaptured"])
        self.assertTrue(d["editAfter"])
        calls = d["calls"]
        record = next(i for i, c in enumerate(calls) if c["name"] == "tp-task" and "--record" in c["args"] and c["args"][0] == "preflight")
        contract = next(i for i, c in enumerate(calls) if c["name"] == "tp-task" and c["args"][0] == "main-contract")
        self.assertLess(record, contract)


if __name__ == "__main__":
    unittest.main()
