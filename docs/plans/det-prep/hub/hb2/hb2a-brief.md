# HB2a: one worker contract, delivered on every launch route, with one read/search rule (Main, 2026-11-11)

Owner workstream: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/det-prep/hub/owner-request.md` (Main needs
reminders about safe_grep/safe_read; workers "do not use safe_grep/read from the beginning"). Plan: `.../hub/README.md` (HB2).
Shared shape: `.../hub/interfaces.md` section 1 (implement exactly that; if it is wrong, say so in the report). Audit with
file:line: `.../hub/audit/A-instructions.md` (sections 1-4). Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.
Target: Claude Main's workers first (Claude and pi worker harnesses both get the contract; Main's own prompt is HB3's). Route
rule-first. Kind of round: design (Opus). Step a of two: the launch record `launch.json` (interfaces.md section 2), Main's launch
record and Agent-tool children are step b; do not build them now.

## Why
SG1 (merged) added a `# Searching and Reading Files` block in `bin/tp-spawn:745-753`, but the audit's other gaps remain: the
contract text is spread over the packet's Return block, coder.md, tp-review's `outside_rule`, the manifest-only STANDING lines
(`lib/agent_tools.py:41-46`, absent in projects without a manifest) and two tool descriptions that disagree by harness
(`agent/extensions/safe.ts:65-68,97-100` vs `claude/mcp/safe.mjs:52-55,79-81`); reviewers never receive
`agent/roles/code-reviewer.md` (`bin/tp-review:482`, `bin/tp-spawn:546`); only coder has a scope rule; bench workers get lines
production workers do not.

## The rules
1. **One contract file.** `agent/contract/worker.md`, first line `contract: worker v1`, ≤ 20 lines, per interfaces.md section 1:
   stay in the brief's scope; the one read/search rule (SG1's wording is the decided behaviour; keep it); at a permission prompt
   stop and report, never work around a denial; report to `report.md`, final reply ends with `RESULT_STATUS:`; reach Main with
   `tp-agent request`; the always-useful STANDING lines from `lib/agent_tools.py:41-46` that do not need a manifest. Tool names are
   placeholders filled from one per-harness table (move the table at `bin/tp-spawn:749-751` to wherever both tp-spawn and tests can
   read it); role files keep only role method.
2. **Delivered on every route, manifest or not.** tp-spawn puts the rendered contract in the system prompt for every launch it
   makes: spawn, retry, review, lane step, contract task, investigate/trial, bench, legacy subagents spawn. Remove what it
   replaces (the SG1 block becomes the contract's rule; duplicate Return/STANDING sentences go) so a worker sees each rule once.
   The manifest stays optional and is rendered as its own block for project tools only.
3. **Reviewers get their role.** Reviews launch with `agent/roles/code-reviewer.md` delivered (or the role text tp-review needs,
   if a part of it conflicts with `review_task`; list any conflict and which wins). `bin/tp-review:244` `outside_rule` becomes the
   contract's rule, not a second wording.
4. **Tool descriptions agree.** `safe.ts` and `safe.mjs` descriptions say the same thing in the same words (harness names aside):
   use safe_grep/safe_read for searches and reads; what each refuses or redacts. Prompt examples
   `docs/prompt-examples/04-tmux-pi-operations.md:102-103` and `03-security-and-quality.md:87,92` match the rule.
5. **A delivery table test.** Render the effective package (system prompt + packet) for every route × harness {claude, pi} with
   and without a manifest, and check: contract present once with its version, the harness's tool names, no `grep -rn`/`rg`
   advice anywhere, role text present where the route has a role (reviewers included), Return block once. Fail-before on the base.

Ownership and neighbours: `bin/tp-spawn` was SG1's and lane local-provider (LP1, near merge) edits its pi `--model` line near 742;
keep your edits off that line and its `launch_model` python block. `bin/tp-review`, `agent/roles/*.md`, `agent/contract/`,
`agent/extensions/safe.ts`, `claude/mcp/safe.mjs`, `lib/agent_tools.py`, `lib/preamble.py`, `docs/agent-tools.md`, the two
prompt-examples, tests you add. Ask with `tp-agent request` before touching anything else (`bin/tp-agent`, hooks, `report.sh`,
`register.tsx` belong to other lanes). Do not change any decision in `lib/sensitive_read.py`, `lib/redact_hook.py`,
`bin/tp-sensitive-read` or `agent/extensions/sensitive.ts`.

## Attack (realistic-use bar; build the rows by code, print the table)
Routes × harness × {manifest, no manifest} × {role given, no role} as above; plus: a project with its own
`.tmux-pi/worker-preamble.md` (contract and preamble both present, order stated), a retry of a retry, a review of a lane step,
a bench copy (now the same contract as production). Expected per row from these rules. Realistic first: the packet a coder on
battle-soccer and a reviewer of a tmux-pi lane would get, shown in the report (rendered, not live).

## Report
Fail-before against the lane base for each new test. Render packages with tp-spawn's dry/print path or a fake harness that logs
its argv (no live Claude or pi start); fresh `TMUX_TMPDIR` plus a private `-L` socket if tmux is needed; never the default server.
Run the touched and importing test modules once with `/usr/bin/python3` outside tmux
(`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`), plus `tests.test_session_env` and any TS test for the
two tool files, the log starting with `date -u`, `TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module
times. Main runs the four cells and the full discovery on the preview; do not run them. Report: delivery table and counts, the
contract text, every sentence removed and where its rule now lives, role conflicts, what step b needs, deviations, known limits.
Search with safe_grep and read with Read/safe_read; never search `bin/` as a directory or `config/`, `agent/`, `claude/`
recursively, name the files; never read `.env*`, auth files or `claude/mcp.json`. Nothing sent to 127.0.0.1:1234 or any network
address; no Claude or pi start. `git add` new files. Commit nothing. No background shells. `RESULT_STATUS: complete` as the last
line, exactly.
