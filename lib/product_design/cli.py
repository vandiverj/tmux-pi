"""tp-product: deterministic product design package commands (init, status, validate, apply, build, import-feedback,
remap-anchor).

Exit status: 0 done; 1 invalid or unreadable package, changeset, bundle or prototype adapter (nothing written); 2 usage;
3 the package cannot be trusted yet (interrupted commit, uncommitted external edit, no commit record, control records
that disagree, a file changed by someone else during the commit) or a build file exists with other content; 4 stale write (--expected-revision is not the current revision);
5 another writer holds the lock. Reads (status, validate) never write and start nothing. With --json every refusal is
one JSON object {"ok": false, "code", "message", "details"} on stdout.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import changes as C
from . import package as P
from . import review as R
from . import schema as S
from . import store as T


def _issue_lines(issues):
    return [f"  {S.format_path(i.path)}: {i.message} [{i.code}]" for i in issues]


def status_report(package_dir) -> tuple:
    """(report dict, exit status). Read-only."""
    state = T.inspect(package_dir)
    docs = state["docs"]
    trust = [{"code": c, "message": m} for c, m in T.trust_problems(state)]
    rep = {"package": None, "revision": None, "trusted": not trust, "trust": trust, "issues": [i._asdict() for i in state["issues"]]}
    if state["issues"]:
        return rep, 1
    m = docs["manifest"]
    readiness = P.readiness(docs)
    idx = P.index(docs)
    notes, inputs = docs["reviews"]["notes"], docs["reviews"]["inputs"]
    anchors = {n["id"]: n.get("anchor") for n in notes}
    try:  # anchors as they stand now, against the registry a build would show (stored ones go stale until the next build)
        reg = R.package_registry(package_dir, docs)
        content_fp = P.content_fingerprint(docs)
        anchors = {n["id"]: C.anchor(n, docs, idx, content_fp, lambda t: R.known(reg, t)) for n in notes}
    except T.StoreError as exc:
        rep["anchor_warning"] = f"anchors are the stored ones: {exc}"
    rep.update({
        "package": m["id"], "title": m["title"], "revision": m["revision"],
        "committed_revision": None if state["commit"] is None else state["commit"]["revision"],
        "activity": m["activity"], "next_action": m["next_action"],
        "content_fingerprint": P.content_fingerprint(docs),
        "slices": {sid: {"state": r["state"], "effective": r["effective"], "stale": r["stale"],
                         "fingerprint": r["fingerprint"], "blockers": r["blockers"]} for sid, r in readiness.items()},
        "stale_cases": P.stale_cases(docs, readiness),
        "open_questions": [{"id": q["id"], "blocking": q["blocking"], "affected_slices": q["affected_slices"],
                            "status": q["status"]} for q in docs["design"]["questions"]
                           if not P.question_resolved(q, idx) and q["status"] != "withdrawn"],
        "pending": {
            "inputs": [i["id"] for i in inputs if i["status"] == "pending"],
            "notes_without_disposition": [n["id"] for n in notes if "disposition" not in n],
            "unresolved_anchors": [n["id"] for n in notes if anchors[n["id"]] == "unresolved"],
            "awaiting_human_review": [sid for sid, r in readiness.items()
                                      if r["state"] == "ready-for-human-review" and not r["blockers"]],
            "needs_re_review": [sid for sid, r in readiness.items() if r["stale"]],
        },
        "remapped": [{"note": n["id"], "original": n["target"]["id"], "target": n["remap"]["target"],
                      "reason": n["remap"]["reason"], "anchor": anchors[n["id"]]} for n in notes if "remap" in n],
    })
    return rep, (3 if trust else 0)


def _print_status(rep, out):
    if rep["package"] is None:
        print("invalid package:", file=out)
        for i in rep["issues"]:
            print(f"  {S.format_path(i['path'])}: {i['message']} [{i['code']}]", file=out)
        for t in rep["trust"]:
            print(f"WARNING {t['code']}: {t['message']}", file=out)
        return
    print(f"{rep['package']}  revision {rep['revision']}  activity {rep['activity']}", file=out)
    for t in rep["trust"]:
        print(f"WARNING {t['code']}: {t['message']} (readiness below is not trusted)", file=out)
    na = rep["next_action"]
    print(f"next action: {na['text']} ({na['owner']}, set {na['set']})", file=out)
    print("slices:", file=out)
    for sid, r in rep["slices"].items():
        line = f"  {sid}: {r['effective']}"
        if r["effective"] != r["state"]:
            line += f" (stored {r['state']})"
        print(line, file=out)
        for b in r["blockers"]:
            print(f"    blocker {b['code']} {b['id']}: {b['reason']}", file=out)
    for c in rep["stale_cases"]:
        print(f"stale case {c['id']}: {c['slice']} changed since it was prepared", file=out)
    for q in rep["open_questions"]:
        print(f"open question {q['id']}" + (f" (blocks {', '.join(q['affected_slices'])})" if q["blocking"] else ""), file=out)
    for key, ids in rep["pending"].items():
        if ids:
            print(f"pending {key.replace('_', ' ')}: {', '.join(ids)}", file=out)
    for r in rep["remapped"]:
        print(f"remapped {r['note']}: {r['original']} -> {r['target']} ({r['reason']})"
              + (" [the new target is gone: unresolved]" if r["anchor"] == "unresolved" else ""), file=out)
    if rep.get("anchor_warning"):
        print(f"WARNING {rep['anchor_warning']}", file=out)


def _print_build(r, out, as_json):
    if as_json:
        print(json.dumps(dict(r, ok=True), indent=2, ensure_ascii=False), file=out)
        return
    if r["up_to_date"]:
        print(f"up to date: {r['file']} shows this content (source {r['source_fingerprint']}); nothing was written", file=out)
    else:
        print(f"built {r['file']}" + (" (the file was already there, byte for byte)" if r["reused"] else ""), file=out)
        print(f"  recorded in the manifest: revision {r['revision']}", file=out)
    print(f"  content {r['content_fingerprint']}", file=out)
    print(f"  source  {r['source_fingerprint']}", file=out)
    for w in r["warnings"]:
        print(f"  warning: {w}", file=out)


def _print_import(r, out, as_json):
    if as_json:
        print(json.dumps(dict(r, ok=True), indent=2, ensure_ascii=False), file=out)
        return
    added = r["added"]
    if r["committed"]:
        print(f"imported {len(added['notes'])} note(s) and {len(added['inputs'])} answer(s)/proposal(s): "
              f"committed revision {r['revision']}", file=out)
    else:
        print(f"nothing new: the package stays at revision {r['revision']}", file=out)
    if r["duplicates"]:
        print(f"  already imported (unchanged): {', '.join(r['duplicates'])}", file=out)
    for kind in ("current", "changed-context", "remapped", "unresolved"):
        if r["anchors"].get(kind):
            print(f"  {kind}: {', '.join(r['anchors'][kind])}", file=out)
    for nid, kind in r.get("reanchored", {}).items():
        print(f"  re-anchored {nid}: now {kind}", file=out)
    if r["anchors"].get("unresolved"):
        print("  unresolved notes are kept; their target, journey or scenario is not in the package or its prototype: "
              "record a disposition for each (tp-product status lists them)", file=out)
    if added["inputs"]:
        print("  answers and proposals are pending until you reconcile them (reconcile-input)", file=out)
    print("  review sessions in the bundle (coverage, task outcomes) are not stored", file=out)


def _fail(exc, out, err, as_json):
    if as_json:
        print(json.dumps({"ok": False, "code": exc.code, "message": str(exc), "details": exc.details}, indent=2), file=out)
    print(f"tp-product: {exc.code}: {exc}", file=err)
    return exc.exit


def main(argv=None, out=None, err=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)

    class Parser(argparse.ArgumentParser):
        def error(self, message):  # a usage error is still exit 2; under --json it is also the JSON error object
            if "--json" in argv:
                print(json.dumps({"ok": False, "code": "usage", "message": message, "details": {}}, indent=2), file=out)
            super().error(message)

    p = Parser(prog="tp-product", description=__doc__.split("\n")[0],
                                epilog=__doc__.split("\n\n", 1)[1], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("init", help="create a package scaffold at revision 1; refuses to overwrite")
    c.add_argument("package_dir")
    c.add_argument("--id", required=True, help="package id: one lower-case slug, at most 64 characters")
    c.add_argument("--title", required=True)
    c.add_argument("--owner", default="Not set")
    c.add_argument("--objective", default="Not framed yet.")
    c = sub.add_parser("status", help="revision, slice readiness, blockers, stale cases, pending reviews, next action")
    c.add_argument("package_dir")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("validate", help="schema, ids, references, invariants, versions and commit state")
    c.add_argument("package_dir")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("apply", help="commit a changeset atomically, or finish an interrupted commit (--recover)")
    c.add_argument("package_dir")
    c.add_argument("changeset", nargs="?", help="changeset JSON file (docs/product-design.md, Changesets)")
    c.add_argument("--expected-revision", type=int, help="the revision the changeset was written against")
    c.add_argument("--recover", action="store_true", help="only finish or discard an interrupted commit")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("build", help="write the offline review page (builds/ID-rREV.html) and record it in the manifest")
    c.add_argument("package_dir")
    c.add_argument("--expected-revision", type=int, help="refuse unless the package is at this revision")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("import-feedback", help="import a feedback bundle exported by a review page; all or nothing")
    c.add_argument("package_dir")
    c.add_argument("bundle", help="the exported feedback bundle (.json)")
    c.add_argument("--expected-revision", type=int, help="refuse unless the package is at this revision")
    c.add_argument("--json", action="store_true")
    c = sub.add_parser("remap-anchor", help="anchor a note to another target of the review build, keeping the original")
    c.add_argument("package_dir")
    c.add_argument("note", help="the note id")
    c.add_argument("target", help="a target id the review build shows (a view, design record or prototype target)")
    c.add_argument("--reason", required=True, help="why the note belongs to the new target")
    c.add_argument("--by", default="tp-product remap-anchor", help="who decided it")
    c.add_argument("--expected-revision", type=int, help="refuse unless the package is at this revision")
    c.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    as_json = getattr(args, "json", False)
    try:
        if args.command == "init":
            docs = T.init(args.package_dir, args.id, args.title, args.owner, args.objective)
            print(f"created {args.package_dir} ({docs['manifest']['id']}, revision 1)", file=out)
            return 0
        if args.command == "status":
            rep, code = status_report(args.package_dir)
            if as_json:
                print(json.dumps(rep, indent=2, ensure_ascii=False), file=out)
            else:
                _print_status(rep, out)
            return code
        if args.command == "validate":
            state = T.inspect(args.package_dir)
            trust = T.trust_problems(state)
            if as_json:
                print(json.dumps({"valid": not state["issues"], "trusted": not trust,
                                  "issues": [i._asdict() for i in state["issues"]],
                                  "trust": [{"code": c, "message": m} for c, m in trust]}, indent=2), file=out)
            elif state["issues"]:
                print(f"invalid: {len(state['issues'])} issue(s)", file=out)
                print("\n".join(_issue_lines(state["issues"])), file=out)
            else:
                print(f"valid: {state['docs']['manifest']['id']} revision {state['docs']['manifest']['revision']}", file=out)
            if not as_json:
                for c, m in trust:
                    print(f"WARNING {c}: {m}", file=out)
            return 1 if state["issues"] else (3 if trust else 0)
        if args.command == "build":
            result = R.build(args.package_dir, args.expected_revision)
            _print_build(result, out, as_json)
            return 0
        if args.command == "import-feedback":
            result = C.ingest(args.package_dir, C.load_bundle(args.bundle), actor="tp-product import-feedback",
                              expected_revision=args.expected_revision)
            _print_import(result, out, as_json)
            return 0
        if args.command == "remap-anchor":
            op = {"op": "remap-anchor", "note": args.note, "target": args.target, "reason": args.reason, "by": args.by,
                  "date": T.now()[:10]}
            cs = {"schema": "tp-product-changeset", "schema_version": 1, "actor": "tp-product remap-anchor",
                  "summary": f"remap the anchor of {args.note} to {args.target}", "evidence": [], "operations": [op]}
            result = C.apply(args.package_dir, cs, args.expected_revision)
            if as_json:
                print(json.dumps(dict(result, ok=True), indent=2, ensure_ascii=False), file=out)
            elif result["committed"]:
                print(f"remapped {args.note} to {args.target}: committed revision {result['revision']} (the original "
                      "target is kept)", file=out)
            else:
                print(f"no change: {args.note} is already remapped to {args.target} with this reason", file=out)
            return 0
        # apply
        if args.recover:
            if args.changeset:
                p.error("--recover takes no changeset")
            with T.lock(args.package_dir):
                done = T.recover(args.package_dir)
            print(json.dumps(done) if as_json else (f"{done['action']} the commit to revision {done['revision']}"
                                                     if done else "nothing to recover"), file=out)
            return 0
        if not args.changeset or args.expected_revision is None:
            p.error("apply needs CHANGESET and --expected-revision REV (or --recover)")
        result = C.apply(args.package_dir, C.load_changeset(args.changeset), args.expected_revision)
        if as_json:
            print(json.dumps(dict(result, ok=True), indent=2, ensure_ascii=False), file=out)
        elif not result["committed"]:
            print(f"no change: the package stays at revision {result['revision']}", file=out)
        else:
            print(f"committed revision {result['revision']}", file=out)
            eff = result["impact"]
            if eff:
                for sid in eff["stale"]:
                    print(f"  {sid} is now stale: its accepted content changed; it needs a new review", file=out)
                for u in eff["unlinked"]:
                    print(f"  {u['id']} changed outside every slice's dependency links"
                          + (f" (named by {', '.join(u['named_by'])})" if u["named_by"] else "")
                          + ": review its impact by hand", file=out)
                for c in eff["cases"]:
                    print(f"  case {c['id']} is stale: {c['slice']} changed", file=out)
        return 0
    except T.StoreError as exc:
        return _fail(exc, out, err, as_json)
    except OSError as exc:
        return _fail(T.StoreError("io.error", f"{exc.strerror or exc}" + (f": {exc.filename}" if exc.filename else ""),
                                  details={"errno": exc.errno}), out, err, as_json)
