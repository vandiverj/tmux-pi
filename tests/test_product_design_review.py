import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
"""Product design review build (PD phase C): `tp-product build`, `tp-product import-feedback`, and the offline page.

In-process (`review.build`, `cli.main(argv)`) on a temporary copy of the fixture. One test drives the built page in
headless system Chrome over the DevTools pipe (tests/product_design_browser.mjs, Node built-ins only) with a fresh
profile, a file:// URL and every non-file request recorded; it is skipped, with the reason, when Node or Chrome is
missing. The generated input spaces (escaping, note capture, reset/back replay, the export/import round trip) are the
PD-C attack scripts (task evidence, not tracked); these are the rows worth keeping.
"""
import contextlib
import copy
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from product_design import changes as C  # noqa: E402
from product_design import cli  # noqa: E402
from product_design import package as P  # noqa: E402
from product_design import review as R  # noqa: E402
from product_design import store as T  # noqa: E402

FIXTURE = ROOT / "templates/product-design/examples/review-queue"
DRIVER = ROOT / "tests/product_design_browser.mjs"
CHROME = os.environ.get("TP_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
BUILD = "builds/review-queue-demo-r3.html"
HOSTILE = "</script><img src=x onerror=alert(1)><!-- & \u2028\u2029 </style>"
# What an offline, inert page never contains in its code: a request API, code from text, markup from text.
FORBIDDEN = ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "eval(", "Function(", "innerHTML",
             "outerHTML", "insertAdjacentHTML", "document.write", "import(", "importScripts", "http://", "https://")


def embedded(page: bytes):
    m = re.search(rb'<script type="application/json" id="tp-review-data">(.*?)</script>', page, re.S)
    return json.loads(m.group(1))


class BuildCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pd-c."))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg = self.tmp / "pkg"
        shutil.copytree(FIXTURE, self.pkg)

    def docs(self):
        docs, issues = P.read_docs(self.pkg)
        self.assertEqual(issues, [])
        return docs

    def tree(self):
        return {str(p.relative_to(self.pkg)): p.read_bytes() for p in sorted(self.pkg.rglob("*"))
                if p.is_file() and "/txn/" not in str(p) and p.name != "lock"}

    def refused(self, code, exit_code=None, **kw):
        before = self.tree()
        with self.assertRaises(T.StoreError) as cm:
            R.build(self.pkg, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        if exit_code is not None:
            self.assertEqual(cm.exception.exit, exit_code)
        self.assertEqual(self.tree(), before)  # nothing was written
        return cm.exception

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = cli.main([str(a) for a in argv], out=out, err=err)
        return rc, out.getvalue(), err.getvalue()


class BuildTests(BuildCase):
    def test_build_is_recorded_deterministic_and_up_to_date_until_the_shown_content_changes(self):
        r = R.build(self.pkg, expected_revision=3)
        self.assertEqual((r["committed"], r["revision"], r["file"], r["up_to_date"], r["reused"], r["warnings"]),
                         (True, 4, BUILD, False, False, []))
        page = (self.pkg / BUILD).read_bytes()
        m = self.docs()["manifest"]
        self.assertEqual(m["builds"][-1], {"revision": 3, "fingerprint": r["content_fingerprint"],
                                           "source": r["source_fingerprint"], "file": BUILD, "digest": r["digest"]})
        self.assertEqual(r["digest"], T.file_digest(self.pkg / BUILD))
        self.assertEqual(T.trust_problems(T.inspect(self.pkg)), [])
        data = embedded(page)
        self.assertEqual((data["build"]["revision"], data["build"]["source_fingerprint"],
                          data["build"]["content_fingerprint"]), (3, r["source_fingerprint"], P.content_fingerprint(self.docs())))
        # the same package elsewhere builds the same bytes
        other = self.tmp / "other"
        shutil.copytree(FIXTURE, other)
        R.build(other)
        self.assertEqual((other / BUILD).read_bytes(), page)
        # again: up to date, nothing written
        before = self.tree()
        again = R.build(self.pkg)
        self.assertEqual((again["up_to_date"], again["committed"], again["file"], again["revision"]), (True, False, BUILD, 4))
        self.assertEqual(self.tree(), before)
        # a commit that changes nothing shown (imported notes) keeps it up to date: the revision is not in the source
        note = copy.deepcopy(self.docs()["reviews"]["notes"][0])
        for f in ("anchor", "disposition"):
            note.pop(f)
        note.update(id="note.session-c.1", review_session="session-c")
        C.ingest(self.pkg, {"schema": "tp-product-feedback", "schema_version": 1, "package": "review-queue-demo",
                            "exported": "2026-11-10T09:00:00Z", "notes": [note], "inputs": []})
        self.assertTrue(R.build(self.pkg)["up_to_date"])
        # a design change is a new build with a new source, next to the old one
        C.apply(self.pkg, {"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main", "summary": "t",
                           "operations": [{"op": "set", "id": "rule.required-fields",
                                           "fields": {"statement": "Refused; the message names each field."}}]}, 5)
        new = R.build(self.pkg)
        self.assertEqual((new["file"], new["revision"], new["up_to_date"]), ("builds/review-queue-demo-r6.html", 7, False))
        self.assertNotEqual(new["source_fingerprint"], r["source_fingerprint"])
        self.assertEqual([b["file"] for b in self.docs()["manifest"]["builds"]], [BUILD, new["file"]])
        self.assertEqual((self.pkg / BUILD).read_bytes(), page)

    def test_a_deleted_build_file_is_built_again(self):
        R.build(self.pkg)
        (self.pkg / BUILD).unlink()
        r = R.build(self.pkg)
        self.assertEqual((r["up_to_date"], r["file"], r["revision"]), (False, "builds/review-queue-demo-r4.html", 5))

    def test_page_is_offline_and_package_text_is_inert(self):
        C.apply(self.pkg, {"schema": "tp-product-changeset", "schema_version": 1, "actor": "Main", "summary": "t",
                           "operations": [{"op": "set", "id": "rule.required-fields", "fields": {"statement": HOSTILE}}]}, 3)
        R.build(self.pkg)
        page = (self.pkg / "builds/review-queue-demo-r4.html").read_bytes()
        text = page.decode("utf-8")
        for token in FORBIDDEN:
            self.assertNotIn(token, text)
        # the only element closers are the page's own: four scripts/styles end where the page ends them
        self.assertEqual([text.count("</script>"), text.count("</style>"), text.count("<!--"), text.count("<img")],
                         [3, 1, 0, 0])
        self.assertNotIn("\u2028", text)
        self.assertNotIn("\u2029", text)
        rules = {r["id"]: r for r in embedded(page)["design"]["rules"]}
        self.assertEqual(rules["rule.required-fields"]["statement"], HOSTILE)  # JSON.parse reads it back unchanged
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', text).group(1)
        self.assertTrue(csp.startswith("default-src 'none'; script-src 'sha256-"), csp)
        self.assertNotIn("unsafe", csp)
        self.assertEqual(len(re.findall(r"<script>", text)), 2)  # runtime and prototype, both hashed in the policy

    def test_title_is_escaped(self):
        docs = self.docs()
        adapter, script, style = R.read_adapter(self.pkg, docs)
        js, css = R.runtime_assets()
        data = R.payload(docs, 3, adapter, script, style, js, css)
        data["package"]["title"] = HOSTILE
        page = R.render_html(data, js, css, script, style).decode("utf-8")
        self.assertIn("<title>&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;&lt;!-- &amp;", page)

    def test_an_existing_build_file_with_other_bytes_is_not_replaced(self):
        (self.pkg / "builds").mkdir()
        (self.pkg / BUILD).write_text("mine")
        self.refused("build.exists", 3)
        self.assertEqual(self.run_cli("build", self.pkg)[0], 3)
        (self.pkg / BUILD).unlink()
        (self.pkg / "builds/x").write_text("x")
        os.symlink("x", self.pkg / BUILD)  # a link is not a build file either
        self.refused("build.exists", 3)

    def test_identical_bytes_already_there_are_reused(self):
        other = self.tmp / "other"
        shutil.copytree(FIXTURE, other)
        R.build(other)
        (self.pkg / "builds").mkdir()
        shutil.copy(other / BUILD, self.pkg / BUILD)
        r = R.build(self.pkg)
        self.assertEqual((r["reused"], r["committed"], r["revision"]), (True, True, 4))

    def test_a_failed_commit_leaves_no_build_file(self):
        def fault(point):
            if point == "before-commit-point":
                raise RuntimeError(point)
        with self.assertRaises(RuntimeError):
            R.build(self.pkg, fault=fault)
        self.assertFalse((self.pkg / BUILD).exists())
        self.assertEqual(T.trust_problems(T.inspect(self.pkg)), [])

    def test_a_recorded_build_with_other_bytes_is_never_up_to_date(self):
        R.build(self.pkg)
        page = (self.pkg / BUILD).read_bytes()
        for why, data in (("one byte changed", page[:-2] + b"X" + page[-1:]), ("empty", b""), ("other page", b"<p>x</p>")):
            with self.subTest(why):
                (self.pkg / BUILD).write_bytes(data)
                e = self.refused("build.exists", 3)
                self.assertIn("not reported up to date", str(e))
                self.assertEqual(self.run_cli("build", self.pkg, "--json")[0], 3)
        (self.pkg / BUILD).unlink()
        (self.pkg / "builds/x").write_bytes(page)
        os.symlink("x", self.pkg / BUILD)  # the recorded bytes behind a link are not the build file either
        self.refused("build.exists", 3)

    def test_every_interruption_point_keeps_the_build_file_after_the_commit_point_and_only_its_own_before(self):
        points = []
        probe = self.tmp / "probe"
        shutil.copytree(FIXTURE, probe)
        R.build(probe, fault=points.append)
        page = (probe / BUILD).read_bytes()
        self.assertGreaterEqual(len(points), 20)
        self.assertEqual(points[:3], ["before-build-write", "after-build-write", "before-commit-point"])
        for i, point in enumerate(points):
            for act in ("stop", "foreign", "delete"):
                with self.subTest(point=point, act=act):
                    pkg = self.tmp / f"p{i}-{act}"
                    shutil.copytree(FIXTURE, pkg)

                    def fault(at, point=point, act=act, pkg=pkg):
                        if at != point:
                            return
                        if (pkg / BUILD).exists() and act == "foreign":
                            (pkg / BUILD).write_bytes(b"someone else's bytes")
                        elif (pkg / BUILD).exists() and act == "delete":
                            (pkg / BUILD).unlink()
                        raise RuntimeError(point)
                    with self.assertRaises((RuntimeError, T.StoreError)):
                        R.build(pkg, fault=fault)
                    after_point = (T.control(pkg) / "txn/txn.json").exists() or self.committed(pkg) == 4
                    held = (pkg / BUILD).read_bytes() if (pkg / BUILD).exists() else None
                    if act == "foreign" and point != "before-build-write":
                        self.assertEqual(held, b"someone else's bytes")  # never deleted, whoever wrote it
                    elif act == "delete" and point != "before-build-write":
                        self.assertIsNone(held)
                    elif after_point:
                        self.assertEqual(held, page)  # after the commit point the file stays
                    else:
                        self.assertIsNone(held)  # before it, this build's own file is removed
                    if not after_point:
                        self.assertFalse((T.control(pkg) / "txn").exists())
                        self.assertEqual(T.trust_problems(T.inspect(pkg)), [])
                        continue
                    if held == page:
                        with T.lock(pkg):
                            T.recover(pkg)
                        self.assertEqual(T.trust_problems(T.inspect(pkg)), [])
                        entry = self.docs_at(pkg)["manifest"]["builds"][-1]
                        self.assertEqual((entry["file"], entry["digest"]), (BUILD, T.file_digest(pkg / BUILD)))
                    elif (T.control(pkg) / "txn/txn.json").exists():
                        # the journal records a build whose file is gone or differs: never committed, and reported
                        codes = [c for c, _ in T.trust_problems(T.inspect(pkg))]
                        self.assertIn("package.build-missing", codes)
                        with T.lock(pkg), self.assertRaises(T.StoreError) as cm:
                            T.recover(pkg)
                        self.assertEqual(cm.exception.code, "package.build-missing")
                        # the commit record names revision 4 only where it was written before the file was touched
                        self.assertEqual(self.committed(pkg) == 4, point in (
                            "after-link:commit.json", "after-replace:commit.json", "after-commit-record", "before-cleanup"))
                        rc, out, _ = self.run_cli("status", pkg)
                        self.assertNotEqual(rc, 0)
                        self.assertIn("build", out)

    def committed(self, pkg):
        path = T.control(pkg) / "commit.json"  # displaced (absent) for a moment while it is replaced
        return json.loads(path.read_text())["revision"] if path.exists() else None

    def docs_at(self, pkg):
        docs, _ = P.read_docs(pkg)
        return docs

    def test_foreign_bytes_written_over_the_build_before_the_commit_point_are_kept(self):
        def fault(point):
            if point == "before-commit-point":
                (self.pkg / BUILD).write_bytes(b"theirs")
                raise RuntimeError(point)
        with self.assertRaises(RuntimeError):
            R.build(self.pkg, fault=fault)
        self.assertEqual((self.pkg / BUILD).read_bytes(), b"theirs")
        self.assertEqual([p.name for p in (self.pkg / "builds").iterdir()], [Path(BUILD).name])
        self.assertEqual(T.trust_problems(T.inspect(self.pkg)), [])

    def test_line_endings_are_hashed_as_the_html_parser_delivers_them(self):
        proto = self.pkg / "prototype"
        js = (proto / "prototype.js").read_text()
        css = (proto / "prototype.css").read_text()
        for name, text, other in (("crlf", js.replace("\n", "\r\n"), css.replace("\n", "\r\n")),
                                  ("cr", js.replace("\n", "\r"), css.replace("\n", "\r")),
                                  ("mixed", js.replace("\n", "\r\n", 3).replace("\n", "\r", 3), css + "\r")):
            with self.subTest(name):
                (proto / "prototype.js").write_bytes(text.encode())
                (proto / "prototype.css").write_bytes(other.encode())
                docs = self.docs()
                adapter, script, style = R.read_adapter(self.pkg, docs)
                self.assertNotIn("\r", script + style)
                page = R.render_html(R.payload(docs, 3, adapter, script, style, *R.runtime_assets()),
                                     *R.runtime_assets(), script, style).decode()
                self.assertNotIn("\r", page)
                csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', page).group(1)
                for body in re.findall(r"<script>(.*?)</script>", page, re.S):
                    self.assertIn(R._csp_hash(body), csp)
        (proto / "prototype.js").write_text(js + "\n// \x00\n")
        self.refused("adapter.invalid", 1)

    def test_invalid_adapter_and_assets_are_refused(self):
        proto = self.pkg / "prototype"
        js = (proto / "prototype.js").read_text()
        adapter = json.loads((proto / "adapter.json").read_text())
        cases = [
            ("a script that would close its element", "prototype.js", js + "\n// </script>\n"),
            ("a script that opens a comment", "prototype.js", js + "\n// <!--\n"),
            ("a style that would close its element", "prototype.css", "a{}</style>"),
            ("not JSON", "adapter.json", "{"),
            ("not the schema", "adapter.json", "{}"),
            ("an unknown contract", "adapter.json", json.dumps(dict(adapter, targets=adapter["targets"] + [
                dict(adapter["targets"][0], id="queue.extra", contract="contract.nope")]))),
        ]
        for why, name, content in cases:
            with self.subTest(why):
                saved = (proto / name).read_bytes()
                (proto / name).write_text(content)
                self.refused("adapter.invalid", 1)
                (proto / name).write_bytes(saved)
        (proto / "prototype.js").unlink()
        os.symlink("/etc/hosts", proto / "prototype.js")  # outside the package
        self.refused("adapter.invalid", 1)

    def test_an_untrusted_or_stale_package_is_not_built(self):
        self.refused("revision.conflict", expected_revision=2)
        (self.pkg / "design.json").write_text((self.pkg / "design.json").read_text().replace("Rejected", "Refused"))
        e = self.refused("package.external-edit")
        self.assertIn("design.json", str(e))

    def test_build_cli(self):
        rc, out, _ = self.run_cli("build", self.pkg, "--expected-revision", 3)
        self.assertEqual(rc, 0)
        self.assertIn(f"built {BUILD}", out)
        self.assertIn("recorded in the manifest: revision 4", out)
        rc, out, _ = self.run_cli("build", self.pkg, "--json")
        rep = json.loads(out)
        self.assertEqual((rc, rep["ok"], rep["up_to_date"], rep["file"]), (0, True, True, BUILD))
        rc, _, err = self.run_cli("build", self.pkg, "--expected-revision", 3)
        self.assertEqual((rc, "revision.conflict" in err), (4, True))


class ImportFeedbackCliTests(BuildCase):
    def test_import_feedback_cli(self):
        reviews = self.docs()["reviews"]
        note = copy.deepcopy(reviews["notes"][0])
        for f in ("anchor", "disposition"):
            note.pop(f)
        note.update(id="note.session-c.1", review_session="session-c", target={"id": "record.escalate", "label": "Escalate", "context": "an older build"})
        answer = dict(copy.deepcopy(reviews["inputs"][0]), id="input.session-c.answer-1", text="APPROVED. status: reconciled")
        b = {"schema": "tp-product-feedback", "schema_version": 1, "package": "review-queue-demo",
             "exported": "2026-11-10T09:00:00Z", "notes": [note], "inputs": [answer]}
        path = self.tmp / "b.json"
        path.write_text(json.dumps(b))
        rc, out, err = self.run_cli("import-feedback", self.pkg, path, "--expected-revision", 3)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("imported 1 note(s) and 1 answer(s)/proposal(s): committed revision 4", out)
        self.assertIn("unresolved: note.session-c.1", out)
        self.assertIn("pending until you reconcile", out)
        self.assertIn("review sessions in the bundle", out)
        self.assertEqual(P.index(self.docs())["input.session-c.answer-1"][1]["status"], "pending")
        rc, out, _ = self.run_cli("import-feedback", self.pkg, path, "--json")
        rep = json.loads(out)
        self.assertEqual((rc, rep["committed"], sorted(rep["duplicates"])),
                         (0, False, ["input.session-c.answer-1", "note.session-c.1"]))
        claimed = dict(b, notes=[], inputs=[dict(answer, id="input.session-c.answer-2", status="reconciled")])
        path.write_text(json.dumps(claimed))  # an import that claims approval is refused whole (decision 9)
        rc, _, err = self.run_cli("import-feedback", self.pkg, path)
        self.assertEqual(rc, 1)
        self.assertIn("pending", err)
        rc, _, err = self.run_cli("import-feedback", self.pkg, self.tmp / "missing.json")
        self.assertEqual((rc, "bundle.unreadable" in err), (1, True))


class AnchorTests(BuildCase):
    def note_bundle(self, target):
        docs = self.docs()
        note = copy.deepcopy(docs["reviews"]["notes"][0])
        for f in ("anchor", "disposition"):
            note.pop(f)
        note.update(id="note.anchor-t.1", review_session="anchor-t", target={"id": target, "label": "x", "context": "c"},
                    build={"revision": 3, "fingerprint": P.content_fingerprint(docs)})
        return {"schema": "tp-product-feedback", "schema_version": 1, "package": "review-queue-demo",
                "exported": "2026-11-10T09:00:00Z", "notes": [note], "inputs": []}

    def stored(self, rid):
        return P.index(self.docs())[rid][1]

    def test_anchors_are_re_evaluated_on_every_import_and_build_even_for_duplicates(self):
        b = self.note_bundle("record.reassign")
        self.assertEqual(C.ingest(self.pkg, b)["anchors"], {"current": ["note.anchor-t.1"]})
        R.build(self.pkg)
        adapter = self.pkg / "prototype/adapter.json"
        a = json.loads(adapter.read_text())
        a["targets"] = [t for t in a["targets"] if t["id"] != "record.reassign"]
        adapter.write_text(json.dumps(a))
        r = R.build(self.pkg)  # the target left the registry this build shows
        self.assertEqual(r["reanchored"]["note.anchor-t.1"], "unresolved")
        self.assertEqual(self.stored("note.anchor-t.1")["anchor"], "unresolved")
        self.assertEqual(self.stored("note.anchor-t.1")["target"]["id"], "record.reassign")  # the original is kept
        rep, _ = cli.status_report(self.pkg)
        self.assertIn("note.anchor-t.1", rep["pending"]["unresolved_anchors"])
        again = C.ingest(self.pkg, b)  # a duplicate: reported under its live anchor, nothing new to commit
        self.assertEqual((again["committed"], again["anchors"]), (False, {"unresolved": ["note.anchor-t.1"]}))
        adapter.write_text(json.dumps(dict(a, targets=a["targets"] + [dict(a["targets"][0], id="record.reassign")])))
        back = C.ingest(self.pkg, b)  # the target is back: the duplicate import re-anchors it
        self.assertEqual((back["committed"], back["reanchored"]["note.anchor-t.1"]), (True, "current"))

    def test_remap_anchor_cli(self):
        C.ingest(self.pkg, self.note_bundle("record.gone"))
        self.assertEqual(self.stored("note.anchor-t.1")["anchor"], "unresolved")
        rev = self.docs()["manifest"]["revision"]
        rc, out, err = self.run_cli("remap-anchor", self.pkg, "note.anchor-t.1", "rule.required-fields",
                                    "--reason", "the button moved to the rule it enforces", "--expected-revision", rev)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("remapped", out)
        rec = self.stored("note.anchor-t.1")
        self.assertEqual((rec["anchor"], rec["target"]["id"], rec["remap"]["target"], rec["remap"]["reason"]),
                         ("remapped", "record.gone", "rule.required-fields", "the button moved to the rule it enforces"))
        hist = json.loads(T.history_path(self.pkg, rev + 1).read_text())
        self.assertEqual(hist["operations"][0]["op"], "remap-anchor")
        before = self.tree()
        rc, out, _ = self.run_cli("remap-anchor", self.pkg, "note.anchor-t.1", "rule.required-fields",
                                  "--reason", "the button moved to the rule it enforces")
        self.assertEqual((rc, self.tree() == before), (0, True))  # idempotent
        self.assertIn("no change", out)
        for target in ("record.nope", "constructor"):
            rc, _, err = self.run_cli("remap-anchor", self.pkg, "note.anchor-t.1", target, "--reason", "r")
            self.assertNotEqual(rc, 0)
            self.assertIn("not a target", err)
        self.assertEqual(self.tree(), before)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):  # a reason is required
            self.run_cli("remap-anchor", self.pkg, "note.anchor-t.1", "rule.required-fields")
        rep, _ = cli.status_report(self.pkg)
        self.assertEqual(rep["remapped"], [{"note": "note.anchor-t.1", "original": "record.gone",
                                            "target": "rule.required-fields",
                                            "reason": "the button moved to the rule it enforces", "anchor": "remapped"}])
        self.assertNotIn("note.anchor-t.1", rep["pending"]["unresolved_anchors"])
        rc, out, _ = self.run_cli("status", self.pkg)
        self.assertIn("note.anchor-t.1", out)
        R.build(self.pkg)  # the page shows the remap with the original target
        page = sorted((self.pkg / "builds").iterdir())[-1].read_bytes()
        self.assertEqual(embedded(page)["remaps"], rep["remapped"])
        b = self.note_bundle("record.gone")
        b["notes"][0]["remap"] = {"target": "rule.required-fields", "reason": "r", "by": "x", "date": "2026-11-10"}
        with self.assertRaises(T.StoreError) as cm:  # never imported
            C.ingest(self.pkg, dict(b, notes=[dict(b["notes"][0], id="note.anchor-t.2")]))
        self.assertEqual(cm.exception.code, "bundle.invalid")


