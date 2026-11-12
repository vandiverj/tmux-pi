#!/usr/bin/env python3
"""HB7a friction report: friction records derived from the places owner corrections are held today. Read-only; Python 3.9,
standard library and lib/ only; nothing here writes a contract, prompt, role, memory or record.

  python3 tools/friction/report.py --map docs/plans/det-prep/hub/hb7/frictions.json [--store DIR] [--labels FILE ...]
                                   [--memory DIR] [--json]

Sources (docs/plans/det-prep/hub/hb7/hb7a-brief.md):
  record:<ws>/<id>        HB3 decision records of kind `correction`, read through lib.work.all_records (`--store` sets TP_WORK_DIR)
  label:<file>#<i>        H48 label rows with label == "process" (`--labels`, matched by file name)
  memory:<file>           Main's feedback memories, `metadata.type: feedback` (`--memory`, no default directory)
  doc:<repo path>#<id>    a heading in a repo doc (the brief-rule signals in loop/ideas.md), provenance main unless worker
Grouping is by the mapping's explicit keys only; an item no key names is `ungrouped`, an item two keys name is a conflict.
Brief rule (a), a `friction:` key on an HB3 record, is DEFERRED (Main's decision): keys come from the mapping only.
Owner authority comes from who wrote the source, never from its text: only an HB3 record with `by: owner` and a quote, or an H48
label row with a quote, is `owner+quote`; a memory file is always `main`; a doc is `main` or `worker`; a mapping source marked
`by: worker` is `worker`. Only `owner+quote` can make a key `active`.
Recurrence is reported as sessions (distinct `sid` after the first correction's session) first, then messages.
The output holds pointers, counts, provenance and times: never a quote, a `why`, a memory body or a label's project.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))
import sensitive_read  # noqa: E402

SCHEMA = "tp-friction-report:v1"
MAP_SCHEMA = "tp-frictions:v1"
HOMES = ("contract line", "tool default", "adapter", "retrieved procedure")
SOURCES = ("record", "label", "memory", "doc")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
POINTER_RE = re.compile(r"^(record|label|memory|doc):(.+)$")
DATE_RE = re.compile(r"^\d{4}-\d\d-\d\d$")
HEADING_DATE_RE = re.compile(r"\((\d{4}-\d\d-\d\d)\)")
FRONT_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][\w-]*):\s*(.*)$")
# A contract line carries evidence when it holds a pointer of the forms this report uses, or an HB3 record id; the word
# "evidence:" in prose is not a pointer.
EVIDENCE_RE = re.compile(r"(?<![\w/-])(?:(?:record|label|memory|doc):[\w./#-]*\w|[a-z0-9][a-z0-9-]*/[DRL]\d+\b)")
CLOSED = {"withdrawn": "withdrawn", "superseded": "superseded", None: "open question (not a record in force)"}


# ------------------------------------------------------------------ time

def parse_time(value):
    """(UTC datetime, date_only) for an ISO date or datetime string, else None (the item is undated). A naive datetime is UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        if DATE_RE.match(text):
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc), True
        stamp = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return (stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)), False


def place(stamp, first):
    """Where a dated item sits against the first owner correction: after, before_or_at, or same_day_unordered (a date-only stamp
    on the first correction's day cannot be ordered against it)."""
    (when, date_only), (first_when, first_date_only) = stamp, first
    if date_only or first_date_only:
        if when.date() == first_when.date():
            return "same_day_unordered"
        return "after" if when.date() > first_when.date() else "before_or_at"
    return "after" if when > first_when else "before_or_at"


def iso(stamp):
    if stamp is None:
        return None
    return stamp[0].date().isoformat() if stamp[1] else stamp[0].strftime("%Y-%m-%dT%H:%M:%SZ")


def item(source, pointer, provenance, stamp, correction, excluded=None, **extra):
    return {"source": source, "pointer": pointer, "provenance": provenance, "time": stamp, "correction": correction,
            "excluded": excluded, **extra}


