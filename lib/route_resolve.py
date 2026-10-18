"""The deterministic half of model routing: turn the router model's reply, or an explicit choice, into one
worker/model/effort or a stop-and-ask. Level order, providers, roles, floors, caps and effort rules all come
from the catalog (lib/router_catalog.py); nothing here names a level, model or provider.

The router's reply is only a request. This code validates it, clamps it, applies role floors and caps,
finds an available model, maps the effort onto what that model allows, and records every step it took.

The top (restricted) level is reached only by the router choosing it, by escalation, or by an explicit
request. Falling "up" for availability never enters a restricted level, and no wording of a task, no risk
and no security signal raises a level.
"""

import fnmatch

from router_catalog import REVIEW_ORDER, level_keys, level_of, provider_of, shown_keys


def review_max(a, b):
    return REVIEW_ORDER[max(REVIEW_ORDER.index(a), REVIEW_ORDER.index(b))]


def effort_rank(cat, effort):
    return cat["effort"]["order"].index(effort)


def provider_ok(health, provider, model=None):
    for key in (provider + ":" + model if model else None, provider):
        if key and health.get(key, {}).get("status", "ok") != "ok":
            return False
    return True


# ---------------------------------------------------------------- the router chain

def choose_router(cat, health=None, local_only=False, skip=()):
    """First router in the chain whose provider is available and not already tried. local_only keeps local ones."""
    health = health or {}
    for entry in cat["router"]["chain"]:
        if entry["model"] in skip:
            continue
        if local_only and cat["providers"][entry["provider"]]["kind"] != "local":
            continue
        if provider_ok(health, entry["provider"], entry["model"]):
            return entry
    return None


# ---------------------------------------------------------------- review need

def review_policy(cat, role, level_key, effort, files_touched=None):
    """none, optional or required from facts alone: the independent maximum of the role floor, the files touched,
    the level and the effort. A read-only role has floor none, but a high level or effort still requires a review.
    The router's own review field can only raise the result (see resolve).
    """
    policy = cat["review_policy"]
    result = policy["role_floor"].get(role, policy["default_floor"])
    keys = level_keys(cat)
    if files_touched:
        if files_touched >= policy["files_required_at"]:
            result = review_max(result, "required")
        elif files_touched >= policy["files_optional_at"]:
            result = review_max(result, "optional")
    if level_key in keys and keys.index(level_key) >= keys.index(policy["required_from_level"]):
        result = review_max(result, "required")
    if (level_key in keys and keys.index(level_key) >= keys.index(policy["high_effort_required_from_level"])
            and effort in cat["effort"]["order"] and effort_rank(cat, effort) >= effort_rank(cat, cat["effort"]["ceiling"])):
        result = review_max(result, "required")
    return result


# ---------------------------------------------------------------- effort

def concrete_effort(cat, level, model, abstract):
    """Map the abstract effort onto the model's allowed list; return (effort, note or None)."""
    order = cat["effort"]["order"]
    want = level["effort_map"].get(abstract, abstract)
    allowed = model["efforts"]
    if want in allowed:
        return want, None
    below = [e for e in allowed if order.index(e) <= order.index(want)]
    used = below[-1] if below else allowed[0]
    return used, "effort {} not allowed on {}; used {}".format(want, model["model"], used)


def clamp_effort(cat, effort, approved):
    """(effort, note): above the ceiling is cut to the ceiling unless an approval is recorded."""
    ceiling = cat["effort"]["ceiling"]
    if effort_rank(cat, effort) > effort_rank(cat, ceiling) and not approved:
        return ceiling, "effort {} capped at {} (no approval recorded)".format(effort, ceiling)
    return effort, None


# ---------------------------------------------------------------- candidates

class ExplicitError(ValueError):
    """An explicit choice (model, worker, effort, level) that cannot be used; the message says why."""


