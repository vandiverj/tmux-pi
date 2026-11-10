# SW1: workers attack their own rule over its whole input space (Main, 2026-11-09)

Owner-approved 2026-11-09 ("yes, run that lane in parallel"). This lane runs beside checkpoint-1 and does not block it.

## Why
Repeated review rounds on one finding family have one shape. The worker fixes the input shape the reviewer showed. The
next reviewer writes a new input that defeats the same rule. The worker never tried to break its own rule; the reviewer
does that every round.
- S1c: seven reviews, all `binding-ambiguity`. Annotation-only names, deletes, namespace packages and alias writes each
  defeated the same "is this name provably bound" rule.
- MS1 and MN1: three rounds each, with timing and concurrency cases (a delayed record, concurrent launches).

The packet already has "Fix a cause, not a site", a Self-review of writers, readers and callers, and, on a gated retry, a
sweep of code **sites** (lib/family_sweep.py). None of it asks for the **inputs** the rule must hold for. Main's manual
version of this fix is docs/plans/det-prep/fixes/ls1c-s1c-round8-attack.md; read it as the worked example.

## Build
1. **Coder role (`agent/roles/coder.md`), every round.** Add a short paragraph to the Self-review requirement. Before
   reporting, the worker:
   - names the input space each changed rule must hold for (the forms, states, orders or interleavings that reach it);
   - lists that space from an authoritative source where one exists: a grammar or `ast` node list, an enum or state
     machine, the event list, the pairs of concurrent operations and their interleavings. The list is printed by code
     when the source is code. Where no closed source exists, it says so and lists the cases it chose and why;
   - runs each case against the real code with an expected outcome and the reason, and reports counts (cases, matches,
     mismatches fixed) with the script and log under `evidence/`;
   - fixes any mismatch by the rule, never by special-casing the input; turns the cases worth keeping into rows of
     existing tests.
   Keep it to one paragraph of plain words. The packet text is read by every worker, so length counts. Mirror the same
   sentence in `docs/prompt-examples/templates/brief-common.md` if that file repeats the Self-review text.
2. **Gated retry sweep (`lib/family_sweep.py`).** Add an "Attack the rule" section after "Find the sites no review cited".
   It says the earlier rounds each fixed the shown input, and requires the step from item 1 for the rule the family names.
   It adds one row to the reported table format, or one short required block under it, for the attack counts. Keep the
   SWEEP_BUDGET guarantee: the new section is fixed text, so the size bound must still hold. Update the module docstring.
3. **A gated retry runs on a stronger model.** For `retry --route rule-first|design`, choose the model through the
   existing escalation path (`escalation_route` / `model_router.escalate_route`) unless `--model` or `--level` is given.
   A repeated family is evidence of a capability gap, so a gated retry goes up one **level** (a stronger model) rather than
   effort only. It stays inside the catalog ceiling and the existing approval rules: no effort above high without
   `--approve-effort`, and never a cloud model for local-only work. A user `--model` or `--level` still wins.
   - First read how `rr.escalate` steps today and whether a level step is reachable. If the catalog cannot express "level
     first for a gated retry" without changing `config/` files, stop and send `tp-agent request` naming the change. Do
     not edit `config/` yourself.
   - Record in `route_info` that the route was chosen because of the gate, so `tp-agent inspect` shows why.
   - Report which model a gated retry would get today for a Sonnet-5.5 coder lineage, as the router decides it (dry
     computation, no dispatch).
4. **Docs.** One sentence each in `docs/lanes.md` where retry routes are described.

## Tests (no live model, no Claude, no dispatch)
- `tests/test_family_sweep.py`: the sweep contains the attack section; it is within budget with the largest existing
  fixture.
- `tests/test_retry_packet.py` (or the test that already checks a gated retry): a gated retry routes through escalation
  and records why; an explicit `--model` skips it; a plain retry is unchanged; local-only never escalates to cloud.
- Whatever test asserts on `agent/roles/coder.md` text or packet size. Find it with `git grep -n "roles/coder"` in `tests/`.
- Fail-before for each new or changed test against the lane base. Put the script and log under `evidence/` in the task dir
  and name them by absolute path.
- Apply item 1 to your own change: name the input space of each rule you changed (for item 3: plain, gated, gated with
  --model, gated with --level, local-only, at the catalog ceiling) and show the table.

## Done means
- The new and changed tests, plus `tests.test_family_sweep`, `tests.test_retry_packet`, `tests.test_task_family`,
  `tests.test_model_router` (if it exists), `tests.test_contract` and `tests.test_session_env`. Run them with
  `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u
  TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- Run the matrix with every new file `git add`-ed (the session-env guard reads only tracked files). Rerun after your last
  edit and list each file's time.
- Self-review, sweep table if any, SLOW_TESTS. Commit nothing. `RESULT_STATUS` on its own line, as the last line of your
  final message. No background shells left running.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets `TMUX_TMPDIR`
to a fresh `mktemp -d /tmp/sw1.XXXX` and uses a private `-L` socket; never the default server, never `kill-server` without
`-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a directory; name the bin
files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit `config/`, the Claude profile or
its trust store.
