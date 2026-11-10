"""Product design package: load, validate, fingerprint, and evaluate slice readiness (read-only).

A package is a directory with three JSON documents (docs/product-design.md, "Package contract"):

  package.json   the manifest: identity, schema version, revision, activity, next action, source roots, builds
  design.json    semantic records: sources, claims, actors, journeys, rules, scenarios, questions, decisions,
                 contracts, slices, cases, measurements
  reviews.json   review bookkeeping: notes (feedback), inputs (review answers and proposals), reviews

`validate_docs(docs)` returns a list of schema.Issue; an empty list is a structurally valid package. Validity says nothing
about whether the business rules are right. `readiness(docs)` reports each slice's stored state, current fingerprint,
staleness and blockers, derived from the records and their dependency links; it never changes anything.

Nothing here writes, runs, or renders record text: note text, sources and imported data are data only.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict, List

from . import schema as S

ROOT = Path(__file__).resolve().parent.parent.parent
SCHEMA_PATH = ROOT / "templates/product-design/schema/package-v1.schema.json"
SUPPORTED_VERSIONS = (1,)

# document key -> (file name, schema name, $defs entry)
DOCUMENTS = {
    "manifest": ("package.json", "tp-product-package", "manifest"),
    "design": ("design.json", "tp-product-design", "design"),
    "reviews": ("reviews.json", "tp-product-reviews", "reviews"),
}
# collection -> (document key, record kind). The record kind is also the record's $defs entry and its id prefix.
COLLECTIONS = {
    "sources": ("design", "source"), "claims": ("design", "claim"), "actors": ("design", "actor"),
    "journeys": ("design", "journey"), "rules": ("design", "rule"), "scenarios": ("design", "scenario"),
    "questions": ("design", "question"), "decisions": ("design", "decision"), "contracts": ("design", "contract"),
    "slices": ("design", "slice"), "cases": ("design", "case"), "measurements": ("design", "measurement"),
    "notes": ("reviews", "note"), "inputs": ("reviews", "input"), "reviews": ("reviews", "review"),
}
ACCEPTED_STATES = ("ready-for-case-preparation", "implemented-verified")
MAX_FILE_BYTES = 8 * 1024 * 1024

_schema_cache = None


def load_schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        S.check_schema(data)
        _schema_cache = data
    return _schema_cache


def read_docs(package_dir) -> tuple:
    """Read the three documents. Returns (docs, issues); a missing, oversized or unparsable file is an issue."""
    docs, issues = {}, []
    for key, (name, _, _) in DOCUMENTS.items():
        path = Path(package_dir) / name
        try:
            if path.is_symlink():
                raise OSError("is a symbolic link")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise OSError(f"larger than {MAX_FILE_BYTES} bytes")
            docs[key] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            issues.append(S.Issue("file.missing", (name,), "file is missing"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            issues.append(S.Issue("file.unreadable", (name,), str(exc)))
    return docs, issues


def validate(package_dir) -> List[S.Issue]:
    docs, issues = read_docs(package_dir)
    return issues or validate_docs(docs)


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def records(docs):
    """Yield (collection, index, kind, record) for every record of every collection present."""
    for coll, (doc, kind) in COLLECTIONS.items():
        items = docs.get(doc, {}).get(coll) if isinstance(docs.get(doc), dict) else None
        if isinstance(items, list):
            for i, rec in enumerate(items):
                yield coll, i, kind, rec


def index(docs) -> Dict[str, tuple]:
    """id -> (kind, record), first occurrence wins (duplicates are reported by validate_docs)."""
    out = {}
    for _, _, kind, rec in records(docs):
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out.setdefault(rec["id"], (kind, rec))
    return out


def semantic(record: dict, kind: str) -> dict:
    """The record without its bookkeeping fields: what its fingerprint covers."""
    skip = S.bookkeeping_fields(load_schema(), kind)
    return {k: v for k, v in record.items() if k not in skip}


def record_fingerprint(record: dict, kind: str) -> str:
    return digest({"kind": kind, "record": semantic(record, kind)})


def dep_refs(record: dict, kind: str) -> List[str]:
    """The ids a record names through x-dep references, in field order."""
    _, _, refs = S.validate(load_schema(), record, entry=kind)
    return [value for _, _, dep, value in refs if dep and isinstance(value, str)]


def closure(docs, start_id: str, idx=None) -> List[str]:
    """Sorted ids reachable from start_id through x-dep references (start excluded). Unknown ids are skipped."""
    idx = idx if idx is not None else index(docs)
    seen, todo = set(), [start_id]
    while todo:
        cur = todo.pop()
        if cur not in idx:
            continue
        for ref in dep_refs(idx[cur][1], idx[cur][0]):
            if ref != start_id and ref not in seen and ref in idx:
                seen.add(ref)
                todo.append(ref)
    return sorted(seen)


def slice_fingerprint(docs, slice_id: str, idx=None) -> str:
    """Fingerprint of a slice's semantic content and of every record in its dependency closure."""
    idx = idx if idx is not None else index(docs)
    kind, rec = idx[slice_id]
    deps = [[ident, record_fingerprint(idx[ident][1], idx[ident][0])] for ident in closure(docs, slice_id, idx)]
    return digest({"slice": semantic(rec, kind), "deps": deps})


