# G2-E — Deno is optional: no tmux-pi test needs a real Deno

## Owner decision (2026-11-08)
"just do not require it on machines not using it." Deno stays where it improves checking the work projects, and only
when the TARGET project has deno.json / deno.jsonc / deno.lock. tmux-pi itself must run and pass its own suite on a
machine with no Deno installed. Product behaviour does not change in this lane.

## Cause (baseline line)
`ERROR: test_r9_the_reach_walk_takes_nothing_there_as_nothing_and_a_file_it_cannot_look_at_as_unproven
(test_ctx_nav_api.IncompleteIndexCase)` — tests/test_ctx_nav_api.py, IncompleteIndexCase. It calls
`nav.imports("src/main.ts", source="deno")` with the default `deno="deno"`, i.e. the real binary, and carries no
`NEEDS_DENO` gate (line 33). On this Mac `deno` is not on PATH, so the answer is 'unavailable' ("deno was not found on PATH").

## Rule (fix the rule, not the one site)
1. A tmux-pi test that checks tmux-pi's logic never needs a real Deno: it uses `fake_deno` / `scripted_deno`
   (ReviewFixesCase line ~651; move `scripted_deno` up to `Tree` if that is the clean way to share it).
2. A test whose point is to exercise a real Deno is gated with `NEEDS_DENO` (or `NEEDS_SANDBOX`) and skips with the reason.
3. A guard test enforces 1–2: no test method in tests/ reaches the real deno ungated. Pick a provable mechanism, e.g.
   run under a PATH whose `deno` is a sentinel script that records any call, or patch the nav default so an ungated
   real-deno call fails loudly naming the test. Show it catches the r9 test before your fix (paste the failure).

## Done means
- The r9 reach-walk test runs WITHOUT Deno and still proves both branches (ok, and refused/unreadable_file); it does
  not become a skip.
- Family sweep: list every test in tests/test_ctx_nav_api.py, test_ctx_surface.py, test_localdocs.py, test_agent_tools.py,
  test_docs_cli.py that would call a real deno; each is either faked (rule 1) or gated (rule 2). Table in the report.
- The guard (rule 3) exists, and its before/after output is in the report.
- Those five modules pass on this machine (no Deno) under both Pythons × both modes; give the skip counts with reasons.
- One sentence in docs/lanes.md: Deno is optional and project-triggered; tmux-pi needs it only to check a Deno project.
- No change to lib/, bin/ or tools/ behaviour. Do not edit docs/plans/det-prep/baseline-failing.txt (Main owns it).
