# HB2c round 2 (mechanical): a contract entry's version follows the same three-way rule

Review review-coder-1ef1088697-192351 F1 (low, contrived): `lib/launch_record.py:584` renders a contract entry that holds an
empty version (`{"name": "contract", "version": ""}`) as "unknown". The brief's rule says a held-but-empty field is "none".
The truthiness check treats empty the same as absent.

Fix: a contract entry with no `version` key reads "unknown". One whose `version` is held but empty (`""` or null) reads
"none". Any other value reads as itself. This matches `scalar()` for role and route.

Tests: add an empty-version row next to the existing no-version row in the same summary test in `tests/test_work_brief.py`.
Add no new method. Run `tests.test_work_brief` once through `tools/agent/test-run.sh` on `/usr/bin/python3`, outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short `TMUX_TMPDIR`
under /tmp, in the foreground.

Touch only lib/launch_record.py and tests/test_work_brief.py. Do not touch the default tmux server. Do not edit config/.
Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
