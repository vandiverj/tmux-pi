# PD-F: integration demonstration, documentation and handoff (Main, 2026-11-10)

Read `docs/plans/product-design/pd-brief.md` first (the lane brief; it wins over the plan where they differ), then the plan it
names in full (sections 12 Phase F, 13 and 14 matter most), then
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md` and `docs/product-design.md` in this lane's
worktree. PD-A..PD-E are closed; keep their behaviour and tests unchanged except where this brief says.

## Do (plan 12F and 13)
1. **One reproducible demonstration script** (e.g. `templates/product-design/examples/demo.sh` or a Python script; say why) that runs
   the plan's section 13 round on the synthetic review-queue fixture through the real commands only (`tp-product` and, for step 9,
   `tp-agent objective` with a scratch state dir): submit/see across pages, guided and unguided review, two reviewers' notes, two
   feedback exports with one imported twice (exactly one copy of each note), a decision and a revised rule, the affected slice stale
   and the unrelated slice still ready, re-review accepted, case drafts and a bounded packet, and recovery of package path and next
   action in a fresh process. Browser steps use the PD-C headless-Chrome driver (fresh `--user-data-dir`, skip with a reason when
   Chrome or Node is missing). Everything it writes goes to one output directory given on the command line (default a `mktemp -d`);
   nothing in the worktree changes. Print each step and its check.
2. **A test that runs the demo** end to end (under SLOW_TESTS if it is slow, the way the repo marks slow tests) and asserts each
   step's observable outcome, not only exit codes.
3. **User guide.** `docs/product-design.md` gets a short routine-usage section (plan section 14's "routine usage"): start a package,
   build and share the review page, import feedback, decide, re-review, prepare cases and packets, recover in a new session, both
   harness entry points. Every command it shows exists and its `--help` matches; check that by code.
4. **Command help is current.** `tp-product --help` and each subcommand's help list what exists; test it.
5. **PD-E note F3:** the exported case draft shows each delivery-slice dependency's readiness, as the packet already does.
6. **Repository checks.** Confirm the README commands (`python3 -m unittest discover -s tests -p 'test_*.py'` and
   `tests/check_dispatcher_types.sh`) are still current. Run the type check if any extension surface changed, and say so. Do not run
   the whole discovery suite yourself; Main runs it on the merged preview.
7. **Handoff draft** (plan section 14) as `evidence/handoff.md`, not a tracked file: commands, harness entry points, new/changed
   files (shared files called out), tests and times, demo HTML path and a sample exported feedback bundle, a sample prepared case and
   worker packet (produced by the demo into its output dir; copy them under `evidence/`), the exact launch command, deferred items,
   and the real-product pilot as the remaining operational step (out of scope here; no private names).

Manually inspect the rendered review page once (headless screenshot is fine) for readable highlights, focus, note placement and
state visibility; keep the screenshot under `evidence/` and say what you saw.

## Attack (realistic-use bar)
The demo is the attack: each step's expected outcome checked by code. Add the realistic edge rows the plan's section 13 list names
that are not already covered by PD-A..E tests (map each list item to the test or demo step that covers it, by code, and report the
gaps you filled). No contrived inputs.

## Report
The lane brief's "Done means": tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package,
tests.test_product_design_integration, tests.test_product_design_cases, tests.test_skill_catalog, tests.test_session_env and your new
tests in all four cells, one after another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and
`git add -A && git write-tree` run in the real index, then `git reset -q`; each module's time per cell). The section-13 coverage map,
Self-review, shared files changed, deviations. Search with safe_grep, read with Read/safe_read; never search `bin/` as a directory or
`config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files. Synthetic fixtures only; no private
product names; nothing sent to 127.0.0.1:1234 or any network address. Commit nothing. `RESULT_STATUS: complete` as the last line,
exactly. No background shells.