def sensitive(path):
    return bool(sensitive_read.SENSITIVE_NAME.match(Path(path).name))


# ------------------------------------------------------------------ sources

def load_records(store, problems):
    """HB3 records through lib.work (no second parser): every record becomes an item; only kind `correction` is a source item."""
    info = {"given": store is not None, "records": 0, "corrections": 0, "other_kinds": {}}
    if store is None:
        return {}, info
    try:
        found = Path(store).is_dir()
    except OSError as error:
        problems.append({"source": "record", "where": str(store), "problem": f"store not readable: {type(error).__name__}"})
        return {}, info
    if not found:
        problems.append({"source": "record", "where": str(store), "problem": "store directory not found"})
        return {}, info
    for sub in ("workspaces", "archive/workspaces"):  # Path.glob/is_dir skip an unreadable directory silently on Python 3.13+; listdir raises
        try:
            os.listdir(Path(store) / sub)
        except FileNotFoundError:
            pass
        except OSError as error:
            problems.append({"source": "record", "where": sub, "problem": f"store not readable: {type(error).__name__}"})
            return {}, info
    import work  # noqa: E402  imported here: the store root is read from TP_WORK_DIR at call time
    old = os.environ.get("TP_WORK_DIR")
    os.environ["TP_WORK_DIR"] = str(store)
    skipped = []
    try:
        records = work.all_records(skipped=skipped)
    except Exception as error:  # noqa: BLE001 - an unreadable store is listed, never a crash
        problems.append({"source": "record", "where": str(store), "problem": f"store not readable: {type(error).__name__}"})
        records = []
    finally:
        if old is None:
            os.environ.pop("TP_WORK_DIR", None)
        else:
            os.environ["TP_WORK_DIR"] = old
    for path in skipped:
        problems.append({"source": "record", "where": Path(path).name, "problem": "workspace file not parseable; skipped"})
    out = {}
    for record in records:
        info["records"] += 1
        prov = record.get("provenance") or {}
        by, quote = prov.get("by"), str(prov.get("quote") or "").strip()
        provenance = {"owner": "owner+quote" if quote else "owner without quote", "main": "main", "worker": "worker"}.get(by, "unknown")
        kind = record.get("kind")
        if kind == "correction":
            info["corrections"] += 1
            excluded = CLOSED.get(record.get("status")) if record.get("status") in CLOSED else None
        else:
            info["other_kinds"][kind] = info["other_kinds"].get(kind, 0) + 1
            excluded = f"kind {kind}, not a correction"
        pointer = f"record:{record['id']}"
        out[pointer] = item("record", pointer, provenance, parse_time(prov.get("at")), kind == "correction", excluded,
                            status=record.get("status"), scope=record.get("scope"))
    return out, info


