# Shared helpers for tp-* scripts. Source after ROOT is set. bash 3.2 compatible.

die() { echo "$(basename "$0"): $*" >&2; exit 1; }

# The tmux-pi session to act on: the current session when inside tmux,
# otherwise $TP_SESSION (default tmux-pi).
tp_session() {
  local s
  if [[ -n "${TMUX:-}" ]]; then
    s="$(tmux display-message -p '#S')"
  else
    s="${TP_SESSION:-tmux-pi}"
  fi
  tmux has-session -t "=$s" 2>/dev/null || die "no tmux-pi session '$s' (start one with: agent)"
  [[ -n "$(tmux show-options -qv -t "=$s:" @tp_state_dir)" ]] || die "session '$s' is not a tmux-pi session"
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
