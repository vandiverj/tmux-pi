import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from capabilities import (REGISTRY_VERSION, disclosure_note, handle_capability_request, registry,  # noqa: E402
                          select_capabilities)


class CapabilityRegistryTests(unittest.TestCase):
    def decision(self, **kw):
        data = {"intent": "implementation", "review": "none", "scope": "brief", "placement": "subagent"}
        data.update(kw)
        return data

    def test_registry_has_required_initial_categories_and_metadata(self):
        reg = registry()
        self.assertEqual(reg["version"], REGISTRY_VERSION)
        categories = {c["category"] for c in reg["capabilities"]}
        self.assertEqual(categories, {
            "repository/filesystem search",
            "shell/test/verification",
            "YouTrack",
            "Context Engine",
            "read-only DB",
            "official documentation",
            "Sequential Thinking",
            "browser/rendered UI validation",
            "independent review",
        })
        for cap in reg["capabilities"]:
            for key in ("authority", "data_boundary", "qualification", "discovery"):
                self.assertIn(key, cap)

    def test_deterministic_selection_is_minimal_and_discloses_only_selected(self):
        manifest = select_capabilities(
            "Fix ticket: PROJ-12 login in src/auth.py and run pytest",
            preflight={"mode": "delegated", "artifact_tier": 1, "no_edits": False},
            decision=self.decision(),
        )
        self.assertIn("repo_search", manifest["selected"])
        self.assertNotIn("youtrack", manifest["selected"])
        self.assertIn("shell_test_verification", manifest["selected"])
        self.assertNotIn("readonly_db", manifest["selected"])
        self.assertIn("youtrack", {f["id"] for f in manifest["filtered"]})
        disclosed = {d["id"] for d in manifest["disclosures"]}
        self.assertEqual(disclosed, set(manifest["selected"]))
        note = disclosure_note(manifest)
        self.assertIn("CAPABILITY_REQUEST", note)
        self.assertIn("repository/filesystem search", note)
        self.assertNotIn("read-only DB: authority", note)

    def test_laya_candidates_cannot_grant_unrelated_or_unknown_categories(self):
        manifest = select_capabilities(
            "Fix login in src/auth.py",
            preflight={"mode": "delegated", "artifact_tier": 1, "no_edits": False},
            decision=self.decision(),
            laya=["read-only DB", "browser/rendered UI validation", "made up write tool"],
        )
        self.assertNotIn("readonly_db", manifest["selected"])
        self.assertNotIn("browser_rendered_ui", manifest["selected"])
        self.assertTrue(all(r.get("filtered") for r in manifest["laya_recommendations"]))

    def test_profile_only_browser_stays_filtered_when_rendered_ui_evidence_would_help(self):
        manifest = select_capabilities(
            "Validate the rendered login page CSS and capture visual evidence",
            preflight={"mode": "delegated", "artifact_tier": 1, "no_edits": False},
            decision=self.decision(intent="implementation"),
        )
        self.assertNotIn("browser_rendered_ui", manifest["selected"])
        self.assertIn("browser_rendered_ui", {f["id"] for f in manifest["filtered"]})

    def test_capability_request_auto_grants_only_approved_readonly_local(self):
        manifest = select_capabilities(
            "Plan a complex auth refactor",
            preflight={"mode": "plan", "artifact_tier": 2, "no_edits": True, "task": "Plan a complex auth refactor"},
            decision=self.decision(intent="planning", review="optional"),
        )
        manifest["selected"].remove("sequential_thinking")
        manifest["selected_categories"] = [c for c in manifest["selected_categories"] if c != "Sequential Thinking"]
        manifest["disclosures"] = [d for d in manifest["disclosures"] if d["id"] != "sequential_thinking"]
        updated, event = handle_capability_request(
            manifest,
            "Sequential Thinking | reason=complex plan tradeoffs",
            preflight={"mode": "plan", "artifact_tier": 2, "task": "Plan a complex auth refactor"},
            decision=self.decision(intent="planning"),
        )
        self.assertEqual(event["decision"], "granted")
        self.assertIn("sequential_thinking", updated["selected"])
        self.assertTrue(any(e["type"] == "capability_request" for e in updated["events"]))

    def test_external_write_and_expanded_data_requests_require_human(self):
        base = select_capabilities("Fix src/auth.py", preflight={"mode": "delegated", "artifact_tier": 1}, decision=self.decision())
        for request in ("YouTrack | ticket=ABC-1", "official documentation | fetch live docs", "read-only DB | query customer records", "browser/rendered UI validation | target=local"):
            with self.subTest(request=request):
                _updated, event = handle_capability_request(json.loads(json.dumps(base)), request)
                self.assertEqual(event["decision"], "human_required")
        _updated, event = handle_capability_request(json.loads(json.dumps(base)), "new write deploy tool")
        self.assertEqual(event["decision"], "human_required")

    def test_capability_resolve_approves_only_bounded_category_and_denies_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "tasks" / "worker-1"
            task.mkdir(parents=True)
            (task / "meta.json").write_text(json.dumps({"id": "worker-1", "worker": "pi", "pane": "%1"}))
            (task / "request.md").write_text("Plan complex tradeoffs\n")
            fake = root / "tmux"
            fake.write_text('#!/bin/sh\necho "$*" >> "$TMUX_LOG"\n')
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ.get("PATH", ""), TMUX_LOG=str(root / "signals"))
            for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
                env.pop(key, None)
            cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(root), "capability-resolve", "worker-1"]
            manifest = select_capabilities("Plan complex tradeoffs")
            manifest["selected"] = []
            manifest["selected_categories"] = []
            manifest["disclosures"] = []
            wrapped = {"capability_manifest": manifest}
            (task / "context-manifest.json").write_text(json.dumps(wrapped))
            pending = {"request": "Sequential Thinking | reason=tradeoffs",
                       "event": {"id": "sequential_thinking", "decision": "human_required"}}
            wrapped["capability_manifest"]["events"].append({"type": "capability_request", **pending["event"], "request": pending["request"]})
            (task / "context-manifest.json").write_text(json.dumps(wrapped))
            (task / "pending-capability.json").write_text(json.dumps(pending))
            result = subprocess.run(cmd + ["--approve", "--json"], env=env, capture_output=True, text=True, check=True)
            self.assertTrue(json.loads(result.stdout)["pending_cleared"])
            self.assertFalse((task / "pending-capability.json").exists())
            self.assertIn("sequential_thinking", json.loads((task / "context-manifest.json").read_text())["capability_manifest"]["selected"])
            self.assertIn("Human approved only Sequential Thinking", (root / "signals").read_text())
            self.assertIn("capability-resolve", (task / "checkpoints.json").read_text())
            pending = {"request": "read-only DB | query customer records",
                       "event": {"id": "readonly_db", "decision": "human_required"}}
            (task / "pending-capability.json").write_text(json.dumps(pending))
            denied_approval = subprocess.run(cmd + ["--approve"], env=env, capture_output=True, text=True)
            self.assertNotEqual(denied_approval.returncode, 0)
            self.assertTrue((task / "pending-capability.json").exists())
            subprocess.run(cmd + ["--deny"], env=env, capture_output=True, text=True, check=True)
            self.assertFalse((task / "pending-capability.json").exists())
            (task / "pending-capability.json").write_text(json.dumps(pending))
            worker_attempt = subprocess.run(cmd + ["--deny"], env=dict(env, TP_TASK_DIR=str(task)),
                                            capture_output=True, text=True)
            self.assertNotEqual(worker_attempt.returncode, 0)
            self.assertIn("human-only", worker_attempt.stderr)
            self.assertTrue((task / "pending-capability.json").exists())
            broken = dict(env, PATH="/usr/bin:/bin")
            failed = subprocess.run(cmd + ["--approve"], env=broken, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertTrue((task / "pending-capability.json").exists())
            bypass = subprocess.run([str(ROOT / "bin/tp-task"), "capability-request", "--manifest",
                                     str(task / "context-manifest.json"), "--human-approved", "Sequential Thinking"],
                                    capture_output=True, text=True)
            self.assertNotEqual(bypass.returncode, 0)
            self.assertIn("capability-resolve", bypass.stderr)

    def test_capability_request_relevance_uses_original_task_not_request_words_and_caps(self):
        base = select_capabilities(
            "Fix src/auth.py",
            preflight={"mode": "delegated", "artifact_tier": 1, "task": "Fix src/auth.py"},
            decision=self.decision(),
        )
        _updated, event = handle_capability_request(json.loads(json.dumps(base)), "Sequential Thinking | reason=I want planning")
        self.assertEqual(event["decision"], "human_required")
        self.assertIn("not task-appropriate", event["reason"])

        capped = json.loads(json.dumps(base))
        for i in range(3):
            capped, event = handle_capability_request(capped, "Sequential Thinking | reason=plan", human_approved=True)
            self.assertEqual(event["decision"], "granted")
        capped, event = handle_capability_request(capped, "Sequential Thinking | reason=plan")
        self.assertEqual(event["decision"], "human_required")
        self.assertIn("limit", event["reason"])


if __name__ == "__main__":
    unittest.main()
