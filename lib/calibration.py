"""Train and measure the learned layer over Laya's answers against a labeled task corpus.

Training needs numpy; routing (lib/router.py) does not.
"""

import json
import re
from pathlib import Path

from router import (QUESTIONS, REVIEWS, ROUTES, RouterError, feature_names, features, laya_answers,
                    laya_intent, laya_route, model_digest, PLACEMENTS, placement_features, placement_policy, questions_digest, review_policy)

INTENTS = tuple(QUESTIONS["task_intent"]["criteria"])
# escalated is a labeling category (sensitive or risky work); it shares expert's target.
LABEL_ROUTES = tuple(sorted(ROUTES | {"escalated"}))
SPLITS = ("tune", "holdout")
FIELDS = {"id", "task", "intent", "route", "review", "split", "placement"}
LAMBDAS = (0.001, 0.01, 0.03, 0.1, 0.3, 1.0)
# Chosen by repeated 5-fold CV on the tune split (tests/calibration/experiments.md, cycle 13):
# the gate threshold with the best review-or-not accuracy and no required review skipped,
# and the highest level threshold with no required review downgraded to optional.
GATE_THRESHOLD = 0.5
LEVEL_THRESHOLD = 0.1
# Cycle 18: the placement threshold with the best CV objective on tune (accuracy minus twice the
# rate of delegable work kept in main); 0.2 ties 0.35 and keeps less delegable work in main.
PLACEMENT_THRESHOLD = 0.2


def route_label(route):
    return "expert" if route == "escalated" else route


