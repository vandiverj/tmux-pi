"""HB3a `tp-agent brief`: Main's working brief from records only. In process (bin/tp-agent's main(argv, env)); temporary stores and
state directories only; no tmux, no hook process (the hook scripts are covered in test_sessionstart_compact and test_work_checkpoint).
The record write rules themselves are in test_work_decisions.Records; here: what the brief selects and shows, how it starts, and how
it reads after a compaction."""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import contextlib
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import work  # noqa: E402
import work_brief  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_env import outside_session  # noqa: E402
from result_status import RESULT_STATUS_LEAD  # noqa: E402

AGENT = SourceFileLoader("tp_agent_work_brief", str(ROOT / "bin/tp-agent")).load_module()
OWNER = ["--by", "owner", "--quote"]

# The attack table (HB3a): writer x scope x lifecycle. Old forms carry no scope (they read as workspace), so they have one scope.
WRITERS = ("owner+quote", "owner no quote", "main", "worker", "old line", "old user+quote")
SCOPES = ("global", "project", "workspace", "lane", "task")
LIFECYCLES = ("new", "superseded", "withdrawn", "confirmed later", "conflicting pair")
OLD = ("old line", "old user+quote")


def expected_shown(writer, scope, life):
    """Whether the cell's own record is in Main's brief: active (owner words: at write or confirmed later), not superseded or
    withdrawn, and in a scope that covers Main (global, its project, its workspace)."""
    active = writer in ("owner+quote", "old user+quote") or life == "confirmed later"
    return active and life not in ("superseded", "withdrawn") and scope in ("global", "project", "workspace")


def assert_structure_whole(case, text, wholes):
    """Round 5 rule: every command, id and name that went in is in the output whole, and only a single overlong whole value makes a
    line longer than LINE_CHARS (prose is the only thing clipped)."""
    for value in wholes:
        case.assertIn(value, text, value[:60])
    for line in text.split("\n"):
        if len(line) > work_brief.LINE_CHARS:
            case.assertIn(line.strip(), wholes, f"an overlong line that is not one whole value: {line[:100]!r}")


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        self.project = self.tmp / "repo"
        self.project.mkdir()
        self.state = self.tmp / "repo" / ".tmux-pi" / "run-one"
        (self.state / "tasks").mkdir(parents=True)
        (self.tmp / "work-config.json").write_text("{}")
        self.env = {**outside_session(), "TP_WORK_DIR": str(self.tmp / "work"), "TP_WORK_CONFIG": str(self.tmp / "work-config.json"),
                    "TP_STATE_DIR": str(self.state), "TP_WORKDIR": str(self.project)}
        for key in ("TP_TASK_ID", "TP_TASK_DIR"):
            self.env.pop(key, None)

    def call(self, *argv, worker=False, env=None):
        out, err, code = io.StringIO(), io.StringIO(), 0
        full = {**self.env, **({"TP_TASK_ID": "coder-1"} if worker else {}), **(env or {})}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                AGENT.main(list(argv), full)
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *argv, worker=False, env=None):
        code, out, err = self.call(*argv, worker=worker, env=env)
        self.assertEqual(code, 0, f"{argv}: {err}")
        return json.loads(out) if out.strip().startswith(("{", "[")) else out.rstrip("\n")

    def brief(self, *extra, **kw):
        return self.ok("brief", *extra, **kw)

    def attached(self):
        path = self.state / "session-work.json"
        return json.loads(path.read_text())["attached"] if path.exists() else []

    def records(self):
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"]}):
            return work.all_records()


class AttackTable(Base):
    def test_every_writer_scope_and_lifecycle_cell(self):
        """One record (or pair) per cell; the brief's selection (work_brief.in_force) must hold exactly the cells whose record is
        active, standing and in Main's scope; every conflicting active pair is named, never resolved. Failures name the cell."""
        self.ok("work", "new", "Hub", "--id", "hub", "--repo", str(self.project))
        self.ok("work", "new", "Other", "--id", "other")  # a workspace this Main has not attached
        scopes = {"global": "global", "project": f"project:{self.project}", "workspace": "workspace", "lane": "lane:alpha",
                  "task": "task:coder-1"}
        cells = [(w, s, l) for w in WRITERS for s in SCOPES for l in LIFECYCLES if w not in OLD or s == "workspace"]
        # The old forms are hand-written into the workspace file first, as a store made before HB3a holds them.
        old_lines, old_entries, n = [], [], 0
        for w, s, l in cells:
            for k in range(2 if l == "conflicting pair" else 1):
                text = f"cell {w} | {s} | {l} | {k}"
                if w == "old line":
                    old_lines.append(f"- 2026-10-01 {text}")
                elif w == "old user+quote":
                    n += 1
                    q = f"Q {w} {s} {l}?"
                    old_entries.append(f"### hub/D{n} · normal · answered\nQuestion: {q}\nResolution: {text}\nBy: user\n"
                                       f"At: 2026-10-01T00:00:00Z\nQuote: owner said {n}\n")
        path = self.tmp / "work" / "workspaces" / "hub.md"
        body = path.read_text()
        self.assertNotIn("## Decisions", body)
        path.write_text(body.rstrip("\n") + "\n\n## Decisions\n" + "\n".join(old_lines) + "\n\n## Open decisions\n" + "\n".join(old_entries))
        ids = {r["text"]: r["id"] for r in self.records()}
        for w, s, l in cells:
            pair = []
            for k in range(2 if l == "conflicting pair" else 1):
                text = f"cell {w} | {s} | {l} | {k}"
                if w not in OLD:
                    extra = {"owner+quote": [*OWNER, f"owner said {text}"], "owner no quote": ["--by", "owner"], "main": ["--by", "main"],
                             "worker": []}[w]
                    question = ["--question", f"Q {w} {s} {l}?"] if l == "conflicting pair" else []
                    ids[text] = self.ok("work", "decide", "hub", text, "--scope", scopes[s], *question, *extra,
                                        worker=w == "worker")["record"]["id"]
                pair.append(ids[text])
            rid = pair[0]
            if l == "superseded":
                self.ok("work", "decide", "hub", f"replacement of {rid}", "--scope", scopes[s], "--supersedes", rid, *OWNER, "replace it")
            elif l == "withdrawn":
                self.ok("work", "decision", "withdraw", rid, "--reason", "moot", "--quote", "drop it")
            elif l == "confirmed later":
                self.ok("work", "decision", "confirm", rid, "--quote", "yes, that one")
        self.ok("work", "attach", "hub")
        records = self.records()
        by_text = {r["text"]: r for r in records}
        shown, conflicts = work_brief.in_force(records, self.project, ["hub"])
        shown_ids = {r["id"] for r in shown}
        counts = {"cells": len(cells), "shown": 0}
        for w, s, l in cells:
            for k in range(2 if l == "conflicting pair" else 1):
                record = by_text[f"cell {w} | {s} | {l} | {k}"]
                want = expected_shown(w, s, l)
                counts["shown"] += want
                self.assertEqual(record["id"] in shown_ids, want, f"cell writer={w} scope={s} lifecycle={l} #{k}: {record}")
            if l == "superseded":  # the owner's replacement stands wherever its scope covers Main
                replacement = by_text[f"replacement of {record['id']}"]
                self.assertEqual((record["superseded_by"], replacement["supersedes"]), (replacement["id"], record["id"]), (w, s, l))
                self.assertEqual(replacement["id"] in shown_ids, s in ("global", "project", "workspace"), f"replacement of cell {w}/{s}")
        named = {frozenset(r["id"] for r in group) for group in conflicts}
        for w, s, l in cells:
            if l != "conflicting pair":
                continue
            pair = frozenset(by_text[f"cell {w} | {s} | {l} | {k}"]["id"] for k in range(2))
            self.assertEqual(pair in named, expected_shown(w, s, l), f"conflict cell writer={w} scope={s}")
        self.assertFalse(any(r["workspace"] == "other" for r in shown))
        # The printed brief: bounded, both members of a conflict named, nothing superseded or withdrawn.
        text = self.brief()
        lines = text.split("\n")
        self.assertLessEqual(len(lines), work_brief.MAX_LINES, text)
        self.assertTrue(any(line.startswith("CONFLICT: ") for line in lines), text)
        self.assertIn("more: " + work_brief.MORE["rules"], text)  # more rules than the brief shows
        for life in ("superseded", "withdrawn"):
            self.assertNotIn(f"| {life} |", text)
        self.assertEqual((counts["cells"], counts["shown"]), (110, 26))  # 4 writers x 5 scopes x 5 lifecycles + 2 old forms x 5 lifecycles
        # Archiving a workspace: its global and project records stand (status untouched, still in Main's brief); the others follow it.
        self.ok("work", "new", "Gone", "--id", "gone")
        archived = {name: self.ok("work", "decide", "gone", f"archived {name}", "--scope", scope, *OWNER, "kept")["record"]["id"]
                    for name, scope in (("global", "global"), ("project", scopes["project"]), ("workspace", "workspace"), ("lane", "lane:alpha"),
                                        ("task", "task:coder-1"))}
        before = {r["id"]: r["status"] for r in self.records() if r["workspace"] == "gone"}
        self.ok("work", "archive", "gone", "--kind", "workspace")
        after = {r["id"]: r["status"] for r in self.records() if r["workspace"] == "gone"}
        self.assertEqual(after, {archived[n]: before[archived[n]] for n in ("global", "project")}, "archive changed a record or lost a standing one")
        shown_after = {r["id"] for r in work_brief.in_force(self.records(), self.project, ["hub"])[0]}
        self.assertEqual({n for n, i in archived.items() if i in shown_after}, {"global", "project"})