def load_labels(paths, problems):
    """H48 label rows; a `process` row is a source item. Pointers are `label:<file name>#<i>`; `i` is the row's own field, not its
    list position. Only `i`, `label`, `at`, whether `quote` is non-empty, `process_kind` and `recurrence_of` are read out."""
    out, info, names = {}, {"files": [], "rows": 0, "process_rows": 0}, set()
    for raw in paths or []:
        path = Path(raw)
        if sensitive(path):
            problems.append({"source": "label", "where": path.name, "problem": "sensitive file name; refused"})
            continue
        if path.name in names:
            problems.append({"source": "label", "where": path.name, "problem": "a second labels file with this name; skipped"})
            continue
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            problems.append({"source": "label", "where": path.name, "problem": f"not readable as JSON: {type(error).__name__}"})
            continue
        if not isinstance(rows, list):
            problems.append({"source": "label", "where": path.name, "problem": "not a JSON list; skipped"})
            continue
        names.add(path.name)
        info["files"].append(path.name)
        seen = set()
        for position, row in enumerate(rows):
            where = f"{path.name} row {position}"
            if not isinstance(row, dict):
                problems.append({"source": "label", "where": where, "problem": "row is not an object; skipped"})
                continue
            index, label = row.get("i"), row.get("label")
            if not isinstance(index, int) or isinstance(index, bool):
                problems.append({"source": "label", "where": where, "problem": "`i` missing or not an integer; skipped"})
                continue
            if index in seen:
                problems.append({"source": "label", "where": where, "problem": f"duplicate i {index}; later row skipped"})
                continue
            if not isinstance(label, str):
                problems.append({"source": "label", "where": where, "problem": "`label` missing or not a string; skipped"})
                continue
            seen.add(index)
            info["rows"] += 1
            stamp = parse_time(row.get("at"))
            if stamp is None and row.get("at") not in (None, ""):
                problems.append({"source": "label", "where": f"{path.name}#{index}", "problem": "`at` not a time; counted undated"})
            quote = row.get("quote")
            provenance = "owner+quote" if isinstance(quote, str) and quote.strip() else "owner without quote"
            process = label == "process"
            info["process_rows"] += process
            of = row.get("recurrence_of")
            pointer = f"label:{path.name}#{index}"
            out[pointer] = item("label", pointer, provenance, stamp, process, None if process else f"label {label}, not process",
                                sid=row.get("sid") if isinstance(row.get("sid"), str) and row.get("sid") else None,
                                process_kind=row.get("process_kind") if isinstance(row.get("process_kind"), str) else None,
                                recurrence_of=f"label:{path.name}#{of}" if isinstance(of, int) and not isinstance(of, bool) else None)
    return out, info


