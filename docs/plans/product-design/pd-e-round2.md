# PD-E round 2: packet context, publication digest, packet scope (Main, 2026-11-10)

Review review-coder-ffa36eecbe-103008 (REQUEST_CHANGES), report, reproductions and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-ffa36eecbe-103008/`
(`report.md`, `reproduce.py`, `reproduce.log`). Keep everything round 1 built and the review confirmed (single readiness rule,
question-only uncertainty acceptance, packet format from `--out`, escaping, lock/expected-revision writes). The bar is
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. All three findings below are realistic.

## F1 (Closes by: code): the Markdown handoff keeps what the worker needs
The `.md` packet (and the case Markdown) must carry the in-scope implementation and verification context that the JSON carries:
contract operations, failure contracts and non-functional requirements of included contracts, scenario seed/actions/expected
results, the case's feasibility/nonblocking notes and the cited claim statements. Anything deliberately abbreviated gets an explicit
targeted link. Escaping stays inert. Test: the documented example in `catalog/skills/prepare-cases/references/case-shape.md` (ready
slice, dependency `scenario.submit-missing-category`) in both formats; every field present in JSON is present (or linked) in Markdown,
checked by code over the JSON packet's fields, not by a hand list.

## F2 (Closes by: code): publication identity covers the title
Retitle a published case through `apply` `set`: the next export proposes an update for its external id. Use a publication-content
digest that includes every published field (title included); PD-B's rule that a title edit does not stale readiness stays unchanged.
Test both: retitle → proposal `update`, readiness and staleness unchanged. Sweep: any other field that `P.semantic` strips but the
exported ticket shows — list them by code and cover each.

## F3 (Closes by: code): a slice dependency is a link, not scope
A case dependency on another slice (`slice.*`) appears as a delivery-dependency link with that slice's readiness; its scenarios,
rules and decisions are not included in the packet or case scope. Direct and transitive dependencies through scenarios, rules,
decisions and contracts keep working as now. Test with the reviewer's `SLICE_DEPENDENCY` flow: the packet includes only the case's own
closure; the blocked dependency slice is named as a link with its state.

## F4 (Main's decision): packets are generated output
Document in docs/product-design.md that `packet` regenerates its file and keeps no annotations (worker notes go elsewhere). No code.

## F5 and F6 (report checks)
- F5: correct the two report claims (every other record appears as a link; stale refusal lists the semantic blocker), or make them
  true and show it. If the answered question of an included decision should be a link, add it.
- F6: enumerate the operation space by code from the changeset operation definitions (`lib/product_design/changes.py`) × the
  dependency kinds a case can reach, and show staleness/export results for each, including `replace` and `accept-decision`. Say which
  rows are enumerated and which are selected.

## Report
The four-cell matrix as in round 1 (same modules), fail-before for each new test against e53a2ee, attack counts before and after,
deviations. Search with safe_grep and read with Read/safe_read; never search `claude/`, `config/`, `agent/` recursively or `bin/` as a
directory; never read `claude/mcp.json`, `.env*` or auth files. Synthetic only; nothing sent to any network address. Commit nothing.
`RESULT_STATUS: complete` as the last line, exactly. No background shells.