class Start(Base):
    """A Claude Main SessionStart with nothing attached: `brief --attach` attaches the newest active workspace for the project, or
    says none is attached and how to make one; nothing is ever created."""

    def test_start_rows(self):
        def first(text):
            return text.split("\n", 1)[0]
        store = self.tmp / "work" / "workspaces"

        def reset():
            (self.state / "session-work.json").unlink(missing_ok=True)

        # row: no store at all (nothing created by reading)
        text = self.brief("--attach")
        self.assertTrue(first(text).startswith("no workspace attached: `tp-agent work new "), "no store")
        self.assertFalse((self.tmp / "work").exists(), "no store: brief created the store")
        # row: empty store
        store.mkdir(parents=True)
        self.assertTrue(first(self.brief("--attach")).startswith("no workspace attached"), "empty store")
        # row: only non-matching workspaces
        self.ok("work", "new", "Elsewhere", "--id", "elsewhere", "--repo", str(self.tmp / "other-repo"))
        self.assertTrue(first(self.brief("--attach")).startswith("no workspace attached"), "only non-matching")
        self.assertEqual(self.attached(), [], "only non-matching")
        # row: one match
        self.ok("work", "new", "Old", "--id", "old", "--repo", str(self.project))
        text = self.brief("--attach")
        self.assertEqual(self.attached(), ["old"], "one match")
        self.assertIn('workspace old "Old" (attached automatically: the newest active workspace for this project', text, "one match")
        # row: a worker gets no attach (and the hook gives it nothing: test_sessionstart_compact)
        reset()
        self.brief("--attach", worker=True)
        self.assertEqual(self.attached(), [], "worker")
        # row: two matches: the newest wins, the other is listed
        self.ok("work", "new", "New", "--id", "new", "--repo", str(self.project))
        text = self.brief("--attach")
        self.assertEqual(self.attached(), ["new"], "two matches")
        self.assertIn("also matching: old", text, "two matches")
        # row: already attached: nothing changes
        self.brief("--attach")
        self.assertEqual(self.attached(), ["new"], "already attached")
        # row: a corrupt workspace file is skipped and counted; the rest still works
        reset()
        (store / "broken.md").write_text('---\nid: broken\ntitle: t\nstatus: active\nrepos: ["/broken\\u0000repo"]\n---\nx\n', encoding="utf-8")
        text = self.brief("--attach")
        self.assertEqual(self.attached(), ["new"], "corrupt file")
        self.assertIn("1 unreadable work files; tp-agent work list", text, "corrupt file")
        # row: an unwritable state directory: the attach fails, the brief says so and still prints
        reset()
        os.chmod(self.state, 0o500)
        try:
            code, out, err = self.call("brief", "--attach")
        finally:
            os.chmod(self.state, 0o700)
        self.assertEqual(code, 0, err)
        self.assertIn("auto-attach of new failed", out, "unwritable state")
        self.assertTrue(first(out).startswith("no workspace attached"), "unwritable state")
        # row: a read-only workspaces folder: reading (and the brief) still works; nothing is written, so nothing is created
        os.chmod(store, 0o500)
        try:
            text = self.brief("--attach")
        finally:
            os.chmod(store, 0o700)
        self.assertEqual(self.attached(), ["new"], "read-only workspaces folder")
        self.assertIn('workspace new "New" (attached', text, "read-only workspaces folder")
        self.assertEqual(sorted(p.stem for p in store.glob("*.md")), ["broken", "elsewhere", "new", "old"], "a workspace was created")
        # rows: an unusable store says so on the first line (exit 0), reads as no workspace and never suggests creating one
        file_store = self.tmp / "a-file"
        file_store.write_text("a file where the store should be")
        locked = self.tmp / "locked"
        (locked / "workspaces").mkdir(parents=True)
        for name, root, why in (("store is a file", file_store, "it is not a directory"),
                                ("store folder cannot be listed", locked, "Permission denied")):
            if name.startswith("store folder") and os.geteuid() == 0:
                continue  # root lists anything
            if name.startswith("store folder"):
                os.chmod(locked, 0o000)
            try:
                for argv in (("--attach",), ("--attach", "--hook-json", "--source", "startup")):
                    code, out, err = self.call("brief", *argv, env={"TP_WORK_DIR": str(root)})
                    text = json.loads(out)["hookSpecificOutput"]["additionalContext"] if "--hook-json" in argv else out
                    self.assertEqual(code, 0, f"{name}: {err}")
                    # the command's output goes through work_find.guard, which spells the store root as `store:` (see PATHS_LEGEND)
                    self.assertTrue(text.startswith("working brief unavailable: work store store: unusable: "), f"{name}: {text!r}")
                    self.assertIn(why, text.split("\n")[0], name)
                    with mock.patch.dict(os.environ, {**self.env, "TP_WORK_DIR": str(root)}, clear=True):  # unguarded: the real path
                        raw = work_brief.build(None, None)[0]
                    self.assertTrue(raw.startswith(f"working brief unavailable: work store {root.resolve()} unusable: "), f"{name}: {raw!r}")
                    for absent in ("work new", "none recorded", "no workspace attached"):
                        self.assertNotIn(absent, text, f"{name}: {absent}")
            finally:
                os.chmod(locked, 0o700)

    def test_no_workspace_command_is_never_clipped_whatever_the_project_path(self):
        long = self.tmp / ("p" * 120) / ("q" * 113)  # 233+ characters with the temp prefix: a valid path, not an attack
        long.mkdir(parents=True)
        text = self.brief("--workdir", str(long))
        line = f"no workspace attached: `tp-agent work new TITLE --repo {long.resolve()} --objective TEXT && tp-agent work attach ID`"
        assert_structure_whole(self, text, [line])
        self.assertTrue(text.startswith(line), text[:300])


