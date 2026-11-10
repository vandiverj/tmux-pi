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
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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



# ---------------------------------------------------------------- round 2: the five rules, rows kept from the attack

def nested(depth):
    """A JSON list nested `depth` levels deep (the outer list is level 1)."""
    v = []
    for _ in range(depth - 1):
        v = [v]
    return v


def deep_text(depth):
    return "[" * depth + "]" * depth  # json.dumps would recurse


class ReadBackTests(PackageCase):
    """Rule 1: what is committed can always be read back. The reader's limits (package.MAX_FILE_BYTES, MAX_DEPTH) are
    checked on every file a commit writes; the input limits apply to bytes, text and parsed values alike."""
    note = IngestTests.note
    refused_bundle = IngestTests.refused_bundle

    def setUp(self):
        super().setUp()
        for obj, name, value in ((C, "MAX_BUNDLE_BYTES", 16384), (P, "MAX_FILE_BYTES", 65536)):  # small, for speed
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def padded(self, size, form):
        b = bundle(self.note(state={"blob": ""}))
        measure = (lambda v: len(P.canonical(v))) if form == "dict" else (lambda v: len(json.dumps(v).encode()))
        b["notes"][0]["state"]["blob"] = "x" * (size - measure(b))
        return {"bytes": lambda: json.dumps(b).encode(), "str": lambda: json.dumps(b), "dict": lambda: b}[form]()

    def test_every_form_of_a_bundle_has_the_same_limits(self):
        for form in ("bytes", "str", "dict"):
            with self.subTest(form, size="at"):
                self.assertTrue(C.ingest(self.pkg, self.padded(C.MAX_BUNDLE_BYTES, form))["committed"])
                shutil.rmtree(self.pkg)
                shutil.copytree(FIXTURE, self.pkg)
            deep = bundle(self.note(state={"deep": nested(P.MAX_DEPTH - 3)}))  # bundle > notes > note > state
            rows = [("size", self.padded(C.MAX_BUNDLE_BYTES + 1, form)),
                    ("depth", {"bytes": json.dumps(deep).encode(), "str": json.dumps(deep), "dict": deep}[form])]
            if form != "dict":
                text = json.dumps(bundle(self.note(state={"deep": "DEEP"}))).replace('"DEEP"', deep_text(5000))
                rows.append(("depth", text.encode() if form == "bytes" else text))  # once a RecursionError
            for problem, raw in rows:
                with self.subTest(form, problem=problem):
                    err = self.refused_bundle("bundle.invalid", raw)
                    self.assertEqual([i["code"] for i in err.details["issues"]], ["bundle." + problem])
        at_depth = bundle(self.note(state={"deep": nested(P.MAX_DEPTH - 4)}))
        self.assertEqual(P.depth_problem(at_depth), None)
        self.assertTrue(C.ingest(self.pkg, at_depth)["committed"])

    def test_every_form_of_a_changeset_has_the_same_limits(self):
        big = cs({"op": "set-manifest", "fields": {"title": "Padded"}}, summary="s" * C.MAX_BUNDLE_BYTES)
        deep = cs({"op": "set", "id": "rule.queue-order", "fields": {"examples": nested(P.MAX_DEPTH - 3)}})
        far = json.dumps(cs({"op": "set", "id": "rule.queue-order", "fields": {"examples": "DEEP"}})).replace(
            '"DEEP"', deep_text(5000))
        rows = [("dict size", big, "size"), ("text size", json.dumps(big), "size"),
                ("bytes size", json.dumps(big).encode(), "size"), ("dict depth", deep, "depth"),
                ("text depth", json.dumps(deep), "depth"), ("far too deep", far, "depth")]
        for name, changeset, problem in rows:
            with self.subTest(name):
                err = self.refused("changeset.invalid", changeset)
                self.assertEqual(err.details, {"problem": problem})
        self.assertTrue(C.apply(self.pkg, json.dumps(cs(CommitTests.EDIT)).encode(), 3)["committed"])  # bytes work

    def test_text_that_is_not_unicode_is_one_structured_refusal_in_every_form(self):
        # A lone surrogate (valid JSON, not UTF-8) once raised UnicodeEncodeError from apply, ingest, status and validate.
        def change(text, title="T"):
            return '{"schema":"tp-product-changeset","schema_version":1,"actor":"A","summary":"s","evidence":["e' + \
                text + '"],"operations":[{"op":"set-manifest","fields":{"title":"' + title + text + '"}}]}'
        for text in ("\\ud800", "\\udc00"):
            escaped, char = change(text), json.dumps(json.loads(change(text)), ensure_ascii=False)  # str holds it raw
            for name, value in (("text", escaped), ("text, raw", char), ("bytes", escaped.encode()),
                                ("dict", json.loads(escaped))):
                with self.subTest(text, form=name):
                    err = self.refused("changeset.invalid", value)
                    self.assertEqual(err.details, {"problem": "encoding"})
            with self.subTest(text, form="bundle"):
                raw = json.dumps(bundle(self.note())).replace('"text": "', '"text": "' + text, 1)
                err = self.refused_bundle("bundle.invalid", raw.encode())
                self.assertEqual([i["code"] for i in err.details["issues"]], ["bundle.encoding"])
            with self.subTest(text, form="changeset file, --json"):
                path = self.tmp / "cs.json"
                path.write_text(escaped)
                out = io.StringIO()
                rc = cli.main(["apply", str(self.pkg), str(path), "--expected-revision", "3", "--json"], out=out,
                              err=io.StringIO())
                self.assertEqual((rc, json.loads(out.getvalue())["code"]), (1, "changeset.invalid"))
        before = self.files()
        (self.pkg / "package.json").write_text(
            (self.pkg / "package.json").read_text().replace('"title": "', '"title": "\\ud800', 1))
        for command in ("status", "validate"):
            for flag in ([], ["--json"]):
                with self.subTest("package on disk", command=command, flag=flag):
                    out = io.StringIO()
                    self.assertEqual(cli.main([command, str(self.pkg)] + flag, out=out, err=io.StringIO()), 1)
                    if flag:
                        rep = json.loads(out.getvalue())
                        self.assertEqual([i["code"] for i in rep["issues"]], ["file.unreadable"])
                        self.assertIn("lone surrogate", rep["issues"][0]["message"])
        (self.pkg / "package.json").write_bytes(before["package.json"])
        # Valid text: a pair written as two escapes and an unescaped astral character are accepted and read back.
        for rev, title, text in ((3, "A", "\\ud83d\\ude00"), (4, "B", "\U0001F600")):
            self.assertTrue(C.apply(self.pkg, change(text, title), rev)["committed"])
            self.assertEqual(self.docs()["manifest"]["title"], title + "\U0001F600")
        self.assert_trusted(5)

    def test_commits_stop_before_a_file_the_reader_would_refuse(self):
        # The reviewer's size probe: three bundles under the bundle limit once summed to a reviews.json no reader
        # accepted. The result is checked before the commit point; the refused commit writes nothing.
        for n in range(1, 40):
            before, raw = self.files(), json.dumps(bundle(self.note(f"note.size.{n}", state={"b": "y" * 13000})))
            try:
                C.ingest(self.pkg, raw)
            except T.StoreError as err:
                self.assertEqual((err.code, err.exit, err.details["file"]), ("result.unreadable", 1, "reviews.json"))
                break
        self.assertGreater(n, 2)
        self.assertEqual(self.files(), before)
        self.assert_trusted(n + 2)
        self.assertLessEqual((self.pkg / "reviews.json").stat().st_size, P.MAX_FILE_BYTES)

    def test_one_change_at_and_over_the_file_limit(self):
        with mock.patch.object(C, "MAX_BUNDLE_BYTES", 2 * P.MAX_FILE_BYTES):
            probe = self.tmp / "probe"
            shutil.copytree(FIXTURE, probe)
            C.ingest(probe, bundle(self.note(state={"blob": ""})))
            base = (probe / "reviews.json").stat().st_size
            self.refused_bundle("result.unreadable",
                                bundle(self.note(state={"blob": "q" * (P.MAX_FILE_BYTES + 1 - base)})))
            C.ingest(self.pkg, bundle(self.note(state={"blob": "q" * (P.MAX_FILE_BYTES - base)})))
        self.assertEqual((self.pkg / "reviews.json").stat().st_size, P.MAX_FILE_BYTES)
        self.assert_trusted(4)  # read back at exactly the limit

    def test_the_writer_checks_depth_on_a_parsed_result(self):
        docs = self.docs()
        docs["manifest"]["revision"] = 4
        rec(docs, "rule.queue-order")["examples"] = nested(P.MAX_DEPTH)
        state, before = T.inspect(self.pkg), self.files()
        with T.lock(self.pkg), self.assertRaises(T.StoreError) as cm:
            T.commit(self.pkg, docs, T.history_entry("a", "s", [], 3), base_digests=T.file_digests(state["docs"]),
                     base_commit=state["commit_digest"])
        self.assertEqual((cm.exception.code, cm.exception.details["file"]), ("result.unreadable", "design.json"))
        self.assertEqual(self.files(), before)
        self.assertFalse((self.pkg / ".tp-product/txn").exists())


