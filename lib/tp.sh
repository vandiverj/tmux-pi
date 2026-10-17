# Shared helpers for tp-* scripts. Source after ROOT is set. bash 3.2 compatible.

TP_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
# One python process builds every row (lib/tp_rows.py); the lifecycle state is joined by the caller.
tp_rows() {
  local tasks panes
  tasks="$(tp_opt "$1" @tp_state_dir)/tasks"
  panes="$(tmux list-panes -a -F '#{pane_id} #{pane_dead}')"
  python3 "$TP_LIB_DIR/tp_rows.py" "$tasks" "$panes"
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

# Writing tmux-pi state without following a link planted at the target (lib/safe_write.py). `>` and `>>` open the path and
# follow a symlink there to any file the user can write; these replace it (tp_write) or refuse it (the others).
# tp_write <path> [--root STATE_DIR] [--mode OCT | --executable | --fresh] — stdin becomes the file: a temp file in the same
# directory, then one rename. --root refuses a link in any directory below it; --fresh gives the mode a new file would get.
tp_write() { python3 "$TP_LIB_DIR/safe_write.py" write "$@"; }
# tp_append <path> — stdin is appended; a symlink at the path is refused.
tp_append() { python3 "$TP_LIB_DIR/safe_write.py" append "$@"; }
# tp_owned_dir <dir>... — mkdir -p for a directory tmux-pi owns; a symlink in its place is refused, not entered.
tp_owned_dir() { python3 "$TP_LIB_DIR/safe_write.py" mkdir "$@"; }
# tp_not_link <path>... — refuse (exit 1, saying so) when any path is a symlink.
tp_not_link() { python3 "$TP_LIB_DIR/safe_write.py" check "$@"; }
