#!/usr/bin/env python3
"""Product design demonstration (plan section 13): one review round on the synthetic review-queue fixture, through the
real commands only, each step's outcome checked by code.

    python3 tools/smoke/product_design_demo.py [--out DIR] [--no-browser]

  1  A requester submits a record; the reviewer sees it on the queue page and the summary page.        (browser)
  2  A guided review of the missing-field path shows the proposed request and its consequence.        (browser)
  3  An unguided reviewer resolves the record without click hints.                                     (browser)
  4  Reviewer A notes a confusing choice; reviewer B asks what happens when the assignment changes.    (browser)
  5  Both export; tp-product import-feedback takes A twice (one copy of each note) and B once.
  6  Main records a decision and revises the rule it settles; a human review accepts that slice;
     Main revises the queue rule from A's note.
  7  The accepted slice the queue rule belongs to is stale; the unrelated slice stays ready, and its case is
     exported with the stale delivery dependency shown.
  8  A re-review accepts the updated behaviour; case drafts and bounded worker packets are generated.
  9  Main records its next action and the session objective (tp-agent objective, a scratch state dir); a fresh
     process recovers the package path and the next action, and another session resumes from the path alone.

Commands run: bin/tp-product (validate, status, build, import-feedback, apply, impact, cases, packet) and, for step 9,
bin/tp-agent (objective, index) with TP_STATE_DIR, TP_WORK_DIR, TP_WORK_CONFIG and TP_WORKDIR inside the output folder.
Browser steps run tools/smoke/product_design_demo_browser.mjs (the PD-C headless-Chrome driver, a fresh
--user-data-dir). They are skipped, with the reason printed, when Node or Chrome (TP_CHROME) is missing or with
--no-browser; step 5's import is then skipped too (it needs the exported bundles) and the rest runs.

Everything is written under the output folder (default: a new temporary folder): package/ (a copy of the fixture),
browser/ (profile, feedback/*.json, screens/*.png), changesets/, cases/, packets/, state/ and demo.json (every check,
with the step, the commands and their seconds). Nothing in the repository changes (children run with
PYTHONDONTWRITEBYTECODE). No model, no tmux, no network. Python 3.9, standard library only.

Exit status: 0 every check passed (skipped steps are not checks); 1 a check failed (stderr names it); 2 usage.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = ROOT / "templates/product-design/examples/review-queue"
BROWSER = ROOT / "tools/smoke/product_design_demo_browser.mjs"
CASE_SHAPE = ROOT / "catalog/skills/prepare-cases/references/case-shape.md"
CHROME_DEFAULT = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# The caller's session (tests/session_env.py SESSION_KEYS) and state: never passed to a child.
DROP = ("TMUX", "TMUX_PANE", "TP_TASK_ID", "TP_TASK_DIR", "TP_MAIN_ID", "TP_MAIN_CWD", "TP_TRIAL_ID", "CLAUDE_CODE_SESSION_ID",
        "TP_MODE", "TP_SESSION", "TP_REDACTION_LAUNCH", "TP_REDACTION_SESSION", "TP_STATE_DIR", "TP_WORK_DIR",
        "TP_WORK_CONFIG", "TP_WORKDIR")
OBJECTIVE = re.compile(r"product-design package=(?P<package>\S+) revision=(?P<revision>\d+) slice=(?P<slice>\S+) next: (?P<next>.+)")
STEPS = {1: "submit, and see the record on another page", 2: "guided review of the missing field",
         3: "unguided review without click hints", 4: "notes from two reviewers",
         5: "export twice, import one twice: one copy of each note", 6: "a decision and a revised rule",
         7: "the affected slice is stale, the unrelated slice stays ready", 8: "re-review, case drafts and bounded packets",
         9: "recovery in a fresh process"}
QUEUE, REASSIGN = "slice.submit-and-queue", "slice.reassignment"
QUESTION, DECISION = "question.reassignment-during-review", "decision.reassignment-keeps-draft"
OWNER, DATE = "Product owner (synthetic)", "2026-11-10"
REVIEW_CASE, REASSIGN_CASE = "case.reject-missing-field", "case.reassign-keeps-draft"
NEXT = "Publish case.reject-missing-field and case.reassign-keeps-draft by hand, then record each with record-publication."


class Failed(Exception):
    pass


class Demo:
    def __init__(self, out: Path, browser: bool):
        self.out = out
        self.pkg = out / "package"
        self.want_browser = browser
        self.checks = []      # [step, name, ok, detail]
        self.commands = []    # [step, argv (repository paths shortened), rc, seconds]
        self.skipped = {}     # step -> reason
        self.files = {}
        self.step = 0
        self.n = 0
        self.env = {k: v for k, v in os.environ.items() if k not in DROP}
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    # ------------------------------------------------------------------ plumbing
    def begin(self, step):
        self.step = step
        print(f"\nstep {step}: {STEPS[step]}", flush=True)

    def check(self, name, ok, detail=None):
        self.checks.append([self.step, name, bool(ok), detail])
        print(f"  check: {name}: {'ok' if ok else 'FAILED'}", flush=True)
        if not ok:
            print(f"    detail: {json.dumps(detail, ensure_ascii=False)[:2000]}", flush=True)
        return ok

    def skip(self, reason):
        self.skipped[self.step] = reason
        print(f"  skipped: {reason}", flush=True)

    def run(self, argv, env=None, timeout=120, show=True):
        shown = [str(a).replace(str(ROOT) + "/", "").replace(str(self.out), "OUT") for a in argv]
        if show:
            print("  $ " + " ".join(shown[1:] if shown[0] == sys.executable else shown), flush=True)
        t0 = time.monotonic()
        r = subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=timeout, cwd=str(self.out),
                           env=env or self.env, stdin=subprocess.DEVNULL)
        self.commands.append([self.step, shown, r.returncode, round(time.monotonic() - t0, 3)])
        return r

    def product(self, *argv, ok=True):
        r = self.run([sys.executable, ROOT / "bin/tp-product", *argv])
        if ok and r.returncode != 0:
            raise Failed(f"tp-product {argv[0]} exited {r.returncode}: {r.stdout[-1500:]}{r.stderr[-1500:]}")
        return r

    def product_json(self, *argv, ok=True):
        r = self.product(*argv, "--json", ok=ok)
        try:
            return r.returncode, json.loads(r.stdout)
        except ValueError:
            raise Failed(f"tp-product {argv[0]} printed no JSON (exit {r.returncode}): {r.stdout[-800:]}{r.stderr[-800:]}")

    def status(self):
        r = self.run([sys.executable, ROOT / "bin/tp-product", "status", self.pkg, "--json"], show=False)
        return json.loads(r.stdout)

    def apply(self, name, summary, *ops):
        """Write the changeset to OUT/changesets and commit it at the current revision; returns the apply report."""
        rev = self.status()["revision"]
        path = self.write_changeset(name, summary, list(ops))
        _, rep = self.product_json("apply", self.pkg, path, "--expected-revision", rev)
        return rep

    def reviews(self):
        return json.loads((self.pkg / "reviews.json").read_text())

    def human_review(self, rid, sid, finding):
        fp = self.status()["slices"][sid]["fingerprint"]
        return [{"op": "add", "collection": "reviews", "record": {
                    "id": rid, "scope": [sid], "fingerprint": fp, "reviewer": {"label": OWNER, "type": "human"},
                    "verdict": "accept", "findings": [finding], "evidence": [f"Walked {sid} in the latest review build."],
                    "date": DATE}},
                {"op": "set-readiness", "slice": sid, "state": "ready-for-case-preparation", "review": rid,
                 "evidence": [f"{rid}: the owner accepted the slice."], "by": OWNER, "date": DATE}]

    # ------------------------------------------------------------------ the round
    def setup(self):
        print(f"output: {self.out}")
        shutil.copytree(FIXTURE, self.pkg)
        self.product("validate", self.pkg)
        _, rep = self.product_json("build", self.pkg)
        self.files["build"] = str(self.pkg / rep["file"])
        print(f"  built {rep['file']} (revision {rep['revision']})")

    def browser_reason(self):
        if not self.want_browser:
            return "--no-browser was given"
        if not shutil.which("node"):
            return "node is not on PATH"
        chrome = os.environ.get("TP_CHROME") or CHROME_DEFAULT
        if not os.access(chrome, os.X_OK):
            return f"Chrome is not executable at {chrome} (set TP_CHROME)"
        return None

    def browser_steps(self):
        reason = self.browser_reason()
        if reason:
            for step in (1, 2, 3, 4):
                self.begin(step)
                self.skip(f"browser step: {reason}")
            return None
        work = self.out / "browser"
        work.mkdir()
        r = self.run(["node", BROWSER, self.files["build"], work], timeout=180, show=False)
        try:
            got = json.loads(r.stdout)
        except ValueError:
            raise Failed(f"the browser steps printed no result (exit {r.returncode}): {r.stdout[-800:]}{r.stderr[-1500:]}")
        for step in (1, 2, 3, 4):
            self.begin(step)
            if step == 1:
                print("  $ node tools/smoke/product_design_demo_browser.mjs OUT/package/builds/" + Path(self.files["build"]).name + " OUT/browser")
            for s, name, ok, detail in got["checks"]:
                if s == step:
                    self.check(name, ok, detail)
        self.files.update(screens=got["screens"], bundles=got["bundles"])
        self.browser_checks_step5 = [c for c in got["checks"] if c[0] == 5]
        if r.returncode != 0 and not got["failures"]:
            raise Failed(f"the browser steps exited {r.returncode}: {r.stderr[-1500:]}")
        return got

    def step5(self, got):
        self.begin(5)
        if not got:
            self.skip("no exported bundle: the browser steps were skipped")
            return None
        for _, name, ok, detail in self.browser_checks_step5:
            self.check(name, ok, detail)
        a, b = got["bundles"]["reviewer-a"], got["bundles"]["reviewer-b"]
        note_a = json.loads(Path(a).read_text())["notes"][0]["id"]
        note_b = json.loads(Path(b).read_text())["notes"][0]["id"]
        before = len(self.reviews()["notes"])
        rev0 = self.status()["revision"]
        _, first = self.product_json("import-feedback", self.pkg, a)
        self.check("importing reviewer A's bundle commits its note", first["committed"] and first["added"]["notes"] == [note_a]
                   and first["revision"] == rev0 + 1, first)
        _, again = self.product_json("import-feedback", self.pkg, a)
        self.check("importing it again commits nothing and names the note as already imported",
                   not again["committed"] and again["added"]["notes"] == [] and note_a in again["duplicates"]
                   and again["revision"] == rev0 + 1, again)
        _, other = self.product_json("import-feedback", self.pkg, b)
        self.check("reviewer B's bundle adds its note", other["committed"] and other["added"]["notes"] == [note_b], other)
        notes = self.reviews()["notes"]
        ids = [n["id"] for n in notes]
        self.check("exactly one copy of each note is stored", ids.count(note_a) == 1 and ids.count(note_b) == 1
                   and len(notes) == before + 2, ids)
        anchors = {n["id"]: n.get("anchor") for n in notes}
        self.check("both notes are anchored to targets of the current build", anchors[note_a] == "current"
                   and anchors[note_b] == "current", anchors)
        return note_a, note_b

    def step6(self, notes):
        self.begin(6)
        q_ops = [{"op": "add", "collection": "decisions", "record": {
                     "id": DECISION, "title": "Keep the draft on reassignment", "question": QUESTION,
                     "choice": "Keep the previous reviewer's draft notes and draft decision with the submission; the new "
                               "reviewer sees them.",
                     "rationale": "No review work is lost; the draft record is small.",
                     "evidence": ["Owner reply to reviewer B's question about reassignment (synthetic)."],
                     "responsible": OWNER, "date": DATE, "status": "proposed"}},
                 {"op": "set", "id": QUESTION, "fields": {"status": "answered", "decision": DECISION}},
                 {"op": "accept-decision", "id": DECISION, "by": OWNER, "date": DATE,
                  "evidence": ["Recorded locally by Main from the owner's reply (synthetic)."]},
                 {"op": "set", "id": "rule.reassignment-in-review", "fields": {
                     "status": "accepted", "decisions": [DECISION],
                     "statement": "When a submission is reassigned during a review, the new reviewer takes it over and "
                                  "sees the previous reviewer's draft notes and draft decision.",
                     "examples": [{"situation": "reviewer-1 has a draft note on S-1; S-1 is reassigned to reviewer-2.",
                                   "outcome": "reviewer-2 opens S-1 and sees reviewer-1's draft note."}]}},
                 {"op": "set", "id": "scenario.reassign-during-review", "fields": {
                     "expected": ["S-1 is assigned to reviewer-2.", "reviewer-1's draft notes stay with S-1."]}},
                 {"op": "reconcile-input", "id": "input.session-b.answer-1", "status": "reconciled", "by": OWNER,
                  "date": DATE, "outcome": f"Matches the owner's decision {DECISION}.", "decision": DECISION}]
        if notes:
            q_ops.append({"op": "set-disposition", "note": notes[1], "kind": "accept-change", "link": DECISION,
                          "rationale": "The owner decided what happens on reassignment.", "by": "Main", "date": DATE})
        rep = self.apply("decide-reassignment", "Owner decision on reassignment during a review", *q_ops)
        st = self.status()
        dec = next(d for d in json.loads((self.pkg / "design.json").read_text())["decisions"] if d["id"] == DECISION)
        self.check("the decision is accepted by a local event (not by the reviewer's answer)",
                   dec["status"] == "accepted" and dec["acceptance"]["origin"] == "local" and dec["acceptance"]["by"] == OWNER, dec)
        self.check(f"{REASSIGN} is no longer blocked: it waits for a human review",
                   st["slices"][REASSIGN]["effective"] == "ready-for-human-review", st["slices"][REASSIGN])
        self.check(f"{QUEUE} is untouched by this change", st["slices"][QUEUE]["effective"] == "ready-for-case-preparation"
                   and QUEUE not in json.dumps(rep.get("impact", {}).get("stale_slices", [])), rep.get("impact"))
        self.apply("accept-reassignment", f"Owner review of {REASSIGN}",
                   *self.human_review("review.reassignment-1", REASSIGN, "Reassignment keeps the draft, as decided."))
        st = self.status()
        self.check(f"a human review and the local acceptance make {REASSIGN} ready for case preparation",
                   st["slices"][REASSIGN]["effective"] == "ready-for-case-preparation", st["slices"][REASSIGN])
        r_ops = [{"op": "set", "id": "rule.queue-order", "fields": {
                     "statement": "The queue lists waiting submissions oldest first, and its header says so (Oldest first).",
                     "examples": [{"situation": "S-1 submitted at 09:00, S-2 at 09:05.",
                                   "outcome": "S-1 is listed above S-2 under the header 'Oldest first'."}]}}]
        if notes:
            r_ops.append({"op": "set-disposition", "note": notes[0], "kind": "accept-change", "link": "rule.queue-order",
                          "rationale": "The order was right but not visible; the rule now says the queue shows it.",
                          "by": "Main", "date": DATE})
        r = self.product("apply", self.pkg, self.write_changeset("revise-queue-order", "Show the queue order (reviewer A's note)",
                                                                  r_ops), "--expected-revision", self.status()["revision"])
        self.check(f"revising rule.queue-order reports {QUEUE} stale", f"{QUEUE} is now stale" in r.stdout, r.stdout)

    def write_changeset(self, name, summary, ops):
        self.n += 1
        path = self.out / "changesets" / f"{self.n:02d}-{name}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main",
                                    "summary": summary, "operations": ops}, indent=2) + "\n")
        return path

    def reassign_case(self, st):
        return {"id": REASSIGN_CASE, "title": "Reassigning during a review keeps the draft", "slice": REASSIGN,
                "actor": "actor.reviewer",
                "outcome": "The new reviewer sees the previous reviewer's draft notes and draft decision.",
                "dependencies": ["rule.reassignment-in-review", "scenario.reassign-during-review", DECISION, QUEUE],
                "acceptance_examples": [{"situation": "reviewer-1 has a draft note on S-1; S-1 is reassigned to reviewer-2.",
                                         "outcome": "reviewer-2 sees the draft note; S-1 is assigned to reviewer-2."}],
                "source": {"package_revision": st["revision"], "slice_fingerprint": st["slices"][REASSIGN]["fingerprint"]}}

    def step7(self):
        self.begin(7)
        st = self.status()
        self.check(f"{QUEUE}: its prior acceptance is stale", st["slices"][QUEUE]["effective"] == "stale", st["slices"][QUEUE])
        self.check(f"{REASSIGN} (the rule change does not reach it) stays ready for case preparation",
                   st["slices"][REASSIGN]["effective"] == "ready-for-case-preparation", st["slices"][REASSIGN])
        _, imp = self.product_json("impact", self.pkg)
        self.check("impact lists only the stale slice, and the review that accepted its old content",
                   list(imp["slices"]) == [QUEUE] and imp["slices"][QUEUE]["effective"] == "stale"
                   and [(r["review"], r["accepts_slice"]) for r in imp["reviews"]] == [("review.submit-and-queue-1", True)], imp)
        rc, body = self.product_json("cases", self.pkg, "--slice", QUEUE, "--out", self.out / "cases" / "refused", ok=False)
        self.check(f"cases for {QUEUE} is refused by name: stale, it needs a new human review", rc == 1
                   and body["code"] == "slice.blocked" and body["details"]["effective"] == "stale"
                   and [b["code"] for b in body["details"]["blockers"]] == ["review.stale"]
                   and not (self.out / "cases" / "refused").exists(), body)
        self.apply("prepare-reassignment-case", f"Case draft for {REASSIGN}",
                   {"op": "add", "collection": "cases", "record": self.reassign_case(st)})
        out = self.out / "cases" / "reassignment-while-queue-stale"
        self.product("cases", self.pkg, "--slice", REASSIGN, "--out", out)
        entry = json.loads((out / "cases.json").read_text())["cases"][0]
        md = (out / f"{REASSIGN_CASE}.md").read_text()
        self.check(f"the ready slice's case exports; its draft shows the delivery dependency {QUEUE} as stale",
                   entry["references"][QUEUE]["state"] == "stale"
                   and re.search(r"delivery dependency.*state now: stale", md.replace("\\", "")) is not None,
                   entry["references"][QUEUE])

    def step8(self):
        self.begin(8)
        self.apply("re-review-queue", f"Owner re-review of {QUEUE}",
                   *self.human_review("review.submit-and-queue-2", QUEUE, "The queue says it lists oldest first."))
        st = self.status()
        self.check(f"the re-review accepts {QUEUE} again", st["slices"][QUEUE]["effective"] == "ready-for-case-preparation",
                   st["slices"][QUEUE])
        block = re.search(r"```json\n(.*?)```", CASE_SHAPE.read_text(), re.S).group(1)
        case = json.loads(block)["operations"][0]["record"]   # the prepare-cases reference's worked case, prepared now
        case["source"] = {"package_revision": st["revision"], "slice_fingerprint": st["slices"][QUEUE]["fingerprint"]}
        self.apply("prepare-queue-case", f"Case draft for {QUEUE}", {"op": "add", "collection": "cases", "record": case})
        st = self.status()
        drafts = {}
        for sid, name in ((QUEUE, "submit-and-queue"), (REASSIGN, "reassignment")):
            out = self.out / "cases" / name
            self.product("cases", self.pkg, "--slice", sid, "--out", out)
            drafts[sid] = (out, json.loads((out / "cases.json").read_text()))
        out, data = drafts[QUEUE]
        md = (out / f"{REVIEW_CASE}.md").read_text()
        self.check(f"{QUEUE} exports its case draft with provenance and examples",
                   [c["key"] for c in data["cases"]] == [REVIEW_CASE] and data["package_revision"] == st["revision"]
                   and data["slice_fingerprint"] == st["slices"][QUEUE]["fingerprint"]
                   and "Category is required" in md and data["cases"][0]["proposal"]["action"] == "create",
                   {k: data[k] for k in ("package_revision", "slice_fingerprint")})
        out, data = drafts[REASSIGN]
        self.check(f"{REASSIGN}'s draft now shows {QUEUE} ready, and proposes a new publication",
                   data["cases"][0]["references"][QUEUE]["state"] == "ready-for-case-preparation", data["cases"][0]["references"][QUEUE])
        self.files["cases"] = {sid: str(o) for sid, (o, _) in drafts.items()}
        packets = self.out / "packets"
        packets.mkdir()
        for cid in (REVIEW_CASE, REASSIGN_CASE):
            self.product("packet", self.pkg, "--case", cid, "--out", packets / f"{cid}.json")
            self.product("packet", self.pkg, "--case", cid, "--out", packets / f"{cid}.md")
        pk = json.loads((packets / f"{REASSIGN_CASE}.json").read_text())
        scope = {r["id"] for kind in ("scenarios", "rules", "decisions", "contracts") for r in pk["scope"][kind]}
        links = {l["id"]: l for l in pk["links"]}
        self.check("the reassignment packet is bounded: its own rule, scenario and decision, none of the other slice's records",
                   {"rule.reassignment-in-review", "scenario.reassign-during-review", DECISION} <= scope
                   and not scope & {"rule.queue-order", "rule.required-fields", "decision.missing-field-rejects"}, sorted(scope))
        self.check(f"it links {QUEUE} as a delivery dependency with its state",
                   links.get(QUEUE, {}).get("relation") == "delivery-dependency"
                   and links[QUEUE].get("state") == "ready-for-case-preparation", links.get(QUEUE))
        self.check("the Markdown packets are written", all((packets / f"{c}.md").stat().st_size > 0 for c in (REVIEW_CASE, REASSIGN_CASE)))
        self.files["packets"] = str(packets)

    def step9(self):
        self.begin(9)
        self.apply("pause", "Pause after case preparation",
                   {"op": "set-manifest", "fields": {"activity": "prepare-cases",
                                                     "next_action": {"text": NEXT, "owner": "Main", "set": DATE}}})
        st = self.status()
        line = f"product-design package={self.pkg} revision={st['revision']} slice={QUEUE} next: {st['next_action']['text']}"
        (self.out / "work-config.json").write_text("{}")
        env = dict(self.env, TP_WORK_DIR=str(self.out / "work"), TP_WORK_CONFIG=str(self.out / "work-config.json"),
                   TP_WORKDIR=str(self.out))

        def agent(state, *argv):
            state.mkdir(exist_ok=True)
            r = self.run([sys.executable, ROOT / "bin/tp-agent", *argv], env=dict(env, TP_STATE_DIR=str(state)))
            if r.returncode != 0:
                raise Failed(f"tp-agent {argv[0]} exited {r.returncode}: {r.stdout[-800:]}{r.stderr[-800:]}")
            return r.stdout
        agent(self.out / "state", "objective", line)
        text = agent(self.out / "state", "index", "--no-write", "--text")   # a fresh process: what a new main reads first
        seen = next((l[len("objective: "):] for l in text.splitlines() if l.startswith("objective: ")), "")
        got = OBJECTIVE.fullmatch(seen)
        self.check("a fresh process reads the package path, revision and slice from the session objective",
                   got is not None and Path(got["package"]) == self.pkg and int(got["revision"]) == st["revision"]
                   and got["slice"] == QUEUE, seen[:300])
        live = self.product_json("status", got["package"] if got else self.pkg)[1]
        self.check("tp-product status on that path gives the saved next action", live["next_action"]["text"] == NEXT
                   and live["activity"] == "prepare-cases" and live["trusted"] and (got and got["next"] == NEXT), live["next_action"])
        r = self.product("status", self.pkg)
        self.check("the plain status names the next action", NEXT in " ".join(r.stdout.split()), r.stdout[-600:])
        text = agent(self.out / "state-other-session", "index", "--no-write", "--text")
        other = next((l for l in text.splitlines() if l.startswith("objective: ")), "")
        self.check("another session has no objective and resumes from the package path alone",
                   other == "objective: (none set)" and self.status()["next_action"]["text"] == NEXT, other)

    def summary(self):
        failed = [c for c in self.checks if not c[2]]
        doc = {"output": str(self.out), "package": str(self.pkg), "checks": self.checks, "skipped": self.skipped,
               "commands": self.commands, "files": self.files, "failed": len(failed)}
        (self.out / "demo.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
        print(f"\n{len(self.checks) - len(failed)} of {len(self.checks)} checks passed; "
              f"{len(self.skipped)} step(s) skipped; {len(self.commands)} commands; summary {self.out / 'demo.json'}")
        for c in failed:
            print(f"FAILED step {c[0]}: {c[1]}", file=sys.stderr)
        return 1 if failed else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="the output folder: new, or empty (default: a new temporary folder)")
    ap.add_argument("--no-browser", action="store_true", help="skip the browser steps (1 to 4) and the import (5)")
    args = ap.parse_args(argv)
    if args.out:
        out = Path(args.out).resolve()
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            ap.error(f"{out} exists and is not an empty folder")
        if out == ROOT or ROOT in out.parents:
            ap.error("the output folder must be outside the repository")
        out.mkdir(parents=True, exist_ok=True)
    else:
        out = Path(tempfile.mkdtemp(prefix="pd-demo.")).resolve()
    demo = Demo(out, not args.no_browser)
    try:
        demo.setup()
        got = demo.browser_steps()
        notes = demo.step5(got)
        demo.step6(notes)
        demo.step7()
        demo.step8()
        demo.step9()
    except (Failed, subprocess.TimeoutExpired, OSError, KeyError, StopIteration, ValueError) as e:
        demo.check("the round ran to the end", False, f"{type(e).__name__}: {e}")
    return demo.summary()


if __name__ == "__main__":
    sys.exit(main())
