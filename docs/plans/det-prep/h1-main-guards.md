# H1: guards for the Main mistakes the campaign prompts warn about

Lane `main-guards`, step 1. Level: `mid` coder. Touches: `bin/tp-agent` (the `message` branch only), `lib/freeze.py`
(new), `claude/plugins/tmux-pi/hooks/` (a new PreToolUse guard and its registration), `tests/test_tp_agent_message.py`
(new), `tests/test_freeze.py` (new), `docs/` (one paragraph each). Starts after C1 is merged (both touch bin/tp-agent).

## Why
The owner's prompts carry rules Main must remember, each from a real incident:
- "Never `tp-agent message` a worker whose permission prompt is open: the message's Enter approves it" (it happened
  twice). `tp-agent message` sends text and Enter with no check (bin/tp-agent, the `send-keys` pair in the message branch).
- "Before any push, ask whether a develop freeze is on" (a push during a freeze happened). Nothing records a freeze.

## Change
1. `tp-agent message`: refuse when the worker's pane shows a permission prompt (the same detection the status pane uses for
   `needs_input`/`permission`, read from the task record; when unknown, capture the last 15 pane lines and match the
   harness's permission dialog text) with: `worker X has a permission prompt open; your Enter would answer it. Deny it
   first: tp-agent message X --deny-prompt --text "..."`. `--deny-prompt` sends Escape, waits until the prompt is gone
   (2 s deadline, polled), then sends the text. Approving a prompt stays a human action.
2. Freeze: `tp-agent freeze on --reason TEXT` / `off` / `status`, stored in the repository's common git dir
   (`.git/tmux-pi/freeze.json`, `schema tp-freeze:v1`, who, when, reason) so every worktree and session of that repo sees
   it. A Claude Main PreToolUse hook and the worker trust guard refuse `git push` (any form, including `-C`) in that repo
   while it is on, naming `tp-agent freeze off`. `tp-agent lane list` shows `FROZEN` in its header.

## Done means
1. A fixture task in `needs_input` with a permission record refuses `message`; `--deny-prompt` sends Escape before the text (tmux stubbed, order asserted).
2. A task with no prompt is messaged exactly as before.
3. `freeze on` then `git push` through the hook is refused with the message; `freeze off` allows it; the record has the schema and reason.
4. A push from another worktree of the same repo is refused too.
5. Tests for 1, 3, 4 fail without the change; docs updated.
