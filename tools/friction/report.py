#!/usr/bin/env python3
"""HB7a friction report: friction records derived from the places owner corrections are held today. Read-only; Python 3.9,
standard library and lib/ only; nothing here writes a contract, prompt, role, memory or record.

  python3 tools/friction/report.py --map docs/plans/det-prep/hub/hb7/frictions.json [--store DIR] [--labels FILE ...]
                                   [--memory DIR] [--ledger FILE] [--ledger-skeleton] [--json]

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
HB7b (hb7b-brief.md): `--ledger` checks a contract ledger (the evidence of each contract line, kept beside the contract) against
the contract files; `--ledger-skeleton` prints one for Main to fill. Both read only; git is run read-only for `commit:` pointers.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
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
# HB7b contract ledger (docs/plans/det-prep/hub/hb7/hb7b-brief.md)
LEDGER_SCHEMA = "tp-contract-ledger:v1"
LEDGER_BY = ("main", "owner", "worker")
LEDGER_STATUS = ("keep", "remove-candidate")
LEDGER_SHA_RE = re.compile(r"^[0-9a-f]{12}$")
LEDGER_POINTER_RE = re.compile(r"^(record|label|memory|doc|key|commit):(.+)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
POPULATION_RE = re.compile(r"^(?:claude/main-prompt\.md|agent/(?:roles|contract)/[^/]+\.md)$")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
BLAME_RE = re.compile(r"^([0-9a-f]{40}) \d+ (\d+)(?: \d+)?$")
GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX")
LEDGER_COUNTS = ("justified_owner", "justified_main", "provenance_only", "worker_evidence_only", "no_resolved_reason", "no_entry",
                 "stale_entries", "removal_with_reason", "removal_without_reason", "problems")
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
    info = {"given": store is not None, "readable": False, "records": 0, "corrections": 0, "other_kinds": {}}
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
    for sub in ("", "workspaces", "archive/workspaces"):  # Path.glob/is_dir skip an unreadable directory silently on Python 3.13+; listdir raises
        try:
            os.listdir(Path(store) / sub)
        except FileNotFoundError:
            pass
        except OSError as error:
            problems.append({"source": "record", "where": sub or str(store), "problem": f"store not readable: {type(error).__name__}"})
            return {}, info
    import work  # noqa: E402  imported here: the store root is read from TP_WORK_DIR at call time
    info["readable"] = True
    old = os.environ.get("TP_WORK_DIR")
    os.environ["TP_WORK_DIR"] = str(store)
    skipped = []
    try:
        records = work.all_records(skipped=skipped)
    except Exception as error:  # noqa: BLE001 - an unreadable store is listed, never a crash
        problems.append({"source": "record", "where": str(store), "problem": f"store not readable: {type(error).__name__}"})
        records, info["readable"] = [], False
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
    info = {"given": directory is not None, "readable": False, "files": 0, "feedback": 0, "other_types": {}}
    if directory is None:
        return {}, info
    folder = Path(directory)
    try:
        if not folder.is_dir():
            problems.append({"source": "memory", "where": folder.name, "problem": "memory directory not found"})
            return {}, info
        entries = [folder / n for n in sorted(os.listdir(folder))]  # listdir raises on every Python; iterdir/glob may skip
    except OSError as error:
        problems.append({"source": "memory", "where": folder.name, "problem": f"memory directory not readable: {type(error).__name__}"})
        return {}, info
    info["readable"] = True
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


def build(map_path, store=None, labels=None, memory=None, root=ROOT, ledger=None):
    import work  # noqa: E402  SCOPE_RE: the closed scope list of HB3
    problems = []
    keys = load_map(map_path, problems)
    records, record_info = load_records(store, problems)
    label_items, label_info = load_labels(labels, problems)
    memory_items, memory_info = load_memory(memory, problems)
    readable = {"record": record_info.pop("readable"), "memory": memory_info.pop("readable")}
    items = {**records, **label_items, **memory_items}
    claims = {}
    for key in keys:
        for ref, _ in key["refs"]:
            claims.setdefault(ref, []).append(key["key"])
    conflicts = [{"pointer": ref, "keys": names} for ref, names in sorted(claims.items()) if len(names) > 1]
    out_keys = []
    for key in keys:
        occurrences, evidence, unresolved = [], [], []
        worker_key = key["by"] == "worker"
        for ref, by in key["refs"]:
            if len(claims[ref]) > 1:
                evidence.append({"pointer": ref, "excluded": "claimed by keys " + ", ".join(claims[ref])})
                continue
            source, rest = POINTER_RE.match(ref).groups()
            if source == "doc":
                found, reason = resolve_doc(ref, rest, "worker" if worker_key else by, root)
            else:
                found = items.get(ref)
                reason = None if found else not_found(source, rest, store, label_info, memory)
            if found is None:
                unresolved.append({"pointer": ref, "problem": reason})
                continue
            if worker_key or by == "worker":  # either mapping level says a worker wrote it: evidence only, whatever the source
                found = {**found, "provenance": "worker"}
            excluded = found["excluded"] or (None if found["correction"] else "not a correction")
            if not excluded and found["provenance"] == "worker":
                excluded = "worker text: evidence only, never policy"
            evidence.append(view(found, excluded))
            if not excluded:
                occurrences.append(found)
        scope_stated = bool(key["scope"] and work.SCOPE_RE.fullmatch(key["scope"]))
        owner = any(o["provenance"] == "owner+quote" for o in occurrences)
        active = owner and scope_stated and not worker_key
        by_source = {}
        for o in occurrences:
            row = by_source.setdefault(o["source"], {"dated": 0, "undated": 0})
            row["dated" if o["time"] else "undated"] += 1
        out_keys.append({
            "key": key["key"], "by": key["by"], "status": "active" if active else "candidate",
            "status_reason": ("key attributed to a worker (by worker): never active" if worker_key else
                              "owner correction with quote, scope stated" if active else
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
    files, unreadable_dirs = read_contract(root, problems)
    out = {"schema": SCHEMA, "inputs": {"records": record_info, "labels": label_info, "memory": memory_info, "keys": len(keys)},
           "keys": out_keys, "ungrouped": ungrouped, "conflicts": conflicts}
    justified = None
    if ledger is not None:
        ctx = {"root": Path(root), "items": items, "keys": {k["key"]: k for k in keys}, "label_files": set(label_info["files"]),
               "store_given": store is not None, "store_readable": readable["record"], "memory_given": memory is not None,
               "memory_readable": readable["memory"]}
        out["contract_ledger"], justified = ledger_report(ledger, files, unreadable_dirs, ctx, problems)
    out["contract_size"] = contract_size(files, justified)
    out["problems"] = problems
    return out


def sort_key(pointer):
    head, _, tail = pointer.rpartition("#")
    return (head, int(tail)) if tail.isdigit() else (pointer, 0)


def not_found(source, rest, store, label_info, memory):
    if source == "record":
        return "no --store given" if store is None else "record not in the store"
    if source == "label":
        return "labels file not given" if rest.partition("#")[0] not in label_info["files"] else "row not in the file"
    return "no --memory given" if memory is None else "memory file not found, malformed or refused"


def read_contract(root, problems):
    """The contract population: Main's prompt, each role file and the worker contract, as {file, in_brief, lines, error} (lines
    is None when the file could not be read; error is `missing` or `not readable`), and the set of unreadable directories."""
    root = Path(root)
    paths = [(root / "claude" / "main-prompt.md", True)]
    unreadable = set()
    for sub, in_brief in (("roles", True), ("contract", False)):
        folder = root / "agent" / sub
        try:  # os.listdir raises on every Python; Path.glob skips an unreadable directory silently from 3.13
            names = sorted(n for n in os.listdir(folder) if n.endswith(".md"))
        except FileNotFoundError:
            names = []
        except OSError as error:
            problems.append({"source": "contract", "where": f"agent/{sub}", "problem": f"directory not readable: {type(error).__name__}"})
            unreadable.add(f"agent/{sub}")
            continue
        paths += [(folder / n, in_brief) for n in names]
    files = []
    for path, in_brief in paths:
        rel = str(path.relative_to(root))
        try:
            lines = path.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeDecodeError) as error:
            problems.append({"source": "contract", "where": rel, "problem": "not readable"})
            files.append({"file": rel, "in_brief": in_brief, "lines": None,
                          "error": "missing" if isinstance(error, FileNotFoundError) else "not readable"})
            continue
        if lines and lines[-1] == "":
            lines.pop()
        files.append({"file": rel, "in_brief": in_brief, "lines": lines, "error": None})
    return files, unreadable


def contract_size(files, justified=None):
    """Lines, non-blank lines, words and evidence-carrying lines of each readable contract file. `total` is every file;
    `brief_total` is the brief's population (Main's prompt and the role files). With a ledger, `justified` maps a file to the
    line indexes a justified entry covers, and those lines carry evidence too."""
    rows = []
    zero = {"lines": 0, "nonblank": 0, "words": 0, "evidence_lines": 0}
    total, brief_total = dict(zero), dict(zero)
    for f in files:
        lines = f["lines"]
        if lines is None:
            continue
        covered = (justified or {}).get(f["file"], set())
        row = {"file": f["file"], "lines": len(lines), "nonblank": sum(bool(l.strip()) for l in lines),
               "words": sum(len(l.split()) for l in lines),
               "evidence_lines": sum(bool(EVIDENCE_RE.search(l)) or n in covered for n, l in enumerate(lines))}
        rows.append(row)
        for k in total:
            total[k] += row[k]
            if f["in_brief"]:
                brief_total[k] += row[k]
    return {"files": rows, "total": total, "brief_total": brief_total}


# ------------------------------------------------------------------ contract ledger (HB7b)

def needing(lines):
    """Indexes of the lines that need a reason: non-blank and not an ATX heading. A fence (``` or ~~~, closed by the same character
    at least as long, or the end of the file) holds no headings: its lines, delimiters included, count like prose."""
    out, fence = [], None
    for n, text in enumerate(lines):
        if not text.strip():
            continue
        opener = FENCE_RE.match(text)
        if fence is None:
            if opener:
                fence = opener.group(1)
            elif HEADING_RE.match(text):
                continue
        elif opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence) and not text.strip().strip(fence[0]):
            fence = None
        out.append(n)
    return out


def line_sha(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:12]


def prefix(text):
    return " ".join(text.split()[:8])


def load_ledger(path, problems, tally=None):
    """(valid entries, {(file, sha): how many entries name it}). The multiplicity counts every entry with a string `file` and `sha`,
    faulty or not, so an identity seen twice is a duplicate whatever else is wrong with either entry. A ledger that cannot be read,
    parsed or is of another schema is one problem and yields no entries; each faulty entry is one problem per fault and is skipped;
    each entry with a duplicated identity is one more problem (valid ones are returned, flagged `duplicate`, so their pointers can
    still be checked). `tally` gets, per problem listed, the file it names (None: no file)."""
    name = Path(path).name

    def add(where, text, file=None):
        problems.append({"source": "ledger", "where": where, "problem": text})
        if tally is not None:
            tally.append(file if isinstance(file, str) else None)

    def fail(problem):
        add(name, problem)
        return [], Counter()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        return fail(f"ledger not readable: {type(error).__name__}")
    except ValueError as error:
        return fail(f"ledger not JSON: {type(error).__name__}")
    if not isinstance(data, dict) or data.get("schema") != LEDGER_SCHEMA:
        return fail(f"schema is not {LEDGER_SCHEMA}; ledger not read")
    if not isinstance(data.get("entries"), list):
        return fail("ledger has no `entries` list; ledger not read")
    named = Counter((e["file"], e["sha"]) for e in data["entries"]
                    if isinstance(e, dict) and isinstance(e.get("file"), str) and isinstance(e.get("sha"), str))
    valid = []
    for position, entry in enumerate(data["entries"]):
        where = f"{name} entry {position}"
        if not isinstance(entry, dict):
            add(where, "entry is not an object; skipped")
            continue
        usable = isinstance(entry.get("file"), str) and isinstance(entry.get("sha"), str)
        if usable:
            where = f"{where} ({entry['file']} {entry['sha'][:12]})"
        faults = []
        need = ["file", "sha", "prefix", "reasons", "by", "status"]
        if entry.get("status") == "remove-candidate":
            need.append("removal_reasons")
        for field in need:
            if field not in entry:
                faults.append(f"missing field {field}")
        for field, value in entry.items():
            if field in ("file", "prefix") and not isinstance(value, str):
                faults.append(f"{field} must be a string")
            elif field == "sha" and not (isinstance(value, str) and LEDGER_SHA_RE.match(value)):
                faults.append("sha must be 12 lowercase hex")
            elif field in ("reasons", "removal_reasons") and not (isinstance(value, list) and all(isinstance(r, str) for r in value)):
                faults.append(f"{field} must be a list of strings")
            elif field in ("by", "status"):
                allowed = LEDGER_BY if field == "by" else LEDGER_STATUS
                if not isinstance(value, str):
                    faults.append(f"{field} must be a string")
                elif value not in allowed:
                    faults.append(f"unknown {field} {value[:40]}")
        if isinstance(entry.get("file"), str) and not POPULATION_RE.match(entry["file"]):
            faults.append("file outside the contract population")
        for fault in faults:
            add(where, fault + "; entry skipped", entry.get("file"))
        duplicate = usable and named[(entry["file"], entry["sha"])] > 1
        if duplicate:
            add(where, "duplicate (file, sha): no entry with it counts", entry["file"])
        if not faults:
            valid.append({**entry, "where": where, "removal_reasons": entry.get("removal_reasons") or [], "duplicate": duplicate})
    return valid, named


def git(root, args, stdin=None):
    """A read-only git call with the repo root as cwd; inherited repository variables (a hook's GIT_DIR) are dropped."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    return subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=str(root), input=stdin, capture_output=True,
                          encoding="utf-8", errors="replace", env=env, timeout=120, check=False)


def check_commits(hexes, root, problems):
    """{hex: True/False} for each commit pointer, or None (with one `commit` problem) when git is missing or fails."""
    if not hexes:
        return {}
    order = sorted(hexes)
    try:
        done = git(root, ["cat-file", "--batch-check"], "".join(f"{h}^{{commit}}\n" for h in order))
    except (OSError, subprocess.SubprocessError) as error:
        problems.append({"source": "commit", "where": str(root), "problem": f"git not usable: {type(error).__name__}; commit pointers unchecked"})
        return None
    out = done.stdout.split("\n")
    if done.returncode != 0 or len(out) < len(order):
        problems.append({"source": "commit", "where": str(root),
                         "problem": f"git cat-file failed (exit {done.returncode}, not a git repository?); commit pointers unchecked"})
        return None
    return {h: line.split(" ")[1:2] == ["commit"] for h, line in zip(order, out)}


def resolve_reason(pointer, ctx):
    """(state, provenance, why): state is resolved, unresolved or unchecked (the source was not given or not readable)."""
    match = LEDGER_POINTER_RE.match(pointer)
    if not match:
        return "unresolved", None, "malformed pointer"
    source, rest = match.groups()
    if source == "commit":
        if not COMMIT_RE.match(rest):
            return "unresolved", None, "malformed pointer (commit:<7-40 lowercase hex>)"
        if ctx["commits"] is None:
            return "unchecked", None, "git not usable (problem listed)"
        return ("resolved", "commit", None) if ctx["commits"][rest] else ("unresolved", None, "commit not in the repository")
    if source == "key":
        if not KEY_RE.match(rest):
            return "unresolved", None, "malformed pointer (key:<friction key>)"
        key = ctx["keys"].get(rest)
        if key is None:
            return "unresolved", None, "key not in the mapping"
        return "resolved", "worker" if key["by"] == "worker" else "main", None
    if source == "doc":
        found, why = resolve_doc(pointer, rest, None, ctx["root"])
        return ("resolved", found["provenance"], None) if found else ("unresolved", None, why)
    if source == "label":
        file_name, sep, index = rest.rpartition("#")
        if not sep or not file_name or not index.isdigit():
            return "unresolved", None, "malformed pointer (label:<file>#<i>)"
        if file_name not in ctx["label_files"]:
            return "unchecked", None, "labels file not given (or not readable)"
    elif source == "record":
        if not ctx["store_given"]:
            return "unchecked", None, "no --store given"
        if not ctx["store_readable"]:
            return "unchecked", None, "store not readable (problem listed)"
    elif source == "memory":
        if not ctx["memory_given"]:
            return "unchecked", None, "no --memory given"
        if not ctx["memory_readable"]:
            return "unchecked", None, "memory directory not readable (problem listed)"
    if source == "label":
        pointer = f"label:{file_name}#{int(index)}"  # the row's own `i`, as load_labels keys it
    found = ctx["items"].get(pointer)
    if found is None:
        return "unresolved", None, {"record": "record not in the store", "label": "row not in the file",
                                    "memory": "memory file not found, malformed or refused"}[source]
    return "resolved", found["provenance"], None


def classify(entry, reasons):
    """The line category of a matched entry from its `by` and its resolved reasons [(pointer, provenance)]."""
    own = [prov for _, prov in reasons if prov != "commit"]
    backing = [prov for prov in own if prov != "worker"]
    if entry["by"] == "worker":
        return "worker_evidence_only"
    if backing:
        return "justified_owner" if "owner+quote" in backing and "worker" not in own else "justified_main"
    if own:
        return "worker_evidence_only"
    return "provenance_only" if reasons else "no_resolved_reason"


def ledger_report(path, files, unreadable_dirs, ctx, problems):
    """The ledger check (HB7b rules 2-9). Returns (section, {file: justified line indexes}). Every ledger problem listed goes into
    `tally` (the file it names, or None) and the counts are taken from it, so the totals equal the problems listed."""
    tally = []
    entries, _ = load_ledger(path, problems, tally)
    hexes = set()
    for entry in entries:  # every structurally valid entry's commit pointers, matched or not
        for pointer in entry["reasons"] + entry["removal_reasons"]:
            if pointer.startswith("commit:") and COMMIT_RE.match(pointer[7:]):
                hexes.add(pointer[7:])
    before = len(problems)
    ctx = {**ctx, "commits": check_commits(hexes, ctx["root"], problems)}
    tally += [None] * (len(problems) - before)
    by_file = {f["file"]: f for f in files}
    counts = {f["file"]: dict.fromkeys(LEDGER_COUNTS, 0) for f in files if f["lines"] is not None}
    lines_of = {}
    for name, row in counts.items():
        need = needing(by_file[name]["lines"])
        row["needing"] = len(need)
        for n in need:
            lines_of.setdefault((name, line_sha(by_file[name]["lines"][n])), []).append(n)
    section = {"given": True, "ledger": Path(path).name, "entries": len(entries), "matched": [], "stale": [], "worker": [], "removal": [],
               "unresolved": [], "unchecked": []}
    justified, matched = {}, set()

    def problem(entry, text):
        problems.append({"source": "ledger", "where": entry["where"], "problem": text})
        tally.append(entry["file"])
    for entry in entries:
        ident = (entry["file"], entry["sha"])
        view_ = {"file": entry["file"], "sha": entry["sha"], "prefix": entry.get("prefix")}
        good = {}
        for role, pointers in (("reason", entry["reasons"]), ("removal", entry["removal_reasons"])):  # diagnostics for every entry
            good[role] = []
            for pointer in pointers:
                state, prov, why = resolve_reason(pointer, ctx)
                if state == "resolved":
                    good[role].append((pointer, prov))
                else:
                    section[state].append({**view_, "role": role, "pointer": pointer[:120], "why": why})
        if entry["duplicate"]:  # listed by the loader; counts for nothing
            continue
        folder = entry["file"].rpartition("/")[0]
        if entry["file"] not in counts:
            state = by_file.get(entry["file"], {}).get("error")
            problem(entry, "directory not readable; entry not checked" if folder in unreadable_dirs else
                    "file not readable; entry not checked" if state == "not readable" else "file not found")
            continue
        covered = lines_of.get(ident)
        if not covered:
            counts[entry["file"]]["stale_entries"] += 1
            section["stale"].append(view_)
            problem(entry, "stale: no current line that needs a reason has this sha")
            continue
        matched.add(ident)
        if entry["status"] == "remove-candidate":
            with_reason = any(prov not in ("commit", "worker") for _, prov in good["removal"])
            counts[entry["file"]]["removal_with_reason" if with_reason else "removal_without_reason"] += 1
            section["removal"].append({**view_, "lines": len(covered),
                                       "removal": "removal with a reason" if with_reason else "removal without a reason"})
        category = classify(entry, good["reason"])
        counts[entry["file"]][category] += len(covered)
        section["matched"].append({**view_, "lines": len(covered), "category": category})
        if category.startswith("justified"):
            justified.setdefault(entry["file"], set()).update(covered)
        if category == "worker_evidence_only":
            section["worker"].append({**view_, "lines": len(covered)})
    for (name, _), covered in lines_of.items():
        if (name, _) not in matched:
            counts[name]["no_entry"] += len(covered)
    for name, row in counts.items():
        row["problems"] = tally.count(name)
    rows = [{"file": name, **row} for name, row in counts.items()]
    section["files"] = rows
    section["total"] = {k: sum(r[k] for r in rows) for k in ("needing",) + LEDGER_COUNTS}
    section["total"]["problems"] = len(tally)  # includes problems that name no readable file
    return section, justified


def skeleton(root=ROOT, ledger=None):
    """(ledger document, problems): one entry per (file, sha) of a needing line that no ledger entry names, reasons from
    `git blame --porcelain` of the working-tree file against HEAD (uncommitted lines add nothing). Writes nothing."""
    problems = []
    files, _ = read_contract(root, problems)
    named = load_ledger(ledger, problems)[1] if ledger else set()
    blame_ok = True
    try:
        head = git(root, ["rev-parse", "--verify", "-q", "HEAD^{commit}"])
        if head.returncode != 0:
            problems.append({"source": "commit", "where": str(root), "problem": "not a git repository or no commit at HEAD; every entry gets reasons []"})
            blame_ok = False
    except (OSError, subprocess.SubprocessError) as error:
        problems.append({"source": "commit", "where": str(root), "problem": f"git not usable: {type(error).__name__}; every entry gets reasons []"})
        blame_ok = False
    entries, index = [], {}
    for f in files:
        if f["lines"] is None:
            continue
        blamed = {}
        if blame_ok:
            try:
                done = git(root, ["blame", "--porcelain", "--", f["file"]])
                if done.returncode != 0:
                    problems.append({"source": "commit", "where": f["file"], "problem": "git blame failed (not committed?); reasons []"})
                for out in done.stdout.split("\n") if done.returncode == 0 else []:
                    match = BLAME_RE.match(out)
                    if match:
                        blamed[int(match.group(2)) - 1] = match.group(1)
            except (OSError, subprocess.SubprocessError) as error:
                problems.append({"source": "commit", "where": f["file"], "problem": f"git blame not usable: {type(error).__name__}"})
        for n in needing(f["lines"]):
            text = f["lines"][n]
            ident = (f["file"], line_sha(text))
            if ident in named:
                continue
            if ident not in index:
                index[ident] = {"file": ident[0], "sha": ident[1], "prefix": prefix(text), "reasons": [], "by": "main", "status": "keep"}
                entries.append(index[ident])
            commit = blamed.get(n)
            pointer = f"commit:{commit[:12]}" if commit and commit.strip("0") else None
            if pointer and pointer not in index[ident]["reasons"]:
                index[ident]["reasons"].append(pointer)
    return {"schema": LEDGER_SCHEMA, "entries": entries}, problems


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
    if "contract_ledger" in report:
        out += render_ledger(report["contract_ledger"])
    if report["problems"]:
        out.append("")
        out.append("## Inputs listed and skipped")
        out += [f"  - {p['source']} {p['where']}: {p['problem']}" for p in report["problems"]]
    return "\n".join(out)


def render_ledger(led):
    def counts(r):
        return (f"{r['needing']} need a reason: justified {r['justified_owner']} owner-stated / {r['justified_main']} main, "
                f"provenance only {r['provenance_only']}, worker text {r['worker_evidence_only']}, no resolved reason "
                f"{r['no_resolved_reason']}, no entry {r['no_entry']}; stale entries {r['stale_entries']}; removal candidates "
                f"{r['removal_with_reason']} with / {r['removal_without_reason']} without a reason; problems {r['problems']}")
    out = ["", f"## Contract ledger ({led['ledger']}, {led['entries']} valid entries)"]
    out += [f"  {r['file']}: {counts(r)}" for r in led["files"]]
    out.append(f"  total: {counts(led['total'])}")
    for title, rows, fmt in (
            ("stale (no current line that needs a reason has this sha)", led["stale"], lambda r: ""),
            ("worker text, evidence only", led["worker"], lambda r: f"  ({r['lines']} line(s))"),
            ("removal candidates (listed, never applied)", led["removal"], lambda r: f"  {r['removal']} ({r['lines']} line(s))"),
            ("unresolved pointers (count for nothing)", led["unresolved"], lambda r: f"  {r['role']} {r['pointer']}: {r['why']}"),
            ("unchecked pointers (source not given; count for nothing)", led["unchecked"],
             lambda r: f"  {r['role']} {r['pointer']}: {r['why']}")):
        if rows:
            out.append(f"  {title}:")
            out += [f"    - {r['file']} {r['sha']}{fmt(r)}" for r in rows]
    return out


EPILOG = ("Grouping comes from the mapping only. A record-local `friction` key is not read: that is deferred (it needs lib/work.py "
          "and interfaces.md section 5). Attribution has two levels: a key or a source pointer marked `by: worker` is worker text and "
          "never makes a key active. "
          "Contract ledger (--ledger, tp-contract-ledger:v1): a non-blank, non-heading line of main-prompt, roles or the worker "
          "contract is matched by (file, sha12 of the stripped line); every pointer (record:, label:, memory:, doc:, key:, "
          "commit:) is resolved, or listed as unresolved or unchecked (source not given) and counts for nothing; commit: alone is "
          "provenance only; by worker is never justified; owner-stated needs a resolved owner record or label row with a quote; "
          "remove-candidate needs a resolved non-commit removal reason or is listed without one; an entry matching no line is "
          "stale. The report never edits a contract file. --ledger-skeleton prints a ledger for lines with no entry (reasons from "
          "git blame) and writes nothing.")


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(prog="tools/friction/report.py", description=__doc__.split("\n")[0], epilog=EPILOG)
    parser.add_argument("--map", required=True, help="the friction mapping (docs/plans/det-prep/hub/hb7/frictions.json)")
    parser.add_argument("--store", help="HB3 work store directory (sets TP_WORK_DIR for lib.work); not read when absent")
    parser.add_argument("--labels", nargs="*", default=[], help="H48 labels JSON files")
    parser.add_argument("--memory", help="a memory directory (no default)")
    parser.add_argument("--ledger", help="a contract ledger JSON (tp-contract-ledger:v1); read only")
    parser.add_argument("--ledger-skeleton", action="store_true",
                        help="print a ledger skeleton for every needing line with no --ledger entry; writes nothing")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.ledger_skeleton:
        doc, problems = skeleton(root, args.ledger)
        print(json.dumps(doc, indent=1, ensure_ascii=False))
        for p in problems:
            print(f"problem: {p['source']} {p['where']}: {p['problem']}", file=sys.stderr)
        return 0
    report = build(args.map, args.store, args.labels, args.memory, root, args.ledger)
    print(json.dumps(report, indent=1, ensure_ascii=False) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
