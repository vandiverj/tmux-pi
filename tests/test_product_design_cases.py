import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""Reviewed cases and worker packets (PD phase E): `tp-product cases`, `packet` and `impact`, the case rule
(package.case_report, read from slice readiness), publication mapping and reopening a question.

In-process (`cli.main(argv, out, err)`, `changes.apply`) on a temporary copy of the fixture, changed only through
changesets. The generated input space is the PD-E attack script (task evidence, not tracked); these are the rows worth
keeping.
"""
import copy
import io
import json
import re
import unittest

from tests.test_product_design_store import C, P, PackageCase, READY, DRAFTY, cs, rec
from product_design import cases as K  # noqa: E402
from product_design import cli  # noqa: E402
from product_design import schema as S  # noqa: E402

OPEN_Q = "question.reassignment-during-review"
ANSWERED_Q = "question.missing-field-behaviour"
EVENT = {"by": "Owner (synthetic)", "date": "2026-11-10"}
CASE_SHAPE = P.ROOT / "catalog/skills/prepare-cases/references/case-shape.md"


def documented_case():
    """The case record of the worked changeset in the prepare-cases reference (its source is filled by the caller)."""
    block = re.search(r"```json\n(.*?)```", CASE_SHAPE.read_text(), re.S).group(1)
    return json.loads(block)["operations"][0]["record"]


def leaves(value, path=()):
    """(path, scalar) for every scalar in a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, path + (k,))
    elif isinstance(value, list):
        for n, v in enumerate(value):
            yield from leaves(v, path + (n,))
    else:
        yield path, value


def flat(text):
    return " ".join(str(text).split())


def missing_from(markdown, data, skip=lambda path: False):
    """The JSON fields of `data` whose value a reader of `markdown` does not see: checked by code over every field."""
    shown = flat(K.unescape(markdown))
    return [(path, v) for path, v in leaves(data) if not skip(path)
            and flat(v if isinstance(v, str) else json.dumps(v)) not in shown]


