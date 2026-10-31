"""Report over trial records: first-pass approval by arm, role and size, finding categories, medians, and the paired view.

Input is the rows of records.jsonl (lib/trial_record.py). `summarize` returns a JSON-safe dict, `render` turns it into text.

Counting rules, all stated in the output where a reader needs them:
- The first-pass rate is approved / (approved + request_changes). A first review that is incomplete, unparsed or label-only
  (`inconclusive`) is neither, and is counted on its own; an attempt with no first review is counted as `no_review`.
- A cell with fewer than NOISE_BELOW conclusive attempts is flagged `noise`.
- If a pair_id has more than one record for an arm, the last one in the file is the one used (a rerun replaces an earlier try).
- Medians skip unknown values. `rule_slips` skips records whose transcript could not be scanned (`complete` false).
- Tokens are input + output; cache tokens are left out.
"""

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import trial_record  # noqa: E402

NOISE_BELOW = 5
TOP_CATEGORIES = 10
ARMS = trial_record.ARMS
UNKNOWN = "unknown"


def load(path):
    """(records, malformed line count) from a records.jsonl; a missing file is empty."""
    return trial_record.read_records(path)


def _latest(records):
    """One record per (pair_id, arm): the last in file order. Records without a pair_id or a known arm are dropped."""
    latest = {}
    for record in records:
        if record.get("arm") in ARMS and isinstance(record.get("pair_id"), str):
            latest[(record["pair_id"], record["arm"])] = record
    return latest


def _outcome(record):
    outcome = record.get("first_review_outcome")
    return outcome if outcome in trial_record.OUTCOMES else "no_review"


def _cell(records):
    counts = {"approved": 0, "request_changes": 0, "inconclusive": 0, "no_review": 0}
    for record in records:
        counts[_outcome(record)] += 1
    n = counts["approved"] + counts["request_changes"]
    return {"n": n, "approved": counts["approved"], "request_changes": counts["request_changes"],
            "inconclusive": counts["inconclusive"], "no_review": counts["no_review"],
            "rate": round(counts["approved"] / n, 4) if n else None, "noise": n < NOISE_BELOW}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _median(values):
    values = [v for v in values if v is not None]
    return {"n": len(values), "median": statistics.median(values) if values else None}


def _tokens(record):
    tokens = record.get("tokens")
    if not isinstance(tokens, dict):
        return None
    parts = [_number(tokens.get("input")), _number(tokens.get("output"))]
    return sum(p for p in parts if p is not None) if any(p is not None for p in parts) else None


def _slips(record):
    slips = record.get("rule_slips")
    return _number(slips.get("total")) if isinstance(slips, dict) and slips.get("complete") else None


def _gates(record):
    gates = record.get("gate_runs")
    return _number(gates.get("total")) if isinstance(gates, dict) else None


def _docs(record):
    docs = record.get("docs_calls")
    return _number(docs.get("count")) if isinstance(docs, dict) else None


MEDIANS = (
    ("wall_time_s", lambda r: _number(r.get("wall_time_s"))),
    ("tokens", _tokens),
    ("review_rounds", lambda r: _number(r.get("review_rounds"))),
    ("rule_slips", _slips),
    ("gate_runs", _gates),
    ("docs_calls", _docs),
)


def _paired(latest):
    """2x2 over pair_ids where both arms have a conclusive first review: rows are the qwen outcome, columns the normal one."""
    table = {"qwen_approved": {"normal_approved": 0, "normal_request_changes": 0},
             "qwen_request_changes": {"normal_approved": 0, "normal_request_changes": 0}}
    pairs = 0
    for pair_id in sorted({pid for pid, _ in latest}):
        qwen, normal = latest.get((pair_id, "qwen")), latest.get((pair_id, "normal"))
        if not qwen or not normal:
            continue
        a, b = _outcome(qwen), _outcome(normal)
        if a in ("approved", "request_changes") and b in ("approved", "request_changes"):
            table["qwen_" + a]["normal_" + b] += 1
            pairs += 1
    return {"pairs": pairs, "table": table}


