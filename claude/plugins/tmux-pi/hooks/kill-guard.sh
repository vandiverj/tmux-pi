#!/usr/bin/env bash
# Denies pattern-based kills (pkill, killall, kill $(pgrep ...), xargs kill) in worker sessions. Policy: lib/kill_guard.py.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if result="$(python3 "$ROOT/lib/kill_guard.py" --claude-hook)"; then
  [[ -z "$result" ]] || printf '%s\n' "$result"
else
  echo "tmux-pi kill guard failed; this tool call cannot be verified" >&2
  exit 2
fi