class Compaction(Base):
    def test_precompact_then_sessionstart_compact_with_and_without_a_decision_between(self):
        """The hook steps in process: PreCompact = `work compacted` + `brief --attach` + `work checkpoint --auto`; SessionStart(compact)
        = `work compacted` + `brief --attach --hook-json --source compact`."""
        self.ok("work", "new", "Hub", "--id", "hub", "--repo", str(self.project), "--objective", "ship the loader")
        self.ok("work", "decide", "hub", "use safe_grep", *OWNER, "use safe_grep")
        stamp = "2099-01-01T00:00:00Z"
        for row, write_between in (("no decision between", False), ("a decision between", True)):
            with self.subTest(row=row):
                self.ok("work", "compacted", "--reason", "precompact:auto")
                self.ok("brief", "--attach")
                self.assertIn("checkpoint written: ", self.ok("work", "checkpoint", "--auto", "--trigger", "precompact"), row)
                # The compaction time is put after every record, so "older than the compaction" does not depend on the clock.
                with mock.patch.object(work_brief, "last_compaction", lambda _state: work._parse_when(stamp)):
                    if write_between:
                        with mock.patch.object(work, "now", lambda: stamp.replace("00:00:00", "00:00:01")):
                            self.ok("work", "decide", "hub", "commit nothing", *OWNER, "commit nothing")
                    self.ok("work", "compacted", "--reason", "session-start:compact")
                    out = self.brief("--attach", "--hook-json", "--source", "compact")
                text = out["hookSpecificOutput"]["additionalContext"]
                brief, _, index = text.partition("\n\ntmux-pi task index")
                self.assertTrue(index, f"{row}: no index text after the brief")
                rules = [l for l in brief.split("\n") if l.startswith("rules in force")][0]
                self.assertEqual("STALE" in rules, not write_between, f"{row}: {rules}")
                self.assertIn("STALE", [l for l in brief.split("\n") if l.startswith("last checkpoint")][0], row)
                self.assertIn("last checkpoint [", brief, row)
                self.assertEqual(self.attached(), ["hub"], row)
                if write_between:
                    self.assertIn("commit nothing", brief, row)


    def test_each_part_carries_its_own_time_against_the_real_compaction_lookup(self):
        """No patched lookup: events are written with `work compacted` at set times. A part is STALE when its own write time is before the
        newest compaction event; a write elsewhere in the file never refreshes it; a part with no stored time says `time unknown`."""
        def at(day):
            return mock.patch.object(work, "now", return_value=f"2026-01-{day:02d}T00:00:00Z")
        with at(1):
            self.ok("work", "new", "Hub", "--id", "hub", "--repo", str(self.project), "--objective", "old outcome")
            self.ok("work", "set", "hub", "--acceptance", "old acceptance")
            self.ok("work", "decision", "add", "hub", "--question", "old question?")
            self.ok("work", "decide", "hub", "old rule", *OWNER, "keep it")
            self.ok("work", "attach", "hub")
        with at(2):
            self.ok("work", "compacted", "--reason", "precompact:auto")
        with at(3):
            self.ok("work", "decision", "add", "hub", "--question", "new question?", "--blocking")
            self.ok("work", "decide", "hub", "unrelated rule", *OWNER, "yes")
        # a decision from before this change carries no time: hand-written entry
        path = self.tmp / "work" / "workspaces" / "hub.md"
        path.write_text(path.read_text().rstrip("\n") + "\n\n### hub/D9 · normal · open\nQuestion: ancient?\n")

        def parts():
            return {l.split(" [")[0]: l for l in self.brief().splitlines() if " [" in l and not l.startswith("- ")}

        def stale(line):
            return "STALE" in line
        first = parts()
        self.assertTrue(all(stale(first[k]) for k in ("outcome", "acceptance")), first)  # the day-2 compaction is already after them
        self.assertNotIn("STALE", first["rules in force (active, owner-confirmed; global, project, workspace)"], first)  # last written day 3
        with at(4):
            self.ok("work", "compacted", "--reason", "session-start:compact")
        lines = self.brief().splitlines()
        by = {l.split(" [")[0]: l for l in lines if " [" in l and not l.startswith("- ")}
        self.assertIn("[2026-01-01T00:00:00Z] STALE", by["outcome"])
        self.assertIn("[2026-01-01T00:00:00Z] STALE", by["acceptance"])  # the later writes did not refresh them
        self.assertIn("old rule", [l for l in lines if "old rule" in l][0])
        self.assertTrue(all("STALE" in l for l in lines if l.startswith("- [") and "(hub/R" in l), lines)  # every rule predates the compaction
        decisions = {l.split(" ")[1]: l for l in lines if l.startswith("- hub/D")}
        self.assertEqual(sorted(decisions), ["hub/D1", "hub/D2", "hub/D9"])
        self.assertIn("2026-01-01T00:00:00Z STALE", decisions["hub/D1"])
        self.assertIn("2026-01-03T00:00:00Z STALE", decisions["hub/D2"])
        self.assertIn("time unknown STALE", decisions["hub/D9"])
        # a compaction between the writes: only the later part is fresh
        with at(5):
            self.ok("work", "set", "hub", "--objective", "new outcome")
        by = {l.split(" [")[0]: l for l in self.brief().splitlines() if " [" in l and not l.startswith("- ")}
        self.assertIn("[2026-01-05T00:00:00Z]:", by["outcome"])
        self.assertIn("new outcome", by["outcome"])
        self.assertIn("STALE", by["acceptance"])
        # a workspace written before this change: the part exists, its time is unknown, and it is never called fresh
        path.write_text(path.read_text().replace("objective_at: 2026-01-05T00:00:00Z\n", ""))
        by = {l.split(" [")[0]: l for l in self.brief().splitlines() if " [" in l and not l.startswith("- ")}
        self.assertIn("[time unknown] STALE", by["outcome"])


