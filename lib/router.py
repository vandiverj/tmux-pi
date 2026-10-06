"""Local Laya decision adapter and explicit OpenAI-compatible fallback."""

import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

THINKING = frozenset(("off", "minimal", "low", "medium", "high", "xhigh", "max"))
QUESTIONS = {
    "task_intent": {"type": "choice", "instructions": "What is the primary requested outcome of this task?",
                    "criteria": {
                        "research": "investigate, gather facts, compare options, or answer questions without making a plan or change",
                        "planning": "produce a plan, design, architecture, roadmap, recommendation, or decision for future work",
                        "implementation": "write, modify, debug, refactor, or test software or other project artifacts",
                        "configuration_or_operations": "change or operate configuration, infrastructure, deployments, data, services, or command-line systems",
                        "review": "inspect or evaluate existing work, code, a plan, or a result",
                        "communication_or_documentation": "write, summarize, explain, report status, or make routine documentation",
                    }},
    "difficulty": {"type": "score", "instructions": "How difficult is this software task?",
                   "criteria": ["simple", "routine", "complex", "expert"]},
    "needs_tools": {"type": "noul", "instructions": "Does this task require coding, shell, browsing, or other tools?"},
    "is_sensitive": {"type": "noul", "instructions": "Does this task involve secrets, security, privacy, or sensitive data?"},
    "is_high_risk": {"type": "noul", "instructions": "Could an incorrect result cause material damage or loss?"},
    "changes": {"type": "noul", "instructions": "Will doing this task change files, systems or data, rather than only producing an answer or a document?",
                "criteria": {"false": "no: it only produces an answer, findings, a plan or a document",
                             "true": "yes: it changes code, configuration, systems or data"}},
    "execution_scope": {"type": "choice", "instructions": "How much work does this request take?",
                        "criteria": {
                            "brief": "a quick answer, lookup, explanation or small edit, done in a few steps",
                            "sustained": "a focused piece of work that takes many steps, such as a feature, an investigation or a document",
                            "multi_step": "several distinct pieces of work, or work across several components or systems",
                        }},
}
# The learned layer reads only these questions; a new question joins them only after it
# passes the tune/holdout gate (tests/calibration/experiments.md).
FEATURE_QUESTIONS = ("task_intent", "difficulty", "needs_tools", "is_sensitive", "is_high_risk", "changes")
PLACEMENTS = ("main", "subagent")
MARKERS = {"+here": ("placement", "main"), "+delegate": ("placement", "subagent"), "+review": ("review", "required")}
ROUTES = frozenset(("simple", "standard", "coding", "complex", "expert"))
REVIEWS = ("none", "optional", "required")


class RouterError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def local_open(request, timeout):
    # Do not send task text through a proxy or follow a remote redirect.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(
        request, timeout=timeout
    )


def local_base_url(value, backend="openai"):
    if not isinstance(value, str):
        raise RouterError("base_url must be a string")
    parsed = urllib.parse.urlsplit(value)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise RouterError("base_url has an invalid host or port") from exc
    if parsed.scheme != "http" or not host or not port or parsed.username or parsed.password:
        raise RouterError("base_url must be an unauthenticated loopback http URL with a port")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    path = "" if backend == "laya" else "/v1"
    if not loopback or parsed.query or parsed.fragment or parsed.path.rstrip("/") != path:
        raise RouterError("base_url must be a loopback {} URL".format(
            "server root" if backend == "laya" else "OpenAI-compatible /v1"))
    return value.rstrip("/")


