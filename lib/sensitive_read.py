"""Identify ordinary tool reads that may expose credentials or sensitive config."""
import json
from pathlib import Path
import re
import shlex

SENSITIVE_NAME = re.compile(
    r"^(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|auth\.json|web\.config|"
    r"appsettings(?:\.[^.]+)?\.json|credentials(?:\..*)?|id_(?:rsa|ed25519|ecdsa).*|"
    r".*\.(?:pem|p12|pfx|key)|.*secret.*)$", re.I
)
SENSITIVE_DIR = {".aws", ".ssh", ".gnupg", ".kube", ".docker"}
SENSITIVE_CONFIG_DIR = {"gcloud", "gh"}
TOOL_PATH_KEYS = ("file_path", "path", "notebook_path", "glob")
INLINE_NAME = re.compile(
    r"(?:\.env(?:\.[A-Za-z0-9_-]+)?|\.aws/|\.ssh/|\.gnupg/|"
    r"appsettings(?:\.[A-Za-z0-9_-]+)?\.json|web\.config|credentials(?:\.[A-Za-z0-9_-]+)?|"
    r"id_(?:rsa|ed25519|ecdsa)|\.npmrc|\.netrc|\.pypirc|[A-Za-z0-9_-]*secret[A-Za-z0-9_-]*)", re.I
)


def sensitive_path(value):
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip().strip("'\"")
    path = Path(value)
    parts = path.parts
    if SENSITIVE_NAME.match(path.name):
        return True
    def sensitive_parts(path_parts):
        return (any(part in SENSITIVE_DIR for part in path_parts) or
                any(left == ".config" and right in SENSITIVE_CONFIG_DIR
                    for left, right in zip(path_parts, path_parts[1:])))
    if sensitive_parts(parts):
        return True
    try:
        resolved = path.resolve(strict=True)
    except (OSError, ValueError):
        return False
    return resolved != path and (SENSITIVE_NAME.match(resolved.name) is not None or
                                 sensitive_parts(resolved.parts))


def classify(tool, params):
    params = params or {}
    if tool in {"Read", "read", "NotebookRead", "notebook_read", "Grep", "grep", "Glob", "glob", "find"}:
        for key in TOOL_PATH_KEYS:
            value = params.get(key)
            if sensitive_path(value):
                return {"sensitive": True, "target": value, "reason": "credential-bearing or sensitive configuration path"}
    if tool not in {"Bash", "bash", "PowerShell", "powershell"}:
        return {"sensitive": False}
    command = str(params.get("command") or "")
    inline = INLINE_NAME.search(command)
    if inline:
        return {"sensitive": True, "target": inline.group(0), "reason": "sensitive path in shell command"}
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    for word in words:
        for piece in re.split(r"[=:,]", word):
            if sensitive_path(piece):
                return {"sensitive": True, "target": piece, "reason": "sensitive path in shell command"}
    if re.search(r"\b(?:rg|grep)\b[^\n]*\s(?:--hidden|--no-ignore|-uu|-uuu|--unrestricted)\b", command):
        return {"sensitive": True, "target": "broad hidden/unignored search", "reason": "search may include credential files"}
    if re.search(r"\bgrep\b[^\n]*\s-(?:R|r)\b", command):
        return {"sensitive": True, "target": "recursive grep", "reason": "recursive search may include credential files"}
    if re.search(r"\bfind\b[^\n]*\s-exec\s+(?:cat|head|tail|sed|grep|rg|python3?)\b", command) or re.search(
            r"\bxargs\b[^\n]*\b(?:cat|head|tail|sed|grep|rg)\b", command):
        return {"sensitive": True, "target": "bulk shell read", "reason": "dynamic file selection may include credential files"}
    return {"sensitive": False}


def hook_response(event):
    result = classify(event.get("tool_name", ""), event.get("tool_input", {}))
    if not result["sensitive"]:
        return None
    reason = f"Read may expose {result['target']}. Authorize this specific tool call only if its scope is needed."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": "ask",
                                   "permissionDecisionReason": reason}}
