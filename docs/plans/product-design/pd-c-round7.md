# PD-C round 7: the whole restore plan is checked before any write; recovery offers every record each supported CLI accepts (Main, 2026-11-10)

Review review-coder-0ca0f49dc8-064635 (REQUEST_CHANGES): F1-F4 defects, F5 report check. Report, probes and evidence:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-0ca0f49dc8-064635/`.
Rerun its probes first (`evidence/aliases.py` for F1; `evidence/prepare.py`, `evidence/probe.mjs` and `evidence/numeric_oracle.py` for
F2-F4) and record each failing before your change. The review confirmed round 6's tables (492 build-entry rows, 48,061 recovery rows,
every earlier campaign at 0) and deviations 1, 3, 4 and 6; keep them. Main's decisions from rounds 2-6 stand.

## Fix (all `Closes by: code`), each as a rule with its input space built by code, counts before and after
- **F1 The whole restore plan is proved before any file is written.** `build` first computes every target it would write and checks
  the plan as a whole; only a plan that passes is published, and a refused plan writes nothing (so "No build file was written" is true).
  - Two entries name one physical file when their paths are equal after Unicode NFC normalization and case folding of each component,
    whatever the current filesystem does. Such a group is one obligation: restored once when every entry agrees, refused by name when
    they disagree. Do not detect this by catching the second write.
  - A target that is an ancestor directory of another target, or whose parent is an existing file, makes the plan refused by name
    before any write.
  - Decide this in one preflight (`review.py:422-433` and `:474-550`); `_builds_dir` and `_restore` stop discovering conflicts mid-loop.
  - Attack: 1-3 entries per group × spellings (exact, case variants, NFC/NFD, mixed) × agreeing / disagreeing × file present unchanged /
    changed / missing × ancestor/child and file-as-parent layouts, on this disk, then `build` and `status`. Expected: 0 rows where any
    file is written by a refused build, 0 rows where an agreeing group is refused, 0 rows where `build` succeeds and `status` reports a
    problem. This closes round 6's deviation 5.
- **F2 Recovery packing measures, never estimates.** A part is accepted only after its exact serialized bytes are measured against the
  limit; a part over the limit is split again, down to one record. Only a record that is over the limit on its own is refused, by name.
  Packing never turns valid records into "none can be imported". Attack: mixed kinds × 1-5 records × sizes at limit-2 .. limit+2 of the
  combined and of each single record × whitespace padding. Expected: every record that fits alone is offered; every part is ≤ the limit.
- **F3 Numeric checks follow what each supported CLI accepts.** Check the effective parsed value (JSON's last member wins), keeping the
  original text for export: a shadowed `1e400` followed by a finite member is accepted. Effective infinity stays refused.
  Main's decision on digits: the supported CLIs are Apple Python 3.9 and Python 3.14, and a recovered part must import on both. Keep the
  4,300-digit refusal as the documented rule "offered only when every supported CLI accepts it", state it in docs/product-design.md, and
  make the oracle the intersection of both interpreters' real ingest.
- **F4 Every accepted kind is offered, sessions included.** A session record accepted on its own is offered even when there are no notes
  or inputs; the UI counts and exports it. Remove the oracle's session-only exemption (`f23_recovery.py:260-261`) and rerun.
  Deviation 1 (the open session is not synthesized) stays accepted.

## Check
- **F5:** use only the four dispositions (`fixed`, `already correct`, `out of scope`, `unresolved`) with no annotations in the cell; any
  `unresolved` row carries `Closes by:` in its Evidence. Qualify every campaign count with what its oracle excludes. Add the review's
  "Additional sites" rows to your sweep with their final dispositions.

## Report
- Fail-before for each new test against 2a0ee2a.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another (fresh `TMUX_TMPDIR`; each log starts with `date -u`, `TMUX_TMPDIR` and
  `git add -A && git write-tree`, followed by `git reset -q`; each module's time per cell).
- The attack table for every rule (input space printed by code, counts before and after, on both interpreters), the sweep of every
  place each rule applies, Self-review, deviations. Rerun rounds 3-6's attacks on the final tree on both interpreters; if one runs
  past a tool time bound, split it and report each part.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
