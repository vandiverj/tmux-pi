import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""Product design persistence and changes (PD phase B): init, lock, journaled commit and recovery, external edits,
changesets, feedback ingestion, impact, and who may make a slice ready (decision 9).

In-process (`cli.main(argv)`, `changes.apply`) on a temporary copy of the fixture; one subprocess checks the wrapper.
Crashes are injected through the commit's fault hook; concurrency waits on threading events, never on a fixed time.
The generated input space is the PD-B attack script (task evidence, not tracked); these are the rows worth keeping.
"""
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from product_design import changes as C  # noqa: E402
from product_design import cli  # noqa: E402
from product_design import package as P  # noqa: E402
from product_design import store as T  # noqa: E402

FIXTURE = ROOT / "templates/product-design/examples/review-queue"
READY, DRAFTY = "slice.submit-and-queue", "slice.reassignment"
HUMAN_REVIEW = "review.submit-and-queue-1"


class Crash(Exception):
    pass


def crash_at(point):
    def fault(p):
        if p == point:
            raise Crash(p)
    return fault


def cs(*ops, actor="Main", summary="test"):
    return {"schema": "tp-product-changeset", "schema_version": 1, "actor": actor, "summary": summary,
            "operations": list(ops)}


def rec(docs, rid):
    return P.index(docs)[rid][1]


class PackageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-b."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "pkg"
        shutil.copytree(FIXTURE, self.pkg)

    def docs(self):
        docs, issues = P.read_docs(self.pkg)
        self.assertEqual(issues, [])
        return docs

    def files(self):
        return {n: (self.pkg / n).read_bytes() for n in T.FILES.values()}

    def assert_trusted(self, revision):
        state = T.inspect(self.pkg)
        self.assertEqual((state["issues"], T.trust_problems(state)), ([], []))
        self.assertEqual((state["docs"]["manifest"]["revision"], state["commit"]["revision"]), (revision, revision))

    def refused(self, code, changeset, rev=3, **kw):
        before = self.files()
        with self.assertRaises(T.StoreError) as cm:
            C.apply(self.pkg, changeset, rev, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        self.assertEqual(self.files(), before)  # nothing was written
        return cm.exception

    def add_review(self, rid, rtype, verdict="accept", scope=READY, fp=None):
        fp = fp or P.slice_fingerprint(self.docs(), scope)
        return {"op": "add", "collection": "reviews", "record": {
            "id": rid, "scope": [scope], "fingerprint": fp, "reviewer": {"label": f"{rtype} reviewer", "type": rtype},
            "verdict": verdict, "findings": [], "evidence": ["walked the scenarios"], "date": "2026-11-10"}}


class InitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-b."))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_init_creates_a_trusted_valid_package_and_refuses_to_overwrite(self):
        pkg = self.tmp / "new"
        T.init(pkg, "demo", "Demo")
        state = T.inspect(pkg)
        self.assertEqual((state["issues"], T.trust_problems(state), state["commit"]["revision"]), ([], [], 1))
        self.assertTrue((pkg / ".tp-product/history/000001.json").exists())
        self.assertEqual((pkg / ".tp-product/.gitignore").read_text().splitlines()[1:], ["lock", "txn/"])
        with self.assertRaises(T.StoreError) as cm:
            T.init(pkg, "demo", "Demo")
        self.assertEqual(cm.exception.code, "init.exists")
        with self.assertRaises(T.StoreError) as cm:
            T.init(FIXTURE, "x", "X")
        self.assertEqual(cm.exception.code, "init.exists")

    def test_interrupted_init_is_retryable_or_recovered(self):
        pkg = self.tmp / "a"
        with self.assertRaises(Crash):
            T.init(pkg, "demo", "Demo", fault=crash_at("before-commit-point"))
        self.assertFalse((pkg / "package.json").exists())
        T.init(pkg, "demo", "Demo")  # the lock file and an empty control dir do not block a retry
        pkg = self.tmp / "b"
        with self.assertRaises(Crash):
            T.init(pkg, "demo", "Demo", fault=crash_at("after-replace:package.json"))
        self.assertIsNotNone(T.inspect(pkg)["interrupted"])
        with self.assertRaises(T.StoreError) as cm:  # init finishes the interrupted init, then refuses to overwrite it
            T.init(pkg, "demo", "Demo")
        self.assertEqual(cm.exception.code, "init.exists")
        self.assertEqual(T.trust_problems(T.inspect(pkg)), [])

    def test_bad_id_is_refused_and_writes_nothing(self):
        with self.assertRaises(T.StoreError) as cm:
            T.init(self.tmp / "c", "Bad Id", "X")
        self.assertEqual(cm.exception.code, "init.invalid")
        self.assertFalse((self.tmp / "c").exists())


class CliTests(PackageCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main([str(a) for a in argv], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_init(self):
        rc, out, _ = self.run_cli("init", self.tmp / "n", "--id", "n", "--title", "N")
        self.assertEqual((rc, "revision 1" in out), (0, True))
        rc, _, err = self.run_cli("init", self.tmp / "n", "--id", "n", "--title", "N")
        self.assertEqual((rc, "init.exists" in err), (1, True))

    def test_status(self):
        rc, out, _ = self.run_cli("status", self.pkg, "--json")
        rep = json.loads(out)
        self.assertEqual((rc, rep["revision"], rep["trusted"]), (0, 3, True))
        self.assertEqual(rep["slices"][READY]["effective"], "ready-for-case-preparation")
        self.assertEqual(rep["pending"]["inputs"], ["input.session-b.answer-1"])
        (self.pkg / "design.json").write_text((self.pkg / "design.json").read_text().replace("Rejected", "Refused"))
        rc, out, _ = self.run_cli("status", self.pkg)
        self.assertEqual(rc, 3)
        self.assertIn("WARNING package.external-edit", out)

    def test_validate(self):
        self.assertEqual(self.run_cli("validate", self.pkg)[0], 0)
        (self.pkg / "reviews.json").write_text("{")
        rc, out, _ = self.run_cli("validate", self.pkg, "--json")
        self.assertEqual((rc, json.loads(out)["valid"]), (1, False))

    def test_apply(self):
        path = self.tmp / "c.json"
        path.write_text(json.dumps(cs({"op": "set", "id": "rule.required-fields",
                                       "fields": {"statement": "Refused; the message names each missing field."}})))
        rc, out, _ = self.run_cli("apply", self.pkg, path, "--expected-revision", 3)
        self.assertEqual(rc, 0)
        self.assertIn(f"{READY} is now stale", out)
        rc, _, err = self.run_cli("apply", self.pkg, path, "--expected-revision", 3)
        self.assertEqual((rc, "revision.conflict" in err), (4, True))
        self.assertEqual(self.run_cli("apply", self.pkg, "--recover")[1].strip(), "nothing to recover")
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):  # usage
            self.run_cli("apply", self.pkg, path)
        self.assertEqual(cm.exception.code, 2)

    def test_wrapper_runs_without_a_session(self):
        r = subprocess.run([sys.executable, str(ROOT / "bin/tp-product"), "validate", str(self.pkg)],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "valid: review-queue-demo revision 3"))


class CommitTests(PackageCase):
    EDIT = {"op": "set", "id": "rule.queue-order", "fields": {"statement": "Oldest first, ties by id."}}

    def test_a_crash_at_every_point_leaves_a_valid_recoverable_package(self):
        points = ["before-commit-point", "after-commit-point"] + ["after-replace:" + n for n in T.FILES.values()] + [
            "after-commit-record"]
        for point in points:
            with self.subTest(point):
                shutil.rmtree(self.pkg)
                shutil.copytree(FIXTURE, self.pkg)
                with self.assertRaises(Crash):
                    C.apply(self.pkg, cs(self.EDIT), 3, fault=crash_at(point))
                state = T.inspect(self.pkg)
                self.assertEqual(state["issues"], [])  # every file is whole: old or new, never partial
                if point == "before-commit-point":
                    self.assert_trusted(3)
                    continue
                self.assertEqual([c for c, _ in T.trust_problems(state)], ["package.interrupted"])
                self.refused("package.interrupted", cs(self.EDIT), 3)
                with T.lock(self.pkg):
                    self.assertEqual(T.recover(self.pkg), {"action": "rolled-forward", "revision": 4})
                self.assert_trusted(4)
                self.assertEqual(rec(self.docs(), "rule.queue-order")["statement"], "Oldest first, ties by id.")

    def test_a_killed_writer_before_the_commit_point_is_discarded(self):
        txn = self.pkg / ".tp-product/txn"
        txn.mkdir()
        (txn / "design.json").write_text("{partial")
        self.assert_trusted(3)  # no txn.json: not a commit
        C.apply(self.pkg, cs(self.EDIT), 3)
        self.assert_trusted(4)
        self.assertFalse(txn.exists())

    def test_recovery_refuses_to_overwrite_an_edit_made_during_the_interruption(self):
        with self.assertRaises(Crash):
            C.apply(self.pkg, cs(self.EDIT), 3, fault=crash_at("after-replace:package.json"))
        d = json.loads((self.pkg / "design.json").read_text())
        d["rules"][0]["statement"] = "Someone else's edit."
        (self.pkg / "design.json").write_text(json.dumps(d))
        with T.lock(self.pkg), self.assertRaises(T.StoreError) as cm:
            T.recover(self.pkg)
        self.assertEqual((cm.exception.code, cm.exception.exit), ("txn.conflict", 3))
        self.assertIn("Someone else's edit.", (self.pkg / "design.json").read_text())

    def test_a_concurrent_writer_waits_then_gets_a_revision_conflict(self):
        inside, release, errors = threading.Event(), threading.Event(), []

        def hold(point):
            if point == "after-commit-point":
                inside.set()
                release.wait(10)

        def first():
            try:
                C.apply(self.pkg, cs(self.EDIT), 3, fault=hold)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        t = threading.Thread(target=first)
        t.start()
        self.assertTrue(inside.wait(10))
        with self.assertRaises(T.StoreError) as cm:  # the lock is held mid-commit
            with T.lock(self.pkg, wait=0.05):
                pass
        self.assertEqual((cm.exception.code, cm.exception.exit), ("lock.busy", 5))
        state = T.inspect(self.pkg)  # a reader sees the interrupted state and does not trust readiness
        self.assertEqual(state["interrupted"], 4)
        release.set()
        t.join(10)
        self.assertEqual(errors, [])
        other = {"op": "set", "id": "rule.required-fields", "fields": {"statement": "Changed by the second writer."}}
        err = self.refused("revision.conflict", cs(other), 3)
        self.assertEqual((err.exit, err.details), (4, {"expected": 3, "current": 4}))
        C.apply(self.pkg, cs(other), 4)
        self.assert_trusted(5)

    def test_history_records_each_commit(self):
        C.apply(self.pkg, cs(self.EDIT, actor="Owner", summary="tie-break"), 3)
        h = json.loads((self.pkg / ".tp-product/history/000004.json").read_text())
        self.assertEqual((h["actor"], h["base_revision"], h["operations"], h["revision"]),
                         ("Owner", 3, [self.EDIT], 4))
        self.assertEqual(C.apply(self.pkg, cs(self.EDIT), 4), {"committed": False, "revision": 4, "impact": None})


class ExternalEditTests(PackageCase):
    def edit(self, mutate, name="design.json", key="design"):
        docs = self.docs()
        mutate(docs)
        (self.pkg / name).write_text(json.dumps(docs[key]))

    def adopt(self):
        return cs({"op": "adopt-external-edit", "by": "Owner", "reason": "edited in the editor"})

    def test_reformatting_is_not_an_edit(self):
        self.edit(lambda d: None)  # re-serialised without indentation
        self.assert_trusted(3)

    def test_a_content_edit_is_detected_refused_and_adoptable(self):
        self.edit(lambda d: rec(d, "rule.required-fields").update(statement="Edited by hand."))
        state = T.inspect(self.pkg)
        self.assertEqual((state["external"], [c for c, _ in T.trust_problems(state)]),
                         (["design.json"], ["package.external-edit"]))
        self.refused("package.external-edit", cs(CommitTests.EDIT))
        r = C.apply(self.pkg, self.adopt(), 3)
        self.assertEqual((r["committed"], r["revision"]), (True, 4))
        self.assert_trusted(4)
        self.assertEqual(P.readiness(self.docs())[READY]["effective"], "stale")  # derived, so still visible
        self.refused("adopt.nothing", self.adopt(), 4)

    def test_an_edit_that_manufactures_an_acceptance_is_not_adopted(self):
        rows = [
            ("slice made ready by hand", lambda d: rec(d, DRAFTY)["readiness"].update(state="draft")),
            ("decision acceptance edited", lambda d: rec(d, "decision.missing-field-rejects")["acceptance"].update(by="X")),
            ("acceptance evidence edited", lambda d: rec(d, READY)["readiness"]["acceptance"].update(evidence=["y"])),
        ]
        for name, mutate in rows:
            with self.subTest(name):
                shutil.rmtree(self.pkg)
                shutil.copytree(FIXTURE, self.pkg)
                self.edit(mutate)
                self.assertTrue(T.inspect(self.pkg)["authority_changed"])
                self.refused("adopt.authority", self.adopt())

    def test_input_status_edit_and_invalid_edit_are_not_adopted(self):
        self.edit(lambda d: d["reviews"]["inputs"][0].update(
            status="reconciled", reconciliation={"by": "X", "date": "2026-11-10", "outcome": "ok", "origin": "local"}),
            name="reviews.json", key="reviews")
        self.refused("adopt.authority", self.adopt())
        shutil.copy(FIXTURE / "reviews.json", self.pkg / "reviews.json")
        self.edit(lambda d: rec(d, "rule.queue-order").update(status="approved"))
        self.refused("adopt.invalid", self.adopt())

    def test_adoption_is_the_only_operation_of_its_changeset(self):
        self.edit(lambda d: rec(d, "rule.required-fields").update(statement="Edited by hand."))
        both = self.adopt()
        both["operations"].append(CommitTests.EDIT)
        self.refused("changeset.invalid", both)

    def test_without_a_commit_record_only_an_authority_free_package_is_adopted(self):
        (self.pkg / ".tp-product/commit.json").unlink()
        self.assertEqual([c for c, _ in T.trust_problems(T.inspect(self.pkg))], ["package.uncommitted"])
        self.refused("package.uncommitted", cs(CommitTests.EDIT))
        self.refused("adopt.authority", self.adopt())  # the fixture holds acceptances nobody recorded here
        fresh = self.tmp / "fresh"
        T.init(fresh, "fresh", "Fresh")
        (fresh / ".tp-product/commit.json").unlink()
        self.assertEqual(C.apply(fresh, self.adopt(), 1)["revision"], 2)


class ChangesetTests(PackageCase):
    def test_changeset_shape_is_checked_before_anything_is_read(self):
        rows = [
            ("not an object", []),
            ("wrong schema", dict(cs(CommitTests.EDIT), schema="x")),
            ("bool version", dict(cs(CommitTests.EDIT), schema_version=True)),
            ("no actor", dict(cs(CommitTests.EDIT), actor=" ")),
            ("unknown field", dict(cs(CommitTests.EDIT), approve=True)),
            ("no operations", cs()),
            ("unknown op", cs({"op": "approve", "id": READY})),
            ("disagreeing expected revision", dict(cs(CommitTests.EDIT), expected_revision=2)),
        ]
        for name, changeset in rows:
            with self.subTest(name):
                self.refused("changeset.invalid", changeset)

    def test_operations_refuse_what_they_may_not_do(self):
        note = "note.session-b.1"
        rows = [  # name, operation, code
            ("set a slice's readiness", {"op": "set", "id": DRAFTY, "fields": {"readiness": {"state": "draft"}}},
             "op.authority"),
            ("unset a decision's acceptance", {"op": "set", "id": "decision.missing-field-rejects",
                                               "unset": ["acceptance"]}, "op.authority"),
            ("set an id", {"op": "set", "id": "rule.queue-order", "fields": {"id": "rule.other"}}, "op.identity"),
            ("edit a note", {"op": "set", "id": note, "fields": {"text": "changed"}}, "op.original"),
            ("edit an input", {"op": "replace", "id": "input.session-b.answer-1", "record": {}}, "op.original"),
            ("edit a review", {"op": "set", "id": HUMAN_REVIEW, "fields": {"verdict": "accept"}}, "op.original"),
            ("add a note", {"op": "add", "collection": "notes", "record": {"id": "note.x.1"}}, "op.original"),
            ("add an accepted slice", {"op": "add", "collection": "slices", "record": dict(
                copy.deepcopy(rec(self.docs(), READY)), id="slice.copy")}, "op.authority"),
            ("reuse an id", {"op": "add", "collection": "rules", "record": rec(self.docs(), "rule.queue-order")},
             "op.exists"),
            ("unknown id", {"op": "set", "id": "rule.gone", "fields": {"statement": "x"}}, "op.unknown-id"),
            ("store a derived state", {"op": "set-readiness", "slice": DRAFTY, "state": "stale"}, "op.shape"),
            ("manifest revision", {"op": "set-manifest", "fields": {"revision": 9}}, "op.shape"),
            ("supersede a slice", {"op": "supersede", "id": READY, "record": {}}, "op.kind"),
            ("reconcile twice", {"op": "reconcile-input", "id": "input.session-b.answer-1", "status": "approved",
                                 "by": "Owner", "date": "2026-11-10", "outcome": "x"}, "op.shape"),
            ("result invalid", {"op": "set", "id": "rule.queue-order", "fields": {"status": "approved"}},
             "result.invalid"),
        ]
        for name, op, code in rows:
            with self.subTest(name):
                self.refused(code, cs(op))

    def test_supersede_links_both_records_and_blocks_dependent_slices(self):
        new = dict(copy.deepcopy(rec(self.docs(), "rule.queue-order")), id="rule.queue-order-v2", statement="Newest first.")
        r = C.apply(self.pkg, cs({"op": "supersede", "id": "rule.queue-order", "record": new}), 3)
        d = self.docs()
        self.assertEqual((rec(d, "rule.queue-order")["superseded_by"], rec(d, "rule.queue-order-v2")["supersedes"]),
                         ("rule.queue-order-v2", "rule.queue-order"))
        self.assertEqual(r["impact"]["slices"][READY]["after"], "blocked")

    def test_impact_stales_dependents_and_flags_unlinked_changes(self):
        r = C.apply(self.pkg, cs(
            {"op": "set", "id": "rule.required-fields", "fields": {"statement": "Refused; it names each missing field."}},
            {"op": "set", "id": "claim.reassignment-is-rare", "fields": {"statement": "Reassignment is rare (unchecked)."}},
            {"op": "set", "id": "rule.queue-order", "fields": {"title": "Queue order (renamed)"}}), 3)
        imp = r["impact"]
        self.assertEqual(imp["stale"], [READY])
        self.assertEqual(imp["changed"], ["claim.reassignment-is-rare", "rule.required-fields"])  # a title is not content
        self.assertEqual(imp["unlinked"], [{"id": "claim.reassignment-is-rare", "named_by": ["rule.reassignment-in-review"]}])
        self.assertNotEqual(imp["content_fingerprint"]["before"], imp["content_fingerprint"]["after"])

    def test_reconcile_and_accept_operations_record_local_events(self):
        q = "question.reassignment-during-review"
        C.apply(self.pkg, cs(
            {"op": "reconcile-input", "id": "input.session-b.answer-1", "status": "declined", "by": "Owner",
             "date": "2026-11-10", "outcome": "Needs a decision first."},
            {"op": "accept-uncertainty", "question": q, "scope": "first release", "rationale": "rare",
             "revisit_trigger": "first reassignment complaint", "by": "Owner", "date": "2026-11-10"},
            {"op": "set-disposition", "note": "note.session-b.1", "kind": "defer", "rationale": "later", "by": "Main",
             "date": "2026-11-10"},
            {"op": "set-manifest", "fields": {"activity": "review-reconcile"}}), 3)
        d = self.docs()
        self.assertEqual(rec(d, "input.session-b.answer-1")["reconciliation"]["origin"], "local")
        self.assertEqual(P.readiness(d)[DRAFTY]["effective"], "ready-for-human-review")  # the blocker is resolved
        self.assert_trusted(4)


class DecisionNineTests(PackageCase):
    """Brief decision 9: a model review moves a slice at most to ready-for-human-review; acceptance needs a local event
    on a human review at the current fingerprint; implemented-verified needs delivery evidence, never a prototype run;
    imported approval claims never count. Every reviewer type x claimed state, through validation and through apply."""

    DELIVERY = [{"kind": "merge-request", "ref": "!42"}]

    def stored(self, rtype, state, delivery=True):
        """The fixture with READY stored in `state`, its acceptance (if any) on a review of type `rtype`."""
        d = self.docs()
        review = dict(copy.deepcopy(rec(d, HUMAN_REVIEW)), id="review.typed", reviewer={"label": "R", "type": rtype})
        d["reviews"]["reviews"].append(review)
        r = rec(d, READY)["readiness"]
        acc = dict(r["acceptance"], review="review.typed")
        new = {"state": state, "evidence": ["review.typed"]}
        if state in P.ACCEPTED_STATES:
            new["acceptance"] = acc
        if state == "implemented-verified" and delivery:
            new["delivery"] = {"by": "Owner", "date": "2026-11-10", "evidence": self.DELIVERY, "origin": "local"}
        rec(d, READY)["readiness"] = new
        return d

    def test_stored_state_matrix(self):
        rows = [  # reviewer type, stored state, expected validation codes, expected effective readiness
            ("human", "draft", [], "draft"),
            ("model", "draft", [], "draft"),
            ("human", "ready-for-human-review", [], "ready-for-human-review"),
            ("model", "ready-for-human-review", [], "ready-for-human-review"),
            ("human", "ready-for-case-preparation", [], "ready-for-case-preparation"),
            ("model", "ready-for-case-preparation", ["acceptance.not-human"], "blocked"),
            ("human", "implemented-verified", [], "implemented-verified"),
            ("model", "implemented-verified", ["acceptance.not-human"], "blocked"),
        ]
        for rtype, state, want_codes, effective in rows:
            with self.subTest(f"{rtype} {state}"):
                d = self.stored(rtype, state)
                self.assertEqual(sorted({i.code for i in P.validate_docs(d)}), want_codes)
                self.assertEqual(P.readiness(d)[READY]["effective"], effective)

    def test_delivery_evidence_rules(self):
        d = self.stored("human", "implemented-verified", delivery=False)
        self.assertEqual({i.code for i in P.validate_docs(d)}, {"delivery.missing"})
        d2 =self.stored("human", "ready-for-case-preparation")
        rec(d2, READY)["readiness"]["delivery"] = {"by": "O", "date": "2026-11-10", "evidence": self.DELIVERY,
                                                  "origin": "local"}
        self.assertEqual({i.code for i in P.validate_docs(d2)}, {"delivery.unexpected"})
        bad = self.stored("human", "implemented-verified")
        rec(bad, READY)["readiness"]["delivery"]["origin"] = "import"
        self.assertIn("schema.const", {i.code for i in P.validate_docs(bad)})

    def test_a_review_build_run_is_not_delivery_evidence(self):
        d = self.stored("human", "implemented-verified")
        build = {"file": "builds/review-r3.html", "revision": 3, "fingerprint": P.content_fingerprint(d)}
        d["manifest"]["builds"] = [build]
        self.assertEqual(P.validate_docs(d), [])
        rec(d, READY)["readiness"]["delivery"]["evidence"] = [{"kind": "ci-run", "ref": build["file"]}]
        self.assertEqual({i.code for i in P.validate_docs(d)}, {"delivery.prototype"})

    def test_set_readiness_matrix(self):
        rows = [  # reviewer type, requested state, expected: committed state or refusal code
            ("human", "draft", "draft"),
            ("model", "draft", "draft"),
            ("human", "ready-for-human-review", "ready-for-human-review"),
            ("model", "ready-for-human-review", "ready-for-human-review"),
            ("human", "ready-for-case-preparation", "ready-for-case-preparation"),
            ("model", "ready-for-case-preparation", "acceptance.not-human"),
            ("human", "implemented-verified", "op.state"),  # not accepted first
            ("model", "implemented-verified", "op.state"),
        ]
        for rtype, state, want in rows:
            with self.subTest(f"{rtype} {state}"):
                shutil.rmtree(self.pkg)
                shutil.copytree(FIXTURE, self.pkg)
                C.apply(self.pkg, cs({"op": "set-readiness", "slice": READY, "state": "draft"}), 3)
                op = {"op": "set-readiness", "slice": READY, "state": state, "review": "review.typed", "by": "Owner",
                      "date": "2026-11-10", "evidence": ["accepted in session"], "delivery": self.DELIVERY}
                changeset = cs(self.add_review("review.typed", rtype), op)
                if want.startswith(("acceptance.", "op.")):
                    self.refused(want, changeset, 4)
                    continue
                C.apply(self.pkg, changeset, 4)
                self.assertEqual(P.readiness(self.docs())[READY]["effective"], want)

    def test_acceptance_needs_an_accepting_review_of_the_current_content(self):
        base = {"op": "set-readiness", "slice": DRAFTY, "state": "ready-for-case-preparation", "review": "review.h",
                "by": "Owner", "date": "2026-11-10", "evidence": ["x"]}
        self.refused("acceptance.review", cs(self.add_review("review.h", "human", verdict="comment", scope=DRAFTY), base))
        self.refused("acceptance.fingerprint", cs(self.add_review("review.h", "human", scope=DRAFTY,
                                                                  fp="sha256:" + "c" * 64), base))
        self.refused("acceptance.blocked", cs(self.add_review("review.h", "human", scope=DRAFTY), base))  # open question
        no_evidence = {k: v for k, v in base.items() if k != "evidence"}
        self.refused("op.missing", cs(self.add_review("review.h", "human", scope=DRAFTY), no_evidence))

    def test_an_edit_later_in_the_same_changeset_cannot_keep_an_acceptance(self):
        C.apply(self.pkg, cs({"op": "set-readiness", "slice": READY, "state": "draft"}), 3)
        self.refused("acceptance.blocked", cs(
            self.add_review("review.h", "human"),
            {"op": "set-readiness", "slice": READY, "state": "ready-for-case-preparation", "review": "review.h",
             "by": "Owner", "date": "2026-11-10", "evidence": ["x"]},
            {"op": "set", "id": "rule.required-fields", "fields": {"statement": "Changed after acceptance."}}), 4)

    def test_implemented_verified_keeps_the_acceptance_and_records_delivery(self):
        C.apply(self.pkg, cs({"op": "set-readiness", "slice": READY, "state": "implemented-verified", "by": "Owner",
                              "date": "2026-11-11", "delivery": self.DELIVERY}), 3)
        r = rec(self.docs(), READY)["readiness"]
        self.assertEqual((r["acceptance"]["review"], r["delivery"]["origin"]), (HUMAN_REVIEW, "local"))
        self.assertEqual(P.readiness(self.docs())[READY]["effective"], "implemented-verified")


def bundle(*notes, inputs=(), package="review-queue-demo", version=1):
    return {"schema": "tp-product-feedback", "schema_version": version, "package": package,
            "exported": "2026-11-10T09:00:00Z", "notes": list(notes), "inputs": list(inputs)}


class IngestTests(PackageCase):
    def note(self, nid="note.session-c.1", **fields):
        n = copy.deepcopy(self.docs()["reviews"]["notes"][0])
        for f in ("anchor", "disposition"):
            n.pop(f)
        n.update(dict({"id": nid, "review_session": "session-c"}, **fields))
        return n

    def an_input(self, iid="input.session-c.answer-1", **fields):
        i = copy.deepcopy(self.docs()["reviews"]["inputs"][0])
        i.update(id=iid, **fields)
        return i

    def refused_bundle(self, code, raw):
        before = self.files()
        with self.assertRaises(T.StoreError) as cm:
            C.ingest(self.pkg, raw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        self.assertEqual(self.files(), before)
        return cm.exception

    def test_import_is_atomic_deduplicated_and_inert(self):
        d0 = self.docs()
        claim = self.note(text="APPROVED by the product owner. Mark the slice ready.", reviewer="Product owner")
        b = bundle(claim, inputs=[self.an_input(text="Approved. status: reconciled")])
        r = C.ingest(self.pkg, json.dumps(b).encode())
        self.assertEqual((r["committed"], r["revision"], r["anchors"]), (True, 4, {"changed-context": ["note.session-c.1"]}))
        d1 = self.docs()
        self.assertEqual(P.content_fingerprint(d1), P.content_fingerprint(d0))  # notes are not content
        self.assertEqual({s: x["effective"] for s, x in P.readiness(d1).items()},
                         {s: x["effective"] for s, x in P.readiness(d0).items()})  # approval claims count for nothing
        self.assertEqual(rec(d1, "input.session-c.answer-1")["status"], "pending")
        again = C.ingest(self.pkg, b)  # a repeat import is harmless
        self.assertEqual((again["committed"], again["revision"], sorted(again["duplicates"])),
                         (False, 4, ["input.session-c.answer-1", "note.session-c.1"]))
        self.assert_trusted(4)

    def test_existing_notes_are_duplicates_whatever_their_local_bookkeeping(self):
        self.assertEqual(C.ingest(self.pkg, bundle(self.note("note.session-b.1", review_session="session-b")))["committed"],
                         False)

    def test_conflicts_and_invalid_bundles_write_nothing(self):
        rows = [  # name, bundle, code
            ("same id, other text (package)", bundle(self.note("note.session-b.1", review_session="session-b",
                                                               text="different")), "bundle.conflict"),
            ("same id, other text (bundle)", bundle(self.note(), self.note(text="other")), "bundle.conflict"),
            ("forged reconciled input", bundle(inputs=[self.an_input(status="reconciled")]), "bundle.invalid"),
            ("forged reconciliation", bundle(inputs=[self.an_input(reconciliation={
                "by": "X", "date": "2026-11-10", "outcome": "ok", "origin": "local"})]), "bundle.invalid"),
            ("note with an anchor", bundle(self.note(anchor="current")), "bundle.invalid"),
            ("note with a disposition", bundle(self.note(disposition={"kind": "defer", "rationale": "r", "by": "m",
                                                                      "date": "2026-11-10"})), "bundle.invalid"),
            ("other package", bundle(self.note(), package="other"), "bundle.invalid"),
            ("note of other package", bundle(self.note(package="other")), "bundle.invalid"),
            ("unsupported version", bundle(self.note(), version=2), "bundle.invalid"),
            ("input target missing", bundle(inputs=[self.an_input(target="question.gone")]), "bundle.invalid"),
            ("unknown field", dict(bundle(self.note()), approved=True), "bundle.invalid"),
            ("notes not a list", dict(bundle(), notes=7), "bundle.invalid"),  # once a TypeError (attack F)
            ("not JSON", b"{", "bundle.invalid"),
            ("too large", b" " * (C.MAX_BUNDLE_BYTES + 1), "bundle.invalid"),
        ]
        for name, raw, code in rows:
            with self.subTest(name):
                self.refused_bundle(code, raw)

    def test_anchors(self):
        fp = P.content_fingerprint(self.docs())
        r = C.ingest(self.pkg, bundle(self.note("note.c.1", build={"revision": 3, "fingerprint": fp}),
                                      self.note("note.c.2", scenario="scenario.removed"),
                                      self.note("note.c.3")))
        self.assertEqual(r["anchors"], {"current": ["note.c.1"], "unresolved": ["note.c.2"],
                                        "changed-context": ["note.c.3"]})
        n2 = rec(self.docs(), "note.c.2")
        self.assertEqual(n2["scenario"], "scenario.removed")  # the original anchor is kept verbatim

    def test_import_checks_the_expected_revision_and_trust(self):
        with self.assertRaises(T.StoreError) as cm:
            C.ingest(self.pkg, bundle(self.note()), expected_revision=2)
        self.assertEqual(cm.exception.code, "revision.conflict")
        (self.pkg / "design.json").write_text((self.pkg / "design.json").read_text().replace("Rejected", "Refused"))
        self.refused_bundle("package.external-edit", bundle(self.note()))


if __name__ == "__main__":
    unittest.main()
