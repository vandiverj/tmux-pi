# DS1 (DECLINE-SETTLES): a declined permission dialog leaves `pending`

Lane `decline-settles`, step DS1. Level: `mid` coder. Base: `main`.

Touches:
- `lib/redact_hook.py`;
- `bin/tp-sensitive-read` and `lib/sensitive_read.py`, only if needed;
- the hook wiring that delivers Stop and UserPromptSubmit to these hooks, only if a mode lacks it;
- tests.

Not touched: `config/`, `agent/`, `tools/smoke/` (Main briefs the G3 script change separately), the Claude profile.

## Why (Main's G3 live run 3, 2026-11-09)
- A haiku coder ran the G3 trigger command. The ask was raised: `permission-request.json` `status: pending`, and inspect
  reported `needs_input`.
- The G3 script sent Escape to the pane. The pane showed "Interrupted · What should Claude do instead?", so the dialog was
  declined.
- `permission-request.json` then stayed `pending` for the full 240 s.

Claude Code fires no `PermissionDenied` event when the owner rejects the dialog. The `PermissionDenied` path in
`redact_hook.settle` and `bin/tp-sensitive-read` covers only other denials. The code already knows this:
- `lib/redaction_store.py` `drop_pending` says: "this covers a rejected ask that fired no PermissionDenied";
- `redact_hook.handle` calls it on `Stop` and `UserPromptSubmit`;
- but nothing settles the task's `permission-request.json`.

So after every real decline the task shows `needs_input` with a stale pending request:
- after Main's normal Escape-and-redirect;
- for good, until the next tool call of the same target.

That is wrong for Main, for the list, for waits, and for G3.

## Change
1. **Settle on a new turn.** In every redaction mode (`on`, `shadow`, `off`), when a `UserPromptSubmit` or `Stop` event
   arrives for a task whose `permission-request.json` is `pending`, rewrite it with:
   - `status: declined`, a new status; do not reuse `denied`, which means a PermissionDenied event;
   - the event name and a timestamp;
   - every other field unchanged.

   Reason: no tool call can be awaiting approval at a new prompt or at the end of a turn.
   - Same atomic write and root checks as `save_request`.
   - A file that is not pending, or cannot be read, is left alone. An unreadable one keeps its current handling.
   - An `authorized` or `denied` request is never changed.
2. **Who owns it.** In mode `on`, `redact_hook` owns permission records; in `shadow` and `off`, `bin/tp-sensitive-read`
   does.
   - Make sure exactly one of them settles in each mode.
   - If one of them does not receive `Stop` or `UserPromptSubmit` today, add only that event to its registration, in the
     narrowest place. Show where the registration lives.
3. **Readers.** Check every reader of `permission-request.json` status: `lib/tp_detail.py`, `lib/request_file.py`,
   `lib/index_text.py`, `bin/tp-agent` around lines 1611 and 1678, and any other the grep finds.
   - `declined` must read as "not pending" everywhere. It must never read as authorized.
   - Give a sweep table of every reader and what it does with `declined`.
4. **Tests.** Use the real entry points (`redact_hook.handle`, and `bin/tp-sensitive-read` `main()` with the arguments
   its hook script passes) in each of the three modes:
   - PreToolUse ask, then pending, then UserPromptSubmit, then `declined`;
   - the same with Stop;
   - an authorized request (PostToolUse) is not changed by a later Stop;
   - a PermissionDenied event still gives `denied`;
   - inspect shows no `needs_input` from a declined request.

   Each test must fail before the change.

## Rules
- Never read `.env*`.
- No live model, no G3 live mode.
- Any test that calls tmux: `TMUX_TMPDIR` set to a temp dir first, and a private `-L` socket.

## Done means
- Four cells green: `/usr/bin/python3` and `/opt/homebrew/bin/python3`, each with the session env set and unset. The cells
  cover the changed modules' tests plus test_redact_hook*, test_sensitive_read* and test_tp_detail*, whichever exist.
  Each file is under 60 s.
- Fail-before shown for each new test.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.
