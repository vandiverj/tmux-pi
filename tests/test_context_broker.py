import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from context_broker import UNTRUSTED_INSTRUCTION, select_context  # noqa: E402


class ContextBrokerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = Path(d.name)
        (self.root / "src").mkdir()
        (self.root / "AGENTS.md").write_text("# Instructions\nUse pytest.\n")
        (self.root / "src" / "auth.py").write_text("def login():\n    return True\n")
        (self.root / "src" / "billing.py").write_text("def invoice():\n    return 'ok'\n")
        (self.root / ".env").write_text("TOKEN=secret\n")

    def test_manifest_records_repo_ticket_provenance_and_untrusted_instruction(self):
        out = select_context("Fix ticket: PROJ-12 in src/auth.py; do not use .env", self.root, preflight={"mode": "delegated", "artifact_tier": 1})
        manifest = out["manifest"]
        self.assertEqual(manifest["schema"], "tmux-pi-context-manifest:v1")
        self.assertIn(UNTRUSTED_INSTRUCTION, manifest["untrusted_content_instruction"])
        paths = [s.get("path") for s in manifest["sources"]]
        self.assertIn("AGENTS.md", paths)
        self.assertIn("src/auth.py", paths)
        self.assertNotIn(".env", out["contexts"])
        self.assertTrue(any(p.get("path") == ".env" and p.get("action") == "excluded" for p in manifest["protections"]))
        self.assertFalse([s for s in manifest["sources"] if s["source"] == "YouTrack"])
        self.assertIn("repository/filesystem search", manifest["selected_capabilities"])
        self.assertNotIn("YouTrack", manifest["selected_capabilities"])
        self.assertIn("capability_manifest", manifest)
        self.assertIn("youtrack", {f["id"] for f in manifest["capability_manifest"]["filtered"]})
        self.assertNotIn("YouTrack: authority", out["capability_note"])

    def test_filename_search_is_bounded_and_compact(self):
        big = self.root / "src" / "cache_policy.py"
        big.write_text("x = 1\n" * 5000)
        out = select_context("Review cache policy behavior", self.root)
        cache = [s for s in out["manifest"]["sources"] if s.get("path") == "src/cache_policy.py"][0]
        self.assertRegex(cache["range"], r"^1-\d+$")
        self.assertLessEqual(cache["content_size"], 16000)
        self.assertIn("src/cache_policy.py:" + cache["range"], out["contexts"])

    def test_profile_only_ticket_and_context_engine_remain_inactive(self):
        out = select_context("Use Context Engine background for ticket: PROJ-12 in src/auth.py", self.root)
        sources = [s["source"] for s in out["manifest"]["sources"]]
        self.assertNotIn("YouTrack", sources)
        self.assertNotIn("Context Engine", sources)
        filtered = {f["id"] for f in out["manifest"]["capability_manifest"]["filtered"]}
        self.assertIn("youtrack", filtered)
        self.assertIn("context_engine", filtered)

    def test_profile_only_db_microsoft_inactive_but_sequential_reference_remains(self):
        out = select_context(
            "Plan Azure SQL schema investigation with Context Engine background",
            self.root,
            preflight={"mode": "plan", "artifact_tier": 2},
        )
        by_source = {s["source"]: s for s in out["manifest"]["sources"]}
        self.assertNotIn("read-only DB", by_source)
        self.assertNotIn("official Microsoft docs", by_source)
        self.assertNotIn("Context Engine", by_source)
        self.assertIn("Sequential Thinking", by_source)
        filtered = {f["id"] for f in out["manifest"]["capability_manifest"]["filtered"]}
        self.assertIn("readonly_db", filtered)
        self.assertIn("official_docs", filtered)
        self.assertIn("context_engine", filtered)
        self.assertEqual(by_source["Sequential Thinking"]["trust"], "reasoning aid, not evidence")
        self.assertNotIn("read-only DB", "\n".join(out["contexts"]))


if __name__ == "__main__":
    unittest.main()