def frontmatter(text):
    """(meta, body) of a `---` block with top-level `key: value` lines and one level of indented children; None when malformed."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(n for n in range(1, len(lines)) if lines[n].strip() == "---")
    except StopIteration:
        return None
    meta, parent = {}, None
    for line in lines[1:end]:
        if not line.strip():
            continue
        match = FRONT_LINE_RE.match(line)
        if not match:
            return None
        indent, key, value = match.groups()
        value = value.strip().strip("'\"")
        if indent:
            if parent is None:
                return None
            meta[parent][key] = value
        elif value:
            meta[key], parent = value, None
        else:
            meta[key], parent = {}, key
    return meta, "\n".join(lines[end + 1:])


def load_memory(directory, problems):
    """Memory files: one fact per file with frontmatter `name`, `metadata.type`. A `feedback` file is a source item; its provenance is
    always main (Main writes every memory file, whatever it quotes). Memory files carry no date: undated."""
    info = {"given": directory is not None, "files": 0, "feedback": 0, "other_types": {}}
    if directory is None:
        return {}, info
    folder = Path(directory)
    try:
        if not folder.is_dir():
            problems.append({"source": "memory", "where": folder.name, "problem": "memory directory not found"})
            return {}, info
        entries = sorted(folder.iterdir())
    except OSError as error:
        problems.append({"source": "memory", "where": folder.name, "problem": f"memory directory not readable: {type(error).__name__}"})
        return {}, info
    out = {}
    for path in entries:
        name = path.name
        if name.startswith(".") or not name.endswith(".md") or name == "MEMORY.md":
            continue
        if sensitive(path):
            problems.append({"source": "memory", "where": name, "problem": "sensitive file name; refused"})
            continue
        try:
            regular = not path.is_symlink() and path.is_file()
        except OSError as error:
            problems.append({"source": "memory", "where": name, "problem": f"not readable: {type(error).__name__}"})
            continue
        if not regular:
            problems.append({"source": "memory", "where": name, "problem": "not a regular file; skipped"})
            continue
        try:
            parsed = frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as error:
            problems.append({"source": "memory", "where": name, "problem": f"not readable: {type(error).__name__}"})
            continue
        meta = parsed[0] if parsed else None
        kind = meta.get("metadata", {}).get("type") if meta and isinstance(meta.get("metadata"), dict) else None
        if not meta or not isinstance(meta.get("name"), str) or not meta["name"] or not kind:
            problems.append({"source": "memory", "where": name, "problem": "frontmatter malformed (needs name and metadata.type)"})
            continue
        info["files"] += 1
        feedback = kind == "feedback"
        if feedback:
            info["feedback"] += 1
        else:
            info["other_types"][kind] = info["other_types"].get(kind, 0) + 1
        pointer = f"memory:{name}"
        out[pointer] = item("memory", pointer, "main", None, feedback, None if feedback else f"type {kind}, not feedback")
    return out, info


def resolve_doc(pointer, rest, by, root):
    """A `doc:<repo path>#<heading id>` pointer: the file must be in the repo and hold a heading `#... <id>`; the heading's
    `(YYYY-MM-DD)` dates it. Provenance main, or worker when the pointer says so or the path is a task artifact."""
    path_text, _, heading = rest.partition("#")
    if not heading or not path_text or Path(path_text).is_absolute() or ".." in Path(path_text).parts:
        return None, "doc pointer must be <repo-relative path>#<heading id>"
    path = root / path_text
    if sensitive(path):
        return None, "sensitive file name; refused"
    pattern = re.compile(rf"^#+\s+{re.escape(heading)}(?![\w.-])")
    try:
        if not path.is_file():
            return None, "doc not found"
        line = next((l for l in path.read_text(encoding="utf-8").split("\n") if pattern.match(l)), None)
    except (OSError, UnicodeDecodeError):
        return None, "doc not readable"
    if line is None:
        return None, "heading not found in doc"
    date = HEADING_DATE_RE.search(line)
    worker = by == "worker" or "/tasks/" in f"/{path_text}" or path.name in ("result.md", "report.md")
    return item("doc", pointer, "worker" if worker else "main", parse_time(date.group(1)) if date else None, True), None


# ------------------------------------------------------------------ mapping

def load_map(path, problems):
    """The tracked mapping: {"schema": "tp-frictions:v1", "keys": {KEY: {by, trigger, correction, successful_action, scope,
    proposed_home, home_target, why_home, worked_if, sources: [POINTER | {"ref": POINTER, "by": "main"|"worker"}]}}}."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        problems.append({"source": "map", "where": Path(path).name, "problem": f"mapping not readable: {type(error).__name__}"})
        return []
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        problems.append({"source": "map", "where": Path(path).name, "problem": "mapping has no `keys` object"})
        return []
    if data.get("schema") != MAP_SCHEMA:
        problems.append({"source": "map", "where": Path(path).name, "problem": f"schema is not {MAP_SCHEMA}; read anyway"})
    keys = []
    for name, entry in data["keys"].items():
        if not KEY_RE.match(name) or not isinstance(entry, dict):
            problems.append({"source": "map", "where": name, "problem": "key name invalid or entry not an object; skipped"})
            continue
        text = {f: entry.get(f) if isinstance(entry.get(f), str) else None
                for f in ("by", "trigger", "correction", "successful_action", "scope", "proposed_home", "home_target", "why_home",
                          "worked_if")}
        if text["proposed_home"] not in HOMES:
            problems.append({"source": "map", "where": name, "problem": f"proposed_home not one of {', '.join(HOMES)}"})
            text["proposed_home"] = None
        refs = []
        for ref in entry.get("sources") if isinstance(entry.get("sources"), list) else []:
            by = None
            if isinstance(ref, dict):
                ref, by = ref.get("ref"), ref.get("by")
            if not isinstance(ref, str) or not POINTER_RE.match(ref) or by not in (None, "main", "worker"):
                problems.append({"source": "map", "where": name, "problem": f"source pointer malformed: {str(ref)[:80]}"})
                continue
            if ref in (r for r, _ in refs):
                problems.append({"source": "map", "where": name, "problem": f"pointer listed twice in the key: {ref}"})
                continue
            refs.append((ref, by))
        if not refs:
            problems.append({"source": "map", "where": name, "problem": "key has no usable source pointers"})
        keys.append({"key": name, **text, "refs": refs})
    return keys


