# HB3c: reviewers get the owner's rules; stale launch comment; tests never read the real work store (Main, 2026-11-12)

The owner adopted HB3b ("adopt HB3b", merged b21a31c). It added the packet section `# Rules in force`, which holds the owner's
active in-scope records, to every worker that `tp-agent` starts. The section is built by `rules_for` (bin/tp-agent:709) and
`work_brief.worker_rules` (lib/work_brief.py:180), and passed through `tp-spawn --rules-file`. This slice is the decision
note's follow-ups 1, 3 and 4 (docs/plans/det-prep/hub/hb3/hb3b-decision.md). Lane hub-rules-review, base main. Kind:
mechanical (Sonnet). Bar: realistic use.

1. **Reviewers get the section.** `bin/tp-review` starts the reviewer through `tp-spawn` (:494) and passes no `--rules-file`.
   - Build the section with the same function the workers use, never a copy of it. Scope it to the reviewer: its workdir (the
     author's), role `code-reviewer`, the author's lane, and the tasks the reviewer continues (the reviewed task and its
     ancestry).
   - Pass it as a temp file, the way `launch` does (bin/tp-agent:750-754, removed afterwards). Records are data and never go
     on a shell line.
   - Fail open as HB3b does: a broken store never stops a review.
   - Say in report.md where the shared builder lives now, if you move it. If you move it, bin/tp-agent must import it.
2. **Stale comment.** `lib/launch_record.py:271` says HB3 decision records are not applied at launch. Make the reason true
   for today: the rules reach the worker through the packet section, not through preference ids. If a test asserts the old
   text, extend it.
3. **No test reads the real store.** Find the tests that start workers or build packets without setting `TP_WORK_DIR`, so
   they read `~/.config/tmux-pi/work` read-only. Point them all at a temp dir through one shared fixture (an existing test
   helper base, if there is one) rather than per-test edits. List each module you changed in report.md. A test that means
   to read a real store must say so in its name or docstring; there should be none.
   - **Proof:** run the changed modules with `TP_WORK_DIR` unset and `HOME` set to a temp dir that holds a store whose one
     active global record would show up in a packet. Show that no packet carries it. Use the evidence-file form: a `.py` in
     your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`.

Tests: extend the existing tests. For rule 1 that means the tp-review tests nearest the spawn call (find them with
safe_grep), with rows for:
- a reviewer of a lane task gets the owner's lane rule;
- a reviewer gets a global rule;
- a worker-written record is never shown;
- an unreadable store gives the one line and the review still starts.

Each row must fail on main, shown in the evidence-file form. Run the touched modules plus `tests.test_session_env` and
`tests.test_work_brief` once each through `tools/agent/test-run.sh`:
- use `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`);
- use a fresh short `TMUX_TMPDIR` under /tmp;
- run in the foreground, one after another.

Edit bin/tp-review, bin/tp-agent (only to share the builder), lib/work_brief.py (only if the builder moves there),
lib/launch_record.py, and the test modules and helpers you need. Do not edit config/ or claude/.

Search with safe_grep and read with Read or safe_read. Never read .env*, auth files, claude/mcp.json or config/local.json.
Never write to `~/.config/tmux-pi/work`.

Do not use the network, send nothing to 127.0.0.1:1234, start no Claude or pi, and never touch the default tmux server.

Approve no prompt. A placeholder or permission prompt means stop and report.

Commit nothing. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
