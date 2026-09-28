import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from contract import (ContractError, artifact_info, build, duration, evaluate, extension, is_local,  # noqa: E402
                      ledger_row, lifecycle, load_research, message, note, quantity, rate, read_ledger, summary_line,
                      upsert)
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
                     "worker": "claude", "model": "claude-sonnet-5", "thinking": "medium"}

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
        for args in (("essay", "/r.md", "time=5m", "", "1m", "1m"),       # kind
                     ("task", "r.md", "time=5m", "", "1m", "1m"),         # relative artifact
                     ("task", "/r.md", "turns=5", "", "1m", "1m"),        # no time
                     ("task", "/r.md", "time=5m,cost=1", "", "1m", "1m"),  # unknown budget key
                     ("task", "/r.md", "time=5m", "outline=6m", "1m", "1m"),  # checkpoint after the limit
                     ("task", "/r.md", "time=20m", "outline=8m,draft=4m", "1m", "1m")):  # out of order
            with self.assertRaises(ContractError):
                build(*args)

    def test_phases_before_each_checkpoint(self):
        m = lambda minutes, art: evaluate(self.meta, {"state": "working", "turns": 1}, art, T0 + minutes * 60, True)
        self.assertEqual(m(1, self.art(False))["phase"], "outline due in 3m00s")
        self.assertEqual(m(5, self.art("outline", now=T0 + 300))["phase"], "draft due in 7m00s")
        self.assertEqual(m(13, self.art("draft", now=T0 + 780))["phase"], "limit in 7m00s")
        self.assertEqual(m(5, self.art(False))["phase"], "outline missed")
        self.assertEqual(m(13, self.art("outline", now=T0 + 780))["phase"], "draft missed")
        self.assertEqual(m(21, self.art("draft", now=T0 + 1260))["phase"], "over budget (time)")
        self.assertEqual(m(21, self.art("final", now=T0 + 1260))["phase"], "final")
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
        (task / "state.json").write_text(json.dumps({"state": "waiting", "turns": 12, "model": "claude-sonnet-5",
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
        at = lambda s: {"state": s, "updated": iso(T0 + 100)}
        self.assertEqual(lifecycle({}, "alive"), "starting")
        self.assertEqual(lifecycle(at("tool"), "alive"), "working")
        self.assertEqual(lifecycle(at("permission"), "alive"), "needs_input")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.\nWhich file?"), "needs_input")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done."), "settled")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.", completed_at=T0 + 120), "completed")
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.", completed_at=T0 + 50), "settled")  # new turn since
        # Claude touches state.json after its idle notification; the reply time decides.
        self.assertEqual(lifecycle(at("waiting"), "alive", reply="Done.", completed_at=T0 + 50, replied_at=T0 + 40), "completed")
        self.assertEqual(lifecycle(at("waiting"), "alive", [{"action": "pause", "at": T0 + 30}], replied_at=T0 + 35), "paused")
        pause = [{"action": "pause", "at": T0 + 95}]
        self.assertEqual(lifecycle(dict(at("working"), tools=9), "alive", [{"action": "pause", "at": T0 + 95, "tools": 9}]), "paused")
        self.assertEqual(lifecycle(dict(at("working"), tools=10), "alive", [{"action": "pause", "at": T0 + 95, "tools": 9}]), "working")
        self.assertEqual(lifecycle(at("error"), "alive", pause), "paused")
        self.assertEqual(lifecycle(at("error"), "alive"), "failed")
        self.assertEqual(lifecycle(at("waiting"), "alive", pause + [{"action": "extend", "at": T0 + 99}]), "settled")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="0"), "settled")
        self.assertEqual(lifecycle(at("waiting"), "dead", exit_code="1"), "failed")
        self.assertEqual(lifecycle(at("working"), "gone"), "killed")
        self.assertEqual(lifecycle(at("working"), "alive", escalated=True), "escalated")
        self.assertEqual(summary_line("Answer: 3.11\n\nShall I go on?", question=True), "Shall I go on?")
        self.assertEqual(summary_line("Answer: 3.11\n\nmore"), "Answer: 3.11")
        self.assertTrue(is_local("lmstudio/qwen/qwen3.6-35b-a3b"))
        self.assertFalse(is_local("claude-sonnet-5") or is_local("openai-codex/gpt-5.5"))

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


if __name__ == "__main__":
    unittest.main()