# ------------------------------------------------------------------ report

def view(entry, excluded=None):
    out = {"pointer": entry["pointer"], "provenance": entry["provenance"], "at": iso(entry["time"])}
    if excluded:
        out["excluded"] = excluded
    return out


def recurrence(occurrences):
    """Occurrences after the first dated owner+quote correction, per source and total; undated and same-day date-only items are
    counted apart, never placed. `recurrence_sessions`: distinct label `sid`s among the items after, not counting the first
    correction's own session; label items without a `sid` are `after_without_sid`, never counted as a session."""
    firsts = [o for o in occurrences if o["provenance"] == "owner+quote" and o["time"] is not None]
    if not firsts:
        return {"first_correction": None, "after": None, "total_after": None, "recurrence_sessions": None,
                "reason": "no dated owner correction with a quote", "undated": sum(o["time"] is None for o in occurrences)}
    start = min(firsts, key=lambda o: (o["time"][0], not o["time"][1]))
    first = start["time"]
    after, counts, sessions, no_sid = {}, {"before_or_at": 0, "same_day_unordered": 0, "undated": 0}, set(), 0
    for o in occurrences:
        where = "undated" if o["time"] is None else place(o["time"], first)
        if where == "after":
            after[o["source"]] = after.get(o["source"], 0) + 1
            if o["source"] == "label":
                if o.get("sid") is None:
                    no_sid += 1
                elif o["sid"] != start.get("sid"):
                    sessions.add(o["sid"])
        else:
            counts[where] += 1
    return {"first_correction": iso(first), "after": after, "total_after": sum(after.values()), "recurrence_sessions": len(sessions),
            "after_without_sid": no_sid, **counts}


def build(map_path, store=None, labels=None, memory=None, root=ROOT):
    import work  # noqa: E402  SCOPE_RE: the closed scope list of HB3
    problems = []
    keys = load_map(map_path, problems)
    records, record_info = load_records(store, problems)
    label_items, label_info = load_labels(labels, problems)
    memory_items, memory_info = load_memory(memory, problems)
    items = {**records, **label_items, **memory_items}
    claims = {}
    for key in keys:
        for ref, _ in key["refs"]:
            claims.setdefault(ref, []).append(key["key"])
    conflicts = [{"pointer": ref, "keys": names} for ref, names in sorted(claims.items()) if len(names) > 1]
    out_keys = []
    for key in keys:
        occurrences, evidence, unresolved = [], [], []
        for ref, by in key["refs"]:
            if len(claims[ref]) > 1:
                evidence.append({"pointer": ref, "excluded": "claimed by keys " + ", ".join(claims[ref])})
                continue
            source, rest = POINTER_RE.match(ref).groups()
            if source == "doc":
                found, reason = resolve_doc(ref, rest, by, root)
            else:
                found = items.get(ref)
                reason = None if found else not_found(source, rest, store, label_info, memory)
            if found is None:
                unresolved.append({"pointer": ref, "problem": reason})
                continue
            if by == "worker":
                found = {**found, "provenance": "worker"}  # the mapping says a worker wrote it: evidence only, whatever the source
            excluded = found["excluded"] or (None if found["correction"] else "not a correction")
            if not excluded and found["provenance"] == "worker":
                excluded = "worker text: evidence only, never policy"
            evidence.append(view(found, excluded))
            if not excluded:
                occurrences.append(found)
        scope_stated = bool(key["scope"] and work.SCOPE_RE.fullmatch(key["scope"]))
        owner = any(o["provenance"] == "owner+quote" for o in occurrences)
        by_source = {}
        for o in occurrences:
            row = by_source.setdefault(o["source"], {"dated": 0, "undated": 0})
            row["dated" if o["time"] else "undated"] += 1
        out_keys.append({
            "key": key["key"], "by": key["by"], "status": "active" if owner and scope_stated else "candidate",
            "status_reason": ("owner correction with quote, scope stated" if owner and scope_stated else
                              "no owner correction with a quote" if not owner else "scope not stated"),
            "scope": key["scope"] if scope_stated else None, "trigger": key["trigger"], "correction": key["correction"],
            "successful_action": key["successful_action"], "proposed_home": key["proposed_home"],
            "home_target": key["home_target"], "why_home": key["why_home"], "worked_if": key["worked_if"],
            "occurrences": by_source, "recurrence": recurrence(occurrences), "evidence": evidence, "unresolved": unresolved})
    ungrouped = {s: [] for s in ("record", "label", "memory")}
    for pointer, found in sorted(items.items(), key=lambda kv: sort_key(kv[0])):
        if not found["correction"] or pointer in claims:
            continue
        row = view(found, found["excluded"])
        if found["source"] == "label":
            row["process_kind"] = found["process_kind"]
            if found["recurrence_of"]:
                row["recurrence_of"] = found["recurrence_of"]
                owners = claims.get(found["recurrence_of"], [])
                if len(owners) == 1:
                    row["hint"] = f"recurrence_of is in key {owners[0]}"
        ungrouped[found["source"]].append(row)
    return {"schema": SCHEMA, "inputs": {"records": record_info, "labels": label_info, "memory": memory_info, "keys": len(keys)},
            "keys": out_keys, "ungrouped": ungrouped, "conflicts": conflicts, "contract_size": contract_size(root, problems),
            "problems": problems}


