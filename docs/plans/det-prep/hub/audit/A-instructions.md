# A. What instructions each tmux-pi agent actually receives (audit of main 6c8094f, 2026-11-10)

Read-only source audit. Paths are relative to /Users/jerrickvandiver/Documents/Projects/tmux-pi. Not read (hard rules):
`claude/mcp.json`, `agent/mcp.json`, `config/local.json`, auth/env files. So "Claude workers have the tmux-pi-safe MCP tools"
rests on `claude/settings.json:14` (pre-allowed `mcp__tmux-pi-safe__safe_read/safe_grep`), README.md:158 and the SG1 brief's
transcript count (docs/plans/det-prep/fixes/sg1-safe-search.md:14-15). I did not see mcp.json itself.

## 0. Key facts that shape every route

- tmux-pi's own repo has **no** `.tmux-pi/worker-preamble.md` and **no** `.tmux-pi/agent-tools.json` (`ls .tmux-pi/`).
  So `lib/preamble.py:92-96` returns "" and `bin/tp-spawn:742-760` adds **no `--append-system-prompt`** for workers here
  (unless config/router.json `local.prompts` has an entry for the model; not seen in practice).
- Evidence: 0 `system-prompt.md` files across every task of every session under `.tmux-pi/sessions/*/tasks/` (tp-spawn writes one
  whenever a system prompt is given, `bin/tp-spawn:786-789`). The real task `coder-2bde9a72ff` run.sh launches
  `tp-claude --name … --tools Agent,Bash,…,Write --model claude-opus-5-5 --effort medium -- "$(cat packet.md)"`: no system prompt.
- Workers load no project instruction files: Claude `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` (`bin/tp-claude:20`), pi
  `--no-context-files` (`bin/tp-pi:74`, `bin/tp-spawn:720`). The packet (first user message) is everything they know.
- Every Claude launch (Main and worker) gets one hidden extra system line: the `|| echo exit-code: nonzero` suffix rule
  (`bin/tp-claude:54,89,96`). It is not in `system-prompt.md`, so the "holds exactly this" note at `bin/tp-spawn:760` is not the full system text.

## 1. Launch routes

