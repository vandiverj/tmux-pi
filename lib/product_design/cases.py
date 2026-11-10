"""Case export, worker packets and impact (docs/product-design.md, "Cases and packets"). Reads only.

`export_cases` renders the case definitions a model prepared (stored in the package through a changeset) for one slice
to Markdown, one file per case, and to one structured JSON file. `packet` assembles the bounded context one case needs for
an implementation worker. `impact_report` lists what changed under accepted slices, reviews and cases. None of them calls
a model, a tracker or the network, and none changes the package: they write only the output they are asked for.

Whether a case may be exported or handed out is package.case_report, which reads slice readiness (decision 9): one rule.

Text is inert. Every package text rendered into Markdown goes through `md`, which backslash-escapes every character
Markdown gives a meaning to and keeps each text inside the line it was given; ids are rendered as code spans only when
they are well-formed ids (no backtick can occur). JSON output is data.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path

from . import package as P
from . import schema as S
from . import store as T

import safe_write  # noqa: E402  (store.py puts lib/ on sys.path)

EXPORT_SCHEMA = "tp-product-cases"
PACKET_SCHEMA = "tp-product-packet"
EXPORT_RECORD = "export.json"  # the digests of the files the last export wrote, so a hand edit is never overwritten
# Characters Markdown (CommonMark and GFM) gives a meaning to anywhere in a line, and those that matter at its start.
_ANYWHERE = set("\\`*_[]<>#|~&{}$!")
_LINE_START = set("-+=")  # a list item or a setext underline; "12." and "12)" are handled in md
_BIDI = re.compile("[\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c]")


def _refuse(code, message, details=None, exit=1):
    raise T.StoreError(code, message, exit=exit, details=details or {})


# ---------------------------------------------------------------- inert Markdown

def md(text) -> str:
    """`text` as inert Markdown inline content: every character Markdown gives a meaning to is backslash-escaped, so it
    renders as the character itself; controls become spaces; bidirectional controls become a visible [U+XXXX]; line
    breaks become hard breaks (`\\` at the end of the line) inside the same paragraph, with leading spaces dropped and
    the start of each line escaped, so no text can start a heading, list, quote, code block, table or HTML block."""
    text = _BIDI.sub(lambda m: f"[U+{ord(m.group()):04X}]", str(text))
    text = "".join(" " if unicodedata.category(ch) == "Cc" and ch not in "\n\r" else ch for ch in text)
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln] or [""]
    out = []
    for ln in lines:
        chars = []
        for n, ch in enumerate(ln):
            after_number = n > 0 and all(c in "0123456789" for c in ln[:n])  # "12." and "12)" start an ordered list
            if ch in _ANYWHERE or (n == 0 and ch in _LINE_START) or (after_number and ch in ".)"):
                chars.append("\\" + ch)
            else:
                chars.append(ch)
        out.append("".join(chars))
    return "\\\n  ".join(out)


def code(ident) -> str:
    """A record id as a code span. Ids are validated slugs (schema.ID_RE): no backtick, no line break."""
    ident = str(ident)
    return f"`{ident}`" if S.ID_RE.fullmatch(ident) or re.fullmatch(r"sha256:[0-9a-f]{64}", ident) else md(ident)


def unescape(text) -> str:
    """The inverse of `md` on its output (for tests and the attack): the text a renderer shows."""
    return re.sub(r"\\(.)", r"\1", text.replace("\\\n  ", "\n"))


# ---------------------------------------------------------------- reading a trusted package

NOT_DESIGN = ("package.build-",)  # trust problems about review builds: they do not change what a case says


def trusted_docs(package_dir):
    """The committed, valid package, or StoreError. A package that cannot be trusted (an interrupted commit, an
    uncommitted external edit, no commit record, disagreeing control records) is refused, as apply refuses it: a case
    exported from content nobody committed would carry a revision that does not describe it."""
    state = T.inspect(package_dir)
    if any(i.code.startswith(("file.", "control.")) for i in state["issues"]):
        _refuse("package.unreadable", "; ".join(f"{S.format_path(i.path)}: {i.message} [{i.code}]" for i in state["issues"]))
    if state["issues"]:
        _refuse("package.invalid", "; ".join(f"{S.format_path(i.path)}: {i.message} [{i.code}]" for i in state["issues"]))
    trust = [(c, m) for c, m in T.trust_problems(state) if not c.startswith(NOT_DESIGN)]
    if trust:
        _refuse(trust[0][0], "; ".join(m for _, m in trust) + ". Nothing was written.", exit=3)
    return state["docs"]


def _without(rec, *fields):
    return {k: v for k, v in rec.items() if k not in fields}


def _refs(docs, case, idx):
    """The records a case names: its dependencies and their dependency closure, plus its actor and implementation-area
    sources, as id -> (kind, record), in id order."""
    ids = set(case["dependencies"])
    for dep in case["dependencies"]:
        ids |= set(P.closure(docs, dep, idx))
    if "actor" in case:
        ids.add(case["actor"])
    ids |= {a["source"] for a in case.get("implementation_areas", []) if "source" in a}
    return {i: idx[i] for i in sorted(ids)}


def case_digest(docs, case, idx) -> str:
    """What a published case says: its semantic content and the fingerprints of the records it names. A revision that
    changes neither leaves it unchanged, so a re-export proposes no update for it."""
    refs = _refs(docs, case, idx)
    return P.digest({"case": P.semantic(case, "case"),
                     "refs": [[i, P.record_fingerprint(r, k)] for i, (k, r) in refs.items()]})


def publication_proposal(case, digest) -> dict:
    """What a re-export proposes to a publisher, per tracker system: create (never published), update (published, and
    the case has changed since: update THAT external id, never a new one) or unchanged. An update says what was
    published, so the publisher can compare it with the tracker's record before overwriting it: an external record
    edited independently since is shown to the human, never overwritten silently. tp-product calls no tracker."""
    last = {}
    for p in case.get("publication", []):
        last[p["system"]] = p
    if not last:
        return {"action": "create", "systems": []}
    systems = []
    for system in sorted(last):
        p = last[system]
        same = p.get("digest") == digest
        systems.append({"system": system, "external_id": p["external_id"], "action": "unchanged" if same else "update",
                        "published_revision": p["package_revision"], "published_digest": p.get("digest")})
    action = "update" if any(s["action"] == "update" for s in systems) else "unchanged"
    return {"action": action, "systems": systems,
            "check": None if action == "unchanged" else
            "before updating, compare the external record with what was published (published_revision, "
            "published_digest); if it was edited there since, show the human both versions: never overwrite it silently"}


# ---------------------------------------------------------------- refusals by name

def _blocker_text(b) -> str:
    text = f"{b['code']} {b['id']}: {b['reason'].rstrip('.')}"
    if "decision_needed" in b:
        text += f" (decision needed: {b['decision_needed']} owner: {b['owner']})"
    return text


def _slice_ready(docs, sid, report):
    if sid not in report:
        known = ", ".join(sorted(report)) or "none"
        _refuse("slice.unknown", f"{sid!r} is not a slice of this package (slices: {known})")
    r = report[sid]
    if r["effective"] not in P.ACCEPTED_STATES:
        idx = P.index(docs)
        blockers = [dict(b) for b in r["blockers"]]
        for b in blockers:
            if b["code"] == "question.unresolved":
                q = idx[b["id"]][1]
                b.update(decision_needed=q["decision_needed"], owner=q["owner"])
        if not blockers and r["stale"]:
            blockers = [{"code": "review.stale", "id": sid, "reason": f"{sid} changed since its acceptance: it needs a "
                                                                      "new human review"}]
        if not blockers:
            blockers = [{"code": "slice.not-accepted", "id": sid,
                         "reason": f"{sid} is {r['state']}: a human review accepts it for case preparation first"}]
        _refuse("slice.blocked" if r["effective"] in ("blocked", "stale") else "slice.not-ready",
                f"{sid} is {r['effective']}; no case is exported: "
                + "; ".join(_blocker_text(b) for b in blockers) + ". Nothing was written.",
                details={"slice": sid, "effective": r["effective"], "blockers": blockers})
    return r


# ---------------------------------------------------------------- tp-product cases

def _lines(items, fmt=md):
    return [f"- {fmt(x)}" for x in items] or ["- None recorded."]


def _examples(examples):
    out = []
    for n, ex in enumerate(examples, 1):
        out += [f"- Example {n}. Situation: {md(ex['situation'])}\\", f"  Outcome: {md(ex['outcome'])}"]
    return out


def _ref_line(idx, ident):
    kind, rec = idx[ident]
    title = rec.get("title") or rec.get("statement") or rec.get("choice") or ident
    line = f"- {code(ident)}: {md(title)}"
    if kind == "rule":
        line += f"\\\n  {md(rec['statement'])}"
    elif kind == "decision":
        line += f" \\({md(rec['status'])}\\)\\\n  {md(rec['choice'])}"
    elif kind == "contract":
        line += f" \\({md(rec['kind'])}, {md(rec['status'])}\\)\\\n  {md(rec['summary'])}"
    elif kind == "scenario":
        line += f" \\({md(rec['kind'])}\\)"
    return line


def render_case(docs, case, idx, provenance, proposal) -> str:
    """One case draft as Markdown. Deterministic: the same package content gives the same bytes."""
    deps = case["dependencies"]
    by_kind = {}
    for d in deps:
        by_kind.setdefault(idx[d][0], []).append(d)
    out = [f"# {md(case['title'])}", "",
           f"Case {code(case['id'])}, slice {code(case['slice'])}. Package {code(provenance['package'])} revision "
           f"{provenance['package_revision']}, content {code(provenance['content_fingerprint'])}; slice fingerprint "
           f"{code(provenance['slice_fingerprint'])}. Prepared from revision {case['source']['package_revision']}.", ""]
    actor = idx[case["actor"]][1].get("title", case["actor"]) if "actor" in case else None
    out += ["## Outcome", "", (f"{md(actor)}: " if actor else "") + md(case.get("outcome", "Not recorded.")), ""]
    out += ["## Scope", ""] + _lines(case.get("scope", [])) + [""]
    out += ["## Exclusions", ""] + _lines(case.get("exclusions", [])) + [""]
    out += ["## Acceptance examples", ""]
    out += _examples(case["acceptance_examples"]) + [""]
    for kind, heading in (("rule", "Rules"), ("scenario", "Scenarios"), ("decision", "Decisions"),
                          ("contract", "Contracts (status is the confidence: proposed or existing-verified)")):
        if by_kind.get(kind):
            out += [f"## {heading}", ""] + [_ref_line(idx, d) for d in by_kind[kind]] + [""]
    other = [d for d in deps if idx[d][0] not in ("rule", "scenario", "decision", "contract")]
    out += ["## Other dependencies", ""] + ([_ref_line(idx, d) for d in other] or ["- None recorded."]) + [""]
    out += ["## Unresolved non-blocking matters", ""] + _lines(case.get("nonblocking", [])) + [""]
    out += ["## Feasibility findings", ""] + _lines(case.get("feasibility", [])) + [""]
    qa = case.get("qa")
    out += ["## QA", ""]
    if qa:
        out += [f"- Approach: {md(qa['approach'])}"] + [f"- Fixture: {md(x)}" for x in qa.get("fixtures", [])] \
            + [f"- Non-functional: {md(x)}" for x in qa.get("nonfunctional", [])]
    else:
        out.append("- None recorded.")
    out.append("")
    areas = [f"- {md(a['area'])} \\({a['basis']}" + (f", {code(a['source'])}" if "source" in a else "") + "\\)"
             for a in case.get("implementation_areas", [])]
    out += ["## Suggested implementation areas", ""] + (areas or ["- None recorded."]) + [""]
    out += ["## Completion evidence", ""] + _lines(case.get("completion_evidence", [])) + [""]
    out += ["## Publication", ""]
    pubs = case.get("publication", [])
    out += [f"- {md(p['system'])} {md(p['external_id'])}, published from revision {p['package_revision']}"
            for p in pubs] or ["- Not published."]
    out.append(f"- Proposal: {proposal['action']}")
    for r in case.get("reopened", []):
        out.append(f"- Reopened {code(r['question'])} on {md(r['date'])} by {md(r['by'])}: {md(r['reason'])} "
                   f"\\(prior revision {r['prior']['package_revision']}\\)")
    return "\n".join(out) + "\n"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _previous(out_dir: Path, package_id, root):
    """The digests the last export to `out_dir` recorded, {} when it holds none. A record of another package, or one
    that cannot be read, is refused: that folder is not this package's export."""
    path = out_dir / EXPORT_RECORD
    try:
        safe_write.check_below(root, path, "export record")
        value = P.read_file(path)
    except (OSError, ValueError) as exc:
        _refuse("export.foreign", f"{path} cannot be read ({exc}); choose another --out folder")
    if value is P.ABSENT:
        return {}
    files = value.get("files") if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get("schema") != "tp-product-export" or value.get("package") != package_id \
            or not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
        _refuse("export.foreign", f"{path} is not an export record of {package_id}; choose another --out folder")
    return files