def edit_file(pkg, target):
    """An outside writer's edit to `target`; returns a check that it is still on disk."""
    if target == "history":
        nums = T.history_numbers(pkg)
        path = T.history_path(pkg, nums[-1] + 1 if nums else 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"foreign": true}\n')
        return lambda: path.read_bytes() == b'{"foreign": true}\n'
    path = pkg / (".tp-product/commit.json" if target == "commit.json" else target)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"external": true}\n')
        return lambda: path.read_bytes() == b'{"external": true}\n'
    doc = json.loads(path.read_text())
    doc["x-outside"] = "kept"
    path.write_text(json.dumps(doc))
    return lambda: json.loads(path.read_text()).get("x-outside") == "kept"


class WriterConflictTests(PackageCase):
    """Rule 2: no write replaces bytes it has not seen. A file that differs, at the commit point or in roll-forward, from
    what the change was computed from is someone else's write: refused, their bytes kept."""
    TARGETS = ("package.json", "design.json", "reviews.json", "commit.json", "history")

    def run_command(self, command, pkg, fault):
        if command == "init":
            return T.init(pkg, "fresh", "Fresh", fault=fault)
        if command == "adopt":
            return C.apply(pkg, cs({"op": "adopt-external-edit", "by": "Owner", "reason": "hand"}), 3, fault=fault)
        if command == "ingest":
            return C.ingest(pkg, bundle(IngestTests.note(self, "note.c.9")), fault=fault)
        return C.apply(pkg, cs(CommitTests.EDIT), 3, fault=fault)

    def fresh_pkg(self, command):
        pkg = self.tmp / f"{command}-{len(list(self.tmp.iterdir()))}"
        if command != "init":
            shutil.copytree(FIXTURE, pkg)
        if command == "adopt":
            m = json.loads((pkg / "package.json").read_text())
            m["title"] = m["title"] + " (hand edit)"
            (pkg / "package.json").write_text(json.dumps(m))
        return pkg

    def test_an_edit_before_the_commit_point_is_kept_and_the_commit_refused(self):
        for command in ("init", "apply", "adopt", "ingest"):
            for target in self.TARGETS:
                with self.subTest(command=command, target=target):
                    pkg, kept = self.fresh_pkg(command), []

                    def fault(point):
                        if point == "before-commit-point":
                            kept.append(edit_file(pkg, target))
                    with self.assertRaises(T.StoreError) as cm:
                        self.run_command(command, pkg, fault)
                    self.assertEqual((cm.exception.code, cm.exception.exit), ("commit.external-edit", 3))
                    self.assertTrue(kept[0]())
                    self.assertFalse((pkg / ".tp-product/txn").exists())

    def test_recovery_refuses_an_edit_to_any_file(self):
        for command in ("init", "apply", "adopt", "ingest"):
            for target in self.TARGETS:
                with self.subTest(command=command, target=target):
                    pkg = self.fresh_pkg(command)
                    with self.assertRaises(Crash):
                        self.run_command(command, pkg, crash_at("after-replace:design.json"))
                    kept = edit_file(pkg, target)
                    with T.lock(pkg), self.assertRaises(T.StoreError) as cm:
                        T.recover(pkg)
                    self.assertEqual((cm.exception.code, cm.exception.exit), ("txn.conflict", 3))
                    self.assertTrue(kept())
                    self.assertTrue((pkg / ".tp-product/txn/txn.json").exists())  # still reported as interrupted

    def test_the_reviewers_editor_probe(self):
        # An editor saved design.json between inspection and the commit point; the commit used to replace it.
        def edit(point):
            if point == "before-commit-point":
                d = json.loads((self.pkg / "design.json").read_text())
                d["rules"][0]["statement"] = "Unsaved external edit"
                (self.pkg / "design.json").write_text(json.dumps(d))
        with self.assertRaises(T.StoreError) as cm:
            C.apply(self.pkg, cs({"op": "set-manifest", "fields": {"title": "New title"}}), 3, fault=edit)
        self.assertEqual(cm.exception.code, "commit.external-edit")
        state = T.inspect(self.pkg)
        self.assertEqual(state["docs"]["design"]["rules"][0]["statement"], "Unsaved external edit")
        self.assertEqual([c for c, _ in T.trust_problems(state)], ["package.external-edit"])

    def test_a_history_entry_is_never_replaced(self):
        foreign = b'{"foreign": true}\n'
        (self.pkg / ".tp-product/history/000004.json").write_bytes(foreign)
        self.refused("package.unreadable", cs(CommitTests.EDIT))  # every history entry is checked: this one is malformed
        state = T.inspect(self.pkg)
        docs = copy.deepcopy(state["docs"])
        docs["manifest"]["revision"] = 4
        with T.lock(self.pkg), self.assertRaises(T.StoreError) as cm:
            T.commit(self.pkg, docs, T.history_entry("a", "s", [], 3), base_digests=T.file_digests(state["docs"]),
                     base_commit=state["commit_digest"])
        self.assertEqual((cm.exception.code, cm.exception.exit), ("history.exists", 3))
        self.assertEqual((self.pkg / ".tp-product/history/000004.json").read_bytes(), foreign)


