"""Deterministic scoring: planted-defect matching on review text, exact answers, hidden vitest results."""
import json
import re
from pathlib import PurePosixPath

FINDING = re.compile(r"^[\s>*#_`-]*\**F(\d+)\b", re.M)
CLOSES = re.compile(r"Closes\s+by:\s*\**\s*(code|check|decision|none)\b", re.I)


def parse_findings(text):
    """[{"id": "F1", "text": ..., "closes_by": "code"|...|None}] from a review reply (each finding starts at a line `F<n>`)."""
    starts = [(m.start(), m.group(1)) for m in FINDING.finditer(text or "")]
    out = []
    for i, (pos, num) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        body = text[pos:end].strip()
        closes = CLOSES.findall(body)
        out.append({"id": f"F{num}", "text": body, "closes_by": closes[-1].lower() if closes else None})
    return out


def _lines_named(text, file):
    """Line numbers a finding gives for `file`: `file:12`, `file:12-20`, `file line 12`, `lines 12-20`, `L12`."""
    numbers = set()
    base = re.escape(PurePosixPath(file).name)
    for m in re.finditer(base + r"[:#( ]*(?:lines?\s*|L)?(\d+)(?:\s*[-–]\s*(?:L)?(\d+))?", text):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        numbers.update(range(lo, min(hi, lo + 200) + 1))
    for m in re.finditer(r"\b(?:lines?|L)\s*(\d+)(?:\s*[-–]\s*(\d+))?", text, re.I):
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        numbers.update(range(lo, min(hi, lo + 200) + 1))
    return numbers


def finding_matches(finding_text, defect):
    """A finding names a planted defect when it mentions the file, a line inside the defect's window, and a keyword."""
    text = finding_text
    if PurePosixPath(defect["file"]).name.lower() not in text.lower():
        return False
    windows = defect.get("windows") or [[defect["line_start"], defect["line_end"]]]
    named = _lines_named(text, defect["file"])
    if not any(lo <= n <= hi for lo, hi in windows for n in named):
        return False
    low = text.lower()
    return any(k.lower() in low for k in defect["keywords"])


def score_review(text, key, clean):
    """{"defects": n, "caught": n, "caught_ids": [...], "false_positives": n, "findings": n}.

    Each finding and each defect is used once. On the clean diff, a finding that `Closes by: code` is a false positive."""
    findings = parse_findings(text)
    caught, used = [], set()
    for defect in key.get("defects", []):
        for f in findings:
            if f["id"] in used:
                continue
            if finding_matches(f["text"], defect):
                caught.append(defect["id"])
                used.add(f["id"])
                break
    fp = sum(1 for f in findings if f["closes_by"] == "code") if clean else 0
    return {"defects": len(key.get("defects", [])), "caught": len(caught), "caught_ids": caught,
            "false_positives": fp, "findings": len(findings)}


def normalise(text):
    return " ".join(str(text).split())


def reply_answer(text):
    """The text after the last `ANSWER:` marker in a reply, else None."""
    found = re.findall(r"^[\s*_`>-]*ANSWER:\s*(.*(?:\n(?![\s*_`>-]*ANSWER:).*)*)", text or "", re.M)
    if not found:
        return None
    return found[-1].strip().strip("`*_ ")


def score_local(reply, expected):
    got = reply_answer(reply)
    return got is not None and normalise(got) == normalise(expected)


def parse_vitest(stdout):
    """(passed, total) from `vitest run --reporter=json` output (noise before or after the JSON is ignored); None if unreadable."""
    start, end = (stdout or "").find("{"), (stdout or "").rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(stdout[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("numTotalTests"), int):
        return None
    return int(data.get("numPassedTests", 0)), data["numTotalTests"]


def code_passed(returncode, parsed):
    """A code task passes when the hidden tests ran (at least one), all passed, and vitest exited 0."""
    return returncode == 0 and parsed is not None and parsed[1] > 0 and parsed[0] == parsed[1]
