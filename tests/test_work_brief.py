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
        self.assertIn("more: tp-agent work records --status active", text)  # more rules than the brief shows
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
        self.assertIn("launch record (HB2): no launch record exists yet", text)
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

    REVIEW = "{verdict}\nRESULT_STATUS: complete\n\nCoverage\n- lib/work_brief.py: reviewed the whole file and its callers\n"

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


if __name__ == "__main__":
    unittest.main()