def _model(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RouterError("{} must be a nonempty model name".format(label))
    return value


def validate_decision(value, config):
    if not isinstance(value, dict) or set(value) != {"model", "thinking"}:
        raise RouterError("decision must be a JSON object with exactly model and thinking")
    model, thinking = value["model"], value["thinking"]
    for entry in config["models"]:
        if model == entry["model"]:
            if thinking not in entry["thinking"]:
                raise RouterError("thinking {!r} is not allowed for model {!r}".format(thinking, model))
            return {"worker": entry["worker"], "model": model, "thinking": thinking}
    raise RouterError("model {!r} is not in the configured roster".format(model))


def _target(value, config):
    return validate_decision(value, config)


LOCAL_KEYS = {"routes", "escalation", "mlx", "rules", "prompts", "followup", "packet_files", "redaction"}


def load_local(path, config):
    """Validate a machine's local assignments (config/local.json) against the shared roster.

    Each machine has different models available, so tier assignments, the local
    MLX server and escalation live here instead of in the committed config.
    Keys starting with "_" are comments.
    """
    try:
        local = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouterError("cannot read local config {}: {}".format(path, exc)) from exc
    if not isinstance(local, dict):
        raise RouterError("local config must be a JSON object")
    local = {k: v for k, v in local.items() if not k.startswith("_")}
    if set(local) - LOCAL_KEYS:
        raise RouterError("local config keys must be among {}".format(", ".join(sorted(LOCAL_KEYS))))
    routes = local.get("routes", {})
    if not isinstance(routes, dict) or set(routes) - ROUTES:
        raise RouterError("local routes must be among {}".format(", ".join(sorted(ROUTES))))
    for name, target in routes.items():
        try:
            _target(target, config)
        except RouterError as exc:
            raise RouterError("invalid local {} route: {}".format(name, exc)) from exc
    redaction = local.get("redaction")
    if redaction is not None and (not isinstance(redaction, dict) or set(redaction) != {"mode"} or
                                  redaction["mode"] not in ("off", "shadow", "on")):
        raise RouterError("redaction needs exactly mode, one of off, shadow or on")
    escalation = local.get("escalation")
    if escalation is not None:
        if not isinstance(escalation, dict) or set(escalation) != {"models", "after_consecutive_errors", "to"}:
            raise RouterError("escalation needs exactly models, after_consecutive_errors and to")
        after = escalation["after_consecutive_errors"]
        if isinstance(after, bool) or not isinstance(after, int) or not 1 <= after <= 20:
            raise RouterError("escalation after_consecutive_errors must be an integer from 1 to 20")
        roster = {item["model"] for item in config["models"]}
        if not isinstance(escalation["models"], list) or not escalation["models"] or \
                any(model not in roster for model in escalation["models"]):
            raise RouterError("escalation models must be a nonempty list of roster models")
        try:
            _target(escalation["to"], config)
        except RouterError as exc:
            raise RouterError("invalid escalation target: {}".format(exc)) from exc
    rules = local.get("rules", [])
    if not isinstance(rules, list):
        raise RouterError("local rules must be a list")
    roster = {item["model"] for item in config["models"]}
    for index, rule in enumerate(rules):
        rule = {k: v for k, v in rule.items() if not k.startswith("_")} if isinstance(rule, dict) else rule
        if not isinstance(rule, dict) or set(rule) != {"pattern", "from", "to"}:
            raise RouterError("rule {} needs exactly pattern, from and to".format(index))
        try:
            re.compile(rule["pattern"])
        except (re.error, TypeError) as exc:
            raise RouterError("rule {} pattern is not a valid regular expression: {}".format(index, exc)) from exc
        if not isinstance(rule["from"], list) or not rule["from"] or any(m not in roster for m in rule["from"]):
            raise RouterError("rule {} from must be a nonempty list of roster models".format(index))
        try:
            _target(rule["to"], config)
        except RouterError as exc:
            raise RouterError("invalid rule {} target: {}".format(index, exc)) from exc
    prompts = local.get("prompts", {})
    if not isinstance(prompts, dict) or any(model not in roster for model in prompts):
        raise RouterError("local prompts must map roster models to text")
    if any(not isinstance(text, str) or not text.strip() for text in prompts.values()):
        raise RouterError("each local prompt must be nonempty text")
    mlx = local.get("mlx")
    if mlx is not None:
        if not isinstance(mlx, dict) or not {"model", "port"} <= set(mlx) or \
                set(mlx) - {"model", "port", "chat_template_args", "temp", "top_p", "top_k"}:
            raise RouterError("mlx needs model and port, and may set chat_template_args, temp, top_p, top_k")
        port = mlx["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise RouterError("mlx port must be an integer from 1024 to 65535")
        if not isinstance(mlx["model"], str) or not mlx["model"]:
            raise RouterError("mlx model must be a Hugging Face id or a local path")
    followup = local.get("followup")
    if followup is not None:
        if not isinstance(followup, dict) or not {"base_url", "model", "ask_between"} <= set(followup) or \
                set(followup) - {"base_url", "model", "ask_between", "extra_body"}:
            raise RouterError("followup needs base_url, model and ask_between, and may set extra_body")
        extra = followup.get("extra_body", {})
        if not isinstance(extra, dict) or set(extra) & FOLLOW_UP_BODY_KEYS:
            raise RouterError("followup extra_body must be an object that does not set {}".format(
                ", ".join(sorted(FOLLOW_UP_BODY_KEYS))))
        local_base_url(followup["base_url"])
        _model(followup["model"], "followup model")
        band = followup["ask_between"]
        if not isinstance(band, list) or len(band) != 2 or \
                not 0 <= _number(band[0], "ask_between[0]") < _number(band[1], "ask_between[1]") <= 1:
            raise RouterError("followup ask_between must be [low, high] with 0 <= low < high <= 1")
    packet_files = local.get("packet_files")
    if packet_files is not None:
        if not isinstance(packet_files, dict) or set(packet_files) != {"models", "selector"}:
            raise RouterError("packet_files needs exactly models and selector")
        if not isinstance(packet_files["models"], list) or not packet_files["models"] or \
                any(model not in roster for model in packet_files["models"]):
            raise RouterError("packet_files models must be a nonempty list of roster models")
        selector = packet_files["selector"]
        if not isinstance(selector, str) or not re.fullmatch(r"claude-[a-z0-9.-]+", selector):
            raise RouterError("packet_files selector must be a Claude model id")
    return local


def load_config(path, local_path=None):
    """Load the shared router config, then apply this machine's local assignments if present.

    local_path defaults to local.json next to the config (or $TP_LOCAL_CONFIG); pass
    False to ignore local assignments.
    """
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouterError("cannot read router config: {}".format(exc)) from exc
    required = {"backend", "laya", "openai", "timeout_seconds", "models", "routes", "laya_model"}
    if not isinstance(config, dict) or not required <= set(config) or set(config) - required - {"claude_preference"}:
        raise RouterError("router config needs exactly {} (and may set claude_preference)".format(
            ", ".join(sorted(required))))
    if config["backend"] not in ("laya", "openai"):
        raise RouterError("backend must be laya or openai")
    for backend in ("laya", "openai"):
        service = config[backend]
        if not isinstance(service, dict) or set(service) != {"base_url", "model"}:
            raise RouterError("{} needs exactly base_url and model".format(backend))
        local_base_url(service["base_url"], backend)
        _model(service["model"], "{} model".format(backend))
    if config["laya"]["model"] != "typed-decisions":
        raise RouterError("laya model must be typed-decisions")
    timeout = config["timeout_seconds"]
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or not 0 < timeout <= 300):
        raise RouterError("timeout_seconds must be a finite number between 0 and 300")
    models = config["models"]
    if not isinstance(models, list) or not models:
        raise RouterError("models must be a nonempty array")
    seen = set()
    for item in models:
        if not isinstance(item, dict) or set(item) != {"worker", "model", "thinking"}:
            raise RouterError("each model needs exactly worker, model, thinking")
        worker, model, levels = item["worker"], item["model"], item["thinking"]
        if worker not in ("pi", "claude"):
            raise RouterError("model worker must be pi or claude")
        _model(model, "roster model")
        if worker == "pi" and "/" not in model:
            raise RouterError("pi model must be provider/model for tp-spawn")
        if model in seen:
            raise RouterError("duplicate model in roster: {}".format(model))
        seen.add(model)
        if (not isinstance(levels, list) or not levels or
                any(not isinstance(level, str) or level not in THINKING for level in levels) or
                len(levels) != len(set(levels))):
            raise RouterError("thinking must be a nonempty array of distinct tp-spawn levels")
    if not isinstance(config["routes"], dict) or set(config["routes"]) != ROUTES:
        raise RouterError("routes must define exactly {}".format(", ".join(sorted(ROUTES))))
    for name, target in config["routes"].items():
        try:
            _target(target, config)
        except RouterError as exc:
            raise RouterError("invalid {} route: {}".format(name, exc)) from exc
    if "claude_preference" in config:
        _check_claude_preference(config["claude_preference"], config)
    if not isinstance(config["laya_model"], str) or not config["laya_model"]:
        raise RouterError("laya_model must be a path relative to the router config")
    config["model_path"] = Path(path).parent / config["laya_model"]
    if config["backend"] == "laya" or config["model_path"].exists():
        config["model"] = load_model(config["model_path"])
    if local_path is None:
        local_path = os.environ.get("TP_LOCAL_CONFIG") or Path(path).parent / "local.json"
    config["local"] = {}
    if local_path and Path(local_path).exists():
        config["local"] = load_local(local_path, config)
        config["routes"] = {**config["routes"], **config["local"].get("routes", {})}
    return config



def provider_models(config, provider, base_url=None):
    """Model ids (provider prefix removed) this machine's config can send to a pi provider.

    Covers the routes, the local escalation target and rule targets, plus the follow-up
    judge's model when it is served from base_url (the provider's own URL). tp-lmstudio
    loads these before workers need them.
    """
    local = config["local"]
    targets = list(config["routes"].values()) + [rule["to"] for rule in local.get("rules", [])]
    if local.get("escalation"):
        targets.append(local["escalation"]["to"])
    prefix = provider + "/"
    ids = [target["model"][len(prefix):] for target in targets if target["model"].startswith(prefix)]
    judge = local.get("followup")
    if judge and base_url and local_base_url(judge["base_url"]) == local_base_url(base_url):
        ids.append(judge["model"])
    return list(dict.fromkeys(ids))

def _post(endpoint, body, timeout, opener):
    request = urllib.request.Request(endpoint, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or local_open)(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if not 200 <= status < 300:
                raise RouterError("router endpoint returned HTTP {} at {}".format(status, endpoint))
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise RouterError("router endpoint returned HTTP {} at {}".format(exc.code, endpoint)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RouterError("router endpoint unavailable at {}: {}".format(endpoint, exc)) from exc
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RouterError("router returned malformed JSON: {}".format(exc)) from exc


def _number(value, name, maximum=1):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RouterError("{} must be a finite number".format(name))
    if not 0 <= value <= maximum:
        raise RouterError("{} must be between 0 and {}".format(name, maximum))
    return value


def laya_answers(reply):
    """Validate a typed-decisions reply and return its answers."""
    if not isinstance(reply, dict) or not isinstance(reply.get("answers"), dict):
        raise RouterError("Laya response needs an answers object")
    if not isinstance(reply.get("routing"), dict) or reply["routing"].get("model") != "typed-decisions":
        raise RouterError("Laya response was not routed through typed-decisions")
    answers = reply["answers"]
    if any(name not in answers for name in QUESTIONS):
        raise RouterError("Laya response is missing required answers")
    for name, question in QUESTIONS.items():
        answer = answers[name]
        field = question["type"]
        if not isinstance(answer, dict) or field not in answer:
            raise RouterError("Laya answer {} needs {}".format(name, field))
        if "type" in answer and answer["type"] != question["type"]:
            raise RouterError("Laya answer {} has the wrong type".format(name))
        if field == "choice":
            if not isinstance(answer[field], str) or answer[field] not in question["criteria"]:
                raise RouterError("Laya answer {} has an invalid choice".format(name))
        else:
            _number(answer[field], "{}.{}".format(name, field),
                    len(question["criteria"]) - 1 if field == "score" else 1)
        if "confidence" in answer:
            _number(answer["confidence"], "{}.confidence".format(name))
        if field != "noul":
            probs = answer.get("probabilities")
            if not isinstance(probs, dict):
                raise RouterError("Laya answer {} needs probabilities".format(name))
            expected = ({str(index) for index in range(len(question["criteria"]))} if field == "score"
                        else set(question["criteria"]))
            if set(probs) != expected:
                raise RouterError("{}.probabilities is incomplete".format(name))
            for key, probability in probs.items():
                _number(probability, "{}.probabilities.{}".format(name, key))
    return answers


def feature_names():
    """Names of the model inputs, in order: choice and score probabilities, the score, noul values."""
    names = []
    for name in FEATURE_QUESTIONS:
        question = QUESTIONS[name]
        if question["type"] == "choice":
            names += ["{}:{}".format(name, key) for key in question["criteria"]]
        elif question["type"] == "score":
            names += ["{}:{}".format(name, index) for index in range(len(question["criteria"]))] + [name + ":score"]
        else:
            names.append(name)
    return names


def features(answers):
    values = []
    for name in FEATURE_QUESTIONS:
        question = QUESTIONS[name]
        answer = answers[name]
        if question["type"] == "choice":
            values += [answer["probabilities"][key] for key in question["criteria"]]
        elif question["type"] == "score":
            levels = len(question["criteria"])
            values += [answer["probabilities"][str(index)] for index in range(levels)]
            values.append(answer["score"] / (levels - 1))
        else:
            values.append(answer["noul"])
    return values


def questions_digest():
    """Digest of every question asked; recorded answers are current only for the same questions."""
    return hashlib.sha256(json.dumps(QUESTIONS, sort_keys=True).encode("utf-8")).hexdigest()


def model_digest():
    """Digest of the questions the learned layer reads; the model is valid only for these."""
    feature_questions = {name: QUESTIONS[name] for name in FEATURE_QUESTIONS}
    return hashlib.sha256(json.dumps(feature_questions, sort_keys=True).encode("utf-8")).hexdigest()


def _head(model, name, labels, extra=0):
    head = model.get(name)
    width = len(feature_names()) + extra
    if not isinstance(head, dict) or set(head) - {"threshold", "lambda"} != {"labels", "mean", "scale", "weights", "bias"}:
        raise RouterError("model {} needs labels, mean, scale, weights and bias".format(name))
    if not isinstance(head["labels"], list) or not head["labels"] or not set(head["labels"]) <= set(labels):
        raise RouterError("model {} labels must be among {}".format(name, ", ".join(sorted(labels))))
    classes = len(head["labels"])
    numbers = lambda v, n: isinstance(v, list) and len(v) == n and all(
        not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x) for x in v)
    if not (numbers(head["mean"], width) and numbers(head["scale"], width) and numbers(head["bias"], classes)
            and isinstance(head["weights"], list) and len(head["weights"]) == width
            and all(numbers(row, classes) for row in head["weights"]) and all(x > 0 for x in head["scale"])):
        raise RouterError("model {} has the wrong shape for {} features".format(name, width))


def load_model(path):
    """Load the learned layer that turns Laya's answers into a route and a review level."""
    try:
        model = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouterError("cannot read Laya model: {}".format(exc)) from exc
    if not isinstance(model, dict) or model.get("questions") != model_digest():
        raise RouterError("Laya model was trained on different questions; run bin/tp-calibrate --fit")
    if model.get("features") != feature_names():
        raise RouterError("Laya model features do not match the questions; run bin/tp-calibrate --fit")
    _head(model, "route", ROUTES)
    _head(model, "intent", QUESTIONS["task_intent"]["criteria"])
    if model["intent"]["labels"] != list(QUESTIONS["task_intent"]["criteria"]):
        raise RouterError("model intent labels must be the task_intent choices in order")
    for name, labels in (("gate", ["none", "review"]), ("level", ["optional", "required"]),
                         ("placement", list(PLACEMENTS))):
        _head(model, name, labels, extra=len(QUESTIONS["execution_scope"]["criteria"]) if name == "placement" else 0)
        head = model[name]
        if head["labels"] != labels:
            raise RouterError("model {} labels must be {}".format(name, ", ".join(labels)))
        threshold = head.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 < threshold < 1:
            raise RouterError("model {} threshold must be between 0 and 1".format(name))
    return model


def _probabilities(head, values):
    z = [head["bias"][k] + sum((v - m) / s * row[k] for v, m, s, row in
                                zip(values, head["mean"], head["scale"], head["weights"]))
         for k in range(len(head["labels"]))]
    top = max(z)
    exp = [math.exp(x - top) for x in z]
    return [x / sum(exp) for x in exp]


def laya_route(reply, model):
    """Route name for a typed-decisions reply: the learned layer's most probable route."""
    probs = _probabilities(model["route"], features(laya_answers(reply)))
    return model["route"]["labels"][probs.index(max(probs))]


def laya_intent(reply, model):
    """The task's intent: the learned layer's reading of Laya's answers, which beats Laya's own choice."""
    head = model["intent"]
    probs = _probabilities(head, features(laya_answers(reply)))
    return head["labels"][probs.index(max(probs))]


def laya_decision(reply, config):
    return _target(config["routes"][laya_route(reply, config["model"])], config)


# Experimental Claude-branch preference (config claude_preference). It only re-picks among
# Claude models for a Laya decision that is already a Claude worker: it never moves a pi/GPT
# route to Claude, and never touches intent, review, placement or any authority.
HEAVY_KEYS = {"difficulty_at_least", "high_risk_at_least", "sensitive_at_least", "scopes"}


def _check_claude_preference(value, config):
    if not isinstance(value, dict) or set(value) != {"ordinary", "heavy", "heavy_when"}:
        raise RouterError("claude_preference needs exactly ordinary, heavy and heavy_when")
    for tier in ("ordinary", "heavy"):
        try:
            target = _target(value[tier], config)
        except RouterError as exc:
            raise RouterError("invalid claude_preference {}: {}".format(tier, exc)) from exc
        if target["worker"] != "claude":
            raise RouterError("claude_preference {} must be a Claude model".format(tier))
    when = value["heavy_when"]
    if not isinstance(when, dict) or set(when) != HEAVY_KEYS:
        raise RouterError("claude_preference heavy_when needs exactly {}".format(", ".join(sorted(HEAVY_KEYS))))
    _number(when["difficulty_at_least"], "heavy_when.difficulty_at_least", len(QUESTIONS["difficulty"]["criteria"]) - 1)
    _number(when["high_risk_at_least"], "heavy_when.high_risk_at_least")
    _number(when["sensitive_at_least"], "heavy_when.sensitive_at_least")
    if not isinstance(when["scopes"], list) or set(when["scopes"]) - set(QUESTIONS["execution_scope"]["criteria"]):
        raise RouterError("heavy_when.scopes must list execution_scope choices")


def claude_preference(decision, answers, config):
    """(decision, record) after the Claude-branch preference; record is None when it does not apply.

    A Claude decision is heavy (preference heavy target) when any Laya trait crosses
    heavy_when; otherwise it is ordinary. Non-Claude decisions pass through unchanged.
    """
    pref = config.get("claude_preference")
    if not pref or decision["worker"] != "claude":
        return decision, None
    when = pref["heavy_when"]
    reasons = []
    if answers["difficulty"]["score"] >= when["difficulty_at_least"]:
        reasons.append("difficulty")
    if answers["is_high_risk"]["noul"] >= when["high_risk_at_least"]:
        reasons.append("high_risk")
    if answers["is_sensitive"]["noul"] >= when["sensitive_at_least"]:
        reasons.append("sensitive")
    if answers["execution_scope"]["choice"] in when["scopes"]:
        reasons.append("scope")
    tier = "heavy" if reasons else "ordinary"
    chosen = _target(pref[tier], config)
    return chosen, {"tier": tier, "reasons": reasons, "from": decision, "changed": chosen != decision}


CHANGE_INTENTS = ("implementation", "configuration_or_operations")


def review_policy(reply, model, force=False):
    """Return none, optional or required review for a typed-decisions reply.

    Research changes nothing, so it is never reviewed. Implementation and
    configuration/operations work, and anything Laya calls sensitive, is always
    reviewed. Otherwise the learned gate decides whether to review. A reviewed
    task is required unless the level head is confident it is optional.
    """
    if force:
        return "required"
    answers = laya_answers(reply)
    intent = laya_intent(reply, model)
    if intent == "research":
        return "none"
    values = features(answers)
    gate, level = model["gate"], model["level"]
    review = (intent in CHANGE_INTENTS or answers["is_sensitive"]["noul"] >= 0.5
              or _probabilities(gate, values)[1] >= gate["threshold"])
    if not review:
        return "none"
    return "required" if _probabilities(level, values)[1] >= level["threshold"] else "optional"


def classify_laya(task, config, base_url=None, router_model=None, opener=None):
    """Ask Laya's typed-decisions model the routing questions; return its raw reply."""
    if not isinstance(task, str) or not task.strip():
        raise RouterError("task must contain text")
    endpoint = local_base_url(base_url or config["laya"]["base_url"], "laya") + "/v1/systemone"
    model = router_model or config["laya"]["model"]
    if model != "typed-decisions":
        raise RouterError("laya model must be typed-decisions")
    body = {"model": model, "state": {"request": task}, "questions": QUESTIONS}
    return _post(endpoint, body, config["timeout_seconds"], opener)


def review_task(task, config, base_url=None, router_model=None, opener=None, force=False):
    """Review level for a task; force skips the classifier."""
    if force:
        return "required"
    return review_policy(classify_laya(task, config, base_url, router_model, opener), config["model"])


def route_laya(task, config, base_url=None, router_model=None, opener=None, trace=None):
    reply = classify_laya(task, config, base_url, router_model, opener)
    laya_target = laya_decision(reply, config)
    decision, preference = claude_preference(laya_target, laya_answers(reply), config)
    if trace is not None:
        trace.update(reply=reply, route=laya_route(reply, config["model"]), laya_target=laya_target,
                     preference=preference)
    return decision


def route_openai(task, config, base_url=None, router_model=None, opener=None):
    if not isinstance(task, str) or not task.strip():
        raise RouterError("task must contain text")
    endpoint = local_base_url(base_url or config["openai"]["base_url"]) + "/chat/completions"
    model = _model(router_model or config["openai"]["model"], "openai model")
    roster = [{"model": item["model"], "thinking": item["thinking"]} for item in config["models"]]
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Choose one route for the user's task. Return only a JSON object with exactly two string fields: model and thinking. The model and thinking combination must match the roster. No markdown or explanation. Roster: " + json.dumps(roster, separators=(",", ":"))},
            {"role": "user", "content": task},
        ],
        "stream": False, "temperature": 0, "max_tokens": 128,
    }
    reply = _post(endpoint, body, config["timeout_seconds"], opener)
    try:
        choice = reply["choices"][0]
        content = choice["message"]["content"]
        if choice.get("finish_reason") != "stop" or not isinstance(content, str):
            raise ValueError("completion was incomplete or had no text content")
        decision = json.loads(content)
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        raise RouterError("router returned invalid JSON completion: {}".format(exc)) from exc
    return validate_decision(decision, config)


