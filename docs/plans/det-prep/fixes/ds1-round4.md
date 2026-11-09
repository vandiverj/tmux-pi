# DS1 round 4 (Main, 2026-11-09)

Review review-coder-603eb62dce-102257 is REQUEST_CHANGES. Read its result.md and report.md in this task's reviews. Main
checkpointed round 3 as commit 251656b.

For each finding: fix it, then sweep for every other site of the same family. Give one sweep table per family, covering
every site: fixed, already right, or not an instance, each with evidence.

## F1: a late PostToolUseFailure must win in shadow and off
Round 3 item 3 requires a same-call `PostToolUseFailure` after `declined` to record the real outcome. In shadow and off it
does not:
- `redact_hook.handle` does not settle in those modes;
- `bin/tp-sensitive-read` ignores the event;
- `sensitive-read.sh` is not registered for it.

`redact.sh` is already registered on `PostToolUseFailure` (hooks.json line 9).

**Fix.**
- In `redact_hook.handle`, in shadow and off, a `PostToolUseFailure` whose `tool_use_id` equals the request's `call`
  settles a `pending` or `declined` request the way mode `on` does for the same event. Use the same outcome and keep every
  field.
- Match on `call` only. A request with no `call` is not touched here.
- The branch never blocks, never prints a decision, and never raises.
- Do not register `sensitive-read.sh` on new events (it exits 2 on failure).
- Say in the `settle` docstring which hook owns each late answer in each mode.

**Test.**
- Drive the real entry points in shadow and off: ask as subagent X, `SubagentStop` X, then `PostToolUseFailure` for the same
  call. Remove the skip in tests/test_g3_e2e.py:367–368 so all three modes run.
- Also cover the main agent: ask, `Stop`, then `PostToolUseFailure`.
- Add a different-call guard: it stays `declined`.
- Add a subprocess case through `bin/tp-redact --claude-hook` in shadow.

**Sweep (family: late-answer settling).** List every event × mode pair that can carry a late answer: `PostToolUse`,
`PostToolUseFailure` and `PermissionDenied`, each in on, shadow and off. For each, name the hook that settles a `declined`
request with the same call, or show that none is needed.

## F2: bounded history must not hand a live grant to the main agent
`Store.record_use` trims `calls` to the last 200 entries, but `agents` and `runs` keep X's entry. After 200 intervening
main-agent calls, `drop_pending("")` no longer sees X's names: `other_live` iterates the trimmed `calls`. It treats X's
grant as main-owned and deletes it. Approving X's command then exits 126 with "no approval is recorded". The reviewer
reproduced it.

**Fix.**
- Keep the owner and names of every outstanding call (one with a recorded run, or with a pending grant) independent of
  the bounded history. One way: never trim a call that is still in `runs`. Another: store the names and agent alongside
  the run.
- Choose one, and say why it stays bounded. A store must not grow without limit; state the bound.
- When ownership of a pending grant cannot be determined, the main agent's Stop must not delete it. Unknown ownership
  keeps the grant. Say what then clears it (`SessionEnd` `sweep`, or the next turn of its owner).

**Test.**
- The reviewer's case: X has a pending Bash ask with a recorded command. The main agent makes 200+ placeholder calls,
  then the main agent's Stop runs. X's grant survives, and approving X's command runs it (not 126).
- A trim test that shows the store stays bounded.
- Both must fail on 251656b where they can.

**Sweep (family: turn-scoped-settling, continued).** Name every map in the store that is trimmed (`calls`, `agents`,
`runs`, any other). For each, say whether trimming it can change who owns a live grant or command, and show the result.

## F3: durable fail-before
For every new test in rounds 3 and 4, keep the command and output of its fail-before run in this task dir: a script plus
its log, run against 620764d for round 3's tests and 251656b for round 4's. Name any test that is a guard (passes there),
with the reason.

## Done means
- Four cells green over the same 13 files, with each file's time.
- The fail-before scripts and logs (F3).
- Both sweep tables.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob: it raises the
sensitive-read prompt. Never search `bin/` as a directory (it holds `tp-secret-exec`); name the bin files. Never search
`claude/`, `agent/` or `config/` recursively. No live model. Any test that calls tmux: set `TMUX_TMPDIR` to a temp dir first,
and use a private `-L` socket.
