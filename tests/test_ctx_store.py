"""CX1 ctx-store: the note format and validation, scope globs and ranking, search, the index cache, freshness flags, the
screens (redact), and writing/approving notes. Every project here is a temp directory; no real store, repo or network is read."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
from ctxengine import freshness, redact, search, store  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ctx_store"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"  # assembled so this file holds no key-shaped literal
API = "apps/launchpad/api/src"
WEB = "apps/launchpad/web/src"


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_notes():
    return json.loads(fixture("notes.json"))


def make_note(entry):
    return store.Note(id=entry["id"], kind=entry["kind"], scope=entry["scope"], source="human", body=entry["body"],
                      status=entry["status"], title=entry["title"], tags=entry["tags"], versions=entry.get("versions", {}),
                      evidence=[f"{entry['scope'][0].split('*')[0].rstrip('/') or 'README.md'}:1"], created="2026-10-01",
                      last_reviewed="2026-10-01" if entry["status"] == "approved" else "")


class Hermetic(unittest.TestCase):
    """A temp project, and an environment with no worker variables and no real work store."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = self.tmp / "project"
        self.project.mkdir()
        config = self.tmp / "work-config.json"
        config.write_text("{}")
        env = {k: v for k, v in os.environ.items() if k not in ("TP_TASK_DIR", "TP_TASK_ID", "TP_CTX_PROJECT", "TP_CTX_TODAY")}
        env.update({"TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(config), "TP_WORKDIR": str(self.project)})
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.layout = store.Layout(self.project)

    def put(self, entry):
        """Write a fixture entry as a note file in the directory its status names, the way the store would have."""
        note = make_note(entry)
        where = "approved" if note.status == "approved" else "candidate"
        directory = self.layout.directory(where)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{note.id}.md").write_text(store.render(note), encoding="utf-8")
        return note

    def corpus(self):
        for entry in load_notes():
            self.put(entry)

    def ids(self, result):
        return [h.note.id.removeprefix("kn-2026-10-01-") for h in result.hits]


class Parser(Hermetic):
    def test_valid_fixture_parses_every_field_and_round_trips(self):
        note = store.parse(fixture("valid.md"))
        self.assertEqual(note.id, "kn-2026-10-31-test-cleanup-shared-tables")
        self.assertEqual((note.kind, note.status, note.created), ("convention", "approved", "2026-10-31"))
        self.assertEqual(note.scope, ["apps/launchpad/api/src/**/*.test.ts"])
        self.assertEqual(note.tags, ["test-isolation", "drizzle"])
        self.assertEqual(note.versions, {"drizzle-orm": ">=0.45 <0.46"})  # the trailing comment is not part of the value
        self.assertEqual(note.evidence[1], "review:review-coder-11a38097c8-032051#F2")
        self.assertEqual(store.parse(store.render(note)), note)

    def rejected(self, name):
        with self.assertRaises(store.NoteError) as caught:
            store.parse(fixture(name))
        return " | ".join(caught.exception.problems)

    def test_an_approved_note_without_evidence_is_rejected(self):
        self.assertIn("evidence is required", self.rejected("missing-evidence-approved.md"))

    def test_a_candidate_may_omit_evidence_only_where_the_caller_does_not_require_it(self):
        note = store.parse(fixture("valid.md"))
        note.status, note.evidence = "candidate", []
        store.validate(note)  # a candidate file without evidence parses
        with self.assertRaises(store.NoteError):
            store.validate(note, require_evidence=True)  # a new candidate (add) never does

    def test_an_oversize_body_is_rejected_naming_both_numbers(self):
        message = self.rejected("oversize-body.md")
        self.assertIn("1250 characters; the limit is 1200", message)

    def test_bad_globs_are_each_reported(self):
        message = self.rejected("bad-glob.md")
        self.assertEqual(message.count("scope glob"), 4)
        self.assertNotIn("/etc/passwd", message)  # the rule is named, the value is not echoed

    def test_unknown_key_repeated_key_and_malformed_lines(self):
        self.assertIn("unknown frontmatter key", self.rejected("unknown-key.md"))
        text = fixture("valid.md").replace("status: approved", "status: approved\nstatus: candidate")
        with self.assertRaisesRegex(store.NoteError, "repeats the key status"):
            store.parse(text)
        with self.assertRaisesRegex(store.NoteError, "not `key: value`"):
            store.parse(fixture("valid.md").replace("kind: convention", "kind convention"))
        with self.assertRaisesRegex(store.NoteError, "must start with"):
            store.parse("no frontmatter\n")
        with self.assertRaisesRegex(store.NoteError, "no closing"):
            store.parse("---\nid: x\n")

    def test_a_secret_shaped_frontmatter_value_is_not_echoed_in_an_error(self):
        text = fixture("valid.md").replace("kind: convention", f"kind: {AWS}")
        with self.assertRaises(store.NoteError) as caught:
            store.parse(text)
        self.assertNotIn(AWS, str(caught.exception))

    def test_flow_values_with_quotes_commas_and_escapes(self):
        self.assertEqual(store.parse_value('["a, b", \'c\', d]'), ["a, b", "c", "d"])
        self.assertEqual(store.parse_value('{"x": "1 2", y: 3}  # note'), {"x": "1 2", "y": "3"})
        self.assertEqual(store.parse_value('"a \\" b" # c'), 'a " b')
        for bad in ('["a"', '{"a" "b"}', '"open', '["a"] junk'):
            with self.assertRaises(store.NoteError, msg=bad):
                store.parse_value(bad)

    def test_dates_must_be_real_and_enumerations_closed(self):
        note = store.parse(fixture("valid.md"))
        for field, value in (("created", "2026-13-40"), ("kind", "poem"), ("status", "pending"), ("id", "KN-X"), ("source", "somebody")):
            broken = store.Note(**{**note.__dict__, field: value})
            with self.assertRaises(store.NoteError, msg=field):
                store.validate(broken)

    def test_tags_and_versions_ranges_are_checked(self):
        note = store.parse(fixture("valid.md"))
        with self.assertRaises(store.NoteError):
            store.validate(store.Note(**{**note.__dict__, "tags": ["Bad Tag"]}))
        with self.assertRaisesRegex(store.NoteError, "versions range"):
            store.validate(store.Note(**{**note.__dict__, "versions": {"drizzle-orm": ">= 0.45"}}))

    def test_headline_falls_back_to_the_first_body_line(self):
        note = store.parse(fixture("valid.md"))
        note.title = ""
        self.assertTrue(note.headline.startswith("Tests under the API source"))


class Globs(unittest.TestCase):
    CASES = [
        ("apps/a/b.ts", "apps/a/b.ts", True),
        ("apps/a/b.ts", "apps/a/c.ts", False),
        ("apps/**/*.test.ts", "apps/a/b/c.test.ts", True),
        ("apps/**/*.test.ts", "apps/c.test.ts", True),  # `**` also stands for no directory
        ("apps/**/*.test.ts", "apps/a/b/c.ts", False),
        ("**/*.test.ts", "x.test.ts", True),
        ("src/*.ts", "src/a/b.ts", False),  # `*` does not cross a slash
        ("src/*.ts", "src/b.ts", True),
        ("src/?.ts", "src/b.ts", True),
        ("src/?.ts", "src/bb.ts", False),
        ("src/**", "src/a/b/c", True),
        ("src/**", "src", False),
        ("a/**/**/b", "a/b", True),
        ("lib/launchpad/types/order.ts", "lib/launchpad/types/order.ts.bak", False),
    ]

    def test_matching_table(self):
        for glob, path, expected in self.CASES:
            self.assertEqual(search.glob_matches(glob, path), expected, f"{glob} vs {path}")

    def test_an_invalid_glob_matches_nothing(self):
        self.assertFalse(search.glob_matches("../x", "../x"))

    def test_grammar_rejects(self):
        for bad in ("", "/abs", "a//b", "a/./b", "a/../b", "a/", "a/**b", "a/[x]", "a\\b", "a/{b,c}", "x" * 201, "a\nb"):
            self.assertFalse(store.valid_glob(bad), repr(bad))
        for good in ("README.md", "**", "apps/*/src/**/*.test.ts", ".github/workflows/*.yml"):
            self.assertTrue(store.valid_glob(good), good)

    def test_specificity_orders_exact_over_directory_over_repo(self):
        order = ["apps/a/b.ts", "apps/a/b/**", "apps/a/**/*.ts", "apps/**", "*.ts", "**/*.ts"]
        scores = [search.specificity(g) for g in order]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(len(set(scores[:5])), 5)
        self.assertEqual(search.specificity("src/**/a/**")[2], -2)


class Ranking(Hermetic):
    def test_scope_ranking_is_exact_then_directory_then_repo_and_stable(self):
        self.corpus()
        path = f"{API}/orders/orders.test.ts"
        first = search.query(self.layout, paths=[path], limit=10)
        expected = ["orders-test-truncate",  # exact path
                    "orders-directory",  # five literal directories
                    "api-src-layering", "business-day-zone", "test-truncate",  # four literal directories, equal: by id
                    "fixture-pii", "repo-test-isolation"]  # repo-wide, equal: by id
        self.assertEqual(self.ids(first), expected)
        self.assertEqual(self.ids(search.query(self.layout, paths=[path], limit=10)), expected)  # same input, same list
        self.assertEqual(self.ids(search.query(self.layout, paths=[path], limit=10, use_cache=False)), expected)

    def test_candidates_are_left_out_unless_asked_and_are_counted(self):
        self.corpus()
        path = f"{API}/orders/orders.test.ts"
        plain = search.query(self.layout, paths=[path], limit=10)
        self.assertNotIn("poisoned-truncate", self.ids(plain))
        self.assertEqual(plain.candidates_counted, 1)  # the candidate whose scope matched
        asked = search.query(self.layout, paths=[path], limit=10, include_candidates=True)
        self.assertIn("poisoned-truncate", self.ids(asked))
        self.assertEqual([h.note.status for h in asked.hits if "poisoned" in h.note.id], ["candidate"])

    def test_a_candidate_does_not_change_the_score_of_an_approved_note(self):
        self.corpus()
        before = search.query(self.layout, query="truncate tables", limit=10)
        self.put({**[e for e in load_notes() if "unreviewed" in e["id"]][0], "id": "kn-2026-10-02-more", "status": "candidate",
                  "body": "truncate truncate tables tables tables"})
        after = search.query(self.layout, query="truncate tables", limit=10)
        self.assertEqual([(h.note.id, h.score) for h in before.hits], [(h.note.id, h.score) for h in after.hits])

    def test_search_on_the_twenty_note_fixture(self):
        self.corpus()
        self.assertEqual(len(load_notes()), 20)
        cases = [
            (dict(query="social security number"), ["mutation-cache-pii"]),
            (dict(query="truncate"), ["orders-test-truncate", "test-truncate"]),
            (dict(tags=["tanstack-query"]), ["mutation-cache-pii", "query-freshness"]),
            (dict(paths=[f"{WEB}/components/Form.tsx"], tags=["tanstack-query"]), ["mutation-cache-pii", "query-freshness"]),
            (dict(paths=["lib/launchpad/types/order.ts"]), ["order-contract-file", "zod-contract-readers"]),
            (dict(paths=[f"{API}/auth/session.ts"], query="revoked"), ["auth-decision"]),
            (dict(query="staletime", tags=["tanstack-query"]), ["query-freshness"]),
            (dict(query="kubernetes"), []),
            (dict(paths=["docs/none.md"]), []),
        ]
        for kwargs, expected in cases:
            self.assertEqual(self.ids(search.query(self.layout, limit=10, **kwargs)), expected, kwargs)

    def test_tag_overlap_breaks_ties_before_the_text_score_and_scope_comes_first(self):
        self.corpus()
        # scope leads: effect-timing is anchored deeper (hooks/**) than identity-scoped-cache (web/src/**)
        got = search.query(self.layout, paths=[f"{WEB}/hooks/x.tsx"], tags=["react", "cache"], limit=10)
        self.assertEqual(self.ids(got), ["effect-timing", "identity-scoped-cache"])
        # no paths: the note with two of the asked tags leads, then the single-tag notes by id
        got = search.query(self.layout, tags=["tanstack-query", "pii"], limit=10)
        self.assertEqual(self.ids(got), ["mutation-cache-pii", "fixture-pii", "query-freshness"])

    def test_limit_is_clamped_and_total_reports_the_rest(self):
        self.corpus()
        got = search.query(self.layout, paths=[f"{API}/orders/orders.test.ts"], limit=2)
        self.assertEqual((len(got.hits), got.total), (2, 7))
        self.assertEqual(len(search.query(self.layout, paths=[f"{API}/orders/orders.test.ts"], limit=99).hits), 7)
        self.assertEqual(len(search.query(self.layout, paths=[f"{API}/orders/orders.test.ts"], limit=0).hits), 1)

    def test_superseded_notes_are_never_returned_by_a_query(self):
        self.corpus()
        old = make_note([e for e in load_notes() if "readme" in e["id"]][0])
        directory = self.layout.directory("superseded")
        directory.mkdir(parents=True)
        (directory / f"{old.id}.md").write_text(store.render(old))
        (self.layout.path("approved", old.id)).unlink()
        self.assertEqual(self.ids(search.query(self.layout, paths=["README.md"])), [])
        self.assertEqual(store.get(self.layout, old.id).where, "superseded")


class IndexCache(Hermetic):
    def test_built_then_hit_then_rebuilt_when_a_note_changes(self):
        self.corpus()
        cache = self.layout.cache / search.INDEX_NAME
        self.assertEqual(search.query(self.layout, query="truncate").cache, "built")
        self.assertTrue(cache.is_file())
        self.assertEqual(search.query(self.layout, query="truncate").cache, "hit")
        path = self.layout.path("approved", "kn-2026-10-01-test-truncate")
        path.write_text(path.read_text().replace("per-test schema", "private zzquux"))
        self.assertEqual(search.query(self.layout, query="zzquux").cache, "built")
        self.assertEqual(self.ids(search.query(self.layout, query="zzquux")), ["test-truncate"])

    def test_a_corrupt_cache_is_rebuilt(self):
        self.corpus()
        search.query(self.layout, query="truncate")
        (self.layout.cache / search.INDEX_NAME).write_text("{not json")
        got = search.query(self.layout, query="truncate")
        self.assertEqual(got.cache, "built")
        self.assertTrue(got.hits)

    def test_a_cache_directory_that_is_a_symlink_is_not_written_through(self):
        self.corpus()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.layout.cache.symlink_to(elsewhere)
        got = search.query(self.layout, query="truncate")
        self.assertEqual(got.cache, "memory")
        self.assertTrue(got.hits)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_note_file_that_does_not_parse_is_listed_and_never_hides_the_others(self):
        self.corpus()
        (self.layout.directory("approved") / "kn-2026-10-01-broken.md").write_text("---\nid: kn-2026-10-01-broken\n")
        got = search.query(self.layout, query="truncate")
        self.assertTrue(got.hits)
        self.assertEqual([bad["path"].rsplit("/", 1)[1] for bad in got.invalid], ["kn-2026-10-01-broken.md"])

    def test_misplaced_and_duplicate_notes_are_invalid(self):
        self.corpus()
        wrong = make_note([e for e in load_notes() if "poisoned" in e["id"]][0])  # a candidate placed in approved/
        (self.layout.directory("approved") / f"{wrong.id}.md").write_text(store.render(wrong))
        approved = make_note([e for e in load_notes() if "readme" in e["id"]][0])
        (self.layout.directory("candidate") / f"{approved.id}.md").write_text(store.render(approved))
        notes, invalid = store.scan(self.layout)
        problems = " | ".join(p for bad in invalid for p in bad["problems"])
        self.assertIn("status is candidate but the file is in the approved directory", problems)
        self.assertIn("status is approved but the file is in the candidates directory", problems)

    def test_a_symlinked_note_file_and_a_symlinked_directory_are_not_read(self):
        self.corpus()
        outside = self.tmp / "outside.md"
        outside.write_text(fixture("valid.md"))
        (self.layout.directory("approved") / "kn-2026-10-31-test-cleanup-shared-tables.md").symlink_to(outside)
        notes, invalid = store.scan(self.layout)
        self.assertNotIn("kn-2026-10-31-test-cleanup-shared-tables", [n.id for n in notes])
        self.assertTrue(any("kn-2026-10-31-test-cleanup-shared-tables.md" in bad["path"] for bad in invalid))
        moved = self.tmp / "moved"
        self.layout.directory("candidate").rename(moved)
        self.layout.directory("candidate").symlink_to(moved)
        notes, invalid = store.scan(self.layout)
        self.assertFalse([n for n in notes if n.where == "candidate"])
        self.assertTrue(any(bad["path"].endswith("candidates") for bad in invalid))


class Versions(unittest.TestCase):
    RANGES = [
        (">=0.45 <0.46", "0.45.0", True), (">=0.45 <0.46", "0.45.9", True), (">=0.45 <0.46", "0.46.0", False),
        (">=0.45 <0.46", "0.44.9", False), (">=0.45, <0.46", "0.45.1", True), ("^1.2.3", "1.9.0", True), ("^1.2.3", "2.0.0", False),
        ("^1.2.3", "1.2.2", False), ("^0.45", "0.45.7", True), ("^0.45", "0.46.0", False), ("^0.0.3", "0.0.3", True),
        ("^0.0.3", "0.0.4", False), ("~1.2.3", "1.2.9", True), ("~1.2.3", "1.3.0", False), ("~1", "1.9.9", True),
        ("1.2.x", "1.2.5", True), ("1.2.x", "1.3.0", False), ("1.2", "1.2.0", True), ("1.2.3", "1.2.3", True),
        ("1.2.3", "1.2.4", False), ("*", "9.9.9", True), (">=5 <6", "5.62.1", True), (">=5 <6", "6.0.0", False),
        (">1.2", "1.3.0", True), (">1.2", "1.2.9", False), ("<=1.2", "1.2.9", True), ("<=1.2", "1.3.0", False),
        ("1.x || 3.x", "3.1.0", True), ("1.x || 3.x", "2.0.0", False), ("=1.2.3", "v1.2.3-beta.1", True),
    ]

    def test_range_table(self):
        for spec, version, expected in self.RANGES:
            self.assertEqual(freshness.satisfies(version, spec), expected, f"{version} in {spec}")

    def test_unreadable_ranges_and_versions(self):
        for bad in ("", ">= 1", "1.2.3.4", "x.2", "latest", "1.2.3 ||", "a" * 101, ">=1 <"):
            with self.assertRaises(ValueError, msg=bad):
                freshness.parse_range(bad)
        with self.assertRaises(ValueError):
            freshness.parse_version("latest")

    def test_a_non_matching_range_is_flagged_stale_by_version_and_not_dropped(self):
        stale, unchecked = freshness.version_flags({"drizzle-orm": ">=0.45 <0.46"}, lambda name: "0.46.1")
        self.assertEqual((stale, unchecked), (["drizzle-orm"], []))
        self.assertEqual(freshness.version_flags({"drizzle-orm": ">=0.45 <0.46"}, lambda name: "0.45.3"), ([], []))

    def test_an_unpinned_package_or_a_missing_reader_is_unchecked_not_stale(self):
        spec = {"zod": "^4", "drizzle-orm": ">=0.45 <0.46"}
        self.assertEqual(freshness.version_flags(spec, None), ([], ["drizzle-orm", "zod"]))
        self.assertEqual(freshness.version_flags(spec, lambda n: "4.6.7" if n == "zod" else None), ([], ["drizzle-orm"]))
        self.assertEqual(freshness.version_flags(spec, mock.Mock(side_effect=RuntimeError("lockfile broke"))), ([], ["drizzle-orm", "zod"]))
        pinned = mock.Mock(version="4.6.7")  # an object with .version, as localdocs.lock.pin returns
        self.assertEqual(freshness.version_flags({"zod": "^4"}, lambda n: pinned), ([], []))


class PathFlags(Hermetic):
    def setUp(self):
        super().setUp()
        for rel, text in {f"{API}/foo.test.ts": "a\nb\nc\n", f"{API}/orders/orders.ts": "x\n", "README.md": "r\n"}.items():
            (self.project / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.project / rel).write_text(text)

    def note(self, **kw):
        base = dict(id="kn-2026-10-31-x", kind="lesson", scope=[f"{API}/**/*.test.ts"], source="human", body="b",
                    evidence=[f"{API}/foo.test.ts:3"], created="2026-10-31", last_reviewed="2026-10-31")
        return store.Note(**{**base, **kw})

    def reasons(self, note):
        return freshness.path_reasons(note, self.project, freshness.Tree(self.project), search.glob_matches)

    def test_a_live_scope_and_evidence_have_no_reasons(self):
        self.assertEqual(self.reasons(self.note()), [])

    def test_a_scope_glob_matching_no_file_is_stale_by_path(self):
        self.assertEqual(self.reasons(self.note(scope=[f"{API}/**/*.test.ts", f"{API}/gone/**"])), [f"scope_no_match:{API}/gone/**"])

    def test_evidence_naming_a_missing_file_or_a_line_past_the_end(self):
        got = self.reasons(self.note(evidence=[f"{API}/foo.test.ts:4", f"{API}/missing.ts:1", "review:r-1#F2", f"{API}/foo.test.ts:3"]))
        self.assertEqual(got, [f"evidence_moved:{API}/foo.test.ts:4", f"evidence_missing:{API}/missing.ts"])

    def test_evidence_naming_a_sensitive_or_outside_path_is_neither_read_nor_flagged(self):
        (self.project / ".env.local").write_text("SECRET=1\n")
        self.assertEqual(self.reasons(self.note(evidence=[".env.local:1", "../outside.ts:1"])), [])

    def test_assess_combines_flags_and_reports_age(self):
        note = self.note(scope=["nothing/**"], versions={"zod": "^4"}, last_reviewed="2026-01-01")
        got = freshness.assess(note, self.project, freshness.Tree(self.project), lambda n: "3.0.0", search.glob_matches, "2026-10-31")
        self.assertEqual(got["flags"], ["stale_by_version", "stale_by_path"])
        self.assertEqual(got["stale_versions"], ["zod"])
        self.assertEqual(got["age_days"], 303)
        fresh = freshness.assess(self.note(), self.project, freshness.Tree(self.project), None, search.glob_matches, "2026-10-31")
        self.assertEqual(fresh, {"flags": []})

    def test_the_tree_listing_agrees_between_git_and_a_walk(self):
        (self.project / "node_modules" / "p").mkdir(parents=True)
        (self.project / "node_modules" / "p" / "i.js").write_text("")
        walked = freshness.Tree(self.project).files
        self.assertNotIn("node_modules/p/i.js", walked)
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        (self.project / ".gitignore").write_text("node_modules/\n")
        listed = freshness.Tree(self.project).files
        self.assertEqual(sorted(set(listed) - {".gitignore"}), sorted(walked))


class Screens(Hermetic):
    def test_a_secret_looking_body_is_refused_without_echoing_it_and_nothing_is_written(self):
        note = make_note(load_notes()[0])
        note.id, note.status, note.body = "kn-2026-10-31-leaky", "candidate", f"Use the key {AWS} for the test account."
        with self.assertRaises(redact.Refused) as caught:
            store.add_candidate(self.layout, note, screen=redact.screen_note)
        self.assertNotIn(AWS, str(caught.exception))
        self.assertNotIn("AKIA", str(caught.exception))
        self.assertRegex(str(caught.exception), r"contains 1 secret-like value\(s\) \(")
        self.assertFalse(self.layout.directory("candidate").exists())

    def test_a_secret_in_the_frontmatter_is_refused_too(self):
        note = make_note(load_notes()[0])
        note.id, note.status = "kn-2026-10-31-leaky-evidence", "candidate"
        note.evidence = [f"apps/x.ts:1 {AWS}"]
        with self.assertRaises(redact.Refused):
            store.add_candidate(self.layout, note, screen=redact.screen_note)

    def test_a_private_tag_is_refused(self):
        note = make_note(load_notes()[0])
        note.id, note.status, note.tags = "kn-2026-10-31-private", "candidate", ["private"]
        with self.assertRaisesRegex(redact.Refused, "private/sensitive"):
            store.add_candidate(self.layout, note, screen=redact.screen_note)

    def test_guard_redacts_output(self):
        self.assertNotIn(AWS, redact.guard(f"x {AWS} y"))
        self.assertNotIn(AWS, json.dumps(redact.guard_json({"a": [f"k {AWS}"], AWS: 1})))

    def test_check_path_accepts_inside_and_normalises(self):
        (self.project / "a").mkdir()
        self.assertEqual(redact.check_path(self.project, "a/./b/../c.ts"), "a/c.ts")  # a file that does not exist is judged by place
        self.assertEqual(redact.check_path(self.project, str(self.project / "a" / "c.ts")), "a/c.ts")

    def test_check_path_refuses_outside_sensitive_and_unusable(self):
        (self.project / "link").symlink_to(self.tmp)
        (self.project / "config").mkdir()
        refused = ["../x.ts", str(self.tmp / "x.ts"), "link/x.ts", ".env", ".env.local", "config/local.json", "agent/auth.json",
                   ".ssh/id_rsa", "", "a\0b", ".", 5, None]
        for value in refused:
            with self.assertRaises(redact.Refused, msg=repr(value)):
                redact.check_path(self.project, value)

    def test_check_path_follows_a_link_that_stays_inside(self):
        (self.project / "real").mkdir()
        (self.project / "alias").symlink_to(self.project / "real")
        self.assertEqual(redact.check_path(self.project, "alias/f.ts"), "real/f.ts")

    def test_check_path_refuses_a_link_to_a_sensitive_file_inside_the_project(self):
        (self.project / ".env").write_text("A=1\n")
        (self.project / "notes.txt").symlink_to(self.project / ".env")
        with self.assertRaises(redact.Refused):
            redact.check_path(self.project, "notes.txt")


class Writing(Hermetic):
    def candidate(self, ident="kn-2026-10-31-first", **kw):
        base = dict(id=ident, kind="convention", scope=[f"{API}/**/*.test.ts"], source="human", body="Do the thing.",
                    evidence=[f"{API}/a.test.ts:1"], title="First", tags=["t"])
        return store.Note(**{**base, **kw})

    def test_add_writes_a_candidate_forces_status_and_never_overwrites(self):
        path = store.add_candidate(self.layout, self.candidate(status="approved", last_reviewed="2026-01-01"), screen=redact.screen_note)
        self.assertEqual(Path(path), self.layout.path("candidate", "kn-2026-10-31-first"))
        written = store.parse(Path(path).read_text())
        self.assertEqual((written.status, written.last_reviewed), ("candidate", ""))
        before = Path(path).read_text()
        with self.assertRaisesRegex(store.StoreError, "already exists"):
            store.add_candidate(self.layout, self.candidate(body="Different."))
        self.assertEqual(Path(path).read_text(), before)
        self.assertEqual([p.name for p in self.layout.directory("candidate").iterdir()], ["kn-2026-10-31-first.md"])  # no temp left

    def test_add_refuses_an_id_that_exists_in_another_directory(self):
        self.corpus()
        taken = self.candidate(ident="kn-2026-10-01-test-truncate")
        with self.assertRaisesRegex(store.StoreError, "already exists in approved"):
            store.add_candidate(self.layout, taken)

    def test_add_requires_evidence(self):
        with self.assertRaisesRegex(store.NoteError, "evidence is required"):
            store.add_candidate(self.layout, self.candidate(evidence=[]))

    def test_add_into_a_symlinked_directory_is_refused(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.layout.knowledge.mkdir(parents=True)
        self.layout.directory("candidate").symlink_to(elsewhere)
        with self.assertRaisesRegex(store.StoreError, "unsafe knowledge location"):
            store.add_candidate(self.layout, self.candidate())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_new_id_is_dated_slugged_and_unique(self):
        with mock.patch.dict(os.environ, {"TP_CTX_TODAY": "2026-10-31"}):
            first = store.new_id(self.layout, "Tests: don't TRUNCATE shared tables!!")
            self.assertEqual(first, "kn-2026-10-31-tests-don-t-truncate-shared-tables")
            store.add_candidate(self.layout, self.candidate(ident=first))
            self.assertEqual(store.new_id(self.layout, "Tests: don't TRUNCATE shared tables!!"), first + "-2")
            self.assertEqual(store.new_id(self.layout, "!!!"), "kn-2026-10-31-note")

    def test_approve_moves_the_candidate_and_sets_status_and_review_date(self):
        store.add_candidate(self.layout, self.candidate())
        with mock.patch.dict(os.environ, {"TP_CTX_TODAY": "2026-11-02"}):
            plan = store.approve_plan(self.layout, "kn-2026-10-31-first", screen=redact.screen_note)
            self.assertEqual(plan[1].status, "approved")
            self.assertTrue(self.layout.path("candidate", "kn-2026-10-31-first").exists())  # planning changes nothing
            store.approve_apply(self.layout, plan)
        self.assertFalse(self.layout.path("candidate", "kn-2026-10-31-first").exists())
        approved = store.load(self.layout, "approved", "kn-2026-10-31-first")
        self.assertEqual((approved.status, approved.last_reviewed, approved.created), ("approved", "2026-11-02", "2026-10-31"))

    def test_approve_supersedes_moves_the_old_note_and_keeps_it_readable_by_id(self):
        self.corpus()
        old = "kn-2026-10-01-test-truncate"
        store.add_candidate(self.layout, self.candidate(supersedes=[old]))
        store.approve_apply(self.layout, store.approve_plan(self.layout, "kn-2026-10-31-first"))
        self.assertFalse(self.layout.path("approved", old).exists())
        self.assertEqual(store.get(self.layout, old).where, "superseded")
        self.assertNotIn("test-truncate", self.ids(search.query(self.layout, paths=[f"{API}/orders/orders.test.ts"], limit=10)))

    def test_approve_with_a_missing_supersedes_target_changes_nothing(self):
        store.add_candidate(self.layout, self.candidate(supersedes=["kn-2026-10-01-nothing"]))
        with self.assertRaisesRegex(store.StoreError, "supersedes kn-2026-10-01-nothing"):
            store.approve_plan(self.layout, "kn-2026-10-31-first")
        self.assertTrue(self.layout.path("candidate", "kn-2026-10-31-first").exists())
        self.assertFalse(self.layout.directory("approved").exists())

    def test_approve_refuses_an_existing_approved_id_an_unknown_candidate_and_a_changed_secret(self):
        store.add_candidate(self.layout, self.candidate())
        shutil_target = self.layout.path("approved", "kn-2026-10-31-first")
        shutil_target.parent.mkdir(parents=True)
        shutil_target.write_text("x")
        with self.assertRaisesRegex(store.StoreError, "already exists"):
            store.approve_plan(self.layout, "kn-2026-10-31-first")
        with self.assertRaisesRegex(store.StoreError, "no candidate note"):
            store.approve_plan(self.layout, "kn-2026-10-31-nope")
        shutil_target.unlink()
        path = self.layout.path("candidate", "kn-2026-10-31-first")
        path.write_text(path.read_text().replace("Do the thing.", f"Use {AWS}."))  # edited by hand after it was screened
        with self.assertRaises(redact.Refused):
            store.approve_plan(self.layout, "kn-2026-10-31-first", screen=redact.screen_note)
        self.assertTrue(path.exists())

    def test_a_candidate_cannot_be_approved_by_editing_its_status_in_place(self):
        store.add_candidate(self.layout, self.candidate())
        path = self.layout.path("candidate", "kn-2026-10-31-first")
        path.write_text(path.read_text().replace("status: candidate", "status: approved"))
        notes, invalid = store.scan(self.layout)
        self.assertEqual(notes, [])
        self.assertIn("status is approved but the file is in the candidates directory", invalid[0]["problems"][0])

    def test_sources_json(self):
        self.assertEqual(store.list_sources(self.layout), [])
        self.layout.knowledge.mkdir(parents=True)
        self.layout.sources.write_text(json.dumps({"sources": [{"id": "engineering-context", "path": str(self.tmp)}]}))
        self.assertEqual(store.list_sources(self.layout), [{"id": "engineering-context", "path": str(self.tmp), "exists": True}])
        self.layout.sources.write_text("[1]")
        with self.assertRaises(store.StoreError):
            store.list_sources(self.layout)

    def test_find_project(self):
        deep = self.project / "a" / "b"
        deep.mkdir(parents=True)
        (self.project / ".tmux-pi").mkdir()
        self.assertEqual(store.find_project(deep), self.project)
        self.assertEqual(store.find_project(deep, env={"TP_CTX_PROJECT": str(self.tmp)}), self.tmp)
        self.assertIsNone(store.find_project(deep, env={"TP_CTX_PROJECT": str(self.tmp / "none")}))

    def corpus(self):
        for entry in load_notes():
            self.put(entry)

    def ids(self, result):
        return [h.note.id.removeprefix("kn-2026-10-01-") for h in result.hits]

    def put(self, entry):
        note = make_note(entry)
        directory = self.layout.directory("approved" if note.status == "approved" else "candidate")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{note.id}.md").write_text(store.render(note))


if __name__ == "__main__":
    unittest.main()
