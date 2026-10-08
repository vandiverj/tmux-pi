import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from contract import (ContractError, artifact_info, build, duration, dynamic_terms, evaluate, extension,  # noqa: E402
                      escalation_record, is_local, ledger_row, lifecycle, load_research, message, note, quantity, rate,
                      read_escalations, read_ledger, select_escalation, summary_line, upsert, write_escalations)
from router import load_config  # noqa: E402

T0 = 1_800_000_000  # a fixed clock; meta.created is this instant


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ContractTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.contract = build("research", str(self.dir / "report.md"), "time=20m,turns=50",
                              "outline=4m,draft=12m", "6m", "3m")
        self.meta = {"id": "research-1", "created": iso(T0), "contract": self.contract, "pane": "%1",
                     "worker": "claude", "model": "claude-sonnet-5-5", "thinking": "medium"}

    def art(self, status=None, age=0, now=T0):
        if status is False:
            return {"exists": False, "size": 0, "mtime": None, "status": None, "urls": 0}
        return {"exists": True, "size": 500, "mtime": now - age, "status": status, "urls": 2}

    def test_parsing_and_validation(self):
        self.assertEqual([duration("90s"), duration("20m"), duration("1h"), duration(45)], [90, 1200, 3600, 45])
        self.assertEqual([quantity("60"), quantity("400k"), quantity("2M")], [60, 400000, 2000000])
        for bad in ("0m", "10d", "", "-5m"):
            with self.assertRaises(ContractError):
                duration(bad)
        self.assertEqual(self.contract["budget"], {"seconds": 1200, "turns": 50, "tokens": None})
        self.assertEqual(self.contract["checkpoints"], {"outline": 240, "draft": 720})
        self.assertEqual(self.contract["warning_thresholds"]["time"], 0.8)
        for args in (("essay", "/r.md", "time=5m", "", "1m", "1m"),       # kind
                     ("task", "r.md", "time=5m", "", "1m", "1m"),         # relative artifact
                     ("task", "/r.md", "turns=5", "", "1m", "1m"),        # no time
                     ("task", "/r.md", "time=5m,cost=1", "", "1m", "1m"),  # unknown budget key
                     ("task", "/r.md", "time=5m", "outline=6m", "1m", "1m"),  # checkpoint after the limit
                     ("task", "/r.md", "time=20m", "outline=8m,draft=4m", "1m", "1m")):  # out of order
            with self.assertRaises(ContractError):
                build(*args)

    def test_reply_only_build_validation_and_enforcement(self):
        for kind, cp, flag in (("research", {}, True), ("task", "outline=1m", True),
                               ("task", {}, "true")):
            with self.assertRaises(ContractError):
                build(kind, "/x.md", "time=5m", cp, "1m", "1m", no_edits=flag)
        c = build("task", str(self.dir / "result.md"), "time=10m,turns=10,tokens=100",
                  {}, "2m", "1m", no_edits=True)
        meta = dict(self.meta, contract=c)
        missing = self.art(False)
        text = note(c, T0)
        self.assertIn("reply-only", text)
        for forbidden in ("artifact.md", "write your output", "Status:", "outline", "draft", "checkpoint"):
            self.assertNotIn(forbidden, text)
        state = {"state": "working", "turns": 8, "tokens": {"input": 80}}
        r = evaluate(meta, state, missing, T0 + 300, True)
        self.assertEqual((r["status"], set(r["warnings"]), r["action"]),
                         ("reply-only", {"turns", "tokens"}, None))  # beyond old outline
        self.assertIn("reply-only", r["phase"])
        self.assertIn("time", evaluate(meta, state, missing, T0 + 480, True)["warnings"])
        self.assertEqual(evaluate(meta, dict(state, tokens={"input": 100}), missing,
                                  T0 + 300, True)["action"][0], "stop")
        self.assertEqual(evaluate(meta, state, missing, T0 + 600, True)["action"][0], "stop")
        self.assertEqual(evaluate(meta, dict(state, toolErrors=4), missing, T0 + 300, True)["action"][0], "nudge-errors")
        errors = [{"action": "nudge-errors", "at": T0 + 300}]
        self.assertEqual(evaluate(meta, dict(state, toolErrors=4), missing, T0 + 361, True, errors)["action"][0], "pause")
        over = evaluate(meta, dict(state, turns=10), missing, T0 + 400, True)
        self.assertEqual(over["action"][0], "stop")
        self.assertNotIn("Status:", message("stop", c))
        self.assertNotIn("save", message("stop", c))
        stopped = [{"action": "stop", "reason": "turns budget used up", "at": T0 + 400}]
        self.assertEqual(evaluate(meta, dict(state, turns=10), missing, T0 + 461, True, stopped)["action"][0], "pause")
        halted = evaluate(meta, {"state": "waiting", "updated": iso(T0 + 450)}, missing, T0 + 470, True, stopped)
        self.assertIn("reply-only", halted["action"][1])
        self.assertNotIn("no partial", halted["action"][1])
        self.assertEqual(extension(meta, state, T0 + 500, stopped)["budget"]["turns"], 18)

    def test_phases_before_each_checkpoint(self):
        m = lambda minutes, art: evaluate(self.meta, {"state": "working", "turns": 1}, art, T0 + minutes * 60, True)
        self.assertEqual(m(1, self.art(False))["phase"], "outline due in 3m00s")
        self.assertEqual(m(5, self.art("outline", now=T0 + 300))["phase"], "draft due in 7m00s")
        self.assertEqual(m(13, self.art("draft", now=T0 + 780))["phase"], "limit in 7m00s")
        self.assertEqual(m(5, self.art(False))["phase"], "outline missed")
        self.assertEqual(m(13, self.art("outline", now=T0 + 780))["phase"], "draft missed")
        self.assertEqual(m(21, self.art("draft", now=T0 + 1260))["phase"], "over budget (time)")
        self.assertEqual(m(21, self.art("final", now=T0 + 1260))["phase"], "final")
        self.assertIn("time", m(17, self.art("draft", now=T0 + 1020))["warnings"])
        stalled = m(13, self.art("draft", age=400, now=T0 + 780))
        self.assertEqual(stalled["phase"], "no new writes for 6m40s")

    def test_actions_escalate_once_each(self):
        def act(minutes, art, events=(), state="working", turns=1):
            r = evaluate(self.meta, {"state": state, "turns": turns}, art, T0 + minutes * 60, True, events)
            return r["action"][0] if r["action"] else None

        empty = self.art(False)
        self.assertIsNone(act(3, empty))
        self.assertEqual(act(4, empty), "nudge-outline")
        self.assertIsNone(act(5, empty, ["nudge-outline"]))
        self.assertEqual(act(7, empty, ["nudge-outline"]), "pause")  # outline + grace
        self.assertIsNone(act(8, empty, ["nudge-outline", "pause"]))
        # Only while the worker is working: a worker waiting on the user is left alone.
        self.assertIsNone(act(7, empty, ["nudge-outline"], state="waiting"))
        self.assertIsNone(act(7, empty, ["nudge-outline"], state="permission"))

        outline = self.art("outline", now=T0 + 700)
        self.assertEqual(act(12, outline), "nudge-draft")
        self.assertIsNone(act(12.5, outline, ["nudge-draft"]))

        stale = self.art("draft", now=T0 + 700 - 400)
        self.assertTrue(act(12, stale).startswith("nudge-stall@"))
        self.assertIsNone(act(12, stale, [act(12, stale)]))

        fresh = lambda minutes: self.art("draft", now=T0 + minutes * 60)
        self.assertEqual(act(20, fresh(20)), "stop")
        stop = {"action": "stop", "at": T0 + 1200}
        self.assertIsNone(act(22, fresh(22), [stop]))
        self.assertEqual(act(23, fresh(23), [stop]), "pause")
        self.assertIsNone(act(23, self.art("final", now=T0 + 1380), [stop]))
        # The turn budget counts too.
        self.assertEqual(act(5, fresh(5), turns=50), "stop")

    def test_spawn_reply_only_packet_and_cli_status_fixture(self):
        state = self.dir / ".tmux-pi"
        state.mkdir()
        fake = self.dir / "fake-bin"
        fake.mkdir()
        tmux = fake / "tmux"
        tmux.write_text('#!/bin/sh\ncase "$1" in\n show-options) echo "$TP_STATE_DIR";;\n new-window) echo "%%42";;\n list-panes) echo "%%42 0 99999999";;\n esac\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), TP_SESSION="fixture",
                   PATH=str(fake) + os.pathsep + os.environ["PATH"])
        env.pop("TMUX", None)
        def spawn(label, pf, main, task=None):
            preflight, main_file = self.dir / (label + "-pf.json"), self.dir / (label + "-main.json")
            preflight.write_text(json.dumps(pf))
            main_file.write_text(json.dumps(main))
            run = subprocess.run([str(ROOT / "bin/tp-spawn"), "--workdir", str(self.dir), "--kind", "task", "--name", label,
                                  "--preflight", str(preflight), "--main-contract", str(main_file),
                                  "--note", "Task contract preflight: no_edits=True; no edit/apply/write tools" if
                                  (main.get("no_edits") or pf.get("no_edits")) else "Edit allowed",
                                  task or ("+delegate Read fixture.txt and report the color of cedar; do not modify files" if
                                  (main.get("no_edits") or pf.get("no_edits")) else "Fix fixture typo")],
                                 env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            d = state / "tasks" / run.stdout.strip()
            return d, json.loads((d / "meta.json").read_text()), (d / "packet.md").read_text()
        d, meta, packet = spawn("read-only", {"no_edits": False}, {"no_edits": True})
        c = meta["contract"]
        self.assertTrue(c["no_edits"])
        self.assertEqual(c["checkpoints"], {})
        self.assertEqual(c["artifact"], str(d / "result.md"))
        self.assertFalse((d / "artifact.md").exists())
        self.assertIn("+delegate Read fixture.txt", packet)
        self.assertIn("no edit/apply/write tools", packet)
        self.assertIn("reply-only", packet)
        self.assertNotIn("artifact.md", packet)
        self.assertNotIn("Status: outline", packet)
        d2, m2, p2 = spawn("scaffold-only", {"no_edits": True}, {"no_edits": False})
        self.assertTrue(m2["contract"]["no_edits"])
        self.assertIn("reply-only", p2)
        edit, edit_meta, edit_packet = spawn("edit-allowed", {"no_edits": False}, {"no_edits": False})
        self.assertNotIn("no_edits", edit_meta["contract"])
        self.assertIn("artifact.md", edit_packet)
        self.assertIn("Status: outline", edit_packet)
        self.assertEqual(json.loads((edit / "skill-selection.json").read_text()), [])
        doc, doc_meta, doc_packet = spawn("doc-read", {"no_edits": True}, {"no_edits": True},
                                          "+delegate Explain how to convert report.docx to Markdown; do not write files")
        selected = json.loads((doc / "skill-selection.json").read_text())
        self.assertEqual([s["id"] for s in selected], ["document-transform"])
        self.assertIn("# Selected Task Skills", doc_packet)
        self.assertIn("DOCX → Markdown", doc_packet)
        self.assertNotIn("Tester-owned setup", doc_packet)
        self.assertTrue(doc_meta["contract"]["no_edits"])
        # Beyond the outline deadline, CLI enforcement must not send file-based nudges.
        import time
        from datetime import datetime, timezone
        meta["created"] = datetime.fromtimestamp(time.time() - 180, timezone.utc).isoformat()
        (d / "meta.json").write_text(json.dumps(meta))
        (d / "state.json").write_text(json.dumps({"state": "working", "turns": 1}))
        status = subprocess.run([str(ROOT / "bin/tp-contract"), "status"], env=env, text=True, capture_output=True)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("reply-only", status.stdout)
        self.assertNotIn("result.md not written", status.stdout)
        # Disable the other two fixture panes so only the reply-only task is enforced.
        (edit / "meta.json").unlink()
        (d2 / "meta.json").unlink()
        enforce = subprocess.run([str(ROOT / "bin/tp-contract"), "enforce"], env=env, text=True, capture_output=True)
        self.assertEqual(enforce.returncode, 0, enforce.stderr)
        self.assertFalse((d / "checkpoints.json").exists(), enforce.stdout)
        (d / "result.md").write_text("cedar is green\n")  # reporter-owned reply fixture
        status = subprocess.run([str(ROOT / "bin/tp-contract"), "status"], env=env, text=True, capture_output=True)
        self.assertIn("reply-only; result.md recorded by reporter", status.stdout)
        extended = subprocess.run([str(ROOT / "bin/tp-contract"), "extend", d.name], env=env,
                                  text=True, capture_output=True)
        self.assertEqual(extended.returncode, 0, extended.stderr)
        self.assertIn("reply only", json.loads(extended.stdout)["message"])
        self.assertNotIn("Status:", extended.stdout)
        for args in (("--kind", "research", "--no-edits"),
                     ("--kind", "task", "--no-edits", "--checkpoints", "outline=1m"),
                     ("--kind", "task", "--no-edits", "--artifact", "/tmp/forbidden.md")):
            result = subprocess.run([str(ROOT / "bin/tp-contract"), "build", "--workdir", str(self.dir),
                                     "--task-dir", str(d), *args], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, args)
        bad = self.dir / "invalid-pf.json"
        bad.write_text('{"no_edits":"true"}')
        result = subprocess.run([str(ROOT / "bin/tp-spawn"), "--workdir", str(self.dir), "-k", "task", "--preflight", str(bad),
                                 "Read fixture.txt"], env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be boolean", result.stderr)

    def test_elapsed_stops_when_settled(self):
        r = evaluate(self.meta, {"state": "waiting", "turns": 9, "updated": iso(T0 + 600)}, self.art("final", now=T0 + 600),
                     T0 + 5000, True)
        self.assertEqual((r["elapsed"], r["turns"], r["action"]), (600, 9, None))

    def test_note_and_messages(self):
        text = note(self.contract, T0)
        for part in (str(self.dir / "report.md"), "Status: outline", "Status: draft", "Status: partial",
                     "20 minutes", "50 turns", "source fact", "access", "primary sources"):
            self.assertIn(part, text)
        self.assertNotIn("primary sources", note(build("task", "/x.md", "time=5m", "", "1m", "1m"), T0))
        for action in ("nudge-outline", "nudge-draft", "nudge-stall@5", "stop"):
            self.assertIn(self.contract["artifact"], message(action, self.contract))
        self.assertIsNone(message("pause", self.contract))

    def test_artifact_info_reads_status_and_sources(self):
        path = self.dir / "report.md"
        self.assertFalse(artifact_info(str(path))["exists"])
        path.write_text("# Report\n\n**Status:** draft\n\nSee https://a.example/x and https://b.example/y, "
                        "again https://a.example/x.\n")
        info = artifact_info(str(path))
        self.assertEqual((info["exists"], info["status"], info["urls"]), (True, "draft", 2))
        path.write_text("Status: Final\n")
        self.assertEqual(artifact_info(str(path))["status"], "final")
        path.write_text("no status here\n")
        self.assertIsNone(artifact_info(str(path))["status"])

    def test_ledger_record_and_rate(self):
        task = self.dir / "tasks" / "research-1"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps(self.meta))
        (task / "task.md").write_text("Which HTTP caching headers does the CDN honor?\n")
        (task / "state.json").write_text(json.dumps({"state": "waiting", "turns": 12, "model": "claude-sonnet-5-5",
                                                     "updated": iso(T0 + 900), "cost": 0,
                                                     "tokens": {"input": 10, "output": 20, "cacheRead": 30, "cacheWrite": 0}}))
        (task / "checkpoints.json").write_text(json.dumps({"events": [{"action": "nudge-draft", "at": T0 + 720, "reason": "x"}]}))
        (self.dir / "report.md").write_text("Status: final\nhttps://cdn.example/docs\n")
        ledger = self.dir / "ledger.jsonl"
        row = upsert(ledger, ledger_row(task, T0 + 1000))
        self.assertEqual((row["kind"], row["elapsed_s"], row["turns"], row["artifact_status"], row["source_urls"],
                          row["checkpoints"], row["stop"], row["provider"]),
                         ("research", 900, 12, "final", 1, ["nudge-draft"], "waiting", "anthropic"))
        rate(ledger, "research-1", {"sources": "ok", "useful": "partly", "note": "missed one header"})
        upsert(ledger, ledger_row(task, T0 + 2000))  # a refresh keeps the rating
        rows = read_ledger(ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rating"], {"sources": "ok", "useful": "partly", "note": "missed one header"})
        for bad in ({"sources": "great"}, {"speed": "fast"}):
            with self.assertRaises(ContractError):
                rate(ledger, "research-1", bad)
        with self.assertRaises(ContractError):
            rate(ledger, "nope", {"useful": "yes"})

    def test_dynamic_terms_and_escalation_target(self):
        brief = dynamic_terms("task", {"route": "standard", "scope": "brief", "model": "mlx/qwen"})
        strong = dynamic_terms("task", {"route": "complex", "scope": "multi_step", "model": "openai-codex/gpt-6-sol"})
        self.assertLess(duration(brief["budget"]["time"]), duration(strong["budget"]["time"]))
        self.assertEqual(brief["profile"]["source"], "dynamic")
        roster = load_config(ROOT / "config/router.json", local_path=False)["models"]
        preferred = {"worker": "claude", "model": "claude-opus-5-5", "thinking": "medium"}
        self.assertEqual(select_escalation({"worker": "claude", "model": "claude-sonnet-5-5", "thinking": "medium"}, roster,
                                           preferred=preferred), preferred)
        target = select_escalation({"worker": "pi", "model": "openai-codex/gpt-6-luna", "thinking": "medium"}, roster)
        self.assertEqual(target["model"], "openai-codex/gpt-6-sol")
        self.assertNotIn(target["thinking"], ("off", "minimal"))
        local_preferred = {"worker": "pi", "model": "openai-codex/gpt-6-luna", "thinking": "low"}
        for local_model in ("slotstream/qwen3.8-flash-next:4bit", "lmstudio/qwen/qwen3.6-35b-a3b"):
            escalated = select_escalation({"worker": "pi", "model": local_model, "thinking": "off"}, roster,
                                          preferred=local_preferred)
            self.assertIsNotNone(escalated)
            self.assertEqual(escalated, local_preferred)
            self.assertIn(escalated["thinking"], next(r["thinking"] for r in roster if r["model"] == escalated["model"]))
        self.assertNotIn("openai-codex/gpt-5.3-codex-spark", [r["model"] for r in roster])
        self.assertNotIn("off", next(r["thinking"] for r in roster if r["model"] == "openai-codex/gpt-6-astra"))
        self.assertIsNone(select_escalation({"worker": "pi", "model": "unknown/model", "thinking": "medium"}, roster))
        local_roster = [
            {"worker": "pi", "model": "slotstream/qwen3.8-flash-next:4bit", "thinking": ["low"]},
            {"worker": "pi", "model": "lmstudio/qwen/qwen3.6-35b-a3b", "thinking": ["low"], "qualified_local_escalation": True},
        ]
        self.assertEqual(select_escalation({"worker": "pi", "model": "slotstream/qwen3.8-flash-next:4bit", "thinking": "off"}, local_roster)["model"],
                         "lmstudio/qwen/qwen3.6-35b-a3b")

    def test_collect_refuses_unresolved_delegated_review_even_if_worker_settled(self):
        state = self.dir / "collect-state"
        task = state / "tasks" / "worker-1"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "worker-1", "worker": "pi", "pane": "%1"}))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "updated": iso(T0)}))
        (task / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
        (task / "exit").write_text("0\n")
        marker = task / "review-required.json"
        marker.write_text(json.dumps({"task_id": "worker-1", "status": "needs_human", "required": True,
                                      "reasons": ["reviewer unavailable"]}))
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 1 99999999"\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        cmd = [str(ROOT / "bin/tp-contract"), "collect", "worker-1"]
        for content in (marker.read_text(), "{bad json", json.dumps({"task_id": "wrong", "status": "resolved"})):
            marker.write_text(content)
            result = subprocess.run(cmd, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("review", result.stderr)
            self.assertFalse((task / "completed").exists())
        human_env = dict(env)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            human_env.pop(key, None)
        marker.write_text(json.dumps({"task_id": "worker-1", "status": "needs_human", "required": True}))
        resolved = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(state), "review-resolve",
                                   "--task-id", "worker-1", "--outcome", "approved", "--reason", "Human waiver",
                                   "--confirm", "RESOLVE-REVIEW-REQUIRED"], env=human_env,
                                  text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        collected = subprocess.run(cmd, env=env, text=True, capture_output=True)
        self.assertEqual(collected.returncode, 0, collected.stderr)
        self.assertEqual(collected.stdout.strip(), "completed")
        self.assertTrue((task / "completed").exists())

    def test_slot_after_waits_for_a_reply_with_a_result_status(self):
        state = self.dir / "slot-state"
        task = state / "tasks" / "dep"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "dep", "worker": "pi", "pane": "%1"}))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "updated": iso(T0)}))
        (task / "result.md").write_text("Started a sleep; waiting.\n")
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 0 99999999"\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        slot = lambda: json.loads(subprocess.run(
            [str(ROOT / "bin/tp-contract"), "slot", "--after", "dep", "--model", "x/y"],
            env=env, text=True, capture_output=True, check=True).stdout)
        held = slot()
        self.assertFalse(held["ok"], held)
        self.assertEqual(held["states"]["dep"], "paused")
        self.assertIn("dep", held["reason"])
        (task / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
        self.assertTrue(slot()["ok"])

    def test_slot_after_waits_for_a_paused_predecessor_even_with_a_result_status(self):
        state = self.dir / "slot-paused-state"
        task = state / "tasks" / "dep"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "dep", "worker": "pi", "pane": "%1"}))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "updated": iso(T0)}))
        (task / "result.md").write_text("Out of budget.\nRESULT_STATUS: blocked\n")
        (task / "checkpoints.json").write_text(json.dumps({"events": [{"action": "pause", "reason": "budget", "at": T0}]}))
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 0 99999999"\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        slot = lambda: json.loads(subprocess.run(
            [str(ROOT / "bin/tp-contract"), "slot", "--after", "dep", "--model", "x/y"],
            env=env, text=True, capture_output=True, check=True).stdout)
        held = slot()
        self.assertEqual(held["states"]["dep"], "paused")
        self.assertFalse(held["ok"], held)
        self.assertIn("dep", held["reason"])
        (task / "checkpoints.json").write_text(json.dumps({"events": [{"action": "pause", "at": T0}, {"action": "extend", "at": T0 + 100}]}))
        self.assertTrue(slot()["ok"])  # continued and settled with a status: the dependency is met

    def test_required_review_without_marker_and_independent_approval(self):
        import hashlib
        state = self.dir / "review-state"
        task = state / "tasks" / "author"
        task.mkdir(parents=True)
        meta = {"id": "author", "worker": "pi", "pane": "%1", "decision": {"review": "required"}}
        (task / "meta.json").write_text(json.dumps(meta))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "updated": iso(T0)}))
        (task / "exit").write_text("0\n")
        (task / "result.md").write_text("Done.\nRESULT_STATUS: complete\n")
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 1 99999999"\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        cmd = [str(ROOT / "bin/tp-contract"), "collect", "author"]
        def collect_ok():
            return subprocess.run(cmd, env=env, text=True, capture_output=True).returncode == 0
        self.assertFalse(collect_ok())  # Legacy task / lost watcher.
        marker = task / "review-required.json"
        self.assertEqual(json.loads(marker.read_text())["status"], "needs_human")
        human_env = dict(env)
        for key in ("TP_TASK_DIR", "TP_TASK_ID", "TMUX", "TMUX_PANE"):
            human_env.pop(key, None)
        resolved = subprocess.run([str(ROOT / "bin/tp-task"), "--state-dir", str(state), "review-resolve",
                                  "--task-id", "author", "--outcome", "approved", "--reason", "Legacy review waived",
                                  "--confirm", "RESOLVE-REVIEW-REQUIRED"], env=human_env, text=True, capture_output=True)
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertTrue(collect_ok())
        self.assertTrue((state / "review-resolutions.log").exists())
        (task / "completed").unlink()
        marker.write_text(json.dumps({"required": True, "status": "pending_review", "task_id": "author"}))
        self.assertFalse(collect_ok())
        reviewer = state / "tasks" / "reviewer"
        reviewer.mkdir()
        (reviewer / "meta.json").write_text(json.dumps({"id": "reviewer", "worker": "pi"}))
        (reviewer / "review-of").write_text("author\n")
        report = "APPROVED\nindependently inspected."
        (reviewer / "result.md").write_text(report)
        approved = {"required": True, "status": "approved", "task_id": "author", "round_nonce": "test-author-round",
                    "approval": {"reviewer_id": "reviewer", "round_nonce": "test-author-round", "evidence_sha256": hashlib.sha256(report.encode()).hexdigest(),
                                 "author_sha256": hashlib.sha256((task / "result.md").read_bytes()).hexdigest()}}
        meta["review"] = "reviewer"
        (task / "meta.json").write_text(json.dumps(meta))
        marker.write_text(json.dumps(approved))
        self.assertFalse(collect_ok())  # Same provider.
        (reviewer / "meta.json").write_text(json.dumps({"id": "reviewer", "worker": "claude"}))
        (reviewer / "result.md").write_text("REQUEST_CHANGES: not approved")
        self.assertFalse(collect_ok())  # Stale evidence / rejected report.
        (reviewer / "result.md").write_text("NOT APPROVED: fail.")
        approved["approval"]["evidence_sha256"] = hashlib.sha256(b"NOT APPROVED: fail.").hexdigest()
        marker.write_text(json.dumps(approved))
        self.assertFalse(collect_ok())  # A negated keyword is not an approval.
        for text, collects in (("APPROVED_WITH_NOTES\nN1 wording gap.", True), ("APPROVED_WITH_NOTES\n", False), ("APPROVED_PENDING: x", False)):
            (reviewer / "result.md").write_text(text)
            approved["approval"]["evidence_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            marker.write_text(json.dumps(approved))
            self.assertEqual(collect_ok(), collects, text)  # notes approve; no notes or an unknown verdict do not
            if collects:
                (task / "completed").unlink()
        (reviewer / "result.md").write_text(report)
        approved["approval"]["evidence_sha256"] = hashlib.sha256(report.encode()).hexdigest()
        marker.write_text(json.dumps(approved))
        self.assertTrue(collect_ok())
        (task / "completed").unlink()
        del approved["approval"]["author_sha256"]
        (task / "result.md").write_text("Changed after legacy approval")
        marker.write_text(json.dumps(approved))
        self.assertFalse(collect_ok())  # No hashless legacy approval can authorize a new result.

    def test_escalated_review_lineage_blocks_until_target_approved(self):
        import hashlib
        from task_record import unresolved_review
        state = self.dir / "lineage" / "tasks"
        source, target, reviewer = (state / name for name in ("source", "target", "reviewer"))
        for d in (source, target, reviewer):
            d.mkdir(parents=True)
        (source / "meta.json").write_text(json.dumps({"id": "source", "worker": "pi"}))
        (source / "review-required.json").write_text(json.dumps({"required": True, "task_id": "source",
            "status": "pending_review", "review_target": "target"}))
        (target / "meta.json").write_text(json.dumps({"id": "target", "worker": "pi", "review": "reviewer", "escalated_from": {"id": "source"}}))
        (reviewer / "meta.json").write_text(json.dumps({"id": "reviewer", "worker": "claude"}))
        (reviewer / "review-of").write_text("target")
        report = "APPROVED\nchecked source and target."
        (reviewer / "result.md").write_text(report)
        marker = {"required": True, "task_id": "target", "status": "pending_review"}
        (target / "review-required.json").write_text(json.dumps(marker))
        self.assertTrue(unresolved_review(source / "review-required.json", "source"))
        (target / "result.md").write_text("Target result")
        marker.update(status="approved", round_nonce="test-target-round", approval={"reviewer_id": "reviewer", "round_nonce": "test-target-round", "evidence_sha256": hashlib.sha256(report.encode()).hexdigest(),
                                                   "author_sha256": hashlib.sha256(b"Target result").hexdigest()})
        (target / "review-required.json").write_text(json.dumps(marker))
        self.assertFalse(unresolved_review(source / "review-required.json", "source"))
        (source / "review-required.json").write_text(json.dumps({"required": True, "task_id": "source", "status": "needs_human", "review_target": "target"}))
        self.assertTrue(unresolved_review(source / "review-required.json", "source"))

    def test_local_escalation_cli_prefers_available_gpt6(self):
        state = self.dir / "state"
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 1 99999999"\n')
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        for idx, model in enumerate(("slotstream/qwen3.8-flash-next:4bit", "lmstudio/qwen/qwen3.6-35b-a3b")):
            task_id = "local-{}".format(idx)
            d = state / "tasks" / task_id
            d.mkdir(parents=True)
            (d / "meta.json").write_text(json.dumps({"id": task_id, "pane": "%1", "worker": "pi",
                                                     "model": model, "thinking": "off", "created": iso(T0)}))
            (d / "state.json").write_text(json.dumps({"state": "working"}))
            (d / "exit").write_text("1\n")
            outcome = subprocess.run([str(ROOT / "bin/tp-contract"), "escalate", task_id,
                                      "--trigger", "model_error", "--reason", "failed"],
                                     text=True, capture_output=True, env=env)
            self.assertEqual(outcome.returncode, 0, outcome.stderr)
            self.assertEqual(json.loads(outcome.stdout)["target"],
                             {"worker": "pi", "model": "openai-codex/gpt-6-luna", "thinking": "low"})

    def test_escalation_cli_plans_without_marking_and_failed_spawn_is_recoverable(self):
        state = self.dir / "state"
        task = state / "tasks" / "task-1"
        task.mkdir(parents=True)
        meta = {"id": "task-1", "created": iso(T0), "pane": "%1", "worker": "pi",
                "model": "openai-codex/gpt-6-luna", "thinking": "medium"}
        (task / "meta.json").write_text(json.dumps(meta))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "updated": iso(T0 + 60)}))
        (task / "checkpoints.json").write_text(json.dumps({"events": []}))
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\necho "%1 1 99999999"\n')
        tmux.chmod(0o755)
        (task / "exit").write_text("1\n")
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        plan = subprocess.run([str(ROOT / "bin/tp-contract"), "escalate", "task-1", "--trigger", "model_error",
                               "--reason", "structured reporter error", "--evidence", "state.json:error"],
                              env=env, text=True, capture_output=True, check=True)
        out = json.loads(plan.stdout)
        self.assertEqual(out["target"]["model"], "openai-codex/gpt-6-sol")
        self.assertFalse((task / "escalation.json").exists())
        failed = subprocess.run([str(ROOT / "bin/tp-contract"), "escalated", "task-1", "--outcome", "failed",
                                 "--record-json", json.dumps(out["record"]), "--message", "spawn failed"],
                                env=env, text=True, capture_output=True, check=True)
        self.assertEqual(failed.returncode, 0)
        self.assertFalse((task / "escalated").exists())
        records = read_escalations(task)
        self.assertEqual(records[-1]["outcome"], "failed")
        self.assertEqual(records[-1]["message"], "spawn failed")

    def test_escalation_cli_refuses_killed_cancelled_and_completed(self):
        state = self.dir / "states"
        task = state / "tasks" / "stopped"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps({"id": "stopped", "pane": "%1", "worker": "pi",
                                                    "model": "openai-codex/gpt-6-luna", "thinking": "medium"}))
        # Never ask the real tmux server: its pane %1 may be alive, which would make "killed" untestable.
        panes = self.dir / "panes"
        tmux = self.dir / "tmux"
        tmux.write_text('#!/bin/sh\ncat "%s"\n' % panes)
        tmux.chmod(0o755)
        env = dict(os.environ, TP_STATE_DIR=str(state), PATH=str(self.dir) + os.pathsep + os.environ.get("PATH", ""))
        cmd = [str(ROOT / "bin/tp-contract"), "escalate", "stopped", "--trigger", "model_error", "--reason", "error"]
        # killed: the pane is gone; cancelled and completed: the pane is still alive.
        for reported, listing in (("working", ""), ("cancelled", "%1 0\n"), ("completed", "%1 0\n")):
            panes.write_text(listing)
            (task / "state.json").write_text(json.dumps({"state": reported}))
            result = subprocess.run(cmd, env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0, reported)
            self.assertIn("blocked", result.stderr)
            self.assertFalse((task / "escalation.json").exists())

    def test_escalation_chain_cap_includes_target_lineage(self):
        state = self.dir / "state2"
        task = state / "tasks" / "task-2"
        task.mkdir(parents=True)
        meta = {"id": "task-2", "created": iso(T0), "pane": "%1", "worker": "pi",
                "model": "openai-codex/gpt-6-sol", "thinking": "medium",
                "escalated_from": {"id": "task-1", "trigger": "model_error"}}
        (task / "meta.json").write_text(json.dumps(meta))
        env = dict(os.environ, TP_STATE_DIR=str(state))
        denied = subprocess.run([str(ROOT / "bin/tp-contract"), "escalate", "task-2", "--trigger", "model_error",
                                 "--reason", "structured reporter error", "--evidence", "state.json:error"],
                                env=env, text=True, capture_output=True)
        self.assertNotEqual(denied.returncode, 0)
        self.assertTrue("already used its one automatic escalation" in denied.stderr or "automatic escalation is blocked" in denied.stderr)

    def test_escalation_record_persists_and_ledger_includes_it(self):
        task = self.dir / "tasks" / "research-esc"
        task.mkdir(parents=True)
        meta = dict(self.meta, id="research-esc")
        (task / "meta.json").write_text(json.dumps(meta))
        (task / "task.md").write_text("Find the official docs.\n")
        (task / "state.json").write_text(json.dumps({"state": "waiting", "turns": 3, "updated": iso(T0 + 300)}))
        (task / "checkpoints.json").write_text(json.dumps({"events": [{"action": "auto-escalate", "at": T0 + 300}]}))
        (self.dir / "report.md").write_text("Status: partial\n")
        rec = escalation_record(meta, "budget", "time budget used up", "Status: partial", {"worker": "claude", "model": "claude-opus-5-5", "thinking": "medium"}, T0 + 301, outcome="started")
        write_escalations(task, [rec])
        self.assertEqual(read_escalations(task)[0]["trigger"], "budget")
        self.assertEqual(ledger_row(task, T0 + 400)["escalation"]["target"]["model"], "claude-opus-5-5")

    def test_research_config(self):
        roster = load_config(ROOT / "config/router.json", local_path=False)["models"]
        config = load_research(ROOT / "config/research.json", roster)
        self.assertEqual(config["worker"], "claude")
        good = json.loads((ROOT / "config/research.json").read_text())
        bad_escalate = {"escalate": {"worker": "claude", "model": "claude-nope", "thinking": "medium"}}
        for change in ({"model": "claude-nope"}, {"thinking": "turbo"}, {"max_active": 0}, {"max_local": True},
                       {"budget": {"turns": 5}}, {"extra": 1}, bad_escalate, {"tool_errors": 0}):
            path = self.dir / "research.json"
            path.write_text(json.dumps(dict(good, **change)))
            with self.assertRaises(ContractError):
                load_research(path, roster)

    def test_breakers_after_a_reminder(self):
        def act(minutes, art, events=(), errors=0):
            state = {"state": "working", "turns": 3, "toolErrors": errors}
            r = evaluate(self.meta, state, art, T0 + minutes * 60, True, events)
            return r["action"][0] if r["action"] else None

        draft = self.art("draft", now=T0 + 300)  # last written at 5m
        self.assertEqual(act(8, draft, ["nudge-outline"], errors=4), "nudge-errors")
        nudged = [{"action": "nudge-errors", "at": T0 + 480}]
        self.assertIsNone(act(10, draft, nudged, errors=5))  # within the grace period
        self.assertEqual(act(11, draft, nudged, errors=6), "pause")
        self.assertNotEqual(act(11, draft, nudged, errors=0), "pause")  # a success in between resets the run
        stall = "nudge-stall@{}".format(T0 + 300)
        self.assertEqual(act(11, draft), stall)
        self.assertIsNone(act(13, draft, [{"action": stall, "at": T0 + 660}]))
        self.assertEqual(act(14, draft, [{"action": stall, "at": T0 + 660}]), "pause")

    def test_halted_and_one_extension(self):
        stop = {"action": "stop", "reason": "time budget used up", "at": T0 + 1200}
        waiting = {"state": "waiting", "turns": 20, "updated": iso(T0 + 1260)}
        partial = self.art("partial", now=T0 + 1250)
        r = evaluate(self.meta, waiting, partial, T0 + 1300, True, [stop])
        self.assertEqual(r["action"], ("halted", "stopped (time budget used up); report saved with Status: partial"))
        r = evaluate(self.meta, waiting, self.art("draft", now=T0 + 900), T0 + 1300, True, [stop])
        self.assertIn("no partial report saved (left at Status: draft)", r["action"][1])
        halted = [stop, {"action": "halted", "at": T0 + 1300}]
        self.assertIsNone(evaluate(self.meta, waiting, partial, T0 + 1400, True, halted)["action"])
        # Waiting at a permission prompt is not halted.
        self.assertIsNone(evaluate(self.meta, dict(waiting, state="permission"), partial, T0 + 1300, True, [stop])["action"])

        # A paused Claude worker still says "working" (no Stop on interrupt): no reminders, and
        # halted once its tool count has not moved since the pause; typed into again, it is working.
        pause = {"action": "pause", "reason": "no outline", "at": T0 + 400, "tools": 9}
        stuck = {"state": "working", "turns": 4, "tools": 9}
        r = evaluate(self.meta, stuck, self.art(None), T0 + 800, True, [pause])
        self.assertEqual(r["action"][0], "halted")
        self.assertIsNone(evaluate(self.meta, dict(stuck, tools=12), self.art(None), T0 + 800, True, [pause])["action"])
        # After a stop, no more reminders; only the pause.
        self.assertIsNone(evaluate(self.meta, dict(stuck, tools=12), self.art(None), T0 + 1210, True, [stop])["action"])

        ext = extension(self.meta, waiting, T0 + 1500, halted)
        self.assertEqual(ext["budget"], {"seconds": 1260 + 1200, "turns": 70, "tokens": None})
        with self.assertRaises(ContractError):
            extension(self.meta, waiting, T0 + 1600, halted + [ext])
        events = halted + [ext]
        working = {"state": "working", "turns": 21}
        self.assertIsNone(evaluate(self.meta, working, partial, T0 + 1600, True, events)["action"])
        # The stall clock restarts at the extension (the last write was at T0 + 1250).
        self.assertIsNone(evaluate(self.meta, working, partial, T0 + 1500 + 300, True, events)["action"])
        self.assertEqual(evaluate(self.meta, working, partial, T0 + 2460, True, events)["action"][0], "stop")
        self.assertEqual(evaluate(self.meta, dict(working, turns=70), partial, T0 + 1600, True, events)["action"][0], "stop")

    def test_lifecycle(self):
        OK = "Done.\nRESULT_STATUS: complete"
        at = lambda s: {"state": s, "updated": iso(T0 + 100)}
        self.assertEqual(lifecycle({}, "alive"), "starting")
        self.assertEqual(lifecycle(at("tool"), "alive"), "working")
        self.assertEqual(lifecycle(at("permission"), "alive"), "needs_input")
        # A question without a RESULT_STATUS line is still a reply without one: paused, never needs_input.
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.\nWhich file?"), "paused")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.\nRESULT_STATUS: blocked\nWhich file?"), "needs_input")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply=OK), "settled")
        # A reply with no RESULT_STATUS line is paused, never settled.
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done."), "paused")
        self.assertEqual(lifecycle(at("waiting"), "alive"), "paused")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="0", reply="Done."), "paused")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply=OK, completed_at=T0 + 120), "completed")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply=OK, completed_at=T0 + 50), "settled")  # new turn since
        # Claude touches state.json after its idle notification; the reply time decides.
        self.assertEqual(lifecycle(at("waiting"), "alive", reply=OK, completed_at=T0 + 50, replied_at=T0 + 40), "completed")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="0", reply="New round\n"+OK, completed_at=T0 + 50,
                                   replied_at=T0 + 120), "settled")
        self.assertEqual(lifecycle(at("waiting"), "alive", [{"action": "pause", "at": T0 + 30}], replied_at=T0 + 35), "paused")
        pause = [{"action": "pause", "at": T0 + 95}]
        self.assertEqual(lifecycle(dict(at("working"), tools=9), "alive", [{"action": "pause", "at": T0 + 95, "tools": 9}]), "paused")
        self.assertEqual(lifecycle(dict(at("working"), tools=10), "alive", [{"action": "pause", "at": T0 + 95, "tools": 9}]), "working")
        self.assertEqual(lifecycle(at("error"), "alive", pause), "paused")
        self.assertEqual(lifecycle(at("error"), "alive"), "failed")
        self.assertEqual(lifecycle(at("waiting"), "alive", pause + [{"action": "extend", "at": T0 + 99}], reply=OK), "settled")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="0", reply=OK), "settled")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="1"), "failed")
        self.assertEqual(lifecycle(at("working"), "gone"), "killed")
        self.assertEqual(lifecycle(at("working"), "alive", escalated=True), "escalated")
        self.assertEqual(summary_line("Answer: 3.11\n\nShall I go on?", question=True), "Shall I go on?")
        self.assertEqual(summary_line("Answer: 3.11\n\nmore"), "Answer: 3.11")
        self.assertTrue(is_local("lmstudio/qwen/qwen3.6-35b-a3b"))
        self.assertFalse(is_local("claude-sonnet-5-5") or is_local("openai-codex/gpt-5.5"))

    def test_ledger_stop_reason(self):
        task = self.dir / "tasks" / "research-2"
        task.mkdir(parents=True)
        (task / "meta.json").write_text(json.dumps(dict(self.meta, id="research-2")))
        (task / "state.json").write_text(json.dumps({"state": "waiting", "turns": 3, "updated": iso(T0 + 1300)}))
        events = [{"action": "stop", "reason": "time budget used up", "at": T0 + 1200}]
        (task / "checkpoints.json").write_text(json.dumps({"events": events}))
        (self.dir / "report.md").write_text("Status: draft\n")
        self.assertEqual(ledger_row(task, T0 + 1400)["stop"], "stop: time budget used up; no partial saved")
        (self.dir / "report.md").write_text("Status: partial\n")
        self.assertEqual(ledger_row(task, T0 + 1400)["stop"], "stop: time budget used up; saved partial")
        events.append({"action": "escalate", "to": "research-3", "at": T0 + 1500})
        (task / "checkpoints.json").write_text(json.dumps({"events": events}))
        self.assertEqual(ledger_row(task, T0 + 1600)["stop"], "escalate to research-3")