# ---------------------------------------------------------------- validation

def validate_docs(docs: dict) -> List[S.Issue]:
    issues = _versions(docs)
    if issues:
        return issues
    schema = load_schema()
    ids, refs = [], []
    for key, (name, _, entry) in DOCUMENTS.items():
        found, doc_ids, doc_refs = S.validate(schema, docs[key], entry=entry, path=(name,))
        issues += found
        ids += doc_ids
        refs += doc_refs
    package_id = docs["manifest"].get("id")
    for key in ("design", "reviews"):
        if docs[key].get("package") != package_id:
            issues.append(S.Issue("package.mismatch", (DOCUMENTS[key][0], "package"),
                                  f"{docs[key].get('package')!r} is not the manifest id {package_id!r}"))
    for coll, i, _, rec in records(docs):
        if coll == "notes" and isinstance(rec, dict) and "package" in rec and rec["package"] != package_id:
            issues.append(S.Issue("package.mismatch", ("reviews.json", "notes", i, "package"),
                                  f"note belongs to {rec['package']!r}, not {package_id!r}"))
    kinds = {}
    for path, kind, value in ids:
        if value in kinds:
            issues.append(S.Issue("id.duplicate", path, f"duplicate id {value!r} (first at {S.format_path(kinds[value][0])})"))
        else:
            kinds[value] = (path, kind)
    for path, allowed, _, value in refs:
        if value not in kinds:
            issues.append(S.Issue("ref.missing", path, f"{value!r} does not exist"))
        elif "*" not in allowed and kinds[value][1] not in allowed:
            issues.append(S.Issue("ref.kind", path, f"{value!r} is a {kinds[value][1]}, expected {' or '.join(allowed)}"))
    if issues:
        return issues
    return _invariants(docs)


def _versions(docs) -> List[S.Issue]:
    issues = []
    for key, (name, schema_name, _) in DOCUMENTS.items():
        doc = docs.get(key)
        if not isinstance(doc, dict):
            issues.append(S.Issue("file.shape", (name,), "must be a JSON object"))
            continue
        if doc.get("schema") != schema_name:
            issues.append(S.Issue("version.schema", (name, "schema"), f"must be {schema_name!r}"))
        version = doc.get("schema_version")
        if type(version) is not int or version not in SUPPORTED_VERSIONS:  # 1.0 and True compare equal to 1
            issues.append(S.Issue("version.unsupported", (name, "schema_version"),
                                  f"{version!r} is not a supported version ({', '.join(map(str, SUPPORTED_VERSIONS))})"))
    return issues