def apply_rules(task, decision, config):
    """Redirect a decision when a local rule names its model and the task text matches.

    Rules cover task types a model is known to get wrong (see tests/calibration/experiments.md);
    the first matching rule wins.
    """
    for rule in config.get("local", {}).get("rules", []):
        if decision["model"] in rule["from"] and re.search(rule["pattern"], task):
            return _target(rule["to"], config)
    return decision


def parse_markers(text):
    """Split leading +here/+delegate/+review markers from a request; return (overrides, text)."""
    overrides = {}
    words = text.lstrip().split(None, 1)
    while words and words[0].lower() in MARKERS:
        key, value = MARKERS[words[0].lower()]
        overrides[key] = value
        words = words[1].lstrip().split(None, 1) if len(words) > 1 else []
    return overrides, " ".join(words)


def placement_features(answers):
    """The learned inputs plus Laya's execution-scope probabilities, for the placement head."""
    scope = answers["execution_scope"]["probabilities"]
    return features(answers) + [scope[key] for key in QUESTIONS["execution_scope"]["criteria"]]


def placement_policy(reply, model, intent, review):
    """Where a request runs: main or a subagent.

    Change-making work and anything needing a required review always goes to a
    subagent. Otherwise the learned placement head decides; its threshold is set
    low because keeping delegable work in main is the costlier mistake.
    """
    if intent in CHANGE_INTENTS or review == "required":
        return "subagent"
    head = model["placement"]
    return "subagent" if _probabilities(head, placement_features(laya_answers(reply)))[1] >= head["threshold"] else "main"


