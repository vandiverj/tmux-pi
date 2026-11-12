"""HB7a: tools/friction/report.py, the read-only friction report (docs/plans/det-prep/hub/hb7/hb7a-brief.md). In process; temp
work store, labels, memory and repo root only; no tmux, no child process. One table of rules, and the brief's planted case 1 with
every expected cell scored (a failure names the cell)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
import importlib.util
import io
import json
import os
import subprocess
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


KINDS = {"record": ("record:fr/R1", "owner+quote"), "label": ("label:labels-T.json#0", "owner+quote"),
         "memory": ("memory:fb-cmd.md", "main"), "doc": ("doc:docs/ideas.md#H1", "main")}


def attribution_cells(p):
    """Rule 1, both levels: every (key by) x (source by) x (source kind) -> (source provenance, key status). One build per
    (key by, source by); each build maps one key per source kind. The source's own provenance holds only when neither level says
    worker; only an owner+quote source can make a key active, and a worker key never is."""
    cells = []
    for key_by in ("main", "worker", None):
        for src_by in (None, "main", "worker"):
            keys = {}
            for kind, (ref, _) in KINDS.items():
                keys[kind] = {"scope": "role:coder", "proposed_home": "adapter", "sources": [ref if src_by is None else {"ref": ref, "by": src_by}]}
                if key_by:
                    keys[kind]["by"] = key_by
            path = Path(p["map"]).with_name("attr.json")
            path.write_text(json.dumps({"schema": "tp-frictions:v1", "keys": keys}))
            got = {k["key"]: k for k in report.build(str(path), p["store"], p["labels"], p["memory"], p["root"])["keys"]}
            for kind, (ref, own) in KINDS.items():
                worker = "worker" in (key_by, src_by)
                want_prov = "worker" if worker else own
                want_status = "active" if own == "owner+quote" and not worker else "candidate"
                cells.append((f"attribution key {key_by} source {src_by} {kind}", (want_prov, want_status),
                              (got[kind]["evidence"][0]["provenance"], got[kind]["status"])))
                if key_by == "worker":
                    cells.append((f"attribution reason key worker {src_by} {kind}",
                                  "key attributed to a worker (by worker): never active", got[kind]["status_reason"]))
    return cells


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


GIT = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@t"]
GIT_ENV = {k: v for k, v in os.environ.items() if k not in report.GIT_ENV_DROP}
LINES_COMMITTED = ["# Main", "", "## Section", "- dup line", "- dup line", "   - spaced rule  ", "```py", "# fenced comment line",
                   "```", "#nospace hashtag line", "- old wording here", "- no entry line", "- faulty target", "- dup target"]
# HB7b ledger rows: (row name, line, reasons, by, status, removal_reasons, category, removal listing or None).
H = "<HEAD12>"
LEDGER_ROWS = [
    ("record resolved owner", "- rec ok", ["record:fr/R1"], "main", "keep", None, "justified_owner", None),
    ("record resolved main", "- rec main", ["record:fr/R2"], "main", "keep", None, "justified_main", None),
    ("record unresolved", "- rec missing", ["record:fr/R99"], "main", "keep", None, "no_resolved_reason", None),
    ("label resolved owner", "- label ok", ["label:labels-T.json#0"], "main", "keep", None, "justified_owner", None),
    ("label without quote is main", "- label noquote", ["label:labels-T.json#7"], "main", "keep", None, "justified_main", None),
    ("label unresolved", "- label missing", ["label:labels-T.json#99"], "main", "keep", None, "no_resolved_reason", None),
    ("label file not given: unchecked", "- label other", ["label:labels-X.json#0"], "main", "keep", None, "no_resolved_reason", None),
    ("memory resolved", "- memory ok", ["memory:fb-cmd.md"], "main", "keep", None, "justified_main", None),
    ("memory unresolved", "- memory missing", ["memory:nope.md"], "main", "keep", None, "no_resolved_reason", None),
    ("doc resolved", "- doc ok", ["doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
    ("doc unresolved", "- doc missing", ["doc:docs/ideas.md#H9"], "main", "keep", None, "no_resolved_reason", None),
    ("doc worker artifact", "- doc worker", ["doc:tasks/w/report.md#R1"], "main", "keep", None, "worker_evidence_only", None),
    ("key resolved", "- key ok", ["key:alpha"], "main", "keep", None, "justified_main", None),
    ("key unresolved", "- key missing", ["key:nokey"], "main", "keep", None, "no_resolved_reason", None),
    ("key by worker", "- key worker", ["key:omega"], "main", "keep", None, "worker_evidence_only", None),
    ("commit alone: provenance only", "- commit only", [f"commit:{H}"], "main", "keep", None, "provenance_only", None),
    ("commit + another reason", "- commit doc", [f"commit:{H}", "doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
    ("commit unresolved", "- commit missing", ["commit:deadbeef0"], "main", "keep", None, "no_resolved_reason", None),
    ("malformed pointers", "- malformed", ["bogus", "commit:XYZ", "label:nohash", "key:Bad Key"], "main", "keep", None,
     "no_resolved_reason", None),
    ("by worker never justified", "- by worker", ["label:labels-T.json#0"], "worker", "keep", None, "worker_evidence_only", None),
    ("by owner alone is not owner-stated", "- by owner", ["memory:fb-cmd.md"], "owner", "keep", None, "justified_main", None),
    ("owner reason + worker reason: main", "- owner and worker", ["record:fr/R1", "doc:tasks/w/report.md#R1"], "main", "keep",
     None, "justified_main", None),
    ("remove without a removal reason", "- rm none", ["key:alpha"], "main", "remove-candidate", [], "justified_main",
     "removal without a reason"),
    ("remove with only commit", "- rm commit", ["key:alpha"], "main", "remove-candidate", [f"commit:{H}"], "justified_main",
     "removal without a reason"),
    ("remove with unresolved reason", "- rm unresolved", ["key:alpha"], "main", "remove-candidate", ["key:nokey"],
     "justified_main", "removal without a reason"),
    ("remove with a resolved reason", "- rm ok", ["key:alpha"], "main", "remove-candidate", ["key:alpha"], "justified_main",
     "removal with a reason"),
    # line side: whitespace-only change keeps the sha; a fenced `#` line and `#nospace` are prose; identical lines share one entry
    ("whitespace changed only", "- spaced rule", ["doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
    ("fenced line", "# fenced comment line", ["doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
    ("hash without space is prose", "#nospace hashtag line", ["doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
    ("duplicate identical lines", "- dup line", ["doc:docs/ideas.md#H1"], "main", "keep", None, "justified_main", None),
]
# the same rows without --store, --labels, --memory: these become unchecked (count for nothing)
UNCHECKED = {"record resolved owner": "no --store given", "label resolved owner": "labels file not given (or not readable)",
             "memory resolved": "no --memory given"}
GOOD = {"file": "claude/main-prompt.md", "prefix": "p", "reasons": [], "by": "main", "status": "keep"}
# ledger-side faults: (entry overrides or removals, expected problem); each targets "- faulty target"
FAULTS = ([({"drop": f}, f"missing field {f}") for f in ("file", "sha", "prefix", "reasons", "by", "status")]
          + [({"status": "remove-candidate"}, "missing field removal_reasons")]
          + [({"file": 3}, "file must be a string"), ({"sha": "ABC"}, "sha must be 12 lowercase hex"),
             ({"sha": 12}, "sha must be 12 lowercase hex"), ({"prefix": []}, "prefix must be a string"),
             ({"reasons": "doc:x#y"}, "reasons must be a list of strings"), ({"reasons": [1]}, "reasons must be a list of strings"),
             ({"by": 1}, "by must be a string"), ({"status": None}, "status must be a string"),
             ({"by": "owner-ish"}, "unknown by owner-ish"), ({"status": "drop"}, "unknown status drop"),
             ({"status": "remove-candidate", "removal_reasons": "key:alpha"}, "removal_reasons must be a list of strings"),
             ({"file": "docs/ideas.md"}, "file outside the contract population"),
             ({"file": "agent/roles/sub/x.md"}, "file outside the contract population")])


def git(root, *args):
    return subprocess.run(GIT + list(args), cwd=str(root), env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()


def ledger_fixture(tmp):
    """plant() plus a git-committed contract population and a ledger holding every LEDGER_ROWS row, FAULTS and drift entries.
    After the commit: one word changed (stale) and one uncommitted line (skeleton)."""
    p = plant(tmp)
    root = p["root"]
    mapping = json.loads(Path(p["map"]).read_text())
    mapping["keys"]["omega"] = {"by": "worker", "scope": "role:coder", "proposed_home": "adapter", "sources": ["memory:fb-cmd.md"]}
    Path(p["map"]).write_text(json.dumps(mapping))
    rows = [r[1] for r in LEDGER_ROWS if r[1] not in LINES_COMMITTED and r[1].strip() not in (l.strip() for l in LINES_COMMITTED)]
    (root / "claude" / "main-prompt.md").write_text("\n".join(LINES_COMMITTED + rows) + "\n")
    (root / "agent" / "roles" / "coder.md").write_text("# Coder\n- coder rule\n")
    (root / "agent" / "contract" / "worker.md").write_text("# W\n- moved rule\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "x")
    head = git(root, "rev-parse", "HEAD")[:12]
    text = (root / "claude" / "main-prompt.md").read_text().replace("- old wording here", "- new wording here")
    (root / "claude" / "main-prompt.md").write_text(text + "- uncommitted line\n")
    sha = report.line_sha
    entries = []
    for _, text_, reasons, by, status, removal, _, _ in LEDGER_ROWS:
        entry = {"file": "claude/main-prompt.md", "sha": sha(text_), "prefix": text_, "reasons": [r.replace(H, head) for r in reasons],
                 "by": by, "status": status}
        if removal is not None:
            entry["removal_reasons"] = [r.replace(H, head) for r in removal]
        entries.append(entry)
    drift = [
        # one word changed: stale; its pointers are still checked (unresolved key, unchecked label, bad removal pointer), uncounted
        {**GOOD, "sha": sha("- old wording here"), "reasons": ["key:nokey", "label:labels-X.json#0"], "status": "remove-candidate",
         "removal_reasons": ["bogus"]},
        {**GOOD, "file": "agent/roles/coder.md", "sha": sha("- moved rule")},      # moved to worker.md: stale + no entry
        {**GOOD, "file": "agent/roles/gone.md", "sha": sha("- x"), "reasons": ["key:nokey"]},  # file removed; pointer still checked
        {**GOOD, "sha": sha("- dup target"), "reasons": ["key:alpha", "key:nokey"]},  # duplicate (file, sha): neither counts
        {**GOOD, "sha": sha("- dup target"), "reasons": ["key:alpha"]},
    ]
    faults = []
    for change, _ in FAULTS:
        entry = {**GOOD, "sha": sha("- faulty target")}
        entry.update({k: v for k, v in change.items() if k != "drop"})
        entry.pop(change.get("drop"), None)
        faults.append(entry)
    # a valid companion with the faulty rows' identity: every identity seen twice is a duplicate, whatever else is wrong with it
    faults.append({**GOOD, "sha": sha("- faulty target"), "reasons": ["key:alpha"]})
    ledger = Path(tmp) / "ledger.json"
    ledger.write_text(json.dumps({"schema": report.LEDGER_SCHEMA, "entries": entries + drift + faults + ["not an object"]}))
    return {**p, "ledger": str(ledger), "head": head, "first_fault": len(entries) + len(drift)}


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

    def test_ledger_matrix_and_skeleton(self):
        """HB7b: every cell of the brief's input-space table that shares the ledger fixture, the unchecked and git-missing
        variants, the ledger-level faults, and the skeleton (committed, uncommitted, invalid entry still blocks a duplicate)."""
        with tempfile.TemporaryDirectory() as tmp:
            p = ledger_fixture(tmp)
            level_paths = {}
            for name, content in (("missing", None), ("not json", "{"), ("wrong schema", json.dumps({"schema": "x", "entries": []})),
                                  ("no entries", json.dumps({"schema": report.LEDGER_SCHEMA, "entries": {}})),
                                  ("a directory", "dir")):
                path = level_paths[name] = Path(tmp) / f"lv-{len(level_paths)}.json"
                if content == "dir":
                    path.mkdir()
                elif content is not None:
                    path.write_text(content)
            before = sorted((str(f), f.stat().st_mtime_ns) for f in Path(tmp).rglob("*") if f.is_file() and ".git" not in f.parts)
            out = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"], p["ledger"])
            bare = report.build(p["map"], None, [], None, p["root"], p["ledger"])  # no store, labels, memory
            with mock.patch.dict(os.environ, {"PATH": ""}):  # git missing: commit pointers unchecked, one problem
                nogit = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"], p["ledger"])
            levels = {name: report.build(p["map"], root=p["root"], ledger=str(path)) for name, path in level_paths.items()}
            skel_all, skel_all_problems = report.skeleton(p["root"])
            skel, skel_problems = report.skeleton(p["root"], p["ledger"])
            after = sorted((str(f), f.stat().st_mtime_ns) for f in Path(tmp).rglob("*") if f.is_file() and ".git" not in f.parts)
        sha, head = report.line_sha, p["head"]
        led = out["contract_ledger"]
        cat = {(m["file"], m["sha"]): m["category"] for m in led["matched"]}
        removal = {m["sha"]: m["removal"] for m in led["removal"]}
        for name, text, _, _, _, _, want, want_removal in LEDGER_ROWS:
            with self.subTest(name):
                self.assertEqual(cat.get(("claude/main-prompt.md", sha(text))), want)
                self.assertEqual(removal.get(sha(text)), want_removal)
        why = {(u["sha"], u["pointer"]): u["why"] for u in led["unresolved"]}
        bare_cat = {m["sha"]: m["category"] for m in bare["contract_ledger"]["matched"]}
        bare_unchecked = {u["sha"]: u["why"] for u in bare["contract_ledger"]["unchecked"]}
        nogit_unchecked = {(u["sha"], u["pointer"]) for u in nogit["contract_ledger"]["unchecked"]}
        nogit_cat = {m["sha"]: m["category"] for m in nogit["contract_ledger"]["matched"]}
        mp = {r["file"]: r for r in led["files"]}
        ledger_problems = {(x["where"], x["problem"]) for x in out["problems"] if x["source"] == "ledger"}

        def fault_listed(n, want):
            return any((w == f"ledger.json entry {n}" or w.startswith(f"ledger.json entry {n} (")) and pr == want + "; entry skipped"
                       for w, pr in ledger_problems)
        skel_by = {(e["file"], e["sha"]): e for e in skel["entries"]}
        listed = [x for x in out["problems"] if x["source"] in ("ledger", "commit")]
        nogit_listed = [x for x in nogit["problems"] if x["source"] in ("ledger", "commit")]
        # entries whose problems name no readable file: a dropped file, file 3, docs/ideas.md, a subfolder file, gone.md, a non-object
        unattributed = 6
        # the faulty rows that keep their (file, sha) identity: with the valid companion each is a duplicate
        kept_identity = sum(not ({"file", "sha"} & ({k for k in c if k != "drop"} | {c.get("drop")})) for c, _ in FAULTS)

        def pointers(rows, sha_):
            return {(u["role"], u["pointer"]) for u in rows if u["sha"] == sha_}
        shown = report.render(out)
        cells = [
            # pointer side: unresolved reasons are listed with why
            ("why record", "record not in the store", why.get((sha("- rec missing"), "record:fr/R99"))),
            ("why label", "row not in the file", why.get((sha("- label missing"), "label:labels-T.json#99"))),
            ("why memory", "memory file not found, malformed or refused", why.get((sha("- memory missing"), "memory:nope.md"))),
            ("why doc", "heading not found in doc", why.get((sha("- doc missing"), "doc:docs/ideas.md#H9"))),
            ("why key", "key not in the mapping", why.get((sha("- key missing"), "key:nokey"))),
            ("why commit", "commit not in the repository", why.get((sha("- commit missing"), "commit:deadbeef0"))),
            ("why malformed", ["malformed pointer", "malformed pointer (commit:<7-40 lowercase hex>)",
                               "malformed pointer (label:<file>#<i>)", "malformed pointer (key:<friction key>)"],
             [why.get((sha("- malformed"), x)) for x in ("bogus", "commit:XYZ", "label:nohash", "key:Bad Key")]),
            ("unchecked label file", {sha("- label other"): "labels file not given (or not readable)",
                                       sha("- old wording here"): "labels file not given (or not readable)"},
             {u["sha"]: u["why"] for u in led["unchecked"]}),
            ("removal pointer listed", "key not in the mapping", next(u["why"] for u in led["unresolved"]
                                                                     if u["sha"] == sha("- rm unresolved") and u["role"] == "removal")),
            # source not given: unchecked, counts for nothing
            *[(f"unchecked {name}", ("no_resolved_reason", reason),
               (bare_cat.get(sha(next(r[1] for r in LEDGER_ROWS if r[0] == name))),
                bare_unchecked.get(sha(next(r[1] for r in LEDGER_ROWS if r[0] == name)))))
              for name, reason in UNCHECKED.items()],
            # git missing: one commit problem; commit pointers unchecked; commit + doc stays justified, commit alone is nothing
            ("git missing problem", ["commit"], [x["source"] for x in nogit["problems"] if x["source"] == "commit"]),
            ("git missing unchecked", True, (sha("- commit only"), f"commit:{head}") in nogit_unchecked),
            ("git missing categories", ("no_resolved_reason", "justified_main"),
             (nogit_cat[sha("- commit only")], nogit_cat[sha("- commit doc")])),
            # line side
            # 11 committed (two headings and a blank need none; the fenced `#` line and both fence lines do), the uncommitted
            # line, and one per row line not among the committed ones
            ("needing main-prompt", 11 + 1 + len({r[1] for r in LEDGER_ROWS} - set(LINES_COMMITTED) - {"- spaced rule"}),
             mp["claude/main-prompt.md"]["needing"]),
            ("duplicate lines covered", 2, next(m["lines"] for m in led["matched"] if m["sha"] == sha("- dup line"))),
            ("one word changed: stale", True, any(s["sha"] == sha("- old wording here") for s in led["stale"])),
            # a stale entry's pointers are still resolved and listed; nothing of it counts
            ("stale pointers listed", ({("reason", "key:nokey"), ("removal", "bogus")}, {("reason", "label:labels-X.json#0")}, False),
             (pointers(led["unresolved"], sha("- old wording here")), pointers(led["unchecked"], sha("- old wording here")),
              ("claude/main-prompt.md", sha("- old wording here")) in cat)),
            ("stale removal not counted", False, any(r["sha"] == sha("- old wording here") for r in led["removal"])),
            ("stale text", True, any(w.startswith("ledger.json entry") and pr == "stale: no current line that needs a reason has this sha"
                                     for w, pr in ledger_problems)),
            ("stale heading", True, "no current line that needs a reason has this sha" in shown),
            # duplicate and missing-file entries: pointers listed, uncounted
            ("duplicate pointers listed", {("reason", "key:nokey")}, pointers(led["unresolved"], sha("- dup target"))),
            ("duplicate entries uncounted", False, ("claude/main-prompt.md", sha("- dup target")) in cat),
            ("missing file pointers listed", {("reason", "key:nokey")},
             {(u["role"], u["pointer"]) for u in led["unresolved"] if u["file"] == "agent/roles/gone.md"}),
            # the valid companion of the faulty rows is a duplicate: no justification, no evidence credit
            ("faulty identity companion uncounted", False, ("claude/main-prompt.md", sha("- faulty target")) in cat),
            # coder.md also has "- coder rule" without an entry
            ("moved to another file", ((1, 1), (0, 1)), ((mp["agent/roles/coder.md"]["stale_entries"], mp["agent/roles/coder.md"]["no_entry"]),
                                                         (mp["agent/contract/worker.md"]["stale_entries"], mp["agent/contract/worker.md"]["no_entry"]))),
            # no entry: no-entry line, new wording, uncommitted, faulty target (invalid entries), dup target (duplicates), the two
            # fence delimiter lines
            ("no entry main-prompt", 7, mp["claude/main-prompt.md"]["no_entry"]),
            ("file removed", True, any(w.startswith("ledger.json entry") and "agent/roles/gone.md" in w and pr == "file not found"
                                       for w, pr in ledger_problems)),
            # the valid pair, then the faulty rows with the valid companion: every entry with the identity is listed
            ("duplicate (file, sha)", 2 + kept_identity + 1,
             sum(pr == "duplicate (file, sha): no entry with it counts" for _, pr in ledger_problems)),
            # one accounting: the totals equal the problems listed, per file where a readable file is named
            ("problems total", (len(listed), len(nogit_listed)), (led["total"]["problems"], nogit["contract_ledger"]["total"]["problems"])),
            ("problems per file", (len(listed) - unattributed - 1, 1, 0),  # coder.md: its one stale entry
             (mp["claude/main-prompt.md"]["problems"], mp["agent/roles/coder.md"]["problems"], mp["agent/contract/worker.md"]["problems"])),
            ("problems text", True, any(l.startswith("  total:") and "need a reason" in l and l.endswith(f"problems {len(listed)}")
                                       for l in shown.split("\n"))),
            *[(f"fault {i} {want}", True, fault_listed(p["first_fault"] + i, want)) for i, (_, want) in enumerate(FAULTS)],
            ("entry not an object", True, any(pr == "entry is not an object; skipped" for _, pr in ledger_problems)),
            # ledger level: listed, the rest of the report runs
            *[(f"ledger {name}", (want, 0, True, 1), (next(x["problem"] for x in o["problems"] if x["source"] == "ledger"),
                                                      o["contract_ledger"]["entries"], bool(o["contract_size"]["files"]),
                                                      o["contract_ledger"]["total"]["problems"]))
              for name, want, o in (("missing", "ledger not readable: FileNotFoundError", levels["missing"]),
                                    ("not json", "ledger not JSON: JSONDecodeError", levels["not json"]),
                                    ("wrong schema", "schema is not tp-contract-ledger:v1; ledger not read", levels["wrong schema"]),
                                    ("no entries", "ledger has no `entries` list; ledger not read", levels["no entries"]),
                                    ("a directory", "ledger not readable: IsADirectoryError", levels["a directory"]))],
            # counting: evidence_lines = inline pointer or justified entry; the totals add up per category
            ("evidence lines", sum(r[6].startswith("justified") for r in LEDGER_ROWS) + 1,  # the dup line counts twice
             out["contract_size"]["files"][0]["evidence_lines"]),
            ("total adds up", led["total"]["needing"],
             sum(led["total"][k] for k in ("justified_owner", "justified_main", "provenance_only", "worker_evidence_only",
                                           "no_resolved_reason", "no_entry"))),
            # skeleton: every needing line once per (file, sha); committed -> commit:<head>, uncommitted -> []
            ("skeleton all size", sum(r["needing"] for r in led["files"]) - 1, len(skel_all["entries"])),
            ("skeleton committed", [f"commit:{head}"], skel_by[("claude/main-prompt.md", sha("- no entry line"))]["reasons"]),
            ("skeleton uncommitted", [], skel_by[("claude/main-prompt.md", sha("- uncommitted line"))]["reasons"]),
            ("skeleton reworded", [], skel_by[("claude/main-prompt.md", sha("- new wording here"))]["reasons"]),
            ("skeleton fields", {"file", "sha", "prefix", "reasons", "by", "status"}, set(skel_all["entries"][0])),
            ("skeleton by/status", {("main", "keep")}, {(e["by"], e["status"]) for e in skel_all["entries"]}),
            ("skeleton prefix", "- no entry line", skel_by[("claude/main-prompt.md", sha("- no entry line"))]["prefix"]),
            ("skeleton skips ledgered", False, ("claude/main-prompt.md", sha("- rec ok")) in skel_by),
            ("skeleton skips invalid-entry line", False, ("claude/main-prompt.md", sha("- faulty target")) in skel_by),
            ("skeleton moved line", True, ("agent/contract/worker.md", sha("- moved rule")) in skel_by),
            ("skeleton committed problems", [], [x for x in skel_all_problems if x["source"] == "commit"]),
            ("skeleton ledger problems", True, any(x["source"] == "ledger" for x in skel_problems)),
            ("read-only", before, after),
        ]
        for cell, want, got in cells:
            with self.subTest(cell):
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
            helped = io.StringIO()
            with contextlib.redirect_stdout(helped), self.assertRaises(SystemExit):
                report.main(["--help"])
            raw = io.StringIO()
            with contextlib.redirect_stdout(raw):
                report.main(["--map", p["map"], "--json"], root=p["root"])  # no store, labels or memory: listed, not a crash
            ledgered, skel, skel_err = io.StringIO(), io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(ledgered):  # a missing ledger: the section and the problem, the rest still reported
                report.main(["--map", p["map"], "--ledger", str(Path(tmp) / "none.json")], root=p["root"])
            with contextlib.redirect_stdout(skel), contextlib.redirect_stderr(skel_err):  # plant's root is not a git repository
                skel_code = report.main(["--map", p["map"], "--ledger-skeleton"], root=p["root"])
            after = sorted((str(f), f.stat().st_mtime_ns) for f in Path(tmp).rglob("*") if f.is_file())
            for cell, want, got in attribution_cells(p):  # writes its own mapping files, so after the read-only snapshot
                with self.subTest(cell):
                    self.assertEqual(got, want)
        self.assertEqual(code, 0)
        self.assertIn("## alpha  [active", text.getvalue())
        self.assertIn("recurrence after correction (2026-11-01T10:00:00Z): sessions 1 ", text.getvalue())
        self.assertIn("A record-local `friction` key is not read: that is deferred", " ".join(helped.getvalue().split()))
        self.assertIn("commit: alone is provenance only; by worker is never justified", " ".join(helped.getvalue().split()))
        self.assertNotIn("contract_ledger", out)  # without --ledger the output is unchanged
        self.assertNotIn("## Contract ledger", text.getvalue())
        self.assertIn("## Contract ledger (none.json, 0 valid entries)", ledgered.getvalue())
        self.assertIn("- ledger none.json: ledger not readable: FileNotFoundError", ledgered.getvalue())
        self.assertIn("## Contract size", ledgered.getvalue())
        skeleton_doc = json.loads(skel.getvalue())  # stdout is a pure ledger; problems go to stderr
        self.assertEqual((skel_code, skeleton_doc["schema"], len(skeleton_doc["entries"])), (0, report.LEDGER_SCHEMA, 4))
        self.assertEqual({tuple(e["reasons"]) for e in skeleton_doc["entries"]}, {()})  # not a git repository: no reasons
        self.assertIn("not a git repository or no commit at HEAD", skel_err.getvalue())
        self.assertNotIn(SECRET, text.getvalue() + json.dumps(out))  # no quote, why, body, description or project leaves the tool
        bare = json.loads(raw.getvalue())
        self.assertEqual((bare["inputs"]["records"]["given"], bare["inputs"]["memory"]["given"], bare["ungrouped"]["label"]),
                         (False, False, []))
        self.assertEqual(before, after)  # read-only

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every directory")
    def test_unreadable_inputs_are_listed(self):
        """An unreadable memory directory, doc directory and store: a listed problem each, every other source still reported.
        HB7b: an unreadable role directory, role file or ledger file is a listed problem; the rest of the report still runs."""
        def ledger_for(tmp):
            path = Path(tmp) / "ledger.json"
            path.write_text(json.dumps({"schema": report.LEDGER_SCHEMA, "entries": [
                {"file": "agent/roles/coder.md", "sha": report.line_sha("Run the affected tests."), "prefix": "Run",
                 "reasons": ["doc:docs/ideas.md#H1"], "by": "main", "status": "keep"}]}))
            return str(path)

        def ledger_problems(out):
            return [x["problem"] for x in out["problems"] if x["source"] == "ledger"]
        with tempfile.TemporaryDirectory() as tmp:
            p = plant(tmp)
            locked = [Path(p["memory"]), p["root"] / "docs", Path(p["store"]) / "workspaces", p["root"] / "agent" / "roles"]
            try:
                for d in locked:
                    d.chmod(0)
                out = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"], ledger_for(tmp))
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
        self.assertEqual(ledger_problems(out), ["directory not readable; entry not checked"])
        self.assertEqual(out["contract_ledger"]["total"]["problems"], 1)  # no readable file named: the total only

        # a store root that can be traversed but not listed (0o111): the root itself is the listed problem; the other sources report
        with tempfile.TemporaryDirectory() as tmp:
            p = plant(tmp)
            ledger = ledger_for(tmp)
            role = p["root"] / "agent" / "roles" / "coder.md"
            try:
                Path(p["store"]).chmod(0o111)
                role.chmod(0)
                out = report.build(p["map"], p["store"], p["labels"], p["memory"], p["root"], ledger)
                Path(ledger).chmod(0)
                no_ledger = report.build(p["map"], root=p["root"], ledger=ledger)
            finally:
                Path(p["store"]).chmod(0o755)
                role.chmod(0o644)
                Path(ledger).chmod(0o644)
        self.assertEqual(ledger_problems(out), ["file not readable; entry not checked"])
        self.assertEqual((out["contract_ledger"]["total"]["problems"], no_ledger["contract_ledger"]["total"]["problems"]), (1, 1))
        self.assertEqual((ledger_problems(no_ledger), len(no_ledger["contract_size"]["files"])),
                         (["ledger not readable: PermissionError"], 2))
        self.assertEqual([(x["where"], x["problem"]) for x in out["problems"] if x["source"] == "record"],
                         [(p["store"], "store not readable: PermissionError")])
        self.assertEqual(out["inputs"]["records"]["records"], 0)
        self.assertEqual(out["inputs"]["memory"]["feedback"], 4)
        self.assertEqual(out["inputs"]["labels"]["rows"], 8)


if __name__ == "__main__":
    unittest.main()
