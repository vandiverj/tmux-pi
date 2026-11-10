import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""PD-F: tools/smoke/product_design_demo.py, the plan's section 13 round, run end to end as a user runs it (one subprocess;
it runs bin/tp-product, bin/tp-agent and the headless-Chrome driver itself). Each step's outcome is read back from what the
round left in its output folder (the package, the bundles, the drafts, the packets, the session state), not only from the
demo's own checks. The browser steps are skipped, with the reason, when Node or Chrome is missing; a second run with
--no-browser checks that the rest of the round stands without them.

Slow by design (about 4 s and some 45 child processes for the full round): reported under SLOW_TESTS."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "tools/smoke/product_design_demo.py"
QUEUE, REASSIGN = "slice.submit-and-queue", "slice.reassignment"
CHROME = os.environ.get("TP_CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def browser_skip_reason():
    if not shutil.which("node"):
        return "node is not on PATH"
    if not os.access(CHROME, os.X_OK):
        return f"Chrome is not executable at {CHROME}"
    return None


def tree_state():
    """The worktree as git sees it, ignored files included: the demo must change none of it."""
    r = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain=v1", "--untracked-files=all", "--ignored"],
                       capture_output=True, text=True, timeout=30)
    return r.stdout


def run_demo(out, *extra):
    return subprocess.run([sys.executable, str(DEMO), "--out", str(out), *extra], capture_output=True, text=True,
                          timeout=300, env=session_env.outside_session(), stdin=subprocess.DEVNULL)


