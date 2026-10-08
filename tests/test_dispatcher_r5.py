import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = ROOT / "agent/extensions/dispatcher.ts"
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from task_record import write_record  # noqa: E402
from test_dispatcher_runtime import scenario  # noqa: E402


class DispatcherR5Fixtures(unittest.TestCase):
    def decision(self, **kw):
        data = {
            "worker": "pi",
            "model": "slotstream/qwen",
            "thinking": "low",
            "route": "coding",
            "intent": "implementation",
            "review": "none",
            "scope": "brief",
            "placement": "subagent",
            "overrides": ["placement"],
            "task": "Fix typo in README.md",
        }
        data.update(kw)
        return data

    def run_json(self, *args, input_text=None):
        p = subprocess.run(args, cwd=ROOT, input=input_text, text=True, capture_output=True, check=True)
        return json.loads(p.stdout)

    def test_spawn_and_edit_guards_exist_during_active_handshake(self):
        out = scenario("stop")
        self.assertTrue(all(out["guards"][name] for name in ("send", "absolute", "variable", "tmux", "edit", "shellwrite")))
        self.assertTrue(out["guards"]["read"])  # shell with arguments is not a safe read-only API

    def test_main_contract_cli_persists_handoff_and_recorded_orient_frame(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            request = "Plan the API boundary"
            sha = hashlib.sha256(request.encode()).hexdigest()
            decision = self.decision(intent="planning", review="required", scope="sustained", placement="subagent", task=request)
            pf = self.run_json(str(ROOT / "bin/tp-task"), "--state-dir", str(state), "preflight", "--json", "--decision-json", json.dumps(decision), request)["preflight"]
            contract = {
                "handshake_id": "r5-fixture",
                "request_sha256": sha,
                "action": "stop",
                "mode": "plan",
                "task_summary": "Plan the API boundary.",
                "frame": "Plan only; stop before edits.",
                "assumptions": ["No implementation in this turn."],
                "no_edits": True,
                "scope": "sustained",
                "risk_notes": ["architecture boundary"],
                "proof_path": ["write plan", "list open questions"],
                "done_when": ["plan is in chat"],
            }
            out = self.run_json(
                str(ROOT / "bin/tp-task"), "--state-dir", str(state), "main-contract", "--json",
                "--handshake-id", "r5-fixture", "--request-sha256", sha,
                "--decision-json", json.dumps(decision), "--preflight-json", json.dumps(pf),
                input_text=json.dumps(contract),
            )
            self.assertTrue((state / "handoffs" / "r5-fixture" / "main-contract.json").exists())
            rec_dir = Path(out["record"]["dir"])
            task_md = (rec_dir / "task.md").read_text(encoding="utf-8")
            self.assertIn("## Main Orient/Frame", task_md)
            self.assertIn("Plan only; stop before edits", task_md)

    def test_policy_after_contract_marks_high_risk_delegate_confirmation(self):
        request = "Deploy the migration to production and delete old backups"
        decision = self.decision(
            worker="claude", model="sonnet", thinking="high", route="expert",
            intent="configuration_or_operations", review="required", scope="multi_step",
            placement="subagent", overrides=["placement"], task=request,
        )
        pf = self.run_json(str(ROOT / "bin/tp-task"), "preflight", "--json", "--decision-json", json.dumps(decision), request)["preflight"]
        sha = hashlib.sha256(request.encode()).hexdigest()
        contract = {
            "schema": "tmux-pi-main-contract:v1",
            "handshake_id": "r5-risk",
            "request_sha256": sha,
            "action": "delegate",
            "mode": "delegated",
            "task_summary": "Deploy migration.",
            "frame": "High-risk production operation.",
            "assumptions": [],
            "no_edits": False,
            "scope": "multi_step",
            "risk_notes": ["production", "delete backups"],
            "proof_path": ["rollback evidence"],
            "done_when": ["operator confirms"],
        }
        policy = self.run_json(
            str(ROOT / "bin/tp-task"), "automation", "--json", "--decision-json", json.dumps(decision),
            "--preflight-json", json.dumps(pf), "--main-contract-json", json.dumps(contract), request,
        )
        self.assertEqual(policy["action"], "auto_dispatch")
        self.assertTrue(policy["confirmation_required"])
        self.assertIn("separate human confirmation", policy["notice"])

    def test_task_record_renders_main_contract_from_legacy_spawn_meta(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "tasks" / "auto-1"
            d.mkdir(parents=True)
            contract = {
                "schema": "tmux-pi-main-contract:v1",
                "handshake_id": "r5-spawn",
                "request_sha256": "0" * 64,
                "action": "delegate",
                "mode": "delegated",
                "task_summary": "Fix docs.",
                "frame": "Docs-only edit.",
                "assumptions": [],
                "no_edits": False,
                "scope": "brief",
                "risk_notes": [],
                "proof_path": ["diff"],
                "done_when": ["docs fixed"],
            }
            meta = {"id": "auto-1", "worker": "pi", "model": "slotstream/qwen", "thinking": "low", "created": "2026-09-08T00:00:00Z", "main_contract": contract}
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
            (d / "main-contract.json").write_text(json.dumps(contract), encoding="utf-8")
            (d / "request.md").write_text("Fix docs\n", encoding="utf-8")
            write_record(d, lifecycle_state="starting", meta=meta)
            body = (d / "task.md").read_text(encoding="utf-8")
            self.assertIn("## Main Orient/Frame", body)
            self.assertIn("Docs-only edit", body)
            self.assertIn("main Orient/Frame contract", body)


if __name__ == "__main__":
    unittest.main()
