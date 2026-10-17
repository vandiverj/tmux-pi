"""The session index as text for the main agent: `tp-agent index --text`, and inside the hook JSON of `--hook-json`.

One renderer for the SessionStart(compact) command hook (claude/plugins/tmux-pi/hooks/sessionstart-compact.sh). The plugin module's
startup append (readIndex in claude/plugins/tmux-pi/hooks/register.tsx) still renders the same text in TypeScript, because it also
needs the rows shown for its wake baseline; claude/plugins/tmux-pi/hooks/index-text-cases.ts holds the cases both renderers must answer
identically (tests/test_index_text.py here, register.test.ts there). Change the two together.

Sizes are counted in UTF-16 code units, as the TypeScript renderer's string lengths are, so the 6000-character budget is the same.
A lone surrogate (valid in a JSON string) is counted and cut as one unit, never raised on; the finished text has each one replaced
by U+FFFD and the C0 controls other than newline and tab dropped (_clean here, wellFormed in register.tsx).
"""
import re

INDEX_CHARS = 6000
WORK_COMMAND = "tp-agent work decisions"
PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}
DEFAULT_DECISION_CHARS = 1200
HEADER = "tmux-pi task index (injected; run `tp-agent index` only if this is missing):"


def _units(text):
    # surrogatepass: a lone surrogate is one code unit, as in JavaScript, and a high and low surrogate side by side are a pair
    return text.encode("utf-16-le", "surrogatepass")


def _n(text):
    return len(_units(text)) // 2


def _head(text, size):
    """The first `size` UTF-16 code units, as JavaScript's slice(0, size) takes them, except that a surrogate pair the cut would split
    is left out whole (cutUnits in register.tsx does the same)."""
    units = _units(text)
    cut = units[:2 * max(size, 0)]
    if len(cut) >= 2 and len(units) > len(cut) and 0xD8 <= cut[-1] <= 0xDB and 0xDC <= units[len(cut) + 1] <= 0xDF:
        cut = cut[:-2]
    return cut.decode("utf-16-le", "surrogatepass")


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")
_LONE = re.compile(r"[\ud800-\udfff]")


def _clean(text):
    """The C0 controls other than newline and tab dropped, then every lone surrogate replaced by U+FFFD (wellFormed in register.tsx
    does the same, in the same order). Run on the finished text: it never lengthens it, so the budget still holds."""
    return _LONE.sub("\ufffd", _units(_CONTROL.sub("", text)).decode("utf-16-le", "surrogatepass"))


def _is_text(value):
    return isinstance(value, str)


def _is_num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


# JavaScript's whitespace (what /\s/ and trim() take), not Python's: str.isspace() also counts U+0085 and U+001C-U+001F, and not U+FEFF.
JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_RUN = re.compile(f"[{JS_SPACE}]+")
_EDGE = re.compile(f"^[{JS_SPACE}]+|[{JS_SPACE}]+$")


def _trim(text):
    return _EDGE.sub("", text)


def _plain(text, size):
    one = _trim(_RUN.sub(" ", text if _is_text(text) else ""))
    return one if _n(one) <= size else _head(one, size - 1) + "…"


def _pending(value):
    return isinstance(value, dict) and value.get("status") == "pending"


def _awaiting(row):
    parts = ["capability request pending" if _pending(row.get("capability_request")) else "",
             "capability approval pending" if row.get("capability_pending") is True else "",
             "permission prompt pending" if _pending(row.get("permission")) else ""]
    return ", ".join(p for p in parts if p)


