# DS1 round 3 (Main, 2026-11-09)

Review review-coder-1f75f603ab-091841 is APPROVED_WITH_NOTES. Its F1 asks whether a turn event can arrive while an
approval is still live. Main checked Claude Code's hook reference (https://code.claude.com/docs/en/hooks, read
2026-11-09). Its answers:

- `PermissionDenied` "only fires in auto mode: it doesn't run when you manually deny a permission dialog". That confirms
  the DS1 premise.
- `UserPromptSubmit` fires on typed prompts, and also on "A background subagent reporting back to the session that
  started it", a scheduled task or `/loop` iteration, and "A message another session sends".
- `Stop` fires "When Claude finishes responding". Its input has `background_tasks`, to tell "session is done" apart from
  "session is paused waiting for background work".
- Hooks that fire inside a subagent carry `agent_id` and `agent_type` (common input fields). `SubagentStop` fires when a
  subagent finishes, with its `agent_id`.

So the main agent can stop while a background subagent of the same worker sits at its own permission dialog. That
dialog's `PreToolUse` ask wrote the task's `permission-request.json`. Round 2 settles every pending request on `Stop`, so
it would mark that live ask `declined`.

## Fix
1. **Record who asked.** Both writers store the asking agent's `agent_id` (absent or empty for the main agent) and the
   `tool_use_id` as `call`:
   - `redact_hook` `save_request` (mode `on`);
   - `bin/tp-sensitive-read` `save_request` (shadow and off).

   Keep every existing field.
2. **Settle only the asker's own request.**
   - `Stop` and `UserPromptSubmit` (no `agent_id` on the event, or the main agent's) settle a pending request only when its
     `agent_id` is empty or absent.
   - `SubagentStop` with `agent_id` X settles a pending request only when its `agent_id` is X.
   - A pending request written before this change, with no `agent_id` field, is treated as the main agent's.
   - Register `redact.sh` for `SubagentStop` in `claude/plugins/tmux-pi/hooks/hooks.json`. It is the same never-blocking
     script that is already on `Stop`. Give `SubagentStop` its own branch in `handle`, before any mode check, that never
     blocks.
3. **A late answer still wins.** If a `PostToolUse` or `PostToolUseFailure` for the same `call` arrives after a request
   was marked `declined`, `settle()` records the real outcome, `authorized` or `denied`. Never go the other way:
   `authorized` and `denied` are never changed.
4. **Sweep (family: turn-scoped settling).** List every action taken on `Stop`, `UserPromptSubmit` or `SessionEnd` that
   assumes "nothing is in flight". The list includes `Store.drop_pending()` in the same branch. For each one, say whether a
   background subagent's live ask breaks it.
   - Fix the ones that can be scoped by `agent_id` in the same way.
   - For any that cannot, say why and what the effect is.
   - Note that there is one `permission-request.json` per task, so two concurrent asks overwrite each other. Report this
     only; do not fix it.
5. **Tests**, each failing on 620764d where it can:
   - main `Stop` with a pending request from subagent X: unchanged;
   - `SubagentStop` X: declined;
   - `SubagentStop` Y: unchanged;
   - main `Stop` with a main pending request: declined;
   - a request with no `agent_id` field: declined on main `Stop`;
   - `PostToolUse` for the same call after declined: authorized;
   - `UserPromptSubmit` from a background report-back with subagent X's ask pending: unchanged;
   - the `hooks.json` registration of `SubagentStop`;
   - a subprocess test of `bin/tp-redact --claude-hook` with `SubagentStop`, in all three modes.

## F2 and F3 (Main)
- **F2:** Main reproduced the fail-before: with round 1's `mode() == "on"` condition restored, 6 errors (shadow and off,
  in-process and subprocess); restored, 10 OK. The output is in coder-1f75f603ab/main-evidence/main-fail-before.txt. You
  owe nothing for F2.
- **F3:** test_orchestration and test_sensitive_trust are over 60 s and unchanged by this lane. Main tracks them as a
  follow-up. Do not change them here.

## Done means
- Four cells green over the same 13 files, with each file's time.
- Fail-before against 620764d for each new test that can fail there. Name any that are guards.
- The sweep table.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.

Never read `.env*`. Never search `claude/` recursively: name the files there. No live model. Any test that calls tmux:
`TMUX_TMPDIR` set to a temp dir first, and a private `-L` socket.
