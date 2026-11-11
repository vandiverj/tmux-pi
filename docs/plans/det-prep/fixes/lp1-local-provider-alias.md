# LP1: a machine-local provider alias for pi launches (Main, 2026-11-11)

## Owner request
On this Mac, pi is logged in with the `openai-codex` provider, so the owner edited the shared `config/router.json` and
`config/model-catalog.json` (uncommitted) to rename `openai/gpt-6-luna`, `openai/gpt-6.1-sol`, `openai/gpt-6-astra` to
`openai-codex/...`. Code and tests still use `openai/...` (`lib/builtin_route_policy.py:15`, `tests/lane_fixture.py:18`,
`tests/route_fixture.py:26-31`, about 50 test references), so the suite run from main's working tree fails ~635 tests. Owner
decision: "Keep it local to this Mac." It matches the HUB rule "Keep machine-specific credentials and settings separate from portable
changes". Bar: realistic use. Lane local-provider, base main.

## What to build
1. **A local setting.** `config/local.json` (already machine-local and git-ignored, read by `lib/router.py` `load_local`) may set
   `"provider_aliases": {"openai": "openai-codex"}`: shared provider prefix → the pi provider this machine launches it with.
   Validate it in `load_local` (strings, provider-name shape, no chains, no alias to itself); a bad value refuses with a clear error.
   No file or no key → no change in behavior.
2. **Applied only where pi is launched.** The shared identity stays `openai/...` everywhere: roster, catalog, routes, task
   `meta.json`, review provider-diversity checks, cost/usage keys, tests. Only the `--model` value handed to pi (`bin/tp-spawn`
   pi branch near line 721, and every other place that starts pi with a model: find them with safe_grep over named files, list
   each) is rewritten from `openai/x` to `openai-codex/x`. Record the launched model next to the shared one where the task records
   its model, so it is visible which provider pi actually used.
3. **Diagnostics.** Whatever command already reports the routing/catalog state (e.g. `tp-agent` doctor/status, or the router CLI)
   shows the active aliases. Document the setting where local.json is documented (find it), with this Mac's example.

## Tests
Unit tests for validation and for the launch rewrite (a fake `tp-pi` that logs its argv; private tmux socket and TMUX_TMPDIR if
any test needs tmux). Prove: alias applied at launch; meta keeps `openai/...` plus the launched name; no alias → identical argv to
before; claude workers untouched; a non-matching provider untouched; review provider-diversity unchanged with the alias set. An input
table for the setting (missing, empty, wrong types, chain, self-alias, unknown keys) with the expected result per row from this brief.

## Constraints
Do not edit `config/` files (the owner's `config/local.json` and uncommitted shared files stay as they are; tests use temp configs
via `TP_LOCAL_CONFIG`). Do not read `agent/auth.json`, `agent/mcp.json`, `claude/mcp.json` or `.env*`. No live pi/Claude start,
no network, nothing to 127.0.0.1:1234. Search with safe_grep, read with Read/safe_read. Four cells (py3.9/3.14 × inside/outside
tmux) over the touched modules plus `tests.test_session_env tests.test_orchestration tests.test_router_catalog tests.test_llm_router`,
each log starting with `date -u`, `TMUX_TMPDIR` (short /tmp path) and `git add -A && git write-tree`, then `git reset -q`; and one
full single-process discovery on a clean tree with 0 failures. Commit nothing. No background shells. `RESULT_STATUS: complete` as
the last line.

## After merge (Main, with the owner)
The owner adds `"provider_aliases": {"openai": "openai-codex"}` to `config/local.json` (or tells Main to), saves the current diff of
the two shared files, and reverts them; then the full suite runs from main's working tree.
