# HB2c round 3 (rule-first, mechanical): "empty" means "" or null, never a falsy value

Review review-coder-d4e53dadcb-193244 F1 (low, contrived): `lib/launch_record.py:586` renders a held version `0` or `false`
as "none", because it tests truthiness. The rule is that only `""` and null are empty.

Rule, for every field in `summary`:
- absent (the key is missing, or the container has the wrong type) reads "unknown";
- held and `""` or null reads "none";
- anything else reads `str(value)`. That includes 0, false and an empty list in a scalar field.

Apply it with one small helper used by the contract version, role and route. `scalar()` has the same truthiness test. The
unsupported list keeps its rule: not a list reads "unknown", an empty list reads "none".

Tests: extend the existing rows in the same summary test in `tests/test_work_brief.py`. Add no new method. Add held `0` and
`false` for the contract version, and held `0` for role. Run `tests.test_work_brief` once through `tools/agent/test-run.sh`
on `/usr/bin/python3`, outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`),
with a fresh short `TMUX_TMPDIR` under /tmp, in the foreground.

Write report.md (review F2): the rule, file:line, each row with its expected text, and the run result.

Touch only lib/launch_record.py and tests/test_work_brief.py. Do not touch the default tmux server. Do not edit config/.
Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
