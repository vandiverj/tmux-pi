"""Product design package changes: changesets (`apply`), feedback ingestion (`ingest`) and impact.

Every change goes through one transaction (store.lock + store.commit): validated in full before anything is written,
committed atomically, recorded in history. A refused change writes nothing. See docs/product-design.md, "Changesets".

Acceptance is never a side effect. The fields in package.AUTHORITY (a decision's acceptance, a slice's readiness, an
input's status and reconciliation, a question's accepted uncertainty) are written only by the acceptance operations
below, each a local event naming who, when and on what evidence; `add`, `set` and `replace` refuse them, imported
bundles cannot carry them, and an external edit that changes them is not adopted.
"""
from __future__ import annotations

import copy
import json
from typing import Callable, Dict, List

from . import package as P
from . import schema as S
from . import store as T

MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_BUNDLE_RECORDS = 5000
GENERIC_KINDS_REFUSED = {"note": "notes enter only through feedback ingestion and are kept verbatim",
                         "input": "inputs enter only through feedback ingestion and are kept verbatim",
                         "review": "review records are append-only"}
MANIFEST_FIELDS = ("title", "owner", "objective", "source_roots", "activity", "next_action")


class OpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _need(op, *keys):
    missing = [k for k in keys if k not in op]
    if missing:
        raise OpError("op.missing", f"{op.get('op')} needs {', '.join(missing)}")


def _get(rec, *path):
    """rec[path...] or None: a record read in the middle of a changeset may be any shape until the result is validated."""
    for key in path:
        rec = rec.get(key) if isinstance(rec, dict) else None
    return rec


def load_json_input(raw, limit=None):
    """(value, problem) for JSON input given as bytes, text or an already-parsed value. The same limits apply to every
    form: at most `limit` bytes (of the UTF-8 text, or of the canonical JSON of a parsed value), package.MAX_DEPTH
    levels of nesting, and text that is valid Unicode (package.text_problem: no lone surrogate, whether it arrives as
    an escape, in text built in memory or in a parsed value). problem is None or (code suffix: "size" | "unreadable" |
    "depth" | "encoding", message)."""
    limit = MAX_BUNDLE_BYTES if limit is None else limit
    if isinstance(raw, (bytes, bytearray, str)):
        data = raw.encode("utf-8", "surrogatepass") if isinstance(raw, str) else bytes(raw)
        if len(data) > limit:
            return None, ("size", f"larger than {limit} bytes ({len(data)})")
        try:  # text is parsed as given: a lone surrogate in it is reported below, as for every form
            value = json.loads(raw if isinstance(raw, str) else data.decode("utf-8"))
        except RecursionError:
            return None, ("depth", f"nested deeper than {P.MAX_DEPTH} levels")
        except (UnicodeDecodeError, ValueError) as exc:
            return None, ("unreadable", f"not JSON: {exc}")
        depth = P.depth_problem(value)
        if depth:
            return None, ("depth", depth)
        text = P.text_problem(value)
        return (None, ("encoding", text)) if text else (value, None)
    depth = P.depth_problem(raw)  # first: canonical() recurses
    if depth:
        return None, ("depth", depth)
    try:
        text = P.text_problem(raw)
        size = None if text else len(P.canonical(raw))
    except (TypeError, ValueError, RecursionError) as exc:
        return None, ("unreadable", f"not a JSON value: {exc}")
    if text:
        return None, ("encoding", text)
    if size > limit:
        return None, ("size", f"larger than {limit} bytes as JSON ({size})")
    return raw, None


def _find(work, rid, kinds):
    idx = P.index(work)
    if rid not in idx:
        raise OpError("op.unknown-id", f"{rid!r} does not exist")
    kind, rec = idx[rid]
    if kind not in kinds:
        raise OpError("op.kind", f"{rid} is a {kind}, expected {' or '.join(kinds)}")
    return kind, rec


def _collection(kind):
    return next(c for c, (_, k) in P.COLLECTIONS.items() if k == kind)


def _refuse_authority(kind, fields, rid):
    held = sorted(set(fields) & set(P.AUTHORITY.get(kind, ())))
    if held:
        raise OpError("op.authority", f"{rid}: {', '.join(held)} records an acceptance event; use the acceptance operation")


def _generic_kind(kind, rid):
    if kind in GENERIC_KINDS_REFUSED:
        raise OpError("op.original", f"{rid}: {GENERIC_KINDS_REFUSED[kind]}")