def unusable_reason(cat, m, ctx, health):
    """Why a catalog model cannot be used for this request, or None when it can."""
    provider = m["provider"]
    if not m["in_roster"] and not ctx.get("allow_unrostered"):
        return "{} is not in the roster".format(m["model"])
    if not provider_ok(health, provider, m["model"]):
        return "{} is marked unavailable".format(provider if not provider_ok(health, provider) else m["model"])
    if provider in ctx.get("exclude_providers", ()):
        return "provider {} is excluded here".format(provider)
    if ctx.get("require_worker") and m["worker"] != ctx["require_worker"]:
        return "{} runs on {}, not {}".format(m["model"], m["worker"], ctx["require_worker"])
    if ctx.get("local_only") and cat["providers"][provider]["kind"] != "local":
        return "{} is not a local model and the task is local-only".format(m["model"])
    tokens = ctx.get("est_tokens", 0)
    if tokens and tokens > 0.6 * m["context_window"]:
        return "the packet (about {} tokens) does not fit {}'s {} token context".format(tokens, m["model"], m["context_window"])
    if m.get("max_packet_tokens") and tokens > m["max_packet_tokens"]:
        return "the packet (about {} tokens) is over {}'s limit of {}".format(tokens, m["model"], m["max_packet_tokens"])
    return None


def candidates_at(cat, level, ctx, health):
    order = ctx.get("provider_order") or cat["provider_order"]["review" if ctx.get("is_review") else "author"]
    out = [m for m in level["models"] if unusable_reason(cat, m, ctx, health) is None]
    out.sort(key=lambda m: order.index(m["provider"]) if m["provider"] in order else len(order))
    return out


def validate_explicit(cat, model, worker, thinking, ctx, health):
    """(level, model entry, effort, notes) for an explicit model choice, or ExplicitError. An explicit choice wins
    only among valid, rostered, shown models whose effort and context fit; the worker/model pair must match."""
    level, entry = level_of(cat, model)
    if not entry:
        raise ExplicitError("model {} is not in the model catalog; choose one of: {}".format(
            model, ", ".join(m["model"] for lv in cat["levels"] if lv.get("shown", True) for m in lv["models"] if m["in_roster"])))
    if level["key"] not in shown_keys(cat):
        raise ExplicitError("model {} is at level {}, which is not offered".format(model, level["key"]))
    if worker and worker != entry["worker"]:
        raise ExplicitError("model {} belongs to {}, not {}".format(model, entry["worker"], worker))
    reason = unusable_reason(cat, entry, dict(ctx, require_worker=worker or ctx.get("require_worker")), health)
    if reason:
        raise ExplicitError("model {} cannot be used: {}".format(model, reason))
    notes = []
    approved = bool(ctx.get("approvals", {}).get("effort_above_high"))
    if thinking:
        if thinking not in entry["efforts"]:
            raise ExplicitError("model {} does not allow effort {!r}; it allows: {}".format(model, thinking, ", ".join(entry["efforts"])))
        effort, note = clamp_effort(cat, thinking, approved)
        if note:
            notes.append(note)
        if effort not in entry["efforts"]:
            effort, note = concrete_effort(cat, level, entry, effort)
            notes.append(note)
    else:
        effort, note = concrete_effort(cat, level, entry, cat["effort"]["default"])
        if note:
            notes.append(note)
    return level, entry, effort, notes


