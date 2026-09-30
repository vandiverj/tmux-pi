"""Durable review recovery and follow-up regressions (fake tmux, no MCP)."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from task_record import approved_delegated_review, unresolved_review  # noqa: E402


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        (self.state / "tasks").mkdir()
        (self.state / "archive").mkdir()
        self.cmd = [str(ROOT / "bin/tp-task"), "--state-dir", str(self.state)]
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state)}
        self.env.pop("TP_TASK_DIR", None)

    def cli(self, *args, env=None):
        return subprocess.run(self.cmd + list(args), text=True, capture_output=True, env=env or self.env)

    def marker(self, directory, status, **extra):
        directory.mkdir(parents=True, exist_ok=True)
        identity = "w1"
        (directory / "meta.json").write_text(json.dumps({"id": identity, "worker": "pi", "decision": {"review": "required"}}))
        path = directory / "review-required.json"
        path.write_text(json.dumps({"required": True, "task_id": identity, "status": status, **extra}))
        return path

    def test_restart_kill_and_archived_pending_review_resolve_only_explicitly(self):
        for parent in ("tasks", "archive"):
            with self.subTest(parent=parent):
                path = self.marker(self.state / parent / ("w1" if parent == "tasks" else "w1-ended-1"), "pending_review",
                                   **({"review_target": "missing-replacement"} if parent == "archive" else {}))
                # A killed or archived worker has no watcher to finish the review.
                (path.parent / "exit").write_text("143\n")
                listing = self.cli("review-list", "--json")
                self.assertEqual(listing.returncode, 0, listing.stderr)
                self.assertIn("pending_review", listing.stdout)
                base = ("review-resolve", "--task-id", "w1", "--outcome", "approved", "--reason", "Human accepts risk")
                self.assertNotEqual(self.cli(*base).returncode, 0)
                denied = self.cli(*base, "--confirm", "RESOLVE-REVIEW-REQUIRED", env={**self.env, "TP_TASK_DIR": "/worker"})
                self.assertNotEqual(denied.returncode, 0)
                resolved = self.cli(*base, "--confirm", "RESOLVE-REVIEW-REQUIRED")
                self.assertEqual(resolved.returncode, 0, resolved.stderr)
                self.assertEqual(json.loads(path.read_text())["resolution"]["actor"], "human_cli")
                self.assertFalse(unresolved_review(path, "w1"))
                self.assertEqual(json.loads(self.cli("review-list", "--json").stdout)["blockers"], [])
                path.parent.joinpath("exit").unlink()
                for file in path.parent.iterdir():
                    file.unlink()
                path.parent.rmdir()
        audit = [json.loads(line) for line in (self.state / "review-resolutions.log").read_text().splitlines()]
        self.assertEqual(len(audit), 2)
        self.assertEqual([entry["resolution"]["outcome"] for entry in audit], ["approved", "approved"])

    def test_invalid_approved_can_be_resolved_but_valid_approval_cannot(self):
        path = self.marker(self.state / "archive" / "w1-ended-1", "approved", approval={"reviewer_id": "lost"})
        self.assertTrue(unresolved_review(path, "w1"))
        result = self.cli("review-resolve", "--task-id", "w1", "--outcome", "reviewed", "--review-reference", "report.md",
                          "--reason", "Checked report", "--confirm", "RESOLVE-REVIEW-REQUIRED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(unresolved_review(path, "w1"))
        # A valid archived approval is already resolved and must not be overridden by the waiver CLI.
        author = path.parent
        reviewer = author.parent / "r1"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "r1", "worker": "claude"}))
        (reviewer / "review-of").write_text("w1")
        (reviewer / "result.md").write_text("\ufeffAPPROVED: checked.\n")
        meta = json.loads((author / "meta.json").read_text())
        meta["review"] = "r1"
        (author / "meta.json").write_text(json.dumps(meta))
        (author / "result.md").write_text("Reviewed result")
        path.write_text(json.dumps({"required": True, "task_id": "w1", "status": "approved", "round_nonce": "test-w1-round", "approval": {
            "reviewer_id": "r1", "round_nonce": "test-w1-round", "evidence_sha256": hashlib.sha256(b"APPROVED: checked.").hexdigest(),
            "author_sha256": hashlib.sha256(b"Reviewed result").hexdigest()}}))
        self.assertTrue(approved_delegated_review(author, meta, json.loads(path.read_text())))
        self.assertFalse(unresolved_review(path, "w1"))
        self.assertNotEqual(self.cli("review-resolve", "--task-id", "w1", "--outcome", "approved",
                                    "--reason", "waive", "--confirm", "RESOLVE-REVIEW-REQUIRED").returncode, 0)
        # A legacy hashless approval cannot authorize a newer author result.
        old = json.loads(path.read_text())
        del old["approval"]["author_sha256"]
        path.write_text(json.dumps(old))
        (author / "result.md").write_text("Unreviewed later edit")
        self.assertTrue(unresolved_review(path, "w1"))
        self.assertEqual(self.cli("review-resolve", "--task-id", "w1", "--outcome", "approved",
                                  "--reason", "Legacy approval invalid", "--confirm", "RESOLVE-REVIEW-REQUIRED").returncode, 0)

    def test_duplicate_id_resolves_only_the_unresolved_copy(self):
        archived = self.marker(self.state / "archive" / "w1", "resolved", round_nonce="old-round", resolution={"actor": "human_cli", "round_nonce": "old-round", "author_sha256": None})
        active = self.marker(self.state / "tasks" / "w1", "pending_review")
        args = ("review-resolve", "--task-id", "w1", "--outcome", "approved", "--reason", "Checked live task",
                "--confirm", "RESOLVE-REVIEW-REQUIRED")
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(json.loads(active.read_text())["status"], "resolved")
        self.assertEqual(json.loads(archived.read_text())["resolution"], {"actor": "human_cli", "round_nonce": "old-round", "author_sha256": None})
        self.assertEqual(len((self.state / "review-resolutions.log").read_text().splitlines()), 1)
        active.write_text(json.dumps({"required": True, "task_id": "w1", "status": "pending_review"}))
        archived.write_text(json.dumps({"required": True, "task_id": "w1", "status": "needs_human"}))
        self.assertNotEqual(self.cli(*args).returncode, 0)  # Two unresolved identities are ambiguous.
        self.assertEqual(json.loads(active.read_text())["status"], "pending_review")
        self.assertEqual(json.loads(archived.read_text())["status"], "needs_human")

    def test_inflight_waiver_rework_and_error_fail_closed(self):
        for name in ("resolved-inflight", "resolved-inflight-error", "formatted-verdict"):
            with self.subTest(name=name):
                run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), name],
                                     text=True, capture_output=True, cwd=ROOT, timeout=18)
                self.assertEqual(run.returncode, 0, run.stderr)
                data = json.loads(run.stdout)
                if name == "formatted-verdict":
                    self.assertEqual(data["taskReview"], "needs_human")
                    self.assertIn("unparseable", data["reviewReasons"][-1])
                else:
                    self.assertTrue(data["blocked"])
                    self.assertEqual(data["collected"], 0)
                    if name == "resolved-inflight":
                        self.assertEqual(data["before"]["status"], "resolved")
                        self.assertEqual(data["marker"]["status"], "resolved")
                        self.assertTrue(data["collectDenied"])
                    else:
                        self.assertEqual(data["marker"]["status"], "needs_human")  # require_skip_resolved mutant

    def test_halted_dialog_branches_keep_review_gate(self):
        for choice in ("continue", "discard", "narrow", "escalate"):
            with self.subTest(choice=choice):
                run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"),
                                      "halted-dialog-" + choice], text=True, capture_output=True, cwd=ROOT, timeout=18)
                self.assertEqual(run.returncode, 0, run.stderr)
                data = json.loads(run.stdout)
                if choice == "continue":
                    self.assertEqual(data["markerAtContinue"]["status"], "pending_review")
                    self.assertNotEqual(data["markerAtContinue"]["round_nonce"], "halted-round")
                else:
                    self.assertTrue(data["sourceBlocked"])
                    self.assertEqual(data["sourceMarker"]["status"], "pending_review")
                    if choice != "discard":
                        self.assertTrue(data["newMarker"]["required"])
                        self.assertTrue(data["newMarker"]["round_nonce"])

    def test_python_and_dispatcher_agree_on_changed_authority(self):
        for name in ("approved-changed", "resolved-changed"):
            with self.subTest(name=name):
                run = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), name],
                                     text=True, capture_output=True, cwd=ROOT, timeout=18)
                self.assertEqual(run.returncode, 0, run.stderr)
                self.assertEqual(json.loads(run.stdout), {"before": "none", "after": "required"})

    def test_resolution_round_hash_null_and_watched_exemption(self):
        path = self.marker(self.state / "tasks" / "w1", "pending_review", round_nonce="round-one")
        args = ("review-resolve", "--task-id", "w1", "--outcome", "approved", "--reason", "Checked",
                "--confirm", "RESOLVE-REVIEW-REQUIRED")
        watched = json.loads(self.cli("review-list", "--json", "--watching", "w1").stdout)
        self.assertTrue(watched["blockers"][0]["watched"])
        self.assertIn("watched", self.cli("review-list", "--watching", "w1").stdout)
        self.assertEqual(self.cli(*args).returncode, 0)
        resolved = json.loads(path.read_text())
        self.assertIsNone(resolved["resolution"]["author_sha256"])
        self.assertEqual(resolved["round_nonce"], resolved["resolution"]["round_nonce"])
        self.assertFalse(unresolved_review(path, "w1"))  # absent output matches null
        (path.parent / "result.md").write_bytes(b"")
        self.assertTrue(unresolved_review(path, "w1"))  # empty output is not absent output
        (path.parent / "result.md").unlink()
        for changed in (lambda m: m["resolution"].pop("author_sha256"),
                        lambda m: m["resolution"].update(round_nonce="other"),
                        lambda m: m.update(round_nonce="other")):
            mutant = json.loads(path.read_text())
            changed(mutant)
            path.write_text(json.dumps(mutant))
            self.assertTrue(unresolved_review(path, "w1"))  # resolution_no_hash / nonce mutants
            path.write_text(json.dumps(resolved))
        (path.parent / "result.md").symlink_to(self.state / "missing")
        self.assertTrue(unresolved_review(path, "w1"))
        (path.parent / "result.md").unlink()
        path.write_text(json.dumps({"required": True, "task_id": "w1", "status": "pending_review", "round_nonce": "round-two"}))
        (path.parent / "result.md").write_bytes(b"Reviewed output")
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(json.loads(path.read_text())["resolution"]["author_sha256"], hashlib.sha256(b"Reviewed output").hexdigest())
        self.assertFalse(unresolved_review(path, "w1"))
        (path.parent / "result.md").write_bytes(b"Later edit")
        self.assertTrue(unresolved_review(path, "w1"))
        (path.parent / "result.md").write_bytes(b"Reviewed output")
        (path.parent / "meta.json").write_text("{}")
        self.assertTrue(json.loads(self.cli("review-list", "--json").stdout)["blockers"])  # missing identity cannot validate R

    def test_clean_preserves_identity_located_approval_and_escalation(self):
        fake = self.state / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('#!/bin/sh\ncase "$1" in\n has-session) exit 0;;\n show-options) echo "$TP_STATE_DIR";;\n list-panes) echo "%1 0";;\n esac\n')
        tmux.chmod(0o755)
        env = {**self.env, "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        env.pop("TMUX", None)
        author = self.state / "tasks" / "w1"
        marker = self.marker(author, "pending_review", round_nonce="round-1")
        reviewer = self.state / "tasks" / "r1"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "r1", "worker": "claude", "pane": "%2"}))
        (reviewer / "review-of").write_text("w1")
        verdict = "APPROVED: checked."
        (reviewer / "result.md").write_text(verdict)
        (author / "result.md").write_text("Done.")
        meta = json.loads((author / "meta.json").read_text())
        meta.update(review="r1", pane="%1")
        (author / "meta.json").write_text(json.dumps(meta))
        marker.write_text(json.dumps({"required": True, "task_id": "w1", "status": "approved", "round_nonce": "round-1", "approval": {
            "reviewer_id": "r1", "round_nonce": "round-1", "evidence_sha256": hashlib.sha256(verdict.encode()).hexdigest(),
            "author_sha256": hashlib.sha256(b"Done.").hexdigest()}}))
        def clear():
            return not json.loads(self.cli("review-list", "--json").stdout)["blockers"]
        self.assertTrue(clear())
        cleaned = subprocess.run([str(ROOT / "bin/tp-clean")], env=env, text=True, capture_output=True)
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertTrue(clear())  # B: gone reviewer, live author
        self.assertTrue(approved_delegated_review(author, meta, json.loads(marker.read_text())))
        # Archive collision rename also cannot invalidate the identity.
        (self.state / "archive" / "r1").rename(self.state / "archive" / "r1-ended-1")
        self.assertTrue(clear())
        source = self.state / "tasks" / "source"
        source.mkdir()
        (source / "meta.json").write_text(json.dumps({"id": "source", "pane": "%3"}))
        (source / "review-required.json").write_text(json.dumps({"required": True, "task_id": "source", "status": "pending_review", "review_target": "w1"}))
        meta["escalated_from"] = {"id": "source"}
        (author / "meta.json").write_text(json.dumps(meta))
        self.assertTrue(clear())
        cleaned = subprocess.run([str(ROOT / "bin/tp-clean")], env=env, text=True, capture_output=True)
        self.assertEqual(cleaned.returncode, 0, cleaned.stderr)
        self.assertTrue(clear())  # B2: archived source, live approved target
        duplicate = self.state / "archive" / "duplicate-r1"
        duplicate.mkdir()
        (duplicate / "meta.json").write_text(json.dumps({"id": "r1"}))
        self.assertFalse(clear())  # ambiguous evidence fails closed
        (duplicate / "meta.json").unlink()
        (duplicate / "meta.json").symlink_to(self.state / "archive" / "r1-ended-1" / "meta.json")
        self.assertTrue(clear())  # symlink never creates an identity match

    def test_read_tools_stay_available_without_edit_authority(self):
        result = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), "read-tools"],
                                text=True, capture_output=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        tools = json.loads(result.stdout)
        for name in ("read", "grep", "find", "ls", "list_subagents", "collect_result"):
            self.assertTrue(tools[name], name)
        for name in ("powershell", "mutating_extension"):
            self.assertFalse(tools[name], name)

    def test_round_snapshot_and_resolved_rework_runtime(self):
        for name in ("midreview-followup", "midreview-pane", "midreview-nonce", "midreview-cap", "resolved-restart", "resolved-failed", "resolved-changes", "needs-human-followup"):
            with self.subTest(name=name):
                result = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), name],
                                        text=True, capture_output=True, cwd=ROOT, timeout=18)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                if name.startswith("midreview"):
                    self.assertEqual(data["rounds"], 2)
                    self.assertEqual(data["reviewSnapshots"], ["Round 1 result.",
                        "Round 1 result." if name == "midreview-nonce" else "Round 2 UNREVIEWED result."])
                    if name == "midreview-cap":
                        self.assertEqual(data["final"]["status"], "needs_human")
                        self.assertEqual(data["collected"], 0)
                        self.assertNotIn("result", data["messages"])
                    else:
                        self.assertEqual(data["final"]["approval"]["reviewer_id"], "review-2")
                        self.assertEqual(data["final"]["approval"]["round_nonce"], data["final"]["round_nonce"])
                        self.assertNotEqual(data["firstNonce"], data["final"]["round_nonce"])
                        self.assertEqual(data["collected"], 1)
                        self.assertEqual(data["messages"].count("result"), 1)
                elif name == "needs-human-followup":
                    self.assertEqual(data["pending"]["status"], "needs_human")
                    self.assertEqual(data["final"]["status"], "needs_human")
                    self.assertTrue(data["denied"])
                else:
                    self.assertEqual(data["pending"]["status"], "pending_review")
                    self.assertEqual(data["pending"]["previous_resolution"]["actor"], "human_cli")
                    self.assertFalse(data["staleCompleted"])
                    self.assertTrue(data["denied"])
                    if name == "resolved-restart":
                        self.assertTrue(data["restartBlocked"])
                    else:
                        self.assertEqual(data["final"]["status"], "needs_human")
                        self.assertEqual(data["rounds"], 1 if name == "resolved-failed" else 2)
                        if name == "resolved-changes":
                            self.assertIn("final round", data["final"]["reasons"][-1])
                        self.assertNotIn("result", data["messages"])

    def test_spawn_avoids_archived_identity_and_seeds_nonce(self):
        state = self.state
        fake = state / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('#!/bin/sh\ncase "$1" in\n show-options) echo "$TP_STATE_DIR";;\n new-window) echo "%%42";;\n esac\n')
        tmux.chmod(0o755)
        from datetime import datetime
        base = "unique-" + datetime.now().strftime("%H%M%S")
        archived = state / "archive" / (base + "-ended-1")
        archived.mkdir()
        (archived / "meta.json").write_text(json.dumps({"id": base}))
        env = {**self.env, "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        env.pop("TMUX", None)
        spawned = subprocess.run([str(ROOT / "bin/tp-spawn"), "--workdir", str(state.parent), "--name", "unique", "--decision-json",
                                  '{"review":"required"}', "New work"], env=env, text=True, capture_output=True)
        self.assertEqual(spawned.returncode, 0, spawned.stderr)
        identity = spawned.stdout.strip()
        self.assertNotEqual(identity, base)
        self.assertEqual(json.loads((state / "tasks" / identity / "review-required.json").read_text())["task_id"], identity)
        self.assertTrue(json.loads((state / "tasks" / identity / "review-required.json").read_text())["round_nonce"])

    def test_runtime_rework_and_halted_after_reload(self):
        for name in ("rework-restart", "rework-approved", "halted-restart", "halted-marker-restart"):
            with self.subTest(name=name):
                result = subprocess.run(["node", "--experimental-strip-types", str(ROOT / "tests/dispatcher_runtime.mjs"), name],
                                        text=True, capture_output=True, cwd=ROOT)
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)
                if name in ("halted-restart", "halted-marker-restart"):
                    self.assertEqual(data["decision"]["review"], "required")
                    self.assertEqual(data["inheritedWhileWatched"], "none")
                    self.assertEqual(data["target"]["status"], "pending_review")
                    self.assertEqual(data["target"]["escalated_from"], "source-1")
                    self.assertEqual(data["source"]["review_target"], "auto-1")
                    self.assertEqual(data["lineage"]["id"], "source-1")
                else:
                    self.assertEqual(data["pending"]["status"], "pending_review")
                    self.assertNotIn("approval", data["pending"])
                    self.assertTrue(data["denied"])
                    self.assertEqual(data["decision"]["review"], "required")
                    if name == "rework-restart":
                        self.assertTrue(data["restartBlocked"])
                        self.assertTrue(data["humanUnblocked"])
                    if name == "rework-approved":
                        self.assertEqual(data["approved"]["status"], "approved")
                        self.assertEqual(data["approved"]["approval"]["reviewer_id"], "review-2")
                        self.assertTrue(data["collected"])
                        self.assertTrue(data["changedResultDenied"])
                        self.assertNotIn("review-needs-human", data["messages"])
