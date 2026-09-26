"""Local Laya decision adapter and explicit OpenAI-compatible fallback."""

import ipaddress
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

THINKING = frozenset(("off", "minimal", "low", "medium", "high", "xhigh", "max"))
QUESTIONS = {
    "difficulty": {"type": "score", "instructions": "How difficult is this software task?",
                   "criteria": ["simple", "routine", "complex", "expert"]},
    "needs_tools": {"type": "noul", "instructions": "Does this task require coding, shell, browsing, or other tools?"},
    "needs_private_context": {"type": "noul", "instructions": "Does this task require private project context?"},
    "is_sensitive": {"type": "noul", "instructions": "Does this task involve secrets, security, privacy, or sensitive data?"},
    "is_high_risk": {"type": "noul", "instructions": "Could an incorrect result cause material damage or loss?"},
}
ROUTES = frozenset(("simple", "standard", "coding", "complex", "expert", "escalated", "fallback"))


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


def load_config(path):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RouterError("cannot read router config: {}".format(exc)) from exc
    required = {"backend", "laya", "openai", "timeout_seconds", "models", "routes"}
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


def laya_decision(reply, config):
    if not isinstance(reply, dict) or not isinstance(reply.get("answers"), dict):
        raise RouterError("Laya response needs an answers object")
    if not isinstance(reply.get("routing"), dict) or reply["routing"].get("model") != "typed-decisions":
        raise RouterError("Laya response was not routed through typed-decisions")
    answers = reply["answers"]
    if any(name not in answers for name in QUESTIONS):
        raise RouterError("Laya response is missing required answers")
    values = {}
    bad_distribution = False
    for name, question in QUESTIONS.items():
        answer = answers[name]
        field = "score" if question["type"] == "score" else "noul"
        if not isinstance(answer, dict) or field not in answer:
            raise RouterError("Laya answer {} needs {}".format(name, field))
        if "type" in answer and answer["type"] != question["type"]:
            raise RouterError("Laya answer {} has the wrong type".format(name))
        values[name] = _number(answer[field], "{}.{}".format(name, field),
                               len(question["criteria"]) - 1 if field == "score" else 1)
        if "confidence" in answer:
            _number(answer["confidence"], "{}.confidence".format(name))
        if "probabilities" in answer:
            probs = answer["probabilities"]
            if not isinstance(probs, dict) or not probs:
                raise RouterError("{}.probabilities must be a nonempty object".format(name))
            if field == "score" and set(probs) != {str(index) for index in range(len(question["criteria"]))}:
                raise RouterError("{}.probabilities is incomplete".format(name))
            for key, probability in probs.items():
                _number(probability, "{}.probabilities.{}".format(name, key))
            if abs(sum(probs.values()) - 1) > 0.02:
                bad_distribution = True
    # These thresholds are policy heuristics, not calibrated task probabilities.
    # Laya's typed-decisions score-temperature warning makes difficulty.confidence
    # unsuitable for routing; validate it above, but do not use it as a gate.
    if values["is_sensitive"] >= 0.35:
        route = "escalated"
    elif values["is_high_risk"] >= 0.35 and values["difficulty"] >= 2.25:
        route = "expert"
    elif values["is_high_risk"] >= 0.35:
        route = "escalated"
    elif bad_distribution or any(0.35 <= values[name] <= 0.65 for name in QUESTIONS if name != "difficulty"):
        route = "fallback"
    elif values["difficulty"] >= 2.75:
        route = "expert"
    elif values["difficulty"] >= 2.25:
        route = "complex"
    elif values["difficulty"] >= 1.25:
        route = "coding"
    elif values["needs_tools"] < 0.35 and values["needs_private_context"] < 0.35:
        route = "simple"
    else:
        route = "standard"
    return _target(config["routes"][route], config)


def route_laya(task, config, base_url=None, router_model=None, opener=None):
    if not isinstance(task, str) or not task.strip():
        raise RouterError("task must contain text")
    endpoint = local_base_url(base_url or config["laya"]["base_url"], "laya") + "/v1/systemone"
    model = router_model or config["laya"]["model"]
    if model != "typed-decisions":
        raise RouterError("laya model must be typed-decisions")
    body = {"model": model, "state": {"request": task}, "questions": QUESTIONS}
    return laya_decision(_post(endpoint, body, config["timeout_seconds"], opener), config)


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


def route_task(task, config, backend=None, base_url=None, router_model=None, opener=None):
    selected = backend or config["backend"]
    if selected == "laya":
        return route_laya(task, config, base_url, router_model, opener)
    if selected == "openai":
        return route_openai(task, config, base_url, router_model, opener)
    raise RouterError("backend must be laya or openai")
