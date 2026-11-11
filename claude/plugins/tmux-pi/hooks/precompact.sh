#!/usr/bin/env bash
# PreCompact: before the conversation is compacted, mark this session's recovery pending and checkpoint the attached
# workspaces' judgement state (tp-agent work compacted / work checkpoint; no model call). Orchestrator main only (TP_TASK_DIR empty,
# TP_TASK_ID unset, TP_MODE=orchestrator).
# HB3a: with no workspace attached it first attaches the newest active one for the project (tp-agent brief --attach; never creates
# one), so a checkpoint is written whenever such a workspace exists. When no checkpoint is written, why is appended (no symlink
# followed) to hook-precompact.log in the session state directory; the brief after the compaction says so as well.
# Fail-open: a compaction is never blocked or delayed by this hook, whatever happens in it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
[[ -z "${TP_TASK_DIR:-}" && -z "${TP_TASK_ID+set}" && "${TP_MODE:-}" == orchestrator ]] || exit 0
input="$(cat 2>/dev/null)" || input=""
kind=unknown
[[ "$input" =~ \"trigger\"[[:space:]]*:[[:space:]]*\"(manual|auto)\" ]] && kind="${BASH_REMATCH[1]}"
# The marker first: it needs no work store and must be set even when the checkpoint cannot be written.
"$ROOT/bin/tp-agent" work compacted --reason "precompact:$kind" >/dev/null 2>&1 || true
"$ROOT/bin/tp-agent" brief --attach >/dev/null 2>&1 || true
said="$("$ROOT/bin/tp-agent" work checkpoint --auto --trigger precompact 2>&1)"
status=$?
if [[ $status -ne 0 || "$said" != *"checkpoint written: "* ]]; then
  dir="${TP_STATE_DIR:-}"
  if [[ -z "$dir" ]]; then
    if [[ -n "${TMUX:-}" ]]; then session="$(tmux display-message -p ${TMUX_PANE:+-t "$TMUX_PANE"} '#S' 2>/dev/null)"; else session="${TP_SESSION:-tmux-pi}"; fi
    dir="$(tmux show-options -qv -t "=$session:" @tp_state_dir 2>/dev/null)"
  fi
  if [[ -n "$dir" && -d "$dir" ]]; then
    printf '%s precompact: no checkpoint written (exit %s): %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$status" \
      "$(printf '%s' "$said" | tr '\r\n' '  ' | cut -c1-300)" |
      python3 "$ROOT/lib/safe_write.py" append "$dir/hook-precompact.log" --root "$dir" --mode 600 >/dev/null 2>&1 || true
  fi
fi
exit 0