def redirect_problems(cat, target, ctx, health, may_enter_restricted, bound=None):
    """Reasons a machine-local redirect to target {worker, model, thinking} breaks a constraint, and the effort to
    use (clamped to the ceiling unless approved). An empty list means the redirect is allowed.

    bound is the route's effective lower bound, the (level index, reason) that resolve recorded as rec["route_bound"]
    (lowered by an explicit level). Without it the bound is recomputed from the role's full floor, the stricter way."""
    level, entry = level_of(cat, target["model"])
    if not entry:
        return ["{} is not in the model catalog".format(target["model"])], target["thinking"]
    problems = []
    if level["key"] not in shown_keys(cat):
        problems.append("level {} is not offered".format(level["key"]))
    if level.get("restricted") and not may_enter_restricted:
        problems.append("level {} is restricted (reached only by the router's own pick, escalation or an explicit request)".format(level["key"]))
    if target["worker"] != entry["worker"]:
        problems.append("{} does not run on {}".format(target["model"], target["worker"]))
    if bound is None:
        bound = route_bound(cat, ctx, _strict_role_floor(cat, ctx.get("role")))
    bound, why = bound
    if bound is not None and level["key"] in shown_keys(cat) and level_keys(cat).index(level["key"]) < bound:
        problems.append("level {} is below the {}".format(level["key"], why))
    reason = unusable_reason(cat, entry, ctx, health)
    if reason:
        problems.append(reason)
    effort, note = clamp_effort(cat, target["thinking"], bool(ctx.get("approvals", {}).get("effort_above_high")))
    if effort not in entry["efforts"]:
        problems.append("effort {} is not allowed on {}".format(effort, target["model"]))
    return problems, effort


def confirm_reason(cat, entry, decision):
    """Why the next router should confirm this router's pick (its catalog `confirm` rule), or None."""
    rule = entry.get("confirm")
    if not rule or not decision:
        return None
    keys, shown = level_keys(cat), shown_keys(cat)
    picked = shown[decision["level"] - 1]
    if rule.get("above_level") and keys.index(picked) > keys.index(rule["above_level"]):
        return "{} picked level {}, above {}".format(entry["model"], picked, rule["above_level"])
    if (rule.get("unqualified_local") and cat["levels"][keys.index(picked)].get("requires_shape")
            and not _qualified(cat, decision.get("shape"))):
        return "{} picked level {} for a shape that is not qualified".format(entry["model"], picked)
    return None


def _strict_role_floor(cat, role):
    """Index of the level a strict-floor role may not go below, or None."""
    rule = cat["roles"].get(role or "") or {}
    return _shown_at_or_above(cat, rule["min_level"]) if rule.get("strict_floor") and "min_level" in rule else None


def lowered_by_explicit(floor_idx, asked_idx):
    """A floor (level index) after an explicit level request: lowered only as far as the level asked, never raised.
    The one rule for a role floor in resolve and for the reviewer minimum in pick_reviewer."""
    return min(floor_idx, asked_idx)


def route_bound(cat, ctx, role_floor):
    """(level index, reason) of the one lower bound of a route, or (None, None): the highest of the strict role floor
    (role_floor, already lowered by an explicit level where that applies) and the protected-path minimum, which an
    explicit level never lowers, and the level named by ctx["min_level_key"] (the reviewer minimum, already lowered by
    resolve for an explicit level). Every downward move (availability, redirect, local-only) stays at or above it."""
    bounds = []
    if role_floor is not None:
        bounds.append((role_floor, "{} floor for role {}".format(level_keys(cat)[role_floor], ctx.get("role"))))
    hit = protected_hit(cat, ctx.get("role"), ctx.get("paths"))
    if hit:
        bounds.append((_shown_at_or_above(cat, hit[0]["min_level"]), "{} minimum for protected path {}".format(hit[0]["min_level"], hit[1])))
    if ctx.get("min_level_key"):
        bounds.append((_shown_at_or_above(cat, ctx["min_level_key"]), "{} minimum for the role".format(ctx["min_level_key"])))
    return max(bounds, key=lambda b: b[0]) if bounds else (None, None)


def _shown_at_or_above(cat, key):
    keys, shown = level_keys(cat), shown_keys(cat)
    return next((keys.index(k) for k in shown if keys.index(k) >= keys.index(key)), keys.index(shown[-1]))


def _shown_at_or_below(cat, key):
    keys, shown = level_keys(cat), shown_keys(cat)
    return next((keys.index(k) for k in reversed(shown) if keys.index(k) <= keys.index(key)), keys.index(shown[0]))


