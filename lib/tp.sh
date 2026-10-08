# Shared helpers for tp-* scripts. Source after ROOT is set. bash 3.2 compatible.

die() { echo "$(basename "$0"): $*" >&2; exit 1; }

# tp_canon <dir> — physical path when the directory exists, else the path as given.
tp_canon() { (cd -- "$1" 2>/dev/null && pwd -P) || printf '%s\n' "${1%/}"; }

# The tmux-pi session to act on: the current session when inside tmux, otherwise
# $TP_SESSION, otherwise the session that owns $TP_STATE_DIR, otherwise tmux-pi.
# $TP_STATE_DIR is authoritative: tp-agent and the Python helpers read task
# records from it directly, so a session with any other state dir is refused
# here rather than used. A main agent's tool shell can lack TMUX and TP_SESSION
# while still carrying TP_STATE_DIR.
tp_session() {
  local s="" want="" have name found=0
  [[ -z "${TP_STATE_DIR:-}" ]] || want="$(tp_canon "$TP_STATE_DIR")"
  if [[ -n "${TMUX:-}" ]]; then
    if [[ -n "${TMUX_PANE:-}" ]]; then
      s="$(tmux display-message -p -t "$TMUX_PANE" '#S')"
    else
      s="$(tmux display-message -p '#S')"
    fi
  elif [[ -n "${TP_SESSION:-}" ]]; then
    s="$TP_SESSION"
  elif [[ -n "$want" ]]; then
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      have="$(tmux show-options -qv -t "=$name:" @tp_state_dir 2>/dev/null)" || continue
      if [[ -n "$have" && "$(tp_canon "$have")" == "$want" ]]; then
        s="$name"
        found=$((found + 1))
      fi
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
    [[ "$found" -gt 0 ]] || die "no tmux-pi session uses TP_STATE_DIR '$want' (start one with: agent)"
    [[ "$found" -eq 1 ]] || die "$found tmux-pi sessions use TP_STATE_DIR '$want'; set TP_SESSION to choose one"
  else
    s="tmux-pi"
  fi
  tmux has-session -t "=$s" 2>/dev/null || die "no tmux-pi session '$s' (start one with: agent)"
  have="$(tmux show-options -qv -t "=$s:" @tp_state_dir)"
  [[ -n "$have" ]] || die "session '$s' is not a tmux-pi session"
  if [[ -n "$want" && "$(tp_canon "$have")" != "$want" ]]; then
    die "session '$s' keeps its state in '$have', not TP_STATE_DIR '$want'; refusing to act on another workspace"
  fi
  echo "$s"
}

# tp_opt <session> <@option>
tp_opt() { tmux show-options -qv -t "=$1:" "$2"; }

# tp_task_dir <session> <id>
tp_task_dir() { echo "$(tp_opt "$1" @tp_state_dir)/tasks/$2"; }

# tp_meta <task-dir> <key>
tp_meta() { jq -r --arg k "$2" '.[$k] // empty' "$1/meta.json" 2>/dev/null; }

# tp_pane_state <pane-id> -> running | exited | gone
# (display-message -t on a missing pane succeeds with empty output, so look the
# pane up in the full list instead.)
tp_pane_state() {
  local dead
  dead="$(tmux list-panes -a -F '#{pane_id} #{pane_dead}' | awk -v p="$1" '$1 == p { print $2 }')"
  case "$dead" in
    1) echo exited ;;
    0) echo running ;;
    *) echo gone ;;
  esac
}

# tp_rows <session> — one line per subagent, oldest first, fields separated by
# \x1f (a tab would collapse empty fields in `read`):
#   id worker status tool tokens cost model
# tokens is ↑uncached-input cCACHE(read+write) ↓output; cost is $? when unknown (Claude).
# status merges the reporter's state.json with the pane: a dead pane is done or
# failed:<code> from run.sh's exit file, a missing pane is killed.
tp_rows() {
  local tasks panes meta d pane pstate code row
  tasks="$(tp_opt "$1" @tp_state_dir)/tasks"
  panes="$(tmux list-panes -a -F '#{pane_id} #{pane_dead}')"
  while IFS= read -r -d '' meta; do
    d="$(dirname "$meta")"
    row="$( { cat "$meta"; cat "$d/state.json" 2>/dev/null || echo '{}'; } | jq -rs '
      .[0] as $m | .[1] as $s
      | def k: if . >= 1000000 then "\(. / 100000 | floor / 10)M"
               elif . >= 1000 then "\(. / 100 | floor / 10)k" else tostring end;
      [ $m.id, $m.worker, $m.pane, ($s.state // "starting"), ($s.tool // ""),
        (if $s.tokens then "↑\($s.tokens.input | k) c\((($s.tokens.cacheRead // 0) + ($s.tokens.cacheWrite // 0)) | k) ↓\($s.tokens.output | k)" else "" end),
        (if $s.costKnown == false or ($s.harness == "claude" and ($s.cost // 0) == 0) then "$?"
         elif ($s.cost // 0) > 0 then "$\($s.cost * 1000 | round / 1000)" else "" end),
        ([ ($s.model // (if $m.model == "" then "default" else $m.model end)),
           (if $m.thinking == "" then empty else $m.thinking end) ] | join(":")) ]
      | join("\u001f")' 2>/dev/null)" || continue
    pane="$(cut -d $'\x1f' -f3 <<<"$row")"
    pstate="$(awk -v p="$pane" '$1 == p { print $2 }' <<<"$panes")"
    if [[ "$pstate" == 1 ]]; then
      code="$(cat "$d/exit" 2>/dev/null || echo "?")"
      if [[ "$code" == 0 ]]; then pstate=done; else pstate="failed:$code"; fi
    elif [[ "$pstate" != 0 ]]; then
      pstate=killed
    else
      pstate=""
    fi
    # Drop the pane field; a dead or missing pane overrides the reported state.
    awk -F $'\x1f' -v OFS=$'\x1f' -v ps="$pstate" \
      '{ if (ps != "") { $4 = ps; $5 = "" } print $1, $2, $4, $5, $6, $7, $8 }' <<<"$row"
  done < <(python3 -c '
import os, pathlib, sys
paths = pathlib.Path(sys.argv[1]).glob("*/meta.json")
for path in sorted(paths, key=lambda p: p.stat().st_mtime):
    sys.stdout.buffer.write(os.fsencode(path) + b"\0")
' "$tasks")
}

# tp_queue_lines <state-dir> — one display line per queued task, from tp-agent's own queue validation. An entry that
# is not a complete record shows its file name and why it was refused, never as an ordinary queued row. If the view
# itself cannot run, every entry is shown refused; nothing is read here with jq.
tp_queue_lines() {
  local dir="$1" out file
  if out="$(TP_STATE_DIR="$dir" "$ROOT/bin/tp-agent" queue-view 2>/dev/null)"; then
    [[ -z "$out" ]] || printf '%s\n' "$out"
    return 0
  fi
  for file in "$dir"/queue/*.json; do
    [[ -e "$file" || -L "$file" ]] || continue
    printf '  %s  queued  start refused: the queue view could not run\n' "$(basename "$file" .json)"
  done
}
