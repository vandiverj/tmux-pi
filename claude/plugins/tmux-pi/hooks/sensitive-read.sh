#!/usr/bin/env bash
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if result="$("$ROOT/bin/tp-sensitive-read" --claude-hook)"; then
  [[ -z "$result" ]] || printf '%s\n' "$result"
else
  echo "tmux-pi sensitive-read classifier failed; this tool call cannot be verified" >&2
  exit 2
fi
