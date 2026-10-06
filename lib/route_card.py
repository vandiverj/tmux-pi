"""The fixed-form card the router model reads. Built without a model, from the role, the task's first sentence,
a few key constraint sentences, the deliverable and, when the task says to read a brief, the head of that brief.

The router sees only this card: no tools, no repository, no other context. The worker still gets the full payload.
Secret-shaped values are replaced before the card leaves the machine.
"""

import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import redact_rules  # noqa: E402

CONSTRAINT = re.compile(r"\b(must|never|do not|don't|only|no edits?|not edit|read-only|without|avoid|fail closed|hard rule)\b", re.I)
PATH_LONG = re.compile(r"(?:/[\w.\-@+]+)*/\.tmux-pi/sessions/[0-9a-f]{8,}/tasks/([\w\-]+)/([\w.\-]+)")
ABS = re.compile(r"/Users/[\w.\-]+/(?:[\w.\-]+/)*?([\w.\-]+)(?=[\s,;:)\]]|$)")
# "read the attached build brief first", "follow the brief", "start from the design note"
READ_BRIEF = re.compile(r"\b(?:read|follow|see|start from|use)\b[^.\n]{0,80}\b(?:brief|packet|spec|design|plan|request)\b", re.I)
BRIEF_NAME = re.compile(r"(?:brief|packet|spec|design|plan|request)", re.I)
BRIEF_LINES = 12
BRIEF_CHARS = 600


def squeeze(text):
    text = PATH_LONG.sub(lambda m: "{}/{}".format(m.group(1), m.group(2)), text)
    text = ABS.sub(lambda m: m.group(1), text)
    return re.sub(r"\s+", " ", text).strip()


def sentences(text):
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"'`\d])", text)
    return [p.strip() for p in parts if p.strip()]


def scrub(text):
    """Replace anything the deterministic secret rules recognize with [secret]."""
    for kind, pattern in redact_rules.FORMATS:
        def hide(match, kind=kind):
            if kind == "private_key" or not match.groups() or not match.group(1):
                return "[secret]"
            return match.group(0).replace(match.group(1), "[secret]")
        text = pattern.sub(hide, text)
    return text


def brief_head(task, context, workdir=None):
    """The first lines of an attached brief when the task tells the worker to read one; else ''.

    Only a context file the caller already selected is read, only when its name looks like a brief, never a path
    the sensitive-read rules flag, and at most BRIEF_LINES lines and BRIEF_CHARS characters.
    """
    if not READ_BRIEF.search(task):
        return ""
    try:
        from sensitive_read import sensitive_path
    except ImportError:
        return ""
    base = Path(workdir) if workdir else Path.cwd()
    for spec in context or ():
        raw = re.sub(r":\d+(?:-\d+)?$", "", spec)
        path = Path(raw) if os.path.isabs(raw) else base / raw
        if not BRIEF_NAME.search(path.name) or not path.is_file() or sensitive_path(str(path)):
            continue
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                lines = [line.strip() for _, line in zip(range(BRIEF_LINES * 3), f) if line.strip()][:BRIEF_LINES]
        except OSError:
            continue
        return squeeze(" / ".join(lines))[:BRIEF_CHARS]
    return ""


def build_card(role, task, deliverable="", context=(), workdir=None, task_cap=200, con_cap=170, con_total=240, del_cap=120):
    t = squeeze(task)
    sents = sentences(t)
    first = (sents[0] if sents else t)[:task_cap]
    cons, used = [], 0
    for s in sents[1:]:
        if CONSTRAINT.search(s):
            s = s[:con_cap]
            if used + len(s) > con_total:
                break
            cons.append(s)
            used += len(s)
    d = squeeze(deliverable or "")
    d = (sentences(d)[0] if d else "")[:del_cap]
    lines = ["Role: {}".format(role or "worker"), "Task: {}".format(first)]
    if cons:
        lines.append("Constraints: " + " | ".join(cons))
    lines.append("Deliverable: {}".format(d))
    head = brief_head(task, context, workdir)
    if head:
        lines.append("Brief: {}".format(head))
    return scrub("\n".join(lines))


def card_sha(card):
    return hashlib.sha256(card.encode("utf-8")).hexdigest()
