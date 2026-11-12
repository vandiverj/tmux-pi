# PF1c round 2 (lane perf-review, on 1a0ff80)

Review review-coder-b649a51800-121748: REQUEST_CHANGES F1 (read its result.md and report.md first; the reproduction is in its
`test-runs/4.json` and `evidence/`). Main's diagnosis: F1 is a real input the brief did not name. Rule 1 holds the packet text
identical to main's under every repository configuration, but the brief listed attribute forms and `diff.external`, not the diff
options that change hunk *shape*. The brief stands. F2 was Main's evidence (see below); nothing for the worker.

1. **F1: the splitter does not depend on line shape that git configuration can change.** `split_sections`
   (`lib/review_rounds.py:399`) treats an empty line inside a hunk body as one context line (one old and one new line), the way
   `diff.suppressBlankEmpty=true` writes blank context. It still counts only by the hunk header (rule 3: structure, never content).
   Check every other `diff.*` / `core.*` setting that changes what one hunk or header looks like when git is given two trees, and
   for each one say in report.md either "same text as main's per-file form (row N)" or "cannot occur because the call passes X".
   At least: `diff.suppressBlankEmpty`, `diff.context`, `diff.interHunkContext`, `diff.noprefix`, `diff.mnemonicPrefix`,
   `diff.algorithm` (patience, histogram, minimal), `diff.indentHeuristic=false`, `diff.orderFile`, `diff.relative`,
   `diff.wsErrorHighlight`, `color.diff=always`, `core.quotePath=false`, `core.autocrlf` / `core.eol`, and a `.gitattributes`
   whitespace or `eol` entry. Find the rest from `git help config` (read-only; no download).
2. **Tests:** extend the existing differential table (rows that compare the new code to main's per-file form computed in the
   test) with the reviewer's case (5 rewritten files, each ending in a blank line, `diff.suppressBlankEmpty=true`) and one row
   per setting from rule 1 that reaches git. Same method, more rows; no new methods. This row must fail on 1a0ff80: show it in
   the evidence-file form (`.py` in your evidence/ dir, run as `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`).
3. **Report:** update the attack table (now with a config axis) and the process-count measures if the code path changed.

Already decided (do not raise again): timers reuse only the round-1 plan; later rounds keep today's timers path. The per-file
fallback on an attribute mismatch stays. The PF1a harness and its 200-file and 3-file numbers stand unless this change moves
them.

F2 (Main's, for the record): the four cells are separate processes. Two runs started together each used their own CPU (2.56 s
user, different instruction counts) yet printed the same "Ran ... in" time. Staggered starts give different end times. Round
2's cell evidence will record per-process CPU.

Tests: run `tests.test_review_rounds`, `tests.test_tp_review` and `tests.test_session_env` once through
`tools/agent/test-run.sh`, as in the brief (`/usr/bin/python3` outside tmux, fresh short `TMUX_TMPDIR`,
`perl -e 'alarm 900; exec @ARGV'`, foreground, one after another). Do not touch bin/tp-spawn, bin/tp-agent, lib/work_brief.py
(lane hub-packet). Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last
line.
