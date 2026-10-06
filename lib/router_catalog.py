"""The model catalog: strict loading, the router prompt generated from it, and parsing of the router's reply.

Nothing here names a level, model, provider or effort. Adding a model or a level is an edit to
config/model-catalog.json; the prompt renumbers itself and lib/route_resolve.py maps the router's
number back to a level key.
"""

import hashlib
import json
import os
import re
from pathlib import Path

CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "model-catalog.json"
REVIEW_ORDER = ("none", "optional", "required")
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
PLACEHOLDER = re.compile(r"\{level:([a-z][a-z0-9_]*)\}")
REPLY_FIELDS = {"level", "effort", "review", "shape", "reason"}


class CatalogError(Exception):
    pass


def _fail(message):
    raise CatalogError(message)


def _object(value, where, required, optional=()):
    if not isinstance(value, dict):
        _fail("{} must be an object".format(where))
    missing = set(required) - set(value)
    extra = set(value) - set(required) - set(optional)
    if missing:
        _fail("{} is missing {}".format(where, ", ".join(sorted(missing))))
    if extra:
        _fail("{} has unknown keys: {}".format(where, ", ".join(sorted(extra))))
    return value


def _text(value, where):
    if not isinstance(value, str) or not value.strip():
        _fail("{} must be nonempty text".format(where))
    return value


def _int(value, where, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail("{} must be an integer of at least {}".format(where, minimum))
    return value


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        _fail("{} must be a positive number".format(where))
    return value


def _flag(value, where):
    if not isinstance(value, bool):
        _fail("{} must be true or false".format(where))
    return value


def _names(value, where, allowed=None):
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value) or len(set(value)) != len(value):
        _fail("{} must be a nonempty list of distinct names".format(where))
    if allowed is not None and not set(value) <= set(allowed):
        _fail("{} names unknown entries: {}".format(where, ", ".join(sorted(set(value) - set(allowed)))))
    return value


TRANSPORTS = ("cli", "direct_chat")


def _chain_options(entry, name, providers, shown, levels):
    """The optional per-router data: transport, sampling, thinking expectation, maximum input and the confirm rule."""
    transport = entry.get("transport", "cli")
    if transport not in TRANSPORTS:
        _fail("router transport for {} must be one of {}".format(name, ", ".join(TRANSPORTS)))
    if transport == "direct_chat":
        if providers[entry["provider"]]["kind"] != "local":
            _fail("router {}: the direct chat transport is for local providers only".format(name))
        if "env" in entry:
            _fail("router {}: env applies to a CLI router only".format(name))
    else:
        for key in ("temperature", "seed"):
            if key in entry:
                _fail("router {}: {} cannot be set through a CLI (claude and pi have no such option)".format(name, key))
    if "temperature" in entry and (isinstance(entry["temperature"], bool) or not isinstance(entry["temperature"], (int, float))
                                   or entry["temperature"] < 0):
        _fail("router temperature for {} must be a number of at least 0".format(name))
    if "seed" in entry:
        _int(entry["seed"], "router seed for " + name, 0)
    if "thinking_off" in entry:
        _flag(entry["thinking_off"], "router thinking_off for " + name)
    if "max_input_tokens" in entry:
        _int(entry["max_input_tokens"], "router max_input_tokens for " + name)
    if "confirm" in entry:
        rule = _object(entry["confirm"], "router confirm for " + name, (), ("above_level", "unqualified_local"))
        if not rule:
            _fail("router confirm for {} must set above_level or unqualified_local".format(name))
        if "above_level" in rule and rule["above_level"] not in shown:
            _fail("router confirm above_level for {} must be a shown level key".format(name))
        if "unqualified_local" in rule:
            _flag(rule["unqualified_local"], "router confirm unqualified_local for " + name)
            if rule["unqualified_local"] and not any(lv.get("requires_shape") for lv in levels):
                _fail("router confirm unqualified_local for {} needs a level with requires_shape".format(name))


