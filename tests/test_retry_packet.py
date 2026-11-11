"""tp-agent retry: a retry that carries its fix brief (--reason naming an .md file) in the request it starts with, and the
gate on a finding family a reviewer names twice. The fixture is the orchestration one (fake tmux, no model runs)."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import copy
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_orchestration  # noqa: E402  (the module, so its own tests are not collected twice)
from fake_tools import fake_tmux  # noqa: E402
from lane_fixture import TMUX, TmuxCase  # noqa: E402

MODEL = test_orchestration.MODEL
OLD = "Make a focused change"


class RetryCase(unittest.TestCase):
    """The orchestration fixture (fake tmux, no model runs). The state tree of a settled author, or of a reviewed chain, is built
    once per class (a real dispatch, real retries) and put back by copy for a later test that asks for the same one first thing."""
    for _name in ("raw", "cli", "task_dir", "packet", "settle", "dispatch", "retry"):
        locals()[_name] = test_orchestration.OrchestrationTests.__dict__[_name]
    del _name

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.class_root = Path(temp.name)
        cls.snapshots = {}  # key -> (copy of the state tree, the item the build returned)

    def setUp(self):
        self.root = self.class_root / "run"  # one path for every test, so a copied state tree names the same paths
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.build_root()

    def build_root(self):
        self.root.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = self.project / ".tmux-pi"
        (self.state / "tasks").mkdir(parents=True)
        fake = self.root / "bin"
        fake.mkdir()
        fake_tmux(fake, '''case "$1" in
 has-session) exit 0 ;;
 show-options) printf '%s\\n' "$TP_STATE_DIR" ;;
 new-window) printf '%%fake\\n' ;;
 send-keys) exit 0 ;;
 *) exit 0 ;;
esac
''')
        self.env = {**os.environ, "TP_STATE_DIR": str(self.state), "TP_SESSION": "fixture", "TP_WORK_DIR": str(self.root / "work-store"),
                    "PATH": str(fake) + os.pathsep + os.environ["PATH"]}
        self.env.pop("TMUX", None)

    def state_files(self):
        return sorted(str(p.relative_to(self.state)) for p in self.state.rglob("*"))

    def built_once(self, key, build):
        """What `build()` returns, and the state tree it leaves. The first call for `key` in the class builds for real and keeps a copy
        of the tree; a later call puts the copy back, but only while the state is still empty (no task, no other file), else it
        builds for real, as a second call inside one test always did."""
        if self.state_files() != ["tasks"]:
            return build()
        saved = self.snapshots.get(key)
        if saved is None:
            item = build()
            kept = self.class_root / f"snap-{len(self.snapshots)}"
            shutil.copytree(self.state, kept, symlinks=True)
            self.snapshots[key] = (kept, copy.deepcopy(item))
            return item
        shutil.rmtree(self.state)
        shutil.copytree(saved[0], self.state, symlinks=True)
        return copy.deepcopy(saved[1])

    def request(self, item):
        return (self.task_dir(item) / "request.md").read_text()

    def settled(self):
        return self.built_once(("settled",), self.build_settled)

    def build_settled(self):
        item = self.dispatch()
        self.settle(item, "RESULT_STATUS: complete\nDone.\n")
        return item

    def fix_brief(self, name="fix.md", text="# Fix\n\nDo the other thing.\n"):
        path = self.root / name
        path.write_text(text)
        return str(path)


class FixBriefTests(RetryCase):
    def sentence(self, brief):
        return f"Read and carry out the fix brief {brief} in full. It replaces the task below where they differ."

    def test_a_reason_naming_a_brief_file_starts_the_new_request_with_it(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(self.request(again), f"{self.sentence(brief)}\n\n{OLD}\n")
        self.assertIn(f"# Task\n\n{self.sentence(brief)}\n\n{OLD}\n", self.packet(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))  # the Notes line stays

    def test_with_task_the_request_is_that_task_only(self):
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief, "--task", "T")
        self.assertEqual(self.request(again), "T\n")
        self.assertNotIn("fix brief", self.request(again))
        self.assertIn(f"Retry of {item['id']}: {brief}. ", self.packet(again))
        self.assertNotIn("not used as a fix brief", self.packet(again))

    def test_a_plain_text_reason_leaves_the_request_as_it_was(self):
        for reason in ("plain words", "fix.md", "see /abs/fix.md for what changed", "/abs/fix.txt"):
            with self.subTest(reason=reason):
                item = self.settled()
                (self.root / "fix.md").write_text("relative, in the wrong place\n")
                again = self.retry(item, reason)
                self.assertEqual(self.request(again), f"{OLD}\n")
                self.assertNotIn("fix brief", self.packet(again))

    def test_a_refused_file_is_not_used_and_the_notes_say_why(self):
        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        secret = self.root / ".env.md"
        secret.write_text("SECRET-FIX-BRIEF\n")
        (self.root / "dir.md").mkdir()
        (self.root / "empty.md").write_text("")
        cases = [(str(secret), "is a .env file"),
                 (str(home / ".config/tmux-pi/trust/never-written-fix.md"), "is inside the trust store"),  # never created or read
                 (str(home / ".config/tmux-pi/state/trust/never-written-fix.md"), "is inside the trust store"),
                 (str(self.root / "missing.md"), "does not exist"),
                 (str(self.root / "dir.md"), "is not a regular file"),
                 (str(self.root / "empty.md"), "is empty")]
        for path, why in cases:
            with self.subTest(path=path):
                item = self.settled()
                again = self.retry(item, path)
                self.assertEqual(self.request(again), f"{OLD}\n")
                packet = self.packet(again)
                self.assertNotIn("Read and carry out the fix brief", packet)
                self.assertNotIn("SECRET-FIX-BRIEF", packet)
                self.assertIn("not used as a fix brief", packet)
                self.assertIn(why, packet)

    def test_a_retry_of_a_retry_with_plain_words_keeps_the_fix_brief_of_the_attempt_it_replaces(self):
        item = self.settled()
        brief = self.fix_brief()
        first = self.retry(item, brief)
        self.settle(first, "RESULT_STATUS: complete\nDone.\n")
        second = self.retry(first, "still wrong")
        self.assertEqual(self.request(second), self.request(first))

    def test_the_review_of_the_retry_reads_the_fix_brief_it_is_named_in(self):
        sys.path.insert(0, str(Path(test_orchestration.ROOT) / "lib"))
        import review_verdict
        item = self.settled()
        brief = self.fix_brief()
        again = self.retry(item, brief)
        self.assertEqual(review_verdict.named_briefs(self.request(again)), [brief])


def review_text(*families, verdict="REQUEST_CHANGES"):
    if verdict != "REQUEST_CHANGES":
        return f"{verdict}\nRESULT_STATUS: complete\n\nCoverage\nI read the changed files and their callers.\n"
    labels = "".join(f"F{n} [defect] one cell of the matrix is wrong.\nFamily: {name}\nCloses by: code\n\n" for n, name in enumerate(families, 1))
    labels = labels or "F1 [defect] wrong.\nCloses by: code\n"  # no backslash inside an f-string expression: Python < 3.12
    return (f"REQUEST_CHANGES\nRESULT_STATUS: complete\n{labels}\n"
            "Coverage\nI read the changed files and their callers.\n")


def rows(items):
    """A list of records that each carry an id (a task row of session-index.json)."""
    return bool(items) and all(isinstance(i, dict) and "id" in i for i in items)


def rows_backwards(node):
    """`node` with every list of rows reversed, as the same index would read if the listing had come in the other order."""
    if isinstance(node, dict):
        return {k: rows_backwards(v) for k, v in node.items()}
    if isinstance(node, list):
        items = [rows_backwards(v) for v in node]
        return items[::-1] if rows(items) else items
    return node


class RepeatedFamilyGateTests(RetryCase):
    def attach_review(self, item, review_id, *families):
        """A review task `review_id` with a reply naming `families`, recorded on the author `item` (reviews, as tp-review writes them)."""
        task = self.state / "tasks" / review_id
        task.mkdir(exist_ok=True)  # a test that chains twice (subTests) reuses the ids
        (task / "meta.json").write_text(json.dumps({"id": review_id, "role": "reviewer"}))
        (task / "result.md").write_text(review_text(*families))
        path = self.task_dir(item) / "meta.json"
        meta = json.loads(path.read_text())
        meta["reviews"] = meta.get("reviews", []) + [review_id]
        path.write_text(json.dumps(meta))

    def chain(self, *rounds):
        return self.built_once(("chain", json.dumps(rounds)), lambda: self.build_chain(*rounds))

    def build_chain(self, *rounds):
        """The author reviewed once per round (each round is the families its review names), each retried from the last (plainly, or
        with --route design where the gate asks for a route); the newest author is returned. Every review is `review-N`."""
        item = self.settled()
        self.attach_review(item, "review-1", *rounds[0])
        for number, families in enumerate(rounds[1:], 2):
            run = self.plain_retry(item)
            if run.returncode:
                run = self.plain_retry(item, "--route", "design")
            self.assertEqual(run.returncode, 0, run.stderr)
            item = json.loads(run.stdout)
            self.settle(item, "RESULT_STATUS: complete\nDone.\n")
            self.attach_review(item, f"review-{number}", *families)
        return item

    def state_digest(self):
        """Every file of the state tree by path, with task ids, selection ids and times replaced, so two builds can be compared."""
        metas = {p.parent.name: json.loads(p.read_text()) for p in (self.state / "tasks").glob("*/meta.json")}

        def depth(task):  # how many retries it follows
            return 0 if not metas.get(task, {}).get("parent") else 1 + depth(metas[task]["parent"])
        owner = {r: author for author, meta in metas.items() for r in meta.get("reviews", [])}  # the author a review was recorded on
        place = {r: n for meta in metas.values() for n, r in enumerate(meta.get("reviews", []))}  # its place among that author's reviews

        def key(task):
            """An id is named by facts both builds share, never by its random digits or where the listing put it: its role, the retries
            behind it (a review: behind the author it was recorded on), and its place among that author's reviews."""
            return (str(metas[task].get("role")), depth(owner.get(task, task)), place.get(task, 0))
        same = {}
        for task in metas:
            same.setdefault(key(task), []).append(task)
        tied = {k: sorted(v) for k, v in same.items() if len(v) > 1}
        if tied:  # the listing would decide which is which
            raise AssertionError(f"state_digest cannot tell these tasks apart by role, retries and place among reviews: {tied}")
        names = {}
        for task in sorted(metas, key=key):
            role = metas[task].get("role")
            names[task] = f"{role}-{sum(1 for n in names if metas[n].get('role') == role)}"
        out = {}
        for path in sorted(self.state.rglob("*")):
            if path.is_file() and not path.name.endswith(".lock"):
                out[str(path.relative_to(self.state))] = path.read_text()
        def clean(text):
            for task, name in names.items():
                text = text.replace(task, name)
            text = re.sub(r"sel-[0-9a-f]{32}", "SEL", text)
            text = re.sub(r'"seconds":[0-9.]+', '"seconds":0', text)  # the time the router took
            return re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(?:Z|\+00:00)?", "TIME", text)

        def tidy(path, text):
            """tp-agent lists the rows of session-index.json by `updated`, a time to the second, so tasks made in the same second come
            in the order the directory listing gave them: a list of rows with an id is compared by (cleaned) id, not by position."""
            if not path.endswith(".json"):
                return text
            try:
                value = json.loads(text)
            except ValueError:
                return text

            def by_id(node):
                if isinstance(node, dict):
                    return {k: by_id(v) for k, v in node.items()}
                if isinstance(node, list):
                    items = [by_id(v) for v in node]
                    return sorted(items, key=lambda i: (str(i["id"]), json.dumps(i, sort_keys=True))) if rows(items) else items
                return node
            return json.dumps(by_id(value), indent=1, sort_keys=True)
        return {clean(path): tidy(path, clean(text)) for path, text in out.items()}

    def listed(self, order):
        """While active, `Path.glob` returns what it found in the order `order` gives, as a different file system would list it."""
        real = Path.glob
        return mock.patch.object(Path, "glob", lambda path, pattern, *a, **kw: iter(order(list(real(path, pattern, *a, **kw)))))

    def test_the_digest_refuses_two_tasks_it_cannot_tell_apart(self):
        for name in ("review-x", "review-y"):  # two reviews no author names: nothing but the listing would number them
            (self.state / "tasks" / name).mkdir()
            (self.state / "tasks" / name / "meta.json").write_text(json.dumps({"id": name, "role": "reviewer"}))
        with self.assertRaisesRegex(AssertionError, r"cannot tell these tasks apart.*review-x.*review-y"):
            self.state_digest()

    def tasks(self):
        return sorted(p.name for p in (self.state / "tasks").iterdir())

    def plain_retry(self, item, *extra):
        return self.raw("retry", item["id"], "--reason", "again", "--worker", "pi", "--model", MODEL, *extra)

    def test_a_family_named_in_two_rounds_refuses_a_plain_retry(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 1, run.stdout)
        for word in ("state-matrix", "review-1", "review-2", "--route rule-first", "--route design", "--route park"):
            self.assertIn(word, run.stderr)
        self.assertEqual(self.tasks(), before)  # no task was spawned
        # the chain this class restores from a copy is the one a real build makes (ids and times aside)
        first = self.state_digest()
        with self.listed(sorted):  # the two reviews tie on role and depth alone: the names must not follow the listing, whichever way it runs
            forward = self.state_digest()
        with self.listed(lambda found: sorted(found, reverse=True)):
            self.assertEqual(self.state_digest(), forward)
        self.assertEqual(first, forward)
        index = self.state / "session-index.json"  # tp-agent lists same-second tasks in listing order: the rows in the other order are the same build
        written = index.read_text()
        self.assertTrue(any(len(v) > 1 and rows(v) for v in json.loads(written).values() if isinstance(v, list)), "no list of rows to turn round")
        index.write_text(json.dumps(rows_backwards(json.loads(written))))
        self.assertEqual(self.state_digest(), forward)
        index.write_text(written)
        shutil.rmtree(self.root)
        self.build_root()
        self.chain(["state-matrix"], ["state-matrix"])  # the cache is warm by now: this one is restored
        restored = self.state_digest()
        shutil.rmtree(self.root)
        self.build_root()
        self.build_chain(["state-matrix"], ["state-matrix"])
        self.assertEqual(restored, self.state_digest())
        self.assertEqual(first, restored)
        self.assertGreater(len(restored), 10)
        (self.state / "tasks" / "review-1" / "result.md").write_text("a different first review")  # the two reviews are two names
        self.assertNotEqual(restored, self.state_digest())

    def test_the_families_are_matched_by_name_with_the_case_folded(self):
        item = self.chain(["State-Matrix"], ["state-matrix"])
        refused = self.plain_retry(item)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("finding family state-matrix was named by review review-1 and again by review review-2", refused.stderr)  # not the two-in-a-row refusal

    def test_route_design_and_rule_first_retry_and_the_notes_say_which_route(self):
        for route in ("design", "rule-first"):
            with self.subTest(route=route):
                item = self.chain(["state-matrix"], ["state-matrix"])
                run = self.plain_retry(item, "--route", route)
                self.assertEqual(run.returncode, 0, run.stderr)
                packet = self.packet(json.loads(run.stdout))
                self.assertIn(f"route {route}", packet)
                self.assertIn("state-matrix", packet)
                self.assertIn("the brief must change the rule or the design, not one more site", packet)
                self.assertTrue(any(line.startswith(f"- Retry of {item['id']}") and f"route {route}" in line for line in packet.splitlines()))
                new_meta = json.loads((self.state / "tasks" / json.loads(run.stdout)["id"] / "meta.json").read_text())
                self.assertEqual(new_meta.get("gate"), route)  # the record lib/review_rounds.py reads to start a fresh findings window

    def test_the_route_note_is_not_replayed_into_the_next_retry(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        again = json.loads(self.plain_retry(item, "--route", "design").stdout)
        self.settle(again, "RESULT_STATUS: complete\nDone.\n")
        self.assertNotIn("route design", self.packet(self.retry(again, "later")))

    def test_route_park_does_not_retry_and_writes_parked_md(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.tasks(), before)
        parked = (self.task_dir(item) / "parked.md").read_text()
        for word in ("state-matrix", "review-1", "review-2"):
            self.assertIn(word, parked)
        self.assertIn("owner decides", run.stdout + run.stderr)
        self.assertEqual(Path(json.loads(run.stdout)["parked"]).resolve(), (self.task_dir(item) / "parked.md").resolve())

    def test_route_park_replaces_a_link_at_parked_md_and_writes_nothing_through_it(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        target = self.root / "elsewhere.txt"
        target.write_text("not the parked record\n")
        parked = self.task_dir(item) / "parked.md"
        parked.symlink_to(target)
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(target.read_text(), "not the parked record\n")
        self.assertFalse(parked.is_symlink())
        self.assertTrue(parked.is_file())
        self.assertIn("state-matrix", parked.read_text())

    def test_escalate_alone_does_not_pass_the_gate(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        before = self.tasks()
        run = self.raw("retry", item["id"], "--reason", "again", "--escalate")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("state-matrix", run.stderr)
        self.assertIn("--route", run.stderr)
        self.assertEqual(self.tasks(), before)

    def test_a_family_round_one_did_not_name_allows_a_plain_retry(self):
        item = self.chain(["state-matrix"], ["error-path"])
        (self.state / "tasks" / "review-2" / "result.md").write_text(review_text("error-path", verdict="APPROVED_WITH_NOTES"))
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertNotIn("gate", json.loads((self.state / "tasks" / json.loads(run.stdout)["id"] / "meta.json").read_text()))  # an ungated retry

    def test_a_latest_review_naming_no_family_after_an_approval_allows_a_plain_retry(self):
        item = self.chain(["state-matrix"], [])
        (self.state / "tasks" / "review-2" / "result.md").write_text(review_text(verdict="APPROVED"))
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def test_two_request_changes_in_a_row_refuse_a_plain_retry_with_no_family_named(self):
        for rounds in ([[], []], [["state-matrix"], ["error-path"]]):
            with self.subTest(rounds=rounds):
                item = self.chain(*rounds)
                before = self.tasks()
                run = self.plain_retry(item)
                self.assertEqual(run.returncode, 1, run.stdout)
                for word in ("review-1", "review-2", "REQUEST_CHANGES twice in a row", "--route rule-first", "--route design", "--route park"):
                    self.assertIn(word, run.stderr)
                self.assertNotIn("finding family", run.stderr)
                self.assertEqual(self.tasks(), before)

    def test_an_approval_between_two_request_changes_breaks_the_run(self):
        item = self.chain(["a"], ["b"], ["c"])  # round 2 is gated, so the chain passes through --route design
        (self.state / "tasks" / "review-2" / "result.md").write_text(review_text(verdict="APPROVED"))
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def unreviewed_chain(self, shape):
        """RC=a REQUEST_CHANGES review, --=an author retried and settled with no review at all; the newest author is returned."""
        item = self.settled()
        for number, step in enumerate(shape, 1):
            if number > 1:
                run = self.plain_retry(item)
                if run.returncode:
                    run = self.plain_retry(item, "--route", "design")
                self.assertEqual(run.returncode, 0, run.stderr)
                item = json.loads(run.stdout)
                self.settle(item, "RESULT_STATUS: complete\nDone.\n")
            if step == "RC":
                self.attach_review(item, f"review-{number}")
        return item

    def test_a_retry_nobody_reviewed_breaks_the_run_wherever_it_sits(self):
        for shape, gated in ((["RC", "--", "RC"], False), (["--", "RC", "RC"], True), (["--", "--", "RC"], False),
                             (["RC", "--", "--", "RC"], False), (["RC", "RC"], True), (["RC", "--"], False)):
            with self.subTest(shape=shape):
                item = self.unreviewed_chain(shape)
                self.assertEqual(self.plain_retry(item).returncode, 1 if gated else 0)

    def test_a_review_that_cannot_be_read_breaks_the_run(self):
        item = self.chain([], [])
        (self.state / "tasks" / "review-1" / "result.md").write_text("not a verdict\n")
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def test_the_gate_words_are_true_for_every_caller_and_every_label_shape(self):
        """caller (refusal, route Notes line, parked.md) x label shape -> the exact sentence; one table."""
        unnamed = "reviews review-1 and review-2 both asked for changes (REQUEST_CHANGES twice in a row); "
        named = "finding family state-matrix was named by review review-1 and again by review review-2"
        table = (
            ([[], []], unnamed + "review-1 gave no Family: line and review-2 gave no Family: line"),
            ([["state-matrix"], []], unnamed + "review-1 gave state-matrix and review-2 gave no Family: line"),
            ([[], ["state-matrix"]], unnamed + "review-1 gave no Family: line and review-2 gave state-matrix"),
            ([["state-matrix"], ["error-path"]], unnamed + "review-1 gave state-matrix and review-2 gave error-path"),
            ([["state-matrix"], ["state-matrix"]], named),
        )
        for rounds, words in table:
            with self.subTest(rounds=rounds):
                item = self.chain(*rounds)
                refused = self.plain_retry(item)
                self.assertEqual(refused.returncode, 1, refused.stdout)
                self.assertIn(words + ": another retry would", refused.stderr)
                routed = self.plain_retry(item, "--route", "rule-first")
                self.assertEqual(routed.returncode, 0, routed.stderr)
                self.assertIn(f"by route rule-first: {words};", self.packet(json.loads(routed.stdout)))
                item = self.chain(*rounds)
                self.assertEqual(self.plain_retry(item, "--route", "park").returncode, 0)
                self.assertIn(f"{words[0].upper()}{words[1:]}. ", (self.task_dir(item) / "parked.md").read_text())

    def test_routes_take_an_unnamed_repeat_and_the_notes_and_parked_md_say_so(self):
        item = self.chain([], [])
        run = self.plain_retry(item, "--route", "rule-first")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("both asked for changes", self.packet(json.loads(run.stdout)))
        item = self.chain([], [])
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 0, run.stderr)
        parked = (self.task_dir(item) / "parked.md").read_text()
        for word in ("review-1", "review-2", "REQUEST_CHANGES"):
            self.assertIn(word, parked)

    def test_only_the_latest_review_is_judged_against_the_earlier_ones(self):
        item = self.chain(["state-matrix", "error-path"], ["state-matrix"], ["error-path"])  # round 3 repeats a family of round 1 only
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("error-path", run.stderr)
        self.assertNotIn("family state-matrix", run.stderr)

    def test_an_attempt_not_yet_reviewed_after_a_routed_retry_retries_plainly(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        again = json.loads(self.plain_retry(item, "--route", "design").stdout)
        self.settle(again, "RESULT_STATUS: complete\nDone.\n")
        self.assertEqual(self.plain_retry(again).returncode, 0)

    def test_a_route_without_a_repeated_family_is_refused(self):
        item = self.chain(["state-matrix"], ["error-path"])
        (self.state / "tasks" / "review-2" / "result.md").write_text(review_text("error-path", verdict="APPROVED_WITH_NOTES"))
        before = self.tasks()
        run = self.plain_retry(item, "--route", "park")
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("repeats no family", run.stderr)
        self.assertEqual(self.tasks(), before)
        self.assertFalse((self.task_dir(item) / "parked.md").exists())

    def test_a_review_that_cannot_be_read_is_skipped(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        (self.state / "tasks" / "review-1" / "result.md").unlink()
        self.assertEqual(self.plain_retry(item).returncode, 0)

    def test_a_retry_lineage_that_cannot_be_read_says_the_gate_was_not_checked(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        meta = self.task_dir(item) / "meta.json"
        value = json.loads(meta.read_text())
        parent = value["parent"]
        (self.state / "tasks" / parent / "meta.json").write_text("not json")
        run = self.plain_retry(item)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("repeated-family gate was not checked", run.stderr)


SITE_REVIEW = ("REQUEST_CHANGES\nRESULT_STATUS: complete\nF1 [defect] the matrix cell is wrong at lib/matrix.py:12 and `lib/other.py:40`.\n"
               "Family: state-matrix\nCloses by: code\n\nCoverage\nI read the changed files and their callers.\n")


class SweepTests(RepeatedFamilyGateTests):
    """sweep.md: generated on a gated retry (--route rule-first or design), attached after the task, not replayed, not on a plain retry."""
    for _name in [n for n in dir(RepeatedFamilyGateTests) if n.startswith("test_")]:
        locals()[_name] = None  # the gate's own tests are not run again here
    del _name

    def gated(self, *extra):
        item = self.chain(["state-matrix"], ["state-matrix"])
        (self.state / "tasks" / "review-1" / "result.md").write_text(SITE_REVIEW.replace("lib/matrix.py:12", "lib/early.py:3"))
        (self.state / "tasks" / "review-2" / "result.md").write_text(SITE_REVIEW)
        run = self.plain_retry(item, *extra)
        self.assertEqual(run.returncode, 0, run.stderr)
        return item, json.loads(run.stdout)

    def test_a_gated_retry_gets_sweep_md_in_its_packet_after_the_task_and_keeps_the_route_note(self):
        for route in ("rule-first", "design"):
            with self.subTest(route=route):
                item, again = self.gated("--route", route)
                self.assertTrue((self.task_dir(item) / "sweep.md").is_file())
                packet = self.packet(again)
                for word in ("# Sweep: finding family `state-matrix`", "lib/early.py:3", "lib/matrix.py:12", "lib/other.py:40",
                             "| Site | Disposition | Evidence |", "git -C ", f"route {route}"):
                    self.assertIn(word, packet)
                self.assertLess(packet.index(OLD), packet.index("# Sweep:"))  # after the task
                self.assertIn(f"## .tmux-pi/tasks/{item['id']}/sweep.md", packet)  # the packet names a context file relative to the workdir

    def test_an_unnamed_family_retry_gets_one_too(self):
        item = self.chain([], [])
        run = self.plain_retry(item, "--route", "rule-first")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("# Sweep: the unnamed family", self.packet(json.loads(run.stdout)))

    def test_a_retry_that_was_not_gated_gets_no_sweep(self):
        item = self.chain(["a"], ["b"])
        (self.state / "tasks" / "review-2" / "result.md").write_text(review_text(verdict="APPROVED"))
        again = json.loads(self.plain_retry(item).stdout)
        self.assertNotIn("# Sweep", self.packet(again))
        self.assertFalse((self.task_dir(item) / "sweep.md").exists())

    def test_route_park_writes_no_sweep(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        self.assertEqual(self.plain_retry(item, "--route", "park").returncode, 0)
        self.assertFalse((self.task_dir(item) / "sweep.md").exists())

    def test_the_sweep_is_not_replayed_into_the_next_retry(self):
        item, again = self.gated("--route", "rule-first")
        self.settle(again, "RESULT_STATUS: complete\nDone.\n")
        later = self.retry(again, "later")
        self.assertNotIn("# Sweep", self.packet(later))
        self.assertNotIn("sweep.md", (self.task_dir(later) / "dispatch.json").read_text())

    def test_a_sweep_that_cannot_be_made_does_not_stop_the_retry(self):
        item = self.chain(["state-matrix"], ["state-matrix"])
        (self.task_dir(item) / "sweep.md").mkdir()  # a directory where the file goes: the write fails
        run = self.plain_retry(item, "--route", "design")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("sweep.md was not written", run.stderr)
        self.assertNotIn("# Sweep", self.packet(json.loads(run.stdout)))


@unittest.skipUnless(TMUX, "tmux not installed")
class ParkWithALiveProcessTests(TmuxCase):
    """Parking starts no attempt, so a process alive on the working tree does not stop it; every other route still is stopped."""

    def reviewed(self, task, review_id, family):
        review = self.state / "tasks" / review_id
        review.mkdir()
        (review / "meta.json").write_text(json.dumps({"id": review_id, "role": "reviewer"}))
        (review / "result.md").write_text(review_text(family))
        meta = json.loads((task / "meta.json").read_text())
        (task / "meta.json").write_text(json.dumps(dict(meta, reviews=meta.get("reviews", []) + [review_id])))

    def repeated_family_with_a_live_process(self):
        first, first_task = self.dispatch()
        self.settle(first_task)
        self.reviewed(first_task, "review-1", "state-matrix")
        second = self.cli("retry", first["id"], "--reason", "again", "--worker", "pi", "--model", MODEL)
        second_task = self.state / "tasks" / second["id"]
        self.settle(second_task)
        self.reviewed(second_task, "review-2", "state-matrix")
        _, _, pane, _ = self.live_task()  # another task alive on the same working tree
        self.assertTrue(self.alive(pane))
        return second, second_task

    def test_route_park_writes_parked_md_beside_a_live_process_and_design_is_still_refused(self):
        second, task = self.repeated_family_with_a_live_process()
        before = sorted(p.name for p in (self.state / "tasks").iterdir())
        design = self.raw("retry", second["id"], "--reason", "again", "--worker", "pi", "--model", MODEL, "--route", "design")
        self.assertEqual(design.returncode, 1, design.stdout)
        self.assertIn("a process is alive on the same working tree", design.stderr)
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)
        park = self.raw("retry", second["id"], "--reason", "again", "--route", "park")
        self.assertEqual(park.returncode, 0, park.stderr)
        self.assertIn("state-matrix", (task / "parked.md").read_text())
        self.assertEqual(sorted(p.name for p in (self.state / "tasks").iterdir()), before)  # nothing was started

    def test_a_plain_retry_beside_a_live_process_is_still_refused_by_the_guard(self):
        second, _ = self.repeated_family_with_a_live_process()
        run = self.raw("retry", second["id"], "--reason", "again", "--worker", "pi", "--model", MODEL)
        self.assertEqual(run.returncode, 1, run.stdout)
        self.assertIn("a process is alive on the same working tree", run.stderr)


if __name__ == "__main__":
    unittest.main()