class Realistic(Base):
    # audit C F6: the owner's process corrections, with their exact words, as records (kind, scope) and the brief built from them.
    F6 = [("preference", "global", "LM Studio is always up: verify it with a small test prompt before calling it unavailable",
           "why do yo think lm studio is not available? you should be able to send it a small test prompt to verify"),
          ("correction", "global", "messages to workers must be delivered; check delivery", "messages to workers not getting sent is one of them"),
          ("design", "global", "Main is Claude Code; side tasks are subagents", "my main is claude code, side tasks are subagents"),
          ("preference", "global", "workers attack their own work and check it before reporting",
           "...almost as though it found one or two things and quite rather than checking its own work..."),
          ("design", "project", "the summarizer is not for outside systems", "explain to me what you are trying to do with the summarizer "
                                                                               "because I am not planning to have it used by outside systems"),
          ("preference", "global", "workers use safe_grep/safe_read from the start and Main tells them to",
           "workers do not use safe_grep/read from the beginning and that main does not tell them to"),
          ("preference", "project", "eval code-quality tools against battle soccer as used by tmux-pi",
           "shouldn't you be evaling it against battle soccer being used by tmux-pi"),
          ("design", "global", "code-quality tools check target projects, not tmux-pi itself",
           "these tools are not for tmux-pi to check itself but for tmux-pi to check the source code that is being worked on agentically")]

    def test_an_uncommitted_supersession_keeps_the_old_rule_and_lists_the_new_one_pending(self):
        """Round 3: the old workspace's save fails after the replacement was saved. The brief keeps the old rule and lists the new one
        as pending with the completion command; `confirm` commits it; a withdrawn pending replacement leaves the old rule in force."""
        import errno
        OLD, NEW = "owner-process-rules-original-store", "owner-process-rules-replacement-store"
        for ws in (OLD, NEW):
            self.ok("work", "new", ws.title(), "--id", ws)
        self.ok("work", "attach", OLD)
        old = self.ok("work", "decide", OLD, "old rule text", "--scope", "global", *OWNER, "yes")["record"]["id"]
        real = work.Doc.save

        def save(doc):
            if doc.id == OLD:
                raise OSError(errno.ENOSPC, "injected disk full")
            return real(doc)

        with mock.patch.object(work.Doc, "save", save):
            code, _, _ = self.call("work", "decide", NEW, "new rule text", "--scope", "global", "--supersedes", old, *OWNER, "yes")
        self.assertEqual(code, 1)
        text = self.brief()
        self.assertIn("old rule text", text)
        self.assertNotIn("- [design global] new rule text", text)
        self.assertIn(f"pending: {NEW}/R1 replaces {old}, not yet committed", text)
        self.assertIn(f"\n  tp-agent work decision confirm {NEW}/R1 --quote \"their exact words\"\n", text + "\n")  # whole, never clipped
        self.assertTrue(self.ok("work", "decision", "confirm", f"{NEW}/R1", "--quote", "yes")["changed"])
        text = self.brief()
        self.assertNotIn("old rule text", text)
        self.assertIn("new rule text", text)
        self.assertNotIn("pending:", text)

    def test_the_audit_f6_owner_rules_as_records_make_the_brief(self):
        self.ok("work", "new", "Hub memory", "--id", "hub", "--repo", str(self.project), "--objective",
                "Main keeps the owner's rules across compactions without being reminded")
        self.ok("work", "set", "hub", "--acceptance", "every F6 rule is in the brief at start and after a compaction")
        for kind, scope, text, quote in self.F6:
            self.ok("work", "decide", "hub", text, "--kind", kind, "--scope", f"project:{self.project}" if scope == "project" else scope,
                    *OWNER, quote)
        self.ok("work", "decide", "hub", "a main guess", "--by", "main")  # provisional: listed by id only, its text is not injected
        text = self.brief("--attach")
        lines = text.split("\n")
        self.assertLessEqual(len(lines), work_brief.MAX_LINES)
        for kind, scope, rule, quote in self.F6:
            self.assertIn(f"{rule} — owner: ", text)
        rule_lines = [line for line in lines if line.startswith("- [")]
        self.assertEqual(len(rule_lines), len(self.F6))
        for line in rule_lines:  # a long rule or quote is clipped, never the record id and time that point back to it
            self.assertRegex(line, r" \(hub/R\d+, \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\)$")
            self.assertLessEqual(len(line), work_brief.LINE_CHARS)
        self.assertIn("outcome [", text)
        self.assertIn("acceptance [", text)
        self.assertIn("obligations (HB5): no obligation records exist yet", text)
        self.assertIn("launch record (HB2; tasks: [launch: ...] in active work; Main) [no record]: none (Main was started before "
                      "launch records, or not by tp-main)", text)
        self.assertIn("provisional (not in force until the owner confirms; tp-agent work decision confirm ID --quote ...):\n  hub/R9", text)
        self.assertNotIn("a main guess", text)
        self.assertNotIn("no workspace attached", text)

    def task(self, ident, role, state, result="", review_of=None, packet="", at=None):
        task = self.state / "tasks" / ident
        task.mkdir(exist_ok=True)
        (task / "meta.json").write_text(json.dumps({"id": ident, "role": role, "worker": "claude", "created": "2026-01-01T00:00:00Z"}))
        (task / "state.json").write_text(json.dumps({"state": state, "updated": "2026-01-01T00:00:00Z"}))
        if result:
            (task / "result.md").write_text(result)
        if review_of:
            (task / "review-of").write_text(review_of + "\n")
        if packet:
            (task / "packet.md").write_text(packet)
        if at:
            for name in ("result.md",):
                os.utime(task / name, (at, at))
        return task

    def test_paused_tasks_are_shown_with_their_result_status_like_running_ones(self):
        self.task("coder-running", "coder", "working")
        self.task("coder-paused", "coder", "paused", "Need more context\nRESULT_STATUS: blocked\n")
        self.task("coder-mute", "coder", "paused", "I stopped\n")  # paused with no result status
        text = self.brief()
        lines = [l for l in text.splitlines() if l.startswith("- coder-")]
        self.assertEqual(len(lines), 3, text)
        self.assertTrue(any(l.startswith("- coder-paused coder paused result_status: blocked") for l in lines), lines)
        self.assertTrue(any(l.startswith("- coder-running coder working") for l in lines), lines)
        self.assertNotIn("no active, paused or unreviewed tasks", text)

    def test_the_brief_shows_each_tasks_launch_record_and_mains(self):
        # HB2b: per listed task, contract version, role, route and unsupported capabilities from its launch.json; a task with none
        # says so; a record of another schema major is refused, not read as v1; Main's own record closes the brief.
        def record(role, route, **more):
            return {"schema": "tp-launch:v1", "status": "prepared", "task": "t", "role": role, "route": route, "harness": "claude",
                    "instructions": [{"name": "contract", "path": "agent/contract/worker.md", "sha256": "0" * 64, "version": "v1"}],
                    "capabilities": {"supported": ["interrupt.escape"], "unsupported": ["interrupt.rpc", "cost.known"]},
                    "recorded": "2026-01-01T00:00:00Z", **more}
        (self.task("coder-new", "coder", "working") / "launch.json").write_text(json.dumps(record("coder", "retry")))
        self.task("coder-old", "coder", "working")
        (self.task("coder-next", "coder", "working") / "launch.json").write_text(json.dumps({**record("coder", "spawn"),
                                                                                              "schema": "tp-launch:v2"}))
        failed = record("code-reviewer", "review", status="failed", failure="the claude harness binary is missing: not on PATH")
        (self.task("review-x", "code-reviewer", "working") / "launch.json").write_text(json.dumps(failed))
        (self.state / "main-launch.json").write_text(json.dumps(record("main", "main", start="resume", model="opus", instructions=[])))
        lines = self.brief().splitlines()
        row = {l.split()[1]: l for l in lines if l.startswith(("- coder-", "- review-"))}
        self.assertEqual(row["coder-new"], "- coder-new coder working [launch: contract v1, role coder, route retry, unsupported "
                                           "interrupt.rpc+cost.known]")
        self.assertEqual(row["coder-old"], "- coder-old coder working [launch: no record]")
        self.assertIn("[launch: unusable: launch.json is tp-launch:v2; this reader knows tp-launch:v1 only]", row["coder-next"])
        self.assertIn("[launch: FAILED (the claude harness binary is missing", row["review-x"])
        main = [l for l in lines if l.startswith("launch record (HB2")]
        self.assertEqual(main, ["launch record (HB2; tasks: [launch: ...] in active work; Main) [2026-01-01T00:00:00Z]: claude resume, "
                                "model opus, contract none, role main, route main, unsupported interrupt.rpc+cost.known"])
        # HB2c: a field the record does not hold is "unknown"; one it holds and is empty is "none"
        error = {"schema": "tp-launch:v1", "status": "error", "error": "RecordError: boom", "task": "t", "role": "coder",
                 "harness": "claude", "recorded": "2026-01-01T00:00:00Z"}  # launch_record._main's shape
        empty = record("", None, instructions=[], capabilities={"unsupported": []})
        odd = record("coder", "spawn", instructions="x", capabilities={"unsupported": "x"})  # containers of the wrong type
        bare = record("coder", "spawn", instructions=[{"name": "contract"}], capabilities={})  # a contract entry with no version
        for ident, rec in (("coder-err", error), ("coder-empty", empty), ("coder-odd", odd), ("coder-bare", bare)):
            (self.task(ident, "coder", "working") / "launch.json").write_text(json.dumps(rec))
        row = {l.split()[1]: l for l in self.brief().splitlines() if l.startswith("- coder-")}
        self.assertEqual(row["coder-err"], "- coder-err coder working [launch: ERROR (RecordError: boom), contract unknown, role coder, "
                                           "route unknown, unsupported unknown]")
        self.assertIn("[launch: contract none, role none, route none, unsupported none]", row["coder-empty"])
        self.assertIn("[launch: contract unknown, role coder, route spawn, unsupported unknown]", row["coder-odd"])
        self.assertIn("[launch: contract unknown, role coder, route spawn, unsupported unknown]", row["coder-bare"])
        for l in lines:
            self.assertLessEqual(len(l), work_brief.LINE_CHARS)

    REVIEW ="{verdict}\nRESULT_STATUS: complete\n\nCoverage\n- lib/work_brief.py: reviewed the whole file and its callers\n"

    def test_last_evidence_is_the_newest_approved_review_and_the_evidence_it_was_given(self):
        def packet(*names):
            return "".join(f"x\n# Evidence supplied by Main: {n}\n\nbody\n" for n in names) + "quoted: Evidence supplied by Main: no-heading.md\n"
        self.assertIn("last approved review: none", self.brief())
        author = self.task("coder-a", "coder", "settled", "done\nRESULT_STATUS: complete\n")
        (author / "main-evidence").mkdir()
        (author / "main-evidence" / "evidence.md").write_text("e")
        (author / "main-evidence" / "later.md").write_text("newer by mtime but never given to a review")
        # row: (review id, verdict text, packet names, mtime): only a complete approval counts, the newest wins
        rows = [("review-1", self.REVIEW.format(verdict="REQUEST_CHANGES"), ["evidence.md"], 1_800_000_000),
                ("review-2", "APPROVED\nRESULT_STATUS: complete\n", ["evidence.md"], 1_800_000_100),  # no Coverage: incomplete, approves nothing
                ("review-3", self.REVIEW.format(verdict="APPROVED"), ["evidence.md"], 1_800_000_200),
                ("review-4", self.REVIEW.format(verdict="REQUEST_CHANGES"), [], 1_800_000_300)]
        for ident, result, names, when in rows:
            self.task(ident, "reviewer", "settled", result, review_of="coder-a", packet=packet(*names), at=when)
        line = [l for l in self.brief().splitlines() if l.startswith("last approved review")][0]
        self.assertIn("review-3 of coder-a", line)
        self.assertIn("approved 2027-01-15T08:03:20Z", line)
        self.assertIn("tasks/coder-a/main-evidence/evidence.md", line)
        self.assertNotIn("later.md", line)
        self.assertNotIn("no-heading", line)
        # a newer approval, given no evidence file, says so
        self.task("review-5", "reviewer", "settled", self.REVIEW.format(verdict="APPROVED_WITH_NOTES") + "\nNotes\n- one note here\n",
                  review_of="coder-a", packet=packet(), at=1_800_000_400)
        line = [l for l in self.brief().splitlines() if l.startswith("last approved review")][0]
        self.assertIn("review-5 of coder-a", line)
        self.assertIn("evidence it was given: none attached", line)

    # Round 6: one campaign fills every section; its structural values at three lengths. `boundary` puts list lines at 239/240/241
    # characters with label and separators: lane 00 with its label is 241 (so it goes below), lanes 01-03 are 239, lanes 04-06 are
    # 241 (so 06 starts the next line), 06-08 are 240; in `short` the first lanes line is exactly 240.
    LENGTHS = {"short": dict(ws=("hub", "hub-other"), project="ordinary-project-with-a-long-descriptive-directory-name",
                             lanes=[35] * 20, task=lambda n: f"coder-{n}"),
               "boundary": dict(ws=("b" * 44 + "-hub", "b" * 42 + "-other"), project="p" * 200,
                                lanes=[224, 67, 68, 68, 68, 68, 69, 67, 68] + [35] * 11,
                                task=lambda n: f"coder-{n}-" + "t" * (215 + n % 3)),  # task rows of 239, 240, 241 characters
               "overlong": dict(ws=("o" * 44 + "-hub", "o" * 42 + "-other"), project="p" * 250, lanes=[250] * 20,
                                task=lambda n: f"coder-{n}-" + "t" * 250)}

    def campaign(self, length):
        """Build the brief for the full campaign at `length` in a store and run of its own. (text, inputs by section, wholes)."""
        spec = self.LENGTHS[length]
        root = self.tmp / length
        project = root / spec["project"]
        project.mkdir(parents=True)
        self.state = root / "state"
        (self.state / "tasks").mkdir(parents=True)
        self.env = {**self.env, "TP_WORK_DIR": str(root / "work"), "TP_STATE_DIR": str(self.state), "TP_WORKDIR": str(project)}
        ws, other = spec["ws"]
        for ident in (other, ws):
            self.ok("work", "new", "T" * 200, "--id", ident, "--repo", str(project), "--objective", "o" * 400)
        rules = []
        for i in range(20):  # 4 conflict pairs (0-7), global rules, then project-scoped ones (written a day later, so shown first)
            scope = f"project:{project}" if i >= 14 else "global"
            question = ["--question", f"question {i // 2}?"] if i < 8 else []
            with mock.patch.object(work, "now", return_value="2026-01-02T00:00:00Z" if i >= 14 else "2026-01-01T00:00:00Z"):
                rules.append(self.ok("work", "decide", ws, f"rule {i} " + "x" * 300, "--kind", "preference", "--scope", scope, *question,
                                     *OWNER, "q" * 200)["record"]["id"])
        real = work.Doc.save
        with mock.patch.object(work.Doc, "save", lambda doc: (_ for _ in ()).throw(OSError(28, "disk full")) if doc.id == ws else real(doc)):
            self.call("work", "decide", other, "replacement", "--scope", "global", "--supersedes", rules[8], *OWNER, "yes")
        pending = f"{other}/R1"
        provisional = [self.ok("work", "decide", ws, f"guess {i}", "--by", "main")["record"]["id"] for i in range(3)]
        decisions = [self.ok("work", "decision", "add", ws, "--question", f"open {i}?", *(["--blocking"] if i % 2 else []))["id"]
                     for i in range(8)]
        self.ok("work", "attach", ws)
        self.assertIn("checkpoint written: ", self.ok("work", "checkpoint", "--auto", "--trigger", "precompact"))
        author = self.task("coder-a", "coder", "settled", "done\nRESULT_STATUS: complete\n")
        (author / "main-evidence").mkdir()
        (author / "main-evidence" / "evidence.md").write_text("e")
        self.task("review-1", "reviewer", "settled", self.REVIEW.format(verdict="APPROVED"), review_of="coder-a",
                  packet="# Evidence supplied by Main: evidence.md\n", at=1_800_000_000)
        lanes = {f"lane-{i:02d}-" + "n" * (size - 8): {"by_status": {"working": 2}} for i, size in enumerate(spec["lanes"])}
        rows = [{"id": spec["task"](n), "role": "coder", "status": "working"} for n in range(10)]
        index = {"active": rows, "paused": [], "unstatused": [], "pending": [], "lanes": lanes}
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"]}):
            text, _ = work_brief.build(self.state, str(project), False, index)
        inputs = {work_brief.MORE["rules"]: rules + [pending] + provisional,
                  work_brief.MORE["work"]: [r["id"] for r in rows] + list(lanes),
                  work_brief.MORE["decisions"]: decisions}
        wholes = [f"project:{project.name}", ws, f"tp-agent work show {ws}", *(f"{name} working 2" for name in lanes),
                  *(f"- {r['id']} coder working" for r in rows), *rules]
        return text, inputs, wholes, rules[14:]

    def test_a_full_campaign_keeps_every_section_and_every_structural_value_at_three_lengths(self):
        """Every section filled at once. At most MAX_LINES; the checkpoint, approved review and pointers are always there; a line is
        at most LINE_CHARS unless it is exactly one whole value; every id, lane name, project name and command that went in is in the
        output whole, or its section ends with `+N more: COMMAND` whose N counts exactly the ones it left out."""
        import re
        base = dict(self.env)
        for length in self.LENGTHS:
            self.env = dict(base)
            text, inputs, wholes, project_rules = self.campaign(length)
            lines = text.split("\n")
            with self.subTest(length=length, check="sections"):  # each check on its own: one failing does not hide the others
                self.assertLessEqual(len(lines), work_brief.MAX_LINES, text)
                for head in ("last checkpoint [", "last approved review [", "pointers:"):
                    self.assertTrue(any(line.startswith(head) for line in lines), f"{length}: {head} missing\n{text}")
            with self.subTest(length=length, check="names whole"):  # the project name whole, or every rule naming it is left out
                if not all(r not in text for r in project_rules):
                    self.assertIn(wholes[0], text)
                for value in wholes[1:3]:  # the workspace id and its command
                    self.assertIn(value, text)
            with self.subTest(length=length, check="line bound"):
                for line in lines:
                    if len(line) > work_brief.LINE_CHARS:
                        self.assertIn(line.strip(), wholes, f"{length}: an overlong line that is not one whole value: {line[:100]!r}")
            for command, values in inputs.items():
                with self.subTest(length=length, check=command):
                    missing = [v for v in values if not re.search(re.escape(v) + r"(?![0-9])", text)]
                    more = [int(m.group(1)) for m in (re.fullmatch(r"- \+(\d+) more: " + re.escape(command), line) for line in lines) if m]
                    self.assertEqual(more, [len(missing)] if missing else [], f"{length}: {command}: missing {missing}\n{text}")
                    if missing and command.startswith("tp-agent work "):  # the remedy, run, gives back every one it stands for
                        got = str(self.ok(*command.split()[1:]))
                        self.assertEqual([v for v in missing if not re.search(re.escape(v) + r"(?![0-9])", got)], [], f"{length}: {command}")