def sort_key(pointer):
    head, _, tail = pointer.rpartition("#")
    return (head, int(tail)) if tail.isdigit() else (pointer, 0)


def not_found(source, rest, store, label_info, memory):
    if source == "record":
        return "no --store given" if store is None else "record not in the store"
    if source == "label":
        return "labels file not given" if rest.partition("#")[0] not in label_info["files"] else "row not in the file"
    return "no --memory given" if memory is None else "memory file not found, malformed or refused"


def contract_size(root, problems):
    """Lines, non-blank lines, words and evidence-carrying lines of Main's prompt, each role file and the worker contract. `total`
    is every file; `brief_total` is the brief's population (Main's prompt and the role files) for comparison with it."""
    root = Path(root)
    files = [(root / "claude" / "main-prompt.md", True)]
    for sub, in_brief in (("roles", True), ("contract", False)):
        folder = root / "agent" / sub
        try:  # os.listdir raises on every Python; Path.glob skips an unreadable directory silently from 3.13
            names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
        except FileNotFoundError:
            names = []
        except OSError as error:
            problems.append({"source": "contract", "where": f"agent/{sub}", "problem": f"directory not readable: {type(error).__name__}"})
            continue
        files += [(folder / n, in_brief) for n in names]
    rows = []
    zero = {"lines": 0, "nonblank": 0, "words": 0, "evidence_lines": 0}
    total, brief_total = dict(zero), dict(zero)
    for path, in_brief in files:
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError):
            problems.append({"source": "contract", "where": str(path.relative_to(root)), "problem": "not readable"})
            continue
        if lines and lines[-1] == "":
            lines.pop()
        row = {"file": str(path.relative_to(root)), "lines": len(lines), "nonblank": sum(bool(l.strip()) for l in lines),
               "words": sum(len(l.split()) for l in lines), "evidence_lines": sum(bool(EVIDENCE_RE.search(l)) for l in lines)}
        rows.append(row)
        for k in total:
            total[k] += row[k]
            if in_brief:
                brief_total[k] += row[k]
    return {"files": rows, "total": total, "brief_total": brief_total}