def dispatch(text, config, base_url=None, router_model=None, opener=None, trace=None):
    """Classify one request for the main agent: route, intent, review, scope and placement.

    Markers at the start of the request override placement or review; the route is
    always a validated roster target. A trace dict, when given, receives the raw Laya
    reply and the route before the Claude preference and rules (for eval logging only).
    """
    overrides, task = parse_markers(text)
    if not task.strip():
        raise RouterError("task must contain text")
    reply = classify_laya(task, config, base_url, router_model, opener)
    answers = laya_answers(reply)
    model = config["model"]
    route = laya_route(reply, model)
    laya_target = _target(config["routes"][route], config)
    preferred, preference = claude_preference(laya_target, answers, config)
    decision = apply_rules(task, preferred, config)
    if trace is not None:
        trace.update(reply=reply, route=route, laya_target=laya_target, preference=preference,
                     rule_applied=decision != preferred)
    intent = laya_intent(reply, model)
    review = overrides.get("review") or review_policy(reply, model)
    scope = answers["execution_scope"]["choice"]
    placement = overrides.get("placement") or placement_policy(reply, model, intent, review)
    return {**decision, "route": route, "intent": intent, "review": review, "scope": scope,
            "placement": placement, "overrides": sorted(overrides), "task": task}


def route_task(task, config, backend=None, base_url=None, router_model=None, opener=None, trace=None):
    selected = backend or config["backend"]
    if selected == "laya":
        decision = route_laya(task, config, base_url, router_model, opener, trace)
    elif selected == "openai":
        decision = route_openai(task, config, base_url, router_model, opener)
    else:
        raise RouterError("backend must be laya or openai")
    ruled = apply_rules(task, decision, config)
    if trace is not None:
        trace["rule_applied"] = ruled != decision
    return ruled


