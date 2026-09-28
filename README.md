# tmux-pi

A tmux workspace for running a main [pi](https://github.com/earendil-works/pi) agent
that delegates to subagents you can watch live.

```
┌──────────────┬──────────────┐
│              │ status       │
│ main pi      ├──────────────┤
│              │ viewer       │
└──────────────┴──────────────┘
```

## Requirements

- tmux 3.3+ (`brew install tmux`)
- pi

## Run

```sh
bin/tp            # workspace for the current directory
bin/tp ~/code/x   # workspace for another directory
```

To launch it as `agent` from any directory:

```sh
ln -s "$PWD/bin/tp" ~/.local/bin/agent
```

Re-running `tp` reattaches to the existing session. Set `TP_SESSION` to run more than one.

tmux-pi is self-contained: pi (`bin/tp-pi`) and Claude Code workers (`bin/tp-claude`) load only
this repo's `agent/`, `claude/` and `skills/`, never `~/.pi`, `~/.agents` or `~/.claude`.
Run `/login` once inside the workspace for pi's subscription providers.

A Claude worker stopped at a choice prompt (Claude Code's one-time folder trust
in a new directory, or a tool permission) is shown in main as a dialog with the
prompt's own options; your pick is keyed into the worker. The dialog closes if
you answer in the worker's pane instead. To settle folder trust ahead of time:

```sh
bin/tp-claude-bootstrap /path/to/target-project
# approve trust if prompted, then type /exit
```

## Subagents

Ask the main agent to delegate. Its subagents extension
(`agent/extensions/subagents.ts`) gives it these tools:

| Tool | Does |
|---|---|
| `spawn_subagent` | Builds the context packet from the task, `files` (whole or line ranges) and `notes`; routes the task with `tp-route` unless a model is given; then `tp-spawn`s it |
| `list_subagents` | `tp-list`; available without main edit authority |
| `send_to_subagent` | Types a follow-up message into the subagent's session |
| `collect_result` | State and last reply; `wait_seconds` waits until it finishes. Available without main edit authority, but its canonical collect step refuses unresolved required review. A subagent stuck at a prompt (Claude's folder trust, a permission request) comes back with its screen |
| `kill_subagent` | `tp-kill` |
| `/reset` | Stops all subagents, clears the viewer, and starts a fresh main Pi session |
| `/end` | Stops the main agent and all subagents, archives/retains task records with a cleanup log, then destroys the tmux-pi workspace |

Inside a tmux-pi session the `tp-*` commands are on PATH (for you and the agents):

```sh
tp-spawn -w claude -m sonnet -t high "Refactor the auth module"   # prints an id
tp-spawn -m slotstream/qwen3.8-flash-next:4bit -f task.md -v       # pi worker, view now
tp-spawn -c src/auth.ts -c src/db.ts:40-90 -N "tokens expire after 1h" "Fix the refresh bug"
tp-spawn -o notes.md -b time=10m,turns=40 -p outline=2m,draft=6m "Survey the logging calls"  # contract
tp-spawn -k research "Does GitHub Pages honor custom Cache-Control?"  # research contract
tp-list            # subagents, state, tokens, cost, current tool
tp-view <id>       # swap its live session into the viewer (click in to type)
tp-view -          # put the placeholder back
tp-kill <id>       # stop it; task files stay in .tmux-pi/tasks/<id>/
tp-clean           # archive killed subagents to .tmux-pi/archive/
tp-task list       # retained task records by id/title/ticket; tp-task resume <query> prints task.md
tp-task cleanup --dry-run --write-review cleanup.json  # review-first retention candidates; no deletion
tp-task cleanup --delete-reviewed cleanup.json --confirm DELETE-ELIGIBLE-UNPINNED  # explicit reviewed deletion only
tp-task preflight --mode plan --record "Design the API"  # Slice 2 durable stop-mode contract
tp-task context --json "Fix PROJ-12 in src/auth.py"  # Slice 3/5 context + capability manifest preview
tp-task capabilities --json "Fix PROJ-12 in src/auth.py"  # Slice 5 selected capability disclosure
tp-task capability-request --manifest .tmux-pi/tasks/<id>/context-manifest.json "Sequential Thinking | reason=..."
tp-task capability-resolve <id> --approve|--deny  # human shell only, not a worker/model command; dispatcher follows resolution through result/review
tp-task review-list    # list unresolved handoff and delegated-task review blockers
tp-task review-resolve --task-id <id> --outcome reviewed --review-reference <report> --reason "..." --confirm RESOLVE-REVIEW-REQUIRED  # human decision for a delegated blocker
tp-ledger          # research and contract runs: time, turns, tokens, sources, your rating
tp-reset           # stop every subagent and restore the viewer (used by /reset)
tp-exit            # stop every agent, archive/retain task records, and destroy the tmux-pi session (used by /end)
```

Review approvals bind the exact author `result.md` bytes and review round; a follow-up re-arms even a previously human-resolved task. Legacy approvals without an author hash are blocked: inspect the result and reviewer report, then use the human-only `review-resolve` command if accepting the risk. If a malformed, symlinked, wrong-id/actor, or `required:false` review marker blocks collection, the CLI intentionally cannot waive it. **Manual repair:** stop the worker, back up the task directory and `.tmux-pi/review-resolutions.log`, inspect `meta.json`, `result.md`, reviewer evidence and the marker, then restore a regular `review-required.json` with `required:true`, `task_id` matching `meta.id`, and `status:"needs_human"`. Re-run `review-list`, explicitly resolve with the human CLI (including reason and report reference when applicable), and retain the backup/audit trail. Never simply delete a blocker to permit collection. For duplicated archived ids, use the renamed archive directory name only when it uniquely identifies the unresolved task; two unresolved copies sharing the original id require individual inspection.

The status pane (top right) is the same list, live. Click it, then:

| Key | Action |
|---|---|
| `j` / `k`, `↑` / `↓` | move |
| `enter` | show the selected subagent in the viewer |
| `o` | show it and move focus into it, to type to it |
| `-` | clear the viewer |
| `r` / `t` | read its last reply / its context packet in a popup |
| `x` | kill it (asks first) |
| `c` | archive killed subagents |

States come from each worker's reporter: `starting`, `working`, `tool` (the tool and
its argument show below the row), `waiting` (finished its turn, ready for more),
`permission` (Claude is asking to use a tool), `error`. Once the process ends:
`done`, `failed:<code>` or `killed`.
A second line shows elapsed time and turns and, for a task with a contract,
its checkpoint state and when its artifact was last written. Settled work also shows the `result.md` path.

### Context packets

A worker's first message is its `packet.md`: the task, then each file passed with
`-c path` or `-c path:start-end` (in a code fence, labelled with its path and range),
then the `-N` notes. Workers load no `AGENTS.md` or `CLAUDE.md` and never see
main's conversation, so the packet is all the project context they start with;
they can still read other files with their tools. Limits: 60k characters per file
and 200k per packet (use line ranges for big files). Credential files (`.env*`,
keys, `auth.json`, `*secret*` and similar) are refused.

Slice 3 adds a provenance-aware context manifest for packets. Slice 5 layers a versioned capability registry over that manifest. After main preflight, deterministic policy selects the minimal initial categories from repository/filesystem search, shell/test/verification, and qualified reasoning/review profiles. Profile-only stubs (YouTrack, Context Engine, read-only DB, official documentation, browser/rendered UI validation) are documented reference categories only: they are not disclosed as available authority and cannot be auto-granted until fixture-qualified. Laya capability category wiring is inactive/pending fixture-safe selection; it may be previewed by CLI but the dispatcher does not pass Laya categories into packets. Only selected categories get detailed instructions in the packet; the full registry is not dumped into model context. Model `CAPABILITY_REQUEST:` lines are recorded in the manifest: already-approved fixture-qualified read-only/local capabilities may be granted within per-task caps, while profile-only, new, unapproved, write-capable, external, destructive, expanded-data, task-irrelevant, or over-cap requests require human approval and put the task in `needs_input`. These are profiles/stubs only; no real MCPs, broad external retrieval, generic RAG, or database row dumps are enabled.

Slice 6 automation is fail-closed while remediation fixtures are pending. Unqualified routed work is a visible recommendation/manual status; it is not auto-started. The low-risk read-only/investigation and small scoped-edit cells exist only as pending cells until real dispatcher-path model/tool-route fixtures qualify and enable them. Structural medium/higher-risk contracts, sensitive/PII/security work, `+review` requests, high-risk/consequential work, and repeated-failure cases require report-only independent review and/or manual handling. `+delegate`/`+here` are explicit placement requests; `+review` only raises review and never bypasses risk/qualification. If an opposite-provider reviewer cannot start, tmux-pi asks the human and does not silently substitute the same provider or mark the review skipped. Cleanup remains review-first: dry-runs may be saved as a reviewed batch, and direct deletion requires the explicit `--delete-reviewed ... --confirm DELETE-ELIGIBLE-UNPINNED` command; pins, ticket-linked tasks, plan/prototype stop-mode records, promoted durable records, paused/resumable state, and durable artifacts are rechecked and protected. `/end`/`tp-exit` no longer removes task records blindly: it archives worker records, retains main-owned durable records, and appends the action batch to `.tmux-pi/cleanup.log`.

Each task dir holds canonical human `task.md`, verbatim `request.md`, `packet.md`, optional `context-manifest.json`, `meta.json`, the generated `run.sh`, and the reporter's `state.json`
and `result.md` (the last reply). `task.md` shows title/ticket, lifecycle state, request summary, context manifest summary, result/artifact links, and next action while the runtime files stay compatible. pi workers report through
`agent/extensions/reporter.ts`, Claude workers through the tmux-pi plugin's hooks
(`claude/plugins/tmux-pi/hooks/`). Claude's Stop settles the current submitted prompt;
late tool hooks cannot reopen it until another UserPromptSubmit. The next prompt
invalidates the previous `result.md` before work resumes. Hook writes serialize per
task using a macOS-compatible advisory lock; `tp-review` still requires both a
result and a settled worker. Token counts include cached input; cost is shown
only for pi providers that price tokens.

See [docs/plan.md](docs/plan.md) for the design and milestones.

## Local model router

`bin/tp-route` uses Laya's `typed-decisions` checkpoint at
`127.0.0.1:11436/v1/systemone` by default. It asks six bounded questions: the
task's intent (a six-way choice), difficulty on a four-level scale, and as
yes/no probabilities whether it needs tools, is sensitive, is high-risk, and
changes files, systems or data. A small learned layer,
[`config/laya-model.json`](config/laya-model.json), reads all of Laya's
probabilities and picks a route from
[`config/router.json`](config/router.json), whose target must be in the allowed
`tp-spawn` roster. The output is exactly `worker`, `model`, and `thinking`;
classification alone never spawns a worker.

Bootstrap once when `.laya/src` and `.laya/venv` are absent:

```sh
mkdir -p .laya
git clone https://github.com/NandhaKishorM/laya.git .laya/src
git -C .laya/src checkout 4066d5d5fbf08b66c6757ddeedbd797bd7655bc0 # verified Laya 0.3.20
python3.11 -m venv .laya/venv # Python 3.10+ required; use your compatible interpreter
.laya/venv/bin/python -m pip install -e '.laya/src[serve]'
.laya/venv/bin/python -c 'from laya import Router; Router().preload(["typed-decisions"])'
```

The final bootstrap command fetches the checkpoint into the local cache.
`bin/tp-laya` itself runs offline: it never installs packages or downloads
weights. It binds only `127.0.0.1`, preloads only `typed-decisions`, chooses
MPS when available (otherwise CPU), and logs under ignored `.laya/`. Set
`TP_LAYA_DEVICE=cpu` or another supported torch device to override selection.
It refuses to start if its venv is missing or port 11436 already has a listener.

```sh
bin/tp-laya start
bin/tp-laya status
curl --noproxy '*' http://127.0.0.1:11436/health
bin/tp-route --check
bin/tp-route 'Review the authentication flow'
bin/tp-route --file task.md
bin/tp-laya stop                 # stops only a process started by tp-laya
```

An independently started Laya service can be used on the same endpoint.
`tp-laya stop` leaves such a service alone; stop it with its own process manager.

The routes are `simple` (Slotstream/off), `standard` (Slotstream/low),
`coding` (GPT-6 Sol/medium), `complex` (GPT-6 Sol/high) and `expert`
(Claude Opus/high, also used for sensitive or risky work). The learned layer is
multinomial logistic regression over Laya's answer probabilities, trained on
labeled tasks (see [Calibration](#calibration)); inference is plain Python.
Laya's answers carry limited information about how hard a task is: the route
matches the labeled tier for about 55% of held-out tasks and the labeled model
for about 66%, and it sends about 22% of tasks to a cheaper tier than labeled.
Pass an explicit model to `spawn_subagent` or `tp-spawn` when that matters.

The configured roster has eight entries: local Qwen, Slotstream, GPT-6
Luna/Sol/Astra and Claude Haiku/Sonnet/Opus. The coding and complex tiers
both use GPT-6 Sol at different thinking levels; the other permitted models
require a deliberate route or model selection. GPT-5.6 and Spark are not in
the active roster. Local/Slotstream quality, model availability and prices
must be rechecked before relying on them for unattended work.

The `models` array is the allowed spawn roster; `routes` must point into it.
It lists eligible targets, not a guarantee that those providers are available.
The adapter accepts only unauthenticated loopback HTTP, disables proxies and
redirects, validates responses and route targets, and fails clearly on server
or data errors.

## Qualified automatic routing

R5 first routes the request with Laya, persists a **non-authorizing** scaffold in `.tmux-pi/handoffs/<handshake-id>/`, and gives main a hidden Orient/Frame instruction. Ordinary low-risk answers need no contract. Delegation, review, stop modes, and edits require main to call `tmux_pi_commit_contract`: it persists the main contract, evaluates deterministic policy, then (only for authorized delegation) terminates the turn and dispatches at `agent_end`. A failed router/policy never authorizes a spawn. The original and effective requests remain in the handoff scaffold for recovery. A main-placed Pi route currently stays on the existing model; R5 does **not** switch models in place. Claude main placement is a foreground worker behind the same gate.

No automatic cells are currently enabled: read-only/investigation and small scoped edits remain pending until real dispatcher-path fixtures qualify them. Everything else becomes a recommendation/manual status with the proposed route and review state. Use `+delegate` or `+here` only when you intentionally make an explicit placement request; `+review` raises review but does not authorize dispatch. Human-facing notices must not be treated by the main model as permission to bypass the gate with manual subagent tools.

Progress is posted into the main conversation as `tmux-pi-auto` messages
(delegated, a worker's question, the result), so main also sees them on its next
turn. When review is required, a report-only opposite-provider reviewer (`tp-review`) starts as
soon as the author reports. If it requests changes, its findings go back to the
author once, and the author is reviewed again. If the reviewer cannot start or fails before approval, tmux-pi asks the human; it does not silently use a same-provider substitute or skip the review. A worker that ends its reply
with a question is not reviewed until you answer.

While delegated tasks are still running, each new message is first checked
against them (`tp-route --follow-up`). A message for one of them is typed into
that worker and saved to its `followups.md`, which its reviewer also reads. When
the judge is unsure, or two tasks could claim the message, you pick. With the
`followup` key in `config/local.json`, the local MLX model judges; without it,
Laya does, and asks you more often.

Start a message with `+followup` to send it to a running task without the
judge: `+followup <id> …` names the task (any live subagent, including one main
started), otherwise it goes to the only running delegated task or you pick one.
Start a message with `+plan`, `+prototype` or `+investigate` for stop-mode work:
main gets a no-edit instruction instead of an implementation dispatch. This is
a guarded no-edit instruction, not an OS sandbox. Before a low-risk direct edit contract (including when no handshake exists), edit/write and all bash except exact `pwd`/`true` are blocked. Shell commands with arguments, including `git --output`, `rg --pre` and `git --ext-diff`, are **not** read-only authority. Registered extension commands are consumed before the input event; slash-prefixed prompts that reach input (templates/skills/path-like text), including extension-source prompts, get their own handshake. Other extensions and unrestricted post-contract bash remain outside this cooperative gate's OS security boundary. A durable stop record is created at input (including `+investigate`), captured at `agent_settled` (after retries/continuations), and remains open if no final output exists. A mismatched direct/delegate stop contract is rejected.
Start a message with `+research` to send one question to a research worker
(see [Research contracts](#research-contracts)); `+research +review` also has
another provider check the report's sources.
Start a message with `+here`, `+delegate` or `+review` to override placement or
review (they can be combined; the route stays a validated roster target; a
marked message is never treated as a follow-up). Before automatic worker dispatch,
the main extension writes a compact Slice 2 preflight into the task record for
non-direct work: task mode, assumptions/frame, allowed paths/tools/MCP stance,
decision boundary, proof path and artifact tier. Direct low-risk main work remains
compact and may stay Tier 0/no record; Tier 2+ promotions are included in the
compact dispatch notice. `/auto off` disables routing and blocks main edits and non-read-only bash; it does not grant manual worker-spawn authority. The real router sends implementation/config changes to subagents, so a direct contract alone does **not** enable main edits on those routes. For a low-risk main placement explicitly request `+here`, then persist a direct contract; any required review or confirmation still blocks edits. Most edits require delegated review or manual handling because there are no qualified automatic cells. Model-initiated spawn/send-to-subagent tools and recognizable bash worker commands remain blocked even with AUTO off, but this is best-effort pattern detection, not a shell sandbox. Required direct-main or delegated review is retained in `review-required.json` across subsequent turns and reported at settlement; inherited blockers are references, not new markers. While a required-review worker is actively watched, its pending marker does not itself block main edits on an otherwise authorized main turn (the shared workdir is cooperative); after restart it blocks. An escalated source is likewise exempt while its linked pending-review replacement is actively watched; after restart, both block until valid approval or explicit human resolution. Follow-ups re-arm an approved task's review marker before any new worker input; collect refuses the new round until reviewed. After watcher loss, kill, discard or archive, a human can inspect `tp-task review-list` and explicitly resolve a pending task marker with `tp-task review-resolve --task-id <id> --outcome reviewed --review-reference <report-path> --reason "..." --confirm RESOLVE-REVIEW-REQUIRED`, or choose `--outcome approved --reason "..."` with the same confirmation to accept the risk. Neither outcome is automatic. Expanded templates/skills invalidate the raw-input route and contract hash: without a new verified route they cannot authorize edits or dispatch. Use `tp-task review-list` to find original blocker ids; it is **not automatically scheduled**. The blocker is ORed into later routing/policy even for innocuous-sounding follow-ups, and main edits remain blocked until a human resolves the original handoff via `tp-task review-resolve <handshake-id> --outcome reviewed --review-reference <report-path> --reason "..." --confirm RESOLVE-REVIEW-REQUIRED` after an independent report-only review, or `--outcome approved --reason "..."` with the same confirmation after an explicit human risk decision. This CLI is a cooperative human boundary, not an OS security control: an unrestricted worker shell or post-contract main shell cannot be sandboxed by this extension. Pending capability resolution likewise stays attached to the watcher until the CLI removes the pending gate and records a resolution; the worker's fresh result is then reviewed and collected. Follow-up text typed into a worker is not re-routed or re-policied: a sensitive follow-up (including a worker-side `/name` expansion) does not raise a review-none task's policy unless an existing blocker is inherited. Treat follow-ups as manual/cooperative, not newly authorized automation. Dispatcher restart does not currently reattach to active delegated tasks. Killed workers (`/reset`, `/end`, `tp-kill`) never auto-escalate. A reporter error or explicit nonzero exit may qualify for one policy-checked escalation. Handoff directories are listed as protected in cleanup dry-runs, not automatically pruned. Human narrow/escalate dialog choices are recorded exceptions to fresh R5 contracts; queued research escalation failures leave a halted recovery notice. Worker-side escalation now writes metadata atomically and carries the effective no-edit preflight into the target's contract; cross-provider escalation targets have not been fixture-qualified, and queued research escalation after restart may not carry the entire context manifest. Runtime harness tests exercise dispatcher events with a fake child-process boundary, pinned real-router decisions and the real `tp-task main-contract`, automation and human deny-resolution CLIs (tmux signalling is faked); they are not a live Pi session or model-quality qualification. Run `tests/check_dispatcher_types.sh` for pinned TypeScript 5.9.3 checking against the installed Pi extension/Node types (`PI_CODING_AGENT_PACKAGE` may override its location). No automation cell is enabled by these tests.

## Per-machine models

Different machines have different local models, so what each tier runs on is
set per machine in `config/local.json` (git-ignored). Start from the committed
[`config/local.example.json`](config/local.example.json); it holds no
credentials. Every key is optional:

- `routes` overrides tier assignments from `config/router.json`
  (`simple`, `standard`, `coding`, `complex`, `expert`); each target must be in
  the `models` roster there.
- `mlx` is the local model `bin/tp-mlx` serves, if one is used: its id, port and sampling.
- `escalation` names the models whose pi workers hand off after too many failed
  tool calls in a row, and what they hand off to.
- `rules` redirect a task type a model is known to get wrong: when the router
  picks a model in `from` and the task text matches `pattern`, it uses `to`.
- `prompts` add standing system-prompt text to a model's workers (pi or
  Claude), for a mistake it is known to make; `tp-spawn` appends it.
- `followup` names a fast local chat model (loopback OpenAI-compatible URL,
  model id, and the `ask_between` probability band where the user is asked)
  that decides whether a message belongs to a running delegated task;
  `extra_body` adds fields to its request (such as `reasoning_effort`).
- `packet_files` lists models whose delegated workers get up to 3 files picked
  for the task in their packet, and the Claude model that picks them from the
  workdir's file list (`claude -p`, no tools; file names only, credentials
  names left out). Qwen3.6-35B-A3B passed 56 of 60 chores with picked files
  and 52 without; stronger workers gain nothing, so leave them out.

On a machine without Slotstream or MLX, leave those out and point `routes` at
models it does have. `bin/tp-route --check` validates the result;
`TP_LOCAL_CONFIG` points at a different file.

### Local model server

The `standard` tier on this machine runs Qwen3.6-35B-A3B (MLX 4-bit) in LM
Studio, which also serves the follow-up judge. pi reaches it as the `lmstudio`
provider in [`agent/models.json`](agent/models.json); thinking off is sent as
`reasoning_effort: "none"` (LM Studio ignores `enable_thinking`).

`bin/tp-lmstudio` keeps it loaded: `tp` runs it at launch and `tp-spawn` before
each worker on an `lmstudio/` model. It finds the LM Studio models the config
can send work to (routes, escalation, rules, and the follow-up judge when LM
Studio serves it), starts LM Studio's server if it is not answering, and loads
any model that is not loaded, with no idle unload. When it fails, `tp` and
`tp-spawn` warn and carry on. By hand: `bin/tp-lmstudio status` or
`bin/tp-lmstudio ensure [model-id...]`.

It takes about 20 GB while loaded. On 40 repository chores it answered 117 of
120 correctly with a median of 1.1 s (Qwen3.5-4B, used before it: 111 of 120,
1.5 s; Slotstream: 13 s). Like the 4B, it sometimes reports a count without
computing one; its workers get a prompt that the grep tool never counts and
counts come from `grep -c`, `wc -l` or `jq length` (82% → 96% correct on
counting chores), so no rule sends counts elsewhere.

`bin/tp-mlx` can serve a model with mlx-lm instead (bound to `127.0.0.1`,
offline), from the `mlx` section of `config/local.json`; add a matching `mlx`
provider to `agent/models.json` and roster entry to `config/router.json`.

### Escalation

Escalation is one-step and evidence-based. A worker may be escalated once for an
observable structured trigger: reporter error state, hard budget/checkpoint or
circuit-breaker event, a validated `TMUX_PI_ESCALATION_SIGNAL` with reason and
evidence, or capability unavailable after policy evaluation. Ordinary reply prose
is not scanned for escalation words. The target is chosen from the existing router
roster: unknown source strength fails closed; thinking is not downgraded to
off/minimal; local-to-local requires explicit qualification; otherwise tmux-pi
prefers a stronger qualified model in the same provider/family and then a
qualified cross-provider fallback. The source task records `escalation.json` with
the trigger, evidence, source/target route, outcome and next action; the target
task is marked `escalated_from`. If the escalated worker also halts, tmux-pi
pauses and asks rather than starting another retry/fan-out.

The older per-machine `escalation` key still names local pi models that should
handoff after too many failed tool calls in a row; that handoff writes the same
source task linkage (`escalated`) so main follows the new worker.

## Cross-provider review loop

After a subagent successfully exits, run `bin/tp-review <task-id>` (or use the
main agent's `review_subagent` tool). It creates a reviewer on the other
provider: Pi-authored work is reviewed by Claude Sonnet, and Claude-authored
work by Pi/Codex. The reviewer receives the original task, packet, and result,
then independently checks the working tree. It is instructed not to edit and
returns `APPROVED` or actionable `REQUEST_CHANGES`. Review linkage is recorded
in the author task's `meta.json`; use `collect_result` on the printed review id.

Before starting a reviewer, `tp-review` asks the review policy about the author's
task (`bin/tp-route --review --file task.md`), which returns `none`, `optional`
or `required` from the same Laya answers:

- research is never reviewed (it changes nothing); the learned layer reads the
  intent from all of Laya's answers, which is more accurate than Laya's own
  intent choice (0.78 against 0.74 on held-out tasks)
- implementation and configuration/operations work, and anything Laya calls
  sensitive (≥ 0.5), is always reviewed
- otherwise a learned review/no-review classifier decides
- a reviewed task is `required` unless a second classifier is confident it is
  only `optional`

On held-out tasks this never skipped a review labeled required, decided
review-or-not correctly for 92%, and ran 6 reviews in 120 that were not needed.

On `none`, `tp-review` starts nothing and exits 3. `tp-review --force <id>` (or
`review_subagent` with `force`) reviews regardless; if Laya is unreachable the
review runs anyway. The level used is recorded as `review_policy` in the author's
`meta.json`. Reviews still start only when asked; nothing hands off to review
automatically yet.

## Research contracts

A task can carry a contract: an artifact file, a budget (`time`, and optionally
`turns` and `tokens`), checkpoints (`outline`, `draft`), a stall limit and a grace
period. `tp-spawn -k task -o FILE -b time=…` adds one (`-p` sets checkpoints);
when budget/artifact/checkpoints are omitted for `-k task`, Slice 4 derives them
from the task scope, selected route/model and local/frontier balance, with hard
caps. Automatically delegated subagents now receive such task contracts;
`tp-spawn -k research` still uses `config/research.json`. The contract is in
`meta.json` and its terms are added to the packet's notes. The worker keeps
`Status: outline|draft|final|partial` as the first line of its artifact. Exception: for
`kind=task` when either the recorded preflight or main contract sets `no_edits=true`,
`tp-spawn` passes `--no-edits` to `tp-contract build`. This reply-only contract keeps
time/turn/token limits, warnings, tool-error reminders, a stop and grace-period
pause, and the existing one-escalation policy; it has no worker-written artifact,
outline/draft checkpoints or write-stall actions. The reporter's `result.md` is
the canonical output; status shows `reply-only`, not a missing artifact. Explicit
`-o`/`-p` are rejected for reply-only tasks. Research and edit-authorized tasks
still require their file artifacts.

The dispatcher runs `tp-contract enforce` every 15 seconds while main is up. For
a working worker whose artifact is not `final`, it:

- types one reminder when the outline or the draft checkpoint is missed, when
  the artifact has not been written for the stall limit, or after `tool_errors`
  failed tool calls in a row (a worker that cannot reach its sources);
- when the budget is used up, asks the worker to save its report with
  `Status: partial` and stop, and tells main;
- pauses the worker (Escape, then clears its input box) if it has no outline by
  outline + grace, keeps working for the grace period after being asked to stop,
  or keeps stalling or failing tool calls for the grace period after the reminder.

Nothing is typed into a paused worker. Each action is recorded in the task's
`checkpoints.json`; a worker waiting on a permission is left alone.

A contract task that was stopped or paused and is no longer working is
*halted*: main first attempts the single automatic qualified escalation described
above, preserving the partial artifact. If no qualified target is available, the
source was already escalated, or the handoff fails, main posts why, whether a
partial report was saved, the worker's last reply and the report path, and asks
what next:

- **Continue**: one more budget of the original size, counted from now
  (`tp-contract extend`); offered once per task.
- **Narrow** (research): a new research task on a narrower question you type,
  told to build on the partial report; the old one is closed.
- **Escalate** (research): the same question on the `escalate` route in
  `config/research.json` (Opus by default), building on the partial report.
- **Discard**: close the task; its report stays.
- **Decide later**: leave it; `+followup` or the status pane still reach it.

After the one escalation, nothing is retried automatically, on local models or otherwise.

`tp-contract status` prints each task's state and the detail line the status
pane shows. Elapsed time is always visible; cost/latency-style budget pressure is
shown as a warning only when a threshold is reached, rather than as continuous
noise. States: `starting`, `working`, `needs_input` (a permission prompt, or
a reply that ends in a question), `paused`, `settled` (idle, reply not yet
handed to main), `completed` (its result was posted into main or read with
`collect_result`), `failed`, `killed` and `escalated`. Only running tasks
(`starting`, `working`, `needs_input`) count toward the limits below.

`+research <question>` spawns a Claude Code worker (the only worker type with web
search and fetch) on the model in `config/research.json` (Sonnet at medium
thinking by default). It is told to use primary sources, to label claims as
source facts (with URL or path and access date), background knowledge,
inference or hypothesis, and to write its report to
`.tmux-pi/research/<id>.md`, which outlives `tp-clean` and `tp-exit`. Main gets
the worker's short answer plus the report path. With `+review`, a reviewer on
another provider checks each key claim against its source and classifies it as
verified, report-derived, background knowledge or synthesis.

Independent research runs in parallel: up to `max_frontier` (3) research
workers on frontier models, `max_local` (1) on local models, and `max_active`
(4) workers of any kind. Research that builds on another waits for it:
`+research +after <id>[,<id>] <question>` says so explicitly, and otherwise, while
research is running, the follow-up judge decides whether the question belongs to
one of those tasks. A request that has to wait (for its inputs or for a free
slot) goes into `.tmux-pi/research-queue.json`, main says why, and it starts on
its own when it can, with the reports it builds on named in its notes
(`tp-contract slot` makes the call).

Contract tasks go into `.tmux-pi/ledger.jsonl` when they settle, pause, finish
or are closed: task, model, provider, elapsed time, turns, tokens, cost, the
artifact's status and distinct source URLs, the checkpoint actions and why it
stopped (for example `stop: time budget used up; no partial saved`, `narrow to
research-…` or `discard`), plus the latest escalation record when there is one.
Add your judgment with `tp-ledger rate <id>
sources=ok|weak|missing cleanup=none|minor|major useful=yes|partly|no
note="…"`. The ledger is the data for deciding whether research needs a model
floor or tighter parallel limits; routing does not use it yet.

## Calibration

`tests/calibration/corpus.jsonl` holds 300 labeled tasks: for each, the
intent, route and review level it *should* get, following
[`LABELS.md`](tests/calibration/LABELS.md), and a split. The learned layer is
trained on the 180 `tune` tasks only; the 120 `holdout` tasks measure it.
`tests/calibration/answers.jsonl` records Laya's replies (typed-decisions is
deterministic, so a recording is a stable baseline), tagged with a digest of
the questions asked. `bin/tp-calibrate` scores the model per split without
contacting Laya:

```sh
bin/tp-calibrate               # score the model offline
bin/tp-calibrate --mismatches  # also list every wrong decision
bin/tp-calibrate --record      # ask Laya about new or edited tasks
bin/tp-calibrate --refresh     # ask Laya about every task again (e.g. after upgrading it)
bin/tp-calibrate --fit         # retrain config/laya-model.json (needs numpy)
```

Changing `QUESTIONS` in `lib/router.py` invalidates the recording and the model:
run `--record --fit`. Every change tried so far, with its tune and holdout
scores and whether it was kept, is in
[`experiments.md`](tests/calibration/experiments.md); check it before trying
another. The tests fail if a task has no current recorded answer, if the model
skips a review labeled required, or if `--fit` would not reproduce the shipped
model.

The previous Slotstream OpenAI-compatible chat classifier remains available
only by explicit selection:

```sh
bin/tp-route --backend openai --file task.md
# For another already-running local chat endpoint:
bin/tp-route --backend openai --base-url http://127.0.0.1:1234/v1 \
  --router-model your-loaded-model-id --file task.md
```

`TP_ROUTER_BACKEND`, `TP_ROUTER_BASE_URL`, and `TP_ROUTER_MODEL` are equivalent
environment overrides. The OpenAI-compatible endpoint must return a complete
`choices[0].message.content` JSON object with `model` and `thinking` in the
roster. The Laya backend never asks the checkpoint to emit raw model IDs.

`spawn_subagent` does this handoff. From a shell:

```sh
decision="$(bin/tp-route --file task.md)" || exit 1
bin/tp-spawn --worker "$(jq -r .worker <<<"$decision")" \
  --model "$(jq -r .model <<<"$decision")" \
  --thinking "$(jq -r .thinking <<<"$decision")" --file task.md
```