| Route | Instruction blocks assembled (by code) | Tools exposed | safe_read / safe_grep guidance reaches it? | Universal contract (scope / report / RESULT_STATUS / safe tools) |
|---|---|---|---|---|
| **Main start, pi** (default; `bin/tp:163` -> `bin/tp-main`) | `agent/orchestrator.md` + `agent/orchestrator-pi.md` as `--append-system-prompt` (`bin/tp-main:145-150`); pi `--no-context-files` (`bin/tp-pi:74`). No project preamble, no AGENTS.md. | `MAIN_TOOLS` = read,bash,edit,write,grep,find,ls,codemode,dispatch_role,task_index,task_collect,tmux_pi_find_skill,ask_local,tool_output,safe_read,safe_grep (`bin/tp-pi:20,76-77`) | Only the tool descriptions (`agent/extensions/safe.ts:65-68,97-100`). Orchestrator text says nothing about safe tools or about telling workers to use them. | N/A as worker; Main has no rule to pass a search rule into briefs. |
| **Main start, Claude** (`TP_MAIN_HARNESS=claude`) | `orchestrator.md` + `orchestrator-claude.md` (`bin/tp-main:152-155`), then tp-claude appends the suffix rule (`bin/tp-claude:89`). CLAUDE.md disabled (`bin/tp-claude:20`). SessionStart: plugin module injects the task index incl. `objective:` (`claude/plugins/tmux-pi/hooks/register.tsx:72-76,675-685`). | All Claude built-ins (no `--tools`), `--allowedTools "Bash(tp-agent:*)"` (`bin/tp-main:153`); MCP from claude/mcp.json; subagents server exposes only `get_skill` to an orchestrator main (`claude/mcp/subagents.mjs:16-18`); docs MCP if `TP_DOCS_TOOLS=1` (`bin/tp-claude:46`); ctx MCP never (needs TP_TASK_DIR, `bin/tp-claude:49`). | Tool descriptions only (`claude/mcp/safe.mjs:52-55,79-81`), which say "for credential/env files the normal Read refuses". | — |
| **Main legacy** (`TP_MAIN=claude`) | `claude/main-prompt.md` (`bin/tp:165`), `TP_MODE=legacy`. Uses spawn_subagent etc. | subagents MCP tools (`claude/mcp/subagents.mjs:16-17`) | No | — |
| **Main restart / compaction (Claude)** | Same system prompt (prompt is re-sent by the harness; a relaunch reruns tp-main). PreCompact: `tp-agent work compacted` + `work checkpoint --auto` (`hooks/precompact.sh:6-12`). SessionStart(compact): `work compacted` + `tp-agent index --no-write --hook-json` injected (`hooks/sessionstart-compact.sh:12-46`). Compact Instructions in `orchestrator-claude.md:10-12`. | as Main | No | Objective survives only via `session-objective.md` (`bin/tp-agent:354,368,1649-1650`; index head 1000 chars, `lib/index_text.py:334-336`) and an attached workspace. |
| **Main compaction (pi)** | No hook; prompt tells it to run `task_index` and `work recover` (`orchestrator-pi.md:7`). | as Main | No | Depends on the model following the sentence. |
| **Worker spawn, Claude** (`tp-agent dispatch` -> `launch` `bin/tp-agent:690-731` -> `bin/tp-spawn`) | System prompt: `# Repository Instructions` = worker-preamble general + role section + Tools block only if manifest (`bin/tp-spawn:538,742-760`); here: none. Packet: `# Task`, `# Test Scope`, `# Local Task Artifacts` (`:539-545`), `# Worker Role` = whole `agent/roles/<role>.md` (`:546-550`), `# Expected Deliverable`, `# Return` (RESULT_STATUS, `:556-557`), request channel (`:559`), FAILING_TESTS (`:561`), `# Selected Task Skills` (`:564-577`), `# Context` (notes, files, lane facts). | `--tools Agent,Bash,Edit,Glob,Grep,ListAgents,Read,ReportFindings,ScheduleWakeup,Skill,ToolSearch,Workflow,Write` (`bin/tp-spawn:727-728`) + MCP safe tools (settings.json:14); ctx MCP when main has TP_CTX_TOOLS=1 (`bin/tp-claude:49`, `bin/tp-spawn:775-777`). | **No.** Only the coder role's `grep -rn` (coder.md:21). STANDING line (safe_read only) needs a manifest. | Scope: "state the assignment" (Return, all roles); "stay inside the brief" only coder.md:25. Report + RESULT_STATUS: yes (Return). Safe tools: **no**. |
| **Worker spawn, pi** | Same packet; same system prompt rule; pi `--no-context-files` | default pi tools + reporter, sensitive, safe (safe_read/safe_grep), kill-guard extensions (`bin/tp-pi:60`); docs/ctx extensions only with a task + switch (`:66-73`). Not MAIN_TOOLS (that is main only, `:76`). | Tool description of safe_grep says "not rg or grep through bash, for files outside the working tree" (`safe.ts:98-99`). No packet text. | Same as Claude worker. |
| **Retry** (`tp-agent retry`, `bin/tp-agent:1537-1614`) | Re-dispatch through the same tp-spawn path; task = `FIX_BRIEF_SENTENCE` + original request when `--reason` names an .md (`:592,1593-1596`); note "Retry of X … Prior packet … Prior result …" (`:1607`); route note (`:1609-1610`); `--parent` passed (`:701`) but `Prior result/review report` line (`lib/agent_tools.py:162-190`) only rendered with a manifest. | as spawn | No (fix briefs under docs/plans/det-prep/fixes do not instruct Bash searches; grep/rg appear there as test-input rows only) | Same as spawn. |
| **Message / correction** (`tp-agent message`, `bin/tp-agent:1715-1730`) | Raw text pasted into the pane via `lib/pane_send.py` send; appended to `messages.jsonl`. No standing text added. Contract reminders likewise typed (`bin/tp-contract:268-273`). | unchanged | No | Nothing added; brief text only. |
| **Review** (`tp-agent review` -> `bin/tp-review:482-487`) | tp-spawn with `--preamble-role code-reviewer` and **no `--role`**: no `# Worker Role` section (`bin/tp-spawn:546`). Task = `review_task` (`bin/tp-review:245`) built from `outside_rule` (`:244`), `findings_rule`, `coverage_rule`, `verdict_rule` (`:240-243`) + round text (`:456-461`); contexts claims-last; notes. | per reviewer harness as spawn | **Yes, partly**: `outside_rule` "Read or search files outside the working tree with safe_read and safe_grep, never with rg or grep through bash" (`:244`). Nothing for in-tree search. | Report/RESULT_STATUS yes (Return + verdict_rule). **code-reviewer.md is never delivered** (see §4). |
| **Lane step** (`dispatch --lane`) | Same as spawn + `--preamble-from` main checkout (`bin/tp-agent:701`, `bin/tp-spawn:215-218`), lane facts file as context, `LANE_BASE` worker env (`bin/tp-agent:703-709`). Main checkout has no preamble/manifest -> nothing added. | as spawn | No | as spawn |
| **Contract task** (`tp-spawn -k research|task`; not passed by `tp-agent launch`) | Contract terms as first note (`bin/tp-spawn:474-478`); later reminders typed by `tp-contract enforce`. | as spawn | No | as spawn |
| **Investigate / trial** | `dispatch --investigate-failure` -> `investigate` (`bin/tp-agent:1237`) -> same dispatch; trials run tp-spawn/tp-agent via `lib/trial_session.py:244-251`. | as spawn | No | as spawn |
| **Native Claude Agent tool (from Main or from a Claude worker)** | Only the prompt the parent writes. Main has Agent (no `--tools`); workers list `Agent` in `--tools` (`bin/tp-spawn:728`). Children inherit env (`TP_MODE=orchestrator`, no TP_TASK_DIR for Main's children) and plugin hooks. | subset chosen by Claude Code | No | **None**: no packet, no RESULT_STATUS, no role, not recorded as a task. |
| **pi subagents extension** (`agent/extensions/subagents.ts`) | Legacy only: not loaded by `bin/tp-pi:57-63` in normal sessions. When used, `spawn_subagent` -> tp-spawn with no `--role` (`subagents.ts:205-217`): Return block yes, role no. | — | No | Return only. Same for Claude legacy `spawn_subagent` (`claude/mcp/subagents.mjs:92-112`). |
| **Bench** (`bin/tp-bench` -> `lib/bench/runner.py:287-289`) | Dispatches through the arm's own `tp-agent dispatch` into a project copy. The bench base commits a `.tmux-pi/agent-tools.json` (`lib/bench/project.py:14,36`; fixture `runner.py:552`). | as spawn | **Yes (safe_read line only)**: bench workers get the STANDING block that production tmux-pi workers do not. Bench is not representative of production instructions. | as spawn + STANDING |

## 2. Every read/search statement

| Where | Statement | Conflict? |
|---|---|---|
| `agent/roles/coder.md:21` | "find the tests that already reach the code: `grep -rn SYMBOL tests/`" | **Yes**: Bash recursive grep; the only search instruction a production coder receives. Conflicts with SG1 direction and tp-review:244 spirit. (Lane `safe-search` changes it to "safe_grep with pattern SYMBOL and path tests/" — bare name, wrong for Claude, whose tool is `mcp__tmux-pi-safe__safe_grep`.) |
| other `agent/roles/*.md` (architect, code-reviewer, data-flow-reviewer, infrastructure, researcher, tester, vulnerability-reviewer) | No read/search statement. | Silent; tester/researcher get nothing at all on how to search. |
| `agent/prompts/*.md` (counterpoint, make-qa, prepare-cases, product-design, product-fit, threat-model) | None. | — |
| `agent/orchestrator.md`, `-claude.md`, `-pi.md`, `claude/main-prompt.md` | None (no grep/rg/Read/safe_* text). | Main is never told the safe-tool rule nor to put it in briefs. |
| `lib/agent_tools.py:43` (STANDING) | "Read a range with the Read tool (offset, limit) or safe_read, not sed -n." | Read-only; no search rule; no safe_grep; Claude name not prefixed. Delivered only with a manifest (`:195-197`). |
| `bin/tp-review:244` | "Read or search files outside the working tree with safe_read and safe_grep, never with rg or grep through bash" | Reviewers only; scoped to outside the tree. |
| `bin/tp-spawn` | No worker-facing read/search text (only internal `sed -n` at :94,407,412 and a comment at :724-728 about adding Grep/Glob). | — |
| `agent/extensions/safe.ts:65-68` | safe_read: "Use this for files outside the working tree and for credential files and env files the normal read tool asks about" | Differs from Claude twin. |
| `agent/extensions/safe.ts:97-100` | safe_grep: "Use this, not rg or grep through bash, for files outside the working tree" | Implies bash grep is fine inside the tree. |
| `claude/mcp/safe.mjs:52-55` | safe_read: "Use this for credential files and env files that the normal Read tool refuses" | **Narrower than pi**: implies ordinary files use Read; says nothing about outside the tree. |
| `claude/mcp/safe.mjs:79-81` | safe_grep: "Includes hidden files such as .env*, so it can search what the normal Grep refuses." | No "instead of Bash grep" at all. |
| `lib/redact_hook.py:330` | deny/ask message: "Use mcp__tmux-pi-safe__safe_read (redacted) for its structure…" | Hook-time only, some file kinds (per SG1 brief). |
| `docs/agent-tools.md:135,143` | Same as STANDING. `:82` "A project with no .tmux-pi/agent-tools.json gives its workers no Tools block at all"; `:123` "No manifest: nothing changes". | Documents the gap as intended. |
| `docs/prompt-examples/04-tmux-pi-operations.md:102-103` | Briefs should use "scoped `git -C <wt> grep -n …` instead of a repo-root `rg`, and `safe_grep` / `safe_read` outside the worktree. Paste the git -C grep form into the brief" | **Conflicts** with SG1 fix text (safe_grep first; Bash only on exact known-safe paths). Owner-facing example, not auto-delivered. |
| `docs/prompt-examples/03-security-and-quality.md:87,92` | "`git -C <wt> grep -l …`", "Grep for a helper before writing one" | Same family as above. |
| `docs/prompt-examples/case-work-prompt.md:51` | "Read, grep, cat or source `.env*` …" (as a forbidden item) | Consistent. |

Net: a production worker on main gets exactly one search instruction (coder.md:21, Bash grep), tool descriptions that disagree
between harnesses on when the safe tools apply, and no read rule unless the project has a manifest.

## 3. Leads

1. **coder.md recommends `grep -rn`; contradictory guidance** — **source-confirmed gap on main.** `agent/roles/coder.md:21`
   vs `lib/agent_tools.py:43` (Read or safe_read), `bin/tp-review:244` (safe tools outside the tree, never bash grep), safe.ts:98
   vs safe.mjs:52-55,79-81 (different scope per harness), prompt-examples 04:102-103 (git grep). Fix is in flight, not merged:
   lane `safe-search` (SG1, brief `docs/plans/det-prep/fixes/sg1-safe-search.md`, round 9 commit 4ebc921) edits coder.md:21 and
   adds a `# Searching and Reading Files` block in tp-spawn for every worker. Residual on that lane: coder.md says bare
   `safe_grep` while the tp-spawn block uses the Claude prefixed name; tool descriptions and prompt-examples are not changed by the diff I saw (only coder.md and tp-spawn vs main).
2. **agent_tools.py omits the whole Tools block without a manifest** — **source-confirmed.** `lib/agent_tools.py:195-197`
   (`render` returns "" when `manifest_path` is None) and `lib/preamble.py:94-96` (returns preamble text unchanged; `_agent_tools`
   :78-85 never imports the module). Lost with it: all four STANDING lines (:41-46: no re-request, Read/safe_read, `tp-agent
   request`, `tp-agent message`/`test-run.sh`) **and** the retry `Prior result: …; review report: …` line (:162-190), even though
   retries pass `--parent`. Real proof: 0 `system-prompt.md` files in all sessions; coder-2bde9a72ff (a retry of coder-df22b45f7c)
   has no Prior result line from this path (its packet has only the tp-agent retry note). Partly mitigated: `tp-agent request` and
   the retry note are also in the packet (`bin/tp-spawn:559`, `bin/tp-agent:1607`). Documented as intended (`docs/agent-tools.md:82,123`).
3. **persistent-work.md describes decision-recording instructions missing from the active Main prompt** — **source-confirmed
   (partly delivered).** `docs/persistent-work.md:93-101` gives the paragraph "record each decision the moment the user makes
   it (`tp-agent work decide ID …`) … Never answer a blocking decision yourself … After a compaction or restart, run work recover,
   re-verify any done claim …" for orchestrator.md/-claude/-pi. `grep "work decide|decision add|blocking decision"` over
   agent/orchestrator*.md and claude/main-prompt.md: no match. Only the two compaction sentences shipped
   (`orchestrator-claude.md:12`, `orchestrator-pi.md:7`). `docs/persistent-work.md:67-69` itself says a PreCompact checkpoint
   cannot capture a decision not written through `work decide`, so the missing paragraph is exactly what would preserve it.
4. **Main = generic orchestrator + overlay; project preamble and context tools serve only workers** — **source-confirmed.**
   Main prompt = orchestrator.md + overlay only (`bin/tp-main:145-155`); project files suppressed (`bin/tp-claude:20`,
   `bin/tp-pi:74`); `worker-preamble.md`/`agent-tools.json` read only by tp-spawn (`bin/tp-spawn:538`); ctx tools only with
   TP_TASK_DIR (`bin/tp-claude:47-49`, `bin/tp-pi:69-73`); pi docs tools task-only (`bin/tp-pi:64-68`), Claude docs MCP is not
   gated on a task (`bin/tp-claude:46`). Main's only project context is the objective/index and whatever it reads itself.

## 4. Other findings (worker loses safe-tool guidance / Main loses purpose)

- **Reviewers never receive `agent/roles/code-reviewer.md`.** tp-review passes `--preamble-role code-reviewer` with no `--role`
  (`bin/tp-review:482`); tp-spawn adds `# Worker Role` only when `--role` is set (`bin/tp-spawn:546-550`; usage `:67-68` says
  "without loading the role's instructions"). Real review `review-coder-2bde9a72ff-213750`: meta `role: null`, packet headings
  Task/Local Task Artifacts/Return/Context, 0 matches for "Claims last" / "Diagnose, do not only list" / "timer-ok" / "SLOW_TESTS".
  Lost: the tests rule and letters (a-d), "Review the tests as code" (timer-wait defects, judged against the packet's "Timer waits"
  section that tp-review does attach), "Diagnose, do not only list" / third-round family escalation, net-seconds budget,
  `SLOW_TESTS:`. `orchestrator.md:23` and `main-prompt.md:26` tell Main tests "are judged by the rule in the … code-reviewer
  roles" — that rule does not reach the reviewer. (review_task restates claims-last, coverage, Closes by, verdict only.)
- **Bench measures a different instruction set from production**: bench copies carry a committed manifest (STANDING lines),
  production tmux-pi workers do not (§1 Bench row).
- **Tool descriptions disagree across harnesses** (safe.ts says "outside the working tree", safe.mjs says "files Read refuses"),
  so the same rule produces different behaviour per harness even after SG1.
- **Native Agent-tool children** (Main's and Claude workers', `Agent` in `bin/tp-spawn:728`) get no packet, no RESULT_STATUS, no
  safe-tool rule and no task record; Main's children inherit `TP_MODE=orchestrator` with no TP_TASK_DIR, so any SG1 "worker
  refused, not asked" rule keyed on TP_TASK_DIR treats them as Main (ask, not refuse).
- **Only coder has "stay inside the brief"** (coder.md:25); tester/researcher/architect/infrastructure have no scope rule beyond the Return sentence.
- **Main purpose on pi**: no session-start or compaction injection; relies on the prompt to call `task_index` (`orchestrator-pi.md:7`).
  Objective is only as current as Main's `tp-agent objective` calls (`orchestrator.md:5`); index shows its first 1000 chars.
- **Messages and contract reminders** carry no standing text, so a correction that changes scope does not re-state the safe-tool or report rules.
- **Hidden system text**: tp-claude's suffix rule is appended to every Claude launch but not recorded in system-prompt.md.

## 5. Is the effective launch package recorded?

Checked `.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/coder-2bde9a72ff/`: `meta.json`, `dispatch.json`, `packet.md`,
`request.md`, `task.md`, `run.sh`, `skill-selection.json` (`[]`), state/result/report, evidence dirs. **No `system-prompt.md`.**

Recorded today:
- `packet.md`: the full first message, including a verbatim copy of the role file text as of launch.
- `run.sh`: exact harness command, `--tools` list, model, effort, env exports (`LANE_BASE`, `TP_MAIN_ID`, `unset TP_CTX_TOOLS`).
- `meta.json`: role, worker, model, thinking, parent, lane, lane_step, preamble_from, workdir, review(s), and `route_info`
  (card_sha256, catalog_version, decided_by, effort, level…; `prompt_sha256` is the router's prompt hash, null here, not the worker's instructions).
- `dispatch.json`: contexts, notes, omitted_context, tests, base_commit (of the project), lane, main.
- `system-prompt.md`: only when a system prompt was given (`bin/tp-spawn:786-789`) — never, in this repo.

Not recorded: the tmux-pi commit / hash of role, orchestrator or preamble files; the MCP server set and tool list actually exposed
(mcp.json, plugin version, settings); tp-claude's appended suffix; for reviewers, which role text was (not) used beyond `role: null`;
Main's own launch package (tp-main composes the prompt in memory; nothing writes it to the state dir); native Agent-tool children.
