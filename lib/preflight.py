"""Slice 2 task contract preflight and artifact-tier selection.

This module is deliberately small and deterministic. It does not route models,
retrieve context, or grant capabilities; it gives the main tmux-pi extension a
compact contract to write before delegation or explicit stop-mode work.
"""

import datetime as _dt
import json
import re
from pathlib import Path

import safe_write
from task_record import write_record

STOP_MODES = {"plan", "prototype", "investigate"}
TIERS = {0, 1, 2, 3}
MAIN_CONTRACT_SCHEMA = "tmux-pi-main-contract:v1"
MAIN_CONTRACT_ACTIONS = {"direct", "delegate", "ask_alignment", "stop"}
MAIN_CONTRACT_MODES = {"direct", "delegated", "plan", "prototype", "investigate"}
MAIN_CONTRACT_SCOPES = {"brief", "sustained", "multi_step"}
FORBIDDEN_CONTRACT_KEYS = {
    "authorized", "approved", "can_dispatch", "capability_grants", "capability_grant",
    "dispatch_authority", "authority", "permission_grant", "approved_tools",
}

STRUCTURAL_RE = re.compile(
    r"\b(architecture|architect|design|schema|migration|migrate|module|service|component|api|public contract|"
    r"dependency|refactor|workflow|state machine|event bus|auth|authentication|authorization|persistence|database)\b",
    re.I,
)
SENSITIVE_RE = re.compile(r"\b(secret|credential|token|password|security|vulnerab|privacy|pii|customer data|gdpr|hipaa|prod(?:uction)?|database|payment|billing)\b", re.I)
INVESTIGATE_DURABLE_RE = re.compile(r"\b(root cause|why|across|every|audit|compare|evidence|sources|report|production|customer|security|incident|continue|decision)\b", re.I)
DESTRUCTIVE_RE = re.compile(r"\b(delete|remove|drop|destroy|rotate|revoke|disable|rollback|restart|failover|deploy|migration|production)\b", re.I)


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _flag(decision, *names):
    for name in names:
        if name in decision:
            value = decision[name]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return value >= 0.5
        risk = decision.get("risk") or {}
        if name in risk:
            return bool(risk[name]) if isinstance(risk[name], bool) else _num(risk[name]) >= 0.5
    return False


def _explicit_mode(mode, request):
    if mode:
        return mode
    m = re.match(r"\s*\+(plan|prototype|investigate)\b", request or "", re.I)
    return m.group(1).lower() if m else ""


def strip_mode_marker(request):
    """Remove one leading +plan/+prototype/+investigate marker, preserving the request."""
    return re.sub(r"^\s*\+(plan|prototype|investigate)\b\s*", "", request or "", flags=re.I).strip()


