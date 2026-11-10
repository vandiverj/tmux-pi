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

def md(text, indent=2) -> str:
    """`text` as inert Markdown inline content: every character Markdown gives a meaning to is backslash-escaped, so it
    renders as the character itself; controls become spaces; bidirectional controls become a visible [U+XXXX]; line
    breaks become hard breaks (`\\` at the end of the line) inside the same paragraph, with leading spaces dropped and
    the start of each line escaped, so no text can start a heading, list, quote, code block, table or HTML block. A
    continuation line is indented by `indent` spaces: the content column of the list item the text is in."""
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
    return ("\\\n" + " " * indent).join(out)


def code(ident) -> str:
    """A record id as a code span. Ids are validated slugs (schema.ID_RE): no backtick, no line break."""
    ident = str(ident)
    return f"`{ident}`" if S.ID_RE.fullmatch(ident) or re.fullmatch(r"sha256:[0-9a-f]{64}", ident) else md(ident)


def unescape(text) -> str:
    """The inverse of `md` on its output (for tests and the attack): the text a renderer shows."""
    return re.sub(r"\\(.)", r"\1", re.sub(r"\\\n *", "\n", text))


def _scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _value(value, indent) -> str:
    """One JSON scalar as inert Markdown: ids and digests as code, true/false/null as in JSON, text through `md`."""
    if value is None or isinstance(value, bool):
        return {None: "null", True: "true", False: "false"}[value]
    if isinstance(value, str) and (S.ID_RE.fullmatch(value) or re.fullmatch(r"sha256:[0-9a-f]{64}", value)):
        return code(value)
    return md(value, indent)


def tree(value, depth=0) -> list:
    """Every field of a JSON object or list as a nested Markdown list (inert): `- field: value`, a nested list for an
    object or list, `none` for an empty one. An object inside a list is one item, headed by its first plain field. The
    Markdown handoff carries what the JSON carries this way, field for field (docs/product-design.md)."""
    pad, col = "  " * depth, 2 * depth + 2
    out = []
    if isinstance(value, dict):
        for key, v in value.items():
            if _scalar(v):
                out.append(f"{pad}- {md(key, col)}: {_value(v, col)}")
            elif not v:
                out.append(f"{pad}- {md(key, col)}: none")
            else:
                out += [f"{pad}- {md(key, col)}:"] + tree(v, depth + 1)
    elif isinstance(value, list):
        for n, v in enumerate(value, 1):
            if _scalar(v):
                out.append(f"{pad}- {_value(v, col)}")
            elif isinstance(v, dict) and v:
                head = next((k for k, x in v.items() if _scalar(x)), None)
                out.append(f"{pad}- " + (f"{md(head, col)}: {_value(v[head], col)}" if head is not None else f"Item {n}"))
                out += tree({k: x for k, x in v.items() if k != head}, depth + 1)
            elif not v:
                out.append(f"{pad}- none")
            else:
                out += [f"{pad}- Item {n}:"] + tree(v, depth + 1)
    return out


def record_md(rec, skip=()) -> list:
    """One record: `- id: title`, then every other field (`tree`)."""
    head = f"- {code(rec['id'])}" + (f": {md(rec['title'])}" if rec.get("title") else "")
    return [head] + tree({k: v for k, v in rec.items() if k not in ("id", "title") + tuple(skip)}, 1)


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
    """The records a case names: its dependencies, the dependency closure of its rule, scenario, decision and contract
    dependencies, its actor and its implementation-area sources, as id -> (kind, record), in id order. A dependency on
    a slice (or a case, journey or measurement) is a delivery dependency: it is named, but its closure is that slice's
    scope, not this case's, so it is not followed."""
    ids = set(case["dependencies"])
    for dep in case["dependencies"]:
        if idx[dep][0] in P.CASE_DEP_KINDS:
            ids |= set(P.closure(docs, dep, idx))
    if "actor" in case:
        ids.add(case["actor"])
    ids |= {a["source"] for a in case.get("implementation_areas", []) if "source" in a}
    return {i: idx[i] for i in sorted(ids)}