class CaseFlow(PackageCase):
    rev = 3

    def apply(self, *ops):
        r = C.apply(self.pkg, cs(*ops), self.rev)
        self.rev = r["revision"]
        return r

    def fp(self, sid):
        return P.readiness(self.docs())[sid]["fingerprint"]

    def case(self, cid, sid, deps, **extra):
        return dict({"id": cid, "title": f"Case {cid}", "slice": sid, "dependencies": deps,
                     "acceptance_examples": [{"situation": "Title 'Laptop request', no category.",
                                              "outcome": "Rejected: 'Category is required'."}],
                     "source": {"package_revision": self.rev, "slice_fingerprint": self.fp(sid)}}, **extra)

    def add_cases(self, *cases):
        return self.apply(*({"op": "add", "collection": "cases", "record": c} for c in cases))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main(list(argv), out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def refused_cli(self, code, *argv, exit=1):
        rc, out, _ = self.run_cli(*argv, "--json")
        body = json.loads(out)
        self.assertEqual((rc, body["code"]), (exit, code), body["message"])
        return body

    def reaccept(self, sid):
        """A new human review of the current content, then the acceptance event (decision 9)."""
        rid = f"review.re-{self.rev}"
        self.apply(self.add_review(rid, "human", scope=sid))
        self.apply({"op": "set-readiness", "slice": sid, "state": "ready-for-case-preparation", "review": rid,
                    "evidence": ["re-review"], **EVENT})

    def reject_case(self, **extra):
        """The documented example (catalog/skills/prepare-cases/references/case-shape.md), prepared now."""
        return dict(documented_case(), source={"package_revision": self.rev, "slice_fingerprint": self.fp(READY)},
                    **extra)


class ExportTests(CaseFlow):
    def test_ready_slice_exports_a_useful_case_and_a_scoped_packet(self):
        self.add_cases(self.reject_case())
        out = self.tmp / "out"
        rc, text, err = self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertEqual(rc, 0, text + err)
        md = (out / "case.reject-missing-field.md").read_text()
        data = json.loads((out / "cases.json").read_text())
        # stable keys and package provenance
        self.assertEqual((data["package"], data["package_revision"], data["slice"], data["slice_fingerprint"]),
                         ("review-queue-demo", 4, READY, self.fp(READY)))
        self.assertEqual([c["key"] for c in data["cases"]], ["case.reject-missing-field"])
        self.assertEqual(data["cases"][0]["proposal"]["action"], "create")
        for want in ("`case.reject-missing-field`", "revision 4", self.fp(READY), "`rule.required-fields`",
                     "Requester", "API test per missing field", "Proposal: create"):
            self.assertIn(want, md)
        # deterministic: a second export writes nothing and the bytes are the same
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        rc, text, err = self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertIn("nothing was written", text)
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)
        # the packet: only the case's scope, with links for the rest, and no live Git facts
        for name in ("p.md", "p.json"):
            rc, text, err = self.run_cli("packet", str(self.pkg), "--case", "case.reject-missing-field",
                                       "--out", str(self.tmp / name))
            self.assertEqual(rc, 0, text + err)
        pk = json.loads((self.tmp / "p.json").read_text())
        body = (self.tmp / "p.md").read_text()
        self.assertEqual(pk["provenance"]["slice_fingerprint"], self.fp(READY))
        self.assertEqual([s["id"] for s in pk["scope"]["scenarios"]], ["scenario.submit-missing-category"])
        self.assertEqual([s["id"] for s in pk["sources"]], ["source.intake-notes"])
        self.assertIn("Every submission needs a title and a category", body)  # the source excerpt
        links = {l["id"] for l in pk["links"]}
        self.assertTrue({"rule.queue-order", "scenario.submit-complete-record", READY} <= links)
        docs = self.docs()
        for out_of_scope in ("rule.queue-order", "scenario.submit-complete-record"):
            r = rec(docs, out_of_scope)
            self.assertNotIn(r.get("statement") or r["expected"][0], body)
        self.assertNotIn("branch", json.dumps(pk["provenance"]))
        self.assertFalse(re.search(r"\b(HEAD|origin/|worktree path|base commit)\b", body))
        # the Markdown handoffs carry what the JSON carries: the contract reached through the scenario (operations,
        # failures, the slow-response requirement), the scenario's seed and actions, feasibility, non-blocking matters,
        # cited claims; the question the included decision answers is a link
        self.assertIn("contract.submissions-api", [c["id"] for c in pk["scope"]["contracts"]])
        self.assertIn(ANSWERED_Q, links)
        self.assertEqual(missing_from(body, pk), [])
        entry = data["cases"][0]
        fingerprints = lambda path: path[:1] == ("references",) and path[-1] == "fingerprint"  # noqa: E731 (in cases.json)
        self.assertEqual(missing_from(md, entry, skip=fingerprints), [])
        self.assertEqual(missing_from(md, {"scope": pk["scope"], "sources": pk["sources"]}), [])
        # a dependency on another slice is a delivery link with that slice's readiness, never its scope
        self.apply({"op": "set", "id": "case.reject-missing-field",
                    "fields": {"dependencies": entry["case"]["dependencies"] + [DRAFTY]}})
        pk = K.build_packet(self.docs(), "case.reject-missing-field")
        self.assertEqual([s["id"] for s in pk["scope"]["scenarios"]], ["scenario.submit-missing-category"])
        self.assertNotIn("rule.reassignment-in-review", [r["id"] for r in pk["scope"]["rules"]])
        link = [l for l in pk["links"] if l["id"] == DRAFTY]
        self.assertEqual([(l["state"], l["relation"]) for l in link], [("blocked", "delivery-dependency")])
        self.assertEqual(missing_from(K.render_packet(pk), pk), [])
        # the exported case draft shows that slice's readiness too (PD-E note F3), in cases.json and the Markdown,
        # and the publication digest covers it (the draft shows it)
        rc, text, err = self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertEqual(rc, 0, text + err)
        md = (out / "case.reject-missing-field.md").read_text()
        entry = json.loads((out / "cases.json").read_text())["cases"][0]
        self.assertEqual(entry["references"][DRAFTY]["state"], "blocked")
        self.assertIn(f"`{DRAFTY}` \\(slice\\)", md)
        self.assertRegex(K.unescape(md), rf"`{re.escape(DRAFTY)}` \(slice\).*delivery dependency.*state now: blocked")
        self.assertEqual(missing_from(md, entry, skip=fingerprints), [])
        self.assertEqual(entry["digest"], K.case_digest(self.docs(), rec(self.docs(), "case.reject-missing-field"),
                                                        P.index(self.docs())))
        self.assertIn("state", K.published(self.docs(), rec(self.docs(), "case.reject-missing-field"),
                                           P.index(self.docs()))["references"][DRAFTY])

    def test_a_blocked_or_unready_slice_names_what_it_is_missing(self):
        self.add_cases(self.case("case.reassign-keeps-notes", DRAFTY, ["rule.assigned-reviewer"]))
        body = self.refused_cli("slice.blocked", "cases", str(self.pkg), "--slice", DRAFTY, "--out", str(self.tmp / "o"))
        q = rec(self.docs(), OPEN_Q)
        self.assertIn(OPEN_Q, body["message"])
        self.assertIn(q["decision_needed"], body["message"])
        self.assertEqual(body["details"]["blockers"][0]["owner"], q["owner"])
        self.assertFalse((self.tmp / "o").exists())  # nothing was written
        # a named person accepts the bounded uncertainty: the question no longer blocks; a human review still accepts
        self.apply({"op": "accept-uncertainty", "question": OPEN_Q, "scope": "Notes only", "rationale": "Rare.",
                    "revisit_trigger": "A reviewer loses notes", **EVENT})
        body = self.refused_cli("slice.not-ready", "cases", str(self.pkg), "--slice", DRAFTY, "--out", str(self.tmp / "o"))
        self.assertEqual([b["code"] for b in body["details"]["blockers"]], ["slice.not-accepted"])
        self.refused_cli("slice.unknown", "cases", str(self.pkg), "--slice", "slice.gone", "--out", str(self.tmp / "o"))

    def test_case_rule_blockers_by_name(self):
        """Each reason a case cannot be handed out, as packet reports it."""
        self.add_cases(self.reject_case(), self.case("case.outside", READY, ["rule.reassignment-in-review"]))
        rows = [  # case, operations first, code, named id
            ("case.outside", [], "case.blocked", "case.dependency-outside-slice rule.reassignment-in-review"),
            ("case.reject-missing-field",
             [{"op": "set", "id": "contract.submissions-api", "fields": {"contradicted_by": [
                 {"evidence": "source.queue-observation", "statement": "The service answers 400."}]}}],
             "case.stale",
             r"case\.slice-changed slice\.submit-and-queue: .*; "
             r"it is also blocked: contract\.contradicted contract\.submissions-api"),
        ]
        for cid, ops, code, named in rows:
            with self.subTest(cid):
                if ops:
                    self.apply(*ops)
                body = self.refused_cli(code, "packet", str(self.pkg), "--case", cid, "--out", str(self.tmp / "p.md"))
                self.assertRegex(body["message"], named)
                self.assertFalse((self.tmp / "p.md").exists())
        # the contradiction blocks the slice by name (the stale check comes first for the case itself)
        r = P.readiness(self.docs())[READY]
        self.assertIn("contract.contradicted", [b["code"] for b in r["blockers"]])


