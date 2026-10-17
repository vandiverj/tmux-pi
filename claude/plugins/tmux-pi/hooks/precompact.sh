#!/usr/bin/env bash
# PreCompact: before the conversation is compacted, mark this session's recovery pending and checkpoint the attached
# workspaces' judgement state (tp-agent work compacted / work checkpoint; no model call). Orchestrator main only.
# Fail-open: a compaction is never blocked or delayed by this hook, whatever happens in it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
[[ -z "${TP_TASK_DIR:-}" && "${TP_MODE:-}" == orchestrator ]] || exit 0
input="$(cat 2>/dev/null)" || input=""
kind=unknown
[[ "$input" =~ \"trigger\"[[:space:]]*:[[:space:]]*\"(manual|auto)\" ]] && kind="${BASH_REMATCH[1]}"
# The marker first: it needs no work store and must be set even when the checkpoint cannot be written.
"$ROOT/bin/tp-agent" work compacted --reason "precompact:$kind" >/dev/null 2>&1 || true
"$ROOT/bin/tp-agent" work checkpoint --auto --trigger precompact >/dev/null 2>&1 || true
exit 0