def summarize(records, malformed=0):
    """The report as data. Counts every record for the cells, medians and categories; pairs use the last record per arm."""
    records = [r for r in records if r.get("arm") in ARMS]
    out = {"records": len(records), "malformed": malformed, "noise_below": NOISE_BELOW,
           "by_arm": {}, "cells": [], "categories": {}, "medians": {}, "paired": _paired(_latest(records))}
    for arm in ARMS:
        mine = [r for r in records if r["arm"] == arm]
        out["by_arm"][arm] = _cell(mine)
        counts = {}
        for record in mine:
            for name in record.get("finding_categories") or []:
                if isinstance(name, str):
                    counts[name] = counts.get(name, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_CATEGORIES]
        out["categories"][arm] = [{"family": name, "count": count} for name, count in ranked]
        out["medians"][arm] = {key: _median([fn(r) for r in mine]) for key, fn in MEDIANS}
    keys = sorted({(r.get("role") or UNKNOWN, r.get("size") or UNKNOWN) for r in records})
    for role, size in keys:
        cell = {"role": role, "size": size}
        for arm in ARMS:
            cell[arm] = _cell([r for r in records if r["arm"] == arm and (r.get("role") or UNKNOWN) == role
                               and (r.get("size") or UNKNOWN) == size])
        out["cells"].append(cell)
    return out


def _rate(cell):
    if not cell["n"]:
        return "-"
    return "{}/{} {:.0f}%{}".format(cell["approved"], cell["n"], cell["rate"] * 100, " noise" if cell["noise"] else "")


def _num(value):
    if value is None:
        return "-"
    return str(int(value)) if float(value).is_integer() else "{:.1f}".format(value)


def render(summary, source=None):
    """Plain text for a terminal. An empty input gives one clear line."""
    if not summary["records"]:
        where = " in {}".format(source) if source else ""
        extra = " ({} malformed line(s) skipped)".format(summary["malformed"]) if summary["malformed"] else ""
        return "No trial records{}{}. Nothing to report yet.".format(where, extra)
    lines = ["Trial records: {} ({} malformed line(s) skipped)".format(summary["records"], summary["malformed"]),
             "First-pass approval = approved / (approved + request_changes); cells under {} are noise.".format(summary["noise_below"]),
             "", "By arm:"]
    for arm in ARMS:
        cell = summary["by_arm"][arm]
        lines.append("  {:<7} {:<16} inconclusive {}  no review {}".format(arm, _rate(cell), cell["inconclusive"], cell["no_review"]))
    lines += ["", "By role and size:", "  {:<14} {:<9} {:<18} {:<18}".format("role", "size", "qwen", "normal")]
    for cell in summary["cells"]:
        lines.append("  {:<14} {:<9} {:<18} {:<18}".format(cell["role"], cell["size"], _rate(cell["qwen"]), _rate(cell["normal"])))
    lines += ["", "Top finding categories:"]
    for arm in ARMS:
        top = summary["categories"][arm]
        lines.append("  {:<7} {}".format(arm, ", ".join("{} {}".format(c["family"], c["count"]) for c in top) or "none"))
    lines += ["", "Medians (n known):", "  {:<14} {:<14} {:<14}".format("", "qwen", "normal")]
    for key, _ in MEDIANS:
        cells = ["{} ({})".format(_num(summary["medians"][arm][key]["median"]), summary["medians"][arm][key]["n"]) for arm in ARMS]
        lines.append("  {:<14} {:<14} {:<14}".format(key, *cells))
    paired = summary["paired"]
    lines += ["", "Paired ({} pair(s) with a first review on both arms):".format(paired["pairs"])]
    if paired["pairs"]:
        table = paired["table"]
        lines += ["  {:<24} {:>16} {:>22}".format("", "normal approved", "normal request_changes"),
                  "  {:<24} {:>16} {:>22}".format("qwen approved", table["qwen_approved"]["normal_approved"],
                                                  table["qwen_approved"]["normal_request_changes"]),
                  "  {:<24} {:>16} {:>22}".format("qwen request_changes", table["qwen_request_changes"]["normal_approved"],
                                                  table["qwen_request_changes"]["normal_request_changes"])]
    return "\n".join(lines)


def report(path, as_json=False):
    """The text (or JSON text) of the report for a records.jsonl path."""
    records, bad = load(path)
    summary = summarize(records, bad)
    if as_json:
        return json.dumps(summary, sort_keys=True, indent=2)
    return render(summary, source=str(path))
