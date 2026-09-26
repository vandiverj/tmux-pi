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

# tp_rows <session> — one line per subagent, oldest first, fields separated by
# \x1f (a tab would collapse empty fields in `read`):
#   id worker status tool tokens cost model
# status merges the reporter's state.json with the pane: a dead pane is done or
# failed:<code> from run.sh's exit file, a missing pane is killed.
tp_rows() {
  local tasks panes meta d pane pstate code row
  tasks="$(tp_opt "$1" @tp_state_dir)/tasks"
  panes="$(tmux list-panes -a -F '#{pane_id} #{pane_dead}')"
  for meta in $(ls -tr "$tasks"/*/meta.json 2>/dev/null); do
    d="$(dirname "$meta")"
    row="$( { cat "$meta"; cat "$d/state.json" 2>/dev/null || echo '{}'; } | jq -rs '
      .[0] as $m | .[1] as $s
      | def k: if . >= 1000000 then "\(. / 100000 | floor / 10)M"
               elif . >= 1000 then "\(. / 100 | floor / 10)k" else tostring end;
      [ $m.id, $m.worker, $m.pane, ($s.state // "starting"), ($s.tool // ""),
        (if $s.tokens then "↑\(($s.tokens.input + $s.tokens.cacheRead + $s.tokens.cacheWrite) | k) ↓\($s.tokens.output | k)" else "" end),
        (if ($s.cost // 0) > 0 then "$\($s.cost * 1000 | round / 1000)" else "" end),
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
  done
}
