# N2: "no failing tests" must never start an investigation

## Problem (owner report, 2026-11-08)
- Workers write `FAILING_TESTS: none` with some extra wording. `lib/failing_tests.py` `parse()` only treats an exact inline match of `none`/`n/a`/`no`/`nothing`/`-`/`[]` as "no block".
- Every other wording becomes a block. `tp-agent` index then sets `failing_tests`, and the Claude hook (`claude/plugins/tmux-pi/hooks/register.tsx` `hint`) tells Main to dispatch `--investigate-failure`.
- Measured with the current parser, these all wrongly parse as a block:
  - `FAILING_TESTS: none.`
  - `FAILING_TESTS: None (all pass)`
  - `FAILING_TESTS: none (nothing was run).` (a real result.md this session)
  - `FAILING_TESTS:` with `none` on the next line
  - `FAILING_TESTS: **none**`
  - `FAILING_TESTS: none — every module passed`
  - `FAILING_TESTS: No failures`

## Rule (closes the family: decide by structure, not by matching negative words)
1. **A report exists only if the block has at least one bulleted entry** (`- ...` or `* ...`) that names a test.
   - Text on the header line, and lines that are not bullets, never create an entry. Keep such text in `text` as a note. `collect` still shows it, and it never triggers.
   - A bullet that names no test also creates no entry. Examples are `- none`, `- n/a`, `- no failures` and `- nothing failed`: after markdown and punctuation are stripped, it starts with a negative word and has no `test:`/`command:` field.
   - A block with no entries returns `None`, the same as having no block.
2. **Do not lose the real reports.** Unchanged behaviour is required for:
   - bulleted free-text entries such as ``- `tests.test_x.T.test_y` fails with `20 != 16`.``;
   - entries with fields;
   - malformed entries that still name a test.
3. **Prompt.** In `bin/tp-spawn` (the FAILING_TESTS packet line), add: "When no test fails that you do not own, leave the block out entirely; do not write FAILING_TESTS: none." Update any golden packet fixtures this changes. Do not change the Main prompts.
4. **Not run is not failing.** "Nothing was run" must never trigger an investigation. Do not add a new signal in this lane. Record it as a candidate in result.md.

## Tests (each must fail without the fix; show the failures in result.md)
- **Negative table** in `tests/test_failing_tests.py`: every phrasing above, plus `- none` / `- n/a` bullets, bold and backtick forms, `FAILING_TESTS:` followed by a blank line and then prose, and `FAILING_TESTS: 0`. All return `None`.
- **Positive table:** at least five real entry shapes, taken from the shapes seen in `.tmux-pi/sessions/*/tasks/*/{result,report}.md`. Rewrite them as fixtures with neutral test names. Each returns entries and the same `malformed` list as today.
- **End to end:** a settled task whose result.md says `FAILING_TESTS: none (nothing was run).` gets no `failing_tests` in `tp-agent` index output, so the hook hint is the plain `collect` one. Use the existing index test harness; do not start real workers.

Run `test_failing_tests`, plus whichever test modules cover tp-spawn packets and the tp-agent index, one module at a time. Put the table in result.md.
