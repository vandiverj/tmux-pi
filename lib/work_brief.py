"""`tp-agent brief` (HB3a): Main's working brief, built from records only (no model call), at most about 60 lines.

Parts (docs/plans/det-prep/hub/interfaces.md section 5): the workspace and its outcome and acceptance; the active records whose
scope covers this Main (global, project:<repo> holding the session's project, and workspace for an attached workspace); the active
work from the session's task records (tasks, reviews with their verdict, lanes); the open decisions (ids only, never question text,
as the index); obligations (HB5) and the launch record (HB2) as named empty slots until those records exist; the last evidence
pointer; pointers. Each part states its record time, and a part older than the newest compaction is marked stale.

With `attach`, a session with no attached workspace attaches the newest active workspace whose `repos` covers the project (the
others that match are listed); with none, the first line says so and how to make one. Nothing is ever created. The text that
reaches Main is owner-confirmed (only `active` records are shown, and only the owner makes a record active), so worker text never
lands in Main's context through it.
"""
import json
import os
from pathlib import Path
import re

import work
import work_checkpoint
import work_index

MAX_LINES = 60
LINE_CHARS = 240
RULES_SHOWN = 20
TASKS_SHOWN = 10
DECISIONS_SHOWN = 8
MAIN_SCOPES = ("global", "project", "workspace")
NO_WORKSPACE = "no workspace attached: `tp-agent work new TITLE --repo {project} --objective TEXT && tp-agent work attach ID`"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def add_parser(sub):
    p = sub.add_parser("brief", help="print Main's working brief from records only (outcome, acceptance, rules in force, active work, "
                                      "open decisions, evidence, pointers; no model call)")
    p.add_argument("--attach", action="store_true", help="first attach the newest active workspace for this project when none is "
                                                          "attached (Claude Main's SessionStart hook; never creates one)")
    p.add_argument("--workdir", help="the session's project (default TP_WORKDIR, else the current directory)")
    p.add_argument("--hook-json", action="store_true", help="print the SessionStart hook output, one JSON line")
    p.add_argument("--source", choices=["startup", "resume", "compact"], default=None,
                   help="the SessionStart source; with compact the task index text follows the brief (register.tsx injects it only at startup)")


def _clip(text, size=LINE_CHARS):
    text = re.sub(r"\s+", " ", _CONTROL.sub("", str(text or ""))).strip()
    return text if len(text) <= size else text[:size - 1] + "…"


def _when(value):
    return work._parse_when(value)


def _stamp(when):
    return when.strftime(work.STAMP) if when else "unknown"


def project_dir(workdir=None):
    raw = workdir or os.getenv("TP_WORKDIR") or os.getcwd()
    try:
        return Path(raw).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def covers(record, project, attached):
    """Whether an active record applies to this Main: global; project:<repo> holding the project; workspace of an attached one.
    lane:, task: and role: records are for the workers they name (dispatch, HB3b), never Main's brief."""
    scope = record["scope"]
    if scope == "global":
        return True
    if scope == "workspace":
        return record["workspace"] in attached
    if scope.startswith("project:") and project is not None:
        try:
            repo = Path(scope[len("project:"):])
            return project == repo or repo in project.parents
        except (OSError, ValueError):
            return False
    return False


def in_force(records, project, attached):
    """(shown, conflicts): the active records in Main's scope, newest first, that nothing active supersedes; and the groups of two or
    more of them in one scope answering the same question (all kept: the brief never picks one)."""
    replaced = {r["supersedes"] for r in records if r["status"] == "active" and r.get("supersedes")}
    shown = [r for r in records if r["status"] == "active" and r["id"] not in replaced and covers(r, project, attached)]
    shown.sort(key=lambda r: _stamp(_when(r["provenance"]["at"])), reverse=True)
    groups = {}
    for r in shown:
        if r.get("question"):
            groups.setdefault((work.scope_key(r), re.sub(r"\s+", " ", r["question"]).strip().lower()), []).append(r)
    return shown, [g for g in groups.values() if len(g) > 1]


