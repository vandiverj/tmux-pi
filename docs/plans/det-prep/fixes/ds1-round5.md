# DS1 round 5 (Main, 2026-11-09)

Review review-coder-51f93f6064-115631 is REQUEST_CHANGES. Read its result.md and report.md in this task's reviews. Main
checkpointed round 4 as commit 289e2ba (four cells 489 OK each).

## F1: late settlement must keep every field (family: late-answer-settling)
`lib/redact_hook.py:872–874` (in `settle`) rewrites the request from a whitelist of fields. A pending request for call `c`,
agent X, declined by `SubagentStop`, then settled by a same-call `PostToolUseFailure` in off mode, becomes `authorized` but
loses `event` and any other field (the reviewer added `extra`; it was lost too).

**Fix.**
- Start from the prior object. Change only the status and the fields the settler means to set (the time, the event that
  settled it), and keep every other field as it was, including unknown ones.
- Keep the existing atomic write and root protections (`save_request`'s checks). If `save_request` cannot take a whole
  record, add a narrow path that can; do not weaken its checks.
- Sweep the other writer of a late answer, `bin/tp-sensitive-read` (around line 86). It also rebuilds the record through its
  own save_request. Fix it the same way, or show why it is not an instance.

**Test.**
- Extend the late-answer tests (shadow, off and on; subagent and main agent): the settled record equals the prior one
  except for the fields the settler sets. Compare the whole dict, including an extra unknown field.
- The same for the tp-sensitive-read path if you change it.
- Each new or extended test fails on 289e2ba. Keep the script and log in this task dir (failbefore-r5.sh, failbefore-r5.log).

**Sweep (family: late-answer-settling).** Every writer of permission-request.json (`save_request` callers in lib/ and the
named bin files): keeps unknown fields, or rebuilds the record (and whether that is intended, for example a new request).

## F2: report accuracy (check)
Refresh report.md: fix the misnamed Store caller (lines 126 and 132 per the review), and remove the design-doc update from
the outstanding list (it is done).

## Done means
1. The F1 tests fail on 289e2ba and pass now.
2. The sweep table.
3. The same 13 modules pass in both interpreters, both modes (list the command and each file's time); Main will rerun the
   four cells.
4. report.md refreshed.
5. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search `bin/`
as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively. No live model. Any test that
calls tmux: set `TMUX_TMPDIR` to a temp dir and use a private `-L` socket.
