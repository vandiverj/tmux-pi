"""Experimental routing evaluation log: per-selection records, human outcomes and a joined export.

Every `tp-route --dispatch` (and plain `tp-route`) appends one bounded selection record to
the session's project-local state dir, `<.tmux-pi>/evals/routing-selections.jsonl`:
sanitized request traits and hashes (no raw request text unless TP_ROUTING_EVALS_TEXT=1),
Laya's bounded answer scores, the learned heads' probabilities, version hashes of the
questions, config, heads and code, and the recommended route/model/effort before and after
the Claude preference and local rules. Humans add ratings and corrections with
`tp-evals rate` (routing-outcomes.jsonl). `tp-evals export` joins each selection to the
tasks it started (task meta, state, review, escalation, tokens, cost, duration) and to
human outcomes.

The log is evidence only. It never grants dispatch, edit or review authority, and a failed
append never changes a routing decision: callers catch EvalLogError and carry on.
Labels are conservative: acceptance is "unknown" unless a human recorded it, oversizing is
"unknown" unless a human judged it (it needs a comparison run), and escalation is a
model/tool-failure signal, not a correctness label.
"""

import datetime
import errno
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import review_verdict
import router

SELECTION_SCHEMA = "tmux-pi-routing-selection:v1"
OUTCOME_SCHEMA = "tmux-pi-routing-outcome:v1"
EXPORT_SCHEMA = "tmux-pi-routing-eval-row:v1"
SELECTIONS = "routing-selections.jsonl"
OUTCOMES = "routing-outcomes.jsonl"
MAX_RECORD_BYTES = 16 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
MAX_TEXT = 2000
MAX_NOTE = 1000
LOCK_SECONDS = 2.0
SELECTION_ID = re.compile(r"^sel-[0-9a-f]{32}$")
TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
RATINGS = ("good", "acceptable", "poor", "wrong")
ACCEPTANCE = ("accepted", "accepted_with_changes", "rejected", "unknown")
SIZING = ("right_sized", "oversized", "undersized", "unknown")
MAIN_LIMITATION = ("main keeps its session model: R5 does not switch the main model in place, so a "
                   "main-placed pi route is a recommendation only (docs/routing-evals.md)")
ESCALATION_NOTE = "escalation signals a model/tool failure; it is not by itself a correctness label"


class EvalLogError(Exception):
    pass


def sha256_text(text):
    """Hex SHA-256 of UTF-8 text; the same digest the dispatcher records as request_sha256."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_digest(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_selection_id():
    return "sel-" + uuid.uuid4().hex


def _round(value):
    return round(float(value), 4)


def request_traits(raw, task, overrides):
    """Sanitized, bounded traits of a request: hashes and shape, never the text itself."""
    return {
        "sha256": sha256_text(raw),
        "task_sha256": sha256_text(task),
        "chars": len(task),
        "words": len(task.split()),
        "lines": task.count("\n") + 1 if task else 0,
        "markers": sorted(overrides),
        "has_code_fence": "```" in task,
        "has_url": bool(re.search(r"https?://", task)),
        "has_path": bool(re.search(r"(?:^|\s)[\w.~-]*/[\w./-]+", task)),
    }


def classifier_scores(reply):
    """Laya's answers, bounded to validated numbers and choices (rounded)."""
    answers = router.laya_answers(reply)
    out = {}
    for name, question in router.QUESTIONS.items():
        answer, field = answers[name], question["type"]
        item = {field: answer[field] if field == "choice" else _round(answer[field])}
        if "confidence" in answer:
            item["confidence"] = _round(answer["confidence"])
        if field != "noul":
            item["probabilities"] = {k: _round(v) for k, v in answer["probabilities"].items()}
        out[name] = item
    return out


def head_scores(reply, model):
    """The learned layer's probabilities for route, intent, review gate/level and placement."""
    answers = router.laya_answers(reply)
    values = router.features(answers)
    probs = lambda name, vals: dict(zip(model[name]["labels"], (_round(p) for p in router._probabilities(model[name], vals))))
    return {
        "route": probs("route", values),
        "intent": probs("intent", values),
        "gate_review": probs("gate", values)["review"],
        "level_required": probs("level", values)["required"],
        "placement_subagent": probs("placement", router.placement_features(answers))["subagent"],
    }