def _invariants(docs) -> List[S.Issue]:
    """Rules the schema cannot state. Runs only on a package whose schema, ids and references are valid."""
    issues = []
    idx = index(docs)

    def add(code, coll, i, field, message):
        issues.append(S.Issue(code, (DOCUMENTS[COLLECTIONS[coll][0]][0], coll, i) + ((field,) if field else ()), message))

    for coll, i, kind, rec in records(docs):
        rid = rec["id"]
        if kind in ("rule", "decision"):
            succ, pred = rec.get("superseded_by"), rec.get("supersedes")
            if rec["status"] == "superseded" and not succ:
                add("status.supersession", coll, i, "superseded_by", "a superseded record names its successor")
            if rec["status"] != "superseded" and succ:
                add("status.supersession", coll, i, "superseded_by", f"only a superseded {kind} has a successor")
            if rid in (succ, pred):
                add("status.supersession", coll, i, None, f"{rid} cannot supersede itself")
            elif succ and idx[succ][1].get("supersedes") != rid:
                add("status.supersession", coll, i, "superseded_by", f"{succ} does not say it supersedes {rid}")
            elif pred and idx[pred][1].get("superseded_by") != rid:
                add("status.supersession", coll, i, "supersedes", f"{pred} does not name {rid} as its successor")
        if kind == "decision":
            if rec["status"] == "accepted" and "acceptance" not in rec:
                add("status.acceptance", coll, i, "acceptance", "an accepted decision records its local acceptance")
            if rec["status"] == "proposed" and "acceptance" in rec:
                add("status.acceptance", coll, i, "acceptance", "a proposed decision has no acceptance")
        if kind == "question":
            st = rec["status"]
            need = {"answered": "decision", "accepted-uncertainty": "uncertainty", "withdrawn": "withdrawn_reason"}
            for status, field in need.items():
                if st == status and field not in rec:
                    add("status.question", coll, i, field, f"a {status} question records {field}")
                if st != status and field in rec:
                    add("status.question", coll, i, field, f"only a {status} question records {field}")
            if rec["blocking"] and not rec["affected_slices"]:
                add("status.question", coll, i, "affected_slices", "a blocking question names the slices it blocks")
            d = rec.get("decision")
            if d and idx[d][1].get("question") not in (None, rid):
                add("status.question", coll, i, "decision", f"{d} answers {idx[d][1]['question']}, not {rid}")
        if kind == "claim" and rec["status"] in ("observed-current", "documented-requirement") and not rec["sources"]:
            add("claim.provenance", coll, i, "sources", f"an {rec['status']} claim cites at least one source")
        if kind == "source" and "revision" not in rec and "digest" not in rec:
            add("source.version", coll, i, None, "a source records its revision or a content digest")
        if kind == "contract":
            need = "entities" if rec["kind"] == "data" else "operations"
            if not rec.get(need):
                add("contract.shape", coll, i, need, f"a {rec['kind']} contract lists its {need}")
        if kind == "slice":
            r = rec["readiness"]
            acc = r.get("acceptance")
            if r["state"] in ACCEPTED_STATES and not acc:
                add("slice.acceptance", coll, i, "readiness", f"a {r['state']} slice records its acceptance")
            elif r["state"] not in ACCEPTED_STATES and acc:
                add("slice.acceptance", coll, i, "readiness", f"a {r['state']} slice has no acceptance")
            elif acc:
                review = idx[acc["review"]][1]
                if review["verdict"] != "accept" or rid not in review["scope"] or review["fingerprint"] != acc["fingerprint"]:
                    add("slice.acceptance", coll, i, "readiness",
                        f"{acc['review']} must accept {rid} at the acceptance fingerprint")
        if kind == "input":
            if rec["status"] == "pending" and "reconciliation" in rec:
                add("input.reconciliation", coll, i, "reconciliation", "a pending input has no reconciliation")
            if rec["status"] != "pending" and "reconciliation" not in rec:
                add("input.reconciliation", coll, i, "reconciliation",
                    f"a {rec['status']} input records its local reconciliation")
    return issues


# ---------------------------------------------------------------- readiness

def decision_accepted(rec: dict) -> bool:
    """Accepted, and unchanged since its local acceptance."""
    return (rec.get("status") == "accepted" and "acceptance" in rec
            and rec["acceptance"]["fingerprint"] == record_fingerprint(rec, "decision"))


def question_resolved(rec: dict, idx) -> bool:
    if rec["status"] in ("accepted-uncertainty", "withdrawn"):
        return True
    return rec["status"] == "answered" and decision_accepted(idx[rec["decision"]][1])


def readiness(docs) -> Dict[str, dict]:
    """Per slice: state, fingerprint, stale, blockers, effective. Call only on a valid package."""
    idx = index(docs)
    out = {}
    for rec in docs["design"]["slices"]:
        sid, state = rec["id"], rec["readiness"]["state"]
        fp = slice_fingerprint(docs, sid, idx)
        blockers = []
        for q in docs["design"]["questions"]:
            if q["blocking"] and sid in q["affected_slices"] and not question_resolved(q, idx):
                blockers.append({"code": "question.unresolved", "id": q["id"],
                                 "reason": f"{q['id']} is {q['status']}" + (
                                     f" and {q['decision']} is not accepted" if q["status"] == "answered" else "")})
        for dep in closure(docs, sid, idx):
            kind, d = idx[dep]
            if kind in ("rule", "decision") and d["status"] == "superseded":
                blockers.append({"code": "dependency.superseded", "id": dep, "reason": f"{dep} is superseded by {d['superseded_by']}"})
            elif state in ACCEPTED_STATES and kind == "rule" and d["status"] != "accepted":
                blockers.append({"code": "rule.not-accepted", "id": dep, "reason": f"{dep} is {d['status']}"})
            elif state in ACCEPTED_STATES and kind == "rule" and not d["examples"]:
                blockers.append({"code": "rule.no-examples", "id": dep, "reason": f"{dep} is accepted without examples"})
            elif state in ACCEPTED_STATES and kind == "decision" and not decision_accepted(d):
                blockers.append({"code": "decision.not-accepted", "id": dep,
                                 "reason": f"{dep} is {d['status']}" if d["status"] != "accepted" else f"{dep} changed after its acceptance"})
        acc = rec["readiness"].get("acceptance")
        stale = bool(acc) and acc["fingerprint"] != fp
        effective = "blocked" if blockers else "stale" if stale else state
        out[sid] = {"state": state, "fingerprint": fp, "stale": stale, "blockers": blockers, "effective": effective}
    return out
