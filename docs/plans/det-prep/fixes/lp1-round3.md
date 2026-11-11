# LP1 round 3: one rule for records and one gate for every pi start (Main, 2026-11-11)

Review: .tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-183dd2a520-083701/result.md and report.md
(probe.py, probe.log beside it). Bar: realistic use. Route: **rule-first** (both findings repeat a round-1 family; round 2 fixed
the reported case, not the rule). Main's diagnosis: the round-2 brief asked to close F1-F3 by the cases named, so the worker
mapped one exact model pair and gated on the `--model` argument; the brief should have stated the rules below.

## Rule A: every recorded model name is the shared id (closes F1, family shared-usage-identity)
The reporter must not depend on the one pair in meta.json. Replace the pair lookup with a reverse of the alias itself:
- `bin/tp-pi` (the one boundary every pi start passes) exports to pi the resolved alias map it applied, e.g.
  `TP_PROVIDER_ALIASES='{"openai":"openai-codex"}'` (only when the local config sets one; unset otherwise).
- `agent/extensions/reporter.ts` reads that variable once. Any model name `<to>/<id>` whose provider `<to>` is an alias target
  is recorded as `<from>/<id>` in `state.model` and every `byModel` key, at session_start, model_select and every usage event,
  for any model id (Sol, Astra, Luna, a model picked later), with or without a task meta.json (pi Main too). The launched
  name is kept in `launched_model` (the latest one). A name whose provider is not an alias target is left alone.
- Drop the meta.json pair lookup if the variable makes it redundant; if you keep it, say why.
Attack table (report, not one test per row): initial model, switch Sol→Astra, switch to an unaliased provider and back, a model
id containing `/`, no variable, an empty or malformed variable (left alone, never crashes the reporter), Main with no meta.json.

## Rule B: a bad alias refuses every pi start (closes F2, family bad-alias-refusal)
`bin/tp-pi` loads and validates the local alias on every start, with or without `--model` (`--model=x` form too), and refuses
with the loader's text and exit 1 before pi starts. Only the rewrite needs `--model`. No alias set: one config read, then the
same behaviour as before. Extend the round-2 tp-pi test table with: no `--model` + bad alias (refused), `--model=` form,
no alias (unchanged argv).

## F3 (Main's, not yours)
Main runs a clean combined-tree discovery after PD1 (lane process-decode) merges. Do not work on it.

## Verification (the worker tests once)
Run tests.test_local_provider, tests.test_router, tests.test_llm_router, tests.test_model_routing, tests.test_orchestration,
tests.test_session_env once with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u
TP_STATE_DIR`), short fresh `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 900; exec @ARGV'`; plus whatever the repo uses to check
the reporter TypeScript (find it with safe_grep; do not install anything). The log starts with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Fail-before for each rule against af81d88. Nothing to 127.0.0.1:1234 or any
network; no real pi or Claude start (fake pi only); private tmux socket only. Do not edit config/. Search with safe_grep, read
with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