def matching(project, skipped):
    """The active workspaces whose `repos` hold the project, newest first (the index's candidate rule)."""
    if project is None:
        return []
    rows = [w for w in work_index._rows("workspace", work_index._Workspace, skipped) if w.active]
    found = [w for w in rows if any(work_index._inside(project, r) for r in w.repos)]
    return sorted(found, key=lambda w: w.updated, reverse=True)


def last_compaction(state_dir):
    """The time of the newest compaction event of this run (PreCompact or SessionStart(compact)), or None."""
    if state_dir is None:
        return None
    try:
        generations = work_checkpoint._generations(state_dir) or []
    except OSError:
        return None
    for generation in reversed(generations):
        event = work_checkpoint._event(state_dir, generation)
        if "compact" in event["reason"]:
            return _when(event["since"])
    return None


def attach_newest(state_dir, ws_id):
    """Attach `ws_id` to this run the way `work attach` does (session-work.json, recovery generation `attach`)."""
    import argparse
    import work_commands
    work_commands.run(argparse.Namespace(work_command="attach", id=ws_id), state_dir)  # takes the store lock itself


def _evidence(state_dir):
    """The newest `tasks/*/main-evidence/evidence.md` of this run: (path relative to the run, mtime) or None."""
    if state_dir is None:
        return None
    best = None
    for path in sorted((Path(state_dir) / "tasks").glob("*/main-evidence/evidence.md")):
        try:
            if path.is_symlink():
                continue
            when = path.stat().st_mtime
        except OSError:
            continue
        if best is None or when > best[1]:
            best = (path, when)
    if best is None:
        return None
    from datetime import datetime, timezone
    return best[0].relative_to(state_dir).as_posix(), datetime.fromtimestamp(best[1], timezone.utc)


class Brief:
    def __init__(self, compaction):
        self.lines, self.compaction = [], compaction

    def head(self, name, when, text=""):
        stale = " STALE (recorded before the last compaction at " + _stamp(self.compaction) + ")" if (
            when is not None and self.compaction is not None and when < self.compaction) else ""
        self.lines.append(_clip(f"{name} [{_stamp(when) if when else 'no record'}]{stale}:" + (f" {text}" if text else "")))

    def add(self, text):
        self.lines.append(_clip(text))


def build(state_dir, workdir=None, attach=False, index=None):
    """(text, info). `index`: a callable giving the session index value (bin/tp-agent build_index), or None when there is no session;
    it is called after the attach, so it sees it. Every part fails on its own: a broken part says it is unavailable and why; the rest
    is still built."""
    project = project_dir(workdir)
    skipped, notes = [], []
    attached = list(dict.fromkeys(work_index.attached_ids(state_dir))) if state_dir else []
    try:
        found = matching(project, skipped)
    except Exception as error:  # noqa: BLE001 - one part
        found, notes = [], [f"workspace match unavailable: {_clip(error, 120)}"]
    auto = None
    if attach and not attached and found and state_dir is not None:
        try:
            attach_newest(state_dir, found[0].id)
            attached, auto = [found[0].id], found[0].id
        except Exception as error:  # noqa: BLE001 - attaching is an extra; the brief still says what it found
            notes.append(f"auto-attach of {found[0].id} failed: {_clip(error, 160)}")
    brief = Brief(last_compaction(state_dir))
    if not attached:
        brief.add(NO_WORKSPACE.format(project=project or "PATH"))
    brief.add(f"working brief (tp-agent brief; from records only; built {work.now()}; last compaction "
              f"{_stamp(brief.compaction) if brief.compaction else 'none recorded'})")
    for note in notes:
        brief.add(note)
    others = [w.id for w in found if w.id not in attached]
    if not attached and others:
        brief.add("matching workspaces (newest first): " + ", ".join(others) + "; attach one with `tp-agent work attach ID`")
    docs = {}
    for ws_id in attached:
        try:
            doc, health = work_checkpoint.workspace_health(ws_id)
        except Exception as error:  # noqa: BLE001
            doc, health = None, f"corrupt: {type(error).__name__}"
        if doc is None:
            brief.add(f"workspace {ws_id}: unavailable ({health}); `tp-agent work recover {ws_id}` prints its last checkpoint")
            continue
        docs[ws_id] = doc
        how = "attached automatically: the newest active workspace for this project" if ws_id == auto else "attached"
        brief.add(f"workspace {ws_id} \"{doc.meta.get('title', ws_id)}\" ({how}; status {doc.meta.get('status')})"
                  + (f"; also matching: {', '.join(others)}" if ws_id == auto and others else ""))
        when = work.updated_at(doc)
        objective = (work.get_section(doc.body, "Objective") or "").strip()
        brief.head("outcome", when, objective or f"not recorded (tp-agent work set {ws_id} --objective TEXT)")
        acceptance = (work.get_section(doc.body, "Acceptance") or "").strip()
        brief.head("acceptance", when, acceptance or f"not recorded (tp-agent work set {ws_id} --acceptance TEXT)")
    _rules(brief, project, attached, skipped)
    _work(brief, index() if callable(index) else index)
    _decisions(brief, docs)
    brief.add("obligations (HB5): no obligation records exist yet")
    brief.add("launch record (HB2): no launch record exists yet")
    _evidence_part(brief, state_dir, attached)
    if work.skipped_count(skipped):
        brief.add(work.skipped_line(skipped))
    ws = attached[0] if attached else "ID"
    brief.add(f"pointers: tp-agent work show {ws} | tp-agent work records | tp-agent work decisions | tp-agent work recover | tp-agent list")
    lines = brief.lines
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES - 1] + [f"(+{len(lines) - MAX_LINES + 1} lines cut; tp-agent brief prints the same)"]
    return "\n".join(lines), {"attached": attached, "auto_attached": auto, "matching": [w.id for w in found]}


