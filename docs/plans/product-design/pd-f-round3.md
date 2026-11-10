# PD-F round 3: a note keeps its recorded task position when the scenario is later revised (Main, 2026-11-10)

Review review-coder-4834ca0f7e-141544 (REQUEST_CHANGES), report and reproduction:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-4834ca0f7e-141544/`
(`report.md`, `repro-old-note.mjs`, `repro-old-note.log`, `old-note-repro*/`). `pd-f-brief.md` and `pd-f-round2.md` still apply; keep
everything rounds 1-2 built and the review confirmed (spaced-path recovery, separated inert context, "all steps done" for a note taken
after the last step). Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. F1 is realistic.

## F1 (Closes by: code): completion is what the note recorded, not a comparison with today's scenario
`templates/product-design/runtime/runtime.js` `stepText` (~584-588) decides "all steps done" with `n.step >= actions.length` of the
**current** scenario. A note taken at step 4 of 5, imported after the scenario was cut to 3 steps, now reads "all steps done".
- A note records completion explicitly when it is captured (round 2's `step: null` for a guided note after the last step, or an
  explicit `completed` marker; say which and why). Display, Feedback list, summary and exported bundle use only what the note recorded.
- An older note that holds a numeric step is shown as that step (`step N`), whatever the current scenario's length. Treat it as
  completed only when the note itself proves it (e.g. it carries the originating build's revision/scenario fingerprint and that
  scenario's step count, and they match). Otherwise show the recorded step; never infer completion from the current build.
- Keep old exported bundles importable; no note changes on import.
- Search for every other place that compares a stored note's step with the current scenario (panel, cards, summary, export, re-review
  digests, case drafts) and apply the same rule; list them by code.

Tests: the reviewer's flow as a browser test through the PD-C driver (note at step 4 of 5 exported; scenario revised to 3 steps via
`tp-product apply`; rebuild; import the untouched bundle; the note still shows step 4, and the summary does not say "all steps done"),
plus a completed-note row that stays completed after the same revision. Fail-before against 1e2bae5, pass after.

## F2 (Closes by: check): handoff paths
`evidence/handoff.md` (copied into this task's evidence) names the round-2 demo under the round-1 task directory, where it does not
exist. Point every path at a file that exists (this task's evidence dir) and check each path by code in the report.

## F3 (Closes by: check): screenshot
Take one fresh headless screenshot of the review page with a note whose context crosses table cells and a completed guided-task note
visible; keep it under `evidence/` and say what you saw. Main will look at it.

## Report
The four cells as in round 2 (same modules), one after another, fresh `TMUX_TMPDIR`, each log starting with `date -u`, `TMUX_TMPDIR`
and `git add -A && git write-tree` then `git reset -q`; per-module times; fail-before for each new check. Search with safe_grep, read
with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`,
`.env*` or auth files. Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address.
Commit nothing. The report's **last line** must be exactly `RESULT_STATUS: complete` (rounds 1-2 omitted it). No background shells.