def protected_hit(cat, role, paths):
    """(catalog entry, path) for the strictest protected-path rule a task's paths match, or None.

    A pattern matches a path as written or below any directory (lib/x.py matches /repo/lib/x.py). An entry with
    `roles` applies to those roles only."""
    best = None
    keys = [lv["key"] for lv in cat["levels"]]
    for entry in cat.get("protected_paths") or ():
        if "roles" in entry and role not in entry["roles"]:
            continue
        for path in paths or ():
            if fnmatch.fnmatchcase(path, entry["pattern"]) or fnmatch.fnmatchcase(path, "*/" + entry["pattern"]):
                if best is None or keys.index(entry["min_level"]) > keys.index(best[0]["min_level"]):
                    best = (entry, path)
                break
    return best


def _qualified(cat, shape):
    return bool(shape and cat["shapes"].get(shape, {}).get("status") == "qualified")


def _stop(rec, reason):
    rec.update(source="stop_and_ask", reason=reason)
    return rec


# ---------------------------------------------------------------- resolution

def resolve(decision, cat, health=None, ctx=None, decided_by=None):
    """One route record, or a stop-and-ask (source "stop_and_ask" with a reason).

    decision: the parsed router reply (level is the shown-level number), a dict with level_key instead of
    level for an internal request, or None when no router gave a usable reply.
    ctx keys: role, explicit {model, worker, thinking}, local_only, exclude_providers, require_worker,
    est_tokens, is_review, approvals {"effort_above_high"}, files_touched, floor_review, allow_unrostered,
    decided_cap (level key), skip_caps (escalation and explicit level), explicit_effort, min_level_key (the reviewer
    minimum, a floor) with min_level_explicit (an explicit level request lowers it only as far as the level asked).
    """
    health = health or {}
    ctx = dict(ctx or {})
    steps = []
    approved = bool(ctx.get("approvals", {}).get("effort_above_high"))
    rec = {"decided_by": decided_by, "catalog_version": cat["catalog_version"], "steps": steps,
           "approvals": {"effort_above_high": approved}}
    keys, shown = level_keys(cat), shown_keys(cat)
    default = cat["effort"]["default"]

    explicit = ctx.get("explicit")
    if explicit and explicit.get("model"):
        level, entry, effort, notes = validate_explicit(cat, explicit["model"], explicit.get("worker"), explicit.get("thinking"),
                                                        ctx, health)
        steps.extend(notes)
        policy = review_policy(cat, ctx.get("role"), level["key"], effort, ctx.get("files_touched"))
        rec.update(source="explicit", model=entry["model"], worker=entry["worker"], effort=effort,
                   effort_abstract=explicit.get("thinking") or default, level_key=level["key"], provider=entry["provider"],
                   review=review_max(policy, ctx.get("floor_review", "none")), review_policy=policy)
        return rec

    if decision is None:
        idx = keys.index(cat["default_level"])
        eff, review, shape = default, "optional", None
        rec["source"] = "router_failed_default"
        steps.append("no usable router reply: default level {} at {}".format(keys[idx], eff))
    elif "level_key" in decision:
        idx = keys.index(decision["level_key"])
        eff, review, shape = decision.get("effort", default), decision.get("review", "optional"), decision.get("shape")
        rec["source"] = decision.get("source", "requested")
        rec["router_reason"] = decision.get("reason", "")
    else:
        idx = keys.index(shown[decision["level"] - 1])
        eff, review, shape = decision["effort"], decision["review"], decision["shape"]
        rec["source"] = "router"
        rec["router_reason"] = decision["reason"]
        rec["router_level"] = decision["level"]
    if ctx.get("explicit_effort"):
        eff = ctx["explicit_effort"]
    rec["asked"] = {"level": keys[idx], "effort": eff, "review": review, "shape": shape}

    if keys[idx] not in shown:
        idx = _shown_at_or_below(cat, keys[idx])
        steps.append("level is not offered: used level {}".format(keys[idx]))

    # a level that needs a qualified shape (local work) is entered only with one
    # (a local-only task at a level it named or was escalated to keeps it: that level had its shape when it was routed)
    named = ctx.get("local_only") and rec["source"] in ("explicit_level", "escalation")
    if cat["levels"][idx].get("requires_shape") and not _qualified(cat, shape) and not named:
        idx = _shown_at_or_above(cat, keys[idx + 1]) if idx + 1 < len(keys) else idx
        steps.append("shape not qualified for the lowest level: moved to level {}".format(keys[idx]))

    # a router can be capped (a local router's route is capped; its picks are least reliable)
    cap = ctx.get("decided_cap")
    if cap and idx > _shown_at_or_below(cat, cap):
        idx = _shown_at_or_below(cat, cap)
        steps.append("decided by a capped router: level {}".format(keys[idx]))

    eff, note = clamp_effort(cat, eff, approved)
    if note:
        steps.append(note)

    # role floors and caps: a floor always applies, a cap only to the router's own choice
    rule = cat["roles"].get(ctx.get("role") or "", {})
    # The floor is waived only for the task shapes the role lists (a coder's small named-file edit with a check).
    # An explicit --level from the orchestrator lowers the floor only as far as the level it asked for (as --model
    # overrides it); the router's own pick, fallbacks, redirects and escalation never do.
    role_floor = _shown_at_or_above(cat, rule["min_level"]) if "min_level" in rule else None
    if role_floor is not None and rec["source"] == "explicit_level":
        role_floor = lowered_by_explicit(role_floor, idx)
    if (role_floor is not None and idx < role_floor and shape not in rule.get("below_floor_shapes", ())):
        idx = role_floor
        steps.append("role {} floor: level {}".format(ctx["role"], keys[idx]))
    if "min_effort" in rule and effort_rank(cat, eff) < effort_rank(cat, rule["min_effort"]):
        eff = rule["min_effort"]
        steps.append("role {} floor: effort {}".format(ctx["role"], eff))
    if "max_level" in rule and not ctx.get("skip_caps") and idx > _shown_at_or_below(cat, rule["max_level"]):
        idx = _shown_at_or_below(cat, rule["max_level"])
        steps.append("role {} cap: level {}".format(ctx["role"], keys[idx]))

    # protected paths: work that touches them never goes below the stated level, whatever its shape or wording
    hit = protected_hit(cat, ctx.get("role"), ctx.get("paths"))
    if hit:
        entry, path = hit
        floor = _shown_at_or_above(cat, entry["min_level"])
        if idx < floor:
            idx = floor
            steps.append("protected path {}: level {}".format(path, keys[idx]))
        if "min_effort" in entry and effort_rank(cat, eff) < effort_rank(cat, entry["min_effort"]):
            eff = entry["min_effort"]
            steps.append("protected path {}: effort {}".format(path, eff))

    # The reviewer minimum (ctx["min_level_key"]) is a floor of the same kind: an explicit level request lowers it only
    # as far as the level asked (ctx["min_level_explicit"]), by the same rule and at the same point as a role floor.
    bound_ctx = ctx
    if ctx.get("min_level_key"):
        min_floor = _shown_at_or_above(cat, ctx["min_level_key"])
        if ctx.get("min_level_explicit"):
            min_floor = lowered_by_explicit(min_floor, idx)
        bound_ctx = dict(ctx, min_level_key=keys[min_floor])

    # The route's one lower bound: no later step crosses it, not the local-only gate, not moving down for availability.
    strict_floor, bound_why = route_bound(cat, bound_ctx, role_floor if rule.get("strict_floor") else None)
    rec["route_bound"] = (strict_floor, bound_why)

    qualified = _qualified(cat, shape)
    if ctx.get("local_only"):
        locals_ = [i for i, lv in enumerate(cat["levels"]) if any(
            cat["providers"][m["provider"]]["kind"] == "local" for m in lv["models"])]
        if not locals_:
            return _stop(rec, "local-only task and the catalog has no local model")
        if rec["source"] in ("explicit_level", "escalation"):
            # a level the user or the escalation rule named: it must itself have a local model; never a cloud one
            if idx not in locals_:
                message = "local-only task: level {} has no local model".format(keys[idx])
                if rec["source"] == "explicit_level":
                    raise ExplicitError(message + "; choose a level with a local model ({})".format(", ".join(keys[i] for i in locals_)))
                return _stop(rec, message + "; no further local escalation")
        else:
            gate = locals_[0]
            if cat["levels"][gate].get("requires_shape") and not qualified:
                return _stop(rec, "local-only task outside the qualified local shapes")
            if strict_floor is not None and gate < strict_floor:
                return _stop(rec, "local-only task, but the {} allows no level below it and no local model is that high; "
                                  "the user must decide (drop --local-only or lower the floor in the catalog)".format(bound_why))
            idx = gate
            steps.append("local-only: level {}".format(keys[idx]))

    # find a model: this level, then up (never into a restricted or hidden level), then down with forced review
    up = [j for j in range(idx + 1, len(keys)) if keys[j] in shown and not cat["levels"][j].get("restricted")]
    down = [j for j in range(idx - 1, -1, -1) if keys[j] in shown and (strict_floor is None or j >= strict_floor)]
    chosen = None
    for j in [idx] + up + down:
        if j != idx and cat["levels"][j].get("requires_shape") and not qualified:
            continue
        found = candidates_at(cat, cat["levels"][j], ctx, health)
        if found:
            chosen = (j, found[0])
            break
    if not chosen:
        if strict_floor is not None:
            return _stop(rec, "no available model at level {} or above, the {}; the user must decide".format(
                keys[strict_floor], bound_why))
        return _stop(rec, "no available model at any level")
    j, model = chosen
    degraded = None
    if j != idx:
        degraded = "up" if j > idx else "down"
        steps.append("no available model at level {}: used level {} ({})".format(keys[idx], keys[j], degraded))
        if degraded == "down":
            review = "required"
            steps.append("moved down: review forced to required")
    final_effort, note = concrete_effort(cat, cat["levels"][j], model, eff)
    if note:
        steps.append(note)
    policy = review_policy(cat, ctx.get("role"), keys[j], eff, ctx.get("files_touched"))
    review = review_max(review_max(review, policy), ctx.get("floor_review", "none"))
    rec.update(level_key=keys[j], level=j + 1, provider=model["provider"], worker=model["worker"], model=model["model"],
               effort=final_effort, effort_abstract=eff, review=review, review_policy=policy, degraded=degraded)
    if rec["source"] == "router" and (idx != j or len(steps) > 0):
        rec["source"] = "router_resolved"
    return rec


