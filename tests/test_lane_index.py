"""L9: lane and lane_step in the task index and `tp-agent list`; a `lanes` summary block. Display only."""
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LaneIndexTests(unittest.TestCase):
    def setUp(self):
        self.agent = SourceFileLoader("tp_agent_lane_index", str(ROOT / "bin/tp-agent")).load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def task(self, ident, status, **extra):
        path = self.base / "tasks" / ident
        path.mkdir(parents=True)
        (path / "meta.json").write_text(json.dumps({"id": ident, "role": "coder", "worker": "claude", **extra}))
        (path / "state.json").write_text(json.dumps({"status": status}))

    def test_lane_tasks_show_lane_and_step_and_plain_tasks_do_not(self):
        self.task("coder-1", "settled", lane="alpha", lane_step="s1")
        self.task("coder-2", "settled")
        rows = {row["id"]: row for row in self.agent.all_records(self.base)}
        self.assertEqual((rows["coder-1"]["lane"], rows["coder-1"]["lane_step"]), ("alpha", "s1"))
        self.assertNotIn("lane", rows["coder-2"])
        self.assertNotIn("lane_step", rows["coder-2"])

    def test_queued_lane_item_shows_lane(self):
        queue = self.base / "queue"
        queue.mkdir()
        item = {"id": "coder-q", "role": "coder", "worker": "claude", "model": "m", "thinking": "high", "task": "t", "workdir": "/tmp",
                "tests": "full", "route_info": {}, "created": "2026-01-01T00:00:00Z", "deliverable": "", "parent": "",
                "after": [], "context": [], "notes": [], "lane": "alpha", "lane_step": "s2", "preamble_from": "/tmp"}
        (queue / "coder-q.json").write_text(json.dumps(item))
        row = [r for r in self.agent.all_records(self.base) if r["id"] == "coder-q"][0]
        self.assertEqual((row["status"], row["lane"], row["lane_step"]), ("queued", "alpha", "s2"))

    def test_index_rows_and_lanes_block(self):
        self.task("coder-1", "settled", lane="alpha", lane_step="s1")
        self.task("coder-2", "settled", lane="alpha", lane_step="s2")
        self.task("coder-3", "settled", lane="beta", lane_step="s1")
        self.task("coder-4", "settled")
        value = self.agent.write_index(self.base)
        rows = {row["id"]: row for row in value["recent"]}
        self.assertEqual(rows["coder-2"]["lane_step"], "s2")
        self.assertNotIn("lane", rows["coder-4"])
        self.assertEqual(value["lanes"]["alpha"]["tasks"], 2)
        self.assertEqual(value["lanes"]["alpha"]["steps"], ["s1", "s2"])
        self.assertEqual(sum(value["lanes"]["beta"]["by_status"].values()), 1)
        self.assertEqual(json.loads((self.base / "session-index.json").read_text())["lanes"], value["lanes"])

    def test_no_lane_tasks_leave_the_index_unchanged(self):
        self.task("coder-1", "settled")
        value = self.agent.write_index(self.base)
        self.assertNotIn("lanes", value)
        self.assertNotIn("lane", value["recent"][0])


if __name__ == "__main__":
    unittest.main()
