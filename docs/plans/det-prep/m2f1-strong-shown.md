# M2-F1 (STRONG): offer the strong level, gpt-6.1-sol at high

Lane `strong-shown`, step F1. Level: `mid` coder. Base: `main`. Touches: `config/model-catalog.json`,
`tests/test_router_catalog.py`, `tests/test_router_floor_followups.py`, `tests/test_route_resolve.py`,
`tests/test_model_routing.py`, `tests/test_llm_router.py`, `tests/test_routing_evals.py`, `docs/plan.md` (only if it
states strong is hidden).

## Why
M2 review F1 (review-coder-0fef98c95d-073102, Closes by: decision): strong is `shown: false` and its gpt-6.1-sol entry
`in_roster: false`, so the complex route (6.1@high) is inert: explicit 6.1@high is refused and the local-rule redirect is
dropped. Owner decision 2026-11-08: "M2 F1 yes at high": show strong and roster gpt-6.1-sol there at high effort.

## Change
1. Before: on main, show (`tp-route --no-log --level strong ...` or the resolver directly with the committed catalog)
   that strong is refused / resolves to mid today. Save `before.log`.
2. `config/model-catalog.json`: strong `shown: true`; its gpt-6.1-sol entry `in_roster: true`, `efforts: ["high"]`
   unchanged. Update strong's `for` text only if it now misstates the roster (keep "No Claude model sits at this level
   today"). Anything else in the catalog unchanged. The committed catalog uses `openai/` model names: do not introduce the
   `openai-codex/` prefix (that is a local-only edit on Main's checkout).
3. `lib/router_catalog.py` validation must pass (`validate_catalog`). If a rule refuses a shown level for a reason you
   think is wrong, stop and report; do not change validation here.
4. Update the tests that pin "strong is hidden" (e.g. test_router_catalog `level(SHIPPED, "strong")["shown"] is False`,
   test_route_resolve line ~50, test_router_floor_followups
   `test_strong_is_not_offered_in_the_shipped_catalog_so_it_resolves_to_mid`) so they pin the new behaviour: strong
   offered, explicit `--level strong` resolves to gpt-6.1-sol at high, escalation mid@medium -> strong@high on the same
   model, the complex route's local-rule redirect is no longer dropped. Each changed assertion: say the old and new value.
5. Check and report the reviewer consequence (run-log, router_floor): health is keyed provider:model, so a strong outage
   also removes mid's only other-provider reviewer. Show what pick_reviewer does for a mid Claude author when
   gpt-6.1-sol is out, with strong shown. Report; change nothing unless a test proves a regression from this change.
6. Run the five routing modules (test_router_catalog, test_route_resolve, test_llm_router, test_model_routing,
   test_routing_evals) plus test_router_floor_followups and test_contract, both modes, under `/usr/bin/python3` and
   `/opt/homebrew/bin/python3`. Nothing outside `baseline-failing.txt`.

## Done means
1. With the committed catalog, `--level strong` and escalation reach gpt-6.1-sol at high; validate_catalog passes.
2. Tests that pinned "hidden" now pin "offered", each change listed.
3. The reviewer consequence of step 5 is reported with evidence.
4. No new failure outside the baseline, both interpreters, both modes.