# ---------------------------------------------------------------- operations

def op_add(work, op, ctx):
    _need(op, "collection", "record")
    coll, rec = op["collection"], op["record"]
    if coll not in P.COLLECTIONS or not isinstance(rec, dict):
        raise OpError("op.shape", f"add needs a known collection and a record object, got {coll!r}")
    kind = P.COLLECTIONS[coll][1]
    rid = rec.get("id")
    if not isinstance(rid, str):
        raise OpError("op.shape", f"add needs a record with a text id, got {rid!r}")
    if kind != "review":  # reviews are append-only: added, never edited
        _generic_kind(kind, rid)
    if rid in P.index(work):
        raise OpError("op.exists", f"{rid} already exists; ids are never reused")
    fields = set(rec) - ({"readiness"} if kind == "slice" else set())
    _refuse_authority(kind, fields, rid)
    if kind == "slice":
        r = rec.get("readiness")
        if not isinstance(r, dict) or set(r) - {"state", "evidence"} or not isinstance(r.get("state"), str) \
                or r["state"] in P.ACCEPTED_STATES:
            raise OpError("op.authority", f"{rid}: a new slice starts as draft or ready-for-human-review; "
                                          "use set-readiness to accept it")
    work[P.COLLECTIONS[coll][0]][coll].append(copy.deepcopy(rec))


def op_set(work, op, ctx):
    _need(op, "id")
    fields, unset = op.get("fields", {}), op.get("unset", [])
    if not isinstance(fields, dict) or not isinstance(unset, list) or not (fields or unset):
        raise OpError("op.shape", "set needs a fields object and/or an unset list")
    kind, rec = _find(work, op["id"], {k for _, k in P.COLLECTIONS.values()})
    _generic_kind(kind, op["id"])
    if "id" in fields or "id" in unset:
        raise OpError("op.identity", "an id is never changed; supersede the record instead")
    _refuse_authority(kind, set(fields) | set(unset), op["id"])
    rec.update(copy.deepcopy(fields))
    for name in unset:
        rec.pop(name, None)


def op_replace(work, op, ctx):
    _need(op, "id", "record")
    kind, rec = _find(work, op["id"], {k for _, k in P.COLLECTIONS.values()})
    _generic_kind(kind, op["id"])
    new = op["record"]
    if not isinstance(new, dict) or new.get("id") != op["id"]:
        raise OpError("op.identity", "replace keeps the record's id")
    _refuse_authority(kind, set(new), op["id"])
    keep = {f: rec[f] for f in P.AUTHORITY.get(kind, ()) if f in rec}
    rec.clear()
    rec.update(copy.deepcopy(new), **keep)


def op_supersede(work, op, ctx):
    _need(op, "id", "record")
    kind, old = _find(work, op["id"], {"rule", "decision"})
    new = op["record"]
    if not isinstance(new, dict) or not isinstance(new.get("id"), str):
        raise OpError("op.shape", "supersede needs the successor record, with a text id")
    if _get(old, "status") == "superseded":
        raise OpError("op.state", f"{op['id']} is already superseded by {old.get('superseded_by')}")
    _refuse_authority(kind, set(new), new.get("id"))
    if {"supersedes", "superseded_by"} & set(new):
        raise OpError("op.shape", "the successor's supersession links are set by the operation")
    if new.get("id") in P.index(work):
        raise OpError("op.exists", f"{new.get('id')} already exists; ids are never reused")
    work["design"][_collection(kind)].append(dict(copy.deepcopy(new), supersedes=op["id"]))
    old.update(status="superseded", superseded_by=new.get("id"))


def op_accept_decision(work, op, ctx):
    _need(op, "id", "by", "date", "evidence")
    _, rec = _find(work, op["id"], {"decision"})
    if _get(rec, "status") == "superseded":
        raise OpError("op.state", f"{op['id']} is superseded; accept its successor")
    rec["status"] = "accepted"
    rec.pop("acceptance", None)
    rec["acceptance"] = {"by": op["by"], "date": op["date"], "evidence": op["evidence"],
                         "fingerprint": P.record_fingerprint(rec, "decision"), "origin": "local"}


