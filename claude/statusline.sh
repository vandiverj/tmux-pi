#!/usr/bin/env bash
# Claude Code status line for tmux-pi: model and context usage, shown under the input line.
# Claude Code pipes a JSON document on stdin; see https://docs.claude.com/en/docs/claude-code/statusline
input="$(cat)"
command -v jq >/dev/null 2>&1 || { echo "statusline: jq missing"; exit 0; }

model="$(jq -r '.model.display_name // .model.id // "unknown"' <<<"$input")"
size="$(jq -r '.context_window.context_window_size // empty' <<<"$input")"
pct="$(jq -r '.context_window.used_percentage // empty' <<<"$input")"
used="$(jq -r '(.context_window.current_usage // {}) | ((.input_tokens // 0) + (.cache_creation_input_tokens // 0) + (.cache_read_input_tokens // 0))' <<<"$input")"

human() { awk -v n="$1" 'BEGIN { if (n >= 1000000) printf "%.1fM", n/1000000; else if (n >= 1000) printf "%.0fk", n/1000; else printf "%d", n }'; }

if [[ -n "$size" ]]; then
  [[ -n "$pct" ]] || pct="$(awk -v u="$used" -v s="$size" 'BEGIN { printf "%.0f", (s > 0 ? u * 100 / s : 0) }')"
  pct_int="$(printf '%.0f' "$pct")"
  color=32                      # green
  (( pct_int >= 60 )) && color=33   # yellow
  (( pct_int >= 85 )) && color=31   # red
  printf '%s | \033[%dmcontext %s%% (%s / %s)\033[0m' "$model" "$color" "$pct_int" "$(human "$used")" "$(human "$size")"
else
  printf '%s | context n/a' "$model"
fi