SCOPE_KINDS = ("scenario", "rule", "decision", "contract", "claim", "actor")
SOURCE_FIELDS = ("id", "title", "locator", "revision", "digest", "anchor", "excerpt", "captured")


def case_scope(docs, case, idx) -> tuple:
    """(refs, scope, sources): the records the case names (`_refs`); the scenarios, rules, decisions (without their
    acceptance event), contracts, claims and actors among them, plus every claim an included rule or contract cites;
    and the sources any of them cites, with their excerpts and anchors. The one definition of a case's scope: the
    case Markdown, cases.json, the publication digest and the packet all read it."""
    refs = _refs(docs, case, idx)
    scope = {}
    for kind in SCOPE_KINDS:
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
    for cl in scope["claims"]:
        cited |= set(cl.get("sources", []))
    sources = [{k: idx[s][1][k] for k in SOURCE_FIELDS if k in idx[s][1]}
               for s in sorted(i for i in cited if idx[i][0] == "source")]
    return refs, scope, sources


def delivery_states(docs, refs) -> dict:
    """The state now of each delivery dependency among `refs` (a slice: its effective readiness; a case: its case
    state), as the packet's links show it. Only direct dependencies are slices or cases (`_refs` follows no slice)."""
    kinds = {k for k, _ in refs.values()}
    if not kinds & {"slice", "case"}:
        return {}
    report = P.readiness(docs)
    cases = P.case_report(docs, report) if "case" in kinds else {}
    return {i: report[i]["effective"] if k == "slice" else cases[i]["state"]
            for i, (k, _) in refs.items() if k in ("slice", "case")}


def references(refs, states) -> dict:
    """cases.json's `references` of a case: kind, title and record fingerprint of every record it names, and for a
    delivery dependency (a slice or a case) its state now (`delivery_states`)."""
    return {i: dict({"kind": k, "title": rec.get("title"), "fingerprint": P.record_fingerprint(rec, k)},
                    **({"state": states[i]} if i in states else {}))
            for i, (k, rec) in refs.items()}


def published(docs, case, idx) -> dict:
    """What an export publishes about a case, the provenance and the publication mapping aside: the case (title and
    reopen events included), its references (with the state of each delivery dependency, which the draft shows) and
    its scope (titles included). The Markdown draft and the cases.json entry render nothing else, so the digest of
    this covers every field a ticket shows. The mapping (`publication`) is left out: recording where a case was
    published does not change what was published."""
    refs, scope, sources = case_scope(docs, case, idx)
    return {"case": _without(case, "publication"), "references": references(refs, delivery_states(docs, refs)),
            "scope": scope, "sources": sources}


def case_digest(docs, case, idx) -> str:
    """The publication digest of a case: of `published`. A revision that changes nothing an export shows (a note, a
    record outside the case's scope) leaves it unchanged, so a re-export proposes no update; a retitle changes it.
    Readiness fingerprints leave titles out (PD-B); this digest does not, because a ticket shows them."""
    return P.digest(published(docs, case, idx))


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


# The case fields the Markdown renders under their own heading; every other field is rendered by `tree` under
# "Case details", so a field the schema adds later is never dropped from the handoff.
CASE_HEADED = ("id", "title", "slice", "dependencies", "outcome", "scope", "exclusions", "acceptance_examples",
               "publication")
# The scope sections: scope key, heading.
SCOPE_HEADINGS = (("scenarios", "Scenarios"), ("rules", "Rules (with their examples)"), ("decisions", "Decisions"),
                  ("contracts", "Contracts (status is the confidence: proposed or existing-verified)"),
                  ("claims", "Claims cited"), ("actors", "Actors"))