def versions(config, config_path):
    """Hashes that pin which questions, heads, config and code produced a selection."""
    config_path = Path(config_path)
    local_path = os.environ.get("TP_LOCAL_CONFIG") or config_path.parent / "local.json"
    model = config.get("model") or {}
    return {
        "questions_sha256": router.questions_digest(),
        "feature_questions_sha256": router.model_digest(),
        "classifier_request_sha256": _json_digest({"model": config["laya"]["model"], "questions": router.QUESTIONS}),
        "router_config_sha256": _file_digest(config_path),
        "local_config_sha256": _file_digest(local_path) if config.get("local") else None,
        "laya_model_sha256": _file_digest(config["model_path"]) if config.get("model_path") else None,
        "heads_sha256": {name: _json_digest(model[name]) for name in ("route", "intent", "gate", "level", "placement")
                         if name in model},
        "claude_preference_sha256": _json_digest(config["claude_preference"]) if config.get("claude_preference") else None,
        "router_code_sha256": _file_digest(Path(router.__file__)),
        "laya_service_model": config["laya"]["model"],
    }


def _target(value):
    return {k: value[k] for k in ("worker", "model", "thinking")} if isinstance(value, dict) else None


def selection_record(raw, config, config_path, kind="dispatch", backend="laya", decision=None, trace=None,
                     error=None, selection_id=None, now=None, include_text=None):
    """One selection record; decision is the router output (None when routing failed)."""
    overrides, task = router.parse_markers(raw)
    if kind != "dispatch":
        overrides, task = {}, raw
    if include_text is None:
        include_text = os.environ.get("TP_ROUTING_EVALS_TEXT") == "1"
    trace = trace or {}
    record = {
        "schema": SELECTION_SCHEMA,
        "selection_id": selection_id or new_selection_id(),
        "created": now or _now(),
        "kind": kind,
        "experimental": True,
        "authority": "none",
        "status": "ok" if decision is not None else "router_error",
        "request": request_traits(raw, task, overrides),
        "versions": versions(config, config_path),
        "classifier": {"backend": backend},
    }
    if include_text:
        record["request"]["text"] = task[:MAX_TEXT]
    reply = trace.get("reply")
    if reply is not None:
        try:
            record["classifier"]["scores"] = classifier_scores(reply)
            if config.get("model"):
                record["classifier"]["heads"] = head_scores(reply, config["model"])
        except (router.RouterError, KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
            record["classifier"]["scores_error"] = type(exc).__name__
    if decision is not None:
        preference = trace.get("preference")
        record["recommendation"] = {
            "route": trace.get("route", decision.get("route")),
            "laya_target": _target(trace.get("laya_target")),
            "claude_preference": None if not preference else {
                "tier": preference["tier"], "reasons": preference["reasons"], "changed": preference["changed"]},
            "rule_applied": bool(trace.get("rule_applied")),
            "final": _target(decision),
            **{k: decision[k] for k in ("intent", "review", "scope", "placement", "overrides") if k in decision},
        }
        main_pi = kind == "dispatch" and decision.get("placement") == "main" and decision.get("worker") == "pi"
        record["actual"] = {
            "main_model_switched": False,
            "main_model": "unknown" if main_pi else None,
            "note": MAIN_LIMITATION if main_pi else "actual worker model/effort is joined from task meta at export",
        }
    if error is not None:
        record["error"] = {"type": type(error).__name__, "message": str(error)[:300]}
    # Keep every record bounded: drop the bulkiest optional parts first.
    for drop in (("request", "text"), ("classifier", "heads"), ("classifier", "scores")):
        if len(json.dumps(record, sort_keys=True)) <= MAX_RECORD_BYTES:
            break
        record[drop[0]].pop(drop[1], None)
        record.setdefault("truncated", []).append(".".join(drop))
    return record


def eval_dir(runner=subprocess.run):
    """The project-local eval dir, or None when there is no session state dir (logging off).

    $TP_ROUTING_EVALS=0 turns logging off; $TP_ROUTING_EVAL_DIR names the dir; otherwise it
    is <state dir>/evals, the state dir from $TP_STATE_DIR or the current tmux session.
    """
    if os.environ.get("TP_ROUTING_EVALS") == "0":
        return None
    if os.environ.get("TP_ROUTING_EVAL_DIR"):
        return Path(os.environ["TP_ROUTING_EVAL_DIR"])
    state = os.environ.get("TP_STATE_DIR")
    if not state and os.environ.get("TMUX"):
        try:
            state = runner(["tmux", "display-message", "-p", "#{@tp_state_dir}"], capture_output=True, text=True,
                           timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            state = ""
    return Path(state) / "evals" if state else None


def append_jsonl(directory, name, record):
    """Append one JSON line under an exclusive lock with a single O_APPEND write.

    Refuses symlinks, oversized records and a full log. Raises EvalLogError on any failure.
    """
    line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_RECORD_BYTES + 1:
        raise EvalLogError("record is larger than {} bytes".format(MAX_RECORD_BYTES))
    directory = Path(directory)
    try:
        if directory.is_symlink():
            raise EvalLogError("eval dir is a symlink: {}".format(directory))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_NOFOLLOW
        lock = os.open(directory / (name + ".lock"), flags | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + LOCK_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES) or time.monotonic() > deadline:
                        raise EvalLogError("eval log is locked") from exc
                    time.sleep(0.02)
            fd = os.open(directory / name, flags | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                if os.fstat(fd).st_size + len(line) > MAX_LOG_BYTES:
                    raise EvalLogError("eval log {} is full ({} bytes); export and rotate it".format(name, MAX_LOG_BYTES))
                written = os.write(fd, line)
                if written != len(line):
                    raise EvalLogError("short write to eval log")
            finally:
                os.close(fd)
        finally:
            os.close(lock)
    except EvalLogError:
        raise
    except OSError as exc:
        raise EvalLogError("cannot append to eval log: {}".format(exc)) from exc


def log_selection(record, directory=None):
    """Append a selection record; returns its path. Raises EvalLogError (never anything else)."""
    try:
        directory = directory if directory is not None else eval_dir()
    except Exception as exc:  # noqa: BLE001 - resolving the dir must never break routing
        raise EvalLogError("cannot resolve eval dir: {}".format(exc)) from exc
    if directory is None:
        raise EvalLogError("no tmux-pi state dir; selection not logged")
    append_jsonl(directory, SELECTIONS, record)
    return Path(directory) / SELECTIONS


def read_jsonl(path):
    """(rows, malformed count); a missing file is empty. A torn last line counts as malformed."""
    rows, bad = [], 0
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return rows, bad
    except (OSError, UnicodeDecodeError) as exc:
        raise EvalLogError("cannot read {}: {}".format(path, exc)) from exc
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            bad += 1
    return rows, bad


def outcome_record(config, selection_id=None, task_id=None, rating=None, acceptance=None, sizing=None,
                   correct_model=None, correct_thinking=None, note=None, rater="human", now=None):
    """A validated human outcome/correction for one selection or task."""
    if not selection_id and not task_id:
        raise EvalLogError("an outcome needs --selection or --task")
    if selection_id and not SELECTION_ID.match(selection_id):
        raise EvalLogError("selection id must look like sel-<32 hex>")
    if task_id and not TASK_ID.match(task_id):
        raise EvalLogError("task id has invalid characters")
    for value, allowed, label in ((rating, RATINGS, "rating"), (acceptance, ACCEPTANCE, "acceptance"),
                                  (sizing, SIZING, "sizing")):
        if value is not None and value not in allowed:
            raise EvalLogError("{} must be one of {}".format(label, ", ".join(allowed)))
    if rating is None and acceptance is None and sizing is None and correct_model is None and not note:
        raise EvalLogError("an outcome needs a rating, acceptance, sizing, correction or note")
    correction = None
    if correct_model is not None or correct_thinking is not None:
        if correct_model is None or correct_thinking is None:
            raise EvalLogError("a correction needs both --correct-model and --correct-thinking")
        try:
            correction = router.validate_decision({"model": correct_model, "thinking": correct_thinking}, config)
        except router.RouterError as exc:
            raise EvalLogError("correction: {}".format(exc)) from exc
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise EvalLogError("note must be text of at most {} characters".format(MAX_NOTE))
    if not isinstance(rater, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rater):
        raise EvalLogError("rater must be a short name")
    record = {"schema": OUTCOME_SCHEMA, "outcome_id": "out-" + uuid.uuid4().hex, "created": now or _now(),
              "rater": rater, "authority": "none"}
    for key, value in (("selection_id", selection_id), ("task_id", task_id), ("rating", rating),
                       ("acceptance", acceptance), ("sizing", sizing), ("correction", correction), ("note", note)):
        if value is not None:
            record[key] = value
    return record


def _read_json(path):
    try:
        if Path(path).is_symlink():
            return None
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_line(path):
    try:
        if Path(path).is_symlink():
            return None
        text = Path(path).read_text(encoding="utf-8").strip()
        return text.splitlines()[0][:200] if text else None
    except (OSError, UnicodeDecodeError):
        return None


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _task_dirs(state_dir):
    root = Path(state_dir)
    for base in (root / "tasks", root / "archive"):
        if base.is_dir() and not base.is_symlink():
            for d in sorted(base.iterdir()):
                if d.is_dir() and not d.is_symlink() and (d / "meta.json").is_file():
                    yield d


def measured_total(values, digits=None):
    """(sum of the values that were measured, how many were not): the sum is None when none was measured (unknown, not
    0), and a mixed selection reports the unmeasured count so the sum reads as partial."""
    known = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    total = sum(known) if known else None
    return (round(total, digits) if digits is not None and total is not None else total), len(values) - len(known)


def totals(tasks):
    if not tasks:
        return {"cost": None, "output_tokens": None}
    cost, cost_missing = measured_total([t.get("cost") for t in tasks], 6)
    output, output_missing = measured_total([(t.get("tokens") or {}).get("output") for t in tasks])
    return {"cost": cost, "cost_unknown_tasks": cost_missing, "cost_partial": bool(cost_missing and cost is not None),
            "output_tokens": output, "output_tokens_unknown_tasks": output_missing,
            "output_tokens_partial": bool(output_missing and output is not None)}


def task_summary(d, dirs_by_id):
    """Joinable, text-free facts about one task dir: actual model, state, review, escalation, usage."""
    meta = _read_json(d / "meta.json") or {}
    state = _read_json(d / "state.json") or {}
    exit_code = _read_line(d / "exit")
    created, updated = _parse_time(meta.get("created")), _parse_time(state.get("updated"))
    tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else None
    review = None
    if isinstance(meta.get("review"), str) and TASK_ID.match(meta["review"]):
        reviewer_dir = dirs_by_id.get(meta["review"])
        review = {"id": meta["review"], "reviewer": _target(meta.get("reviewer")), "verdict": None}
        if reviewer_dir is not None:
            # review_verdict.assess decides on the whole reply; anything it refuses is "unparsed", a review without a usable
            # Coverage section is "incomplete" and is never counted as the verdict it states; a REQUEST_CHANGES with no
            # Closes by: code finding is "UNUSABLE", no rejection and no approval
            try:
                text = review_verdict.read_review(reviewer_dir / "result.md")
                stated, _, incomplete = review_verdict.assess(text)
                review["verdict"] = stated
                if incomplete:
                    review.update({"verdict": "incomplete", "stated_verdict": stated, "incomplete_reason": incomplete})
                else:
                    unusable = review_verdict.request_changes_problem(text)
                    if unusable:
                        review.update({"verdict": "UNUSABLE", "stated_verdict": stated, "unusable_reason": unusable})
            except review_verdict.ReviewMissing:
                pass
            except ValueError as exc:
                review.update({"verdict": "unparsed", "incomplete_reason": str(exc)})
    marker = _read_json(d / "review-required.json")
    escalated_to = meta.get("escalated_to") if isinstance(meta.get("escalated_to"), dict) else None
    return {
        "task_id": meta.get("id") or d.name,
        "location": d.parent.name,
        "actual": _target(meta) if meta.get("model") else None,
        "route": (meta.get("decision") or {}).get("route") if isinstance(meta.get("decision"), dict) else None,
        "handshake_id": meta.get("handshake_id"),
        "state": state.get("state"),
        "exit": int(exit_code) if exit_code and re.fullmatch(r"-?\d+", exit_code) else None,
        "turns": state.get("turns"),
        "tool_errors": state.get("toolErrors"),
        "tokens": {k: tokens.get(k) for k in ("input", "output", "cacheRead", "cacheWrite")} if tokens else None,
        "cost": state.get("cost") if isinstance(state.get("cost"), (int, float)) and state.get("costKnown") is not False else None,  # a harness that reports none (Claude) has no cost, not 0
        "duration_seconds": int((updated - created).total_seconds()) if created and updated and updated >= created else None,
        "review": review,
        "review_required": {"status": marker.get("status")} if marker else None,
        "escalated_to": {"id": escalated_to.get("id"), "model": escalated_to.get("model")} if escalated_to else None,
        "escalated_from": (meta.get("escalated_from") or {}).get("id") if isinstance(meta.get("escalated_from"), dict) else None,
        "refs": sorted(name for name in ("result.md", "escalation.md", "review-required.json", "main-contract.json",
                                          "policy-decision.json") if (d / name).is_file()),
    }


def export_rows(directory, state_dir):
    """One eval row per selection, joined to its tasks, handoff and human outcomes."""
    selections, bad_selections = read_jsonl(Path(directory) / SELECTIONS)
    outcomes, bad_outcomes = read_jsonl(Path(directory) / OUTCOMES)
    dirs = list(_task_dirs(state_dir)) if state_dir else []
    dirs_by_id = {d.name: d for d in dirs}
    metas = {d: _read_json(d / "meta.json") or {} for d in dirs}
    by_selection, by_task_hash = {}, {}
    for d, meta in metas.items():
        decision = meta.get("decision") if isinstance(meta.get("decision"), dict) else {}
        sid = decision.get("selection_id")
        if isinstance(sid, str) and SELECTION_ID.match(sid):
            by_selection.setdefault(sid, []).append(d)
        elif isinstance(decision.get("task"), str):
            by_task_hash.setdefault(sha256_text(decision["task"]), []).append(d)
    hash_counts = {}
    for sel in selections:
        h = (sel.get("request") or {}).get("task_sha256")
        hash_counts[h] = hash_counts.get(h, 0) + 1
    handoffs = {}
    if state_dir and (Path(state_dir) / "handoffs").is_dir():
        for scaffold in sorted((Path(state_dir) / "handoffs").glob("*/scaffold.json")):
            data = _read_json(scaffold) or {}
            sid = (data.get("decision") or {}).get("selection_id") if isinstance(data.get("decision"), dict) else None
            if isinstance(sid, str):
                handoffs.setdefault(sid, []).append(data.get("handshake_id"))
    rows = []
    for sel in selections:
        sid = sel.get("selection_id")
        task_hash = (sel.get("request") or {}).get("task_sha256")
        linked, link = by_selection.get(sid, []), "selection_id"
        if not linked and task_hash in by_task_hash:
            # A task that predates selection ids: join by the task text hash only when unambiguous.
            linked, link = (by_task_hash[task_hash], "task_sha256") if hash_counts.get(task_hash) == 1 else ([], "ambiguous_task_sha256")
        tasks = [task_summary(d, dirs_by_id) for d in linked]
        # Follow escalation one hop so the doing worker's usage is in the row.
        for t in list(tasks):
            target = (t.get("escalated_to") or {}).get("id")
            if target and target in dirs_by_id and all(x["task_id"] != target for x in tasks):
                tasks.append(task_summary(dirs_by_id[target], dirs_by_id))
        task_ids = {t["task_id"] for t in tasks}
        human = [o for o in outcomes if o.get("selection_id") == sid or (o.get("task_id") and o.get("task_id") in task_ids)]
        latest = lambda key: next((o[key] for o in reversed(human) if key in o), None)
        final = (sel.get("recommendation") or {}).get("final")
        first = tasks[0]["actual"] if tasks else None
        verdicts = [t["review"]["verdict"] for t in tasks if t.get("review") and t["review"].get("verdict")]
        rows.append({
            "schema": EXPORT_SCHEMA,
            "selection_id": sid,
            "created": sel.get("created"),
            "status": sel.get("status"),
            "request": {k: v for k, v in (sel.get("request") or {}).items() if k != "text"},
            "versions": sel.get("versions"),
            "classifier": sel.get("classifier"),
            "recommendation": sel.get("recommendation"),
            "link": link if tasks else None,
            "handshakes": handoffs.get(sid, []),
            "tasks": tasks,
            "actual": {
                "main_model_switched": False,
                "worker": first,
                "matches_recommendation": (first == final) if first and final else None,
                "overridden": bool(first and final and first != final),
                "escalated": any(t.get("escalated_to") for t in tasks),
            },
            "totals": totals(tasks),
            "labels": {
                "acceptance": latest("acceptance") or "unknown",
                "acceptance_source": "human" if latest("acceptance") else None,
                "review_verdicts": verdicts,
                "rating": latest("rating"),
                "sizing": latest("sizing") or "unknown",
                "sizing_note": None if latest("sizing") else "oversizing needs a comparison run or a human judgment",
                "correction": latest("correction"),
                "escalation_note": ESCALATION_NOTE,
            },
            "outcomes": [o.get("outcome_id") for o in human],
        })
    return rows, {"selections": len(selections), "malformed_selections": bad_selections,
                  "outcomes": len(outcomes), "malformed_outcomes": bad_outcomes,
                  "linked": sum(1 for r in rows if r["tasks"])}