def _index_line(row):
    if not isinstance(row, dict) or not _is_text(row.get("id")):
        return None
    fields = [row[k] for k in ("id", "role", "harness", "status") if _is_text(row.get(k)) and row[k] != ""]
    if _is_text(row.get("result_status")):
        fields.append(f"result={row['result_status']}")
    # a review's stated verdict never reads as an approval unless its Coverage section was there
    if _is_text(row.get("verdict")):
        fields.append(f"verdict={row['verdict']}" if row.get("review_complete") is True
                      else f"verdict={row['verdict']} [review incomplete: not an approval]")
    if _is_text(row.get("result_path")):
        fields.append(row["result_path"])
    if _is_text(row.get("reason")) and row["reason"] != "":
        fields.append(f"({row['reason']})")
    awaits = _awaiting(row)
    if awaits:
        fields.append(f"[awaits: {awaits}]")
    return f"- {' '.join(fields)}"


def _id_mark(row):
    awaits = _awaiting(row)
    return str(row["id"]) if awaits == "" else f"{row['id']} [{awaits}]"


def _is_refused(row):
    row = _dict(row)
    return row.get("status") == "queued" and _is_text(row.get("reason")) and row["reason"].startswith("start refused: ")


def _is_asking(row):
    return isinstance(row, dict) and row.get("status") == "needs_input"


def _joined(lines):
    return _n("\n".join(lines))


def _refused_lines(rows, room):
    if not rows:
        return [], []
    name = "queue entries that will not start"
    fits = lambda lines: _joined(lines) + 1 <= room  # noqa: E731
    full = [f"{name}:", *[_index_line(r) for r in rows]]
    if fits(full):
        return full, rows
    header = f"{name} (ids only; tp-agent stop <id> removes one):"
    ids = [f"{header} {', '.join(str(r['id']) for r in rows)}"]
    if fits(ids):
        return ids, rows
    shown = []
    for row in rows:
        if not fits([f"{header} {', '.join(str(r['id']) for r in [*shown, row])}"]):
            break
        shown.append(row)
    lines = [] if not shown else [f"{header} {', '.join(str(r['id']) for r in shown)}"]
    lines.append(f"… {len(rows) - len(shown)} refused not shown")
    return lines, shown


def _paused_lines(groups, room):
    named = [(name, [r for r in _list(group) if _index_line(r) is not None]) for name, group in groups]
    all_rows = [r for _, rows in named for r in rows]
    full = []
    for name, rows in named:
        full += [f"{name}:{' none' if not rows else ''}", *[_index_line(r) for r in rows]]
    if _joined(full) <= room:
        return full, all_rows
    ids_header = lambda name: f"{name} (ids only; tp-agent inspect <id>):"  # noqa: E731
    ids = [f"{name}: none" if not rows else f"{ids_header(name)} {', '.join(_id_mark(r) for r in rows)}" for name, rows in named]
    if _joined(ids) <= room:
        return ids, all_rows
    rest = lambda hidden: f"… {hidden} more workers waiting for you not shown; run tp-agent list to see them"  # noqa: E731
    lines, shown = [], []
    size = _n(rest(len(all_rows)))
    is_full = False
    for name, rows in named:
        line = ids_header(name)
        count = 0
        for row in rows:
            piece = f"{' ' if count == 0 else ', '}{_id_mark(row)}"
            # a group's first id also pays for its header and the newline before it
            cost = _n(piece) + (_n(line) + 1 if count == 0 else 0)
            if is_full or size + cost > room:
                is_full = True
                break
            size += cost
            line += piece
            count += 1
            shown.append(row)
        if count > 0:
            lines.append(line)
    lines.append(rest(len(all_rows) - len(shown)))
    return lines, shown


# ---- the durable `work` block ----

def _trailer(more, more_blocking):
    return f"+{more} more ({more_blocking} blocking): {WORK_COMMAND}" if more > 0 else f"{WORK_COMMAND} (every question and detail)"