def op_set_readiness(work, op, ctx):
    _need(op, "slice", "state")
    sid, state = op["slice"], op["state"]
    _, rec = _find(work, sid, {"slice"})
    old = rec.get("readiness") if isinstance(rec.get("readiness"), dict) else {}
    idx = P.index(work)
    if state in ("draft", "ready-for-human-review"):
        new = {"state": state}
        if "evidence" in op:
            new["evidence"] = op["evidence"]
        if "review" in op:  # any review may bring a slice here, a model review included (decision 9)
            if idx.get(op["review"], (None,))[0] != "review":
                raise OpError("op.kind", f"{op['review']!r} is not a review")
            new["evidence"] = list(new.get("evidence", [])) + [op["review"]]
        rec["readiness"] = new
    elif state == "ready-for-case-preparation":
        _need(op, "by", "date", "review", "evidence")
        kind, review = idx.get(op["review"], (None, None))
        if kind != "review":
            raise OpError("op.kind", f"{op['review']!r} is not a review")
        reviewer = _get(review, "reviewer", "type")
        if reviewer != "human":
            raise OpError("acceptance.not-human", f"{op['review']} is a {reviewer!r} review: only a human "
                                                  "review accepts a slice; a model review brings it at most to "
                                                  "ready-for-human-review")
        fp = P.slice_fingerprint(work, sid, idx)
        scope = _get(review, "scope")
        if _get(review, "verdict") != "accept" or not isinstance(scope, list) or sid not in scope:
            raise OpError("acceptance.review", f"{op['review']} does not accept {sid}")
        if _get(review, "fingerprint") != fp:
            raise OpError("acceptance.fingerprint", f"{op['review']} reviewed {_get(review, 'fingerprint')!r}; {sid} is "
                                                    f"now {fp}: it needs a review of the current content")
        rec["readiness"] = {"state": state, "acceptance": {
            "by": op["by"], "date": op["date"], "review": op["review"], "evidence": op["evidence"],
            "fingerprint": fp, "origin": "local"}}
        ctx["accepted"].append(sid)
    elif state == "implemented-verified":
        _need(op, "by", "date", "delivery")
        if old.get("state") not in P.ACCEPTED_STATES or "acceptance" not in old:
            raise OpError("op.state", f"{sid} is {old.get('state')!r}: it is accepted for case preparation before it "
                                      "can be implemented-verified")
        rec["readiness"] = {"state": state, "acceptance": old["acceptance"], "delivery": {
            "by": op["by"], "date": op["date"], "evidence": op["delivery"], "origin": "local"}}
        ctx["accepted"].append(sid)
    else:
        raise OpError("op.shape", f"{state!r} is not a stored readiness state (blocked and stale are derived)")


def op_reconcile_input(work, op, ctx):
    _need(op, "id", "status", "by", "date", "outcome")
    _, rec = _find(work, op["id"], {"input"})
    if _get(rec, "status") != "pending":
        raise OpError("op.state", f"{op['id']} is already {_get(rec, 'status')!r}")
    if op["status"] not in ("reconciled", "declined"):
        raise OpError("op.shape", "status is reconciled or declined")
    event = {"by": op["by"], "date": op["date"], "outcome": op["outcome"], "origin": "local"}
    if "decision" in op:
        event["decision"] = op["decision"]  # a link only: it does not accept the decision
    rec.update(status=op["status"], reconciliation=event)


def op_accept_uncertainty(work, op, ctx):
    _need(op, "question", "scope", "rationale", "revisit_trigger", "by", "date")
    _, rec = _find(work, op["question"], {"question"})
    if _get(rec, "status") in ("accepted-uncertainty", "withdrawn"):
        raise OpError("op.state", f"{op['question']} is {rec['status']}")
    rec.pop("decision", None)
    rec.update(status="accepted-uncertainty", uncertainty={
        "scope": op["scope"], "rationale": op["rationale"], "revisit_trigger": op["revisit_trigger"],
        "accepted_by": op["by"], "date": op["date"]})


def op_set_disposition(work, op, ctx):
    _need(op, "note", "kind", "rationale", "by", "date")
    _, rec = _find(work, op["note"], {"note"})
    rec["disposition"] = {k: op[k] for k in ("kind", "rationale", "link", "by", "date") if k in op}


def op_set_manifest(work, op, ctx):
    _need(op, "fields")
    fields = op["fields"]
    bad = sorted(set(fields) - set(MANIFEST_FIELDS)) if isinstance(fields, dict) else ["(not an object)"]
    if bad or not fields:
        raise OpError("op.shape", f"set-manifest changes only {', '.join(MANIFEST_FIELDS)}; refused: {', '.join(bad)}")
    work["manifest"].update(copy.deepcopy(fields))