# ---------------------------------------------------------------- escalation and reviewers

def escalate(rec, cat, health=None, ctx=None, approvals=None):
    """After two failed attempts or two REQUEST_CHANGES: raise the effort one step if it is below the ceiling,
    otherwise move one level up. Effort never goes above the ceiling without a recorded approval; with one,
    effort keeps rising (as far as the model allows) before the level does. At the top level at the ceiling with
    nothing further, stop and ask."""
    ctx = dict(ctx or {}, skip_caps=True)
    approved = bool((approvals or {}).get("effort_above_high")) or bool(ctx.get("approvals", {}).get("effort_above_high"))
    ctx["approvals"] = {"effort_above_high": approved}
    vocab, order, ceiling = cat["effort"]["vocabulary"], cat["effort"]["order"], cat["effort"]["ceiling"]
    keys, shown = level_keys(cat), shown_keys(cat)
    level_key, eff = rec["level_key"], rec["effort_abstract"]
    model = (level_of(cat, rec.get("model"))[1] or {"efforts": order}) if rec.get("model") else {"efforts": order}
    if eff in vocab and effort_rank(cat, eff) < effort_rank(cat, ceiling):
        new_eff, new_key, how = vocab[vocab.index(eff) + 1], level_key, "effort up"
    elif approved and any(effort_rank(cat, e) > effort_rank(cat, eff) for e in model["efforts"]):
        new_eff = next(e for e in model["efforts"] if effort_rank(cat, e) > effort_rank(cat, eff))
        new_key, how = level_key, "effort up (approved)"
    else:
        above = [k for k in shown if keys.index(k) > keys.index(level_key)]
        if not above:
            return dict(rec, source="stop_and_ask", reason="top level at the effort ceiling: approval needed for more")
        new_eff, new_key, how = eff, above[0], "level up"
    out = resolve({"level_key": new_key, "effort": new_eff, "review": rec.get("review", "optional"), "shape": None,
                   "reason": "escalation: " + how, "source": "escalation"}, cat, health, ctx, rec.get("decided_by"))
    if out["source"] != "stop_and_ask":
        out["source"] = "escalation_" + how.split(" (")[0].replace(" ", "_")
    return out