def export_cases(package_dir, slice_id, out_dir) -> dict:
    """Render the slice's cases to OUT_DIR/<case id>.md and OUT_DIR/cases.json. Refuses, writing nothing, when the
    slice is not ready for case preparation (naming its blockers and the decisions they need), when it has no cases,
    and when any of its cases is stale or blocked (by name). Re-exporting replaces only files this command wrote and
    nobody changed since: a file edited by hand is refused by name (export.edited), never overwritten."""
    docs = trusted_docs(package_dir)
    report = P.readiness(docs)
    r = _slice_ready(docs, slice_id, report)
    idx = P.index(docs)
    cases = [c for c in docs["design"]["cases"] if c["slice"] == slice_id]
    if not cases:
        _refuse("cases.none", f"{slice_id} has no prepared cases; prepare them with /prepare-cases and store them "
                              "with a changeset first")
    states = P.case_report(docs, report)
    bad = [(c["id"], states[c["id"]]) for c in cases if states[c["id"]]["state"] != "ready"]
    if bad:
        why = "; ".join(f"{cid} is {s['state']}: " + "; ".join(_blocker_text(b) for b in s["stale"] + s["blockers"])
                        for cid, s in bad)
        _refuse("case.stale" if any(s["state"] == "stale" for _, s in bad) else "case.blocked",
                f"{why}. Prepare the case again from the current package. Nothing was written.",
                details={"cases": {cid: s for cid, s in bad}})
    m = docs["manifest"]
    provenance = {"package": m["id"], "package_revision": m["revision"],
                  "content_fingerprint": P.content_fingerprint(docs), "slice": slice_id,
                  "slice_fingerprint": r["fingerprint"]}
    files, entries = {}, []
    for case in sorted(cases, key=lambda c: c["id"]):
        digest = case_digest(docs, case, idx)
        proposal = publication_proposal(case, digest)
        name = f"{case['id']}.md"
        files[name] = render_case(docs, case, idx, provenance, proposal).encode("utf-8")
        refs = _refs(docs, case, idx)
        entries.append({"key": case["id"], "file": name, "digest": digest,
                        "case": _without(case, "publication", "reopened"),
                        "references": {i: {"kind": k, "title": rec.get("title"), "fingerprint": P.record_fingerprint(rec, k)}
                                       for i, (k, rec) in refs.items()},
                        "publication": case.get("publication", []), "reopened": case.get("reopened", []),
                        "proposal": proposal})
    files["cases.json"] = P.render(dict({"schema": EXPORT_SCHEMA, "schema_version": 1}, **provenance, cases=entries))
    written = _write_outputs(Path(out_dir), m["id"], slice_id, files)
    return dict(provenance, out=str(out_dir), cases=[{"key": e["key"], "file": e["file"], "proposal": e["proposal"]}
                                                       for e in entries], written=written)