OPERATIONS = {
    "add": op_add, "set": op_set, "replace": op_replace, "supersede": op_supersede,
    "accept-decision": op_accept_decision, "set-readiness": op_set_readiness, "reconcile-input": op_reconcile_input,
    "accept-uncertainty": op_accept_uncertainty, "set-disposition": op_set_disposition, "set-manifest": op_set_manifest,
    "adopt-external-edit": None,  # handled by apply: it must be the only operation
}

# The fields each operation takes: (required, optional), each field -> its type. Checked for every operation before the
# package is read, so a wrong type is a refusal (op.missing, op.shape), never a crash. Unknown fields are refused.
TYPES = {"text": (lambda v: isinstance(v, str), "text"),
         "object": (lambda v: isinstance(v, dict), "an object"),
         "list": (lambda v: isinstance(v, list), "a list"),
         "texts": (lambda v: isinstance(v, list) and all(isinstance(e, str) for e in v), "a list of text")}
_EVENT = {"by": "text", "date": "text"}
SPECS = {
    "add": ({"collection": "text", "record": "object"}, {}),
    "set": ({"id": "text"}, {"fields": "object", "unset": "texts"}),
    "replace": ({"id": "text", "record": "object"}, {}),
    "supersede": ({"id": "text", "record": "object"}, {}),
    "accept-decision": (dict(_EVENT, id="text", evidence="texts"), {}),
    "set-readiness": ({"slice": "text", "state": "text"},
                      dict(_EVENT, review="text", evidence="texts", delivery="list")),
    "reconcile-input": (dict(_EVENT, id="text", status="text", outcome="text"), {"decision": "text"}),
    "accept-uncertainty": (dict(_EVENT, question="text", scope="text", rationale="text", revisit_trigger="text"), {}),
    "set-disposition": (dict(_EVENT, note="text", kind="text", rationale="text"), {"link": "text"}),
    "set-manifest": ({"fields": "object"}, {}),
    "adopt-external-edit": ({"by": "text", "reason": "text"}, {}),
}


def check_operation(n, op):
    """Refuse an operation whose fields are missing, unknown or of the wrong type (StoreError op.missing / op.shape)."""
    name = op["op"]
    required, optional = SPECS[name]

    def refuse(code, message):
        raise T.StoreError(code, f"operation {n} ({name}): {message}. Nothing was written.", details={"operation": n})

    missing = [k for k in required if k not in op]
    if missing:
        refuse("op.missing", f"needs {', '.join(missing)}")
    extra = sorted(set(op) - set(required) - set(optional) - {"op"})
    if extra:
        refuse("op.shape", f"unknown field(s) {', '.join(map(str, extra))}")
    for field, kind in list(required.items()) + list(optional.items()):
        test, what = TYPES[kind]
        if field in op and not test(op[field]):
            refuse("op.shape", f"{field} is {what}, got {type(op[field]).__name__}")


# ---------------------------------------------------------------- impact

def _all_refs(docs):
    """referenced id -> sorted ids of the records that name it (any reference, dependency or not)."""
    out: Dict[str, set] = {}
    for _, _, kind, rec in P.records(docs):
        _, _, refs = S.validate(P.load_schema(), rec, entry=kind)
        for _, _, _, value in refs:
            if isinstance(value, str) and value != rec.get("id"):
                out.setdefault(value, set()).add(rec["id"])
    return out


