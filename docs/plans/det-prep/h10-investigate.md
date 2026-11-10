# H10 investigation: why Claude workers get the auto-mode "hook changed this call" denial

Role: researcher (read-only). Level: mid. Output: a report, no code changes. The owner chose to investigate
before any fix (2026-11-08).

## Facts so far
- `lib/redact_hook.py` PreToolUse returns `updatedInput` with ` || echo exit-code: nonzero` added (`wrap()`,
  lines 168-192; emitted at about 614-630). It sends no `permissionDecision` when there is no prompt.
- The suffix must stay, or something must do its job. A failing Bash command fires PostToolUseFailure, which still
  cannot replace output (current hooks docs: only `additionalContext`). Without the suffix, failing output that
  holds a secret reaches the model unredacted (`docs/sensitive-read-redaction-design.md` rows 8-9).
- Current hooks docs (https://code.claude.com/docs/en/hooks.md, "PreToolUse updatedInput in auto mode"): without
  `permissionDecision`, auto mode "can deny the changed call". A `command` hook in **settings or a plugin hooks file**
  can change Bash calls without that denial (interactive terminal and VS Code on a direct Anthropic API connection).
  The denial can still appear when an earlier call in the same response was changed, or when a Bash command asks to run
  outside the sandbox. If Claude repeats the changed input exactly, the hook's `updatedInput` is dropped.
- Workers start through `bin/tp-claude`: `exec claude --settings "$settings" --plugin-dir "$CLAUDE_DIR/plugins" ...`
  (lines 51-55). The redact hook is registered in `claude/plugins/tmux-pi/hooks/hooks.json` (PreToolUse `*`, timeout 60).
- Observed 2026-11-08: a Sonnet 5.5 worker (task coder-62fe70c08d) was asked twice. The first time was a long `&&`/`;`
  chain. The second was a plain `mkdir -p <task dir>/base`. The prompt said "4 consecutive actions were blocked" and
  "A hook or plugin changed (or may have changed) a call in this response after auto mode reviewed it". Earlier Opus
  workers ran many Bash calls without a prompt. Main (this Claude session, not started via tp-claude) never sees it.

## Questions to answer, with evidence (file:line or doc quote for each)
1. How does a worker session differ from a standard session on each condition the docs name? Cover: where the hook
   comes from (`--plugin-dir` vs an installed plugin vs `--settings`), interactive vs not, API connection type, sandbox
   settings, permission mode, and whether TP_REDACTION is on for workers but not for Main. Read `bin/tp-claude`, the
   settings file it builds, `claude/plugins/tmux-pi/` and the redaction mode resolution (`lib/redact_hook.py` ~line 60).
2. Which commands does `wrap()` change, and which does it leave alone? Classify both blocked commands above.
3. Ranked hypotheses for the denial. For each one, give a cheap repro that Main can run in a throwaway tmux pane.
   Each repro needs: the exact launch command, a two-call prompt, and the expected result if the hypothesis holds.
   Main runs the repros. You do not start Claude sessions yourself.
4. Candidate fixes that keep failing-output redaction. For each: what it changes, why the docs say auto mode would
   accept it, the security effect, and the tests that would prove it. Include "load the hook from settings instead of
   --plugin-dir" and "pair updatedInput with an explicit decision", each only if the evidence supports it.

## Rules
Read-only. Never read `.env*` or print secrets. Search agent/, config/ and claude/ with safe_grep or by exact path
(a bash grep over those directories triggers a sensitive-read prompt that Main will deny). Web reads of the Claude
Code docs are allowed; do not download or install anything. End with `RESULT_STATUS: complete`.