def render(report):
    out, inputs = [], report["inputs"]
    rec, lab, mem = inputs["records"], inputs["labels"], inputs["memory"]
    out.append("# Friction report (read-only)")
    out.append("HB3 records: " + ("--store not given" if not rec["given"] else
                                  f"{rec['records']} records, {rec['corrections']} of kind correction"
                                  + ("  (the store holds no records)" if rec["records"] == 0 else "")))
    out.append(f"label rows: {lab['rows']} in {len(lab['files'])} file(s), {lab['process_rows']} process")
    out.append("memory: " + ("--memory not given" if not mem["given"] else f"{mem['files']} files, {mem['feedback']} feedback"))
    for key in report["keys"]:
        rec_ = key["recurrence"]
        out.append("")
        out.append(f"## {key['key']}  [{key['status']}: {key['status_reason']}]  by {key['by'] or 'unknown'}")
        out.append(f"scope: {key['scope'] or 'not stated'}   home: {key['proposed_home'] or 'unknown'}"
                   + (f" -> {key['home_target']}" if key["home_target"] else ""))
        occ = ", ".join(f"{s} {c['dated']} dated/{c['undated']} undated" for s, c in sorted(key["occurrences"].items())) or "none"
        out.append(f"occurrences: {occ}")
        if rec_["total_after"] is None:
            out.append(f"recurrence after correction: n/a ({rec_['reason']}; undated {rec_['undated']})")
        else:
            per = ", ".join(f"{s} {n}" for s, n in sorted(rec_["after"].items())) or "none"
            out.append(f"recurrence after correction ({rec_['first_correction']}): sessions {rec_['recurrence_sessions']} "
                       f"(label items without a session id, not counted: {rec_['after_without_sid']}); messages "
                       f"{rec_['total_after']} ({per}); before/at {rec_['before_or_at']}, same-day unordered "
                       f"{rec_['same_day_unordered']}, undated {rec_['undated']}")
        for ev in key["evidence"]:
            out.append(f"  - {ev['pointer']}  {ev.get('provenance', '')} {ev.get('at') or 'undated'}"
                       + (f"  EXCLUDED: {ev['excluded']}" if ev.get("excluded") else ""))
        for un in key["unresolved"]:
            out.append(f"  - {un['pointer']}  UNRESOLVED: {un['problem']}")
    out.append("")
    out.append("## Ungrouped (no key names them)")
    for source, rows in report["ungrouped"].items():
        out.append(f"{source}: {len(rows)}")
        for row in rows:
            extra = "".join(f"  {k}={row[k]}" for k in ("process_kind", "recurrence_of", "hint", "excluded") if row.get(k))
            out.append(f"  - {row['pointer']}  {row['provenance']} {row['at'] or 'undated'}{extra}")
    if report["conflicts"]:
        out.append("")
        out.append("## Conflicts (counted in no key)")
        out += [f"  - {c['pointer']}: {', '.join(c['keys'])}" for c in report["conflicts"]]
    size = report["contract_size"]
    out.append("")
    out.append("## Contract size")
    out += [f"  {r['file']}: {r['lines']} lines ({r['nonblank']} non-blank), {r['words']} words, {r['evidence_lines']} with evidence"
            for r in size["files"]]
    t = size["total"]
    out.append(f"  total: {t['lines']} lines ({t['nonblank']} non-blank), {t['words']} words, {t['evidence_lines']} with evidence")
    b = size["brief_total"]
    out.append(f"  brief population (main-prompt + roles): {b['lines']} lines ({b['nonblank']} non-blank), {b['words']} words, "
               f"{b['evidence_lines']} with evidence")
    if report["problems"]:
        out.append("")
        out.append("## Inputs listed and skipped")
        out += [f"  - {p['source']} {p['where']}: {p['problem']}" for p in report["problems"]]
    return "\n".join(out)


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(prog="tools/friction/report.py", description=__doc__.split("\n")[0])
    parser.add_argument("--map", required=True, help="the friction mapping (docs/plans/det-prep/hub/hb7/frictions.json)")
    parser.add_argument("--store", help="HB3 work store directory (sets TP_WORK_DIR for lib.work); not read when absent")
    parser.add_argument("--labels", nargs="*", default=[], help="H48 labels JSON files")
    parser.add_argument("--memory", help="a memory directory (no default)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = build(args.map, args.store, args.labels, args.memory, root)
    print(json.dumps(report, indent=1, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