def impact(before, after) -> dict:
    """What a change did to readiness. Both packages must be valid.

    changed     design records whose semantic fingerprint changed, appeared or disappeared
    slices      per slice: effective readiness before and after, whether its fingerprint changed
    stale       slices that were not stale and now are: their acceptance no longer matches (they need a new review)
    unlinked    changed records no slice reaches through dependency links, with the records that name them: possible
                impact the package cannot follow (for human or model review; impact detection is not complete)
    cases       draft cases whose slice changed since they were prepared
    """
    fps = [{rec["id"]: P.record_fingerprint(rec, kind) for coll, _, kind, rec in P.records(d) if P.COLLECTIONS[coll][0] == "design"}
           for d in (before, after)]
    changed = sorted(i for i in set(fps[0]) | set(fps[1]) if fps[0].get(i) != fps[1].get(i))
    rep = [P.readiness(d) for d in (before, after)]
    slices = {sid: {"before": rep[0].get(sid, {}).get("effective"), "after": r["effective"],
                    "changed": rep[0].get(sid, {}).get("fingerprint") != r["fingerprint"]} for sid, r in rep[1].items()}
    stale = sorted(sid for sid, r in rep[1].items() if r["stale"] and not rep[0].get(sid, {}).get("stale"))
    reached = set()
    for d in (before, after):
        idx = P.index(d)
        for s in d["design"]["slices"]:
            reached |= set(P.closure(d, s["id"], idx)) | {s["id"]}
    refs = _all_refs(after)
    unlinked = [{"id": i, "named_by": sorted(refs.get(i, ()))} for i in changed if i not in reached]
    return {"changed": changed, "slices": slices, "stale": stale, "unlinked": unlinked,
            "cases": P.stale_cases(after, rep[1]),
            "content_fingerprint": {"before": P.content_fingerprint(before), "after": P.content_fingerprint(after)}}


# ---------------------------------------------------------------- the transaction

def _issues_text(issues):
    return "; ".join(f"{S.format_path(i.path)}: {i.message} [{i.code}]" for i in issues)


def _open(package_dir, expected_revision, adopting=False):
    """Under the lock: the committed package, or StoreError. Refuses an interrupted commit, an external edit (unless
    adopting) and a stale expected revision."""
    state = T.inspect(package_dir)
    if state["interrupted"] is not None:
        raise T.StoreError("package.interrupted", T.trust_problems(state)[0][1], exit=3)
    if any(i.code.startswith("file.") or i.code.startswith("control.") for i in state["issues"]):
        raise T.StoreError("package.unreadable", _issues_text(state["issues"]), exit=1,
                           details={"issues": [i._asdict() for i in state["issues"]]})
    if state["control"]:  # control records that disagree are not believed, and not written over, even by an adoption
        code, message = state["control"][0]
        raise T.StoreError(code, "; ".join(m for _, m in state["control"]), exit=3)
    commit = state["commit"]
    if (commit is None or state["external"]) and not adopting:
        code, message = T.trust_problems(state)[0]
        raise T.StoreError(code, message, exit=3, details={"external": state["external"]})
    # With no commit record (a package written by hand), only adoption is possible, and it is refused while the package
    # holds any acceptance record (authority_changed then lists every one): acceptances are recorded only by operations.
    # Without a commit record but with a history (commit.json deleted), the revision continues from the history, which
    # inspect has checked whole: the next entry follows its top, whatever the manifest says.
    current = commit["revision"] if commit else (state["history"][-1] if state["history"] else
                                                 state["docs"].get("manifest", {}).get("revision"))
    if not isinstance(current, int) or isinstance(current, bool):
        raise T.StoreError("package.invalid", "the manifest has no integer revision", exit=1)
    if expected_revision is not None and expected_revision != current and commit is None and state["history"]:
        raise T.StoreError("revision.conflict",
                           f"the package has no commit record and its history ends at revision {current} (the manifest "
                           f"says {_get(state['docs'], 'manifest', 'revision')!r}): an adoption continues from the history. "
                           f"Apply it with --expected-revision {current}, or restore .tp-product/commit.json from version "
                           "control. Nothing was written.",
                           exit=4, details={"expected": expected_revision, "current": current})
    if expected_revision is not None and expected_revision != current:
        raise T.StoreError("revision.conflict",
                           f"the package is at revision {current}, not {expected_revision}: another change was committed "
                           f"since you read it. Read `tp-product status` again, rebase the changeset on revision {current} "
                           "and apply it with that --expected-revision. Nothing was written.",
                           exit=4, details={"expected": expected_revision, "current": current})
    return state, current


