"""tp-bench compare A B: per-metric means, the spread of each arm's repeats, and the keep/discard/inconclusive verdict
by the rule in docs/plans/det-prep/loop/PROTOCOL.md (A is the control, B the candidate)."""
from .record import CORRECTNESS, METRICS

ARM_GATES = ("TP_X1_ARM", "TP_HL1")  # env keys allowed to differ between the two arms
TOKEN_DROP = 0.10
ORIENT_DROP = 0.25
FP_RISE = 1.0  # per clean diff


class CompareError(RuntimeError):
    pass


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def spread(values):
    values = [v for v in values if v is not None]
    return max(values) - min(values) if values else None


def column(records, name):
    return [r["metrics"].get(name) for r in records]


def check(a, b, allow_env_diff=False):
    """Refuse records that are not comparable: a different task set, or an env that differs beyond the arm gates."""
    hashes = {r["task_set_hash"] for r in a + b}
    if len(hashes) > 1:
        raise CompareError("the records ran different task sets (task_set_hash differs); re-run one arm on the same tasks")
    if allow_env_diff:
        return
    envs = [r.get("env") or {} for r in a + b]
    keys = sorted({k for e in envs for k in e if k not in ARM_GATES and len({str(x.get(k)) for x in envs}) > 1})
    if keys:
        raise CompareError("env differs in " + ", ".join(keys) + " (only " + ", ".join(ARM_GATES)
                           + " may differ); pass --allow-env-diff to compare anyway")


def flipped(a, b):
    """Task ids that passed in every control repeat and failed in every candidate repeat."""
    def statuses(records):
        out = {}
        for r in records:
            for t in r["tasks"]:
                out.setdefault(t["id"], []).append(t["status"])
        return out
    sa, sb = statuses(a), statuses(b)
    return sorted(i for i in sa if i in sb and all(s == "pass" for s in sa[i]) and all(s != "pass" for s in sb[i]))


def verdict(a, b):
    """(word, reason). Correctness first, then tokens, then orient waste; review_fp is a discard."""
    stats = {m: (mean(column(a, m)), mean(column(b, m)), spread(column(a, m))) for m in METRICS}
    for m in CORRECTNESS:
        ma, mb, sp = stats[m]
        if ma is not None and mb is not None and mb < ma - (sp or 0) - 1e-9:
            return "discard", f"{m} dropped from {ma:.3f} to {mb:.3f}, more than the control spread {sp or 0:.3f}"
    lost = flipped(a, b)
    if lost:
        return "discard", "task(s) passed in every control repeat and failed in every candidate repeat: " + ", ".join(lost)
    fa, fb, _ = stats["review_fp"]
    if fa is not None and fb is not None and fb - fa > FP_RISE + 1e-9:
        return "discard", f"review_fp rose from {fa:.2f} to {fb:.2f} per clean diff (more than {FP_RISE:g})"
    rose, equal = [], True
    for m in CORRECTNESS:
        ma, mb, sp = stats[m]
        if ma is None or mb is None:
            continue
        if mb > ma + (sp or 0) + 1e-9:
            rose.append(m)
        if abs(mb - ma) > (sp or 0) + 1e-9:
            equal = False
    if rose:
        return "keep", "correctness rose beyond the control spread: " + ", ".join(rose)
    if equal:
        ta, tb, _ = stats["tokens_per_accepted"]
        if ta and tb is not None and (ta - tb) / ta >= TOKEN_DROP - 1e-9:
            return "keep", f"correctness equal and tokens per accepted task dropped {100 * (ta - tb) / ta:.0f}%"
        oa, ob, _ = stats["orient_waste"]
        if oa and ob is not None and (oa - ob) / oa >= ORIENT_DROP - 1e-9:
            return "keep", f"correctness and tokens equal and orient_waste dropped {100 * (oa - ob) / oa:.0f}%"
    return "inconclusive", "no correctness gain beyond the spread and no token or orient_waste drop large enough"


def fmt(value, name):
    if value is None:
        return "-"
    return f"{value:.1f}" if name in ("tokens_per_accepted", "wall_s", "orient_waste") else f"{value:.3f}"


def render(a, b, label_a, label_b, allow_env_diff=False):
    check(a, b, allow_env_diff)
    rows = [("metric", f"{label_a} mean", f"{label_a} spread", f"{label_b} mean", f"{label_b} spread")]
    for m in METRICS:
        rows.append((m, fmt(mean(column(a, m)), m), fmt(spread(column(a, m)), m), fmt(mean(column(b, m)), m), fmt(spread(column(b, m)), m)))
    widths = [max(len(r[i]) for r in rows) for i in range(5)]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    word, why = verdict(a, b)
    lines.append(f"repeats: {label_a}={len(a)} {label_b}={len(b)}")
    lines.append(f"verdict: {word} ({why})")
    return "\n".join(lines), word