def _rules(brief, project, attached, skipped):
    try:
        records = work.all_records(skipped)
    except Exception as error:  # noqa: BLE001
        brief.add(f"rules in force: unavailable ({_clip(error, 160)})")
        return
    shown, conflicts = in_force(records, project, attached)
    newest = max((_when(r["provenance"]["at"]) for r in shown if _when(r["provenance"]["at"])), default=None)
    brief.head("rules in force (active, owner-confirmed; global, project, workspace)", newest, "" if shown else "none recorded")
    for r in shown[:RULES_SHOWN]:
        quote = r["provenance"]["quote"] or ""
        tail = f" ({r['id']}, {_stamp(_when(r['provenance']['at']))})"  # kept whole: the head is clipped, never the id and time
        head = f"- [{r['kind']} {_clip(_scope_label(r['scope']), 40)}] {_clip(r['text'], 120)} — owner: \"{_clip(quote, 60)}\""
        brief.add(_clip(head, LINE_CHARS - len(tail)) + tail)
    if len(shown) > RULES_SHOWN:
        brief.add(f"- +{len(shown) - RULES_SHOWN} more: tp-agent work records --status active")
    for group in conflicts:
        brief.add(f"CONFLICT: {', '.join(r['id'] for r in group)} are active in one scope and answer the same question; ask the owner "
                  "which stands (`work decide ... --supersedes ID --by owner --quote ...`)")
    waiting = [r["id"] for r in records if r["status"] == "provisional" and covers(r, project, attached)]
    if waiting:
        brief.add(f"provisional (not in force until the owner confirms): {', '.join(waiting[:8])}"
                  + (f" +{len(waiting) - 8}" if len(waiting) > 8 else "") + "; tp-agent work decision confirm ID --quote ...")


def _scope_label(scope):
    return "project:" + Path(scope[len("project:"):]).name if scope.startswith("project:") else scope