def _browser_skip_reason():
    if not shutil.which("node"):
        return "node is not installed"
    if not os.access(CHROME, os.X_OK):
        return f"Chrome is not installed at {CHROME} (set TP_CHROME)"
    return None


@unittest.skipIf(_browser_skip_reason(), _browser_skip_reason() or "")
class BrowserAcceptanceTests(BuildCase):
    def test_review_round_trip_in_headless_chrome(self):
        """Act in the prototype and see the consequence on another page; select the matching engineering event and
        note on it; export, then restore and resume in a fresh browser context; back, reset and reload never repeat
        an action; storage denied is shown; nothing leaves file:. Then the exported bundle imports into the package."""
        R.build(self.pkg)
        work = Path(tempfile.mkdtemp(prefix="pd-c-chrome."))
        self.addCleanup(shutil.rmtree, work, True)
        # its own process group: a run that overstays is killed with the Chrome it started, never left behind
        proc = subprocess.Popen(["node", str(DRIVER), "acceptance", str(self.pkg / BUILD), str(work)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                                env=dict(os.environ, TP_CHROME=CHROME))
        try:
            stdout, stderr = proc.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
            self.fail("the acceptance run took over 45 s; its process group was killed\n" + stderr[-3000:])
        r = subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)
        self.assertTrue(r.stdout.strip(), r.stderr[-3000:])
        out = json.loads(r.stdout)
        self.assertEqual([c[0] for c in out["failures"]], [], json.dumps(out["failures"], indent=1)[:4000])
        self.assertEqual(r.returncode, 0)
        self.assertGreaterEqual(len(out["checks"]), 50)
        self.assertTrue(out["requests"] and all(u.startswith(("file:", "blob:", "data:")) for u in out["requests"]))
        # the page's export is what import-feedback reads
        rc, text, err = self.run_cli("import-feedback", self.pkg, out["exported"], "--json")
        self.assertEqual(rc, 0, err)
        rep = json.loads(text)
        self.assertEqual((rep["committed"], len(rep["added"]["notes"]), len(rep["added"]["inputs"])), (True, 2, 1))
        self.assertEqual(sorted(rep["anchors"]), ["current"])
        recs = P.index(self.docs())
        notes = [recs[n][1] for n in rep["added"]["notes"]]
        self.assertEqual(sorted(n["target"]["id"] for n in notes), ["contract.submissions-api", "page.queue"])
        self.assertEqual(recs[rep["added"]["inputs"][0]][1]["status"], "pending")
        self.assertEqual(T.trust_problems(T.inspect(self.pkg)), [])


if __name__ == "__main__":
    unittest.main()