def _decision_summary(decisions, chars):
    """Header, blocking-first ids (one line each) while they fit `chars`, then the trailer (reserved first). `chars` of 0 gives the
    minimum: the header and the trailer."""
    if not isinstance(decisions, dict):
        return ""
    items = [i for i in _list(decisions.get("items")) if isinstance(i, dict) and _is_text(i.get("id"))]
    opened = decisions["open"] if _is_num(decisions.get("open")) else len(items)
    if opened <= 0:
        return ""
    blocking = decisions["blocking"] if _is_num(decisions.get("blocking")) else sum(1 for i in items if i.get("blocking") is True)
    rank = lambda item: PRIORITY_RANK.get(str(item.get("priority")), 9)  # noqa: E731
    ordered = sorted(items, key=lambda i: (i.get("blocking") is not True, rank(i)))
    header = f"decisions: {opened} open ({blocking} blocking)"
    size = _n(header) + 1 + max(_n(_trailer(opened, blocking)), _n(_trailer(0, 0)))
    lines, shown_blocking = [], 0
    for item in ordered:
        line = f"- {_plain(item['id'], 80)} [{_plain(item.get('priority'), 10)}]" + (" blocking" if item.get("blocking") is True else "")
        if chars <= 0 or size + _n(line) + 1 > chars:
            break
        size += _n(line) + 1
        lines.append(line)
        shown_blocking += 1 if item.get("blocking") is True else 0
    return "\n".join([header, *lines, _trailer(opened - len(lines), blocking - shown_blocking)])


def _decision_chars(work):
    chars = _dict(work.get("decisions")).get("chars")
    return chars if _is_num(chars) and chars > 0 else DEFAULT_DECISION_CHARS


def _work_reserve(index):
    work = index.get("work")
    if not isinstance(work, dict):
        return 0
    text = _decision_summary(work.get("decisions"), 0)
    return 0 if text == "" else _n(text) + 1


def _work_row(row):
    if not isinstance(row, dict) or not _is_text(row.get("id")):
        return None
    health = row.get("health")
    # an attached workspace whose file cannot be used is still named, with why
    if _is_text(health) and health not in ("", "ok"):
        return f"- {_plain(row['id'], 60)} ({_plain(health, 60)})"
    status = row.get("status")
    status = f" [{_plain(status, 12)}]" if _is_text(status) and status not in ("", "active") else ""
    nxt = _plain(row.get("next"), 100)
    sessions = [s for s in _list(row.get("sessions")) if _is_text(s)]
    return (f"- {_plain(row['id'], 60)} \"{_plain(row.get('title'), 80)}\"{status}" + ("" if nxt == "" else f" — next: {nxt}")
            + ("" if not sessions else f" ({', '.join(_plain(s, 40) for s in sessions[-3:])})"))


def _note_list(label, notes):
    rows = [n for n in _list(notes) if isinstance(n, dict) and _is_text(n.get("id"))]
    if not rows:
        return None
    shown = [_plain(n["id"], 60) + ' "' + _plain(n.get("title"), 80) + '"' for n in rows]
    return f"{label}: {'; '.join(shown)}"