class DisplacementTests(PackageCase):
    """A commit never destroys bytes it did not write. Each live file is renamed into txn/displaced/, checked, and the
    new file linked into place; an editor at any step keeps its bytes, live or in a named copy that status reports."""
    POINTS = ("before-displace", "after-displace", "after-check", "before-link", "after-link", "after-replace",
              "before-cleanup")
    EDIT_ALL = (CommitTests.EDIT, {"op": "set-manifest", "fields": {"title": "Placed"}})
    KINDS = ("rename", "path", "held")
    fresh_pkg = WriterConflictTests.fresh_pkg

    def run_command(self, command, pkg, fault):
        if command == "apply":
            return C.apply(pkg, cs(*self.EDIT_ALL), 3, fault=fault)
        if command == "recover":  # an apply interrupted after its commit point, finished with the fault
            with self.assertRaises(Crash):
                C.apply(pkg, cs(*self.EDIT_ALL), 3, fault=crash_at("after-commit-point"))
            with T.lock(pkg):
                return T.recover(pkg, fault=fault)
        return WriterConflictTests.run_command(self, command, pkg, fault)

    @staticmethod
    def editor(target, kind, data):
        """An editor that saves by rename, writes through the path, or writes through a file it opened earlier."""
        fd = os.open(target, os.O_WRONLY) if kind == "held" else None

        def save():
            if kind == "rename":
                tmp = target.with_name(target.name + ".editor")
                tmp.write_bytes(data)
                os.replace(tmp, target)
            elif kind == "path":
                target.write_bytes(data)
            else:
                os.ftruncate(fd, 0)
                os.pwrite(fd, data, 0)
        return save, (lambda: os.close(fd)) if fd is not None else (lambda: None)

    def test_an_editor_at_every_step_keeps_its_bytes(self):
        reached = 0
        for command, names in (("apply", ("package.json", "design.json", "commit.json")),
                               ("ingest", ("package.json", "reviews.json", "commit.json")),
                               ("adopt", ("commit.json",)), ("recover", ("design.json", "commit.json"))):
            for name in names:
                for n, point in enumerate(self.POINTS):  # every editor for apply; one per step, in turn, for the rest
                    for kind in self.KINDS if command == "apply" else self.KINDS[(n + len(name)) % 3:][:1]:
                        with self.subTest(command=command, file=name, point=point, editor=kind):
                            pkg = self.fresh_pkg(command)
                            target = pkg / (".tp-product/commit.json" if name == "commit.json" else name)
                            data = f'{{"editor": "{command} {name} {point} {kind}"}}\n'.encode()
                            save, close = self.editor(target, kind, data)
                            want, fired = point if point == "before-cleanup" else f"{point}:{name}", []

                            def fault(p):
                                if p == want and not fired:
                                    fired.append(save())
                            try:
                                self.run_command(command, pkg, fault)
                                err = None
                            except T.StoreError as exc:
                                err = exc
                            finally:
                                close()
                            self.assertTrue(fired)
                            reached += 1
                            where = sorted((p for p in pkg.rglob("*") if p.is_file() and p.read_bytes() == data),
                                           key=lambda p: "displaced" not in p.parts)
                            # kept, as one file: put back in place, its displaced name is the retained copy
                            self.assertEqual(len({p.stat().st_ino for p in where}), 1, where)
                            rep, rc = cli.status_report(pkg)
                            self.assertEqual(rep["trusted"], False)
                            self.assertIn(rc, (1, 3))  # 1 when the editor's bytes are not a valid document
                            if "displaced" in where[0].parts:  # kept aside: named, reported, and kept by recover
                                self.assertEqual((err.code, err.details["kept"]), ("txn.conflict", [str(where[0])]))
                                self.assertIn("package.displaced", {t["code"] for t in rep["trust"]})
                                with T.lock(pkg), self.assertRaises(T.StoreError):
                                    T.recover(pkg)
                                self.assertEqual(where[0].read_bytes(), data)
                            elif err is not None:
                                self.assertIn(err.code, ("txn.conflict", "commit.external-edit"))
        self.assertEqual(reached, 7 * 3 * 3 + 7 * 6)

    def test_a_crash_at_every_step_recovers_and_leaves_nothing_displaced(self):
        for name in ("package.json", "design.json", "commit.json"):
            for point in self.POINTS:
                with self.subTest(file=name, point=point):
                    pkg = self.fresh_pkg("apply")
                    want = point if point == "before-cleanup" else f"{point}:{name}"
                    with self.assertRaises(Crash):
                        C.apply(pkg, cs(*self.EDIT_ALL), 3, fault=crash_at(want))
                    self.assertEqual(T.inspect(pkg)["interrupted"], 4)
                    with T.lock(pkg):
                        self.assertEqual(T.recover(pkg), {"action": "rolled-forward", "revision": 4})
                    rep, rc = cli.status_report(pkg)
                    self.assertEqual((rc, rep["revision"]), (0, 4))
                    self.assertFalse((pkg / ".tp-product/txn").exists())

    def test_a_journal_without_its_commit_point_never_discards_a_displaced_copy(self):
        held = self.pkg / ".tp-product/txn/displaced"
        held.mkdir(parents=True)
        (held / "design.json~0").write_bytes(b"only copy")
        rep, rc = cli.status_report(self.pkg)
        self.assertEqual((rc, rep["trusted"]), (3, False))
        with self.assertRaises(T.StoreError) as cm:
            C.apply(self.pkg, cs(CommitTests.EDIT), 3)
        self.assertEqual(cm.exception.code, "control.invalid")
        self.assertEqual((held / "design.json~0").read_bytes(), b"only copy")


