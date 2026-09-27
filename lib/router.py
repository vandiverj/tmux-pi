"""Local Laya decision adapter and explicit OpenAI-compatible fallback."""

import hashlib
import ipaddress
import json
import math
import os
import re
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
}
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


LOCAL_KEYS = {"routes", "escalation", "mlx", "rules", "prompts"}


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
    if not isinstance(config, dict) or set(config) != required:
        raise RouterError("router config needs exactly {}".format(", ".join(sorted(required))))
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
    for name, question in QUESTIONS.items():
        if question["type"] == "choice":
            names += ["{}:{}".format(name, key) for key in question["criteria"]]
        elif question["type"] == "score":
            names += ["{}:{}".format(name, index) for index in range(len(question["criteria"]))] + [name + ":score"]
        else:
            names.append(name)
    return names


def features(answers):
    values = []
    for name, question in QUESTIONS.items():
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
    return hashlib.sha256(json.dumps(QUESTIONS, sort_keys=True).encode("utf-8")).hexdigest()


def _head(model, name, labels):
    head = model.get(name)
    width = len(feature_names())
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
    if not isinstance(model, dict) or model.get("questions") != questions_digest():
        raise RouterError("Laya model was trained on different questions; run bin/tp-calibrate --fit")
    if model.get("features") != feature_names():
        raise RouterError("Laya model features do not match the questions; run bin/tp-calibrate --fit")
    _head(model, "route", ROUTES)
    _head(model, "intent", QUESTIONS["task_intent"]["criteria"])
    if model["intent"]["labels"] != list(QUESTIONS["task_intent"]["criteria"]):
        raise RouterError("model intent labels must be the task_intent choices in order")
    for name, labels in (("gate", ["none", "review"]), ("level", ["optional", "required"])):
        _head(model, name, labels)
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


def route_laya(task, config, base_url=None, router_model=None, opener=None):
    return laya_decision(classify_laya(task, config, base_url, router_model, opener), config)


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


def route_task(task, config, backend=None, base_url=None, router_model=None, opener=None):
    selected = backend or config["backend"]
    if selected == "laya":
        decision = route_laya(task, config, base_url, router_model, opener)
    elif selected == "openai":
        decision = route_openai(task, config, base_url, router_model, opener)
    else:
        raise RouterError("backend must be laya or openai")
    return apply_rules(task, decision, config)
