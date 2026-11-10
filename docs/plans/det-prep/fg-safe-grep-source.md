# F-GREP: safe_grep leaks the content of a protected file

## Defect (found by HL1 phase 0, 2026-11-08)

`safe_grep` over a `.env` file returns the file's lines unredacted, in both the Pi tool (`agent/extensions/safe.ts`)
and, unless shown otherwise, the Claude MCP server (`claude/mcp/safe.mjs`). HL1 saw a decoy token come back in both arms
and both modes. Its note: the absolute-path prefix of each grep line is itself partly redacted as "unverified", so the
rule that recognises the line's *source* as `.env` never matches, and the line passes through as ordinary text.
Source: `tmux-pi-wt-hl1-eval/docs/plans/hl1-eval/results.md:70-72`, harness `tmux-pi-wt-hl1-eval/eval/hl1/guard/`
(results-v3-safe-grep.json there).

## Rule

Which protection applies to an output line is decided from the **resolved real path of the file the line came from**,
as known to the tool when it produced the line. It is never decided by parsing the display text afterwards. A line
whose source cannot be established is treated as coming from a protected file (fail closed).

## What to build

1. Reproduce first, with a temp project holding a decoy `.env` (a fake token you write in the test; never read a real
   `.env*` and never print a real secret). Record the leaking output shape for every tool listed in step 3, before the fix.
2. Fix at the source: the grep path carries the matched file's real path to the redaction decision. Find every other
   read/search tool that decides protection from display text (safe_read, safe_grep, any bundle/map/symbol view in
   these files, the Claude MCP equivalents) and give each a sweep row: file:line, decides from real path / display
   text, fixed / not applicable.
3. A test table that runs every read/search tool in both runtimes (Pi extension via `tests/safe_runtime.mjs`, Claude MCP
   via the existing `tests/test_claude_safe_mcp.py` harness) over `.env`, `.env.local`, `config/local.json` and one
   ordinary file. Path forms: absolute, relative, `./`-prefixed, through a symlinked directory, and a symlink named
   ordinarily that points at `.env`. Expected: the decoy never appears for a protected source; the ordinary file is
   unchanged. It must fail on lane/integrate before the fix. Put it in a new module, `tests/test_safe_source_redaction.py`.
4. Do not weaken any existing rule or test. If a test asserts current leaking behaviour, report it rather than change it.

## Done means

- The table passes. The before-fix run shows the leak, recorded in the task dir.
- The touched test modules (`test_safe_source_redaction`, `test_pi_safe_tools`, `test_claude_safe_mcp`,
  `test_sensitive_read`, plus any other module covering a file you change) run per module in both modes (outside a tmux
  session and with dummy `TMUX`/`TP_*`), with the table of command, count, seconds, outcome in your final reply. Every
  failure is matched to a baseline line in `docs/plans/det-prep/baseline-failing.txt` or explained.
- The sweep table from step 2.