class ControlRecordTests(PackageCase):
    """Rule 3: control records are checked before they are believed. Malformed is a structured refusal (exit 1);
    records that disagree with each other are untrusted (exit 3); nothing writes over a history entry."""
    H3, TXN = ".tp-product/history/000003.json", ".tp-product/txn/txn.json"

    @staticmethod
    def rewrite(path, fn):
        d = json.loads(path.read_text())
        fn(d)
        path.write_text(json.dumps(d))

    def variants(self):
        def commit(fn):
            return lambda p: self.rewrite(p / ".tp-product/commit.json", fn)

        def write(rel, data):
            return lambda p: (p / rel).write_bytes(data)

        def hist(dst, fn):
            def f(p):
                d = json.loads((p / self.H3).read_text())
                fn(d)
                (p / f".tp-product/history/{dst:06d}.json").write_text(json.dumps(d))
            return f

        def txn(fn):
            return lambda p: self.rewrite(p / self.TXN, fn)
        zero = "sha256:" + "0" * 64
        return [  # name, mutate, interrupted first, exit
            ("commit empty", write(".tp-product/commit.json", b""), False, 1),
            ("commit a list", write(".tp-product/commit.json", b"[1]"), False, 1),  # once an AttributeError
            ("commit revision text", commit(lambda d: d.update(revision="3")), False, 1),
            ("commit revision bool", commit(lambda d: d.update(revision=True)), False, 1),
            ("commit files a list", commit(lambda d: d.update(files=[])), False, 1),
            ("commit digest a number", commit(lambda d: d["files"].update({"design.json": 1})), False, 1),
            ("commit unknown field", commit(lambda d: d.update(extra=1)), False, 1),
            ("commit revision lower", commit(lambda d: d.update(revision=2)), False, 3),
            ("commit revision higher", commit(lambda d: d.update(revision=4)), False, 3),
            ("history top missing", lambda p: (p / self.H3).unlink(), False, 3),
            ("history top empty", write(self.H3, b""), False, 1),
            ("history top a list", write(self.H3, b"[1]"), False, 1),
            ("history top revision text", hist(3, lambda d: d.update(revision="3")), False, 1),
            ("history top other files", hist(3, lambda d: d["files"].update({"design.json": zero})), False, 3),
            ("history beyond the commit", hist(4, lambda d: d.update(revision=4)), False, 3),
            ("history gap", hist(1, lambda d: d.update(revision=1)), False, 3),
            # Every entry is checked, not only the top (the review's F2-F4 probes), without building a range (F6).
            ("history older a list", write(".tp-product/history/000002.json", b"[]"), False, 1),
            ("history older empty", write(".tp-product/history/000002.json", b""), False, 1),
            ("history older base text", hist(2, lambda d: d.update(revision=2, base_revision="1")), False, 1),
            ("history top base far", hist(3, lambda d: d.update(base_revision=999)), False, 3),
            ("history top base negative", hist(3, lambda d: d.update(base_revision=-1)), False, 1),
            ("history top revision huge", hist(3, lambda d: d.update(revision=10 ** 30)), False, 1),
            ("history entry numbered huge", hist(10 ** 30, lambda d: d.update(revision=10 ** 30, base_revision=3)),
             False, 3),
            ("commit revision huge", commit(lambda d: d.update(revision=10 ** 30)), False, 3),
            ("commit revision negative", commit(lambda d: d.update(revision=-1)), False, 1),
            ("txn empty", write(self.TXN, b""), True, 1),
            ("txn a list", write(self.TXN, b"[1]"), True, 1),
            ("txn revision text", txn(lambda d: d.update(revision="4")), True, 1),
            ("txn revision lower", txn(lambda d: d.update(revision=2)), True, 1),  # disagrees with the journal's own entry
            ("txn revision far ahead", txn(lambda d: d.update(revision=6)), True, 1),
        ]

    def snapshot(self):
        return {str(p): p.read_bytes() for p in sorted(self.pkg.rglob("*")) if p.is_file() and p.name != "lock"}

    def test_every_command_refuses_a_bad_control_record_without_a_traceback(self):
        for name, mutate, interrupted, exit_code in self.variants():
            with self.subTest(name):
                shutil.rmtree(self.pkg)
                shutil.copytree(FIXTURE, self.pkg)
                if interrupted:
                    with self.assertRaises(Crash):
                        C.apply(self.pkg, cs(CommitTests.EDIT), 3, fault=crash_at("after-commit-point"))
                mutate(self.pkg)
                before = self.snapshot()
                codes = {}
                for command in ("status", "validate"):
                    out = io.StringIO()
                    rc = cli.main([command, str(self.pkg), "--json"], out=out, err=io.StringIO())
                    rep = json.loads(out.getvalue())
                    self.assertTrue(rep["trust"], (command, rep))  # never trusted, in either output (F2)
                    self.assertIs(rep["trusted"], False)
                    codes[command] = rc
                self.assertIn(codes["status"], (1, 3) if exit_code == 1 else (3,))
                self.assertEqual(codes["validate"] != 0, True)
                for command, run in (("apply", lambda: C.apply(self.pkg, cs(CommitTests.EDIT), 3)),
                                     ("ingest", lambda: C.ingest(self.pkg, bundle(IngestTests.note(self))))):
                    with self.assertRaises(T.StoreError) as cm:
                        run()
                    self.assertIn(cm.exception.exit, (1, 3), (command, cm.exception.code))
                out = io.StringIO()
                rc = cli.main(["apply", str(self.pkg), "--recover", "--json"], out=out, err=io.StringIO())
                if interrupted:
                    self.assertEqual(rc, exit_code, out.getvalue())
                    self.assertFalse(json.loads(out.getvalue())["ok"])
                self.assertEqual(self.snapshot(), before)  # nothing was written

    def test_the_reviewers_control_probe(self):
        # commit.json lowered to revision 1 used to be trusted, and the next apply replaced history entry 2.
        fresh = self.tmp / "p"
        T.init(fresh, "p", "one")
        for rev, title in [(1, "two"), (2, "three")]:
            C.apply(fresh, cs({"op": "set-manifest", "fields": {"title": title}}), rev)
        old = (fresh / ".tp-product/history/000002.json").read_bytes()
        self.rewrite(fresh / ".tp-product/commit.json", lambda d: d.update(revision=1))
        rep, rc = cli.status_report(fresh)
        self.assertEqual((rc, rep["trusted"]), (3, False))
        self.assertIn("package.control-mismatch", {t["code"] for t in rep["trust"]})
        for rev in (1, 3):
            with self.assertRaises(T.StoreError) as cm:
                C.apply(fresh, cs({"op": "set-manifest", "fields": {"title": "overwrite"}}), rev)
            self.assertEqual(cm.exception.code, "package.control-mismatch")
        self.assertEqual((fresh / ".tp-product/history/000002.json").read_bytes(), old)

    def test_a_journal_that_does_not_match_its_files_is_not_rolled_forward(self):
        with self.assertRaises(Crash):
            C.apply(self.pkg, cs(CommitTests.EDIT), 3, fault=crash_at("after-commit-point"))
        self.rewrite(self.pkg / self.TXN, lambda d: d["new"].update({"design.json": "sha256:" + "1" * 64}))
        before = self.snapshot()
        with T.lock(self.pkg), self.assertRaises(T.StoreError) as cm:
            T.recover(self.pkg)
        self.assertEqual((cm.exception.code, cm.exception.exit), ("control.invalid", 1))
        self.assertEqual(self.snapshot(), before)

    def test_an_adoption_without_a_commit_record_continues_from_the_history(self):
        # The review's adopt-gap probe: with commit.json deleted and the manifest saying 3, adoption wrote entry 4 after
        # entry 1, a history with a gap. It now follows the history (checked whole) or refuses.
        fresh = self.tmp / "p"
        T.init(fresh, "p", "one")
        (fresh / ".tp-product/commit.json").unlink()
        self.rewrite(fresh / "package.json", lambda d: d.update(revision=3))
        adopt = cs({"op": "adopt-external-edit", "by": "Owner", "reason": "restore control"})
        before = {p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and p.name != "lock"}
        with self.assertRaises(T.StoreError) as cm:
            C.apply(fresh, adopt, 3)
        self.assertEqual((cm.exception.code, cm.exception.exit, cm.exception.details["current"]),
                         ("revision.conflict", 4, 1))
        self.assertEqual({p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and p.name != "lock"}, before)
        self.assertEqual(C.apply(fresh, adopt, 1)["revision"], 2)
        self.assertEqual(T.history_numbers(fresh), [1, 2])
        rep, rc = cli.status_report(fresh)
        self.assertEqual((rc, rep["trusted"], rep["revision"]), (0, True, 2))
        # A history that starts after revision 1 is whole when its chain is (the fixture's starts at 3).
        T.history_path(fresh, 1).unlink()
        self.assertEqual(cli.status_report(fresh)[1], 0)

    def test_init_writes_a_complete_history_entry(self):
        fresh = self.tmp / "p"
        T.init(fresh, "p", "one")
        self.assertEqual(T.history_problems(json.loads(T.history_path(fresh, 1).read_text()), 1), [])
        self.assertEqual(T.commit_problems(json.loads((fresh / ".tp-product/commit.json").read_text())), [])


