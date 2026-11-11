# LP1 round 2: refuse a bad alias on every route, keep the shared name in usage, cover every pi launch (Main, 2026-11-11)

Review review-coder-30849c9fc9-070442 (REQUEST_CHANGES): `result.md`, `report.md`, `probe.py`, `probe.log`, `usage-probe.mjs`,
`usage-probe.log` in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-30849c9fc9-070442/`.
The LP1 brief (`lp1-local-provider-alias.md`, this dir) still applies. Bar: realistic use. Lane base for fail-before: 7e8c867. Kind
of round: mechanical, the brief's own rules not met (Sonnet).

## Main's diagnosis of round 1
Worker misses of stated rules: "a bad value refuses" was met in the loader but not on the router path that catches it (F1); "cost/
usage keys keep the shared identity" was reported as out of scope although the brief names it (F2); "every other place that starts
pi with a model" missed `bin/tp-main` → `bin/tp-pi` and `tools/qual/summarize_run.py` (F3). F4 (full discovery on the combined tree)
is Main's and is being run. F5 is a report correction.

## The rules
1. **A bad alias refuses on every route (F1).** An invalid `provider_aliases` refuses before any pi is started, through every entry
   that can start pi: the direct spawn path and `lib/model_router.py` → `lib/llm_router.py` (the router must not continue with
   `config=None` after an alias error). Same error text as the loader. One table row per entry route, using the review's probe.
2. **Usage and records keep the shared name (F2).** With the alias set, `state.json` `model`, the usage/cost buckets (`byModel`,
   `lib/tp_usage.py` groups), and the records in `lib/contract.py` and `lib/task_family.py` carry the shared `openai/...` name. The
   launched name is kept beside it in its own field (e.g. `launched_model`), never as a key. How the reporter learns the shared name
   (e.g. from the task's `meta.json`, or an env value tp-spawn sets) is your choice; state it. No alias → records identical to before.
   Remove the deviation note in `docs/legacy-workflow.md` and state the rule instead.
3. **Every pi launch with a model (F3).** Apply the alias at one common boundary that `bin/tp-spawn`, `bin/tp-pi` (used by
   `bin/tp-main`) and `tools/qual/summarize_run.py` all pass through, or at each, with no double rewrite (an already-local name is
   left alone). Inventory `agent/extensions/assistant.ts` and any other pi start you find (list each with file:line and what you did).
   Claude launches stay untouched.
4. **Report (F5).** Count the setting table's rows from the file (the review counts 27), correct the fail-before count, and keep
   the fail-before log in the evidence directory.

## Tests (tests earn their place)
Extend `tests/test_local_provider.py`: rows for each route in rule 1, the reporter/usage keys in rule 2 (reuse the review's
`usage-probe.mjs` approach: real reporter code, synthetic events, no model), and each launcher in rule 3 with a fake pi that logs its
argv. Fail-before against 7e8c867. Report added vs extended.

## Verification (the worker tests once)
Run tests.test_local_provider, tests.test_router_catalog, tests.test_llm_router, tests.test_orchestration, tests.test_session_env and
any module importing what you touch, once, with `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u
TP_TASK_ID -u TP_STATE_DIR`), under `perl -e 'alarm 900; exec @ARGV'`, the log starting with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree`, then `git reset -q`. Main runs the four cells and the full discovery. Do not edit `config/`; temp
configs via `TP_LOCAL_CONFIG`. No live pi/Claude start, no network, nothing to 127.0.0.1:1234. Never read agent/auth.json,
agent/mcp.json, claude/mcp.json or .env*. Search with safe_grep, read with Read/safe_read. Commit nothing. No background shells.
`RESULT_STATUS: complete` as the last line.