def validate_catalog(cat):
    """Raise CatalogError unless the catalog is complete and consistent; return it."""
    _object(cat, "catalog", ("catalog_version", "default_level", "effort", "providers", "provider_order", "router",
                             "escalation", "reviewer", "review_policy", "principles", "review", "shapes", "roles",
                             "levels"), ("description", "protected_paths"))
    _int(cat["catalog_version"], "catalog_version")
    eff = _object(cat["effort"], "effort", ("order", "vocabulary", "default", "ceiling", "ceiling_rule", "meaning"))
    order = _names(eff["order"], "effort.order")
    vocab = _names(eff["vocabulary"], "effort.vocabulary", order)
    if sorted(vocab, key=order.index) != vocab:
        _fail("effort.vocabulary must follow effort.order")
    for key in ("default", "ceiling"):
        if eff[key] not in vocab:
            _fail("effort.{} must be in the vocabulary".format(key))
    _text(eff["ceiling_rule"], "effort.ceiling_rule")
    meaning = _object(eff["meaning"], "effort.meaning", vocab)
    for name in vocab:
        _text(meaning[name], "effort.meaning.{}".format(name))

    if not isinstance(cat["providers"], dict) or not cat["providers"]:
        _fail("providers must be a nonempty object")
    for name, provider in cat["providers"].items():
        _object(provider, "provider {}".format(name), ("worker", "kind"))
        if provider["worker"] not in ("pi", "claude"):
            _fail("provider {} worker must be pi or claude".format(name))
        if provider["kind"] not in ("cloud", "local"):
            _fail("provider {} kind must be cloud or local".format(name))
    providers = cat["providers"]
    po = _object(cat["provider_order"], "provider_order", ("author", "review"))
    for name in ("author", "review"):
        _names(po[name], "provider_order.{}".format(name), providers)

    levels = cat["levels"]
    if not isinstance(levels, list) or len(levels) < 2:
        _fail("levels must list at least two levels")
    keys, models, last_rank = [], {}, None
    for level in levels:
        where = "level {}".format(level.get("key") if isinstance(level, dict) else "?")
        _object(level, where, ("key", "name", "rank", "for", "not_for", "anchors", "expected_share", "models", "effort_map"),
                ("shown", "restricted", "requires_shape"))
        if not isinstance(level["key"], str) or not KEY.match(level["key"]):
            _fail("{}: key must be lowercase letters, digits and underscores".format(where))
        if level["key"] in keys:
            _fail("duplicate level key {}".format(level["key"]))
        keys.append(level["key"])
        _text(level["name"], where + " name")
        _text(level["for"], where + " for")
        _text(level["not_for"], where + " not_for")
        _int(level["rank"], where + " rank")
        if last_rank is not None and level["rank"] <= last_rank:
            _fail("levels are out of order: {} has rank {} after rank {}".format(level["key"], level["rank"], last_rank))
        last_rank = level["rank"]
        for flag in ("shown", "restricted", "requires_shape"):
            if flag in level:
                _flag(level[flag], "{} {}".format(where, flag))
        share = level["expected_share"]
        if (not isinstance(share, list) or len(share) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in share)
                or not 0 <= share[0] <= share[1] <= 1):
            _fail("{}: expected_share must be [low, high] within 0..1".format(where))
        emap = _object(level["effort_map"], where + " effort_map", vocab)
        for name in vocab:
            if emap[name] not in order:
                _fail("{}: effort_map.{} names unknown effort {!r}".format(where, name, emap[name]))
        ranks = [order.index(emap[name]) for name in vocab]
        if ranks != sorted(ranks):
            _fail("{}: effort_map must not decrease from {} to {}".format(where, vocab[0], vocab[-1]))
        if not isinstance(level["anchors"], list) or not level["anchors"]:
            _fail("{}: needs at least one anchor".format(where))
        for anchor in level["anchors"]:
            _object(anchor, where + " anchor", ("task", "effort"), ("shape",))
            _text(anchor["task"], where + " anchor task")
            if anchor["effort"] not in vocab:
                _fail("{}: anchor effort {!r} is not in the vocabulary".format(where, anchor["effort"]))
            if anchor.get("shape") is not None and anchor["shape"] not in cat["shapes"]:
                _fail("{}: anchor shape {!r} is unknown".format(where, anchor["shape"]))
        if not isinstance(level["models"], list) or not level["models"]:
            _fail("{} has no models".format(where))
        for model in level["models"]:
            _object(model, where + " model", ("provider", "worker", "model", "efforts", "context_window", "max_output", "in_roster"),
                    ("max_packet_tokens", "unverified"))
            name = _text(model["model"], where + " model name")
            if model["provider"] not in providers:
                _fail("{}: unknown provider {!r}".format(name, model["provider"]))
            if providers[model["provider"]]["worker"] != model["worker"]:
                _fail("{}: worker {!r} does not match provider {}".format(name, model["worker"], model["provider"]))
            if model["worker"] == "pi" and "/" not in name:
                _fail("{}: a pi model must be provider/model".format(name))
            if name in models:
                _fail("model {} is listed twice".format(name))
            efforts = _names(model["efforts"], name + " efforts", order)
            if sorted(efforts, key=order.index) != efforts:
                _fail("{}: efforts must follow effort.order".format(name))
            _int(model["context_window"], name + " context_window")
            _int(model["max_output"], name + " max_output")
            _flag(model["in_roster"], name + " in_roster")
            if "max_packet_tokens" in model:
                _int(model["max_packet_tokens"], name + " max_packet_tokens")
            for target in emap.values():
                if order.index(efforts[0]) > order.index(target):
                    _fail("{}: every allowed effort is above effort_map target {!r} at level {}".format(name, target, level["key"]))
            models[name] = (level["key"], model)

    shown = [lv["key"] for lv in levels if lv.get("shown", True)]
    if len(shown) < 2:
        _fail("at least two levels must be shown")
    default = next((lv for lv in levels if lv["key"] == cat["default_level"]), None)
    if default is None:
        _fail("default_level {!r} is not a level key".format(cat["default_level"]))
    if default.get("shown", True) is False or default.get("restricted") or default.get("requires_shape"):
        _fail("default_level must be a shown, unrestricted level without a shape requirement")
    if levels[0].get("restricted"):
        _fail("the lowest level cannot be restricted")

    router = _object(cat["router"], "router", ("chain", "budget_seconds", "retries_on_invalid"))
    _number(router["budget_seconds"], "router.budget_seconds")
    _int(router["retries_on_invalid"], "router.retries_on_invalid", 0)
    if not isinstance(router["chain"], list) or not router["chain"]:
        _fail("router.chain must list at least one router")
    for entry in router["chain"]:
        _object(entry, "router chain entry", ("provider", "worker", "model", "effort", "timeout_seconds"),
                ("env", "max_level", "transport", "temperature", "seed", "thinking_off", "max_input_tokens", "confirm"))
        name = entry["model"]
        if name not in models:
            _fail("router model {} is not in the catalog".format(name))
        if models[name][1]["provider"] != entry["provider"] or models[name][1]["worker"] != entry["worker"]:
            _fail("router model {} does not match its catalog provider and worker".format(name))
        if entry["effort"] != "lowest" and entry["effort"] not in models[name][1]["efforts"]:
            _fail("router effort {!r} is not allowed on {}".format(entry["effort"], name))
        _number(entry["timeout_seconds"], "router timeout for " + name)
        if "env" in entry and (not isinstance(entry["env"], dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in entry["env"].items())):
            _fail("router env for {} must map names to strings".format(name))
        if "max_level" in entry and entry["max_level"] not in shown:
            _fail("router max_level for {} must be a shown level key".format(name))
        _chain_options(entry, name, providers, shown, levels)
    if not any(providers[e["provider"]]["kind"] == "local" for e in router["chain"]):
        _fail("router.chain needs a local router for local-only work")

    _object(cat["escalation"], "escalation", ("after",))
    _int(cat["escalation"]["after"], "escalation.after")
    reviewer = _object(cat["reviewer"], "reviewer", ("min_level", "default_effort"))
    if reviewer["min_level"] not in keys or reviewer["default_effort"] not in vocab:
        _fail("reviewer needs a level key and a vocabulary effort")
    policy = _object(cat["review_policy"], "review_policy", ("role_floor", "default_floor", "files_optional_at", "files_required_at",
                                                              "required_from_level", "high_effort_required_from_level"))
    if not isinstance(policy["role_floor"], dict) or any(v not in REVIEW_ORDER for v in policy["role_floor"].values()):
        _fail("review_policy.role_floor must map roles to none, optional or required")
    if policy["default_floor"] not in REVIEW_ORDER:
        _fail("review_policy.default_floor must be none, optional or required")
    _int(policy["files_optional_at"], "review_policy.files_optional_at")
    _int(policy["files_required_at"], "review_policy.files_required_at")
    if policy["files_required_at"] < policy["files_optional_at"]:
        _fail("review_policy.files_required_at must not be below files_optional_at")
    for key in ("required_from_level", "high_effort_required_from_level"):
        if policy[key] not in keys:
            _fail("review_policy.{} must be a level key".format(key))

    _object(cat["review"], "review", REVIEW_ORDER)
    for name in REVIEW_ORDER:
        _text(cat["review"][name], "review." + name)
    if not isinstance(cat["shapes"], dict):
        _fail("shapes must be an object")
    for name, shape in cat["shapes"].items():
        _object(shape, "shape " + name, ("status", "text", "evidence"))
        if shape["status"] not in ("qualified", "candidate"):
            _fail("shape {} status must be qualified or candidate".format(name))
        _text(shape["text"], "shape {} text".format(name))
    for role, rule in cat["roles"].items():
        _object(rule, "role " + role, (), ("min_level", "max_level", "min_effort", "below_floor_shapes"))
        if "below_floor_shapes" in rule:
            listed = rule["below_floor_shapes"]
            if "min_level" not in rule or not isinstance(listed, list) or not listed or any(name not in cat["shapes"] for name in listed):
                _fail("role {}: below_floor_shapes needs min_level and a list of shape keys".format(role))
        for key in ("min_level", "max_level"):
            if key in rule and rule[key] not in keys:
                _fail("role {}: {} {!r} is not a level key".format(role, key, rule[key]))
        if "min_level" in rule and "max_level" in rule and keys.index(rule["min_level"]) > keys.index(rule["max_level"]):
            _fail("role {}: min_level is above max_level".format(role))
        if "min_effort" in rule and rule["min_effort"] not in vocab:
            _fail("role {}: min_effort must be in the vocabulary".format(role))
    for i, entry in enumerate(cat.get("protected_paths") or []):
        name = "protected_paths[{}]".format(i)
        _object(entry, name, ("pattern", "min_level"), ("min_effort", "roles", "why"))
        _text(entry["pattern"], name + " pattern")
        if entry["min_level"] not in keys:
            _fail("{}: min_level {!r} is not a level key".format(name, entry["min_level"]))
        if "min_effort" in entry and entry["min_effort"] not in vocab:
            _fail("{}: min_effort must be in the vocabulary".format(name))
        if "roles" in entry and (not isinstance(entry["roles"], list) or not entry["roles"]
                                 or any(not isinstance(r, str) or not r for r in entry["roles"])):
            _fail("{}: roles must be a list of role names".format(name))
    if "protected_paths" in cat and not isinstance(cat["protected_paths"], list):
        _fail("protected_paths must be a list")
    if not isinstance(cat["principles"], list) or not cat["principles"] or any(
            not isinstance(p, str) or not p.strip() for p in cat["principles"]):
        _fail("principles must be a nonempty list of text")
    try:
        build_system(cat)
    except (KeyError, ValueError, StopIteration) as exc:
        _fail("the router prompt cannot be generated: {!r}".format(exc))
    return cat


