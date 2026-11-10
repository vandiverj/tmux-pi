# SW1 round 4: effort approval is an input to resolution; a rejected catalog is never read again (Main, 2026-11-09)

Review review-coder-d568636586-222822 (REQUEST_CHANGES). Report, `probe.py` and `probe.log` are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-d568636586-222822/`.
Run `probe.py` on both interpreters before you change anything. Round 3's original cases (missing-catalog xhigh/max capped, roles
null and protected_paths 42 refused with a reason) and the 3549-case shape attack were confirmed; keep them.

## The defects
- **F1:** recovery after a rejected catalog rereads the same file leniently (`lib/model_router.py` ~373-400). An invalid effort
  order `["medium","max","high"]` lets unapproved `max` through; order `[null]` raises `TypeError`.
- **F2:** the final approval cap rewrites xhigh to high without resolving the model again (`lib/model_router.py` ~397-400,
  `lib/route_resolve.py` ~440-447), so a valid catalog whose pi models allow only xhigh yields `pi / openai/gpt-6-astra / high`,
  a pair that catalog does not allow.
- **F3:** 2000 nested JSON arrays raise `RecursionError` in `load_catalog` and again in the fallback's reread; router budget and
  timeout `1e100` pass validation and raise `OverflowError` at the socket timeout (`lib/router_catalog.py` ~52-55, 338-345;
  `lib/llm_router.py` ~147-154; `lib/model_router.py` ~97-104).

## Why the rounds keep coming
Each round patched one exit: first the gate, then the fallback, then the shape. The approval check is still a rewrite applied after
resolution, and the fallback still trusts data the validator refused. This round removes both patterns.

## Main's decisions
1. **A rejected catalog is never read again.** There is one function that reads and parses catalog bytes; it either returns a
   validated catalog or raises `CatalogError` for any content (catch `RecursionError`, `ValueError`, `OverflowError`, `TypeError`
   from the parser and validator there, and nowhere else). The fallback after a `CatalogError` uses a fixed built-in policy defined
   in code (effort scale, ceiling, default worker/model); it reads no catalog bytes. A test checks that the built-in policy agrees
   with the shipped catalog when the shipped catalog is valid. Remove the lenient rereads (`_raw_catalogs`, `_effort_scale` on raw
   data) from every decision path.
2. **Approval is an input to resolution, not a rewrite of its output.** The effort the request may use (requested effort, capped at
   high unless `--approve-effort`) is fixed before resolution. Resolution then chooses only model/effort pairs the active policy
   allows within that cap. If none fits, the route stops with the existing stop message style (no started route) and says why. After
   resolution, one check confirms the returned (worker, model, effort) is an allowed pair and within the cap; a failure of that check
   is a stop, never a rewrite. This applies to plain selection, explicit level, gated retry, `--escalate`, first dispatch and the
   fallback.
3. **Numbers in a catalog are checked at load.** Every number the router reads (list them by code from the readers) must be finite
   and within bounds defined once beside the validator (timeouts and budgets: greater than 0 and at most 3600 seconds unless the
   current shipped value is larger, then document the bound you choose). Out of range is `CatalogError`. In `llm_router.call`, an
   exception while setting up the transport is a failed router attempt, not a traceback.
4. Gated fallback downgrade, ceiling availability and review_plan's CatalogError handling stay follow-ups.

## Attack the rule before you report
- Rule 1: catalog content (valid; missing; empty; invalid JSON; 2000-deep nesting; each wrong type for every key the readers use,
  listed by code; invalid effort scales including the review's two; numbers NaN, inf, 1e100, 0, negative) × request kind (first
  dispatch, plain retry, gated rule-first, gated design, `--escalate`, explicit level) × `--thinking` (none, low, medium, high, xhigh,
  max) × `--approve-effort` (on, off) × worker (claude, pi). Run through the real `tp-agent retry` / dispatch path dry (no live
  dispatch). Expected per row: the full tuple (worker, model, concrete effort, decided_by, source) or the stop message, compared
  with an oracle written from the rules above; no traceback; never above high without approval; every returned pair allowed by the
  active policy.
- Rule 2: the review's F2 catalog and variations (each level's effort_map forced to one value; each worker's models allowed only
  one effort) × the same request axes. Expected: an allowed pair within the cap, or a stop.
- Print each input space by code and report counts on both interpreters. Keep worth-keeping rows as tests; fold `probe.py` in.

## Report
- Fail-before for the new tests against the round-3 checkpoint b2a8ae4.
- The round-3 module set (tests.test_family_sweep tests.test_retry_packet tests.test_task_family tests.test_contract
  tests.test_session_env tests.test_model_routing tests.test_route_resolve tests.test_llm_router tests.test_router_floor_followups
  tests.test_routing_evals tests.test_tp_review tests.test_router_catalog) in all four cells: /usr/bin/python3 and
  /opt/homebrew/bin/python3, each outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside
  (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), after your last edit, one module per invocation with its time.
- **Provenance:** each log starts with `date -u` and the hash of the files that ran: `git add -A` the worktree, then
  `git write-tree` (index tree of the final bytes; do not commit). The review rejected logs that named the old index tree.
- Attack counts per rule, the review's probes rerun, Self-review, sweep table, deviations. Commit nothing; `git add` new files.
  `RESULT_STATUS: complete` as the last line.