class EnforceKeepsTheRecordTests(unittest.TestCase):
    """cmd_enforce persists a pause in checkpoints.json; task.md must say so too, even when typing the Escape fails."""

    def setUp(self):
        import importlib.machinery
        import importlib.util
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / ".tmux-pi"
        self.task = self.state / "tasks" / "coder-1"
        self.task.mkdir(parents=True)
        contract = build("task", str(self.task / "artifact.md"), "time=20m", "", "6m", "3m")
        (self.task / "meta.json").write_text(json.dumps({"id": "coder-1", "created": iso(T0), "contract": contract,
                                                         "pane": "%1", "worker": "claude"}))
        (self.task / "state.json").write_text(json.dumps({"state": "working", "turns": 3, "tools": 4}))
        loader = importlib.machinery.SourceFileLoader("tp_contract_for_enforce", str(ROOT / "bin/tp-contract"))
        self.tool = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.tool)

    def recorded_state(self):
        import re
        match = re.search(r"- \*\*Lifecycle State:\*\* `([^`]+)`", (self.task / "task.md").read_text())
        return match.group(1) if match else None

    def enforce(self, send_fails):
        from unittest import mock
        self.tool.write_record(self.task, lifecycle_state="working", force=True)
        self.assertEqual(self.recorded_state(), "working")

        def run(command, *args, **kwargs):
            if send_fails:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)
        decision = {"action": ("pause", "stalled"), "status": None, "elapsed": 60, "turns": 3}
        with mock.patch.dict(os.environ, {"TP_STATE_DIR": str(self.state)}), \
                mock.patch.object(self.tool, "panes", return_value={"%1": "alive"}), \
                mock.patch.object(self.tool, "evaluate", return_value=decision), \
                mock.patch.object(self.tool.subprocess, "run", side_effect=run), \
                mock.patch.object(self.tool.time, "sleep"):
            self.tool.cmd_enforce(None)

    def test_a_pause_updates_the_record_with_no_pane_involved(self):
        self.enforce(send_fails=False)
        self.assertEqual(self.tool.task_lifecycle(self.task, "alive")[0], "paused")
        self.assertEqual(self.recorded_state(), "paused")

    def test_a_pause_whose_escape_cannot_be_sent_still_updates_the_record(self):
        self.enforce(send_fails=True)
        self.assertEqual(json.loads((self.task / "checkpoints.json").read_text())["events"][-1]["action"], "pause")
        self.assertEqual(self.recorded_state(), "paused")


if __name__ == "__main__":
    unittest.main()
