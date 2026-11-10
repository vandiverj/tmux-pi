import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""Reviewed cases and worker packets (PD phase E): `tp-product cases`, `packet` and `impact`, the case rule
(package.case_report, read from slice readiness), publication mapping and reopening a question.

In-process (`cli.main(argv, out, err)`, `changes.apply`) on a temporary copy of the fixture, changed only through
changesets. The generated input space is the PD-E attack script (task evidence, not tracked); these are the rows worth
keeping.
"""
import io
import json
import re
import unittest

from tests.test_product_design_store import C, P, PackageCase, READY, DRAFTY, cs, rec
from product_design import cases as K  # noqa: E402
from product_design import cli  # noqa: E402

OPEN_Q = "question.reassignment-during-review"
ANSWERED_Q = "question.missing-field-behaviour"
EVENT = {"by": "Owner (synthetic)", "date": "2026-11-10"}


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
        return self.case("case.reject-missing-field", READY,
                         ["rule.required-fields", "decision.missing-field-rejects", "scenario.submit-missing-category"],
                         **dict({"actor": "actor.requester",
                                 "outcome": "An incomplete submission is refused, naming the field.",
                                 "qa": {"approach": "API test per missing field", "fixtures": ["empty-queue"]},
                                 "completion_evidence": ["A test run showing the refusal for each field"]}, **extra))


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
             "case.stale", "slice.submit-and-queue"),
        ]
        for cid, ops, code, named in rows:
            with self.subTest(cid):
                if ops:
                    self.apply(*ops)
                body = self.refused_cli(code, "packet", str(self.pkg), "--case", cid, "--out", str(self.tmp / "p.md"))
                self.assertIn(named, body["message"])
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