def apply(package_dir, changeset, expected_revision, fault: Callable = T._noop) -> dict:
    """Apply a changeset atomically. Returns {"committed": bool, "revision": N, "impact": {...}} or raises StoreError.
    `changeset` is a parsed value, JSON text or bytes; every form has the same size and depth limits."""
    changeset, problem = load_json_input(changeset)
    if problem:
        raise T.StoreError("changeset.invalid", f"the changeset is {problem[1]}", details={"problem": problem[0]})
    problems = check_changeset(changeset)
    if problems:
        raise T.StoreError("changeset.invalid", "; ".join(problems), details={"problems": problems})
    if changeset.get("expected_revision", expected_revision) != expected_revision:
        raise T.StoreError("changeset.invalid", f"the changeset expects revision {changeset['expected_revision']}, "
                                                f"--expected-revision says {expected_revision}")
    ops = changeset["operations"]
    for n, op in enumerate(ops):
        check_operation(n, op)
    adopting = any(o["op"] == "adopt-external-edit" for o in ops)
    with T.lock(package_dir):
        state, current = _open(package_dir, expected_revision, adopting)
        docs = state["docs"]
        if adopting:
            return _adopt(package_dir, state, current, changeset, fault)
        if state["issues"]:  # a committed, unedited package is valid; say so plainly if it is not
            raise T.StoreError("package.invalid", _issues_text(state["issues"]))
        work = copy.deepcopy(docs)
        ctx = {"accepted": []}
        for n, op in enumerate(ops):
            try:
                OPERATIONS[op["op"]](work, op, ctx)
            except OpError as exc:
                raise T.StoreError(exc.code, f"operation {n} ({op['op']}): {exc}. Nothing was written.",
                                   details={"operation": n}) from None
        return _finish(package_dir, state, work, current, changeset, ctx, fault, before=docs)


def _finish(package_dir, state, work, current, changeset, ctx, fault, before):
    """Validate `work` as revision current+1 and commit it. `state` is the inspection the change was computed from: the
    commit refuses to replace a file that no longer matches it; `before` is the committed content to measure impact
    against, or None (an adopted edit: unknown)."""
    if before is not None and P.canonical(work) == P.canonical(before):
        return {"committed": False, "revision": current, "impact": None}
    work["manifest"]["revision"] = current + 1
    issues = P.validate_docs(work)
    if issues:
        raise T.StoreError("result.invalid", f"the result would not be a valid package: {_issues_text(issues)}. "
                                             "Nothing was written.", details={"issues": [i._asdict() for i in issues]})
    report = P.readiness(work)
    for sid in ctx["accepted"]:
        r = report[sid]
        if r["blockers"] or r["stale"]:
            why = "; ".join(b["reason"] for b in r["blockers"]) or "a later operation in the changeset changed its content"
            raise T.StoreError("acceptance.blocked", f"{sid} cannot be {r['state']}: {why}. Nothing was written.",
                               details={"slice": sid, "blockers": r["blockers"]})
    effect = impact(before, work) if before is not None else None
    entry = {"schema": "tp-product-history", "schema_version": 1, "base_revision": current, "committed": T.now(),
             "actor": changeset["actor"], "summary": changeset["summary"], "evidence": changeset.get("evidence", []),
             "operations": changeset["operations"],
             "impact": effect and {k: effect[k] for k in ("changed", "stale", "unlinked", "content_fingerprint")}}
    T.commit(package_dir, work, entry, fault=fault, base_digests=T.file_digests(state["docs"]),
             base_commit=state["commit_digest"])
    return {"committed": True, "revision": current + 1, "impact": effect}


def _adopt(package_dir, state, current, changeset, fault):
    """Make an external edit the committed content. Refused when the edit is invalid or changes an acceptance record.
    The revision continues from the commit record, whatever the edited manifest says. Impact is not computed: the content
    before the edit is not kept; readiness is derived from fingerprints, so an edited dependency still shows stale."""
    if len(changeset["operations"]) != 1:
        raise T.StoreError("changeset.invalid", "adopt-external-edit is the only operation of its changeset")
    if not state["external"]:
        raise T.StoreError("adopt.nothing", "there is no external edit to adopt")
    if state["issues"]:
        raise T.StoreError("adopt.invalid", f"the edited package is not valid: {_issues_text(state['issues'])}",
                           details={"issues": [i._asdict() for i in state["issues"]]})
    if state["authority_changed"]:
        raise T.StoreError("adopt.authority",
                           f"the edit changes acceptance records of {', '.join(state['authority_changed'])}; an acceptance "
                           "is recorded only by its operation. Restore those fields to their committed values, adopt "
                           "the rest, then record each acceptance with its operation. Nothing was written.",
                           details={"ids": state["authority_changed"]})
    if state["originals_changed"]:
        raise T.StoreError("adopt.original",
                           f"the edit removes or changes the original records {', '.join(state['originals_changed'])} "
                           "(imported notes and inputs, review records): originals are kept verbatim. Restore them, adopt "
                           "the rest, then record a disposition or reconciliation instead. Nothing was written.",
                           details={"ids": state["originals_changed"]})
    return _finish(package_dir, state, copy.deepcopy(state["docs"]), current, changeset, {"accepted": []}, fault,
                   before=None)