# Follow-up routing: does a new message belong to a delegated task that is still running?
# Laya separates these poorly (AUC 0.89; most messages land in its ask band), so a machine
# with a fast local chat model uses it instead (Qwen3.6-35B-A3B: 54/56 on tests/calibration/followups.jsonl).
FOLLOW_UP_QUESTION = {"type": "score", "instructions": "How related is the new message to the active task?",
                      "criteria": ["unrelated", "loosely related", "same work"]}
# Laya's score (0-2) is halved to a probability; between these it is unsure (from the
# followups.jsonl tune split: no errors outside).
LAYA_FOLLOW_UP_BAND = (0.3, 0.73)
# With several running tasks, the task that claims a message gets it only when every other
# task is this far below it.
FOLLOW_UP_MARGIN = 0.15
# The judge request's own fields; extra_body (e.g. reasoning_effort for a model that thinks
# by default) may add others but not change these.
FOLLOW_UP_BODY_KEYS = {"model", "temperature", "max_tokens", "logprobs", "top_logprobs", "messages"}
FOLLOW_UP_PROMPT = (
    "A worker is doing a task for the user. The user just sent a new message. Decide whether the message is "
    "meant for that worker (it answers the worker's question, corrects it, asks about its work, or extends or "
    "continues the same task) or is a separate request (a different question or piece of work, even in the same "
    "project). Reply with exactly one word: worker or separate.")