class StalenessTests(CaseFlow):
    def test_changing_a_shared_rule_marks_every_affected_case_stale(self):
        self.add_cases(self.reject_case(),
                       self.case("case.queue-shows-new", READY, ["rule.queue-order", "rule.required-fields"]),
                       self.case("case.reassign-keeps-notes", DRAFTY, ["rule.assigned-reviewer"]),
                       self.case("case.queue-order-only", READY, ["rule.queue-order"]))
        unrelated = self.apply({"op": "set", "id": "rule.reassignment-in-review", "fields": {"statement": "Changed."}})
        self.assertEqual([c["id"] for c in unrelated["impact"]["cases"]], ["case.reassign-keeps-notes"])
        self.apply({"op": "set", "id": "case.reassign-keeps-notes", "fields": {
            "source": {"package_revision": self.rev, "slice_fingerprint": self.fp(DRAFTY)}}})
        # rule.required-fields is shared: submit-and-queue links it, and reassignment through contract.submissions-api
        r = self.apply({"op": "set", "id": "rule.required-fields", "fields": {"statement": "Refused, naming each field."}})
        stale = ["case.queue-order-only", "case.queue-shows-new", "case.reassign-keeps-notes", "case.reject-missing-field"]
        self.assertEqual(sorted(c["id"] for c in r["impact"]["cases"]), stale)
        rc, text, _ = self.run_cli("status", str(self.pkg))
        for cid in stale:
            self.assertIn(f"stale case {cid}", text)
        # before a new review the slice itself is refused (its accepting review is stale) ...
        body = self.refused_cli("slice.blocked", "cases", str(self.pkg), "--slice", READY, "--out", str(self.tmp / "o"))
        self.assertIn("review.stale", body["message"])
        # ... and after it, every case prepared from the old content is refused by name until prepared again
        self.reaccept(READY)
        body = self.refused_cli("case.stale", "cases", str(self.pkg), "--slice", READY, "--out", str(self.tmp / "o"))
        self.assertEqual(sorted(body["details"]["cases"]), stale[:2] + stale[3:])
        self.refused_cli("case.stale", "packet", str(self.pkg), "--case", "case.reject-missing-field",
                         "--out", str(self.tmp / "p.md"))
        rc, text, _ = self.run_cli("impact", str(self.pkg), "--since", "4")
        self.assertIn("rule.required-fields", text)
        self.assertIn("case case.reject-missing-field: stale", text)

    def test_reopen_after_publication_keeps_the_published_id_and_prior_revision(self):
        self.add_cases(self.reject_case())
        out = self.tmp / "out"
        self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        exported = json.loads((out / "cases.json").read_text())["cases"][0]
        publish = {"op": "record-publication", "case": "case.reject-missing-field", "system": "tracker",
                   "external_id": "T-101", "package_revision": 4, "digest": exported["digest"], **EVENT}
        self.apply(publish)
        prepared = rec(self.docs(), "case.reject-missing-field")["source"]
        self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertEqual(json.loads((out / "cases.json").read_text())["cases"][0]["proposal"]["action"], "unchanged")
        # a retitle is published content: the next export proposes an update of T-101; readiness does not move (PD-B)
        ready = P.readiness(self.docs())
        self.apply({"op": "set", "id": "case.reject-missing-field", "fields": {"title": "Name each missing field"}})
        self.assertEqual(P.readiness(self.docs()), ready)
        self.assertEqual(P.case_report(self.docs())["case.reject-missing-field"]["state"], "ready")
        self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        prop = json.loads((out / "cases.json").read_text())["cases"][0]["proposal"]
        self.assertEqual((prop["action"], prop["systems"][0]["external_id"]), ("update", "T-101"))
        self.assert_digest_covers_what_an_export_shows("case.reject-missing-field")
        # a second id for the same case, or the same id for another case, is a duplicate: refused
        self.add_cases(self.case("case.other", READY, ["rule.queue-order"]))
        for op in (dict(publish, external_id="T-102"), dict(publish, case="case.other")):
            with self.subTest(op["case"], external_id=op["external_id"]):
                self.refused("publication.duplicate", cs(op), rev=self.rev)
        # an implementation discovery reopens the answered question
        self.apply({"op": "reopen-question", "question": ANSWERED_Q, "reason": "The API client cannot show field errors.",
                    **EVENT})
        d = self.docs()
        case, q = rec(d, "case.reject-missing-field"), rec(d, ANSWERED_Q)
        self.assertEqual(case["publication"][0]["external_id"], "T-101")
        self.assertEqual(case["reopened"][-1]["prior"], prepared)
        self.assertEqual((q["status"], q["reopened"][-1]["prior"]["decision"]), ("open", "decision.missing-field-rejects"))
        self.assertEqual(P.case_report(d)["case.reject-missing-field"]["state"], "stale")
        body = self.refused_cli("case.stale", "packet", str(self.pkg), "--case", "case.reject-missing-field",
                                "--out", str(self.tmp / "p.md"))
        self.assertIn(ANSWERED_Q, body["message"])
        rc, text, _ = self.run_cli("impact", str(self.pkg))
        self.assertIn("published as tracker T-101; kept", text)
        # decided again, and the case prepared again: the re-export proposes an update of T-101, never a new record
        self.apply({"op": "set", "id": ANSWERED_Q, "fields": {"status": "answered",
                                                               "decision": "decision.missing-field-rejects"}})
        self.apply({"op": "set", "id": "case.reject-missing-field", "fields": {
            "source": {"package_revision": self.rev, "slice_fingerprint": self.fp(READY)},
            "exclusions": ["Field errors in the API client"]}})
        self.assertEqual(rec(self.docs(), "case.other")["reopened"][-1]["question"], ANSWERED_Q)  # same slice: stale too
        self.apply({"op": "set", "id": "case.other", "fields": {
            "source": {"package_revision": self.rev, "slice_fingerprint": self.fp(READY)}}})
        rc, text, err = self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertEqual(rc, 0, text + err)
        exported = {c["key"]: c for c in json.loads((out / "cases.json").read_text())["cases"]}
        self.assertEqual(exported["case.other"]["proposal"]["action"], "create")  # never published
        prop = exported["case.reject-missing-field"]["proposal"]
        self.assertEqual((prop["action"], prop["systems"][0]["external_id"], prop["systems"][0]["published_revision"]),
                         ("update", "T-101", 4))
        self.assertIn("never overwrite it silently", prop["check"])

    def assert_digest_covers_what_an_export_shows(self, cid):
        """By code, over every field readiness fingerprints leave out (schema x-bookkeeping) of every record the case's
        export shows (the case, its references with a slice dependency added, its scope and sources): a change to the
        field changes the publication digest exactly when the case Markdown shows it. The publication mapping is the
        one field shown and not covered: recording a publication does not change what was published."""
        docs = copy.deepcopy(self.docs())
        P.index(docs)[cid][1]["dependencies"].append(DRAFTY)
        prov = {"package": "p", "package_revision": 1, "content_fingerprint": "sha256:" + "0" * 64, "slice": READY,
                "slice_fingerprint": "sha256:" + "0" * 64}

        def export(d):
            idx = P.index(d)
            return (K.render_case(d, idx[cid][1], idx, prov, {"action": "create", "systems": []}, prov["slice_fingerprint"]),
                    K.case_digest(d, idx[cid][1], idx))

        md0, digest0 = export(docs)
        idx = P.index(docs)
        pub = K.published(docs, idx[cid][1], idx)
        shown = {cid} | set(pub["references"]) | {s["id"] for s in pub["sources"]} \
            | {r["id"] for coll in pub["scope"].values() for r in coll}
        rows = set()
        for i in sorted(shown):
            kind = idx[i][0]
            for field in sorted(S.bookkeeping_fields(P.load_schema(), kind) - {"publication"}):
                with self.subTest(i, field=field):
                    d = copy.deepcopy(docs)
                    r = P.index(d)[i][1]
                    marker = f"Marker {i} {field}"
                    if isinstance(r.get(field), dict):
                        r[field] = dict(r[field], marker=marker)
                    elif isinstance(r.get(field), list):
                        r[field] = r[field] + [{"reason": marker}]
                    else:
                        r[field] = marker
                    text, digest = export(d)
                    self.assertEqual(marker in K.unescape(text), digest != digest0, (i, field))
                    rows.add((kind, field, marker in K.unescape(text)))
        self.assertTrue({("case", "title", True), ("case", "reopened", True), ("contract", "title", True),
                         ("slice", "title", True), ("claim", "note", True), ("decision", "acceptance", False),
                         ("slice", "readiness", False)} <= rows, rows)

    def test_re_export_never_overwrites_a_hand_edited_file(self):
        self.add_cases(self.reject_case())
        out = self.tmp / "out"
        self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(out))
        target = out / "case.reject-missing-field.md"
        target.write_text(target.read_text() + "\nMy own notes.\n")
        self.apply({"op": "set", "id": "case.reject-missing-field", "fields": {"outcome": "Refused, naming the field."}})
        before = {p.name: p.read_bytes() for p in out.iterdir()}
        body = self.refused_cli("export.edited", "cases", str(self.pkg), "--slice", READY, "--out", str(out))
        self.assertEqual(body["details"]["files"], ["case.reject-missing-field.md"])
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before)

    def test_reads_change_nothing_and_an_untrusted_package_is_refused(self):
        self.add_cases(self.reject_case())
        files = self.files()
        self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(self.tmp / "o"))
        self.run_cli("packet", str(self.pkg), "--case", "case.reject-missing-field", "--out", str(self.tmp / "p.md"))
        self.run_cli("impact", str(self.pkg), "--json")
        self.assertEqual(self.files(), files)
        self.assert_trusted(4)
        design = self.pkg / "design.json"
        design.write_text(design.read_text().replace("Reject incomplete submissions", "Hold incomplete submissions"))
        self.refused_cli("package.external-edit", "packet", str(self.pkg), "--case", "case.reject-missing-field",
                         "--out", str(self.tmp / "q.md"), exit=3)
        self.refused_cli("package.external-edit", "packet", str(self.pkg), "--case", "case.gone",
                         "--out", str(self.tmp / "q.md"), exit=3)  # trust is checked before anything is read