def _write_outputs(out_dir: Path, package_id, slice_id, files) -> list:
    """Write `files` (name -> bytes) into out_dir, each atomically, then the export record. Every file is checked first:
    one that exists and is neither what the last export wrote nor the new bytes is someone else's edit (export.edited),
    and nothing is written."""
    root = out_dir.parent
    try:
        safe_write.check_below(root, out_dir, "export folder")
    except OSError as exc:
        _refuse("export.unsafe", f"{out_dir}: {exc}")
    previous = _previous(out_dir, package_id, root) if out_dir.is_dir() else {}
    edited = []
    for name, data in files.items():
        path = out_dir / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            edited.append(f"{name} (not a regular file)")
            continue
        if not path.exists():
            continue
        have = _sha(path.read_bytes())
        if have != _sha(data) and have != previous.get(name):
            edited.append(name)
    if edited:
        _refuse("export.edited", f"{', '.join(edited)} in {out_dir} changed since the last export (or were not written "
                                 "by it); keep your edits elsewhere or remove them, then export again. Nothing was written.",
                details={"files": edited})
    safe_write.ensure_dir(out_dir, what="export folder", root=root)
    written = []
    for name, data in files.items():
        path = out_dir / name
        if path.exists() and path.read_bytes() == data:
            continue
        safe_write.write_atomic(path, data, root=out_dir)
        written.append(name)
    record = P.render({"schema": "tp-product-export", "schema_version": 1, "package": package_id, "slice": slice_id,
                       "files": dict(sorted(dict(previous, **{n: _sha(d) for n, d in files.items()}).items()))})
    path = out_dir / EXPORT_RECORD
    if not (path.exists() and path.read_bytes() == record):
        safe_write.write_atomic(path, record, root=out_dir)
    return written


