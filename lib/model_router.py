"""Model selection for dispatch, `tp-route` and reviews: the catalog router (lib/llm_router.py) proposes a level and
an effort from a fixed-form card, and lib/route_resolve.py turns that, or an explicit choice, into a worker and model
that are always a valid pair. Every selection, explicit ones included, is written to the session's evals directory.

Nothing here calls Laya or reads the learned head. Machine-local route rules from config/local.json keep working
through lib/router.py's loader (apply_rules after the choice).
"""

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_router  # noqa: E402
import route_card  # noqa: E402
import route_resolve as rr  # noqa: E402
import router  # noqa: E402
import router_catalog as rc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SELECTIONS = "model-selections.jsonl"
SCHEMA = "tmux-pi-model-selection:v1"
MAX_TEXT = 1200


class RouteStop(ValueError):
    """The router cannot choose without the user: nothing suitable is available or allowed."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_roster_config(root=ROOT, config_path=None):
    """(config, error): config/router.json with this machine's local overrides, without the learned head."""
    try:
        return router.load_config(config_path or Path(root) / "config" / "router.json", laya_model=False), None
    except (router.RouterError, OSError) as exc:
        return None, str(exc)[:200]


def read_health(base):
    """Provider health from <state>/provider-health.json: {"<provider>": {"status": "exhausted"}, "<provider>:<model>": {...}}. Absent means ok."""
    data = _read_json(Path(base) / "provider-health.json") if base else {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def estimate_tokens(task, deliverable="", context=(), workdir=None):
    """Rough packet size in tokens: the text plus each named context file (tp-spawn caps a file at 60000 characters)."""
    chars = len(task) + len(deliverable or "")
    base = Path(workdir) if workdir else Path.cwd()
    for spec in context or ():
        raw = spec.rsplit(":", 1)[0] if spec.rsplit(":", 1)[-1].replace("-", "").isdigit() else spec
        path = Path(raw) if os.path.isabs(raw) else base / raw
        try:
            chars += min(path.stat().st_size, 60000) if path.is_file() else 0
        except OSError:
            pass
    return chars // 4


def explicit_pair(cat, worker, model):
    """Validate an explicit worker/model pair as `tp-agent dispatch --worker/--model` always did; return the worker."""
    known = rc.level_of(cat, model)[1] if cat else None
    if known and worker and worker != known["worker"]:
        raise ValueError("model {} belongs to {}, not {}".format(model, known["worker"], worker))
    if not known and worker and (("/" in model) != (worker == "pi")):
        raise ValueError("model {} is not compatible with {}".format(model, worker))
    return worker or (known["worker"] if known else "pi" if "/" in model else "claude")


def _raw_catalogs(catalog_path):
    """The catalog files that might say what a role's floor is, read leniently (the selected one failed validation, so
    nothing in it is trusted beyond what is checked below): the selected file, then the shipped one. Unreadable ones are skipped."""
    found = []
    for path in dict.fromkeys(str(p) for p in (catalog_path or os.environ.get("TP_MODEL_CATALOG") or rc.CATALOG_PATH, rc.CATALOG_PATH)):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def _at_or_above_floor(raw, role, model):
    """True when the raw catalog puts `model` in a level at or above the role's min_level; False on anything malformed."""
    try:
        keys = [level["key"] for level in raw["levels"]]
        floor = keys.index(raw["roles"][role]["min_level"])
        return any(entry["model"] == model for level in raw["levels"][floor:] for entry in level["models"])
    except (KeyError, IndexError, TypeError, ValueError):
        return False


def _emergency(config, worker, role=None, catalog_path=None):
    """Last resort when the catalog cannot be read: the legacy router config's ordinary pair.

    A model is returned only for a role that a leniently read catalog file names. A role with a strict floor, or with any min_level (the catalog is invalid, so a lost strict_floor flag is not trusted), gets the
    pair only when it is confirmed at or above the floor; it is never replaced by another model of the worker. When no
    readable catalog names the role (including an invalid shipped catalog that lost it) nothing is routed."""
    if not config:
        raise RouteStop("model catalog and router config are both unreadable; choose a model explicitly with --model")
    fallback = (config.get("claude_preference", {}).get("ordinary", {}) if worker == "claude"
                else config.get("routes", {}).get("coding", {}))
    entry = next((m for m in config["models"] if m["model"] == fallback.get("model")), None)
    fits = bool(entry) and not (worker and entry["worker"] != worker)
    raws = _raw_catalogs(catalog_path)
    if not raws:
        raise RouteStop("model catalog unreadable and no catalog file can say what role {} may run on, so it is not routed; "
                        "fix the catalog".format(role))
    named = [raw["roles"][role] for raw in raws if isinstance(raw.get("roles"), dict) and isinstance(raw["roles"].get(role), dict)]
    if not named:
        # Absence of the role in a catalog that failed validation is not evidence that it has no floor.
        raise RouteStop("model catalog unreadable and no catalog file confirms the floor of role {}, so it is not routed; "
                        "fix the catalog or use --model".format(role))
    strict = [raw for raw in raws if isinstance(raw.get("roles"), dict) and isinstance(raw["roles"].get(role), dict)
              and (raw["roles"][role].get("strict_floor") or "min_level" in raw["roles"][role])]
    if strict:
        if not fits or not all(_at_or_above_floor(raw, role, entry["model"]) for raw in strict):
            raise RouteStop("model catalog unreadable and the configured fallback ({}) cannot be confirmed at or above the floor "
                            "of role {} for worker {}, so the user must decide (use --model)".format(fallback.get("model"), role, worker))
    elif not fits:
        entry = next((m for m in config["models"] if not worker or m["worker"] == worker), None)
    if not entry:
        raise RouteStop("no model available for worker {}".format(worker))
    thinking = fallback.get("thinking") if fallback.get("thinking") in entry["thinking"] else entry["thinking"][0]
    return entry["worker"], entry["model"], thinking


def write_selection(record, base=None):
    """Append the selection record under <base>/evals (or the usual eval dir); the id, or None. Never raises."""
    try:
        import routing_evals
        directory = Path(base) / "evals" if base else routing_evals.eval_dir()
        if directory is None:
            return None
        routing_evals.append_jsonl(directory, SELECTIONS, record)
        return record["selection_id"]
    except Exception as exc:  # noqa: BLE001 - logging must never change a routing decision
        print("model-router: selection not logged: {}".format(exc), file=sys.stderr)
        return None


def _record(kind, req, cat, rec, card, chain, config_error, started):
    import routing_evals
    out = {
        "schema": SCHEMA, "selection_id": routing_evals.new_selection_id(), "created": _now(), "kind": kind,
        "role": req.get("role"), "task_sha256": sha(req.get("task", "")), "card_sha256": sha(card) if card else None,
        "catalog_version": cat["catalog_version"] if cat else None, "catalog_sha": rc.catalog_sha(cat) if cat else None,
        "prompt_sha256": chain.get("prompt_sha") if chain else None,
        "constraints": {k: req.get(k) for k in ("worker", "model", "thinking", "level") if req.get(k)}
        | {"local_only": bool(req.get("local_only")), "approve_effort": bool(req.get("approve_effort"))},
        "source": rec.get("source"), "level": rec.get("level_key"), "effort": rec.get("effort"),
        "effort_abstract": rec.get("effort_abstract"), "decided_by": rec.get("decided_by_name"),
        "router": rec.get("decided_by"), "reason": rec.get("reason"), "review": rec.get("review"),
        "router_review": rec.get("router_review"), "router_level": rec.get("router_level"),
        "resolved": {k: rec.get(k) for k in ("worker", "model", "provider", "effort")},
        "substitution": list(rec.get("steps", [])), "degraded": rec.get("degraded"),
        "attempts": (chain or {}).get("attempts", []), "confirmation": (chain or {}).get("confirmation"),
        "config_error": config_error,
        "seconds": round(time.monotonic() - started, 2),
    }
    if os.environ.get("TP_ROUTING_EVALS_TEXT") == "1" and card:
        out["card"] = card[:MAX_TEXT]
    return out


def _local_rule(rec, req, config, ctx, health):
    """Apply a machine-local route rule (config/local.json "rules") to a router's choice, as tp-route always did.

    The redirect gets no bypass: the same constraints the resolver applied are applied again to its target
    (local-only, restricted level, hidden or unrostered model, effort ceiling and the model's efforts, provider
    health, context fit, required harness). A redirect that breaks one is dropped, and the reason is recorded."""
    if not config or rec.get("source") == "explicit" or not config.get("local", {}).get("rules"):
        return
    cat = rec["_cat"]
    try:
        target = router.apply_rules(req.get("task", ""), {"worker": rec["worker"], "model": rec["model"], "thinking": rec["effort"]}, config)
    except router.RouterError as exc:
        rec["steps"].append("local rule ignored: {}".format(str(exc)[:160]))
        return
    if target["model"] == rec["model"]:
        return
    if req.get("worker") and target["worker"] != req["worker"]:
        rec["steps"].append("local rule redirect of {} to {} dropped: it runs on {}, and --worker {} was required".format(
            rec["model"], target["model"], target["worker"], req["worker"]))
        return
    here = rc.level_of(cat, rec["model"])[0]
    entering = rec.get("source") in ("explicit_level", "escalation") or bool(here and here.get("restricted"))
    problems, effort = rr.redirect_problems(cat, target, ctx, health, entering)
    if problems:
        rec["steps"].append("local rule redirect of {} to {} dropped: {}".format(rec["model"], target["model"], "; ".join(problems)))
        return
    rec["steps"].append("local rule redirected {} to {}".format(rec["model"], target["model"]))
    level = rc.level_of(cat, target["model"])[0]
    rec.update(worker=target["worker"], model=target["model"], effort=effort, provider=rc.provider_of(cat, target["worker"], target["model"]),
               level_key=level["key"])
    rec["review"] = rr.review_max(rec.get("review") or "none", rr.review_policy(cat, ctx.get("role"), level["key"], effort, ctx.get("files_touched")))
    rec["source"] = "router_resolved" if rec["source"] == "router" else rec["source"]


PATHISH = re.compile(r"(?<![\w.\-/])(?:[\w.\-]+/)+[\w.\-*]+")


def task_paths(task, deliverable="", context=()):
    """The file paths a task names: its --context specs (range dropped) and the path-shaped words of its text."""
    found = [re.sub(r":\d+-\d+$", "", str(spec)) for spec in context or ()]
    found += PATHISH.findall("{} {}".format(task or "", deliverable or ""))
    return sorted({path.rstrip(".,;:") for path in found if path})


def select(req, runner=subprocess.run, base=None, catalog_path=None, config_path=None, log=True, opener=None):
    """Choose a worker, model and effort for a request. req keys: task, role, deliverable, context, workdir, worker,
    model, thinking, level, local_only, approve_effort. Returns a dict with worker, model, thinking and the record
    of how they were chosen; raises ValueError for a bad request and RouteStop when the user must decide."""
    started = time.monotonic()
    worker, model, thinking = req.get("worker"), req.get("model"), req.get("thinking")
    if model and req.get("level"):
        raise ValueError("use either --model or --level, not both")
    config, config_error = load_roster_config(ROOT, config_path)
    # The card is built and hashed before anything branches, so every selection record, explicit ones included, carries it.
    card = route_card.build_card(req.get("role"), req.get("task", ""), req.get("deliverable", ""), req.get("context") or (),
                                 req.get("workdir"))
    local_only = bool(req.get("local_only"))
    try:
        cat = rc.load_catalog(catalog_path)
    except rc.CatalogError as exc:
        if local_only:
            raise RouteStop("model catalog unreadable ({}): a local-only task cannot be checked, so it is not routed".format(str(exc)[:120]))
        if model:
            # An explicit model wins only among valid, rostered, shown models. With no catalog that cannot be
            # checked, so the choice is refused rather than started unchecked.
            raise RouteStop("model catalog unreadable ({}): --model {} cannot be validated, so it is not routed".format(
                str(exc)[:120], model))
        w, m, t = _emergency(config, worker, req.get("role"), catalog_path)
        return _finish("route", req, None, {"source": "catalog_error_fallback", "worker": w, "model": m, "effort": thinking or t,
                       "reason": "model catalog unreadable: {}".format(str(exc)[:160]), "steps": []},
                       card, None, config_error, started, base, log)
    if thinking and thinking not in cat["effort"]["order"]:
        raise ValueError("effort {!r} is not one of: {}".format(thinking, ", ".join(cat["effort"]["order"])))
    if local_only and worker and worker not in {v["worker"] for v in cat["providers"].values() if v["kind"] == "local"}:
        raise ValueError("--worker {} cannot run local-only work: its models are cloud models; local models run on {}".format(
            worker, ", ".join(sorted({v["worker"] for v in cat["providers"].values() if v["kind"] == "local"}))))
    approvals = {"effort_above_high": bool(req.get("approve_effort"))}
    ctx = {"role": req.get("role"), "approvals": approvals, "local_only": local_only,
           "require_worker": worker, "est_tokens": estimate_tokens(req.get("task", ""), req.get("deliverable", ""),
                                                                  req.get("context"), req.get("workdir"))}
    ctx["paths"] = task_paths(req.get("task", ""), req.get("deliverable", ""), req.get("context"))
    health = read_health(base or os.environ.get("TP_STATE_DIR"))
    chain = None
    if model:
        worker = explicit_pair(cat, worker, model)
        ctx["explicit"] = {"model": model, "worker": worker, "thinking": thinking}
        rec = rr.resolve(None, cat, health, ctx, None)
        rec["decided_by_name"], rec["reason"] = "explicit", "explicit dispatch selection"
    else:
        if thinking:
            ctx["explicit_effort"] = thinking
        if req.get("level"):
            if req["level"] not in rc.shown_keys(cat):
                raise ValueError("level {!r} is not offered; choose one of {}".format(req["level"], ", ".join(rc.shown_keys(cat))))
            ctx["skip_caps"] = True
            decision = {"level_key": req["level"], "effort": thinking or cat["effort"]["default"], "review": "optional",
                        "shape": None, "reason": "explicit level request", "source": "explicit_level"}
            rec = rr.resolve(decision, cat, health, ctx, None)
            rec["decided_by_name"] = "explicit"
        else:
            chain = llm_router.run_chain(cat, card, health, ctx["local_only"], runner, config=config, opener=opener)
            decided = chain["decided_by"]
            ctx["decided_cap"] = (decided or {}).get("max_level")
            if chain["decision"] is None and ctx["local_only"]:
                rec = {"steps": [], "source": "stop_and_ask",
                       "reason": "local-only task and no local router answered: {}".format(chain["reason"])}
            else:
                rec = rr.resolve(chain["decision"], cat, health, ctx, decided)
                rec["router_review"] = (chain["decision"] or {}).get("review")
                rec["decided_by_name"] = decided["model"] if decided else "fallback"
                if rec["source"] == "stop_and_ask":
                    pass  # the resolver's own reason stands
                elif decided is None:
                    rec["reason"] = "router unavailable: {}".format(chain["reason"])[:400]
                    rec["steps"].insert(0, rec["reason"])
                else:
                    rec["reason"] = chain["decision"]["reason"]
    rec["_cat"] = cat
    if rec["source"] != "stop_and_ask":
        _local_rule(rec, req, config, ctx, health)
    return _finish("route", req, cat, rec, card, chain, config_error, started, base, log)


def _finish(kind, req, cat, rec, card, chain, config_error, started, base, log):
    rec.pop("_cat", None)
    rec.setdefault("steps", [])
    record = _record(kind, req, cat, rec, card, chain, config_error, started)
    record["status"] = "stop_and_ask" if rec["source"] == "stop_and_ask" else "ok"
    selection_id = write_selection(record, base) if log else None
    if rec["source"] == "stop_and_ask":
        raise RouteStop(rec.get("reason", "no model available"))
    result = {
        "worker": rec["worker"], "model": rec["model"], "thinking": rec["effort"], "level": rec.get("level_key"),
        "provider": rec.get("provider"), "effort": rec["effort"], "effort_abstract": rec.get("effort_abstract"),
        "review": rec.get("review"), "router_review": rec.get("router_review"), "source": rec["source"],
        "reason": rec.get("reason", ""), "decided_by": rec.get("decided_by_name"), "steps": rec["steps"],
        "approvals": rec.get("approvals", {}), "local_only": bool(req.get("local_only")),
        "catalog_version": record["catalog_version"], "catalog_sha": record["catalog_sha"],
        "prompt_sha256": record["prompt_sha256"], "card_sha256": record["card_sha256"], "selection_id": selection_id,
        "substituted": bool(rec["steps"]),
    }
    return result


# ---------------------------------------------------------------- escalation and review

def level_effort_of_task(meta, cat):
    """(level key, abstract effort) of an earlier attempt, from its recorded route or, failing that, its model."""
    route = meta.get("route_info") or {}
    level = route.get("level") if route.get("level") in rc.level_keys(cat) else None
    if not level:
        found = rc.level_of(cat, meta.get("model"))[0]
        level = found["key"] if found else cat["default_level"]
    effort = route.get("effort_abstract") or meta.get("thinking") or cat["effort"]["default"]
    if effort not in cat["effort"]["order"]:
        effort = cat["effort"]["default"]
    return level, effort


def meta_card(meta, role=None):
    """The router card of an earlier task, rebuilt from its recorded request, so escalation and review records carry
    the same card hash as the selection that placed it."""
    return route_card.build_card(role or meta.get("role"), meta.get("task", ""), meta.get("deliverable", ""),
                                 meta.get("context") or (), meta.get("workdir"))


def escalate_route(meta, cat, role=None, worker=None, approve_effort=False, base=None, log=True, local_only=False):
    """The next route after repeated failure: effort up before level, never above the ceiling without approval.
    Returns the same result as select(); raises RouteStop at the top level at the ceiling, and for a local-only task
    (the flag is also read from the failed attempt's recorded route) when no stronger local option exists: it never
    escalates to a cloud model."""
    started = time.monotonic()
    local_only = bool(local_only or (meta.get("route_info") or {}).get("local_only"))
    level, effort = level_effort_of_task(meta, cat)
    prior = {"level_key": level, "effort_abstract": effort, "model": meta.get("model"), "review": "optional",
             "decided_by": None}
    ctx = {"role": role, "require_worker": worker, "approvals": {"effort_above_high": bool(approve_effort)},
           "local_only": local_only,
           "est_tokens": estimate_tokens(meta.get("task", ""), meta.get("deliverable", ""), meta.get("context"), meta.get("workdir"))}
    if local_only and worker and worker not in {v["worker"] for v in cat["providers"].values() if v["kind"] == "local"}:
        raise ValueError("--worker {} cannot run local-only work".format(worker))
    rec = rr.escalate(prior, cat, read_health(base), ctx, ctx["approvals"])
    if local_only and rec["source"] != "stop_and_ask" and (rec["model"], rec["effort"]) == (meta.get("model"), meta.get("thinking")):
        rec = dict(rec, source="stop_and_ask", reason="local-only task: no stronger local model or effort to escalate to")
    rec["decided_by_name"] = "escalation"
    rec["reason"] = "escalation after repeated failure: {}".format(rec.get("reason") or rec.get("source", ""))
    request = {"role": role, "task": meta.get("task") or "escalation of {}".format(meta.get("id", "")), "worker": worker,
               "approve_effort": approve_effort, "local_only": local_only}
    return _finish("escalation", request, cat, rec, meta_card(meta, role), None, None, started, base, log)


def git_files_touched(workdir, runner=subprocess.run):
    """Number of changed or new files in a working tree (not attributed to one task), or None when unknown."""
    try:
        done = runner(["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).tmux-pi"],
                      capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return len([line for line in done.stdout.splitlines() if line.strip()]) if done.returncode == 0 else None


def review_plan(task_dir, force=False, level=None, thinking=None, approve_effort=False, base=None, catalog_path=None,
                runner=subprocess.run, files_touched=None, log=True):
    """Decide whether a review is needed and, if so, who reviews. Returns a dict with review (none|optional|required|
    forced) and, unless review is none, the reviewer's worker, model, thinking and independence. Raises RouteStop when
    a required review has no independent provider, and ValueError for a bad request."""
    started = time.monotonic()
    cat = rc.load_catalog(catalog_path)
    if base is None and Path(task_dir).resolve().parent.name == "tasks":
        base = Path(task_dir).resolve().parent.parent  # the session's state directory
    meta = _read_json(Path(task_dir) / "meta.json")
    if not meta.get("task"):  # the request text lives beside meta.json; the card is rebuilt from it for the record
        try:
            meta["task"] = (Path(task_dir) / "request.md").read_text(encoding="utf-8").strip()
        except OSError:
            pass
    route = meta.get("route_info") or {}
    worker, model = meta.get("worker"), meta.get("model") or _read_json(Path(task_dir) / "state.json").get("model")
    if worker not in ("pi", "claude"):
        raise ValueError("the author's worker is not recorded")
    author_level, author_effort = level_effort_of_task(dict(meta, model=model), cat)
    if files_touched is None and meta.get("workdir"):
        files_touched = git_files_touched(meta["workdir"], runner)
    policy = rr.review_policy(cat, meta.get("role"), author_level, author_effort, files_touched)
    need = rr.review_max(policy, route.get("router_review") or "none")
    plan = {"review": need, "policy": policy, "router_review": route.get("router_review"), "files_touched": files_touched,
            "author": {"worker": worker, "model": model or None, "level": author_level, "effort": author_effort}}
    if need == "none" and not force:
        return plan
    if force:
        plan["review"] = "forced"
    # A worker started without a model (the harness default) is placed by its harness: every provider that uses it is excluded.
    author_provider = rc.provider_of(cat, worker, model) if model else tuple(
        name for name, spec in cat["providers"].items() if spec["worker"] == worker)
    effort = thinking or (cat["effort"]["ceiling"] if author_effort == cat["effort"]["ceiling"] else cat["reviewer"]["default_effort"])
    ctx = {"explicit_effort": effort, "approvals": {"effort_above_high": bool(approve_effort)},
           "local_only": bool(route.get("local_only"))}
    if level:
        if level not in rc.shown_keys(cat):
            raise ValueError("level {!r} is not offered; choose one of {}".format(level, ", ".join(rc.shown_keys(cat))))
        ctx["level_key"] = level
    picked = rr.pick_reviewer(author_provider, author_level, cat, read_health(base), "required" if force else need, ctx)
    request = {"role": "reviewer", "task": meta.get("task") or "review of {}".format(meta.get("id", "")), "level": level,
               "local_only": ctx["local_only"]}
    record = _record("review", request, cat, dict(picked, decided_by_name="policy",
                     reason="independent reviewer for {} work".format(author_provider)), meta_card(meta), None, None, started)
    record["status"] = "stop_and_ask" if picked["source"] == "stop_and_ask" else "ok"
    record["author"] = plan["author"] | {"provider": author_provider if isinstance(author_provider, str) else list(author_provider)}
    record["independence"] = picked.get("independence")
    if log:
        write_selection(record, base)
    if picked["source"] == "stop_and_ask":
        raise RouteStop(picked.get("reason", "no independent reviewer available"))
    plan["reviewer"] = {"worker": picked["worker"], "model": picked["model"], "thinking": picked["effort"],
                        "provider": picked["provider"], "independence": picked["independence"], "level": picked["level_key"],
                        "steps": picked["steps"]}
    return plan
