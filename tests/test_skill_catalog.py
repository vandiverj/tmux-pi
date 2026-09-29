import hashlib
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(request, mode="select"):
    p = subprocess.run([str(ROOT / "bin/tp-skills"), "--mode", mode],
                       input=request, text=True, capture_output=True, check=True)
    return json.loads(p.stdout)


class SkillCatalogTests(unittest.TestCase):
    def test_no_normal_turn_inventory(self):
        self.assertEqual(run("Fix a TypeScript validation bug"), [])
        self.assertEqual(run("Summarize the QA team meeting"), [])
        self.assertEqual(run("list all skills and capabilities"), [])
        launcher = (ROOT / "bin/tp-pi").read_text()
        self.assertNotIn('--skill "$ROOT/skills"', launcher)

    def test_qa_command_and_natural_language(self):
        for request in ("/make-qa test person search", "Write a staging test script for person search"):
            self.assertEqual([s["id"] for s in run(request)], ["make-qa"])

    def test_document_directions_are_selected_only_when_relevant(self):
        for request in ("Read report.pdf", "Convert report.docx to Markdown",
                        "Export Markdown to PDF", "Generate a Word document from Markdown",
                        "Extract from PDF and write report.docx"):
            self.assertEqual([s["id"] for s in run(request)], ["document-transform"], request)

    def test_worker_packet_contains_only_selected_body_with_hash(self):
        self.assertEqual(run("Fix a TypeScript bug", "packet"), {"selected": [], "packet": ""})
        item = run("convert report.docx to Markdown", "packet")
        self.assertEqual(len(item["selected"]), 1)
        skill = item["selected"][0]
        self.assertEqual(skill["id"], "document-transform")
        self.assertEqual(skill["sha256"], hashlib.sha256(Path(skill["path"]).read_bytes()).hexdigest())
        self.assertIn("not** an authorization", item["packet"])
        self.assertNotIn("Tester-owned setup", item["packet"])
        self.assertIn("experimental", skill["status"])

    def test_catalogue_paths_confined_to_repo(self):
        for skill in json.loads((ROOT / "config/skill-catalog.json").read_text())["skills"]:
            self.assertTrue((ROOT / skill["path"]).resolve().is_relative_to(ROOT))


if __name__ == "__main__":
    unittest.main()