class DemoRound(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pd-f.")).resolve()
        cls.out = cls.tmp / "out"
        cls.before = tree_state()
        cls.r = run_demo(cls.out)
        cls.after = tree_state()
        cls.summary = json.loads((cls.out / "demo.json").read_text())
        cls.pkg = cls.out / "package"
        cls.browser = browser_skip_reason()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def checks(self, step):
        return [c for c in self.summary["checks"] if c[0] == step]

    def step_passed(self, step, at_least):
        got = self.checks(step)
        self.assertGreaterEqual(len(got), at_least, f"step {step}: {got}")
        self.assertEqual([c[1] for c in got if not c[2]], [], f"step {step} failed")
        self.assertIn(f"\nstep {step}: ", self.r.stdout)

    def needs_browser(self):
        if self.browser:
            self.assertIn(self.browser, self.summary["skipped"].get("1", ""))
            self.skipTest(f"browser steps skipped by the demo: {self.browser}")

    def status(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/tp-product"), "status", str(self.pkg), "--json"],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)

    def design(self, kind, rid):
        return next(r for r in json.loads((self.pkg / "design.json").read_text())[kind] if r["id"] == rid)

    def test_0_the_round_passes_writes_only_its_output_folder_and_prints_each_step(self):
        self.assertEqual(self.r.returncode, 0, self.r.stdout[-3000:] + self.r.stderr[-2000:])
        self.assertEqual(self.summary["failed"], 0)
        self.assertEqual(self.before, self.after, "the demo changed the worktree")
        for step in range(1, 10):
            self.assertRegex(self.r.stdout, rf"\nstep {step}: ")
        self.assertRegex(self.r.stdout, r"check: .*: ok")
        self.assertTrue(all(str(self.out) in v or not isinstance(v, str) for v in self.summary["files"].values()))

    def test_1_to_4_the_reviewers_use_the_page(self):
        self.needs_browser()
        for step, n in ((1, 6), (2, 3), (3, 5), (4, 4)):
            self.step_passed(step, n)
        screens = self.summary["files"]["screens"]
        self.assertTrue(all(Path(f).stat().st_size > 0 for f in screens.values()), screens)

    def test_5_one_copy_of_each_note_after_importing_one_bundle_twice(self):
        self.needs_browser()
        self.step_passed(5, 8)
        bundles = self.summary["files"]["bundles"]
        a = json.loads(Path(bundles["reviewer-a"]).read_text())
        b = json.loads(Path(bundles["reviewer-b"]).read_text())
        self.assertEqual((a["notes"][0]["category"], b["notes"][0]["category"]), ("confusing", "question"))
        self.assertEqual((a["notes"][0]["target"]["id"], b["notes"][0]["target"]["id"]), ("queue.row.s-1", "record.reassign"))
        ids = [n["id"] for n in json.loads((self.pkg / "reviews.json").read_text())["notes"]]
        self.assertEqual((ids.count(a["notes"][0]["id"]), ids.count(b["notes"][0]["id"])), (1, 1))
        imports = [c for c in self.summary["commands"] if c[1][2] == "import-feedback"]
        self.assertEqual([Path(c[1][4]).name for c in imports], ["reviewer-a.json", "reviewer-a.json", "reviewer-b.json"])
        history = sorted((self.pkg / ".tp-product/history").glob("*.json"))
        imported = [json.loads(h.read_text()) for h in history if "import-feedback" in h.read_text()]
        self.assertEqual(len(imported), 2, "the second import of A committed a revision")

    def test_6_a_local_decision_and_the_revised_rules(self):
        self.step_passed(6, 5)
        dec = self.design("decisions", "decision.reassignment-keeps-draft")
        self.assertEqual((dec["status"], dec["acceptance"]["origin"]), ("accepted", "local"))
        self.assertEqual(self.design("questions", "question.reassignment-during-review")["decision"], dec["id"])
        rule = self.design("rules", "rule.reassignment-in-review")
        self.assertEqual((rule["status"], rule["decisions"], len(rule["examples"])), ("accepted", [dec["id"]], 1))
        self.assertIn("Oldest first", self.design("rules", "rule.queue-order")["statement"])
        inp = json.loads((self.pkg / "reviews.json").read_text())["inputs"][0]
        self.assertEqual((inp["status"], inp["reconciliation"]["decision"]), ("reconciled", dec["id"]))

    def test_7_the_affected_slice_went_stale_and_the_unrelated_one_stayed_ready(self):
        self.step_passed(7, 5)
        refused = next(c for c in self.summary["commands"] if c[0] == 7 and c[1][2] == "cases")
        self.assertEqual(refused[2], 1)
        self.assertFalse((self.out / "cases/refused").exists())
        early = json.loads((self.out / "cases/reassignment-while-queue-stale/cases.json").read_text())
        self.assertEqual(early["cases"][0]["references"][QUEUE]["state"], "stale")
        history = [json.loads(h.read_text()) for h in sorted((self.pkg / ".tp-product/history").glob("*.json"))]
        stale = {e["summary"]: e["impact"]["stale"] for e in history if e.get("impact") and e["impact"]["changed"]}
        self.assertEqual(stale["Owner decision on reassignment during a review"], [])   # nothing was accepted there yet
        self.assertEqual(stale["Show the queue order (reviewer A's note)"], [QUEUE])     # the unrelated slice is not in it

    def test_8_re_review_case_drafts_and_bounded_packets(self):
        self.step_passed(8, 6)
        st = self.status()
        self.assertEqual({s: st["slices"][s]["effective"] for s in (QUEUE, REASSIGN)},
                         {QUEUE: "ready-for-case-preparation", REASSIGN: "ready-for-case-preparation"})
        for name, case in (("submit-and-queue", "case.reject-missing-field"), ("reassignment", "case.reassign-keeps-draft")):
            data = json.loads((self.out / "cases" / name / "cases.json").read_text())
            self.assertEqual([c["key"] for c in data["cases"]], [case])
            self.assertEqual((data["package_revision"], data["cases"][0]["proposal"]["action"]), (st["revision"] - 1, "create"))
            self.assertTrue((self.out / "cases" / name / f"{case}.md").read_text().startswith("#"))
        packet = json.loads((self.out / "packets/case.reassign-keeps-draft.json").read_text())
        self.assertEqual(packet["provenance"]["slice"], REASSIGN)
        ids = {r["id"] for recs in packet["scope"].values() for r in recs}
        self.assertNotIn("rule.queue-order", ids)
        link = next(l for l in packet["links"] if l["id"] == QUEUE)
        self.assertEqual((link["relation"], link["state"]), ("delivery-dependency", "ready-for-case-preparation"))
        md = (self.out / "packets/case.reassign-keeps-draft.md").read_text()
        self.assertIn("decision.reassignment-keeps-draft", md)

    def test_9_a_fresh_process_recovers_the_package_and_next_action(self):
        self.step_passed(9, 4)
        objective = json.loads((self.out / "state" / "session-index.json").read_text())["objective"]
        m = re.fullmatch(r"product-design package=(\S+) revision=(\d+) slice=(\S+) next: (.+)", objective.strip())
        self.assertIsNotNone(m, objective)
        st = self.status()
        self.assertEqual((Path(m[1]), int(m[2]), m[4]), (self.pkg, st["revision"], st["next_action"]["text"]))
        other = self.out / "state-other-session" / "session-index.json"
        self.assertFalse(other.exists() and json.loads(other.read_text()).get("objective"))


class DemoWithoutBrowser(unittest.TestCase):
    def test_the_browser_steps_skip_with_a_reason_and_the_rest_of_the_round_stands(self):
        with tempfile.TemporaryDirectory(prefix="pd-f.") as tmp:
            r = run_demo(Path(tmp) / "out", "--no-browser")
            self.assertEqual(r.returncode, 0, r.stdout[-3000:] + r.stderr[-2000:])
            summary = json.loads((Path(tmp) / "out/demo.json").read_text())
            self.assertEqual(set(summary["skipped"]), {"1", "2", "3", "4", "5"})
            self.assertIn("--no-browser was given", summary["skipped"]["1"])
            self.assertIn("skipped: no exported bundle", r.stdout)
            self.assertEqual(sorted({c[0] for c in summary["checks"]}), [6, 7, 8, 9])
            self.assertEqual(summary["failed"], 0)

    def test_the_output_folder_must_be_new_or_empty_and_outside_the_repository(self):
        with tempfile.TemporaryDirectory(prefix="pd-f.") as tmp:
            (Path(tmp) / "x").write_text("keep")
            for out in (tmp, str(ROOT / "tmp-demo-out")):
                r = run_demo(out)
                self.assertEqual(r.returncode, 2, r.stderr)
            self.assertEqual((Path(tmp) / "x").read_text(), "keep")
            self.assertFalse((ROOT / "tmp-demo-out").exists())


if __name__ == "__main__":
    unittest.main()