# ---------------------------------------------------------------- tp-product packet

def build_packet(docs, case_id) -> dict:
    """The packet of one case as data (see `packet`). Refuses a stale case first, then a blocked one, by name."""
    idx = P.index(docs)
    if idx.get(case_id, (None,))[0] != "case":
        _refuse("case.unknown", f"{case_id!r} is not a case of this package")
    case = idx[case_id][1]
    state = P.case_report(docs)[case_id]
    if state["stale"]:
        _refuse("case.stale", f"{case_id} is stale: " + "; ".join(_blocker_text(b) for b in state["stale"])
                + ". Prepare it again from the current package before handing it out. Nothing was written.",
                details={"case": case_id, "stale": state["stale"]})
    if state["blockers"]:
        _refuse("case.blocked", f"{case_id} is blocked: " + "; ".join(_blocker_text(b) for b in state["blockers"])
                + ". Nothing was written.", details={"case": case_id, "blockers": state["blockers"]})
    m = docs["manifest"]
    refs = _refs(docs, case, idx)
    scope = {}
    for kind in ("scenario", "rule", "decision", "contract", "claim", "actor"):
        scope[kind + "s"] = [_without(r, "acceptance") if kind == "decision" else r
                            for i, (k, r) in refs.items() if k == kind]
    cited = set(i for i, (k, _) in refs.items() if k == "source")
    for r in scope["rules"] + scope["contracts"]:
        cited |= set(r.get("sources", []))
        for cl in r.get("claims", []):
            cited.add(cl)
            cited |= set(idx[cl][1].get("sources", []))
    for cl in sorted(i for i in cited if idx[i][0] == "claim" and i not in refs):
        scope["claims"].append(idx[cl][1])
    claims = scope["claims"]
    for cl in claims:
        cited |= set(cl.get("sources", []))
    sources = [{k: idx[s][1][k] for k in ("id", "title", "locator", "revision", "digest", "anchor", "excerpt", "captured")
                if k in idx[s][1]} for s in sorted(i for i in cited if idx[i][0] == "source")]
    inside = {r["id"] for coll in scope.values() for r in coll} | {s["id"] for s in sources} | {case_id}
    sid = case["slice"]
    linked = sorted(set(P.closure(docs, sid, idx)) | {sid} | set(idx[sid][1].get("depends_on", []))
                    | {d for d in case["dependencies"] if idx[d][0] in ("case", "slice", "journey", "measurement")}
                    | {c["id"] for c in docs["design"]["cases"] if c["slice"] == sid})
    links = [{"id": i, "kind": idx[i][0], "title": idx[i][1].get("title"), "in": P.DOCUMENTS["design"][0]}
             for i in linked if i not in inside]
    verification = {"acceptance_examples": case["acceptance_examples"], "qa": case.get("qa"),
                    "completion_evidence": case.get("completion_evidence", []),
                    "scenario_expectations": [{"scenario": s["id"], "expected": s.get("expected", [])}
                                              for s in scope["scenarios"]],
                    "rule_examples": [{"rule": r["id"], "examples": r.get("examples", [])} for r in scope["rules"]]}
    return {"schema": PACKET_SCHEMA, "schema_version": 1,
            "provenance": {"package": m["id"], "package_revision": m["revision"],
                           "content_fingerprint": P.content_fingerprint(docs), "case": case_id, "slice": sid,
                           "slice_fingerprint": state["current"], "case_digest": case_digest(docs, case, idx)},
            "case": _without(case, "publication", "reopened"), "dependencies": case["dependencies"],
            "scope": scope, "sources": sources, "verification": verification,
            "publication": case.get("publication", []), "links": links,
            "note": "Package facts only. Branch, base and worktree are the delivery dispatch's to supply; this packet "
                    "does not know them. Read the linked records in the package for anything outside this scope."}


