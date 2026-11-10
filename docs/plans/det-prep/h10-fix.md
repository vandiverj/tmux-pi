# H10 fix: auto mode stops denying Claude's rewritten Bash calls

Lane `h10-fix`, step `h1`, base `lane/integrate`. Author: Pi worker. Reviewer: Claude (cross-provider).

## Problem (proved live 2026-11-08, run-log "H10 repros")
With redaction on, `lib/redact_hook.py` `wrap()` appends `SUFFIX = " || echo exit-code: nonzero"` to Bash commands through
PreToolUse `updatedInput`. The suffix must stay: a failing command fires PostToolUseFailure, which cannot replace output
(docs/sensitive-read-redaction-design.md rows 8-9), so dropping it would leak secrets in failing output.
In Claude Code auto mode the rewrite causes denials: "The server-side auto mode classifier gave no verdict for Bash: a hook
changed this call's input after auto mode reviewed it, so the review does not cover what would run."
- Allow-listed calls (`pwd`, `git status`, `ls`) are denied because `X || echo exit-code: nonzero` no longer fully matches the rule.
- Adding allow rule `Bash(echo exit-code: nonzero)` fixed all 7 allow-listed calls tried (repro F).
- Calls that still need the classifier (e.g. `ls /nonexistent-dir`, outside the project) stay denied (F, twice).
- When the model sends the already-wrapped command itself, `wrap()` returns it unchanged and the hook emits no
  `updatedInput` (redact_hook.py ~line 340), so the classifier reviews exactly what runs: it ran, output redacted (repro G).

## Change (do not touch lib/: lane py39-union owns it)
1. `claude/settings.json`: add `"Bash(echo exit-code: nonzero)"` to `permissions.allow`.
2. `bin/tp-claude`: every Claude session it starts (Main and workers) gets one standing instruction in its system prompt, e.g.
   "Append ` || echo exit-code: nonzero` to every Bash command you run (not to heredocs, background commands, or commands
   ending in an operator). tmux-pi's redaction hook adds it otherwise, and auto mode refuses a command a hook changed."
   Keep it word-for-word consistent with what `wrap()` accepts (read wrap(); do not change it).
   Callers already pass `--append-system-prompt` (bin/tp-spawn ~667, bin/tp-main, bin/tp uses `--append-system-prompt-file`).
   Find out from `claude --help` (no network, no install) whether repeating or mixing these flags is accepted; if not, merge
   the line into the caller's value inside tp-claude. Do not break callers that pass none. Choose and state the rule for
   redaction mode (`tp-redact --mode`: off/shadow/on): add the line when the mode is on (recommended) or always; prove it.
3. Tests (new file `tests/test_tp_claude_suffix.py`, plus `tests/test_claude_launcher_settings.py` /
   `tests/test_settings_template.py` only where they assert the allow list):
   - the allow rule is present in claude/settings.json;
   - the exact suffix string in settings and in the instruction equals `redact_hook.SUFFIX` (import, do not copy);
   - tp-claude's final argv carries the instruction once, in each caller shape (no prompt flag, `--append-system-prompt`,
     `--append-system-prompt-file`), using a stub `claude` on PATH that prints its argv; no real Claude is started;
   - a command written per the instruction passes through `wrap()` unchanged (so no updatedInput).
   First line of any new test module: `import session_env`. Show fail-before (test written first, failing) then pass.
4. Design doc: one short paragraph in docs/sensitive-read-redaction-design.md near rows 8-9 recording the auto-mode
   interaction and this fix.

## Proof required in report.md
- before.log / after.log for the new and touched test modules, each run in both modes:
  outside: `env -u TMUX -u TMUX_PANE -u TP_TASK_ID -u TP_TASK_DIR -u TP_MAIN_ID -u TP_MAIN_CWD -u TP_TRIAL_ID -u CLAUDE_CODE_SESSION_ID -u TP_MODE -u TP_SESSION python3 -m unittest MODULE`;
  inside: the same keys set to dummy values.
- Also run test_claude_redact_hook, test_redact, test_docs_wiring in both modes; compare with
  docs/plans/det-prep/baseline-failing.txt (main checkout); no new failures.
- Do not start Claude, Pi or any live model from the worker pane. Main re-runs the live repro (Sonnet, auto mode) after merge.
- Do not commit. Never read .env* files.
