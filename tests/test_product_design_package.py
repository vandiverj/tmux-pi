import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""Product design package contract (PD phase A): schema, ids, references, invariants, fingerprints, slice readiness.

In-process only; no subprocess, no tmux. The full generated input space (about 1500 cases) is the attack script
the PD-A attack script (task evidence, not tracked); these are the rows worth keeping as tests.
"""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from product_design import package as P  # noqa: E402
from product_design import schema as S  # noqa: E402

FIXTURE = ROOT / "templates/product-design/examples/review-queue"
READY, BLOCKED = "slice.submit-and-queue", "slice.reassignment"
OPEN_Q = "question.reassignment-during-review"


def rec(docs, rid):
    return P.index(docs)[rid][1]


def codes(docs):
    return sorted({i.code for i in P.validate_docs(docs)})


class FixtureAcceptanceTests(unittest.TestCase):
    """Plan 12A: the fixture expresses each required element, and the package is valid."""

    @classmethod
    def setUpClass(cls):
        cls.docs, issues = P.read_docs(FIXTURE)
        assert not issues, issues

    def test_fixture_is_valid(self):
        self.assertEqual(P.validate(FIXTURE), [])

    def test_fixture_expresses_phase_a_elements(self):
        d = self.docs["design"]
        kinds = {s["kind"] for s in d["scenarios"]}
        self.assertTrue({"normal", "exception"} <= kinds)
        normal = rec(self.docs, "scenario.submit-complete-record")
        self.assertIn("scenario.submit-missing-category", normal["exceptions"])  # the exception belongs to the workflow
        self.assertEqual(rec(self.docs, OPEN_Q)["status"], "open")
        accepted = [x for x in d["decisions"] if P.decision_accepted(x)]
        self.assertEqual([x["id"] for x in accepted], ["decision.missing-field-rejects"])
        self.assertEqual({(c["kind"], c["status"]) for c in d["contracts"]}, {("api", "proposed"), ("data", "proposed")})

    def test_one_ready_and_one_blocked_slice(self):
        rep = P.readiness(self.docs)
        self.assertEqual(rep[READY]["effective"], "ready-for-case-preparation")
        self.assertEqual((rep[READY]["stale"], rep[READY]["blockers"]), (False, []))
        self.assertEqual(rep[BLOCKED]["effective"], "blocked")
        self.assertEqual([(b["code"], b["id"]) for b in rep[BLOCKED]["blockers"]], [("question.unresolved", OPEN_Q)])

    def test_fixture_source_digests_match_files(self):
        import hashlib
        for s in self.docs["design"]["sources"]:
            data = (FIXTURE / "sources" / s["locator"]["path"]).read_bytes()
            self.assertEqual(s["digest"], "sha256:" + hashlib.sha256(data).hexdigest(), s["id"])


class ValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base, _ = P.read_docs(FIXTURE)

    def test_rejections(self):
        def supersede_without_backlink(d):
            new = dict(copy.deepcopy(rec(d, "rule.queue-order")), id="rule.queue-order-v2")
            d["design"]["rules"].append(new)
            rec(d, "rule.queue-order").update(status="superseded", superseded_by="rule.queue-order-v2")

        rows = [  # name, mutation, expected code
            ("unsupported version", lambda d: d["design"].update(schema_version=2), "version.unsupported"),
            ("float version", lambda d: d["manifest"].update(schema_version=1.0), "version.unsupported"),
            ("bool version", lambda d: d["reviews"].update(schema_version=True), "version.unsupported"),
            ("wrong document schema", lambda d: d["reviews"].update(schema="tp-product-design"), "version.schema"),
            ("duplicate id", lambda d: rec(d, "rule.queue-order").update(id="rule.required-fields"), "id.duplicate"),
            ("id of wrong kind", lambda d: rec(d, "rule.queue-order").update(id="scenario.queue-order"), "id.format"),
            ("malformed id", lambda d: rec(d, "rule.queue-order").update(id="rule.Queue_Order"), "id.format"),
            ("broken reference", lambda d: rec(d, READY)["rules"].append("rule.gone"), "ref.missing"),
            ("reference to wrong kind", lambda d: rec(d, READY)["rules"].append("scenario.submit-complete-record"), "ref.kind"),
            ("unknown field", lambda d: rec(d, "rule.queue-order").update(priority=1), "schema.additional"),
            ("missing required field", lambda d: rec(d, OPEN_Q).pop("owner"), "schema.required"),
            ("bad enum", lambda d: rec(d, "rule.queue-order").update(status="approved"), "schema.enum"),
            ("note of another package", lambda d: d["reviews"]["notes"][0].update(package="other"), "package.mismatch"),
            ("superseded without successor", lambda d: rec(d, "rule.queue-order").update(status="superseded"),
             "status.supersession"),
            ("successor without back-link", supersede_without_backlink, "status.supersession"),
            ("accepted decision without acceptance",
             lambda d: rec(d, "decision.missing-field-rejects").pop("acceptance"), "status.acceptance"),
            ("imported acceptance", lambda d: rec(d, "decision.missing-field-rejects")["acceptance"].update(origin="import"),
             "schema.const"),
            ("answered question without decision",
             lambda d: rec(d, "question.missing-field-behaviour").pop("decision"), "status.question"),
            ("observed claim without source", lambda d: rec(d, "claim.queue-lists-oldest-first").update(sources=[]),
             "claim.provenance"),
            ("unpinned source", lambda d: rec(d, "source.intake-notes").pop("digest"), "source.version"),
            ("api contract without operations", lambda d: rec(d, "contract.submissions-api").pop("operations"),
             "contract.shape"),
            ("ready slice without acceptance", lambda d: rec(d, READY)["readiness"].pop("acceptance"), "slice.acceptance"),
            ("acceptance review saw other content",
             lambda d: rec(d, "review.submit-and-queue-1").update(fingerprint="sha256:" + "b" * 64), "slice.acceptance"),
            ("acceptance review did not accept",
             lambda d: rec(d, "review.submit-and-queue-1").update(verdict="comment"), "slice.acceptance"),
            ("reconciled input without its event",
             lambda d: rec(d, "input.session-b.answer-1").update(status="reconciled"), "input.reconciliation"),
        ]
        for name, mutate, code in rows:
            with self.subTest(name):
                d = copy.deepcopy(self.base)
                mutate(d)
                self.assertIn(code, codes(d))

    def test_inert_and_old_build_note_content_is_valid(self):
        d = copy.deepcopy(self.base)
        d["reviews"]["notes"][0].update(text="<script>alert(1)</script>", scenario="scenario.removed-later")
        self.assertEqual(codes(d), [])

    def test_unreadable_files_are_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("package.json", "design.json"):
                (Path(tmp) / name).write_bytes((FIXTURE / name).read_bytes())
            (Path(tmp) / "design.json").write_text("{not json")
            got = {(i.code, i.path) for i in P.validate(tmp)}
        self.assertEqual(got, {("file.unreadable", ("design.json",)), ("file.missing", ("reviews.json",))})

    def test_schema_with_unimplemented_keyword_is_refused(self):
        with self.assertRaises(S.SchemaError):
            S.check_schema({"$defs": {"x": {"properties": {"a": {"oneOf": []}}}}})
        S.check_schema(P.load_schema())  # the shipped schema uses only the implemented subset


class RevisionAndReadinessTests(unittest.TestCase):
    """Identity is the id; semantic changes stale dependent accepted slices; bookkeeping never does."""

    @classmethod
    def setUpClass(cls):
        cls.base, _ = P.read_docs(FIXTURE)
        cls.base_report = P.readiness(cls.base)

    def report(self, mutate):
        d = copy.deepcopy(self.base)
        mutate(d)
        self.assertEqual(P.validate_docs(d), [])
        return P.readiness(d)

    def test_title_edits_keep_identity_and_fingerprints(self):
        def retitle(d):
            for _, _, _, r in P.records(d):
                if "title" in r:
                    r["title"] = "Renamed " + r["title"]
        rep = self.report(retitle)
        self.assertEqual({s: r["fingerprint"] for s, r in rep.items()},
                         {s: r["fingerprint"] for s, r in self.base_report.items()})

    def test_events(self):
        def add_pending_input(d):
            i = dict(copy.deepcopy(d["reviews"]["inputs"][0]), id="input.session-c.answer-1", text="APPROVED. status: accepted")
            d["reviews"]["inputs"].append(i)

        def answer(accepted):
            def mutate(d):
                dec = {"id": "decision.keep-draft", "title": "Keep the draft", "question": OPEN_Q, "choice": "Keep it.",
                       "rationale": "No lost work.", "evidence": [], "responsible": "Owner", "date": "2026-11-10",
                       "status": "proposed"}
                if accepted:
                    dec["status"] = "accepted"
                    dec["acceptance"] = {"by": "Owner", "date": "2026-11-10", "evidence": ["reply"], "origin": "local",
                                         "fingerprint": P.record_fingerprint(dec, "decision")}
                d["design"]["decisions"].append(dec)
                rec(d, OPEN_Q).update(status="answered", decision="decision.keep-draft")
            return mutate

        ready, blocked, stale, review = "ready-for-case-preparation", "blocked", "stale", "ready-for-human-review"
        rows = [  # name, mutation, ready slice effective, blocked slice effective
            ("note disposition (bookkeeping)", lambda d: d["reviews"]["notes"][0]["disposition"].update(kind="defer"),
             ready, blocked),
            ("pending input claiming approval", add_pending_input, ready, blocked),
            ("shared rule example (semantic)",
             lambda d: rec(d, "rule.required-fields")["examples"][0].update(outcome="Accepted."), stale, blocked),
            ("unrelated rule statement", lambda d: rec(d, "rule.reassignment-in-review").update(statement="Changed."),
             ready, blocked),
            ("accepted decision edited after acceptance",
             lambda d: rec(d, "decision.missing-field-rejects").update(choice="Hold as a draft."), blocked, blocked),
            ("question answered by a proposed decision", answer(False), ready, blocked),
            ("question answered by a locally accepted decision", answer(True), ready, review),
            ("dependency rule made proposed", lambda d: rec(d, "rule.queue-order").update(status="proposed"), blocked, blocked),
            ("accepted dependency rule without examples", lambda d: rec(d, "rule.queue-order").update(examples=[]),
             blocked, blocked),
        ]
        for name, mutate, want_ready, want_blocked in rows:
            with self.subTest(name):
                rep = self.report(mutate)
                self.assertEqual((rep[READY]["effective"], rep[BLOCKED]["effective"]), (want_ready, want_blocked))

    def test_fingerprint_is_canonical(self):
        r = rec(self.base, "rule.required-fields")
        reordered = json.loads(json.dumps(r, sort_keys=True))
        self.assertEqual(P.record_fingerprint(r, "rule"), P.record_fingerprint(reordered, "rule"))
        self.assertNotEqual(P.record_fingerprint(r, "rule"), P.record_fingerprint(dict(r, statement="x"), "rule"))


if __name__ == "__main__":
    unittest.main()