def _clip(text, limit=1500, tail=False):
    text = text.strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:] if tail else text[:limit] + "…"


def follow_up_judge(message, task, reply, config, opener=None):
    """Return (probability the message is for this task's worker, (low, high) unsure band)."""
    judge = config["local"].get("followup")
    if not judge:
        body = {"model": "typed-decisions", "questions": {"follow_up": FOLLOW_UP_QUESTION},
                "state": {"active_task": _clip(task), "worker_last_reply": _clip(reply, tail=True),
                          "new_message": message}}
        endpoint = local_base_url(config["laya"]["base_url"], "laya") + "/v1/systemone"
        reply_json = _post(endpoint, body, config["timeout_seconds"], opener)
        try:
            score = _number(reply_json["answers"]["follow_up"]["score"], "follow_up.score", 2)
        except (KeyError, TypeError) as exc:
            raise RouterError("Laya follow-up answer is missing its score") from exc
        return score / 2, LAYA_FOLLOW_UP_BAND
    user = "Worker's task: {}\nWorker's last reply: {}\nUser's new message: {}".format(
        _clip(task), _clip(reply, tail=True), message)
    body = dict(judge.get("extra_body", {}), model=judge["model"], temperature=0, max_tokens=3, logprobs=True,
                top_logprobs=5, messages=[{"role": "system", "content": FOLLOW_UP_PROMPT}, {"role": "user", "content": user}])
    out = _post(local_base_url(judge["base_url"]) + "/chat/completions", body, config["timeout_seconds"], opener)
    try:
        top = out["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
        mass = {"worker": 0.0, "separate": 0.0}
        for item in top:
            token = item["token"].strip().lstrip("\u0120\u2581").lower()
            for word in mass:
                if token and word.startswith(token):
                    mass[word] += math.exp(item["logprob"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RouterError("follow-up judge returned no logprobs: {}".format(exc)) from exc
    total = mass["worker"] + mass["separate"]
    return (mass["worker"] / total if total else 0.5), tuple(judge["ask_between"])


def follow_up(message, tasks, config, opener=None):
    """Pick the running task a message belongs to.

    tasks is a list of {"id", "task", "reply"}. Returns {"target": id or None, "ask": [ids]}:
    a target when one task claims the message and no other comes near it, ask (most likely
    first) when the user must choose, neither when every task rejects it (a new request).
    """
    overrides, text = parse_markers(message)
    if not text.strip() or overrides.get("placement") or not tasks:
        return {"target": None, "ask": []}
    judged = {item["id"]: follow_up_judge(text, item["task"], item.get("reply", ""), config, opener)
              for item in tasks}
    ranked = sorted(judged, key=lambda key: -judged[key][0])
    best, (low, high) = judged[ranked[0]]
    rival = judged[ranked[1]][0] if len(ranked) > 1 else 0.0
    if best >= high and rival <= best - FOLLOW_UP_MARGIN:
        return {"target": ranked[0], "ask": []}
    return {"target": None, "ask": [key for key in ranked if judged[key][0] > judged[key][1][0]]}


# Packet files for workers that explore poorly (M8). A Claude model picks files from the
# workdir's file list; the worker gets them in its packet (tp-spawn -c). On 30 chores that
# do not name their files, Qwen3.5-4B went from 44/60 to 53/60 with Sonnet-picked files;
# Slotstream was unchanged (28/30). Picks by the local model were right less often
# (18-20/30 against 26/30). See tests/calibration/experiments.md.
PICK_PROMPT = ("A worker agent will do the task below in this repository. Choose the files it should be given up front: "
               "at most {n}, the ones most likely to contain what the task needs. Reply with only the paths, one per line."
               "\n\nTask: {task}\n\nFiles:\n{files}")
MAX_PICKED_FILES = 3
MAX_PICKED_CHARS = 16000
MAX_LISTED_FILES = 1500
SKIPPED_DIRS = {".git", ".tmux-pi", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
# The same names bin/tp-spawn refuses as credentials.
SECRET_NAMES = re.compile(r"^(\.env|\.env\..*|.*\.pem|.*\.key|.*\.p12|.*\.pfx|id_rsa.*|id_ed25519.*|id_ecdsa.*|"
                          r"\.netrc|\.npmrc|\.pypirc|auth\.json|credentials.*|.*secret.*)$")


def workdir_files(workdir, runner=subprocess.run):
    """Text-candidate files in a workdir: git's tracked and untracked-not-ignored files, else a walk."""
    r = runner(["git", "-C", workdir, "ls-files", "-co", "--exclude-standard"], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        paths = r.stdout.splitlines()
    else:
        paths = []
        for root, dirs, names in os.walk(workdir):
            dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
            paths += [os.path.relpath(os.path.join(root, n), workdir) for n in names]
            if len(paths) > MAX_LISTED_FILES:
                break
    keep = []
    for p in sorted(paths):
        full = os.path.join(workdir, p)
        if set(Path(p).parts) & SKIPPED_DIRS or SECRET_NAMES.match(os.path.basename(p)) or not os.path.isfile(full):
            continue
        keep.append(p)
    return keep[:MAX_LISTED_FILES]


def _text_spec(workdir, path):
    """A tp-spawn -c spec for a text file, cut to its first MAX_PICKED_CHARS; None for binary or empty files."""
    try:
        text = Path(workdir, path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.strip():
        return None
    if len(text) <= MAX_PICKED_CHARS:
        return path
    lines = max(1, text[:MAX_PICKED_CHARS].count("\n"))
    return "{}:1-{}".format(path, lines)


def pick_files(task, workdir, model, config, runner=subprocess.run):
    """tp-spawn -c specs for a worker on `model`, or [] when that model gets no picked files."""
    packet_files = config["local"].get("packet_files")
    if not packet_files or model not in packet_files["models"]:
        return []
    files = workdir_files(workdir, runner)
    if not files:
        return []
    listing = "\n".join("{} ({} bytes)".format(p, os.path.getsize(os.path.join(workdir, p))) for p in files)
    prompt = PICK_PROMPT.format(n=MAX_PICKED_FILES, task=_clip(task, 4000), files=listing)
    # No tools, settings or MCP servers: the selector only reads the list it is given.
    r = runner(["claude", "-p", "--setting-sources", "", "--strict-mcp-config", "--tools", "",
                "--model", packet_files["selector"], prompt],
               capture_output=True, text=True, timeout=120, cwd=tempfile.gettempdir())
    if r.returncode != 0:
        raise RouterError("file selector failed: {}".format((r.stderr or r.stdout).strip()[:300]))
    known, picked = set(files), []
    for line in r.stdout.splitlines():
        p = line.strip().strip("`*-• ").strip()
        p = p[2:] if p.startswith("./") else p
        if p in known and p not in picked:
            picked.append(p)
    specs = [_text_spec(workdir, p) for p in picked[:MAX_PICKED_FILES]]
    return [spec for spec in specs if spec]