def render_packet(pk) -> str:
    """The packet as Markdown (inert, deterministic)."""
    pv, case = pk["provenance"], pk["case"]
    out = [f"# Worker packet: {md(case['title'])}", "",
           f"Case {code(pv['case'])}, slice {code(pv['slice'])}. Package {code(pv['package'])} revision "
           f"{pv['package_revision']}, content {code(pv['content_fingerprint'])}; slice fingerprint "
           f"{code(pv['slice_fingerprint'])}.", "", md(pk["note"]), "",
           "## Outcome", "", md(case.get("outcome", "Not recorded.")), ""]
    out += ["## Scope", ""] + _lines(case.get("scope", [])) + ["", "## Exclusions", ""] + _lines(case.get("exclusions", [])) + [""]
    out += ["## Acceptance examples", ""]
    out += _examples(case["acceptance_examples"]) + [""]
    sc = pk["scope"]
    if sc["scenarios"]:
        out += ["## Scenarios", ""]
        for s in sc["scenarios"]:
            out.append(f"- {code(s['id'])}: {md(s.get('title', s['id']))} \\({md(s['kind'])}\\)")
            out += [f"  - Expected: {md(e)}" for e in s.get("expected", [])]
        out.append("")
    if sc["rules"]:
        out += ["## Rules", ""]
        for r in sc["rules"]:
            out.append(f"- {code(r['id'])}: {md(r['statement'])}")
            out += [f"  - Example: {md(e['situation'])} Outcome: {md(e['outcome'])}" for e in r.get("examples", [])]
        out.append("")
    if sc["decisions"]:
        out += ["## Decisions", ""] + [f"- {code(d['id'])} \\({md(d['status'])}\\): {md(d['choice'])}"
                                       for d in sc["decisions"]] + [""]
    if sc["contracts"]:
        out += ["## Contracts", ""] + [f"- {code(c['id'])} \\({md(c['kind'])}, {md(c['status'])}\\): {md(c['summary'])}"
                                       for c in sc["contracts"]] + [""]
    if pk["sources"]:
        out += ["## Sources", ""]
        for s in pk["sources"]:
            loc = s["locator"]
            ver = s.get("revision") or s.get("digest")
            out.append(f"- {code(s['id'])}: {md(loc['root'])} {md(loc['path'])} \\({md(loc['kind'])}, {md(ver)}\\)"
                       + (f", anchor {md(s['anchor'])}" if "anchor" in s else ""))
            if "excerpt" in s:
                out.append(f"  - Excerpt: {md(s['excerpt'])}")
        out.append("")
    v = pk["verification"]
    out += ["## Verification", ""]
    if v["qa"]:
        out += [f"- QA approach: {md(v['qa']['approach'])}"] + [f"- Fixture: {md(x)}" for x in v["qa"].get("fixtures", [])] \
            + [f"- Non-functional: {md(x)}" for x in v["qa"].get("nonfunctional", [])]
    out += [f"- Completion evidence: {md(x)}" for x in v["completion_evidence"]] or []
    out += ["", "## Dependencies", ""] + [f"- {code(d)}" for d in pk["dependencies"]] + [""]
    out += ["## Links (read in the package when needed)", ""]
    out += [f"- {code(l['id'])} \\({l['kind']}\\)" + (f": {md(l['title'])}" if l["title"] else "") for l in pk["links"]] \
        or ["- None."]
    return "\n".join(out) + "\n"