def select_preflight(request, decision=None, mode=None):
    """Return a compact task contract preflight.

    `decision` may be the router dispatch JSON. The function treats those values as
    hints only; it does not call a model or change model routing policy.
    """
    decision = dict(decision or {})
    raw_request = request or decision.get("task") or ""
    explicit = _explicit_mode(mode, raw_request)
    task = strip_mode_marker(raw_request) if explicit else raw_request.strip()
    intent = decision.get("intent") or ("research" if explicit == "investigate" else "planning" if explicit in {"plan", "prototype"} else "unknown")
    scope = decision.get("scope") or "brief"
    review = decision.get("review") or "none"
    route = decision.get("route") or "unrouted"
    placement = decision.get("placement") or "main"
    changes = _flag(decision, "changes", "changes_files") or intent in ("implementation", "configuration_or_operations")
    sensitive = _flag(decision, "sensitive", "is_sensitive") or bool(SENSITIVE_RE.search(task))
    high_risk = _flag(decision, "high_risk", "is_high_risk") or bool(DESTRUCTIVE_RE.search(task))
    structural = bool(STRUCTURAL_RE.search(task)) or scope == "multi_step"

    if explicit:
        task_mode = explicit
    elif placement == "main" and scope == "brief" and review == "none" and not changes and not sensitive and not high_risk:
        task_mode = "direct"
    else:
        task_mode = "delegated"

    stop = task_mode in STOP_MODES
    # R2: read-only/research/direct contracts are not edit-authorized merely
    # because they are not explicit stop modes. Edits require a change intent.
    no_edits = stop or not changes
    triggers = []
    if stop:
        triggers.append("explicit stop mode")
    if structural:
        triggers.append("structural shape")
    if sensitive:
        triggers.append("sensitive/security/data shape")
    if high_risk:
        triggers.append("high-risk or consequential operation")
    if review == "required":
        triggers.append("required review")
    if scope in ("sustained", "multi_step"):
        triggers.append(scope)

    tier = 0
    if task_mode in {"plan", "prototype"}:
        tier = 1
    elif task_mode == "investigate":
        tier = 1 if (scope != "brief" or sensitive or high_risk or structural or INVESTIGATE_DURABLE_RE.search(task)) else 0
    elif task_mode != "direct":
        tier = 1
    if structural or review == "required" or sensitive:
        tier = max(tier, 2)
    if (sensitive and high_risk) or (high_risk and re.search(r"\b(prod(?:uction)?|customer|credential|security|gdpr|payment|billing)\b", task, re.I)):
        tier = max(tier, 3)
    if task_mode == "direct" and not triggers:
        tier = 0

    durable = tier >= 1
    promotion_notice = ""
    if tier >= 2:
        promotion_notice = "Promoted to Tier {} durable contract ({})".format(tier, "; ".join(triggers[:3]) or "policy trigger")
    elif tier == 1 and task_mode in STOP_MODES:
        promotion_notice = "Tier 1 durable stop-mode contract"
    elif tier == 1 and task_mode != "direct":
        promotion_notice = "Tier 1 task record selected for delegated/non-direct work"

    allowed_tools = ["read/search"]
    if no_edits:
        allowed_tools.append("no edit/apply/write tools")
        if not stop and re.search(r"\b(test|verify|check|lint|build)\b", task, re.I):
            allowed_tools.append("non-destructive verification commands only if needed for the answer")
    else:
        allowed_tools.append("file edits within task scope")
        allowed_tools.append("shell/test/verification commands")
    proof = []
    if task_mode == "plan":
        proof = ["write a plan/decision record", "identify assumptions and open questions", "stop before implementation"]
    elif task_mode == "prototype":
        proof = ["describe the prototype approach or throwaway sketch", "mark what is unsafe/incomplete", "stop before implementation edits"]
    elif task_mode == "investigate":
        proof = ["report findings and confidence", "cite files/commands/sources used when material", "make no edits"]
    elif task_mode == "direct":
        proof = ["answer compactly in chat", "mention assumptions if any"]
    else:
        proof = ["complete requested work within scope", "run relevant checks", "report files changed and evidence"]

    if task_mode == "direct":
        boundary = "Proceed compactly; ask only if ambiguity would materially change the answer or cause rework."
    elif stop:
        boundary = "Stop at the {} artifact; do not edit files or start implementation without a new user request.".format(task_mode)
    else:
        boundary = "Ask before proceeding if ambiguity could materially change behavior, security/data exposure, scope, architecture, acceptance evidence, or rework."

    frame = "{} {} request; scope {}; review {}; route {}; placement {}.".format(
        "Stop-mode" if stop else "Direct" if task_mode == "direct" else "Delegated",
        task_mode,
        scope,
        review,
        route,
        placement,
    )
    assumptions = []
    if not sensitive:
        assumptions.append("No expanded sensitive data source is approved by this Slice 2 preflight.")
    assumptions.append("No generic retrieval/RAG or MCP broker is selected in Slice 2.")
    if stop:
        assumptions.append("The user asked for {} only; implementation requires a later request.".format(task_mode))
    elif no_edits:
        assumptions.append("This preflight classified the request as read-only/non-changing; edits require an explicit change task.")

    return {
        "schema": "tmux-pi-preflight:v1",
        "created": _now(),
        "mode": task_mode,
        "task": task,
        "stop": stop,
        "no_edits": no_edits,
        "artifact_tier": tier,
        "durable": durable,
        "frame": frame,
        "assumptions": assumptions,
        "allowed": {"paths": ["working directory only unless the user approves more"], "tools": allowed_tools, "mcps": ["none auto-selected in Slice 2"]},
        "decision_boundary": boundary,
        "proof_path": proof,
        "triggers": triggers,
        "promotion_notice": promotion_notice,
        "routing": {k: decision.get(k) for k in ("worker", "model", "thinking", "route", "intent", "review", "scope", "placement") if decision.get(k) is not None},
    }


def _contract_error(message):
    raise ValueError(message)


def _clip_string(value, limit, field):
    if value is None:
        return ""
    if not isinstance(value, str):
        _contract_error("main contract field {} must be a string".format(field))
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    return text[:limit]


