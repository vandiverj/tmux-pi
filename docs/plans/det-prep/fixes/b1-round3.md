# B1 round 3: per-app checks, a type table, fail closed on fingerprint (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-88290d3d23-165906/result.md and report.md beside it.

## Rules
1. **No aggregate per-app checks.** ESLint is recorded as `eslint:APP` for each changed app, like `typecheck:APP`. Each app gets its own status, so an app without ESLint configuration is `not_applicable` and never hides behind another app's pass. Sweep every check the gate records and split any other aggregate the same way.
2. **One type table.** `lib/gate_record.py` holds one table giving each field's required type and allowed values, at the top level and per check, per status. `status()` validates against it before any comparison or hashing, and any mismatch returns `invalid`. It never raises.
3. **No record without a tree.** If the fingerprint fails, `test-run.sh` exits non-zero and writes no successful record, whatever the test command returned. The same goes for any required field it cannot fill.

## Tests
- One table of mixed-app cases: one app passes and one has no config; both pass; one fails; neither has config.
- A generated type table: every field in turn set to each wrong JSON type (list, dict, number, null, bool, string), each asserting `invalid`.
- A fingerprint shim that fails while the tests pass, asserting a non-zero exit and no success record.

## Report
- Run `test_gate_record` against the base's `lib/gate_record.py` and give the failure count.
- Include a Site / Disposition / Evidence sweep table.
