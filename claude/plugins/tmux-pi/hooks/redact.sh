#!/usr/bin/env bash
# Redaction gate and output rewrite; the policy and its fail-closed handling live in lib/redact_hook.py.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
exec "$ROOT/bin/tp-redact" --claude-hook
