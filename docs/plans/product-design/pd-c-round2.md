# PD-C round 2: the review page keeps feedback exactly, survives bad saved state, and builds stay true (Main, 2026-11-10)

Review review-coder-d3bea0b5b2-003951 (REQUEST_CHANGES). Report, probes and logs:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-d3bea0b5b2-003951/`.
Rerun its probes first (probe.py, extra-probes.py, anchor-probe.py, browser-mutations.py, probe.mjs, category-probe.mjs, crlf-probe.mjs,
restore-depth-probe.mjs) and record each failing before your change. The acceptance flow, the 4074 attack rows, the four round-1 code
fixes and their fail-without tests were confirmed; keep them. The two documented CLI/page differences (`schema_version: 1.0`; a
repeated session) and the 6-process Chrome peak were accepted; leave them.

## Fix (all `Closes by: code`)
Each fix is stated as a rule. For each rule, build its input space by code, run it, and report counts before and after.
- **F1 Feedback values are kept exactly.** The page never re-encodes an imported value it did not create. Numbers outside the
  IEEE-754 safe-integer range, `-0`, exponents and long decimals in imported notes, inputs and state must re-export byte-equal in
  value to what the CLI stores (keep the original text, or refuse at import with a visible reason; do not alter silently). A
  re-export of an imported bundle must import into the CLI as a no-op.
- **F2 Any saved value starts the page.** Every value storage can hold for the page's keys (any JSON value, invalid JSON, wrong
  version, a version-1 object with any field missing, null or of the wrong type) leads to a working page: the stored value is kept
  untouched, a visible notice says it was not read, and the page starts with fresh visit state. No validation path may throw past
  the parser's catch.
- **F3 A build that committed keeps its file.** After the transaction's commit point, nothing deletes the build file. Before it, the
  cleanup removes only a file this build created and whose bytes are still its own (never another writer's). Recovery after an
  interrupted build never commits a manifest entry whose file is absent or differs; status reports that as a trust problem. Attack
  every interruption point in `build` (before and after each write, rename, fsync and commit).
- **F4 What the browser hashes is what CSP lists.** Hash every inlined script and style exactly as the HTML parser will deliver it
  (CRLF and lone CR normalized to LF), or normalize the inlined text before hashing. Attack: LF, CRLF, CR, mixed, BOM, trailing
  whitespace, `</script` and `</style` in strings, for JS and CSS, checked in headless Chrome (the prototype runs, no CSP violation).
- **F5 "Up to date" means the bytes match.** A build is current only when the file's digest equals the recorded one; any other bytes
  give `build.exists` (or the refusal that already exists for foreign bytes), never `up_to_date: true`.
- **F6 Every export the page reports as done can be imported.** Before marking feedback exported, the page checks the bundle against
  the same byte, record and depth limits the importers use. If it is over, the page splits the export into bundles that each import
  (preferred), or refuses with a visible reason and does not mark anything exported. Aggregate note/input limits apply when
  accumulating, not only per item.
- **F7 An open note keeps its whole draft.** Text, category and every other draft field survive every repaint (paused prototype
  action, toolbar navigation, view switch, storage notice). Attack: every draft field × every repaint trigger.
- **F8 Own properties only.** Every lookup keyed by data (schema properties, id indexes, registries, dedup pools) uses own-property
  checks or a null-prototype map; valid ids such as `constructor`, `__proto__`, `toString`, `hasOwnProperty` work, and unknown fields
  with those names are refused exactly as the CLI refuses them. Attack: every `Object.prototype` name as a field and as an id, page
  and CLI agreeing.
- **F9 Explicit remapping (plan section 8).** Add a CLI operation (e.g. `tp-product remap-anchor PACKAGE NOTE TARGET --reason TEXT`)
  that goes through PD-B's atomic changeset path: it records the new target and the rationale, keeps the original anchor unchanged,
  is idempotent, refuses an unknown target, and shows in status. The page shows the remapped target and the original.
- **F10 Anchors are re-checked on every import and build.** Deduplication must not skip anchor re-evaluation: after a rebuild whose
  registry no longer holds a target, every stored note on that target becomes `unresolved` (original kept) and status lists it.
- **F11 Deep input is refused, not crashed.** Depth is measured without recursion (or bounded before recursing); any nesting depth up
  to the byte limit gives the 64-level refusal in the page's import report.

## Main's decisions on the notes
- **F12:** restoring a session resumes the task that was in progress (scenario, step, assistance level), and the next note carries that
  context. Add it to acceptance.
- **F13:** keep the behavior; document in docs/product-design.md that back/reset reopen guided tasks while a completed unguided outcome
  stays recorded as an observation.
- **F14:** correct the report: the runtime digest is in the HTML; the manifest records revision, content and source fingerprints and
  the file.
- **F15:** log the Chrome process peak (sampled process list during the browser test) and the preview-style diff/private-name check
  you ran; attach the raw logs.

## Report
- Fail-before for each new test against 9593440.
- tests.test_product_design_review, tests.test_product_design_store, tests.test_product_design_package, tests.test_skill_catalog,
  tests.test_session_env in all four cells, one after another (not at the same moment): /usr/bin/python3 and
  /opt/homebrew/bin/python3, each outside (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside
  (`env TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`), with `TMUX_TMPDIR` set to a fresh `mktemp -d`. Each log starts with
  `date -u` and `git add -A && git write-tree`; each module's time per cell.
- The attack table for every rule above (input space printed by code, counts before and after), the sweep of every place each rule
  applies, Self-review, deviations.
- Synthetic fixtures only; no private product names; nothing sent to 127.0.0.1:1234 or any network address. Do not change
  `docs/plans/product-design/pd-b-note.md`. Commit nothing. `RESULT_STATUS: complete` as the last line of your final reply, exactly.
  No background shells or Chrome processes left running.