def load_corpus(path):
    rows, seen = [], set()
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = "{}:{}".format(path, number)
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise RouterError("{}: invalid JSON: {}".format(where, exc)) from exc
        if not isinstance(row, dict) or set(row) - {"note"} != FIELDS:
            raise RouterError("{}: needs exactly {} (and optional note)".format(where, ", ".join(sorted(FIELDS))))
        if not isinstance(row["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", row["id"]):
            raise RouterError("{}: id must be lowercase letters, digits and dashes".format(where))
        if row["id"] in seen:
            raise RouterError("{}: duplicate id {}".format(where, row["id"]))
        seen.add(row["id"])
        if not isinstance(row["task"], str) or not row["task"].strip():
            raise RouterError("{}: task must contain text".format(where))
        for field, allowed in (("intent", INTENTS), ("route", LABEL_ROUTES), ("review", REVIEWS),
                               ("split", SPLITS), ("placement", PLACEMENTS)):
            if row[field] not in allowed:
                raise RouterError("{}: {} must be one of {}".format(where, field, ", ".join(allowed)))
        rows.append(row)
    return rows


def load_answers(path):
    """Recorded Laya replies by corpus id: {id: {"task", "questions", "reply"}}."""
    path = Path(path)
    if not path.exists():
        return {}
    answers = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            answers[item["id"]] = {"task": item["task"], "questions": item.get("questions"), "reply": item["reply"]}
    return answers


def save_answers(path, corpus, answers):
    with open(path, "w", encoding="utf-8") as f:
        for row in corpus:
            if row["id"] in answers:
                item = answers[row["id"]]
                f.write(json.dumps({"id": row["id"], "task": item["task"], "questions": item["questions"],
                                    "reply": item["reply"]}, sort_keys=True) + "\n")


def is_current(row, answers):
    item = answers.get(row["id"])
    return item is not None and item["task"] == row["task"] and item["questions"] == questions_digest()


def _matrix(corpus, answers, split):
    rows = [row for row in corpus if row["split"] == split]
    return rows, [features(laya_answers(answers[row["id"]]["reply"])) for row in rows]


def _fit_head(X, y, classes, lam, iterations=4000, rate=0.1):
    """Softmax regression with L2 on standardized inputs, by full-batch gradient descent."""
    import numpy as np
    mean, scale = X.mean(0), X.std(0) + 1e-6
    Z = (X - mean) / scale
    n, width = Z.shape
    W, b, Y = np.zeros((width, classes)), np.zeros(classes), np.eye(classes)[y]
    with np.errstate(all="ignore"):  # numpy 2 on macOS Accelerate raises spurious matmul warnings
        for _ in range(iterations):
            logits = Z @ W + b
            P = np.exp(logits - logits.max(1, keepdims=True))
            P /= P.sum(1, keepdims=True)
            G = (P - Y) / n
            W -= rate * (Z.T @ G + lam * W)
            b -= rate * G.sum(0)
    if not np.isfinite(W).all():
        raise RouterError("training diverged")
    return mean, scale, W, b


def _choose_lambda(X, y, classes, folds=5, seed=0):
    """Regularization by k-fold cross-validation on the training rows only."""
    import numpy as np
    order = np.random.default_rng(seed).permutation(len(y))
    best = None
    for lam in LAMBDAS:
        correct = 0
        for fold in range(folds):
            test = order[fold::folds]
            train = np.setdiff1d(order, test)
            mean, scale, W, b = _fit_head(X[train], y[train], classes, lam)
            with np.errstate(all="ignore"):
                correct += (((X[test] - mean) / scale @ W + b).argmax(1) == y[test]).sum()
        if best is None or correct > best[0]:
            best = (correct, lam)
    return best[1]


def fit(corpus, answers):
    """Train the route, intent, gate and level heads on the tune split; return the model to save."""
    import numpy as np
    rows, X = _matrix(corpus, answers, "tune")
    X = np.array(X, dtype=float)
    P = np.array([placement_features(laya_answers(answers[r["id"]]["reply"])) for r in rows], dtype=float)
    model = {"questions": model_digest(), "features": feature_names(), "trained_on": len(rows)}
    routes = [route for route in sorted(ROUTES) if any(route_label(r["route"]) == route for r in rows)]
    reviewed = np.array([r["review"] != "none" for r in rows])
    heads = (
        ("route", routes, X, np.array([routes.index(route_label(r["route"])) for r in rows]), None),
        ("intent", list(INTENTS), X, np.array([INTENTS.index(r["intent"]) for r in rows]), None),
        ("gate", ["none", "review"], X, reviewed.astype(int), GATE_THRESHOLD),
        ("level", ["optional", "required"], X[reviewed],
         np.array([r["review"] == "required" for r in rows])[reviewed].astype(int), LEVEL_THRESHOLD),
        ("placement", list(PLACEMENTS), P, np.array([r["placement"] == "subagent" for r in rows]).astype(int),
         PLACEMENT_THRESHOLD),
    )
    for name, labels, data, y, threshold in heads:
        lam = _choose_lambda(data, y, len(labels))
        mean, scale, W, b = _fit_head(data, y, len(labels), lam)
        model[name] = {"labels": labels, "lambda": lam, "mean": mean.round(6).tolist(),
                       "scale": scale.round(6).tolist(), "weights": W.round(6).tolist(), "bias": b.round(6).tolist()}
        if threshold is not None:
            model[name]["threshold"] = threshold
    return model


def evaluate(corpus, answers, model):
    """One result per corpus row with a current recorded answer, and the ids without one."""
    results, missing = [], []
    for row in corpus:
        if not is_current(row, answers):
            missing.append(row["id"])
            continue
        reply = answers[row["id"]]["reply"]
        got = {"intent": laya_intent(reply, model), "route": laya_route(reply, model),
               "review": review_policy(reply, model)}
        got["placement"] = placement_policy(reply, model, got["intent"], got["review"])
        expected = {"intent": row["intent"], "route": route_label(row["route"]), "review": row["review"],
                    "placement": row["placement"]}
        results.append({"id": row["id"], "split": row["split"], "expected": expected, "got": got})
    return results, missing


def summary(results):
    n = len(results)
    if not n:
        return {}
    out = {dim: sum(r["got"][dim] == r["expected"][dim] for r in results) / n
           for dim in ("intent", "route", "review", "placement")}
    out["kept_in_main"] = sum(r["expected"]["placement"] == "subagent" and r["got"]["placement"] == "main" for r in results)
    out["delegated_needlessly"] = sum(r["expected"]["placement"] == "main" and r["got"]["placement"] == "subagent"
                                      for r in results)
    level = lambda r, side: REVIEWS.index(r[side]["review"])
    out["required_skipped"] = sum(r["expected"]["review"] == "required" and r["got"]["review"] == "none" for r in results)
    out["optional_skipped"] = sum(r["expected"]["review"] == "optional" and r["got"]["review"] == "none" for r in results)
    out["required_downgraded"] = sum(r["expected"]["review"] == "required" and r["got"]["review"] == "optional"
                                     for r in results)
    out["unneeded_reviews"] = sum(r["expected"]["review"] == "none" and r["got"]["review"] != "none" for r in results)
    out["under"] = sum(level(r, "got") < level(r, "expected") for r in results)
    out["gate"] = sum((r["expected"]["review"] == "none") == (r["got"]["review"] == "none") for r in results) / n
    out["n"] = n
    return out


def report(results, missing, show_mismatches=False):
    lines = []
    for split in SPLITS:
        s = summary([r for r in results if r["split"] == split])
        if not s:
            continue
        lines.append("{} ({} tasks): intent {:.2f}  route {:.2f}  review {:.2f}  review-or-not {:.2f}".format(
            split, s["n"], s["intent"], s["route"], s["review"], s["gate"]))
        lines.append("  reviews skipped: {} required, {} optional; required→optional {}; unneeded reviews {}".format(
            s["required_skipped"], s["optional_skipped"], s["required_downgraded"], s["unneeded_reviews"]))
        lines.append("  placement {:.2f}: kept in main but should delegate {}; delegated needlessly {}".format(
            s["placement"], s["kept_in_main"], s["delegated_needlessly"]))
    if show_mismatches:
        lines.append("")
        for r in results:
            diffs = ["{} {}->{}".format(d, r["expected"][d], r["got"][d])
                     for d in ("intent", "route", "review", "placement") if r["got"][d] != r["expected"][d]]
            if diffs:
                lines.append("  {} [{}]: {}".format(r["id"], r["split"], "; ".join(diffs)))
    if missing:
        lines.append("")
        lines.append("no current Laya answer (run --record): {}".format(", ".join(missing)))
    return "\n".join(lines)
