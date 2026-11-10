# SW1 round 2: a gated retry keeps every constraint normal routing applies (Main, 2026-11-09)

Review review-coder-81dc9a8dd3-201503 (REQUEST_CHANGES). Its report, `reproduce.py`, `reproduce.log` and `consumers.log` are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-81dc9a8dd3-201503/`.
Run `reproduce.py` before you change anything. The packet text (items 1, 2, 4 of the SW1 brief) was confirmed; do not rework it.

## The defects
- **F1:** from a gated Sonnet/medium lineage, `retry --route rule-first --worker claude --task 'Fix lib/sensitive_read.py and
  run its tests.'` gets Opus/**medium**; the catalog's protected-path floor needs **high**, and normal routing applies it.
  `escalation_route` builds the card from the *previous* request (`request.md`) before the replacement task exists, and
  `model_router.escalate_route` puts no `paths` in `ctx` (bin/tp-agent ~1565-1580, lib/model_router.py ~365-371).
- **F2:** `--thinking high` alone turns the gated upgrade off (Sonnet/high). The SW1 brief exempts only `--model` and
  `--level`.
- **F3:** with `TP_MODEL_CATALOG` pointing at a missing file, a gated retry dies with an uncaught `CatalogError`. Before SW1,
  normal selection fell back to the validated catalog; `except ValueError` does not catch `CatalogError`.

## Main's decision: one rule
- **Rule:** escalation picks a stronger level; it never loosens a constraint that normal selection of the *actual retry
  request* applies. Every constraint normal routing evaluates (protected-path effort floors, local-only, approvals,
  `--worker`, the effort ceiling, the catalog fallback) is evaluated on the request the retry will dispatch: the replacement
  task text, its context and its paths. Build it so escalation and normal selection share one place that applies those
  constraints (no second list of constraints in the escalation path that can drift).
- Order: assemble the replacement task (brief sentence + original) first, then route it.
- `--thinking` is an effort request, not a model choice. With a gated retry and `--thinking X`, escalate the level and
  apply effort X, subject to the floors and the ceiling (above high still needs `--approve-effort`). Only `--model` and
  `--level` turn off the gated level-up. `--escalate` with `--thinking` stays refused as today. Update docs/lanes.md.
- Catalog errors: a gated retry whose catalog cannot be read behaves as a plain retry does today (the same validated
  fallback) and prints why it was not escalated. No traceback for any catalog state.
- F4 (plain pinned-worker escalation returns unchanged) stays a follow-up; do not change plain `--escalate` behavior.

## Attack the rule before you report
Input space: retry kind (plain, gated rule-first, gated design, `--escalate`) × user flags (none, `--model`, `--level`,
`--thinking` low/high/above-high with and without `--approve-effort`, `--worker`) × request (ordinary path, protected path in
the new task only, protected path in the old task only, local-only lineage) × catalog (normal, at ceiling, missing file,
unreadable, invalid JSON). Print the list by code, dry-route each case through the real `tp-agent retry` code path (no live
dispatch), give the expected worker/model/effort/decided_by and the reason, and report counts. For each case also run normal
selection on the same request and show that the retry's effort is never below normal selection's floor. Keep the rows
worth keeping as tests (tests/test_model_routing.py or tests/test_retry_packet.py). Fold the reviewer's reproduce.py in.

## Report
- Fail-before for the new tests against the SW1 checkpoint 53c4052.
- The SW1 brief's modules plus `tests.test_route_resolve tests.test_model_routing tests.test_family_sweep tests.test_session_env`
  in all four cells (both interpreters, outside and inside tmux) after your last edit, each file's time per cell.
- F5: for every verification claim in your report, the checkpoint or tree hash it ran on and when (log timestamps), so the
  order of last edit and last run is shown.
- Attack table counts, Self-review, deviations. Commit nothing; `git add` new files. `RESULT_STATUS` as the last line.
