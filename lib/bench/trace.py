"""Tool-trace scoring for orient tasks (and tool counts for every task), built on lib/routines.py."""
import re

import routines

HELP_PROBE = "tp-agent --help/-h/help"
BASE_PROBE = "lane base / merge-base / origin base"
HELP_ANY = re.compile(r"(?:^|\s)(?:--help|-h)(?:\s|$)")
SLEEP = re.compile(r"(?:^|[;&|\s])sleep\s+\d")


def load_calls(path):
    try:
        return routines.parse_transcript(path)
    except OSError:
        return None


def analyse(calls):
    """{"calls", "tool_counts", "help", "reread", "base_lookup", "sleep_poll", "orient_waste"} for a parsed transcript.

    A re-read is a Read of a file range (path, offset, limit) already read, with no Edit or Write of that path in between."""
    counts, help_n, reread, base, sleeps = {}, 0, 0, 0, 0
    seen = set()
    for call in calls:
        name = call["name"] or "?"
        counts[name] = counts.get(name, 0) + 1
        inp = call["input"]
        if name == "Bash":
            cmd = inp.get("command") if isinstance(inp.get("command"), str) else ""
            probes, _ = routines.keys_for(call)
            if HELP_PROBE in probes or HELP_ANY.search(routines.HEREDOC.sub("", cmd)):
                help_n += 1
            if BASE_PROBE in probes:
                base += 1
            if SLEEP.search(cmd):
                sleeps += 1
        elif name == "Read":
            path = inp.get("file_path") or inp.get("path") or ""
            key = (path, inp.get("offset"), inp.get("limit"))
            if key in seen:
                reread += 1
            seen.add(key)
        elif name in ("Edit", "Write"):
            path = inp.get("file_path") or inp.get("path") or ""
            seen = {k for k in seen if k[0] != path}
    return {"calls": len(calls), "tool_counts": counts, "help": help_n, "reread": reread, "base_lookup": base,
            "sleep_poll": sleeps, "orient_waste": help_n + reread + base + sleeps}
