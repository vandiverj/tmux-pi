# HB6d (design): record Main's turns and lane lease changes, so the timeline can say where waiting:main time went

Owner, 2026-11-12: "HB6 follow-ups approved". This is the owner's OK for a hook that runs in Main's own Claude session.

Source: hb6b1-decision.md. `lease.*` and Main's own turns were left out of HB6b1, and they are what explains `waiting:main`
on real runs. In the HB6a smoke, 28% of the critical path was Main's time between tasks. In the last live measure, 16% of
the time was waiting on Main. tools/waits/timeline.py paints that time as `waiting:main`, mostly `inferred` ("turn ended;
next prompt from Main", line ~514) or `derived` ("reply waits for Main", line ~520). What the owner wants to see is whether
Main was busy (working on something else), idle waiting on the owner, or simply had not looked yet.

## What to record
1. **Main's turns.** A new hook script, claude/plugins/tmux-pi/hooks/main-turns.sh, registered in hooks.json for
   UserPromptSubmit and Stop, plus SessionStart if the rule needs it.
   - It acts only in Main: no TP_TASK_DIR, and a tmux-pi session directory found the way tools/agent/_common.sh
     tp_session_dir finds it. Anywhere else it exits 0 silently.
   - It appends one JSON line per event to SESSION/main-turns.jsonl: time, event, and the Claude session id.
   - Never prompt text, reply text, tool input or file names. The owner's words stay out of the record.
   - Cost under 30 ms per event, measured. It never fails or blocks the hook: exit 0 on every error.
   - Say how concurrent appends stay whole lines.
   - report.sh is out of scope (lane hub-dup-stop is changing it). Do not touch it.
2. **Lease changes.** lib/lane_lease.py appends `lease.taken` / `lease.activated` / `lease.released` events, with time, task,
   role and holder kind, to `<common git dir>/tmux-pi/lanes/<name>/events.jsonl`, under the lane lock it already holds. Today
   only `taken` survives, in lease.json; the release time is lost.
3. **The timeline reads both.**
   - A `waiting:main` span is split by Main's turns into Main busy (a Main turn was running), Main idle (between Main turns),
     and unrecorded (no main-turns.jsonl covers it).
   - Lease events give exact `waiting:lane-lease` spans.
   - Keep the HB6 rules: unknown stays unknown, elapsed is never mixed with accumulated, and each span carries its source and
     confidence.
   - Pick the state names and say how tools/waits/status.py and existing readers see them. Old sessions with no new records
     must read exactly as today.

## Step 1 (before the rule)
Read Claude Code's hook input for UserPromptSubmit and Stop as report.sh already uses it. Read how Main's session gets its
tmux-pi session directory today: TP_STATE_DIR, or the tmux `@tp_state_dir` option. Report whether a Main started by tp-main
can always find it, and what happens in a Claude session outside tmux-pi. No live Claude session. Read only.

## Tests
Extend tests/test_wait_timeline.py and tests/test_lane_lease.py. Add the hook's rows to the existing hook test that suits it
(say which), with no new test file unless none fits.

Fail-before: a `.py` in evidence/, run with `python3 -I` against main extracted to /tmp. It names the failing assertions.

Attack table in report.md, each row marked realistic or contrived:
- Main events: prompt or Stop missing, out of order, duplicated, across compaction or restart, two Main sessions in one
  tmux-pi session;
- lease events: taken without released, stale holder removed;
- the hook outside tmux-pi;
- the record file unwritable, or a symlink;
- timeline spans crossing a Main turn boundary.

Measure the hook cost (median of 20 calls) and put the number in report.md.

Run once on `/usr/bin/python3` through `tools/agent/test-run.sh`: tests.test_wait_timeline, tests.test_lane_lease,
tests.test_claude_plugin_module, the hook test you extend, and tests.test_session_env. Run outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short TMUX_TMPDIR under
/tmp, in the foreground. Tests use a private `-L` socket. Never touch the default tmux server.

Touch only claude/plugins/tmux-pi/hooks/main-turns.sh (new), claude/plugins/tmux-pi/hooks/hooks.json, lib/lane_lease.py,
tools/waits/timeline.py, tools/waits/status.py, tests/test_wait_timeline.py, tests/test_lane_lease.py,
tests/test_claude_plugin_module.py and one existing hook test. Ask Main first for anything else. If a guard refuses an edit,
ask Main; do not route around it. Do not edit config/ or claude/main-prompt.md. Do not commit. No git notes. No background
shells. `RESULT_STATUS: complete` as the last line.
