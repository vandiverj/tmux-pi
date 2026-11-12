"""HB7a: tools/friction/report.py, the read-only friction report (docs/plans/det-prep/hub/hb7/hb7a-brief.md). In process; temp
work store, labels, memory and repo root only; no tmux, no child process. One table of rules, and the brief's planted case 1 with
every expected cell scored (a failure names the cell)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402

_spec = importlib.util.spec_from_file_location("friction_report", ROOT / "tools" / "friction" / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

SECRET = "PLANTEDQUOTE"  # every planted quote, why and memory body holds it; no output may


def line(ident, kind, by, quote, at, status, text="the coder ran the whole suite"):
    prov = {k: v for k, v in (("by", by), ("quote", quote), ("at", at)) if v}
    return f"- {at[:10]} {text} <!-- record: " + work.record_json(
        {"id": ident, "kind": kind, "scope": "role:coder", "provenance": prov, "status": status}) + " -->"


def plant(tmp):
    """Case 1 of the brief: a work store, a labels file, a memory dir, a repo root (docs and contract files) and a mapping."""
    tmp = Path(tmp)
    (tmp / "cfg.json").write_text("{}")
    env = {"TP_WORK_DIR": str(tmp / "store"), "TP_WORK_CONFIG": str(tmp / "cfg.json")}
    with mock.patch.dict(os.environ, env):
        doc = work.create("workspace", "Fr", wanted="fr")
    lines = [
        line("fr/R1", "correction", "owner", f"{SECRET} run only yours", "2026-11-01T10:00:00Z", "active"),   # alpha: first
        line("fr/R2", "correction", "main", None, "2026-11-03T00:00:00Z", "provisional"),                      # alpha: after
        line("fr/R3", "correction", "worker", None, "2026-11-04T00:00:00Z", "provisional"),                    # alpha: worker
        line("fr/R4", "correction", "owner", f"{SECRET} w", "2026-11-05T00:00:00Z", "withdrawn"),               # alpha: withdrawn
        line("fr/R5", "correction", "owner", f"{SECRET} s", "2026-11-06T00:00:00Z", "superseded"),              # beta: superseded
        line("fr/R6", "correction", "main", None, "2026-11-07T00:00:00Z", "provisional"),                      # unmapped, same text as R1
        line("fr/R7", "preference", "owner", f"{SECRET} p", "2026-11-07T00:00:00Z", "active"),                 # beta: not a correction
        line("fr/R8", "correction", "worker", f"{SECRET} wq", "2026-11-08T00:00:00Z", "provisional"),          # gamma: worker with quote
    ]
    open_entry = ("### fr/D1 · normal · open\nQuestion: which tests?\nRecord: "
                  + work.record_json({"kind": "correction", "scope": "role:coder"}) + "\n")
    doc.path.write_text(doc.path.read_text().rstrip("\n") + "\n\n## Decisions\n" + "\n".join(lines)
                        + "\n\n## Open decisions\n" + open_entry)
    rows = [
        {"i": 0, "sid": "s", "at": "2026-11-02T00:00:00Z", "label": "process", "quote": f"{SECRET} 0", "why": SECRET},  # alpha after
        {"i": 1, "sid": "s", "at": "2026-11-02T00:00:00Z", "label": "product", "quote": f"{SECRET} 1", "why": SECRET},  # beta: product
        {"i": 2, "sid": "s", "label": "process", "quote": f"{SECRET} 2", "why": SECRET},                                # alpha undated
        {"i": 3, "sid": "s", "at": "2026-11-09T00:00:00Z", "label": "process", "quote": f"{SECRET} 3", "why": SECRET,
         "process_kind": "verification-or-evals", "recurrence": True, "recurrence_of": 0, "proj": SECRET},               # unmapped
        {"i": 4, "sid": "s", "at": "2026-11-09T00:00:00Z", "label": "process", "quote": f"{SECRET} 4", "why": SECRET},  # two keys
        {"i": 6, "sid": "s", "at": "2026-11-01T09:00:00Z", "label": "process", "quote": f"{SECRET} 6", "why": SECRET},  # delta
        {"i": 7, "sid": "s", "at": "2026-11-10T00:00:00Z", "label": "process", "quote": "  ", "why": SECRET},           # no quote
        "not a row", {"i": "5", "label": "process"}, {"i": 0, "label": "process"},
        {"i": 8, "sid": "s", "at": "2026-11-08T00:00:00Z", "label": "process", "quote": f"{SECRET} 8"},                  # zeta: by worker
    ]
    (tmp / "labels-T.json").write_text(json.dumps(rows))
    mem = tmp / "memory"
    mem.mkdir()
    (mem / "fb-quote.md").write_text(f"---\nname: fb-quote\ndescription: {SECRET}\nmetadata:\n  type: feedback\n---\n"
                                     f'The owner said "{SECRET} stop it".\n**Why:** {SECRET}\n**How to apply:** x\n')
    (mem / "fb-noquote.md").write_text(f"---\nname: fb-noquote\ndescription: d\nmetadata:\n  type: feedback\n---\n{SECRET} paraphrase\n")
    (mem / "fb-cmd.md").write_text(f'---\nname: fb-cmd\nmetadata:\n  type: feedback\n---\nMy inference: prefer "--tests {SECRET}".\n')
    (mem / "fb-worker.md").write_text(f'---\nname: fb-worker\nmetadata:\n  type: feedback\n---\nThe worker said "{SECRET} only".\n')
    (mem / "project.md").write_text("---\nname: project\ndescription: d\nmetadata:\n  type: project\n---\nfact\n")
    (mem / "broken.md").write_text("---\nname broken\n---\nbody\n")
    (mem / "MEMORY.md").write_text("- [x](fb-quote.md) index\n")
    repo = tmp / "repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "ideas.md").write_text("# Ideas\n## H1: a rule (2026-10-30)\nx\n## H2 — same day (2026-11-01)\n## H3 no date\n")
    (repo / "tasks" / "w").mkdir(parents=True)
    (repo / "tasks" / "w" / "report.md").write_text("# R\n## R1 a worker finding\n")
    (repo / "claude").mkdir()
    (repo / "claude" / "main-prompt.md").write_text("# Main\n\n- a rule (record:fr/R1)\n- evidence: prose, not a pointer\n")
    (repo / "agent" / "contract").mkdir(parents=True)
    (repo / "agent" / "contract" / "worker.md").write_text("# W\nOne two.\n")
    (repo / "agent" / "roles").mkdir(parents=True)
    (repo / "agent" / "roles" / "coder.md").write_text("# Coder\nRun the affected tests.\n")
    mapping = {"schema": "tp-frictions:v1", "keys": {
        "alpha": {"by": "main", "trigger": "t", "correction": "c", "successful_action": "s", "scope": "role:coder",
                  "proposed_home": "contract line", "home_target": "agent/roles/coder.md", "why_home": "w", "worked_if": "k",
                  "sources": ["record:fr/R1", "record:fr/R2", "record:fr/R3", "record:fr/R4", "record:fr/D1", "label:labels-T.json#0",
                              "label:labels-T.json#2", "label:labels-T.json#4", "doc:docs/ideas.md#H1", "doc:docs/ideas.md#H2",
                              "doc:tasks/w/report.md#R1", "record:fr/R99", "label:labels-X.json#0"]},
        "beta": {"by": "main", "scope": "role:main", "proposed_home": "retrieved procedure",
                 "sources": ["record:fr/R5", "record:fr/R7", "label:labels-T.json#1", "memory:fb-quote.md", "memory:broken.md"]},
        "gamma": {"by": "main", "scope": "global", "proposed_home": "prompt",
                  "sources": ["record:fr/R8", {"ref": "doc:docs/ideas.md#H3", "by": "worker"}, "label:labels-T.json#4", "bogus"]},
        "epsilon": {"by": "main", "scope": "role:coder", "proposed_home": "adapter", "sources": ["memory:fb-cmd.md"]},
        "zeta": {"by": "main", "scope": "role:coder", "proposed_home": "adapter",
                 "sources": [{"ref": "memory:fb-worker.md", "by": "worker"}, {"ref": "label:labels-T.json#8", "by": "worker"}]},
        "delta": {"by": "main", "scope": "brief writing", "proposed_home": "adapter", "sources": ["label:labels-T.json#6",
                                                                                                    "label:labels-T.json#7"]}}}
    (tmp / "frictions.json").write_text(json.dumps(mapping))
    return {"map": str(tmp / "frictions.json"), "store": str(tmp / "store"), "labels": [str(tmp / "labels-T.json")],
            "memory": str(mem), "root": repo}


def excluded(key):
    return {e["pointer"]: e["excluded"] for e in key["evidence"] if e.get("excluded")}


def case1_cells(out):
    """(cell, expected, got) for every expected cell of case 1. Fail-before rows of the brief are marked in comments."""
    keys = {k["key"]: k for k in out["keys"]}
    a, b, g, d = keys["alpha"], keys["beta"], keys["gamma"], keys["delta"]
    e, z = keys["epsilon"], keys["zeta"]
    worker = "worker text: evidence only, never policy"
    return [
        ("alpha status", "active", a["status"]),
        ("alpha occurrences", {"record": {"dated": 2, "undated": 0}, "label": {"dated": 1, "undated": 1},
                               "doc": {"dated": 2, "undated": 0}}, a["occurrences"]),
        ("alpha first correction", "2026-11-01T10:00:00Z", a["recurrence"]["first_correction"]),
        # an undated item is never a recurrence; a date-only item on the first correction's day is not placed
        ("alpha recurrence", ({"record": 1, "label": 1}, 2, 2, 1, 1),
         tuple(a["recurrence"][k] for k in ("after", "total_after", "before_or_at", "same_day_unordered", "undated"))),
        # withdrawn, open and worker items are listed, never counted (fail-before: a naive count includes them)
        ("alpha excluded", {"record:fr/R3": worker, "record:fr/R4": "withdrawn", "record:fr/D1": "open question (not a record in force)",
                            "label:labels-T.json#4": "claimed by keys alpha, gamma", "doc:tasks/w/report.md#R1": worker},
         excluded(a)),
        ("alpha unresolved", {"record:fr/R99": "record not in the store", "label:labels-X.json#0": "labels file not given"},
         {u["pointer"]: u["problem"] for u in a["unresolved"]}),
        # a memory file is always Main's (whatever it quotes): it never raises a key; superseded and non-correction items do not count
        ("beta status", "candidate", b["status"]),
        ("beta occurrences", {"memory": {"dated": 0, "undated": 1}}, b["occurrences"]),
        ("beta recurrence", (None, "no dated owner correction with a quote", 1),
         (b["recurrence"]["total_after"], b["recurrence"]["reason"], b["recurrence"]["undated"])),
        ("beta excluded", {"record:fr/R5": "superseded", "record:fr/R7": "kind preference, not a correction",
                           "label:labels-T.json#1": "label product, not process"}, excluded(b)),
        ("beta unresolved", ["memory:broken.md"], [u["pointer"] for u in b["unresolved"]]),
        # a worker with a quote never raises status (fail-before: a naive "has a quote" rule makes it active)
        ("gamma status", "candidate", g["status"]),
        ("gamma home", None, g["proposed_home"]),
        ("gamma excluded", {"record:fr/R8": worker, "doc:docs/ideas.md#H3": worker, "label:labels-T.json#4": "claimed by keys alpha, gamma"},
         excluded(g)),
        # quote marks prove nothing: a quoted command and a quoted worker remark are main, the key stays candidate; a mapping
        # entry `by: worker` makes a memory or a label row worker text (fail-before: quote detection / mapping ignored)
        ("epsilon", ("candidate", ["main"]), (e["status"], [x["provenance"] for x in e["evidence"]])),
        ("zeta", ("candidate", {"memory:fb-worker.md": "worker", "label:labels-T.json#8": "worker"}, 0),
         (z["status"], {x["pointer"]: x["provenance"] for x in z["evidence"]}, len(z["occurrences"]))),
        ("recurrence sessions", (1, 0), (a["recurrence"]["recurrence_sessions"], a["recurrence"]["after_without_sid"])),
        ("delta status", ("candidate", "scope not stated", None), (d["status"], d["status_reason"], d["scope"])),
        ("delta provenance", ["owner+quote", "owner without quote"], [e["provenance"] for e in d["evidence"]]),
        # grouping is by key only: R6 has R1's text and stays ungrouped (fail-before: text similarity joins it to alpha)
        ("ungrouped record", ["record:fr/R6"], [r["pointer"] for r in out["ungrouped"]["record"]]),
        ("ungrouped label", [("label:labels-T.json#3", "verification-or-evals", "label:labels-T.json#0", "recurrence_of is in key alpha")],
         [(r["pointer"], r["process_kind"], r.get("recurrence_of"), r.get("hint")) for r in out["ungrouped"]["label"]]),
        ("ungrouped memory", [("memory:fb-noquote.md", "main")], [(r["pointer"], r["provenance"]) for r in out["ungrouped"]["memory"]]),
        ("conflicts", [{"pointer": "label:labels-T.json#4", "keys": ["alpha", "gamma"]}], out["conflicts"]),
        ("problems", {("label", "labels-T.json row 7"), ("label", "labels-T.json row 8"), ("label", "labels-T.json row 9"),
                      ("memory", "broken.md"), ("map", "gamma")}, {(p["source"], p["where"]) for p in out["problems"]}),
        ("inputs", ({"records": 9, "corrections": 8, "other_kinds": {"preference": 1}}, 8, 4, 1),
         ({k: out["inputs"]["records"][k] for k in ("records", "corrections", "other_kinds")}, out["inputs"]["labels"]["rows"],
          out["inputs"]["memory"]["feedback"], out["inputs"]["memory"]["other_types"].get("project"))),
        ("contract size", [("claude/main-prompt.md", 4, 3, 12, 1), ("agent/roles/coder.md", 2, 2, 6, 0), ("agent/contract/worker.md", 2, 2, 4, 0)],
         [(r["file"], r["lines"], r["nonblank"], r["words"], r["evidence_lines"]) for r in out["contract_size"]["files"]]),
        ("contract subtotals", ((8, 7, 22, 1), (6, 5, 18, 1)),
         tuple(tuple(out["contract_size"][k][f] for f in ("lines", "nonblank", "words", "evidence_lines")) for k in ("total", "brief_total"))),
    ]


class FrictionReport(unittest.TestCase):
    def test_rules_table(self):
        """One row per rule input: time parsing and placement, label/memory provenance, frontmatter, evidence pointers."""
        t = report.parse_time
        def sess(*pairs):
            """First correction in session pairs[0][0]; each pair is (first's sid, an item after it's sid)."""
            first = {"source": "label", "provenance": "owner+quote", "time": t("2026-11-01T00:00:00Z"), "sid": pairs[0][0]}
            return report.recurrence([first] + [{"source": "label", "provenance": "owner+quote", "time": t("2026-11-02T00:00:00Z"),
                                                 "sid": sid} for _, sid in pairs])

        rows = [
            ("time z", t("2026-11-08T08:39:04.081Z")[1], False),
            ("time date only", t("2026-11-08")[1], True),
            ("time missing", t(None), None), ("time empty", t(""), None), ("time junk", t("yesterday"), None),
            ("time wrong type", t(20261108), None),
            ("time naive is utc", report.iso(t("2026-11-08T08:00:00")), "2026-11-08T08:00:00Z"),
            ("time offset", report.iso(t("2026-11-08T08:00:00+02:00")), "2026-11-08T06:00:00Z"),
            ("after", report.place(t("2026-11-02T00:00:01Z"), t("2026-11-02T00:00:00Z")), "after"),
            ("equal", report.place(t("2026-11-02T00:00:00Z"), t("2026-11-02T00:00:00Z")), "before_or_at"),
            ("date same day", report.place(t("2026-11-02"), t("2026-11-02T10:00:00Z")), "same_day_unordered"),
            ("date later day", report.place(t("2026-11-03"), t("2026-11-02T23:00:00Z")), "after"),
            ("date earlier day vs date", report.place(t("2026-11-01"), t("2026-11-02")), "before_or_at"),
            ("frontmatter ok", report.frontmatter("---\nname: n\nmetadata:\n  type: feedback\n---\nb")[0],
             {"name": "n", "metadata": {"type": "feedback"}}),
            ("frontmatter unclosed", report.frontmatter("---\nname: n\n"), None),
            ("frontmatter orphan child", report.frontmatter("---\n  type: x\n---\n"), None),
            ("frontmatter none", report.frontmatter("name: n\n"), None),
            ("evidence pointer", bool(report.EVIDENCE_RE.search("rule (record:fr/R1)")), True),
            ("evidence record id", bool(report.EVIDENCE_RE.search("see hub/D12")), True),
            ("evidence prose", bool(report.EVIDENCE_RE.search("a Self-review section with evidence: the writers")), False),
            ("evidence path", bool(report.EVIDENCE_RE.search("tests/test_x.py and agent/roles/coder.md")), False),
            # sessions: the first correction's own session and items without a sid are not counted; two sessions vs one
            ("sessions two", sess(("a", "a"), ("a", "b"), ("a", "b"), ("a", None))["recurrence_sessions"], 1),
            ("sessions one", sess(("a", "a"), ("a", "a"))["recurrence_sessions"], 0),
            ("sessions both new", sess(("a", "b"), ("a", "c"), ("a", "b"))["recurrence_sessions"], 2),
            ("sessions no sid apart", sess(("a", "b"), ("a", None))["after_without_sid"], 1),
            ("recurrence only worker-free owner", report.recurrence([])["reason"], "no dated owner correction with a quote"),
        ]
        for name, got, want in rows:
            with self.subTest(name):
                self.assertEqual(got, want)

    def test_case1_planted_cells_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = plant(tmp)
            before = sorted((str(f), f.stat().st_mtime_ns) for f in Path(tmp).rglob("*") if f.is_file())
            out = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"])
            for cell, want, got in case1_cells(out):
                with self.subTest(cell):
                    self.assertEqual(got, want)
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                code = report.main(["--map", p["map"], "--store", p["store"], "--labels", *p["labels"], "--memory", p["memory"]],
                                   root=p["root"])
            raw = io.StringIO()
            with contextlib.redirect_stdout(raw):
                report.main(["--map", p["map"], "--json"], root=p["root"])  # no store, labels or memory: listed, not a crash
            after = sorted((str(f), f.stat().st_mtime_ns) for f in Path(tmp).rglob("*") if f.is_file())
        self.assertEqual(code, 0)
        self.assertIn("## alpha  [active", text.getvalue())
        self.assertIn("recurrence after correction (2026-11-01T10:00:00Z): sessions 1 ", text.getvalue())
        self.assertNotIn(SECRET, text.getvalue() + json.dumps(out))  # no quote, why, body, description or project leaves the tool
        bare = json.loads(raw.getvalue())
        self.assertEqual((bare["inputs"]["records"]["given"], bare["inputs"]["memory"]["given"], bare["ungrouped"]["label"]),
                         (False, False, []))
        self.assertEqual(before, after)  # read-only

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every directory")
    def test_unreadable_inputs_are_listed(self):
        """An unreadable memory directory, doc directory and store: a listed problem each, every other source still reported."""
        with tempfile.TemporaryDirectory() as tmp:
            p = plant(tmp)
            locked = [Path(p["memory"]), p["root"] / "docs", Path(p["store"]) / "workspaces", p["root"] / "agent" / "roles"]
            try:
                for d in locked:
                    d.chmod(0)
                out = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"])
            finally:
                for d in locked:
                    d.chmod(0o755)
        got = {(x["source"]) for x in out["problems"]}
        alpha = next(k for k in out["keys"] if k["key"] == "alpha")
        self.assertIn("memory", got)
        self.assertIn("record", got)
        self.assertIn("contract", got)
        self.assertEqual(
            {u["pointer"] for u in alpha["unresolved"] if u["pointer"].startswith("doc:docs/")},
            {"doc:docs/ideas.md#H1", "doc:docs/ideas.md#H2"})
        self.assertEqual(alpha["occurrences"].get("label"), {"dated": 1, "undated": 1})  # labels and contract files still reported
        self.assertEqual(len(out["contract_size"]["files"]), 2)  # roles dir unreadable: listed, main prompt and worker contract still counted


if __name__ == "__main__":
    unittest.main()
