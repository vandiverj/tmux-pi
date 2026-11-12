# HB3d: a shadow trial gets the rules its source coder got (lane hub-trial-rules)

HB3b (adopted, `hb3b-decision.md`) gives every worker that `tp-agent` starts the packet section `# Rules in force`
(lib/work_brief.py `PACKET_HEADING`, built by `rules_for_task`, passed as `tp-spawn --rules-file`). Follow-up 2 in that note:
shadow trials and other direct `tp-spawn` callers get no section. A shadow (`bin/tp-trial`, `lib/trial_shadow.py`
`spawn_args`) runs Qwen on the brief a normal coder got, so the two can be compared. Today the coder's packet holds the owner's
rules and the shadow's does not, so the comparison is unfair. Kind: mechanical (Sonnet). Bar: realistic use.

## Rule first (write it in report.md before code, then code to it)
A shadow gets the same `# Rules in force` text its source coder got, byte for byte. It is read from the source task's
`packet.md`, not rebuilt from the store (the store may have changed since). Then:
- the source packet has no section (a task started before HB3b, or no rule applied): the shadow gets none;
- the source packet cannot be read: the shadow still starts, without a section, and the run's record or log says why in one
  line (fail open, as HB3b does).

The text goes to `tp-spawn --rules-file` as a temp file removed afterwards, the way `bin/tp-agent` `launch` does. Records are
data: never on a shell line.

State exactly how the section's end is found in packet.md: what tp-spawn appends after it, if anything (bin/tp-spawn ~:544),
and why a record's own text (a line starting `#`, `RESULT_STATUS:`) cannot end it early or extend it. If the boundary cannot
be found reliably from packet.md, say so and stop with a proposal instead of guessing. One proposal would be tp-spawn saving
the rules file in the task dir.

## Inventory (report.md, a short table)
List every other direct `tp-spawn` caller: tools, harnesses, smoke flows, manual starts. Find them with safe_grep, and do not
search bin/ or config/ as directories. For each, say whether it starts workers on the owner's real work and whether it should
get the section. Change nothing outside the shadow; the table is for Main's decision.

## Attack table (one table in report.md)
| Source packet | Expected |
|---|---|
| has a section | the same text |
| no section | none |
| unreadable, or missing | none, plus a one-line reason |
| a section whose record text holds `#` headings, `RESULT_STATUS:`, or shell metacharacters | the same text, never cut and never run |
| a section followed by other packet text | only the section |

For each row, name the test row that proves it. Extend the nearest existing shadow test that checks `spawn_args`, or the
packet golden (`tests/fixtures/trial_shadow/`), with one table-driven row set. Add no new module and no ritual tests. Show
the fail-before on main in the evidence-file form: a `.py` in your evidence/ dir, run as
`cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`.

## Verification
Run the touched test modules, `tests.test_work_brief` and `tests.test_session_env` once each through
`tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground, one after another.

Touch only lib/trial_shadow.py (and lib/work_brief.py if a reader for the section belongs beside the builder), plus their
tests and fixtures. Do not edit bin/, config/ or claude/. Never write to `~/.config/tmux-pi/work`.

Do not use the network. Send nothing to 127.0.0.1:1234. Start no Claude, pi or shadow run. Never touch the default tmux
server. Approve no prompt. Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
