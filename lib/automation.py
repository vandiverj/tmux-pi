"""Slice 6 automation and review activation policy.

This module is deliberately deterministic. It does not route models or grant new
capabilities; it decides whether the dispatcher may act automatically on the
already-routed task, and whether a task contract needs independent report-only
review. Automation cells are fail-closed until real dispatcher-path fixtures mark
them enabled. Everything outside an enabled low-risk cell falls back to a visible
recommendation/manual path unless the user made an explicit placement request
(+delegate/+here/manual tp-spawn). +review only raises review; it does not grant
placement or dispatch authority.
"""

import datetime as _dt
import json
import re

SCHEMA = "tmux-pi-automation-policy:v1"
VERSION = "2026-09-28.r3.highrisk-confirmation.v1"

STRUCTURAL_RE = re.compile(
    r"\b(new\s+)?(module|service|component|boundary|public contract|api contract|dependency direction|"
    r"cross[- ]?layer|workflow|state machine|domain model|persistence|integration architecture|"
    r"schema|migration|event bus|major refactor|architecture|architectural)\b",
    re.I,
)
SENSITIVE_RE = re.compile(
    r"\b(secret|credential|token|password|security|vulnerab|privacy|pii|personally identifiable|"
    r"customer data|gdpr|hipaa|prod(?:uction)?|payment|billing|payroll|medical|ssn)\b",
    re.I,
)
DESTRUCTIVE_RE = re.compile(r"\b(delete|drop|destroy|purge|wipe|disable|revoke|rotate|deploy|restart|failover|production)\b", re.I)
SMALL_EDIT_RE = re.compile(r"\b(fix|update|change|edit|adjust|rename|add|remove)\b", re.I)
READ_ONLY_INTENTS = {"research", "review", "communication_or_documentation", "planning"}
CHANGE_INTENTS = {"implementation", "configuration_or_operations"}
# Cell state machine: pending -> fixture-qualified -> enabled -> limited/disabled.
# R2 keeps every cell pending until real dispatcher-path fixture/model/tool-route
# evidence exists. Unit tests of this policy are not qualification evidence.
QUALIFIED_CELLS = {
    "read_only_investigation": {
        "state": "pending",
        "enabled": False,
        "evidence": ["pending real dispatcher-path read-only/investigation fixture qualification"],
    },
    "small_scoped_edit": {
        "state": "pending",
        "enabled": False,
        "evidence": ["pending real dispatcher-path small scoped-edit fixture qualification"],
    },
}


def now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(request, decision=None, preflight=None):
    decision = decision or {}
    preflight = preflight or {}
    return "\n".join(str(x or "") for x in (
        request,
        decision.get("task"), decision.get("intent"), decision.get("scope"), decision.get("route"),
        preflight.get("task"), preflight.get("frame"), " ".join(preflight.get("triggers") or []),
    ))


def _bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value >= 0.5
    return str(value).lower() in {"true", "yes", "required", "high"}


def explicit_request(decision):
    """True only for explicit placement authority (+delegate/+here), not +review."""
    overrides = set((decision or {}).get("overrides") or [])
    # Router decisions record marker effects by field name. Older/manual callers
    # may pass marker-ish names; accept those only when they are placement markers.
    return bool(overrides.intersection({"placement", "+delegate", "+here", "delegate", "here"}))


def structural(request, decision=None, preflight=None):
    preflight = preflight or {}
    return "structural shape" in (preflight.get("triggers") or []) or bool(STRUCTURAL_RE.search(_text(request, decision, preflight)))


def sensitive(request, decision=None, preflight=None):
    decision = decision or {}
    preflight = preflight or {}
    return (_bool(decision.get("sensitive")) or _bool(decision.get("is_sensitive")) or
            "sensitive/security/data shape" in (preflight.get("triggers") or []) or
            bool(SENSITIVE_RE.search(_text(request, decision, preflight))))


def destructive_or_high(request, decision=None, preflight=None):
    decision = decision or {}
    preflight = preflight or {}
    return (_bool(decision.get("high_risk")) or _bool(decision.get("is_high_risk")) or
            "high-risk or consequential operation" in (preflight.get("triggers") or []) or
            bool(DESTRUCTIVE_RE.search(_text(request, decision, preflight))))


def risk_level(request, decision=None, preflight=None):
    decision = decision or {}
    preflight = preflight or {}
    tier = int(preflight.get("artifact_tier") or 0)
    scope = decision.get("scope") or "brief"
    route = decision.get("route") or ""
    intent = decision.get("intent") or ""
    if sensitive(request, decision, preflight) or destructive_or_high(request, decision, preflight) or tier >= 3:
        return "high"
    if tier >= 2 or scope in {"sustained", "multi_step"} or route in {"complex", "expert"} or intent == "configuration_or_operations":
        return "medium"
    return "low"


def review_requirement(request, decision=None, preflight=None, repeated_failure=False):
    """Return report-only review requirement from canonical task contract fields."""
    decision = decision or {}
    preflight = preflight or {}
    reasons = []
    if decision.get("review") == "required" or "review" in (decision.get("overrides") or []):
        reasons.append("explicit review request")
    if sensitive(request, decision, preflight):
        reasons.append("sensitive/PII/security trigger")
    if repeated_failure or decision.get("repeated_failure"):
        reasons.append("repeated failure trigger")
    risk = risk_level(request, decision, preflight)
    if structural(request, decision, preflight) and risk in {"medium", "high"}:
        reasons.append("structural + {} risk trigger".format(risk))
    # Destructive/high-risk operations are not automatically dispatched; if they
    # reach review policy, keep the review visible as an independent report.
    if destructive_or_high(request, decision, preflight) and risk == "high":
        reasons.append("high-risk/consequential trigger")
    return {"required": bool(reasons), "mode": "report_only", "reasons": reasons, "risk": risk,
            "structural": structural(request, decision, preflight)}


