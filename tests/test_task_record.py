import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from task_record import (capture_main_output, end_session_retention, find_task, format_record_for_resume, parse_record, request_text, retention_report, set_pin,  # noqa: E402
                         ticket_in, write_record)


class TaskRecordTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "tasks").mkdir()

    def task(self, task_id="pi-120000", request="Fix TEST-123 login redirect", **meta):
        d = self.root / "tasks" / task_id
        d.mkdir(parents=True)
        data = {"id": task_id, "worker": "pi", "model": "slotstream/qwen", "thinking": "low",
                "created": "2026-09-08T00:00:00Z"}
        data.update(meta)
        (d / "meta.json").write_text(json.dumps(data))
        (d / "task.md").write_text(request + "\n")  # legacy raw task layout
        (d / "packet.md").write_text("# Task\n\n" + request + "\n")
        return d

    def test_legacy_task_md_migrates_to_canonical_record(self):
        d = self.task(request="ticket: ABC-42 Fix checkout timeout\n\nKeep the current API.")
        rec = write_record(d, lifecycle_state="working", detail="0m03s")
        text = (d / "task.md").read_text()
        self.assertIn("tmux-pi-task-record:v1", text)
        self.assertIn("# [ABC-42] ticket: ABC-42 Fix checkout timeout", text)
        self.assertIn("- **Lifecycle State:** `working`", text)
        self.assertIn("Keep the current API.", text)
        self.assertEqual((d / "request.md").read_text().strip(), "ticket: ABC-42 Fix checkout timeout\n\nKeep the current API.")
        self.assertEqual(request_text(d), "ticket: ABC-42 Fix checkout timeout\n\nKeep the current API.")
        self.assertEqual((rec["ticket"], rec["pinned"]), ("ABC-42", True))

    def test_result_artifact_and_next_action_are_visible(self):
        artifact = self.root / "report.md"
        artifact.write_text("Status: final\nDone\n")
        d = self.task(request="Summarize cache behavior", contract={"kind": "research", "artifact": str(artifact)})
        (d / "result.md").write_text("Answer: cache honors max-age.\n")
        write_record(d, lifecycle_state="settled")
        text = (d / "task.md").read_text()
        self.assertIn("Result file:", text)
        self.assertIn("Answer: cache honors max-age.", text)
        self.assertIn("Contract artifact:", text)
        self.assertIn("(final)", text)
        self.assertIn("Read/collect `result.md`", text)

    def test_find_and_resume_use_canonical_record(self):
        d = self.task(task_id="work-1", request="ticket: OPS-9 Update runbook")
        write_record(d, lifecycle_state="paused")
        matches = find_task(self.root, "OPS-9")
        self.assertEqual(len(matches), 1)
        body = format_record_for_resume(matches[0][0])
        self.assertIn("Task dir:", body)
        self.assertIn("[OPS-9] ticket: OPS-9 Update runbook", body)
        self.assertIn("Lifecycle State:** `paused`", body)

    def test_retention_dry_run_protects_ticket_pins_promoted_and_contract_artifacts(self):
        old = time.time() - 120 * 86400
        candidate = self.task(task_id="old-1", request="Clean transient notes")
        write_record(candidate, lifecycle_state="completed")
        ticket = self.task(task_id="ticket-1", request="ticket: PROJ-7 Fix prod alert")
        write_record(ticket, lifecycle_state="completed")
        pinned = self.task(task_id="pin-1", request="Keep this investigation")
        write_record(pinned, lifecycle_state="completed")
        set_pin(pinned, True, "important")
        artifact = self.root / "durable.md"
        artifact.write_text("Status: final\n")
        durable = self.task(task_id="durable-1", request="Research durable", contract={"kind": "research", "artifact": str(artifact)})
        write_record(durable, lifecycle_state="completed")
        promoted = self.task(task_id="promoted-1", request="Design service boundary", preflight={"artifact_tier": 2, "promotion_notice": "Promoted"})
        write_record(promoted, lifecycle_state="completed")
        for d in (candidate, ticket, pinned, durable, promoted):
            for p in list(d.rglob("*")) + [d]:
                os.utime(p, (old, old))
        rows = {r["id"]: r for r in retention_report(self.root, days=90, now=time.time())}
        self.assertTrue(rows["old-1"]["eligible"])
        self.assertFalse(rows["ticket-1"]["eligible"])
        self.assertIn("YouTrack ticket PROJ-7", "; ".join(rows["ticket-1"]["protected"]))
        self.assertFalse(rows["pin-1"]["eligible"])
        self.assertIn("important", "; ".join(rows["pin-1"]["protected"]))
        self.assertFalse(rows["durable-1"]["eligible"])
        self.assertIn("externally durable contract artifact exists", rows["durable-1"]["protected"])
        self.assertFalse(rows["promoted-1"]["eligible"])
        self.assertIn("promoted durable task record", rows["promoted-1"]["protected"])
        self.assertTrue(candidate.exists())  # dry-run reporting never deletes

    def test_cleanup_delete_requires_review_and_confirm_and_rechecks_protection(self):
        old = time.time() - 120 * 86400
        candidate = self.task(task_id="old-delete", request="Clean transient notes")
        write_record(candidate, lifecycle_state="completed")
        protected = self.task(task_id="keep-delete", request="ticket: OPS-10 Keep linked ticket")
        write_record(protected, lifecycle_state="completed")
        for d in (candidate, protected):
            for p in list(d.rglob("*")) + [d]:
                os.utime(p, (old, old))
        review = self.root / "cleanup-review.json"
        cmd = [str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "cleanup", "--dry-run", "--days", "90", "--write-review", str(review), "--json"]
        out = subprocess.run(cmd, text=True, capture_output=True, check=True)
        batch = json.loads(out.stdout)
        self.assertIn("old-delete", batch["eligible_ids"])
        self.assertNotIn("keep-delete", batch["eligible_ids"])
        no_confirm = subprocess.run([str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "cleanup", "--delete-reviewed", str(review)], text=True, capture_output=True)
        self.assertNotEqual(no_confirm.returncode, 0)
        subprocess.run([str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "cleanup", "--delete-reviewed", str(review), "--confirm", "DELETE-ELIGIBLE-UNPINNED"], text=True, capture_output=True, check=True)
        self.assertFalse(candidate.exists())
        self.assertTrue(protected.exists())
        self.assertTrue((self.root / "cleanup.log").exists())

    def test_cleanup_delete_uses_reviewed_path_and_id_identity(self):
        old = time.time() - 120 * 86400
        tasks_dup = self.task(task_id="dup-120000", request="Clean active duplicate")
        archive_dup = self.root / "archive" / "dup-120000"
        archive_dup.mkdir(parents=True)
        data = {"id": "dup-120000", "worker": "pi", "model": "slotstream/qwen", "thinking": "low",
                "created": "2026-09-08T00:00:00Z"}
        (archive_dup / "meta.json").write_text(json.dumps(data))
        (archive_dup / "task.md").write_text("Clean archived duplicate\n")
        for d in (tasks_dup, archive_dup):
            write_record(d, lifecycle_state="completed")
            for p in list(d.rglob("*")) + [d]:
                os.utime(p, (old, old))
        review = self.root / "cleanup-review.json"
        subprocess.run([str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "cleanup", "--dry-run", "--days", "90", "--write-review", str(review), "--json"], text=True, capture_output=True, check=True)
        batch = json.loads(review.read_text())
        reviewed = next(r for r in batch["tasks"] if r["eligible"] and r["where"] == "archive")
        batch["tasks"] = [reviewed]
        batch["eligible_review_keys"] = [reviewed["review_key"]]
        review.write_text(json.dumps(batch))
        subprocess.run([str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "cleanup", "--delete-reviewed", str(review), "--confirm", "DELETE-ELIGIBLE-UNPINNED"], text=True, capture_output=True, check=True)
        self.assertTrue(tasks_dup.exists())
        self.assertFalse(archive_dup.exists())

    def test_parse_record_supports_legacy_without_migration(self):
        d = self.task(task_id="legacy-1", request="Plain legacy request")
        rec = parse_record(d)
        self.assertFalse(rec["canonical"])
        self.assertEqual(rec["request"], "Plain legacy request")
        self.assertEqual(rec["title"], "Plain legacy request")

    def test_request_fence_adapts_to_embedded_markdown_fence(self):
        request = "Explain this fenced sample:\n```\ncode\n```\nEnd."
        d = self.task(task_id="fence-1", request=request)
        write_record(d, lifecycle_state="working")
        text = (d / "task.md").read_text()
        self.assertIn("````text", text)
        self.assertEqual(request_text(d), request)

    def test_status_poll_does_not_rewrite_record_or_reset_retention_age(self):
        old = time.time() - 120 * 86400
        d = self.task(task_id="age-1", request="Clean transient notes")
        write_record(d, lifecycle_state="working", detail="0m01s")
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (old, old))
        before = (d / "task.md").stat().st_mtime
        write_record(d, lifecycle_state="working", detail="0m02s")
        after = (d / "task.md").stat().st_mtime
        self.assertEqual(before, after)
        row = retention_report(self.root, days=90, now=time.time())[0]
        self.assertTrue(row["eligible"])
        self.assertGreaterEqual(row["age_days"], 119)

    def test_ticket_detection_avoids_generic_technical_tokens(self):
        for token in ("UTF-8", "GPT-5", "SHA-256", "ISO-8601", "HTTP-2"):
            self.assertEqual(ticket_in("Fix {} decoding".format(token)), "")
        d = self.task(task_id="generic-token", request="Fix GPT-5 notes and UTF-8 decoding")
        rec = write_record(d, lifecycle_state="completed")
        self.assertEqual(rec["ticket"], "")
        self.assertFalse((d / "retention.json").exists())
        self.assertEqual(ticket_in("Fix PROJ-7 prod alert"), "")
        self.assertEqual(ticket_in("ticket: PROJ-7 Fix prod alert"), "PROJ-7")

    def test_unpin_does_not_repin_and_ordinary_delegated_tier1_ages_out(self):
        old = time.time() - 200 * 86400
        d = self.task(task_id="tier1", request="ticket: KEY-1 Fix AES-256 notes",
                      preflight={"mode": "delegated", "artifact_tier": 1,
                                 "promotion_notice": "Tier 1 task record selected for delegated/non-direct work"})
        write_record(d, lifecycle_state="completed")
        set_pin(d, False, "manual unpin")
        write_record(d, lifecycle_state="completed")
        self.assertFalse(json.loads((d / "retention.json").read_text())["pinned"])
        self.assertIn("ticket: KEY-1", (d / "task.md").read_text().splitlines()[0])
        (d / "request.md").write_text("Fix AES-256 notes\n")
        write_record(d, lifecycle_state="completed")
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (old, old))
        row = retention_report(self.root, days=90, now=time.time())[0]
        self.assertTrue(row["eligible"])
        self.assertNotIn("promoted durable task record", row["protected"])

    def test_ticket_fail_closed_without_configured_prefixes(self):
        from unittest.mock import patch
        with patch("task_record._configured_ticket_prefixes", return_value=set()):
            for token in ("AES-256", "CVE-2024-3094", "PEP-8", "ES-2022", "MP-3", "WCAG-2", "COVID-19", "KEY-1"):
                self.assertEqual(ticket_in("Fix " + token), "")
            self.assertEqual(ticket_in("ticket: KEY-1"), "KEY-1")
        with patch("task_record._configured_ticket_prefixes", return_value={"PROJ"}):
            self.assertEqual(ticket_in("Fix PROJ-7"), "PROJ-7")
            self.assertEqual(ticket_in("Fix AES-256"), "")

    def test_transient_task_local_artifact_does_not_protect_retention(self):
        old = time.time() - 120 * 86400
        d = self.task(task_id="local-artifact", request="Implement tiny fix", contract={"kind": "task", "artifact": str(self.root / "tasks" / "local-artifact" / "artifact.md")})
        (d / "artifact.md").write_text("Status: final\n")
        write_record(d, lifecycle_state="completed")
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (old, old))
        row = retention_report(self.root, days=90, now=time.time())[0]
        self.assertTrue(row["eligible"])
        self.assertNotIn("externally durable contract artifact exists", row["protected"])

    def test_end_session_archives_workers_preserves_pin_ticket_plan_and_logs(self):
        ordinary = self.task(task_id="ordinary-end", request="Ordinary completed task")
        write_record(ordinary, lifecycle_state="completed")
        pinned = self.task(task_id="pin-end", request="Pinned task")
        set_pin(pinned, True, "manual keep")
        write_record(pinned, lifecycle_state="completed")
        ticket = self.task(task_id="ticket-end", request="ticket: PROJ-88 Fix retained ticket")
        write_record(ticket, lifecycle_state="completed")
        plan = self.root / "tasks" / "plan-end"
        plan.mkdir()
        meta = {"id": "plan-end", "worker": "main", "model": "main-agent", "created": "2026-09-08T00:00:00Z", "preflight": {"mode": "plan", "stop": True, "no_edits": True, "artifact_tier": 1, "promotion_notice": "Tier 1 durable stop-mode contract"}}
        (plan / "meta.json").write_text(json.dumps(meta))
        (plan / "request.md").write_text("Plan the API boundary\n")
        write_record(plan, lifecycle_state="working", meta=meta)
        out = end_session_retention(self.root)
        self.assertFalse(ordinary.exists())
        self.assertTrue((self.root / "archive" / "ordinary-end").exists())
        self.assertTrue((self.root / "archive" / "pin-end").exists())
        self.assertTrue((self.root / "archive" / "ticket-end").exists())
        self.assertTrue(plan.exists())
        self.assertTrue((self.root / "cleanup.log").exists())
        log = (self.root / "cleanup.log").read_text()
        self.assertIn("session-end-retention", log)
        self.assertEqual(len(out["archived"]), 3)
        self.assertEqual(len(out["retained"]), 1)
        self.assertEqual(len(find_task(self.root, "PROJ-88", include_archive=True)), 1)
        resume = subprocess.run([str(ROOT / "bin" / "tp-task"), "--state-dir", str(self.root), "resume", "PROJ-88"], text=True, capture_output=True, check=True).stdout
        self.assertIn("PROJ-88 Fix retained ticket", resume)
        self.assertIn("Next Action", resume)

    def test_archive_relocates_reply_only_artifact_and_resume_repairs_old_archive(self):
        d = self.task("reply-only", request="Report cedar color", contract={"kind": "task", "artifact": str(self.root / "tasks/reply-only/result.md")})
        (d / "result.md").write_text("Cedar is green.\n")
        write_record(d, lifecycle_state="completed")
        old = time.time() - 120 * 86400
        for p in list(d.rglob("*")) + [d]:
            os.utime(p, (old, old))
        before = retention_report(self.root, days=90)[0]["age_days"]
        dest = Path(end_session_retention(self.root)["archived"][0]["to"])
        new_path = str(dest / "result.md")
        self.assertEqual(json.loads((dest / "meta.json").read_text())["contract"]["artifact"], new_path)
        text = (dest / "task.md").read_text()
        self.assertIn("Result file: `" + new_path + "`", text)
        self.assertIn("Contract artifact: `" + new_path + "` (present)", text)
        self.assertIn("Archived artifact origin: `" + str(d / "result.md") + "`", text)
        self.assertEqual(retention_report(self.root, days=90)[0]["age_days"], before)
        # Simulate an old /end archive: canonical text and metadata both stale.
        meta = json.loads((dest / "meta.json").read_text())
        meta["contract"]["artifact"] = str(d / "result.md")
        meta.pop("archive_relocation")
        (dest / "meta.json").write_text(json.dumps(meta))
        (dest / "task.md").write_text(text.replace(new_path, str(d / "result.md")).replace(
            "- Archived artifact origin: `" + str(d / "result.md") + "`\n", ""))
        cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "resume", "reply-only"]
        resume = subprocess.run(cmd, text=True, capture_output=True, check=True).stdout
        self.assertIn("Contract artifact: `" + new_path + "` (present)", resume)
        self.assertIn("Archived artifact origin: `" + str(d / "result.md") + "`", resume)
        self.assertNotIn("Result file: `" + str(d / "result.md") + "`", resume)
        stamp = (dest / "task.md").stat().st_mtime_ns
        subprocess.run(cmd, text=True, capture_output=True, check=True)
        self.assertEqual((dest / "task.md").stat().st_mtime_ns, stamp)

    def test_archive_collision_and_external_artifact(self):
        external = self.root / "research" / "report.md"
        external.parent.mkdir()
        external.write_text("Status: final\n")
        d = self.task("collision", request="Research", contract={"kind": "research", "artifact": str(external)})
        (d / "result.md").write_text("Done\n")
        write_record(d, lifecycle_state="completed")
        occupied = self.root / "archive" / "collision"
        occupied.mkdir(parents=True)
        (occupied / "proof").write_text("untouched")
        dest = Path(end_session_retention(self.root)["archived"][0]["to"])
        self.assertNotEqual(dest, occupied)
        self.assertEqual((occupied / "proof").read_text(), "untouched")
        self.assertEqual(json.loads((dest / "meta.json").read_text())["contract"]["artifact"], str(external))
        self.assertIn("Contract artifact: `" + str(external) + "` (final)", (dest / "task.md").read_text())
        self.assertIn(str(dest / "result.md"), (dest / "task.md").read_text())
        self.assertNotIn(str(d / "result.md"), (dest / "task.md").read_text())

    def test_tp_clean_collision_reconciles_local_artifact(self):
        fake = self.root / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('#!/bin/sh\ncase "$1" in\n has-session) exit 0;;\n show-options) echo "$TP_STATE_DIR";;\n list-panes) echo "%1 0";;\nesac\n')
        tmux.chmod(0o755)
        env = {**os.environ, "PATH": str(fake) + os.pathsep + os.environ["PATH"], "TP_STATE_DIR": str(self.root), "TP_SESSION": "fixture"}
        env.pop("TMUX", None)
        d = self.task("gone", request="Reply only", pane="%2", contract={"kind": "task", "artifact": str(self.root / "tasks/gone/result.md")})
        (d / "result.md").write_text("Reply\n")
        write_record(d, lifecycle_state="completed")
        occupied = self.root / "archive/gone"
        occupied.mkdir(parents=True)
        (occupied / "proof").write_text("original")
        result = subprocess.run([str(ROOT / "bin/tp-clean")], env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "archived 1")
        self.assertEqual((occupied / "proof").read_text(), "original")
        dest = next(p for p in (self.root / "archive").iterdir() if p != occupied)
        self.assertEqual(json.loads((dest / "meta.json").read_text())["contract"]["artifact"], str(dest / "result.md"))
        self.assertIn("Result file: `" + str(dest / "result.md") + "`", (dest / "task.md").read_text())

    def test_main_contract_preserves_handoff_request_and_cleanup_reports_it(self):
        request = "+plan Original request details"
        handoff = self.root / "handoffs" / "hand-1"
        handoff.mkdir(parents=True)
        (handoff / "scaffold.json").write_text(json.dumps({"original_request": request,
            "effective_request": "Original request details", "created": "2026-01-01T00:00:00Z", "mode": "plan"}))
        sha = hashlib.sha256(request.encode()).hexdigest()
        contract = {"handshake_id": "hand-1", "request_sha256": sha, "action": "stop", "mode": "plan",
                    "task_summary": "Plan the request", "frame": "No edits", "no_edits": True, "scope": "brief",
                    "assumptions": [], "risk_notes": [], "proof_path": ["write plan"], "done_when": ["plan is in chat"]}
        pf = json.loads(subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.root),
                                        "preflight", "--json", "--mode", "plan", request],
                                       text=True, capture_output=True, check=True).stdout)["preflight"]
        subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "main-contract", "--json",
                        "--handshake-id", "hand-1", "--request-sha256", sha,
                        "--preflight-json", json.dumps(pf)],
                       input=json.dumps(contract), text=True, capture_output=True, check=True)
        scaffold = json.loads((handoff / "scaffold.json").read_text())
        self.assertEqual(scaffold["original_request"], request)
        self.assertEqual(scaffold["effective_request"], "Original request details")
        ended = json.loads(subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.root),
                                           "end-session", "--json"], text=True, capture_output=True,
                                          check=True).stdout)
        self.assertEqual(ended["handoffs"][0]["id"], "hand-1")
        self.assertIn('"handoffs"', (self.root / "cleanup.log").read_text())
        batch = json.loads(subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.root),
                                          "cleanup", "--dry-run", "--json"], text=True,
                                         capture_output=True, check=True).stdout)
        self.assertEqual(batch["handoffs"][0]["id"], "hand-1")
        self.assertFalse(batch["handoffs"][0]["eligible"])
        self.assertTrue(handoff.exists())

    def test_review_resolve_requires_human_confirmation_and_records_audit(self):
        handoff = self.root / "handoffs" / "hand-1"
        handoff.mkdir(parents=True)
        blocker = {"required": True, "status": "needs_human", "handshake_id": "hand-1",
                   "reasons": ["sensitive/PII/security trigger"]}
        path = handoff / "review-required.json"
        path.write_text(json.dumps(blocker))
        direct = self.root / "tasks" / "direct-review"
        direct.mkdir()
        (direct / "meta.json").write_text(json.dumps({"handshake_id": "hand-1", "worker": "main"}))
        (direct / "review-required.json").write_text(json.dumps(blocker))
        base = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "review-resolve", "hand-1",
                "--outcome", "reviewed", "--reason", "human verified report", "--review-reference", "reports/independent.md"]
        def run(args, env=None):
            clean_env = dict(os.environ)
            for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
                clean_env.pop(key, None)
            if env:
                clean_env.update(env)
            return subprocess.run(args, text=True, capture_output=True, env=clean_env)
        self.assertNotEqual(run(base).returncode, 0)
        self.assertNotEqual(run(base + ["--confirm", "WRONG"]).returncode, 0)
        self.assertNotEqual(run(base + ["--confirm", "RESOLVE-REVIEW-REQUIRED"],
                                env=dict(os.environ, TP_TASK_DIR="/tmp/worker")).returncode, 0)
        self.assertNotEqual(run([x for x in base if x != "reports/independent.md"] +
                                ["--confirm", "RESOLVE-REVIEW-REQUIRED"]).returncode, 0)
        self.assertEqual(json.loads(path.read_text()), blocker)
        result = run(base + ["--confirm", "RESOLVE-REVIEW-REQUIRED", "--json"])
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved = json.loads(path.read_text())
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["reasons"], blocker["reasons"])
        self.assertEqual(resolved["resolution"]["outcome"], "reviewed")
        self.assertEqual(resolved["resolution"]["review_reference"], "reports/independent.md")
        self.assertEqual(resolved["resolution"]["actor"], "human_cli")
        self.assertEqual(json.loads((direct / "review-required.json").read_text())["resolution"], resolved["resolution"])
        audit = json.loads((self.root / "review-resolutions.log").read_text().strip())
        self.assertEqual(audit["resolution"], resolved["resolution"])
        self.assertNotEqual(run(base + ["--confirm", "RESOLVE-REVIEW-REQUIRED"]).returncode, 0)
        self.assertEqual(len((self.root / "review-resolutions.log").read_text().splitlines()), 1)

    def test_review_resolve_approval_waiver_and_invalid_handoff(self):
        base = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "review-resolve", "hand-2",
                "--outcome", "approved", "--reason", "Human accepts risk", "--confirm", "RESOLVE-REVIEW-REQUIRED"]
        self.assertNotEqual(subprocess.run(base, capture_output=True).returncode, 0)
        handoff = self.root / "handoffs" / "hand-2"
        handoff.mkdir(parents=True)
        path = handoff / "review-required.json"
        path.write_text(json.dumps({"status": "needs_human", "required": True, "handshake_id": "wrong"}))
        self.assertNotEqual(subprocess.run(base, capture_output=True).returncode, 0)
        path.write_text(json.dumps({"status": "needs_human", "required": True, "handshake_id": "hand-2"}))
        clean_env = dict(os.environ)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            clean_env.pop(key, None)
        result = subprocess.run(base, text=True, capture_output=True, env=clean_env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text())["resolution"]["outcome"], "approved")

    def test_review_list_task_handoff_and_references_and_task_resolution(self):
        handoff = self.root / "handoffs" / "hand-blocked"
        handoff.mkdir(parents=True)
        (handoff / "review-required.json").write_text(json.dumps({
            "handshake_id": "hand-blocked", "required": True, "status": "needs_human", "reasons": ["sensitive work"]}))
        inherited = self.root / "handoffs" / "hand-inherited"
        inherited.mkdir()
        (inherited / "scaffold.json").write_text(json.dumps({"blocked_by": ["hand-blocked"]}))
        task = self.task("worker-review", request="Review work")
        (task / "review-required.json").write_text(json.dumps({
            "task_id": "worker-review", "required": True, "status": "needs_human",
            "reasons": ["reviewer unavailable"], "review_reference": "review-worker"}))
        cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root)]
        env = dict(os.environ)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        inventory = json.loads(subprocess.run(cmd + ["review-list", "--json"], env=env,
                                              text=True, capture_output=True, check=True).stdout)
        self.assertEqual({(b["kind"], b["id"]) for b in inventory["blockers"]},
                         {("task", "worker-review"), ("handoff", "hand-blocked")})
        self.assertEqual(inventory["references"][0]["blocked_by"], ["hand-blocked"])
        self.assertIn("review-worker", subprocess.run(cmd + ["review-list"], env=env,
                                                   text=True, capture_output=True, check=True).stdout)
        base = cmd + ["review-resolve", "--task-id", "worker-review", "--outcome", "reviewed",
                      "--reason", "Human inspected report", "--review-reference", "reports/review.md"]
        for argv, override in ((base, {}), (base + ["--confirm", "WRONG"], {}),
                               (base + ["--confirm", "RESOLVE-REVIEW-REQUIRED"], {"TP_TASK_DIR": str(task)}),
                               (base + ["--confirm", "RESOLVE-REVIEW-REQUIRED", "hand-blocked"], {})):
            self.assertNotEqual(subprocess.run(argv, env=dict(env, **override), capture_output=True).returncode, 0)
        self.assertEqual(json.loads((task / "review-required.json").read_text())["status"], "needs_human")
        resolved = subprocess.run(base + ["--confirm", "RESOLVE-REVIEW-REQUIRED"], env=env,
                                  text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads((task / "review-required.json").read_text())["status"], "resolved")
        self.assertEqual(json.loads((self.root / "review-resolutions.log").read_text())["task_id"], "worker-review")
        self.assertNotEqual(subprocess.run(base + ["--confirm", "RESOLVE-REVIEW-REQUIRED"], env=env,
                                          capture_output=True).returncode, 0)
        self.assertEqual(len(json.loads(subprocess.run(cmd + ["review-list", "--json"], env=env,
                                                  text=True, capture_output=True, check=True).stdout)["blockers"]), 1)

    def test_task_review_resolution_is_explicit_and_ambiguous_archives_are_rejected(self):
        task = self.task("shared-id", request="Fix worker output")
        marker = {"task_id": "shared-id", "status": "needs_human", "required": True}
        (task / "review-required.json").write_text(json.dumps(marker))
        handoff = self.root / "handoffs" / "shared-id"
        handoff.mkdir(parents=True)
        (handoff / "review-required.json").write_text(json.dumps({
            "handshake_id": "shared-id", "status": "needs_human", "required": True}))
        env = dict(os.environ)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "review-resolve", "--task-id", "shared-id",
               "--outcome", "approved", "--reason", "Human accepted risk", "--confirm", "RESOLVE-REVIEW-REQUIRED"]
        archive = self.root / "archive" / "shared-id"
        archive.mkdir(parents=True)
        (archive / "meta.json").write_text((task / "meta.json").read_text())
        (archive / "review-required.json").write_text(json.dumps(marker))
        self.assertNotEqual(subprocess.run(cmd, env=env, capture_output=True).returncode, 0)
        self.assertEqual(json.loads((task / "review-required.json").read_text()), marker)
        (archive / "review-required.json").unlink()
        (archive / "meta.json").unlink()
        archive.rmdir()
        self.assertEqual(subprocess.run(cmd, env=env, capture_output=True).returncode, 0)
        self.assertEqual(json.loads((handoff / "review-required.json").read_text())["status"], "needs_human")

    def test_archive_collision_uses_metadata_identity_and_never_deletes_blocker(self):
        # An older copy with the same task id already owns the archive name.
        existing = self.root / "archive" / "collision"
        existing.mkdir(parents=True)
        (existing / "meta.json").write_text(json.dumps({"id": "collision", "worker": "pi"}))
        (existing / "task.md").write_text("Other work\n")
        task = self.task("collision", request="Review this worker")
        marker = {"required": True, "task_id": "collision", "status": "needs_human", "reasons": ["review failed"]}
        (task / "review-required.json").write_text(json.dumps(marker))
        write_record(task, lifecycle_state="completed")
        archived = end_session_retention(self.root)["archived"][0]
        renamed = Path(archived["to"])
        self.assertNotEqual(renamed.name, "collision")
        cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root)]
        env = dict(os.environ)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        listing = json.loads(subprocess.run(cmd + ["review-list", "--json"], env=env,
                                            text=True, capture_output=True, check=True).stdout)
        self.assertEqual([(b["id"], b["path"]) for b in listing["blockers"]],
                         [("collision", str(renamed / "review-required.json"))])
        old = time.time() - 120 * 86400
        for p in list(renamed.rglob("*")) + [renamed]:
            os.utime(p, (old, old))
        for p in list(existing.rglob("*")) + [existing]:
            os.utime(p, (old, old))
        batch = json.loads(subprocess.run(cmd + ["cleanup", "--dry-run", "--json"], env=env,
                                          text=True, capture_output=True, check=True).stdout)
        row = next(r for r in batch["tasks"] if r["path"] == str(renamed))
        self.assertFalse(row["eligible"])
        self.assertIn("pending human review", row["protected"])
        batch_path = self.root / "batch.json"
        batch_path.write_text(json.dumps(batch))
        resolved = subprocess.run(cmd + ["review-resolve", "--task-id", "collision", "--outcome", "approved",
                                         "--reason", "Human accepted risk", "--confirm", "RESOLVE-REVIEW-REQUIRED"],
                                  env=env, text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(json.loads((renamed / "review-required.json").read_text())["resolution"]["actor"], "human_cli")
        self.assertFalse(json.loads(subprocess.run(cmd + ["review-list", "--json"], env=env,
                                                text=True, capture_output=True, check=True).stdout)["blockers"])
        outcome = json.loads(subprocess.run(cmd + ["cleanup", "--delete-reviewed", str(batch_path),
                                                 "--confirm", "DELETE-ELIGIBLE-UNPINNED", "--json"],
                                            env=env, text=True, capture_output=True, check=True).stdout)
        self.assertNotIn(str(renamed), [r["path"] for r in outcome["deleted"]])
        self.assertTrue(renamed.exists())

    def test_pending_review_and_capability_never_eligible_even_with_reviewed_batch(self):
        old = time.time() - 120 * 86400
        review = self.task("review-evidence", request="Evidence")
        cap = self.task("cap-evidence", request="Capability evidence")
        direct = self.task("direct-evidence", request="Direct evidence", handshake_id="handoff-evidence")
        handoff = self.root / "handoffs" / "handoff-evidence"
        handoff.mkdir(parents=True)
        (handoff / "review-required.json").write_text(json.dumps({
            "handshake_id": "handoff-evidence", "required": True, "status": "needs_human"}))
        (review / "review-required.json").write_text(json.dumps({
            "task_id": "review-evidence", "required": True, "status": "needs_human"}))
        (cap / "pending-capability.json").write_text("{}")
        for d in (review, cap, direct):
            write_record(d, lifecycle_state="completed")
            for p in list(d.rglob("*")) + [d]:
                os.utime(p, (old, old))
        rows = {r["id"]: r for r in retention_report(self.root, now=time.time())}
        self.assertTrue(all(not r["eligible"] for r in rows.values()))
        self.assertIn("pending human review", rows["review-evidence"]["protected"])
        self.assertIn("pending human capability resolution", rows["cap-evidence"]["protected"])
        self.assertIn("pending handoff review", rows["direct-evidence"]["protected"])
        # An old reviewed batch cannot delete a newly blocked record.
        (review / "review-required.json").unlink()
        batch_cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "cleanup"]
        batch = json.loads(subprocess.run(batch_cmd + ["--dry-run", "--json"],
                                          text=True, capture_output=True, check=True).stdout)
        (review / "review-required.json").write_text("{invalid")  # malformed is still blocking
        batch_path = self.root / "review-batch.json"
        batch_path.write_text(json.dumps(batch))
        outcome = json.loads(subprocess.run(batch_cmd + ["--delete-reviewed", str(batch_path), "--confirm",
                                                     "DELETE-ELIGIBLE-UNPINNED", "--json"],
                                            text=True, capture_output=True, check=True).stdout)
        self.assertFalse(outcome["deleted"])
        self.assertTrue(review.exists())

    def test_direct_main_capture_output_closes_record(self):
        d = self.root / "tasks" / "direct-1"
        d.mkdir()
        meta = {"id": "direct-1", "worker": "main", "model": "main-agent", "created": "2026-09-08T00:00:00Z",
                "preflight": {"mode": "delegated", "stop": False, "artifact_tier": 1}}
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "request.md").write_text("Answer directly within the durable frame\n")
        write_record(d, lifecycle_state="working", meta=meta)
        result = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(self.root), "capture-output", "direct-1"],
                                input="Final direct answer\n", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(parse_record(d)["state"], "completed")
        self.assertTrue((d / "completed").exists())
        self.assertIn("Final direct answer", (d / "result.md").read_text())
        self.assertNotIn("Captured main-owned stop-mode output", (d / "task.md").read_text())

    def test_stop_mode_output_capture_completes_and_resume_shows_output(self):
        d = self.root / "tasks" / "prototype-1"
        d.mkdir()
        meta = {"id": "prototype-1", "worker": "main", "model": "main-agent", "created": "2026-09-08T00:00:00Z", "preflight": {"mode": "prototype", "stop": True, "no_edits": True, "artifact_tier": 1, "promotion_notice": "Tier 1 durable stop-mode contract"}}
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "request.md").write_text("Sketch a throwaway parser prototype\n")
        write_record(d, lifecycle_state="working", meta=meta)
        capture_main_output(d, "Prototype sketch:\n- Parse tokens first.\n- No files edited.")
        rec = parse_record(d)
        self.assertEqual(rec["state"], "completed")
        text = (d / "task.md").read_text()
        self.assertIn("Captured main-owned stop-mode output", text)
        self.assertIn("Prototype sketch:", text)
        self.assertIn("No-Edit Enforcement Limitation", text)
        body = format_record_for_resume(d)
        self.assertIn("Sketch a throwaway parser prototype", body)
        self.assertIn("Lifecycle State:** `completed`", body)
        self.assertIn("Resume output from result.md", body)
        self.assertIn("Parse tokens first", body)
        self.assertIn("Verify/close out", body)


if __name__ == "__main__":
    unittest.main()