class OriginalsTests(PackageCase):
    """Rule 4: original feedback is never lost. An adoption that removes or changes an original (note, input, review)
    is refused; a re-import with other content under the same id is a conflict; both orders end with the original."""
    ADOPT = cs({"op": "adopt-external-edit", "by": "Owner", "reason": "hand"})

    def test_an_original_note_or_input_is_kept_whatever_the_order(self):
        for coll, kind in (("notes", "note"), ("inputs", "input")):
            for change in ("delete", "change"):
                for content in ("same", "different"):
                    for order in ("adopt first", "re-import first"):
                        with self.subTest(kind=kind, change=change, content=content, order=order):
                            shutil.rmtree(self.pkg)
                            shutil.copytree(FIXTURE, self.pkg)
                            self.check(coll, kind, change, content, order)

    def check(self, coll, kind, change, content, order):
        orig = json.loads((FIXTURE / "reviews.json").read_text())[coll][0]
        again = copy.deepcopy(orig)
        if kind == "note":
            for f in ("anchor", "disposition"):
                again.pop(f, None)
        if content == "different":
            again["text"] = "Different text"

        def reimport():
            b = bundle(*([again] if kind == "note" else []), inputs=[again] if kind == "input" else [])
            before = self.files()
            if content == "same":
                self.assertEqual(C.ingest(self.pkg, b)["duplicates"], [orig["id"]])
            else:
                with self.assertRaises(T.StoreError) as cm:
                    C.ingest(self.pkg, b)
                self.assertEqual(cm.exception.code, "bundle.conflict")
            self.assertEqual(self.files(), before)

        def hand_edit_and_adopt():
            d = json.loads((self.pkg / "reviews.json").read_text())
            if change == "delete":
                d[coll] = d[coll][1:]
            else:
                d[coll][0]["text"] = "Changed by hand"
            (self.pkg / "reviews.json").write_text(json.dumps(d))
            with self.assertRaises(T.StoreError) as cm:
                C.apply(self.pkg, self.ADOPT, 3)
            # an input's status is an acceptance field, so deleting an input is refused by that check first
            expected = "adopt.authority" if (kind, change) == ("input", "delete") else "adopt.original"
            self.assertEqual(cm.exception.code, expected)
            shutil.copy(FIXTURE / "reviews.json", self.pkg / "reviews.json")  # the owner restores the file

        for step in ((hand_edit_and_adopt, reimport) if order == "adopt first" else (reimport, hand_edit_and_adopt)):
            step()
        self.assertEqual(P.index(self.docs())[orig["id"]][1], orig)
        self.assert_trusted(3)

    def test_a_changed_review_is_not_adopted(self):
        d = json.loads((self.pkg / "reviews.json").read_text())
        d["reviews"][0]["evidence"] = ["rewritten by hand"]
        (self.pkg / "reviews.json").write_text(json.dumps(d))
        self.refused("adopt.original", self.ADOPT)

    def test_the_reviewers_originals_probe_and_bookkeeping(self):
        # Deleting an imported note by hand and adopting that used to let a re-import replace the original.
        d = json.loads((self.pkg / "reviews.json").read_text())
        d["notes"] = []
        (self.pkg / "reviews.json").write_text(json.dumps(d))
        self.refused("adopt.original", self.ADOPT)
        shutil.copy(FIXTURE / "reviews.json", self.pkg / "reviews.json")
        d = json.loads((self.pkg / "reviews.json").read_text())
        d["notes"][0]["anchor"] = "unresolved"  # local bookkeeping stays adoptable
        (self.pkg / "reviews.json").write_text(json.dumps(d))
        self.assertTrue(C.apply(self.pkg, self.ADOPT, 3)["committed"])