def _work(brief, index):
    if index is None:
        brief.add("active work: no session state directory, so no task records were read")
        return
    if "error" in index:
        brief.add(f"active work: unavailable ({_clip(index['error'], 160)})")
        return
    rows = [*index.get("active", []), *index.get("unstatused", []), *index.get("pending", [])]
    newest = max((_when(r.get("updated")) for r in rows if _when(r.get("updated"))), default=None)
    brief.head("active work (session task records)", newest, "" if rows else "no active, paused or unreviewed tasks")
    for r in rows[:TASKS_SHOWN]:
        extra = "".join(f" {k}: {r[k]}" for k in ("verdict", "result_status") if r.get(k))
        lane = f" lane {r['lane']}/{r['lane_step']}" if r.get("lane") else ""
        brief.add(f"- {r.get('id')} {r.get('role')} {r.get('status')}{extra}{lane}")
    if len(rows) > TASKS_SHOWN:
        brief.add(f"- +{len(rows) - TASKS_SHOWN} more: tp-agent list")
    lanes = index.get("lanes") or {}
    if lanes:
        brief.add("lanes: " + "; ".join(f"{name} " + ", ".join(f"{k} {v}" for k, v in sorted(e.get("by_status", {}).items()))
                                        for name, e in sorted(lanes.items())))


def _decisions(brief, docs):
    """Open decisions of the attached workspaces: ids, priority and blocking only (question text stays in the file, as in the index)."""
    rows, newest = [], None
    for ws_id, doc in docs.items():
        try:
            entries = [e for e in work.decisions_of(doc) if e["status"] == "open"]
        except Exception as error:  # noqa: BLE001
            brief.add(f"open decisions of {ws_id}: unavailable ({_clip(error, 120)})")
            continue
        rows += entries
        when = work.updated_at(doc)
        newest = when if newest is None or when > newest else newest
    if not docs:
        return
    rows.sort(key=lambda e: (not e["blocking"], work.PRIORITY_RANK.get(e["priority"], 9)))
    text = ", ".join(f"{e['id']} [{e['priority']}{' blocking' if e['blocking'] else ''}]" for e in rows[:DECISIONS_SHOWN])
    more = f" +{len(rows) - DECISIONS_SHOWN}" if len(rows) > DECISIONS_SHOWN else ""
    brief.head("open decisions", newest, (text + more + "; tp-agent work decisions shows each question") if rows else "none")


def _evidence_part(brief, state_dir, attached):
    found = _evidence(state_dir)
    if found:
        brief.head("last evidence", found[1], found[0])
    else:
        brief.add("last evidence: none recorded (no tasks/*/main-evidence/evidence.md in this run)")
    if attached:
        try:
            latest = work_checkpoint.latest(attached[0])["valid"]
        except Exception:  # noqa: BLE001
            latest = None
        if latest:
            brief.head("last checkpoint", _when(latest[1].get("at")), f"{attached[0]} {latest[0].name} ({latest[1].get('trigger')})")
        else:
            brief.add(f"last checkpoint: none valid for {attached[0]}")


def text_or_unavailable(build_text):
    """The brief, or one line saying it is unavailable and why: a broken store never blocks a start or a compaction."""
    try:
        return build_text()
    except Exception as error:  # noqa: BLE001 - fail open
        return f"working brief unavailable: {type(error).__name__}: {_clip(error, 200)}"


def run(args, state_dir_fn, build_index, render_index, guard=None):
    """The `brief` command. `state_dir_fn` raises without a session; then the brief is built from the store alone."""
    try:
        state = state_dir_fn()
    except Exception:  # noqa: BLE001 - no session: the brief still reads the store
        state = None

    memo = []

    def index():  # once, after any attach, and shared by the brief and the compact index text
        if not memo:
            try:
                memo.append(None if state is None else build_index(state))
            except Exception as error:  # noqa: BLE001
                memo.append({"error": str(error)})
        return memo[0]

    text = text_or_unavailable(lambda: build(state, args.workdir, args.attach and not work.in_worker(), index)[0])
    if guard is not None:
        text = text_or_unavailable(lambda: guard(text))
    if args.source == "compact":  # register.tsx appends the index at startup only; after a compaction this is its one delivery
        try:
            value = index()
            if value is None or "error" in value:
                raise ValueError(value["error"] if value else "no session state directory")
            text += "\n\n" + render_index(value)
        except Exception as error:  # noqa: BLE001
            text += f"\n\ntask index unavailable: {_clip(error, 200)}"
    if args.hook_json:
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}})
    return text
