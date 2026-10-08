#!/usr/bin/env bash
# Denies tool calls that touch the trust store; runs in every redaction mode. Policy: lib/trust_guard.py.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
if result="$("$ROOT/bin/tp-trust-guard" --claude-hook)"; then
  [[ -z "$result" ]] || printf '%s\n' "$result"
else
  echo "tmux-pi trust guard failed; this tool call cannot be verified" >&2
  exit 2
fi