def _case_body(case, refs, scope, sources, states) -> list:
    """The case's own content and its whole scope, every field: the part of the case Markdown and of the packet that
    is the same. `refs` maps each dependency and the actor to (kind, record or link); `states` gives each delivery
    dependency's state now (a slice's readiness, a case's state)."""
    actor = refs[case["actor"]][1].get("title") or case["actor"] if "actor" in case else None
    out = ["## Outcome", "", (f"{md(actor)}: " if actor else "") + md(case.get("outcome", "Not recorded.")), ""]
    out += ["## Scope", ""] + _lines(case.get("scope", [])) + [""]
    out += ["## Exclusions", ""] + _lines(case.get("exclusions", [])) + [""]
    out += ["## Acceptance examples", ""] + _examples(case["acceptance_examples"]) + [""]
    details = {k: v for k, v in case.items() if k not in CASE_HEADED}
    out += ["## Case details", "", "Every other field of the case (feasibility findings, unresolved non-blocking "
            "matters, QA, completion evidence, suggested implementation areas, the revision it was prepared from)."]
    out += [""] + tree(details) + [""]
    out += ["## Dependencies", ""]
    for d in case["dependencies"]:
        kind, rec = refs[d]
        out.append(f"- {code(d)} \\({md(kind)}\\)" + (f": {md(rec['title'])}" if rec.get("title") else "")
                   + ("" if kind in SCOPE_KINDS or kind == "source" else
                      " \\(linked: a delivery dependency, not this case's scope"
                      + (f"; state now: {md(states[d])}" if d in states else "") + "\\)"))
    out.append("")
    for key, heading in SCOPE_HEADINGS:
        if scope[key]:
            out += [f"## {heading}", ""]
            for rec in scope[key]:
                out += record_md(rec)
            out.append("")
    if sources:
        out += ["## Sources (excerpts and anchors)", ""]
        for s in sources:
            out += record_md(s)
        out.append("")
    return out


def _publications(pubs) -> list:
    return [f"- {md(p['system'])} {md(p['external_id'])}, published from revision {p['package_revision']} at digest "
            f"{code(p['digest']) if 'digest' in p else 'not recorded'} by {md(p.get('by', 'not recorded'))} on "
            f"{md(p.get('date', 'not recorded'))}" for p in pubs] or ["- Not published."]