def _work_lines(index, room):
    """The `work` lines, most useful first, never more than `room` characters (each line counts its newline). Every line goes
    through the one `append`, which adds it only if it fits; the optional groups also leave the decisions minimum its room."""
    lines = []
    size = 0

    def append(lead, rest, after):
        nonlocal size
        cost = _n(lead) + 1
        if size + cost + after > room:
            return
        fit = []
        for line in rest:
            if size + cost + _n(line) + 1 + after > room:
                continue
            cost += _n(line) + 1
            fit.append(line)
        if rest and not fit:
            return
        size += cost
        lines.extend([lead, *fit])

    error = index.get("work_error")
    work = index.get("work")
    if not isinstance(work, dict):
        if _is_text(error) and error != "":
            append(f"work context not read: {_plain(error, 160)}", [], 0)
        return lines
    reserve = _work_reserve(index)
    rows = lambda group: [r for r in (_work_row(x) for x in _list(group)) if r is not None]  # noqa: E731
    attached, candidates = rows(work.get("attached")), rows(work.get("candidates"))
    others = work["others"] if _is_num(work.get("others")) and work["others"] > 0 else 0
    notes = _dict(work.get("notes"))
    if attached:
        append("work attached to this session (tp-agent work show ID):", attached, reserve)
    checkpoint = work.get("checkpoint") if isinstance(work.get("checkpoint"), dict) else None
    recovery = work.get("recovery") if isinstance(work.get("recovery"), dict) else None
    if recovery is not None and recovery.get("pending") is True:
        what = "workspace attached" if recovery.get("reason") == "attach" else "compaction"
        append(f"recovery pending ({what} at {_plain(recovery.get('since'), 30)}): run tp-agent work recover; "
               f"re-verify any \"done\" claim against live records first", [], reserve)
    autonomy = work.get("autonomy") if isinstance(work.get("autonomy"), dict) else None
    if autonomy is not None and _is_text(autonomy.get("line")) and autonomy["line"] != "":
        append(_plain(autonomy["line"], 300), [], reserve)
    if checkpoint is not None and _is_text(checkpoint.get("line")) and checkpoint["line"] != "":
        append(_plain(checkpoint["line"], 300), [], reserve)
    if recovery is not None and recovery.get("pending") is True and checkpoint is not None \
            and _is_text(checkpoint.get("caution")) and checkpoint["caution"] != "":
        append(_plain(checkpoint["caution"], 300), [], reserve)
    if candidates:
        append("work in this directory, not attached (tp-agent work attach ID):", candidates, reserve)
    if others > 0:
        append(f"+{others} other active workspaces: tp-agent work list", [], reserve)
    for line in (_note_list("notes", notes.get("recent")), _note_list("important notes", notes.get("important"))):
        if line is not None:
            append(line, [], reserve)
    unreadable = _dict(work.get("skipped")).get("count")
    if _is_num(unreadable) and unreadable > 0:
        append(f"{int(unreadable // 1)} unreadable work files; tp-agent work list", [], reserve)
    decisions = _decision_summary(work.get("decisions"), min(_decision_chars(work), max(0, room - size)))
    if decisions != "":
        head, *tail = decisions.split("\n")
        # all or nothing: a decisions block that does not fit whole is left out, never cut mid-way
        if size + _n(decisions) + 1 <= room:
            append(head, tail, 0)
    return lines


def render(index, limit_chars=INDEX_CHARS):
    """The index text the main reads: objective, work, paused, active and pending (not recent), never longer than `limit_chars`."""
    index = _dict(index)
    objective = index.get("objective")
    objective = _trim(objective) if _is_text(objective) and _trim(objective) != "" else "(none set)"
    lines = [HEADER, f"objective: {_head(objective, 1000)}"]
    # room kept for the active and pending headers and the omission line, so the whole text fits
    limit = limit_chars - 120
    active_rows = _list(index.get("active"))
    reserve = _work_reserve(index)
    head = len(lines)
    first, first_rows = _paused_lines(
        [("replied without a result status", index.get("unstatused")), ("paused", index.get("paused")),
         ("needs input", [r for r in active_rows if _is_asking(r)])],
        limit - _joined(lines) - 1 - reserve)
    lines.extend(first)
    refused, _ = _refused_lines([r for r in active_rows if _is_refused(r) and _index_line(r) is not None],
                                limit - _joined(lines) - reserve)
    lines.extend(refused)
    lines[head:head] = _work_lines(index, limit - _joined(lines))
    size = _joined(lines)
    cut = 0
    for name, group in (("active", [r for r in active_rows if not _is_asking(r) and not _is_refused(r)]), ("pending", index.get("pending"))):
        rows = _list(group)
        lines.append(f"{name}:{' none' if not rows else ''}")
        for row in rows:
            line = _index_line(row)
            if line is None:
                continue
            if size + _n(line) + 1 > limit:
                cut += 1
                continue
            size += _n(line) + 1
            lines.append(line)
    if cut > 0:
        lines.append(f"… {cut} more not shown; run tp-agent index for all of them")
    return _clean("\n".join(lines))