def pick_reviewer(author_provider, author_level_key, cat, health=None, review_level="required", ctx=None):
    """A reviewer on a different provider (author_provider may be a list when only the harness is known): level at least the catalog's reviewer floor (and the author's, but never
    a restricted level), the first available by the review provider order. When no other provider is available a
    required review stops and asks; an optional one may use the author's provider (marked same_provider)."""
    ctx = dict(ctx or {}, is_review=True, skip_caps=True)
    excluded = (author_provider,) if isinstance(author_provider, str) else tuple(author_provider)
    keys = level_keys(cat)
    floor = keys.index(cat["reviewer"]["min_level"])
    if ctx.get("level_key"):  # an explicit request for a reviewer level
        start = keys.index(ctx["level_key"])
    else:
        start = max(floor, keys.index(author_level_key) if author_level_key in keys else floor)
        while start > floor and (keys[start] not in shown_keys(cat) or cat["levels"][start].get("restricted")):
            start -= 1
    effort = ctx.get("explicit_effort") or cat["reviewer"]["default_effort"]
    request = {"level_key": keys[start], "effort": effort, "review": "optional", "shape": None,
               "reason": "review of {} work".format("/".join(excluded)), "source": "review"}
    # The route's bound includes the reviewer minimum. resolve lowers it for an explicit reviewer level exactly as it
    # lowers a role floor for an explicit --level; with no level, availability may not move a review below it.
    ctx["min_level_key"] = keys[floor]
    ctx["min_level_explicit"] = bool(ctx.get("level_key"))
    if ctx.get("same_provider_approved"):
        # The user approved a reviewer from the author's own provider: every other provider is excluded, so a healthy
        # other provider is not picked and the usual level floor and effort rules still apply.
        others = tuple(name for name in cat["providers"] if name not in excluded)
        chosen = resolve(request, cat, health, dict(ctx, exclude_providers=others))
        if chosen["source"] != "stop_and_ask":
            chosen["independence"] = "same_provider_user_approved"
            return chosen
        return dict(chosen, reason="no same-provider reviewer available ({})".format(chosen.get("reason", "no reason recorded")))
    first = resolve(request, cat, health, dict(ctx, exclude_providers=excluded))
    if first["source"] != "stop_and_ask":
        first["independence"] = "different_provider"
        return first
    if review_level == "optional" and not ctx.get("local_only"):
        second = resolve(request, cat, health, dict(ctx, exclude_providers=()))
        if second["source"] != "stop_and_ask":
            second["independence"] = "same_provider"
            return second
    return dict(first, source="stop_and_ask", reason="no independent reviewer available; review is {} ({})".format(
        review_level, first.get("reason", "no reason recorded")))