def check_changeset(cs) -> List[str]:
    if not isinstance(cs, dict):
        return ["a changeset is a JSON object"]
    out = []
    if cs.get("schema") != "tp-product-changeset":
        out.append("schema must be 'tp-product-changeset'")
    if type(cs.get("schema_version")) is not int or cs.get("schema_version") != 1:
        out.append(f"schema_version {cs.get('schema_version')!r} is not supported (1)")
    for field in ("actor", "summary"):
        if not isinstance(cs.get(field), str) or not cs[field].strip():
            out.append(f"{field} is required text")
    if not isinstance(cs.get("evidence", []), list) or not all(isinstance(e, str) for e in cs.get("evidence", [])):
        out.append("evidence is a list of text")
    if "expected_revision" in cs and type(cs["expected_revision"]) is not int:
        out.append("expected_revision is an integer")
    extra = sorted(set(cs) - {"schema", "schema_version", "package", "expected_revision", "actor", "summary", "evidence",
                              "operations"})
    if extra:
        out.append(f"unknown field(s): {', '.join(extra)}")
    ops = cs.get("operations")
    if not isinstance(ops, list) or not ops:
        out.append("operations is a non-empty list")
    else:
        for n, op in enumerate(ops):
            if not isinstance(op, dict) or not isinstance(op.get("op"), str) or op["op"] not in OPERATIONS:
                out.append(f"operation {n}: op must be one of {', '.join(OPERATIONS)}")
    return out


