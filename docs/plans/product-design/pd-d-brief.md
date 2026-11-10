# PD-D: orchestrator and skills integration (Main, 2026-11-10)

Read `docs/plans/product-design/pd-brief.md` first (it is the lane brief and wins over the plan where they differ), then the plan it
names in full (sections 9, 10 and 12 Phase D matter most), then `docs/plans/realistic-use-bar.md` and `docs/product-design.md` in this
lane's worktree. PD-A, PD-B and PD-C are closed on this lane; keep their behaviour and tests unchanged.

## Do (plan section 12 Phase D and section 9)
1. **Two selective skills.** Add `product-design` and `prepare-cases` to `config/skill-catalog.json` (the only `config/` file you may
   edit) with explicit markers and narrow phrases, short core `SKILL.md` files that point to optional references and to `tp-product`
   commands that exist. `max_selected` stays 3. Read how the catalog is selected (name the files) and test actual selection:
   each marker selects its skill; ordinary unrelated task text (take the existing catalog tests' examples and realistic dispatch
   texts from this repo's task history) selects neither. `product-design` must separate generated hypotheses from observed user
   evidence; `prepare-cases` must make small observable slices, not one ticket per screen. Reuse `product-fit` for its own purpose.
2. **Harness entry points (lane decision 8).** Claude: a command file under `claude/plugins/tmux-pi/commands/` like `product-fit.md`.
   Pi: find how pi loads skills and prompts in this repo (name the files you read) and use that path; if there is no alias parity,
   document the explicit prompt path. Document no command that does not exist, and show each documented entry point resolving.
3. **Product-review contract (lane decision 7).** A contract file used with `tp-review --contract FILE`, with the plan's criteria
   (value assumptions, journeys, rules, exceptions, testability, scope, evidence), the instruction to compare claims with selected
   underlying sources and exercise the built prototype, separate dispositions for product findings and prototype defects, and the
   rule that AI role-play is never customer validation. Read `bin/tp-review`'s contract handling (do not edit it) and show the
   contract is accepted and that ordinary code review and the provider-independence rule are unchanged. Do not start a reviewer.
4. **New-session recovery.** Record package path, revision, selected slice and next action through `tp-agent objective` (and the
   session index), and show a fresh session recovering them with the package manifest as the durable record. Use a scratch state
   dir and a private `TMUX_TMPDIR`/`-L` socket; no Claude or pi start.
5. **Default behaviour preserved.** Show that ordinary dispatch does not load the product workflow (fail-before for any changed
   shared behaviour against the lane base).
6. **PD-C note.** Account for PD-C's browser test under SLOW_TESTS the way the repo does for other slow tests, and record its timing.

## Attack (realistic-use bar)
Attack the selection rule with realistic task texts: the markers, natural requests for product design and case preparation, and
ordinary coding, review, QA and document requests that must not select them, plus the existing catalog tests' rows. Print the list by
code, report counts before and after. No cross-products of contrived spellings.

## Report
The lane brief's "Done means", with `tests.test_product_design_review`, `tests.test_product_design_store`,
`tests.test_product_design_package`, `tests.test_skill_catalog`, `tests.test_session_env` and your new tests in all four cells, one after
another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`;
each module's time per cell). List every shared file changed. Synthetic fixtures only; no private product names; nothing sent to
127.0.0.1:1234 or any network address. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