class InertMarkdownTests(CaseFlow):
    HOSTILE = ["<script>alert(1)</script>", "# heading", "1. list", "12) list", "- item", "+ item", "> quote",
               "```\ncode\n```", "    indented code", "a | b\n--- | ---", "Title\n===", "***", "[link](javascript:x)",
               "![img](x)", "&amp; &#60;", "`code`", "*em* _em_ ~~del~~", "<!-- c -->", "x\n\n\ny", "a\\b",
               "a\u202eb", "tab\there\x00nul", "$math$", "{attr}", "<https://example.invalid>"]

    def test_every_rendered_field_is_inert(self):
        for s in self.HOSTILE:
            with self.subTest(s):
                out = K.md(s)
                # every Markdown-active character is escaped, and no line starts a block
                self.assertFalse(re.search(r"(?<!\\)(?:\\\\)*[<>`*_\[\]#|~&{}$!]", out.replace("\\\n  ", " ")), out)
                for n, line in enumerate(out.split("\n")):
                    line = line[2:] if n else line  # a continuation line is indented by two, inside its list item
                    self.assertFalse(re.match(r"(?:[-+=>#\s]|\d+[.)]|```)", line), out)
                normal = " ".join(x.strip() for x in s.replace("\t", " ").replace("\x00", " ").split("\n") if x.strip())
                self.assertEqual(K.unescape(out).replace("\n", " ").replace("[U+202E]", "\u202e"), normal)
        title = " ".join(self.HOSTILE[:6])
        self.add_cases(self.reject_case(title=title, outcome="\n".join(self.HOSTILE), scope=self.HOSTILE,
                                        acceptance_examples=[{"situation": s, "outcome": s} for s in self.HOSTILE]))
        rc, text, err = self.run_cli("cases", str(self.pkg), "--slice", READY, "--out", str(self.tmp / "o"))
        self.assertEqual(rc, 0, text + err)
        self.run_cli("packet", str(self.pkg), "--case", "case.reject-missing-field", "--out", str(self.tmp / "p.md"))
        for path in (self.tmp / "o" / "case.reject-missing-field.md", self.tmp / "p.md"):
            body = path.read_text()
            self.assertNotIn("<script>", body)
            self.assertNotIn("\n# heading", body)
            self.assertNotIn("\n```", body)
            self.assertNotIn("\u202e", body)


if __name__ == "__main__":
    unittest.main()