class OperationShapeTests(PackageCase):
    """Rule 5: every expected bad input is a structured refusal: operation fields are checked against changes.SPECS
    before use, and under --json every refusal is the JSON error object with the documented exit code."""
    WRONG = {"list": [], "dict": {}, "number": 3, "null": None, "bool": True, "list of numbers": [1]}
    GOOD = {"text": "x", "object": {}, "list": [], "texts": []}

    def test_every_field_of_every_operation_refuses_a_wrong_type(self):
        for name, (required, optional) in C.SPECS.items():
            base = dict({k: self.GOOD[t] for k, t in required.items()}, op=name)
            for field, kind in list(required.items()) + list(optional.items()):
                for wrong, value in self.WRONG.items():
                    if C.TYPES[kind][0](value):
                        continue
                    with self.subTest(name, field=field, wrong=wrong):
                        err = self.refused("op.shape", cs(dict(base, **{field: value})))
                        self.assertEqual(err.details, {"operation": 0})
            for field in required:
                with self.subTest(name, missing=field):
                    self.refused("op.missing", cs({k: v for k, v in base.items() if k != field}))
            with self.subTest(name, extra=True):
                self.refused("op.shape", cs(dict(base, approve=True)))
        for wrong, value in self.WRONG.items():
            with self.subTest("op", wrong=wrong):
                self.refused("changeset.invalid", cs({"op": value}))

    def test_the_reviewers_malformed_operations(self):
        path = self.tmp / "c.json"
        for op, code in (({"op": "set", "id": [], "fields": {"title": "x"}}, "op.shape"),
                         ({"op": []}, "changeset.invalid"),
                         ({"op": "set", "id": "rule.queue-order", "unset": [{}]}, "op.shape"),
                         ({"op": "add", "collection": "rules", "record": {"id": 5}}, "op.shape"),
                         ({"op": "supersede", "id": "rule.queue-order", "record": {"id": ["x"]}}, "op.shape"),
                         ({"op": "adopt-external-edit"}, "op.missing")):
            with self.subTest(op):
                path.write_text(json.dumps(cs(op)))
                out = io.StringIO()
                rc = cli.main(["apply", str(self.pkg), str(path), "--expected-revision", "3", "--json"], out=out,
                              err=io.StringIO())
                self.assertEqual((rc, json.loads(out.getvalue())["code"]), (1, code))

    def test_changeset_files_and_usage_errors_under_json(self):
        good = self.tmp / "good.json"
        good.write_text(json.dumps(cs(CommitTests.EDIT)))
        files = {"missing": (self.tmp / "absent.json", "changeset.unreadable"),
                 "a directory": (self.tmp, "changeset.unreadable"),
                 "unreadable": (self.tmp / "noread.json", "changeset.unreadable"),
                 "empty": (self.tmp / "empty.json", "changeset.invalid"),
                 "not JSON": (self.tmp / "bad.json", "changeset.invalid"),
                 "not UTF-8": (self.tmp / "latin.json", "changeset.invalid"),
                 "too large": (self.tmp / "big.json", "changeset.invalid")}
        files["unreadable"][0].write_text("{}")
        files["unreadable"][0].chmod(0)
        self.addCleanup(files["unreadable"][0].chmod, 0o600)
        files["empty"][0].write_bytes(b"")
        files["not JSON"][0].write_bytes(b"{nope")
        files["not UTF-8"][0].write_bytes(b'{"x": "\xff"}')
        files["too large"][0].write_bytes(b'"' + b"x" * C.MAX_BUNDLE_BYTES + b'"')
        before = self.files()
        for name, (path, code) in files.items():
            if name == "unreadable" and os.access(path, os.R_OK):  # root reads anything
                continue
            for as_json in (True, False):
                with self.subTest(name, json=as_json):
                    out, err = io.StringIO(), io.StringIO()
                    argv = ["apply", str(self.pkg), str(path), "--expected-revision", "3"] + ["--json"] * as_json
                    self.assertEqual(cli.main(argv, out=out, err=err), 1)
                    self.assertIn(f"tp-product: {code}:", err.getvalue())
                    if as_json:
                        obj = json.loads(out.getvalue())
                        self.assertEqual((obj["ok"], obj["code"], sorted(obj)),
                                         (False, code, ["code", "details", "message", "ok"]))
                    else:
                        self.assertEqual(out.getvalue(), "")
        for argv in (["apply", str(self.pkg), str(good)], ["apply", str(self.pkg), "--recover", str(good)],
                     ["apply", str(self.pkg), str(good), "--expected-revision", "x"], ["status", str(self.pkg), "--bogus"]):
            with self.subTest(argv[2:]):
                out = io.StringIO()
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                    cli.main(argv + ["--json"], out=out, err=io.StringIO())
                self.assertEqual((cm.exception.code, json.loads(out.getvalue())["code"]), (2, "usage"))
        out = io.StringIO()
        rc = cli.main(["apply", str(good), str(good), "--expected-revision", "3", "--json"], out=out, err=io.StringIO())
        self.assertNotEqual(rc, 0)  # a file where the package should be
        self.assertFalse(json.loads(out.getvalue())["ok"])
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