def load_changeset(path):
    """The parsed changeset file. A file that cannot be read is changeset.unreadable; one that is too large, too deep
    or not JSON is changeset.invalid (both exit 1)."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BUNDLE_BYTES + 1)
    except OSError as exc:
        raise T.StoreError("changeset.unreadable", f"cannot read the changeset {path}: {exc.strerror or exc}",
                           details={"path": str(path)}) from None
    value, problem = load_json_input(data)
    if problem:
        raise T.StoreError("changeset.invalid", f"{path} is {problem[1]}", details={"problem": problem[0]})
    return value


# ---------------------------------------------------------------- feedback ingestion

def anchor(note, docs, idx, content_fp) -> str:
    """Where a note stands against the current design: unresolved when its journey or scenario is gone; current when it
    was captured against the same semantic content; else changed-context. Its original anchors are kept unchanged."""
    for field in ("journey", "scenario"):
        value = note.get(field)
        if value is not None and idx.get(value, (None,))[0] != field:
            return "unresolved"
    return "current" if note["build"]["fingerprint"] == content_fp else "changed-context"


def check_bundle(docs, raw) -> dict:
    """Classify a feedback bundle against a package. Pure: returns {issues, conflicts, notes, inputs, duplicates}.

    issues      the bundle is refused: format, identity, version, size, field constraints, forged bookkeeping
    conflicts   an id the package (or the bundle itself) already holds with different content: refused
    notes/inputs  records new to the package, in bundle order
    duplicates  ids already present with the same content: importing them again is a no-op
    """
    out = {"issues": [], "conflicts": [], "notes": [], "inputs": [], "duplicates": [], "bundle": None}

    def issue(code, path, message):
        out["issues"].append(S.Issue(code, tuple(path), message))

    bundle, problem = load_json_input(raw)  # bytes, text or a parsed value: the same size and depth limits
    if problem:
        issue("bundle." + problem[0], (), problem[1])
        return out
    out["bundle"] = bundle
    if not isinstance(bundle, dict):
        issue("bundle.shape", (), "a feedback bundle is a JSON object")
        return out
    if bundle.get("schema") != "tp-product-feedback":
        issue("version.schema", ("schema",), "must be 'tp-product-feedback'")
    version = bundle.get("schema_version")
    if type(version) is not int or version not in P.SUPPORTED_VERSIONS:
        issue("version.unsupported", ("schema_version",), f"{version!r} is not a supported version")
    package_id = docs["manifest"]["id"]
    if bundle.get("package") != package_id:
        issue("package.mismatch", ("package",), f"bundle is for {bundle.get('package')!r}, not {package_id!r}")
    if out["issues"]:
        return out
    count = sum(len(v) for v in (bundle.get("notes"), bundle.get("inputs")) if isinstance(v, list))  # shape: schema below
    if count > MAX_BUNDLE_RECORDS:
        issue("bundle.size", (), f"{count} records; at most {MAX_BUNDLE_RECORDS}")
        return out
    found, _, refs = S.validate(P.load_schema(), bundle, entry="feedback")
    out["issues"] += found
    if found:
        return out
    idx = P.index(docs)
    for path, allowed, _, value in refs:
        if path[0] == "inputs" and idx.get(value, (None,))[0] not in allowed:
            issue("ref.missing", path, f"{value!r} is not a {' or '.join(allowed)} of this package")
    seen = {}
    for coll, kind in (("notes", "note"), ("inputs", "input")):
        for i, rec in enumerate(bundle[coll]):
            path = (coll, i)
            if kind == "note":
                if rec["package"] != package_id:
                    issue("package.mismatch", path + ("package",), f"note belongs to {rec['package']!r}")
                for field in ("anchor", "disposition"):
                    if field in rec:
                        issue("feedback.bookkeeping", path + (field,), f"an imported note has no {field}: it is set locally")
            if kind == "input" and (rec["status"] != "pending" or "reconciliation" in rec):
                issue("feedback.forged-authority", path + ("status",),
                      "an imported input is pending; only a local reconciliation event changes that")
            rid, sem = rec["id"], P.digest(P.semantic(rec, kind))
            if rid in seen:
                if seen[rid] != sem:
                    out["conflicts"].append({"id": rid, "with": "bundle", "reason": "the bundle holds two different records with this id"})
                continue
            seen[rid] = sem
            if rid in idx:
                if P.digest(P.semantic(idx[rid][1], kind)) == sem:
                    out["duplicates"].append(rid)
                else:
                    out["conflicts"].append({"id": rid, "with": "package",
                                             "reason": "the package holds a different record with this id"})
            else:
                out[coll].append(copy.deepcopy(rec))
    return out


def ingest(package_dir, raw, actor="tp-product import", expected_revision=None, fault: Callable = T._noop) -> dict:
    """Import a feedback bundle atomically. A repeat import is a no-op; any conflict or invalid field refuses the whole
    bundle and writes nothing. Returns {committed, revision, added, duplicates, anchors}."""
    with T.lock(package_dir):
        state, current = _open(package_dir, expected_revision)
        docs = state["docs"]
        if state["issues"]:
            raise T.StoreError("package.invalid", _issues_text(state["issues"]))
        got = check_bundle(docs, raw)
        if got["issues"]:
            raise T.StoreError("bundle.invalid", f"the bundle was refused: {_issues_text(got['issues'])}. Nothing was "
                                                 "written.", details={"issues": [i._asdict() for i in got["issues"]]})
        if got["conflicts"]:
            raise T.StoreError("bundle.conflict", "conflicting ids: " + ", ".join(
                f"{c['id']} ({c['reason']})" for c in got["conflicts"]) + ". Nothing was written; the originals in the "
                "package are unchanged.", details={"conflicts": got["conflicts"]})
        result = {"added": {"notes": [n["id"] for n in got["notes"]], "inputs": [i["id"] for i in got["inputs"]]},
                  "duplicates": got["duplicates"], "anchors": {}}
        if not got["notes"] and not got["inputs"]:
            return dict(result, committed=False, revision=current)
        work = copy.deepcopy(docs)
        idx, content_fp = P.index(work), P.content_fingerprint(work)
        for note in got["notes"]:
            note["anchor"] = anchor(note, work, idx, content_fp)
            result["anchors"].setdefault(note["anchor"], []).append(note["id"])
        work["reviews"]["notes"] += got["notes"]
        work["reviews"]["inputs"] += got["inputs"]
        cs = {"actor": actor, "summary": f"import feedback: {len(got['notes'])} note(s), {len(got['inputs'])} input(s)",
              "evidence": [], "operations": [{"op": "import-feedback", "bundle": P.digest(got["bundle"]),
                                              "notes": result["added"]["notes"], "inputs": result["added"]["inputs"]}]}
        done = _finish(package_dir, state, work, current, cs, {"accepted": []}, fault, before=docs)
        return dict(result, committed=done["committed"], revision=done["revision"])
