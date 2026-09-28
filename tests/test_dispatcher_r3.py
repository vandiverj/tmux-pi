import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DISPATCHER = ROOT / "agent/extensions/dispatcher.ts"
ESCALATE = ROOT / "agent/extensions/escalate.ts"


class DispatcherR3StaticTests(unittest.TestCase):
    def test_ordinary_reply_language_is_not_an_escalation_trigger(self):
        source = DISPATCHER.read_text(encoding="utf-8")
        self.assertIn("TMUX_PI_ESCALATION_SIGNAL", source)
        self.assertIn("structuredSignal", source)
        self.assertNotIn("unable to|model error|context length|failed because", source)
        self.assertNotIn("missing .*capability", source)

    def test_structured_model_error_signal_can_escalate(self):
        source = DISPATCHER.read_text(encoding="utf-8")
        self.assertIn('trigger !== "model_error" && trigger !== "missing_capability"', source)
        self.assertIn("autoEscalateSettled(task, signal.trigger, signal.reason, signal.evidence)", source)

    def test_original_request_is_spawned_for_escalation_target(self):
        dispatcher = DISPATCHER.read_text(encoding="utf-8")
        local_extension = ESCALATE.read_text(encoding="utf-8")
        self.assertIn("task.request,", dispatcher)
        self.assertIn("requestText(dir)", local_extension)
        self.assertNotIn('"--", instruction', local_extension)

    def test_spawn_and_register_before_source_kill(self):
        source = DISPATCHER.read_text(encoding="utf-8")
        spawn = source.index('bin("tp-spawn")', source.index("const autoEscalateSettled"))
        register = source.index('bin("tp-contract"), ["escalated", sourceId', spawn)
        kill_source = source.index('bin("tp-kill"), [sourceId]', register)
        self.assertLess(spawn, register)
        self.assertLess(register, kill_source)
        self.assertIn('await run(bin("tp-kill"), [id]);', source)  # orphan-target cleanup on registration failure
        self.assertIn("source task is left recoverable", source)

    def test_required_review_is_recorded_before_spawned_pane_is_visible(self):
        source = (ROOT / "bin/tp-spawn").read_text(encoding="utf-8")
        marker = source.index('mv "$task_dir/review-required.json.tmp" "$task_dir/review-required.json"')
        pane = source.index('pane="$(tmux new-window', marker)
        self.assertLess(marker, pane)
        self.assertIn('{decision:$decision}', source)

    def test_review_round_lineage_survives_escalation(self):
        source = DISPATCHER.read_text(encoding="utf-8")
        settled = source[source.index("const autoEscalateSettled"):source.index("const autoEscalateHalted")]
        halted = source[source.index("const autoEscalateHalted"):source.index("// A halted contract task")]
        self.assertNotIn("rounds = 0", settled)
        self.assertNotIn("rounds: 0, since: Date.now()", halted)
        self.assertIn("const next: Delegated = { ...task, id, since: Date.now() };", source)
        self.assertIn("rounds: q.rounds ?? 0", source)


if __name__ == "__main__":
    unittest.main()
