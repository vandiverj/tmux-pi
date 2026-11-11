#!/usr/bin/env bash
# SessionStart (matcher: startup|resume|compact): hand Claude Main its working brief (HB3a: tp-agent brief, built from records only,
# no model call) as additionalContext. `brief --attach` first attaches the newest active workspace for the project when none is
# attached (never creates one). After a compaction it also marks this session's recovery pending (tp-agent work compacted), and the
# task index text follows the brief: the plugin module (register.tsx) appends the index once at an interactive startup and never
# after a compaction, so startup/resume get the brief alone, compact gets brief + index, and nothing is delivered twice.
# Orchestrator main only, the gate the plugin module uses plus TP_TASK_ID: TP_MODE=orchestrator, TP_ROOT set, TP_TASK_DIR and
# TP_TASK_ID unset (a set but empty one is a worker's or reviewer's too, as in the module and autonomy.env_problem), and
# TP_MAIN_HARNESS, when set (even empty), must be claude (the check autonomy.env_problem makes; unset is accepted because only Claude
# runs this hook and tp-claude does not export it).
# The output never passes through a shell variable (that would drop a NUL without a word): tp-agent writes it to a temporary file,
# which is printed as it is once tp-agent has succeeded.
# Fail-open: the session start is never blocked or delayed by this hook. When `tp-agent brief` fails, one line is appended (no
# symlink followed) to hook-sessionstart-compact.log in the session state directory and the injection says the brief is unavailable
# and why (after a compaction followed by `index --no-write --hook-json`'s text when that still works); when even that cannot be
# printed, nothing is.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
[[ -z "${TP_TASK_DIR+set}" && -z "${TP_TASK_ID+set}" && "${TP_MODE:-}" == orchestrator && -n "${TP_ROOT:-}" ]] || exit 0
[[ -z "${TP_MAIN_HARNESS+set}" || "$TP_MAIN_HARNESS" == claude ]] || exit 0
input="$(cat 2>/dev/null)" || input=""
# The matcher already limits the sources; refuse any other that is named. No source named is read as a compaction, as before.
source=compact
[[ "$input" =~ \"source\"[[:space:]]*:[[:space:]]*\"([a-z]*)\" ]] && source="${BASH_REMATCH[1]}"
case "$source" in startup|resume|compact) ;; *) exit 0 ;; esac

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

# A failed `work compacted` is noted but does not stop the brief: the brief is the point of this hook, and the main is better told
# where its work is than left bare because the recovery marker could not be written (the PreCompact hook is the other writer).
if [[ "$source" == compact ]]; then
  err="$("$ROOT/bin/tp-agent" work compacted --reason session-start:compact 2>&1 >/dev/null)" ||
    note "work compacted failed (the brief is still read): $err"
fi
out="$(mktemp 2>/dev/null)" && errfile="$(mktemp 2>/dev/null)" || { [[ -n "$out" ]] && rm -f "$out"; note "no temporary file, nothing injected"; exit 0; }
trap 'rm -f "$out" "$errfile"' EXIT
"$ROOT/bin/tp-agent" brief --attach --hook-json --source "$source" >"$out" 2>"$errfile"
status=$?
if [[ $status -eq 0 && -s "$out" ]]; then
  cat "$out" 2>/dev/null || note "could not print the hook output"
  exit 0
fi
why="$(head -c 200 "$errfile" 2>/dev/null | tr '\r\n' '  ' | sed 's/ *$//')"
note "brief --hook-json failed (exit $status): $(head -c 400 "$errfile" 2>/dev/null)"
: >"$out"
if [[ "$source" == compact ]]; then
  if ! "$ROOT/bin/tp-agent" index --no-write --hook-json >"$out" 2>"$errfile" || [[ ! -s "$out" ]]; then
    note "index --hook-json failed too, nothing injected but the notice: $(head -c 300 "$errfile" 2>/dev/null)"
    : >"$out"
  fi
fi
WHY="tp-agent brief failed (exit $status)${why:+: $why}" python3 -c 'import json, os, sys
text = "working brief unavailable: " + os.environ["WHY"]
try:
    index = json.load(open(sys.argv[1], encoding="utf-8"))["hookSpecificOutput"]["additionalContext"]
except Exception:
    index = ""
print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text + ("\n\n" + index if index else "")}}))' "$out" 2>/dev/null ||
  note "could not print the unavailable notice"
exit 0