def render_case(docs, case, idx, provenance, proposal, digest) -> str:
    """One case draft as Markdown: the case, everything in its scope, its publications and the proposal. Every field
    of its cases.json entry is in it but the per-reference fingerprints (machine data, in cases.json). Deterministic:
    the same package content gives the same bytes."""
    refs, scope, sources = case_scope(docs, case, idx)
    states = delivery_states(docs, refs)
    out = [f"# {md(case['title'])}", "",
           f"Case {code(case['id'])}, slice {code(case['slice'])}. Package {code(provenance['package'])} revision "
           f"{provenance['package_revision']}, content {code(provenance['content_fingerprint'])}; slice fingerprint "
           f"{code(provenance['slice_fingerprint'])}; publication digest {code(digest)}. Prepared from revision "
           f"{case['source']['package_revision']}. File {md(case['id'] + '.md')}; per-record fingerprints of the "
           "references are in cases.json.", ""]
    out += _case_body(case, refs, scope, sources, states)
    out += ["## Publication", ""] + _publications(case.get("publication", []))
    out.append(f"- Proposal: {proposal['action']}")
    for s in proposal["systems"]:
        at = code(s["published_digest"]) if s["published_digest"] else "null"
        out.append(f"  - {md(s['system'])} {md(s['external_id'])}: {s['action']} \\(published from revision "
                   f"{s['published_revision']} at digest {at}\\)")
    if proposal.get("check"):
        out.append(f"  - Check: {md(proposal['check'], 4)}")
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
        refs = _refs(docs, case, idx)
        files[name] = render_case(docs, case, idx, provenance, proposal, digest).encode("utf-8")
        entries.append({"key": case["id"], "file": name, "digest": digest,
                        "case": _without(case, "publication", "reopened"),
                        "references": references(refs, delivery_states(docs, refs)),
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

_DOC_OF = {kind: doc for doc, kind in P.COLLECTIONS.values()}


def _named(rec, kind) -> set:
    """Every id a record names (dependency or not): the schema's x-ref fields."""
    _, _, refs = S.validate(P.load_schema(), rec, entry=kind)
    return {value for _, _, _, value in refs if isinstance(value, str)}


def build_packet(docs, case_id) -> dict:
    """The packet of one case as data (see `packet`). Refuses a stale case first (naming its blockers too), then a
    blocked one, by name."""
    idx = P.index(docs)
    if idx.get(case_id, (None,))[0] != "case":
        _refuse("case.unknown", f"{case_id!r} is not a case of this package")
    case = idx[case_id][1]
    report = P.readiness(docs)
    states = P.case_report(docs, report)
    state = states[case_id]
    if state["stale"]:
        also = ("; it is also blocked: " + "; ".join(_blocker_text(b) for b in state["blockers"])
                if state["blockers"] else "")
        _refuse("case.stale", f"{case_id} is stale: " + "; ".join(_blocker_text(b) for b in state["stale"]) + also
                + ". Prepare it again from the current package before handing it out. Nothing was written.",
                details={"case": case_id, "stale": state["stale"], "blockers": state["blockers"]})
    if state["blockers"]:
        _refuse("case.blocked", f"{case_id} is blocked: " + "; ".join(_blocker_text(b) for b in state["blockers"])
                + ". Nothing was written.", details={"case": case_id, "blockers": state["blockers"]})
    m = docs["manifest"]
    refs, scope, sources = case_scope(docs, case, idx)
    inside = {r["id"] for coll in scope.values() for r in coll} | {s["id"] for s in sources} | {case_id}
    sid = case["slice"]
    deps = set(case["dependencies"])
    named = set()
    for key, coll in scope.items():
        for r in coll:
            named |= _named(r, key[:-1])
    for s in sources:
        named |= _named(idx[s["id"]][1], "source")
    linked = (set(P.closure(docs, sid, idx)) | {sid} | set(idx[sid][1].get("depends_on", [])) | deps | named
              | {c["id"] for c in docs["design"]["cases"] if c["slice"] == sid})
    links = []
    for i in sorted(linked):
        if i in inside or i not in idx:
            continue
        kind, rec = idx[i]
        link = {"id": i, "kind": kind, "title": rec.get("title"), "in": P.DOCUMENTS[_DOC_OF[kind]][0],
                "relation": "delivery-dependency" if i in deps else "context"}
        if kind == "slice":
            link["state"] = report[i]["effective"]
        elif kind == "case":
            link["state"] = states[i]["state"]
        links.append(link)
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
    """The packet as Markdown (inert, deterministic), from the packet's JSON alone: every field of it, the verification
    requirements gathered as a checklist, and the links with their state."""
    pv, case = pk["provenance"], pk["case"]
    refs = {l["id"]: (l["kind"], {"title": l["title"]}) for l in pk["links"]}
    refs.update({s["id"]: ("source", s) for s in pk["sources"]})
    refs.update({r["id"]: (key[:-1], r) for key, coll in pk["scope"].items() for r in coll})
    out = [f"# Worker packet: {md(case['title'])}", "",
           f"Format {md(pk['schema'])} version {pk['schema_version']}. Case {code(pv['case'])}, slice "
           f"{code(pv['slice'])}. Package {code(pv['package'])} revision {pv['package_revision']}, content "
           f"{code(pv['content_fingerprint'])}; slice fingerprint {code(pv['slice_fingerprint'])}; case digest "
           f"{code(pv['case_digest'])}.", "", md(pk["note"]), ""]
    out += _case_body(case, refs, pk["scope"], pk["sources"], {l["id"]: l["state"] for l in pk["links"] if "state" in l})
    v = pk["verification"]
    out += ["## Verification", "",
            "The acceptance examples above, each scenario's expected results and each rule's examples, and:"]
    out += [""] + (tree({k: v[k] for k in ("qa", "completion_evidence")}) or ["- None recorded."]) + [""]
    out += ["## Publication", ""] + _publications(pk["publication"]) + [""]
    out += ["## Links (read in the package when needed)", ""]
    out += [f"- {code(l['id'])} \({md(l['kind'])}" + (f", {md(l['state'])}" if "state" in l else "")
            + f", {md(l['relation'])}, in {md(l['in'])}\)" + (f": {md(l['title'])}" if l["title"] else "")
            for l in pk["links"]] or ["- None."]
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
