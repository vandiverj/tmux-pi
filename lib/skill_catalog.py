"""Private skill catalogue: deterministic shortlist, never a source of authority."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _catalog(root: Path = ROOT) -> dict:
    data = json.loads((root / "config/skill-catalog.json").read_text())
    if data.get("version") != 1 or not 1 <= data.get("max_selected", 0) <= 3:
        raise ValueError("unsupported skill catalogue")
    ids = set()
    for item in data["skills"]:
        ident = item["id"]
        if not re.fullmatch(r"[a-z][a-z0-9-]*", ident) or ident in ids:
            raise ValueError("invalid or duplicate skill id")
        ids.add(ident)
        file = (root / item["path"]).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file() or file.stat().st_size > 25000:
            raise ValueError(f"unsafe or missing skill: {ident}")
    return data


def _explicit(item: dict, text: str) -> bool:
    # A /command counts when it starts a token (optionally after an opening
    # bracket or quote) and is not continued as a path, longer name or file:
    # "/make-qa," "(/threat-model)" and "/threat-model." match;
    # "src/make-qa/notes", "x/threat-model" and "/threat-model.md" do not.
    return any(re.search(r"(?<![^\s(\[{\"'`])" + re.escape(marker) + r"(?![\w/-]|[.:]\w)", text)
               for marker in item.get("markers", []))


def _matches(item: dict, request: str) -> bool:
    text = request.lower()[:8000]
    if _explicit(item, text):
        return True
    if any(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) for phrase in item.get("phrases", [])):
        return True
    if item["id"] == "document-transform":
        if re.search(r"(?<!\w)[\w./-]+\.(?:pdf|docx)(?!\w)", text, re.I):
            return True
        if re.search(r"\b(?:read|extract|convert|open|summarize|create|export|produce|generate)\b.{0,90}\b(?:pdf|docx|word document)\b", text, re.S):
            return True
        if re.search(r"\b(?:pdf|docx|word document)\b.{0,90}\b(?:read|extract|convert|summarize|markdown)\b", text, re.S):
            return True
    return False


def select(request: str, root: Path = ROOT) -> list[dict]:
    data = _catalog(root)
    ranked = []
    text = request.lower()[:8000]
    for order, item in enumerate(data["skills"]):
        if _matches(item, request):
            result = {key: item[key] for key in ("id", "description", "status", "authority")}
            result["path"] = str((root / item["path"]).resolve())
            # A human's explicit /command must not disappear behind broad
            # phrase/file matches when the bounded shortlist is full.
            ranked.append((0 if _explicit(item, text) else 1, order, result))
    return [item for _, _, item in sorted(ranked)[:data["max_selected"]]]
