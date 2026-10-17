#!/usr/bin/env bash
# SessionStart (matcher: compact): after a compaction, mark this session's recovery pending (tp-agent work compacted) and hand the
# main the task index as additionalContext (tp-agent index --no-write --hook-json prints the finished hook output; no model call).
# Orchestrator main only, the gate the plugin module uses: TP_MODE=orchestrator, TP_ROOT set, TP_TASK_DIR unset (a set but empty one
# is a worker's too, as in the module and autonomy.env_problem), and TP_MAIN_HARNESS, when set (even empty), must be claude (the
# check autonomy.env_problem makes; unset is accepted because only Claude runs this hook and tp-claude does not export it).
# The index never passes through a shell variable (that would drop a NUL without a word): tp-agent writes it to a temporary file,
# which is printed as it is once tp-agent has succeeded.
# Fail-open: the session start is never blocked or delayed by this hook. Any failure is exit 0 with nothing on stdout and one line
# appended (no symlink followed) to hook-sessionstart-compact.log in the session state directory.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
[[ -z "${TP_TASK_DIR+set}" && "${TP_MODE:-}" == orchestrator && -n "${TP_ROOT:-}" ]] || exit 0
[[ -z "${TP_MAIN_HARNESS+set}" || "$TP_MAIN_HARNESS" == claude ]] || exit 0
input="$(cat 2>/dev/null)" || input=""
# The matcher already limits this to a compaction; refuse any other source that is named.
if [[ "$input" =~ \"source\"[[:space:]]*:[[:space:]]*\"([a-z]*)\" && "${BASH_REMATCH[1]}" != compact ]]; then exit 0; fi

state_dir() {
  local session
  if [[ -n "${TP_STATE_DIR:-}" ]]; then printf '%s' "$TP_STATE_DIR"; return; fi
  if [[ -n "${TMUX:-}" ]]; then session="$(tmux display-message -p ${TMUX_PANE:+-t "$TMUX_PANE"} '#S' 2>/dev/null)"; else session="${TP_SESSION:-tmux-pi}"; fi
  tmux show-options -qv -t "=$session:" @tp_state_dir 2>/dev/null
}

note() {
  local dir line
  dir="$(state_dir)" || return 0
  [[ -n "$dir" && -d "$dir" ]] || return 0
  line="$(printf '%s' "$1" | tr '\r\n' '  ' | cut -c1-400)"
  printf '%s sessionstart-compact: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" |
    python3 "$ROOT/lib/safe_write.py" append "$dir/hook-sessionstart-compact.log" --root "$dir" --mode 600 >/dev/null 2>&1 || true
}

# A failed `work compacted` is noted but does not stop the index: the index is the point of this hook, and the main is better told
# where its work is than left bare because the recovery marker could not be written (the PreCompact hook is the other writer).
err="$("$ROOT/bin/tp-agent" work compacted --reason session-start:compact 2>&1 >/dev/null)" ||
  note "work compacted failed (the index is still read): $err"
out="$(mktemp 2>/dev/null)" && errfile="$(mktemp 2>/dev/null)" || { [[ -n "$out" ]] && rm -f "$out"; note "no temporary file, nothing injected"; exit 0; }
trap 'rm -f "$out" "$errfile"' EXIT
"$ROOT/bin/tp-agent" index --no-write --hook-json >"$out" 2>"$errfile"
status=$?
if [[ $status -ne 0 || ! -s "$out" ]]; then
  note "index --hook-json failed (exit $status), nothing injected: $(head -c 400 "$errfile" 2>/dev/null)"
  exit 0
fi
cat "$out" 2>/dev/null || note "could not print the hook output"
exit 0