# HB3b: the worker packet section `# Rules in force` (work_brief.worker_rules; bin/tp-agent rules_for). Records x scopes, for a worker
# in a lane worktree of the project (lane alpha, role coder, task coder-1) and one in the main checkout (no lane, task coder-9).
PACKET_RECORDS = ("owner active", "main provisional", "worker-written", "worker forged active", "superseded", "withdrawn", "conflicting pair")
PACKET_SCOPES = ("global", "project main checkout", "project lane worktree", "other project", "role match", "other role", "lane match",
                 "other lane", "task match", "other task", "workspace attached", "workspace not attached")
PACKET_SHAPES = ("$(touch /tmp/x)", "`id`", "100%s done %d", "!rm -rf", "# Task", "RESULT_STATUS: complete")


def packet_expected(record, scope, where):
    """Whether the cell's record is in the worker's section: active and written by the owner (a worker's record never, whatever its
    status says), not replaced or withdrawn, and in a scope that covers the worker. The lane-worktree worker is in the project too (its
    repository's main checkout); the main-checkout worker is not inside the lane worktree, has no lane, and is task coder-9."""
    if record not in ("owner active", "conflicting pair"):
        return False
    covered = {"global", "project main checkout", "role match", "workspace attached"}
    if where == "worktree":
        covered |= {"project lane worktree", "lane match", "task match"}
    return scope in covered