def packet(package_dir, case_id, out_file) -> dict:
    """Write the case's worker packet to OUT_FILE: JSON when it ends in .json, Markdown otherwise. Refuses a stale case
    (checked first) and a blocked one, by name, writing nothing. The file is replaced atomically; a file there that a
    previous packet did not write is refused (packet.exists), never overwritten."""
    docs = trusted_docs(package_dir)
    pk = build_packet(docs, case_id)
    target = Path(out_file)
    data = P.render(pk) if target.suffix == ".json" else render_packet(pk).encode("utf-8")
    if target.is_symlink() or (target.exists() and not target.is_file()):
        _refuse("packet.exists", f"{target} is not a regular file; choose another --out")
    if target.exists() and target.read_bytes() != data:
        old = target.read_bytes()
        ours = old.startswith(b"# Worker packet: ") or b'"schema": "tp-product-packet"' in old[:200]
        if not ours:
            _refuse("packet.exists", f"{target} exists and is not a worker packet; choose another --out. Nothing was written.")
    root = target.parent
    safe_write.ensure_dir(root, what="packet folder")
    safe_write.write_atomic(target, data, root=root)
    return dict(pk["provenance"], out=str(target), format="json" if target.suffix == ".json" else "markdown",
                scope={k: [r["id"] for r in v] for k, v in pk["scope"].items()},
                sources=[s["id"] for s in pk["sources"]], links=[l["id"] for l in pk["links"]])


