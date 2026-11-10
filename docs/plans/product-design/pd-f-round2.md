# PD-F round 2: demo recovery with spaces in the path, note context and step label (Main, 2026-11-10)

Review review-coder-62603cdc4e-131031 (REQUEST_CHANGES), report and reproductions:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-62603cdc4e-131031/`
(`report.md`, `demo-spaces.log`, `demo with spaces/demo.json`, `demo-plain.log`). `pd-f-brief.md` still applies. Keep everything
round 1 built and the review confirmed (48/48 demo checks, routine-use doc checked against --help, F3 dependency state, coverage map,
docs-mutation attack). Main's commit e7b5a17 (sibling import by top-level name in tests/test_product_design_cases.py) stays. Bar:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`. All three are realistic.

## F1 (Closes by: code): an output directory with spaces recovers
`tools/smoke/product_design_demo.py` step 9 parses the saved objective with `package=(\S+)`, so `--out "…/demo with spaces"` fails the
two recovery checks. Recover the package path without treating a space as its end (read it the way the documented convention writes
it, or from a structured field if the objective/manifest has one; say which and why). Do not refuse ordinary directories. If the
documented objective convention in docs/product-design.md has the same spaces limit for normal users, fix it there too, or state why
not. The demo test runs one round under a path with a space (the existing `--no-browser` run is enough) and asserts the recovery
checks pass.

## F2 (Closes by: code): a note after a completed guided task names a real step
`templates/product-design/runtime/runtime.js` (~553-583): after the last step completes, a note keeps `step` equal to the step count
(0-based past the end) and the page shows "step 6" of 5. Record and show the task as completed (e.g. the last step, or a `completed`
marker without a step), consistently in the stored note, the panel, the Feedback list and the exported bundle. Keep old exported
notes importable.

## F3 (Closes by: code): note context separates table cells
`contextOf` (~559-564) uses a row's `textContent`, giving "S-1Laptop requestHardwareWaiting-". Join cell (and other block child) texts
with a visible separator (e.g. " · ") and collapse whitespace; keep the stable target id and label unchanged and the text inert.

## Tests
Browser checks through the PD-C driver for F2 and F3 (fail before against e7b5a17, pass after), the demo spaces run for F1, and the
demo's own checks updated if their expected text changes. Update the handoff (`evidence/handoff.md` of coder-62603cdc4e, copied into
this task's evidence) to drop R1/R2 from the deferred list.

## Report
The four cells as in round 1 (same modules plus tests.test_product_design_demo), one after another, fresh `TMUX_TMPDIR`, each log
starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree` then `git reset -q`; per-module times; fail-before for each
new check. Search with safe_grep, read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/`
recursively; never read `claude/mcp.json`, `.env*` or auth files. Synthetic fixtures only; no private product names; nothing sent to
127.0.0.1:1234 or any network address. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