def _string_list(value, field, limit=8, item_limit=240):
    if value is None:
        return []
    if not isinstance(value, list):
        _contract_error("main contract field {} must be a list".format(field))
    out = []
    for item in value[:limit]:
        text = _clip_string(item, item_limit, field)
        if text:
            out.append(text)
    return out


def _forbidden_keys(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text.lower() in FORBIDDEN_CONTRACT_KEYS:
                found.append((path + "." if path else "") + key_text)
            found.extend(_forbidden_keys(child, (path + "." if path else "") + key_text))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            found.extend(_forbidden_keys(child, "{}[{}]".format(path, i)))
    return found


def normalize_main_contract(contract, handshake_id, request_sha256):
    """Validate and normalize the main-authored compact Orient/Frame contract.

    The contract is descriptive only. It cannot grant authority; forbidden authority
    words are rejected so policy must keep deriving placement/edit power from the
    original deterministic markers and qualification state.
    """
    if not isinstance(contract, dict):
        _contract_error("main contract must be a JSON object")
    bad = _forbidden_keys(contract)
    if bad:
        _contract_error("main contract contains forbidden authority field(s): {}".format(", ".join(bad[:6])))
    if contract.get("handshake_id") != handshake_id:
        _contract_error("main contract handshake_id mismatch")
    if contract.get("request_sha256") != request_sha256:
        _contract_error("main contract request_sha256 mismatch")
    action = contract.get("action")
    mode = contract.get("mode")
    scope = contract.get("scope")
    if action not in MAIN_CONTRACT_ACTIONS:
        _contract_error("main contract action must be one of {}".format(", ".join(sorted(MAIN_CONTRACT_ACTIONS))))
    if mode not in MAIN_CONTRACT_MODES:
        _contract_error("main contract mode must be one of {}".format(", ".join(sorted(MAIN_CONTRACT_MODES))))
    if scope not in MAIN_CONTRACT_SCOPES:
        _contract_error("main contract scope must be one of {}".format(", ".join(sorted(MAIN_CONTRACT_SCOPES))))
    if not isinstance(contract.get("no_edits"), bool):
        _contract_error("main contract no_edits must be boolean")
    material = contract.get("material_ambiguity")
    if material is not None:
        if not isinstance(material, dict):
            _contract_error("main contract material_ambiguity must be an object")
        question = _clip_string(material.get("question"), 500, "material_ambiguity.question")
        why = _clip_string(material.get("why_material"), 500, "material_ambiguity.why_material")
        material = {"question": question, "why_material": why} if question or why else None
    required = {
        "task_summary": 500,
        "frame": 800,
    }
    normalized = {
        "schema": MAIN_CONTRACT_SCHEMA,
        "created": _now(),
        "handshake_id": handshake_id,
        "request_sha256": request_sha256,
        "action": action,
        "mode": mode,
        "task_summary": _clip_string(contract.get("task_summary"), required["task_summary"], "task_summary"),
        "frame": _clip_string(contract.get("frame"), required["frame"], "frame"),
        "assumptions": _string_list(contract.get("assumptions"), "assumptions"),
        "material_ambiguity": material,
        "no_edits": bool(contract.get("no_edits")),
        "scope": scope,
        "risk_notes": _string_list(contract.get("risk_notes"), "risk_notes"),
        "proof_path": _string_list(contract.get("proof_path"), "proof_path", item_limit=260),
        "done_when": _string_list(contract.get("done_when"), "done_when", item_limit=260),
    }
    for field in ("task_summary", "frame"):
        if not normalized[field]:
            _contract_error("main contract field {} is required".format(field))
    if action == "ask_alignment":
        if not normalized.get("material_ambiguity") or not normalized["material_ambiguity"].get("question"):
            _contract_error("ask_alignment contracts require material_ambiguity.question")
    if action == "stop" and mode not in STOP_MODES:
        _contract_error("stop contracts require plan/prototype/investigate mode")
    if action == "delegate" and mode == "direct":
        _contract_error("delegate contracts cannot use direct mode")
    return normalized


def merge_main_contract(preflight, main_contract):
    """Attach main Orient/Frame fields to a scaffold preflight without granting authority."""
    base = dict(preflight or {})
    contract = dict(main_contract or {})
    if not contract:
        return base
    frame_bits = [base.get("frame") or ""]
    if contract.get("frame"):
        frame_bits.append("Main frame: " + contract["frame"])
    base["frame"] = " ".join(bit for bit in frame_bits if bit).strip()
    proof = list(base.get("proof_path") or [])
    for item in contract.get("proof_path") or []:
        if item and item not in proof:
            proof.append(item)
    if proof:
        base["proof_path"] = proof[:12]
    base["main_orient_frame"] = {
        "schema": contract.get("schema") or MAIN_CONTRACT_SCHEMA,
        "handshake_id": contract.get("handshake_id"),
        "request_sha256": contract.get("request_sha256"),
        "action": contract.get("action"),
        "mode": contract.get("mode"),
        "scope": contract.get("scope"),
        "task_summary": contract.get("task_summary"),
        "frame": contract.get("frame"),
        "assumptions": contract.get("assumptions") or [],
        "material_ambiguity": contract.get("material_ambiguity"),
        "no_edits": contract.get("no_edits"),
        "risk_notes": contract.get("risk_notes") or [],
        "proof_path": contract.get("proof_path") or [],
        "done_when": contract.get("done_when") or [],
    }
    # Intersect edit authority: the main contract may narrow a scaffold but
    # cannot relax a scaffold no-edit decision. Do not alter stop/mode or grant
    # tools absent from the scaffold.
    base["main_contract_no_edits"] = bool(contract.get("no_edits"))
    base["no_edits"] = bool(base.get("no_edits") or base["main_contract_no_edits"])
    if base["no_edits"]:
        allowed = dict(base.get("allowed") or {})
        scaffold_tools = allowed.get("tools") or []
        tools = ["read/search", "no edit/apply/write tools"]
        if not base.get("stop") and any(
            tool in scaffold_tools for tool in (
                "shell/test/verification commands",
                "non-destructive verification commands only if needed for the answer",
            )
        ):
            tools.append("non-destructive verification commands only if needed for the answer")
        allowed["tools"] = tools
        base["allowed"] = allowed
    return base


def contract_note(preflight):
    """One compact packet note for workers."""
    bits = [
        "Task contract preflight: mode={mode}, Tier {artifact_tier}, stop={stop}, no_edits={no_edits}.".format(**preflight),
        "Frame: " + preflight["frame"],
        "Boundary: " + preflight["decision_boundary"],
        "Proof path: " + "; ".join(preflight["proof_path"]),
        "Allowed tools/paths: " + "; ".join(preflight["allowed"]["tools"]) + "; " + "; ".join(preflight["allowed"]["paths"]),
    ]
    return " ".join(bits)


def create_preflight_record(state_dir, request, preflight, name="contract", main_contract=None, policy_decision=None, lifecycle_state="working"):
    """Create a durable main-owned task directory without spawning a worker."""
    tasks = Path(state_dir) / "tasks"
    safe_write.ensure_dir(tasks, what="tasks directory", root=state_dir)
    stamp = _dt.datetime.now().strftime("%H%M%S")
    safe = re.sub(r"[^\w.-]+", "-", name or "contract").strip("-") or "contract"
    task_id = f"{safe}-{stamp}"
    n = 0
    while (tasks / task_id).exists():
        n += 1
        task_id = f"{safe}-{stamp}-{n}"
    d = tasks / task_id
    d.mkdir()
    safe_write.write_atomic(d / "request.md", (request or preflight.get("task") or "").strip() + "\n", root=state_dir)
    meta = {
        "id": task_id,
        "worker": "main",
        "model": "main-agent",
        "thinking": "",
        "created": preflight.get("created") or _now(),
        "preflight": preflight,
    }
    if main_contract:
        meta["main_contract"] = main_contract
        safe_write.write_atomic(d / "main-contract.json", json.dumps(main_contract, indent=2, sort_keys=True) + "\n", root=state_dir)
        if main_contract.get("handshake_id"):
            meta["handshake_id"] = main_contract.get("handshake_id")
    if policy_decision:
        meta["policy_decision"] = policy_decision
        safe_write.write_atomic(d / "policy-decision.json", json.dumps(policy_decision, indent=2, sort_keys=True) + "\n", root=state_dir)
    safe_write.write_atomic(d / "meta.json", json.dumps(meta, indent=2, sort_keys=True) + "\n", root=state_dir)
    write_record(d, lifecycle_state=lifecycle_state, meta=meta)
    return {"id": task_id, "dir": str(d), "record": str(d / "task.md")}
