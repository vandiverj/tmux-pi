"""tp_usage.py — the session token usage summary of the status pane, split into a per-record part and a combining part.

The summary used to be one jq program over every task's state.json on every refresh. What one state.json adds to it does
not depend on any other file, so `contribution` works that out for one file's JSON values (the caller caches the result
against the file's stat) and `lines` combines the contributions of every record. The text is the jq program's, and
tests/test_tp_window.py compares the two on the same files.

Where jq would stop with an error (a value it cannot index or add), Refused is raised and the caller shows no summary,
as the old `2>/dev/null` did.
"""
import math

from tp_rows import Refused, isnum, num

ZERO = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
KEYS = ("input", "output", "cacheRead", "cacheWrite")


def number(x):
    """jq `num`: a number stays, anything else is 0."""
    return x if isnum(x) else 0


def tk(o):
    """The four token counts of an object (null counts as empty); any other type is an error in jq."""
    if o is None:
        return dict(ZERO)
    if not isinstance(o, dict):
        raise Refused
    return {key: number(o.get(key)) for key in KEYS}


def plus(a, b):
    return {key: a[key] + b[key] for key in KEYS}


def total(parts):
    out = dict(ZERO)
    for part in parts:
        out = plus(out, part)
    return out


def minus0(a, b):
    return {key: max(a[key] - b[key], 0) for key in KEYS}


def empty4(t):
    return t["input"] + t["output"] + t["cacheRead"] + t["cacheWrite"] == 0


def k(x):
    if x >= 1000000:
        return num(math.floor(x / 100000) / 10) + "M"
    if x >= 1000:
        return num(math.floor(x / 100) / 10) + "k"
    return num(x)


def rounded(x):
    """jq `round`: half away from zero."""
    f = math.floor(abs(x))
    f = f + 1 if abs(x) - f >= 0.5 else f
    return -f if x < 0 else f


def counts(t):
    return "  in %s  cache-read %s  cache-write %s  out %s" % (k(t["input"]), k(t["cacheRead"]), k(t["cacheWrite"]), k(t["output"]))


def costtxt(costs):
    """The cost column for a list of costs (a number, or None when unknown)."""
    known = [c for c in costs if c is not None]
    amount = 0
    for c in known:
        amount = amount + c
    unknown = len(known) < len(costs)
    shown = num(rounded(amount * 1000) / 1000)
    if unknown:
        return "$" + shown + "+?" if amount > 0 else "$?"
    return "$" + shown


def contribution(values):
    """What the JSON values of one state.json add to the summary: a list with one item per value that carries tokens,
    {"ents": [...], "zero": bool}. Raises Refused where jq would fail."""
    out = []
    for s in values:
        if not isinstance(s, dict):
            continue
        tokens, by_model = s.get("tokens"), s.get("byModel")
        if not (isinstance(tokens, dict) or isinstance(by_model, dict)):
            continue
        unknown = s.get("harness") == "claude" or s.get("costKnown") is False
        models = by_model if isinstance(by_model, dict) else {}
        attributed = []
        for model, value in models.items():
            t = tk(value)
            cost = None
            if not unknown and isinstance(value, dict) and isnum(value.get("cost")):
                cost = value["cost"]
            attributed.append({"model": model, "t": t, "cost": cost})
        whole = tk(tokens) if isinstance(tokens, dict) else dict(ZERO)
        rest = minus0(whole, total(a["t"] for a in attributed))
        spent = 0
        for a in attributed:
            spent = spent + (a["cost"] or 0)
        rest_cost = max(number(s.get("cost")) - spent, 0)
        ents = list(attributed)
        if not (empty4(rest) and attributed):
            last = s.get("model")
            last = "" if last is None or last is False else last
            if not isinstance(last, str):
                raise Refused
            ents.append({"model": None, "t": rest if attributed else whole, "cost": None if unknown else rest_cost,
                         "last": last, "partial": bool(attributed)})
        if isinstance(tokens, dict):
            zero = empty4(whole)
        else:
            zero = empty4(total(tk(v) for v in models.values()))
        out.append({"ents": ents, "zero": zero})
    return out


def lines(records, workers_with_meta):
    """The summary lines. records is [(is_main, contribution item), ...] over every state.json value, in file order;
    workers_with_meta is how many task directories have a meta.json."""
    ents = [e for _, item in records for e in item["ents"]]
    by_model = {}
    for e in ents:
        if e["model"] is not None:
            by_model.setdefault(e["model"], []).append(e)
    leg = [e for e in ents if e["model"] is None and not empty4(e["t"])]
    out = ["session tokens (per-message by model; cache separate)"]
    for model in sorted(by_model):
        group = by_model[model]
        out.append(model)
        out.append("%s  %s" % (counts(total(e["t"] for e in group)), costtxt([e["cost"] for e in group])))
    if leg:
        out.append("unattributed (legacy/partial; model switches not ruled out)")
        out.append("%s  %s" % (counts(total(e["t"] for e in leg)), costtxt([e["cost"] for e in leg])))
        lasts = sorted(set(e["last"] for e in leg if e["last"] != ""))
        if lasts:
            out.append("  last reported model: %s (not an attribution)" % ", ".join(lasts))
    everything = [e["t"] for group in by_model.values() for e in group] + [e["t"] for e in leg]
    out.append("total" + counts(total(everything)))
    workers = sum(1 for is_main, _ in records if not is_main)
    has_main = 1 if any(is_main for is_main, _ in records) else 0
    zero = sum(1 for _, item in records if item["zero"])
    out.append("records %d (main %d, workers %d); zero-token %d; workers with no usage data %d" % (
        len(records), has_main, workers, zero, max(workers_with_meta - workers, 0)))
    return out
