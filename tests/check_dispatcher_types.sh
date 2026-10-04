#!/usr/bin/env bash
# Pinned TypeScript check against the installed Pi package's extension + Node types.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
pi_package="${PI_CODING_AGENT_PACKAGE:-$(dirname "$(dirname "$(dirname "$(realpath "$(command -v pi)")")")")}"
if [[ ! -f "$pi_package/package.json" || ! -d "$pi_package/node_modules/@types/node" ]]; then
  echo 'Install Pi with its Node types or set PI_CODING_AGENT_PACKAGE' >&2
  exit 1
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/node_modules/@earendil-works" "$tmp/node_modules/@types"
cp "$root/agent/extensions/dispatcher.ts" "$tmp/dispatcher.ts"
cp "$root/agent/extensions/catalogue.ts" "$tmp/catalogue.ts"
cp "$root/agent/extensions/goal.ts" "$tmp/goal.ts"
cp "$root/agent/extensions/assistant.ts" "$tmp/assistant.ts"
cp "$root/agent/extensions/orchestration.ts" "$tmp/orchestration.ts"
cp "$root/agent/extensions/sensitive.ts" "$tmp/sensitive.ts"
cp "$root/agent/extensions/subagents.ts" "$tmp/subagents.ts"
cp "$root/agent/extensions/escalate.ts" "$tmp/escalate.ts"
printf '{"type":"module"}\n' > "$tmp/package.json"
ln -s "$pi_package" "$tmp/node_modules/@earendil-works/pi-coding-agent"
ln -s "$pi_package/node_modules/@earendil-works/pi-ai" "$tmp/node_modules/@earendil-works/pi-ai"
ln -s "$pi_package/node_modules/@types/node" "$tmp/node_modules/@types/node"
tsc=(npm exec --yes --package=typescript@5.9.3 -- tsc)
if [[ -n "${TP_TSC_BIN:-}" ]]; then tsc=("$TP_TSC_BIN"); fi
"${tsc[@]}" --noEmit --skipLibCheck --module nodenext --target es2023 \
  --typeRoots "$tmp/node_modules/@types" --types node "$tmp/dispatcher.ts" "$tmp/catalogue.ts" "$tmp/goal.ts" "$tmp/subagents.ts" "$tmp/escalate.ts" "$tmp/assistant.ts" "$tmp/orchestration.ts" "$tmp/sensitive.ts"
