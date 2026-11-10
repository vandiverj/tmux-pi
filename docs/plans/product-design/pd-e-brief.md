# PD-E: reviewed cases and worker packets (Main, 2026-11-10)

Read `docs/plans/product-design/pd-brief.md` first (the lane brief; it wins over the plan where they differ), then the plan it names in
full (sections 10, 11 and 12 Phase E matter most), then
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md` and `docs/product-design.md` in this lane's
worktree. PD-A..PD-D are closed; keep their behaviour and tests unchanged. The `prepare-cases` skill (PD-D) already names
`tp-product cases` and `packet` as not built yet: build them now and update that text.

## Do (plan section 12 Phase E)
1. **Slice readiness for cases.** Use PD-B's readiness rule (lane decision 9) as the one source; do not add a second readiness path.
   A case is blocked, by name, when a material dependent question is unresolved, a required review is stale, an accepted rule lacks
   usable examples, or a required contract contradicts known evidence (plan section 11). Recorded human acceptance of bounded
   uncertainty (scope and rationale, named person) unblocks it; no generic force flag.
2. **`tp-product cases PACKAGE_DIR --slice ID --out DIR`**: deterministic rendering of model-prepared case definitions (stored in the
   package through the documented changeset path) to Markdown and structured JSON, with stable local keys, package revision and
   slice fingerprint. No model call. Refuses a blocked slice with the actual missing decision.
3. **`tp-product packet PACKAGE_DIR --case ID --out FILE`**: only the case's scenarios, decisions, source excerpts/anchors,
   dependencies, verification requirements and package fingerprint, with links for the rest. It checks staleness first and refuses
   (by name) a stale case. No live Git branch/base/worktree facts in the packet.
4. **Staleness and impact.** Changing a shared rule marks every affected case stale; an implementation discovery can reopen a design
   question and mark its case stale while keeping its published id and prior accepted revision. `tp-product impact` if the plan's
   section 10 row is needed for this; say whether you built it.
5. **Publication mapping data only**: local case key + published revision → external id, kept in the package. No tracker calls, no
   credentials, no project ids; a re-export proposes updates, never duplicates, and never overwrites an independently edited record
   silently. Automatic publication is out of scope.
6. Mutations use PD-B's lock, expected-revision and atomic-write mechanism; reads change nothing.

Acceptance (plan 12E) on the synthetic fixture: the ready slice exports a useful case and scoped packet; the blocked slice reports its
actual missing decision; changing a shared rule marks all affected cases stale; exports keep stable keys and package provenance.

## Attack (realistic-use bar)
For each rule above, the input space from the schema and normal flows: case states × readiness states × blocking reasons, the
changeset operations that touch a rule, decision, scenario or contract a case depends on, re-export after edits, reopen after
publication. Print it by code; counts before and after. Packages only as tp-product writes them or as changed through its documented
commands; no hand-edited manifests. Escaping: every case/packet field rendered into Markdown is inert.

## Report
The lane brief's "Done means": tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package,
tests.test_product_design_integration, tests.test_skill_catalog, tests.test_session_env and your new tests in all four cells, one after
another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree` run in the real index,
then `git reset -q`; each module's time per cell). Attack table, sweep, Self-review, shared files changed, deviations. If you need a
decision from Main, send `tp-agent request` and keep working on the rest. Search with safe_grep, read with Read/safe_read; never
search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files.
Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Commit nothing.
`RESULT_STATUS: complete` as the last line, exactly. No background shells.