def _changes(decision, preflight):
    intent = (decision or {}).get("intent")
    if (preflight or {}).get("no_edits"):
        return False
    if intent in CHANGE_INTENTS:
        return True
    allowed = " ".join(((preflight or {}).get("allowed") or {}).get("tools") or []).lower()
    return "file edits" in allowed


def _qualified_cell(request, decision, preflight, review):
    decision = decision or {}
    preflight = preflight or {}
    risk = review["risk"]
    intent = decision.get("intent") or ""
    scope = decision.get("scope") or "brief"
    route = decision.get("route") or ""
    if review["required"] or risk != "low" or sensitive(request, decision, preflight) or destructive_or_high(request, decision, preflight):
        return None, ["not low-risk/no-review"]
    if not _changes(decision, preflight) and (intent in READ_ONLY_INTENTS or preflight.get("mode") in {"direct", "investigate", "plan"}):
        return "read_only_investigation", []
    if (_changes(decision, preflight) and intent == "implementation" and scope == "brief" and
            route in {"simple", "standard", "coding"} and int(preflight.get("artifact_tier") or 0) <= 1 and
            not structural(request, decision, preflight) and SMALL_EDIT_RE.search(_text(request, decision, preflight))):
        return "small_scoped_edit", []
    return None, ["no qualified Slice 6 automation cell matched"]


def _main_contract_summary(main_contract):
    contract = main_contract or {}
    if not contract:
        return None
    return {
        "schema": contract.get("schema"),
        "handshake_id": contract.get("handshake_id"),
        "request_sha256": contract.get("request_sha256"),
        "action": contract.get("action"),
        "mode": contract.get("mode"),
        "scope": contract.get("scope"),
        "no_edits": bool(contract.get("no_edits")),
        "material_ambiguity": contract.get("material_ambiguity"),
    }


def automation_policy(request, decision=None, preflight=None, main_contract=None, repeated_failure=False):
    decision = decision or {}
    preflight = preflight or {}
    main_contract = main_contract or {}
    review = review_requirement(request, decision, preflight, repeated_failure=repeated_failure)
    explicit = explicit_request(decision)
    cell, blockers = _qualified_cell(request, decision, preflight, review)
    qualification = QUALIFIED_CELLS.get(cell) if cell else None
    enabled = bool(qualification and qualification.get("enabled") and qualification.get("state") == "enabled")
    action = "auto_dispatch" if enabled else "recommend_manual"
    mode = "qualified" if enabled else "recommendation_manual"
    if cell and not enabled:
        blockers = list(blockers) + ["automation cell {} is {} pending fixture qualification".format(cell, qualification.get("state", "unknown"))]
    contract_action = main_contract.get("action")
    if contract_action == "ask_alignment":
        blockers = list(blockers) + ["main contract requested material alignment before dispatch"]
    elif contract_action == "stop":
        blockers = list(blockers) + ["main contract is a stop-mode contract"]
    elif contract_action == "direct":
        blockers = list(blockers) + ["main contract selected direct main handling"]
    if explicit:
        # Explicit placement markers are a human request to use the route mechanism;
        # keep them available but visibly separate from qualified automation. +review
        # alone is not explicit placement and reaches the fail-closed path above.
        action = "auto_dispatch"
        mode = "explicit_human_request"
        # Explicit placement remains human authority, but all high-risk work
        # requires separate human confirmation in the dispatcher.
        blockers = []
    confirmation_required = bool(explicit and review["risk"] == "high")
    if main_contract and (contract_action in {"ask_alignment", "stop"} or (contract_action == "direct" and not explicit)):
        action = "recommend_manual"
        mode = "recommendation_manual"
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "created": now(),
        "action": action,
        "mode": mode,
        "qualified_cell": cell if enabled else None,
        "candidate_cell": cell,
        "qualification": qualification,
        "review": review,
        "manual_available": True,
        "experimental_requires_explicit": True,
        "post_contract": bool(main_contract),
        "main_contract": _main_contract_summary(main_contract),
        "confirmation_required": confirmation_required,
        "confirmation_reason": "explicit high-risk routing requires separate human confirmation" if confirmation_required else "",
        "blockers": blockers,
        "notice": notice(action, mode, cell, review, blockers, confirmation_required=confirmation_required),
    }


def notice(action, mode, cell, review, blockers=(), confirmation_required=False):
    if confirmation_required:
        return "Slice 6/R5: explicit high-risk routing requires separate human confirmation before dispatch."
    if action == "auto_dispatch" and mode == "explicit_human_request":
        return "Slice 6: explicit human routing/review marker; proceeding visibly outside qualified automation."
    if action == "auto_dispatch":
        return "Slice 6: qualified automatic dispatch enabled for {}.".format(cell)
    bits = ["Slice 6: recommendation/manual mode; automatic dispatch not enabled for this task"]
    if review.get("required"):
        bits.append("review required ({})".format("; ".join(review.get("reasons") or [])))
    if blockers:
        bits.append("blockers: {}".format("; ".join(blockers)))
    return "; ".join(bits) + "."


def dumps(policy):
    return json.dumps(policy, indent=2, sort_keys=True)