# ---------------------------------------------------------------- tp-product impact

def impact_report(package_dir, since=None) -> dict:
    """What needs attention under accepted slices, reviews and cases (plan section 10): slices that are stale or blocked
    with their blockers, reviews that no longer match the slice they reviewed, and cases that are stale or blocked with
    their published ids (kept). With `since`, also the design records the committed changes after that revision touched
    and the changes no slice reaches (from the history). Reads only."""
    docs = trusted_docs(package_dir)
    report = P.readiness(docs)
    idx = P.index(docs)
    slices = {sid: {"state": r["state"], "effective": r["effective"], "stale": r["stale"], "blockers": r["blockers"]}
              for sid, r in report.items() if r["stale"] or r["blockers"]}
    reviews = []
    for rv in docs["reviews"]["reviews"]:
        for sid in rv["scope"]:
            if sid in report and rv["fingerprint"] != report[sid]["fingerprint"]:
                reviews.append({"review": rv["id"], "slice": sid, "verdict": rv["verdict"],
                                "reviewer": rv["reviewer"]["type"], "reviewed": rv["fingerprint"],
                                "current": report[sid]["fingerprint"],
                                "accepts_slice": idx[sid][1]["readiness"].get("acceptance", {}).get("review") == rv["id"]})
    cases = []
    for cid, c in P.case_report(docs, report).items():
        if c["state"] != "ready":
            cases.append({"id": cid, "slice": c["slice"], "state": c["state"], "stale": c["stale"],
                          "blockers": c["blockers"], "publication": idx[cid][1].get("publication", [])})
    out = {"package": docs["manifest"]["id"], "revision": docs["manifest"]["revision"], "slices": slices,
           "reviews": reviews, "cases": cases}
    if since is not None:
        hist = T.check_history(Path(package_dir))
        changed, unlinked = set(), {}
        for rev in hist["numbers"]:
            entry = hist["entries"].get(rev)
            if not entry or rev <= since:
                continue
            eff = entry.get("impact") or {}
            changed |= set(eff.get("changed", []))
            for u in eff.get("unlinked", []):
                unlinked[u["id"]] = u
        out["since"] = {"revision": since, "changed": sorted(changed),
                        "unlinked": [unlinked[k] for k in sorted(unlinked)]}
    return out