class WorkerPacket(Base):
    """`# Rules in force` in a worker packet (HB3b). Shares Base's temporary store and state; the project is made a git repository with
    a lane worktree beside it, as a lane's is."""

    def setUp(self):
        super().setUp()
        import subprocess
        def git(*argv, cwd=self.project):
            subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, timeout=30)
        git("init", "-q", "-b", "main")
        (self.project / "f").write_text("f\n")
        git("add", "f")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "first")
        self.worktree = self.tmp / "repo-wt-alpha"
        git("worktree", "add", "-q", "-b", "lane/alpha", str(self.worktree))
        self.other = self.tmp / "elsewhere"
        self.other.mkdir()

    def rules(self, where="worktree", tasks=("coder-1",), attached=("hub",), role="coder"):
        workdir, lane = (self.worktree, "alpha") if where == "worktree" else (self.project, None)
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"], "TP_WORK_CONFIG": self.env["TP_WORK_CONFIG"]}):
            return work_brief.worker_rules(str(workdir), role, lane, tasks, list(attached))

    def assert_bounded(self, text, label):
        lines = text.rstrip("\n").split("\n")
        self.assertEqual(lines[0], work_brief.PACKET_HEADING, label)
        self.assertLessEqual(len(lines), work_brief.PACKET_LINES, f"{label}\n{text}")
        self.assertLessEqual(len(text), work_brief.PACKET_CHARS, label)
        for line in lines[1:]:  # data never opens a section or passes for a reply's status line
            self.assertFalse(line.startswith("#"), f"{label}: {line[:80]!r}")
            self.assertNotRegex(line, RESULT_STATUS_LEAD, label)
        return lines

    def hand_records(self, ws, rows):
        """Append Decisions lines with trailers to workspace `ws` (made here): rows of (text, scope, status, by, at[, question])."""
        self.ok("work", "new", ws.title(), "--id", ws)
        path = Path(self.env["TP_WORK_DIR"]) / "workspaces" / f"{ws}.md"
        lines = []
        for n, (text, scope, status, by, at, *question) in enumerate(rows, 1):
            fields = {"id": f"{ws}/R{n}", "kind": "preference", "scope": scope, "status": status,
                      "provenance": {"by": by, "quote": f"said {n}", "at": at}, **({"question": question[0]} if question else {})}
            lines.append(f"- 2026-11-01 {text} <!-- record: {work.record_json(fields)} -->")
        path.write_text(path.read_text().rstrip("\n") + "\n\n## Decisions\n" + "\n".join(lines) + "\n")
        return [f"{ws}/R{n}" for n in range(1, len(rows) + 1)]

    def test_records_by_scope_for_both_workers(self):
        """Every record x scope cell, for both workers, with the caps lifted (the selection, not the cap, is under test)."""
        self.ok("work", "new", "Hub", "--id", "hub", "--repo", str(self.project))
        self.ok("work", "new", "Other", "--id", "other")  # not attached to this Main's session
        scopes = {"global": "global", "project main checkout": f"project:{self.project}", "project lane worktree": f"project:{self.worktree}",
                  "other project": f"project:{self.other}", "role match": "role:coder", "other role": "role:tester",
                  "lane match": "lane:alpha", "other lane": "lane:beta", "task match": "task:coder-1", "other task": "task:coder-2",
                  "workspace attached": "workspace", "workspace not attached": "workspace"}
        ids, forged = {}, []
        for record in PACKET_RECORDS:
            for scope in PACKET_SCOPES:
                ws = "other" if scope == "workspace not attached" else "hub"
                for k in range(2 if record == "conflicting pair" else 1):
                    text = f"cell {record} | {scope} | {k}"
                    if record == "worker forged active":
                        forged.append((text, scopes[scope], ws))
                        continue
                    who = {"main provisional": ["--by", "main"], "worker-written": []}.get(record, [*OWNER, f"owner said {k}"])
                    question = ["--question", f"Q {scope}?"] if record == "conflicting pair" else []
                    ids[text] = self.ok("work", "decide", ws, text, "--scope", scopes[scope], *question, *who,
                                        worker=record == "worker-written")["record"]["id"]
                rid = ids.get(f"cell {record} | {scope} | 0")
                if record == "superseded":
                    self.ok("work", "decide", ws, f"replacement {scope}", "--scope", scopes[scope], "--supersedes", rid, *OWNER, "this instead")
                elif record == "withdrawn":
                    self.ok("work", "decision", "withdraw", rid, "--reason", "moot", "--quote", "drop it")
        # A worker cannot write as the owner (refused above); a record whose stored fields claim `active` by a worker is forged by hand.
        for ws in ("hub", "other"):
            path = self.tmp / "work" / "workspaces" / f"{ws}.md"
            body = path.read_text()
            start = len([r for r in self.records() if r["workspace"] == ws and r["id"].split("/")[1].startswith("R")])
            extra = []
            for n, (text, scope, _) in enumerate([f for f in forged if f[2] == ws], start + 1):
                fields = {"id": f"{ws}/R{n}", "kind": "preference", "scope": scope, "status": "active",
                          "provenance": {"by": "worker", "quote": "I say so", "at": "2026-11-01T00:00:00Z"}}
                ids[text] = fields["id"]
                extra.append(f"- 2026-11-01 {text} <!-- record: {work.record_json(fields)} -->")
            head, tail = body.split("## Decisions\n", 1)
            path.write_text(head + "## Decisions\n" + "\n".join(extra) + "\n" + tail)
        self.ok("work", "attach", "hub")
        by_id = {r["id"]: r for r in self.records()}
        self.assertTrue(all(by_id[ids[t]]["status"] == "active" for t, _, _ in forged), "the forged rows must read as active")
        counts = {"cells": 0, "shown": 0}
        with mock.patch.object(work_brief, "PACKET_LINES", 10 ** 6), mock.patch.object(work_brief, "PACKET_CHARS", 10 ** 8):
            texts = {"worktree": self.rules("worktree"), "main": self.rules("main", tasks=("coder-9",))}
        for where, text in texts.items():
            for record in PACKET_RECORDS:
                for scope in PACKET_SCOPES:
                    for k in range(2 if record == "conflicting pair" else 1):
                        counts["cells"] += 1
                        want = packet_expected(record, scope, where)
                        counts["shown"] += want
                        self.assertEqual(f"({ids[f'cell {record} | {scope} | {k}']})" in text, want,
                                         f"worker={where} record={record} scope={scope} #{k}\n{text}")
                    if record == "superseded":  # the owner's replacement is in force wherever its scope covers the worker
                        self.assertEqual(f"replacement {scope}" in text, packet_expected("owner active", scope, where), (where, scope))
                    if record == "conflicting pair":  # one conflict line naming both, never resolved
                        pair = [ids[f"cell {record} | {scope} | {k}"] for k in range(2)]
                        line = [l for l in text.split("\n") if l.startswith("- CONFLICT: ") and all(i in l for i in pair)]
                        self.assertEqual(len(line), 1 if packet_expected(record, scope, where) else 0, (where, scope, text))
        self.assertEqual(counts, {"cells": 2 * 8 * 12, "shown": 3 * 7 + 3 * 4}, counts)
        # A repository whose git directory is elsewhere (`--separate-git-dir`): the main checkout and its linked worktree both resolve to it
        import subprocess
        def git(*argv, cwd):
            subprocess.run(["git", "-C", str(cwd), *argv], check=True, capture_output=True, timeout=30)
        sep, sep_wt = self.tmp / "sep", self.tmp / "sep-wt-alpha"
        sep.mkdir()
        git("init", "-q", "-b", "main", "--separate-git-dir", str(self.tmp / "gitdir-elsewhere"), cwd=sep)
        (sep / "f").write_text("f\n")
        git("add", "f", cwd=sep)
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "first", cwd=sep)
        git("worktree", "add", "-q", "-b", "lane/alpha", str(sep_wt), cwd=sep)
        self.ok("work", "decide", "hub", "rule for the separate-git-dir project", "--scope", f"project:{sep}", *OWNER, "mine")
        self.ok("work", "decide", "hub", "rule for another project", "--scope", f"project:{self.tmp / 'sep-other'}", *OWNER, "mine")
        for name, workdir in (("main checkout", sep), ("linked worktree", sep_wt), ("subdirectory", sep / "sub")):
            (sep / "sub").mkdir(exist_ok=True)
            with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"], "TP_WORK_CONFIG": self.env["TP_WORK_CONFIG"]}):
                text = work_brief.worker_rules(str(workdir), "coder", None, (), ["hub"])
            self.assertIn("rule for the separate-git-dir project", text, name)
            self.assertNotIn("rule for another project", text, name)
        # Under the real caps the same store gives a bounded section that ends by naming how many were left out and where to see them.
        lines = self.assert_bounded(self.rules("worktree"), "capped")
        self.assertRegex(lines[-1], r"^- \+\d+ more left out \(newest shown first\); all of them: tp-agent work records --status active$")

    def test_store_states_counts_and_content(self):
        """Store rows (none, empty, corrupt record, unreadable file, unreadable store, not a directory, a reader that raises), counts (0, 1, 20, 21, 200 matching), one 5000-character
        record, and the content shapes that must arrive as text."""
        none = self.rules()
        store = self.tmp / "work"
        self.assertEqual(none, f"# Rules in force\n\n- no work store at {store}, so no owner rules are recorded for this task\n", "no store: one calm line")
        self.assertFalse((self.tmp / "work").exists(), "no store: reading created it")
        (self.tmp / "work" / "workspaces").mkdir(parents=True)
        self.assertEqual(self.rules(), "", "empty store: no section")
        self.hand_records("zero", [("only another lane", "lane:beta", "active", "owner", "2026-11-01T00:00:00Z")])
        self.assertEqual(self.rules(), "", "0 matching: no section (not an empty heading)")
        for count in (1, 20, 21, 200):
            store = self.tmp / f"count-{count}"
            self.env["TP_WORK_DIR"] = str(store)
            (store / "workspaces").mkdir(parents=True)
            ids = self.hand_records("many", [(f"rule {n}", "global", "active", "owner", f"2026-11-01T00:{n // 60:02d}:{n % 60:02d}Z")
                                             for n in range(count)])
            lines = self.assert_bounded(self.rules(), f"{count} records")
            shown = [l for l in lines if l.startswith("- [preference global] ")]
            if count == 1:
                self.assertEqual(shown, [f'- [preference global] rule 0 — owner: "said 1" ({ids[0]})'])
                self.assertEqual(len(lines), 4)
                continue
            self.assertEqual([l.rsplit(" (", 1)[1] for l in shown], [f"{i})" for i in reversed(ids)][:len(shown)], "newest first, whole lines")
            self.assertEqual(lines[-1], f"- +{count - len(shown)} more left out (newest shown first); all of them: tp-agent work records --status active")
        # a rule is never clipped: a 321-character rule arrives whole (its last sentence too); a 5000-character record alone does not fit
        # the section, so it is left out whole and counted (the shorter, older rule still comes)
        store = self.tmp / "long"
        self.env["TP_WORK_DIR"] = str(store)
        (store / "workspaces").mkdir(parents=True)
        rule = "Keep every commit small. " * 12 + "Final sentence: never skip the check."
        self.assertGreater(len(rule), 320)
        old, huge = self.hand_records("long", [(rule, "global", "active", "owner", "2026-11-01T00:00:00Z"),
                                               ("x" * 5000, "global", "active", "owner", "2026-11-02T00:00:00Z")])
        lines = self.assert_bounded(self.rules(), "321 and 5000 characters")
        self.assertEqual(len(lines), 5, lines)
        self.assertEqual(lines[3], f'- [preference global] {rule} — owner: "said 1" ({old})')
        self.assertEqual(lines[4], "- +1 more left out (newest shown first); all of them: tp-agent work records --status active")
        self.assertNotIn(huge, "\n".join(lines))
        # 40 records in 20 conflicting pairs, over the cap: the closing number counts records, not lines (a conflict line is not one)
        store = self.tmp / "pairs"
        self.env["TP_WORK_DIR"] = str(store)
        (store / "workspaces").mkdir(parents=True)
        self.hand_records("pairs", [(f"pair {n // 2} side {n % 2}", "global", "active", "owner", f"2026-11-01T00:{n // 60:02d}:{n % 60:02d}Z",
                                     f"question {n // 2}?") for n in range(40)])
        lines = self.assert_bounded(self.rules(), "40 records in 20 pairs")
        shown = [l for l in lines if l.startswith("- [preference global] ")]
        self.assertEqual(lines[-1], f"- +{40 - len(shown)} more left out (newest shown first); all of them: tp-agent work records --status active")
        self.assertTrue(any(l.startswith("- CONFLICT: ") for l in lines), lines)
        # content shapes: verbatim on one list line each (a multi-line record's lines are joined by spaces)
        store = self.tmp / "shapes"
        self.env["TP_WORK_DIR"] = str(store)
        self.ok("work", "new", "Shapes", "--id", "shapes")
        for shape in PACKET_SHAPES:
            self.ok("work", "decide", "shapes", f"{shape}\n{shape}", "--scope", "global", *OWNER, shape)
        text = self.rules()
        self.assert_bounded(text, "shapes")
        for shape in PACKET_SHAPES:
            self.assertIn(f"{shape} {shape} — owner: \"{shape}\"", text, shape)
        # corrupt: a workspace file that cannot be parsed is skipped and said; the readable rules still come
        # corrupt: a record whose trailer is not JSON reads as an old line (provisional), so it is never shown
        path = store / "workspaces" / "shapes.md"
        path.write_text(path.read_text().rstrip("\n") + '\n- 2026-11-01 broken trailer <!-- record: {"status": "active", -->\n')
        self.assertNotIn("broken trailer", self.rules(), "corrupt trailer")
        # an unreadable workspace file is skipped and said; the readable rules still come
        broken = store / "workspaces" / "broken.md"
        broken.write_text("---\nid: broken\n---\n")
        os.chmod(broken, 0o000)
        try:
            lines = self.assert_bounded(self.rules(), "unreadable file") if os.geteuid() != 0 else None
        finally:
            os.chmod(broken, 0o600)
            broken.unlink()
        if lines is None:
            lines = ["- 1 work file(s) could not be read, so rules in them are missing: tp-agent work list"] + ["- [design global]"] * len(PACKET_SHAPES)
        self.assertIn("- 1 work file(s) could not be read, so rules in them are missing: tp-agent work list", lines, "unreadable file")
        self.assertEqual(len([l for l in lines if l.startswith("- [design global]")]), len(PACKET_SHAPES), "unreadable file")
        # unreadable / not a directory / a failing reader: the heading and one line saying why; never raises
        locked = self.tmp / "locked"
        (locked / "workspaces").mkdir(parents=True)
        a_file = self.tmp / "a-file"
        a_file.write_text("not a store")
        rows = [("not a directory", a_file, "it is not a directory")]
        if os.geteuid() != 0:
            rows.append(("unreadable", locked, "Permission denied"))
        for name, root, why in rows:
            self.env["TP_WORK_DIR"] = str(root)
            os.chmod(locked, 0o000)
            try:
                text = self.rules()
            finally:
                os.chmod(locked, 0o700)
            lines = self.assert_bounded(text, name)
            self.assertEqual(len(lines), 3, text)
            self.assertTrue(lines[2].startswith("- the owner's rules in force could not be read (work store "), text)
            self.assertIn(why, lines[2], name)
        self.env["TP_WORK_DIR"] = str(store)
        with mock.patch.object(work, "all_records", side_effect=RuntimeError("boom")):
            lines = self.assert_bounded(self.rules(), "reader raises")
        self.assertEqual(lines[2], "- the owner's rules in force could not be read (RuntimeError: boom); this task starts without them: "
                                   "ask Main which rules apply")

    def test_rules_for_names_the_task_its_ancestors_and_the_attached_workspaces(self):
        """bin/tp-agent rules_for (every start through launch: dispatch, lane dispatch, queue tick, retry, investigate): task: records
        of the task, the task it retries or investigates and that one's parent; workspace records of the session's attached workspace."""
        self.ok("work", "new", "Hub", "--id", "hub")
        for scope in ("task:coder-new", "task:coder-prior", "task:coder-first", "task:coder-unrelated", "workspace"):
            self.ok("work", "decide", "hub", f"rule {scope}", "--scope", scope, *OWNER, "yes")
        for ident, parent in (("coder-prior", "coder-first"), ("coder-first", None)):
            (self.state / "tasks" / ident).mkdir()
            (self.state / "tasks" / ident / "meta.json").write_text(json.dumps({"parent": parent} if parent else {}))
        item = {"id": "coder-new", "parent": "coder-prior", "workdir": str(self.worktree), "role": "coder", "lane": "alpha"}
        with mock.patch.dict(os.environ, {"TP_WORK_DIR": self.env["TP_WORK_DIR"]}):
            text = AGENT.rules_for(self.state, item)
            self.assertNotIn("rule workspace", text, "not attached yet")
            self.ok("work", "attach", "hub")
            text = AGENT.rules_for(self.state, item)
            self.assertIn("rule workspace", text)
            for scope in ("task:coder-new", "task:coder-prior", "task:coder-first"):
                self.assertIn(f"rule {scope}", text, scope)
            self.assertNotIn("coder-unrelated", text)
            with mock.patch.object(AGENT.work_index, "attached_ids", side_effect=OSError("gone")):
                text = AGENT.rules_for(self.state, item)
        self.assertEqual(text.split("\n")[2], "- the owner's rules in force could not be read (the task's ancestry or attached workspaces: "
                                              "OSError: gone); this task starts without them: ask Main which rules apply")


if __name__ == "__main__":
    unittest.main()
