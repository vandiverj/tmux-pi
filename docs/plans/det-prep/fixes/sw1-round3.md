# SW1 round 3: approval and catalog shape hold on every path, fallback included (Main, 2026-11-09)

Review review-coder-4ad33dd694-213016 (REQUEST_CHANGES). Its report, `probe.py` and `probe.log` are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-4ad33dd694-213016/`.
Run `probe.py` on both interpreters before you change anything. Round-2 F1, F2 and the original F3 case were confirmed fixed;
do not rework them.

## The defects
- **F1:** with the catalog missing, a gated Sonnet/medium retry with `--route rule-first --thinking xhigh` and no
  `--approve-effort` starts at **xhigh** (`max` too). The catalog-error fallback (lib/model_router.py ~268-272) returns
  `thinking or t` without the approval check. Plain selection has the same hole.
- **F2:** a catalog that parses but has the wrong shape (`"roles": null`, `"protected_paths": 42`) ends a gated retry with an
  uncaught `AttributeError` / `TypeError` (lib/model_router.py ~256-258; lib/router_catalog.py ~286, 302, 332).

## Main's decisions
1. **Effort approval is checked in one place that every route result passes through**, the catalog-error fallback
   included, for plain and gated requests alike. Above high without `--approve-effort` is refused (or capped, whichever
   normal selection does today; do the same) on every path. No path returns a user effort that has not passed that check.
2. **A catalog either loads as a valid shape or raises `CatalogError`.** Shape validation happens at load
   (lib/router_catalog.py), covering every key and nested value the router reads, so callers never meet a wrong type later.
   Every caller then gets the existing fallback with the reason printed. No traceback for any catalog content.
3. **F4 decision:** plain `--escalate` honoring protected-path floors is accepted: escalation never loosens a constraint.
   Say it in docs/lanes.md.
4. Gated fallback downgrade and ceiling availability (review F3) stay follow-ups.

## Attack the rule before you report
- Rule 1: retry kind (plain, gated rule-first, gated design, `--escalate`, first dispatch) × `--thinking` (none, low, high,
  xhigh, max) × `--approve-effort` (on, off) × catalog (normal, missing, invalid JSON, wrong shape). Expected: no started
  route above high without approval.
- Rule 2: for every key the router reads from the catalog (list them by code from the reader, not by hand), each wrong
  type (null, number, string, list, dict, bool) and a missing key × plain and gated retry and first dispatch. Expected:
  `CatalogError` at load, the fallback, a printed reason, exit as today, no traceback.
- F5: every attack row prints and checks the full expected tuple (worker, model, concrete effort, decided_by) and the
  reason, not only properties. Print each input space by code, dry-route through the real `tp-agent retry` / dispatch
  code path (no live dispatch), report counts. Keep worth-keeping rows as tests; fold `probe.py` in.

## Report
- Fail-before for the new tests against the round-2 checkpoint 6116b8d.
- The round-2 module set (the SW1 brief's modules plus `tests.test_route_resolve tests.test_model_routing
  tests.test_family_sweep tests.test_session_env`) in all four cells: /usr/bin/python3 and /opt/homebrew/bin/python3, each
  outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`env TMUX=/tmp/x,1,1
  TP_SESSION=dummy TP_TASK_ID=dummy`), after your last edit. Each log starts with `date -u` and the tree hash; time per
  file per cell.
- Attack counts, Self-review, deviations. Commit nothing; `git add` new files. `RESULT_STATUS: complete` as the last line.
