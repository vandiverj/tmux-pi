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
        self.assertEqual(run("Fix an auth bug in login tests"), [])
        self.assertEqual(run("Write a small plan to fix validation"), [])
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

    def test_focused_review_and_planning_guidance(self):
        cases = {
            "/counterpoint Challenge the plan to split this module": "counterpoint",
            "Play devil's advocate on the proposed architecture": "counterpoint",
            "/product-fit Review the person creation journey": "product-fit",
            "Do a product-fit check before implementation": "product-fit",
            "Review the API contract for this SDK": "product-fit",
            "/threat-model Review these new agent tool boundaries": "threat-model",
            "Conduct a privacy review of the proposed data export": "threat-model",
            "Test skill behavior before updating the selector": "test-skill-behavior",
        }
        for request, expected in cases.items():
            self.assertEqual([s["id"] for s in run(request)], [expected], request)

    def test_explicit_marker_tolerates_surrounding_punctuation(self):
        cases = {
            "/make-qa, test person search": "make-qa",
            "Please run /threat-model.": "threat-model",
            "(/threat-model) review the new tool boundary": "threat-model",
            "Run `/product-fit` on the signup journey": "product-fit",
            'Then "/counterpoint": is the split worth it?': "counterpoint",
            "/Threat-Model!": "threat-model",
        }
        for request, expected in cases.items():
            self.assertEqual([s["id"] for s in run(request)], [expected], request)

    def test_marker_embedded_in_path_or_longer_name_is_not_explicit(self):
        for request in ("See src/make-qa/notes for the old layout",
                        "Fetch http://example.test/threat-model and summarize it",
                        "Edit agent/prompts/threat-model.md wording",
                        "Rename /threat-model.md to something clearer",
                        "List /product-fit/drafts",
                        "Add a /counterpoint-v2 command stub",
                        "Compare and/counterpoint the two words"):
            self.assertEqual(run(request), [], request)

    def test_narrowed_phrases_reject_incidental_prose(self):
        for request in ("Fix the security review comments in the PR",
                        "Implement a security review checklist page",
                        "Review security headers middleware naming",
                        "Read the contract spec section of the docs",
                        "Rename the contract spec fixture file",
                        "Run an adversarial pass over the parser fuzz corpus",
                        "Add adversarial pass counts to the fuzz report",
                        "Rename the threat model fixture in the docs"):
            self.assertEqual(run(request), [], request)

    def test_narrowed_phrases_keep_deliberate_requests(self):
        cases = {
            "Write a threat model for the export endpoint": "threat-model",
            "Conduct a security review of the new MCP tool boundary": "threat-model",
            "Do a security threat review of the upload path": "threat-model",
            "Please create a threat model for the upload path": "threat-model",
            "Draft a contract spec for the person search endpoint": "product-fit",
            "Sketch the observable contract for this CLI flag": "product-fit",
            "Do an adversarial pass on this plan before we commit": "counterpoint",
            "Give me a counterpoint on the migration decision": "counterpoint",
        }
        for request, expected in cases.items():
            self.assertEqual([s["id"] for s in run(request)], [expected], request)

    def test_selection_never_exceeds_three_and_explicit_ranks_first(self):
        every = run("/make-qa, (/counterpoint) /product-fit. /threat-model: for report.pdf")
        self.assertEqual([s["id"] for s in every], ["make-qa", "counterpoint", "product-fit"])
        mixed = run("Read report.pdf, write a QA script, product-fit check, then /threat-model.")
        self.assertEqual(len(mixed), 3)
        self.assertEqual(mixed[0]["id"], "threat-model")

    def test_explicit_request_survives_bounded_multi_match(self):
        selection = run("Write a QA script for report.pdf, product-fit check, and /threat-model review")
        self.assertLessEqual(len(selection), 3)
        self.assertEqual(selection[0]["id"], "threat-model")
        self.assertIn("make-qa", [s["id"] for s in selection])
        self.assertNotIn("product-fit", [s["id"] for s in selection])

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
        review = run("/threat-model Examine a proposed agent MCP", "packet")
        self.assertEqual([s["id"] for s in review["selected"]], ["threat-model"])
        self.assertIn("not** a substitute for tmux-pi's required independent review", review["packet"])
        self.assertNotIn("PDF → DOCX", review["packet"])

    def test_catalogue_paths_confined_to_repo(self):
        for skill in json.loads((ROOT / "config/skill-catalog.json").read_text())["skills"]:
            self.assertTrue((ROOT / skill["path"]).resolve().is_relative_to(ROOT))


if __name__ == "__main__":
    unittest.main()