def load_catalog(path=None):
    """Load and validate a catalog: the given path, else TP_MODEL_CATALOG (tests point the commands at a fixture), else the shipped one."""
    path = Path(path or os.environ.get("TP_MODEL_CATALOG") or CATALOG_PATH)
    try:
        cat = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError("cannot read model catalog {}: {}".format(path, exc)) from exc
    return validate_catalog(cat)


def catalog_sha(cat):
    return hashlib.sha256(json.dumps(cat, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def prompt_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def level_keys(cat):
    return [lv["key"] for lv in cat["levels"]]


def shown_levels(cat):
    """Levels the router is told about. A level with shown:false stays in the catalog but is never offered."""
    return [lv for lv in cat["levels"] if lv.get("shown", True)]


def shown_keys(cat):
    return [lv["key"] for lv in shown_levels(cat)]


def level_of(cat, model):
    """(level, model entry) for a catalog model name, else (None, None)."""
    for level in cat["levels"]:
        for entry in level["models"]:
            if entry["model"] == model:
                return level, entry
    return None, None


def provider_of(cat, worker, model):
    """Provider name of a model: its catalog provider, else a guess from the harness and name prefix."""
    _, entry = level_of(cat, model)
    if entry:
        return entry["provider"]
    if worker == "claude":
        return next((p for p, v in cat["providers"].items() if v["worker"] == "claude"), "unknown")
    return model.split("/")[0] if "/" in model else "unknown"


def lowest_effort(cat, chain_entry):
    """The effort a router call uses: the chain entry's own, or the model's lowest allowed one."""
    if chain_entry["effort"] != "lowest":
        return chain_entry["effort"]
    return level_of(cat, chain_entry["model"])[1]["efforts"][0]


def _prose(text, cat):
    """Replace {level:KEY} with the shown level's number, so prose never hard-codes a number."""
    numbers = {k: i for i, k in enumerate(shown_keys(cat), 1)}

    def number(match):
        if match.group(1) not in numbers:
            raise ValueError("prose names level {} which is not shown".format(match.group(1)))
        return "Level {}".format(numbers[match.group(1)])
    return PLACEHOLDER.sub(number, text)


def _level_models(level):
    by = {}
    for m in level["models"]:
        by.setdefault(m["provider"], []).append(m["model"].split("/")[-1])
    return "; ".join("{}: {}".format(p, ", ".join(ms)) for p, ms in by.items())


def build_system(cat):
    """The router's system prompt, built only from the catalog."""
    levels = shown_levels(cat)
    vocab = cat["effort"]["vocabulary"]
    shapes = cat["shapes"]
    qualified = [k for k, v in shapes.items() if v["status"] == "qualified"]
    candidates = [k for k, v in shapes.items() if v["status"] != "qualified"]
    out = ["You route software tasks to a model level and an effort level. You do not do the task. "
           "Read the request and reply with one JSON object and nothing else.", "", "OUTPUT (exactly these five fields):",
           '{"level": <integer 1-%d>, "effort": "%s", "review": "none|optional|required", '
           '"shape": "<shape id or null>", "reason": "<one line, at most 140 characters>"}' % (len(levels), "|".join(vocab)),
           "", "LEVELS, lowest to highest:"]
    for i, level in enumerate(levels, 1):
        out += ["", "Level %d - %s (models: %s)" % (i, level["name"], _level_models(level)),
                "  For: " + _prose(level["for"], cat), "  Not for: " + _prose(level["not_for"], cat)]
        if len(set(level["effort_map"].values())) == 1:
            out.append("  Effort has no effect at this level; answer %s." % vocab[0])
        for anchor in level["anchors"]:
            tail = ", shape %s" % anchor["shape"] if anchor.get("shape") else ""
            out.append('  Example: "%s" -> level %d, effort %s%s' % (anchor["task"], i, anchor["effort"], tail))
    out += ["", "EFFORT:"]
    out += ["  %s: %s" % (e, cat["effort"]["meaning"][e]) for e in vocab]
    out.append("  Default: %s. Ceiling: %s. %s" % (cat["effort"]["default"], cat["effort"]["ceiling"], cat["effort"]["ceiling_rule"]))
    gated = next((lv["key"] for lv in levels if lv.get("requires_shape")), None)
    where = " Only the first group may use {}.".format(_prose("{level:%s}" % gated, cat)) if gated else ""
    out += ["", "SHAPES (field `shape`; name the one the task matches, else null.%s):" % where]
    out += ["  %s: %s" % (k, shapes[k]["text"]) for k in qualified]
    if candidates:
        # The shape is still named: role floors can be waived for it (a coder's small named-file edit).
        out.append("  Not yet proven%s; name the shape all the same:" % (" (use the next level up)" if gated else ""))
        out += ["  %s: %s" % (k, shapes[k]["text"]) for k in candidates]
    out += ["", "REVIEW (field `review`):"] + ["  %s: %s" % (k, v) for k, v in cat["review"].items()]
    out += ["", "RULES:"] + ["%d. %s" % (i, _prose(p, cat)) for i, p in enumerate(cat["principles"], 1)]
    shares = ["level %d (%s) %d-%d%%" % (i, lv["name"], round(lv["expected_share"][0] * 100), round(lv["expected_share"][1] * 100))
              for i, lv in enumerate(levels, 1)]
    default = next(i for i, lv in enumerate(levels, 1) if lv["key"] == cat["default_level"])
    out += ["", "EXPECTED MIX over many tasks: " + "; ".join(shares) + ". Most tasks are level %d at effort %s. "
            "A mix far from this usually means you are rating too high." % (default, cat["effort"]["default"])]
    return "\n".join(out)


def user_message(card):
    return "REQUEST TO ROUTE:\n" + card.strip() + "\n\nReply with the JSON object only."


def reply_schema(cat):
    """JSON schema of the router's reply (documentation and tests; the parser below is the authority)."""
    return {
        "type": "object", "additionalProperties": False, "required": sorted(REPLY_FIELDS),
        "properties": {
            "level": {"type": "integer", "minimum": 1, "maximum": len(shown_keys(cat))},
            "effort": {"enum": cat["effort"]["vocabulary"]},
            "review": {"enum": list(REVIEW_ORDER)},
            "shape": {"enum": [None] + list(cat["shapes"])},
            "reason": {"type": "string", "maxLength": 200},
        },
    }


def extract_json(text):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    a, b = t.find("{"), t.rfind("}")
    if a < 0 or b < a:
        raise ValueError("no JSON object")
    return json.loads(t[a:b + 1])


def parse_decision(text, cat):
    """The router's reply as a dict, or ValueError. Strict: exactly the five fields, in range."""
    obj = extract_json(text)
    if not isinstance(obj, dict) or set(obj) != REPLY_FIELDS:
        raise ValueError("fields: {}".format(sorted(obj) if isinstance(obj, dict) else type(obj).__name__))
    if not isinstance(obj["level"], int) or isinstance(obj["level"], bool) or not 1 <= obj["level"] <= len(shown_keys(cat)):
        raise ValueError("level {!r}".format(obj["level"]))
    if obj["effort"] not in cat["effort"]["vocabulary"]:
        raise ValueError("effort {!r}".format(obj["effort"]))
    if obj["review"] not in REVIEW_ORDER:
        raise ValueError("review {!r}".format(obj["review"]))
    if obj["shape"] is not None and obj["shape"] not in cat["shapes"]:
        raise ValueError("shape {!r}".format(obj["shape"]))
    if not isinstance(obj["reason"], str):
        raise ValueError("reason")
    obj["reason"] = obj["reason"][:200]
    return obj
