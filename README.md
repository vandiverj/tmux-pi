# tmux-pi

A local tmux workspace for a main agent that coordinates visible specialist workers. Pi or Claude Code can sit in the main pane. Workers run in their own tmux windows, and the status and viewer panes let you inspect or enter a live worker session.

The normal path is deliberately light: the main agent answers simple questions, delegates substantial work by role, checks evidence, and chooses the next action. There is no required contract, stage sequence, or universal review gate.

## Start

Requirements: tmux 3.3+, Pi or Claude Code for the chosen main harness, Python 3, and jq. Install the worker harnesses and model services you intend to use.

```sh
bin/tp --main pi /path/to/project
bin/tp --main claude /path/to/project
```

The console `agent` command defaults to Claude as the main harness; `tp` defaults to Pi. Use `--main pi` or `--main claude` to override either default. `TP_MAIN_HARNESS` also overrides the default when inherited from an existing workspace; use `--main` to override it explicitly. With no directory argument, `agent` (or `tp`) uses the current directory. Every launch starts a fresh workspace with an empty subagent list and its own `.tmux-pi/sessions/<run-id>/` records. Existing runs and their history remain available. Use `agent --resume` to attach to the most recently created running workspace for the current project, or `TP_SESSION=<name> agent --resume` for a specific run. `TP_SESSION` can also name a new run, but an existing name requires `--resume`. A stopped run cannot be reattached; its records can be read with `bin/tp-task --state-dir /path/to/project/.tmux-pi/sessions/<run-id> list`. Older project-level records remain at `.tmux-pi/` and can be read the same way. Outside tmux, set `TP_SESSION` when targeting a running workspace with other `tp-*` commands. The main pane is left, the status pane is above the viewer on the right. Select a worker in status and press Enter to view it; press `o` to enter its live pane. `r` and `t` show its result and packet.

If the status pane looks stale, type `/refresh-status` in the Pi main agent (runs the script directly, no model turn) or run `bin/tp-refresh-status` to reload only this session's status pane (respawned with its original command) and clear its scrollback. It fails without touching anything outside a tmux-pi session or when the recorded status pane is missing or not in the main window; main, viewer, and workers are unaffected.

Claude Code uses a dedicated per-machine config directory at `~/.config/tmux-pi/claude` so unrelated global skills, hooks, and plugins do not enter the workspace. Set `TP_CLAUDE_CONFIG_DIR` to choose another location. Sign in and settle folder trust once in that isolated profile using `bin/tp-claude-bootstrap /path/to/project`. The selected profile is used by main and Claude workers. In orchestrator mode, the Claude main pane uses Claude Code's built-in Bash spill-to-file at 10,000 output characters (rather than the default 30,000): Claude receives a preview and file path for longer successful output. tmux-pi workers keep the normal setting; this does not retroactively remove output already in Claude's context. Pi main instead offloads large tool results after use while preserving their raw session entries.

Neither main harness auto-loads a project's `AGENTS.md` or `CLAUDE.md`. Give workers the relevant project instructions explicitly in their assignment or selected context.

Local model services are loaded only when a worker is routed to them. Failure to start an optional local model does not prevent the workspace from opening.

## Dispatch and inspect

The main agent uses `tp-agent`, a shared command used by both harnesses. In Pi, `dispatch_role`, `task_index`, and `task_collect` are native bindings to it.

```sh
tp-agent dispatch --role coder --task "Fix the parser error" --context src/parser.py
tp-agent dispatch --role researcher --task "Find the cache invalidation rule" --deliverable "Source-backed note"
tp-agent list
tp-agent inspect <id>
tp-agent collect <id>
tp-agent retry <id> --reason "The first attempt misread the task" --worker pi --model openai-codex/gpt-6-sol
tp-agent retry <id> --reason "Two attempts failed" --escalate
tp-agent review <id>
tp-agent message <id> --text "Please check the empty input case"
tp-agent stop <id>
tp-agent reap [<id>...] [--yes]
tp-agent index
```

`collect` of a finished task (also `tp-contract collect`, which the legacy Claude-main MCP tools use) and `stop` end the worker's process (the pane and its scrollback stay). `retry` and `tp-review` (so `tp-agent review` and the MCP `review_subagent` too) refuse, naming the process, while another worker is alive on the same working tree (`--force-alive` overrides, except on a lane's working tree: see Lanes), and refuse when a live task's record, or the tmux pane list, cannot be read; `reap` lists collected or stopped tasks whose process is still alive and ends them with `--yes` (a settled task is ended by `collect` or `stop`, not `reap`); each refusal names only the commands that clear the state it describes. A worker's own `report.md` in the task directory is never overwritten by its final reply (`result.md`); `collect` and `inspect` show its path and size. `agent/extensions/subagents.ts` and `dispatcher.ts` (the old strict flow, loaded only under `TP_MODE=strict`, which nothing in `bin/` sets) are not covered by any of this.

A dispatch records the role, assignment, expected result and how the model was chosen. Use `--worker` (a constraint: the router still picks the model on that harness), `--model`, `--thinking`, and `--level KEY` to make an explicit choice; an explicit choice skips the router and is still recorded. `--after <id>` queues a task until that result is ready. The initial limits are two local-model workers and four active workers total, configurable in [config/orchestration.json](config/orchestration.json). Queued tasks start as slots and dependencies clear.

### Model choice

Without an explicit model, a catalog-driven router chooses. [config/model-catalog.json](config/model-catalog.json) lists the levels in order, each with the work it is for and not for, anchored examples, its models per provider (allowed efforts, context window), role floors and caps, the Qwen-qualified shapes and the router chain. Today: local Qwen, then Haiku or GPT-6 luna, then Sonnet or GPT-6 sol, then (hidden until rostered) GPT sol 6.1, then Opus or GPT-6 astra. Role floors are catalog data under `roles`: the coder, tester and researcher each have `"min_level": "mid", "strict_floor": true` (tester and researcher also `"max_level": "mid"`, so they run at exactly the mid level), so their work is never routed below Sonnet or GPT-6 sol (no Haiku, luna or local model; owner decision 2026-10-07 until Haiku is more capable). `strict_floor` means no step can cross the floor: not the router's pick, not the router-failed default, not an availability fallback (if no mid-or-higher model is available the route stops and asks rather than moving down), and not `--local-only` (private coder, tester or researcher work with no local model at that level stops and asks; drop `--local-only` or lower the floor). A machine-local redirect (`config/local.json` rules) to a model below the floor is dropped with the reason recorded, and with the catalog failing validation or unreadable a role gets only the configured fallback pair (`routes.coding`, `claude_preference.ordinary` in `config/router.json`), and only if the catalog file read leniently (the selected one, then the shipped one) places that model at or above the role's floor; any role rule that still has a `min_level` counts as strict there, even if `strict_floor` was lost, and a role no readable catalog names is not routed; otherwise the route stops and asks, never another model of the worker. If no catalog file can be read at all there is no floor to check and nothing is routed. An explicit `--model` from the orchestrator overrides the floor; an explicit `--level` lowers it only as far as the level asked for (effective floor is the lower of the role floor and that level): `--level small` on a coder resolves to Haiku or luna and never goes below small, while `--level mid` or higher keeps availability fallbacks at or above mid and stops and asks if none is available. The router's own pick, availability fallbacks, local-rule redirects, escalation and the catalog-error path stay held to it. A route has one lower bound, the highest of that effective role floor and the minimum level of any protected path the task names (`protected_paths`), which an explicit `--level` never lowers: every downward move (availability fallback, local-rule redirect, `--local-only` gate) stays at or above it or the route stops and asks, so `--level small` for a coder editing `lib/sensitive_read.py` runs at mid or stops, never Haiku; this applies to roles without `strict_floor` too (an architect on a protected path no longer falls to small when mid is unavailable). To lower the floor, edit `min_level` (and drop `strict_floor` to allow availability fallbacks downward). Adding a model or a level is a catalog edit only; `lib/router_catalog.py` validates it strictly and generates the router prompt from it.

`tp-agent dispatch` (and `bin/tp-route`, its command-line entry) builds a short fixed-form card from the role, the task's first sentence, its key constraint lines, the deliverable and, when the task says to read a brief, the head of that brief, and a small router model reads only the card (no tools, no repository, secret-shaped values replaced). The chain is Claude Haiku with thinking off, then GPT-6 luna, then local Qwen3.6, each with its own timeout; an invalid reply is retried once, then the next router answers. Qwen is called directly on the local server's chat-completions endpoint (not through pi), with temperature 0, a fixed seed and reasoning off, taking its URL and model from the same local configuration as the follow-up judge; the claude and pi CLIs have no temperature option, so the other two routers cannot be set to 0. A router that must not think (Haiku, Qwen) and reports thinking tokens fails that attempt: it is retried once, then the next router answers; a CLI that reports no count is accepted and the selection record notes it. The chain order, each router's `max_input_tokens`, and an optional `confirm` rule (ask the next router when this one picks above a level, or picks the local level for an unqualified shape) are data in the catalog's `router.chain`. A route decided by Qwen is capped at the mid level. If every router fails the dispatch falls back visibly to Sonnet at medium and records why. The router proposes a level and an effort; `lib/route_resolve.py` validates it, applies role floors and caps, finds an available model by provider order and context fit, and maps the effort onto what that model allows. The pair is always valid or the command stops and says the user must decide (for example `--local-only` work with no local model).

Explicit choices win only among valid ones: an unknown, hidden or unrostered model, an effort the model does not allow, a provider marked out, or a packet that does not fit is rejected with an error, and `--local-only` rejects any explicit model, worker or level that is not local (an escalation of a local-only task stays local or stops). Effort `medium` is the common case, `high` is preferred to moving up a level, and nothing above `high` is used unless the user approves it: `--thinking xhigh` is cut to `high` unless `--approve-effort` is given, which records the approval. The top level (Opus or GPT-6 astra) is rare and is reached only by an explicit request (`--model`, or `--level top`), by escalation, or by the router choosing it for a very large breadth of scope or work that needs a great deal of inference; security wording, length and risk never raise a level. `tp-agent retry <id> --escalate` is the escalation: after two failed attempts or two REQUEST_CHANGES reviews it raises the effort one step, and only at `high` moves up a level, then stops and asks. When a provider is out, the equivalent model on another provider is used; if only a lower level is left the work moves down and its review is forced to required.

Every selection (explicit ones included) writes a record to `<state>/evals/model-selections.jsonl`: role, task and card hashes, level, effort, who decided, the reason, the resolved model, any substitution and each router attempt, never the task text. `tp-route --check` validates the catalog and config; `<state>/provider-health.json` (`{"anthropic": {"status": "exhausted"}}`) marks a provider or `provider:model` as unavailable. Machine-local `rules` in `config/local.json` still redirect a choice, but never past a constraint: a redirect that breaks local-only, enters the restricted top level, names a hidden, unrostered or unlisted model, is above the effort ceiling (it is clamped), is on an unavailable provider or does not fit the packet is dropped, and the reason is recorded in the selection's steps.

### Reviews

`tp-agent review <id>` (and `bin/tp-review`) picks the reviewer through the same resolver: never the author's provider when another is available, GPT first for Claude- or Qwen-authored work, at least the mid level. Whether a review is needed is the larger of a deterministic policy (the independent maximum of role, files touched, level and effort: a read-only role still needs one at the top level or at high effort from mid up) and the router's review field recorded at dispatch; `--force` reviews regardless. A required review with no independent provider stops and asks; an optional one may use the author's provider and is marked `same_provider`. `--level`, `--thinking` and `--approve-effort` choose the reviewer explicitly.

Roles are defined under [agent/roles](agent/roles): coder, researcher, code reviewer, tester, vulnerability reviewer, data-flow reviewer, architect, and infrastructure. Role and harness are separate choices. The orchestrator instructions live in [agent/orchestrator.md](agent/orchestrator.md), with thin Pi and Claude Code overlays.

Each task has a local record under the project's ignored `.tmux-pi/sessions/<run-id>/tasks/<id>/`. The packet, metadata, state, result and transcript pointer remain inspectable. `tp-agent index` writes a short session index with the current objective, active work, pending results and artifact pointers. The main agent can recover from compaction or restart without loading all transcripts.

## Lanes

A lane is a git worktree and branch that one stream of work owns (`tp-agent lane create`, `tp-agent lanes`). Work goes into it with `tp-agent dispatch --lane NAME --step ID ...` (a lane that does not exist yet is created from `--domain` and `--touches`). Lanes need a `lanes` block in `config/orchestration.json`.

- **One writer per lane.** Every start on a lane's tree (`dispatch`, `tick`, `retry`, `tp-review`, and `tp-spawn --lane` called directly or by a worker's escalation) first takes the lane's lease, `<git common dir>/tmux-pi/lanes/NAME/lease.json`, which every session of the repository shares. A second writer is refused, naming the holder and the command that ends it; any number of reviewers may share the lane, but not with a writer. Starting anything in a lane's working tree without `--lane` (a nested repository in it included) is refused too.
- **Tags.** A lane task records `lane` and `lane_step`; `retry` and `tp-review` copy them, so a retry or a review stays in its lane and step. A lane task reads the repository's `.tmux-pi/worker-preamble.md` from the main checkout (the lane's checkout has none): `--preamble-from` names it, and `tp-spawn --lane` uses the main checkout when it is left out.
- **Escalation.** A lane worker that escalates (`agent/extensions/escalate.ts`) hands the lane to the stronger worker (`tp-spawn --lane-replaces`): both hold the lane until the first one ends, which it does right after the handoff.
- **`--force-alive`** (on `retry` and `review`) skips only the check of this session's tasks for a process on the tree. It does not apply to a lane: the lane's lease still refuses a second writer, or a reviewer beside a writer. To review or retry a lane task while its writer runs, end the writer: `tp-agent collect WRITER` if it has settled, else `tp-agent stop WRITER` (the refusal prints the command with the prefix that makes it work from any tmux session).

### Lane recovery

A holder is released when its process ends; nothing is released by age. Each refusal names its fix. In order of how often they happen:

| What you see | What it means | Clear it with |
| --- | --- | --- |
| `lane NAME already has a writer: TASK (writer, running, pid P, session S; …)` or `lane NAME is in use by a reviewer: TASK (reviewer, …)` | another task holds the lane. A reviewer shares the lane with other reviewers but excludes a writer | `running`: wait, or end it with the command the message prints. From another tmux session that command is `env -u TMUX -u TMUX_PANE -u TP_SESSION TP_STATE_DIR=S tp-agent stop TASK`, which works as printed in any session. The lane is free once the process is gone |
| the same with `settled: finished, not collected` | the holder has finished and its pane is still alive: waiting never frees the lane | `tp-agent collect TASK` (ends the process), or `tp-agent stop TASK`. `tp-agent reap` does not list a settled task |
| the same with `wait: TASK is still starting` | a `tp-spawn` is mid-start (a few seconds) | wait and dispatch again. If it is still there after a minute, nothing runs from it: the next dispatch drops it by itself |
| the same with `kill PID` or `tmux kill-pane -t %N` | a `tp-spawn` was killed after it made the pane; the pane still runs, so the lane stays held | run that command; the lane is free once the pane's process ends |
| `pane %N of TASK could not be ended; its task directory and lane NAME stay as they are` | a failed start could not remove its own pane | `tmux kill-pane -t %N`, then dispatch again |
| `TASK has no readable meta.json and a process of it is running` | a start was killed after it made its task directory, and its pane runs | the command in the message; a task directory with no `meta.json` and nothing running is only a warning (`rm -r DIR` when you no longer need it) |
| `lease ... is not valid JSON` / `not a tp-lane-lease:v1 lease` | the lease file is damaged and might hide a writer, so it is not used | check nothing runs on the lane (`tp-agent lanes`, `tmux list-panes -a`), then `rm` the file named in the message; the lane starts with no holder |
| `queue entry X.json cannot be read` | a queued item the lane cannot rule out as a holder | `tp-agent stop X` removes it (a file whose name is not a task id is deleted by hand: the message says so) |
| `TASK has no recorded working tree` / `cannot read TASK/meta.json` / `records an unreadable pane` | a task of this session whose record is damaged and that might be running | if it runs (`tmux list-panes -a`), end it: `tmux kill-pane -t %N`. Otherwise add `"workdir": "PATH"` to its `meta.json` (or fix the pane id) and dispatch again |
| `cannot read tmux panes` / `cannot read the process table` | tmux or `ps` could not be asked, so a writer cannot be ruled out | run `tp-agent` from inside the tmux session (`tmux list-panes -a` must work there) and dispatch again; it clears itself once the table reads |
| `lane NAME has an unreadable record PATH` | the lane's `lane.json` does not parse or belongs to another repository | `python3 -m json.tool PATH` names the broken line; fix it, comparing with a healthy `lane.json` of another lane. Or retire the lane (below) |
| `worktree W does not exist` / `is on X, expected BRANCH` / `is not registered with git` | the lane's tree was moved, deleted or switched by hand | `git -C W switch BRANCH` for a switched tree; for a missing or unregistered one retire the lane (below) and make a new one |
| `N lanes are open (limit M)` | `lanes.max_active_lanes` is reached | retire a lane you no longer need (below), or raise `lanes.max_active_lanes` in `config/orchestration.json` |
| `lane NAME is integrating` / `integrated` / `abandoned` | the lane takes no more work | dispatch into a new lane with another name |
| a malformed `lanes` block in `config/orchestration.json` or the repository's `lanes.json` (the message names the key and file) | the configuration cannot be used | set the named key to the value the message states, in the named file |
| `invalid lane name` / `needs --step` / `--lane chooses the working tree; remove --workdir` / `lane NAME does not exist; pass --domain ... --touches ...` / `already exists ... its declaration is set when it is created` / `--touches X is owned by the orchestrator` | a flag is missing, wrong or not allowed | run the same command with the flag change the message names |
| `lane tasks run the tests that check their change` | `--tests full` in a lane | pass `--tests affected` or `--tests none`; run the whole suite once from the main checkout |
| `X is inside lane NAME's working tree; pass --lane NAME --step ID` | a start in a lane's tree without the lane | repeat it with `--lane NAME --step ID` (or start it in the main checkout) |
| `waiting for lane NAME's writer TASK` (tick, queued) | not a refusal: the item starts when the writer is gone | nothing, or `tp-agent stop TASK` / `tp-agent stop ITEM` |

#### Retiring a lane by hand

`tp-agent lane remove` and `integrate` ship in a later slice. Until then, to drop a lane that is no longer wanted (this discards its unmerged work; look at `tp-agent lanes` first): end its tasks (`tp-agent stop ID`), then `git -C REPO worktree remove WORKTREE` (add `--force` only when you have kept what is in it), `git -C REPO branch -D BRANCH`, and `rm -r GIT_COMMON_DIR/tmux-pi/lanes/NAME`. REPO, WORKTREE and BRANCH are in the lane's row of `tp-agent lanes`; GIT_COMMON_DIR is `git -C REPO rev-parse --git-common-dir`.

## Codemode on the Pi main

The Pi orchestrator main (started through `bin/tp-main`, so `TP_MODE=orchestrator` with no task directory) loads Pi's built-in `codemode` tool: the model can run a short script that makes several tool calls and returns only the script's output. `bin/tp-pi` passes an explicit `--tools` list for that launch, because `--tools` replaces the whole selection; a new main tool must be added to `MAIN_TOOLS` there or it is silently dropped, which `tests/test_pi_launcher.py` checks.

Codemode on main is **not a read-only boundary**. Its scripts can call every tool in that list, including `bash`, `edit`, `write` and the dispatch tools. Nested calls still pass the sensitive-read check and are counted by the reporter. Workers, strict sessions and the `ask_local` child get no codemode.

The same launch passes `--no-mcp`. This does not change what the Pi main can reach: `--no-extensions` already disabled Pi's built-in MCP support for main and workers, so the servers in `agent/mcp.json` were not connected there before either. The flag keeps MCP tools out of codemode's reach if the extension flags change later. A legacy strict session (`TP_MODE=strict`) does not pass `--no-extensions` and still loads Pi's MCP support.

## Sensitive reads

A worker's supported read tools and common shell read/search commands ask before exposing a credential-bearing file or sensitive config. The permission appears in its live pane and in the status/task record; ordinary reads proceed without that prompt. A sensitive file selected for a context packet is omitted so the task can start, and the worker can request the specific information if needed.

With `redaction.mode` set to `on` in `config/local.json` (or `TP_REDACTION=on`), tool output from Claude Code and pi is also scrubbed: secrets become stable numbered placeholders, ambiguous tokens are reviewed by a local LM Studio model (`config/redaction.json`, fails closed), and only real credential stores prompt. See `docs/sensitive-read-redaction-design.md`.

In `on` mode an agent can use a secret without seeing it:
- A Bash command that contains a placeholder asks you once per placeholder per session. The real value is then filled in locally and never shown.
- Write content, or an Edit `new_string`, can carry placeholders from the same file. They are filled in silently.
- The `tmux-pi-safe` MCP server gives Claude `safe_read`/`safe_grep` (redacted, no prompt) for files the normal tools refuse. Its `reveal` tool asks every time before sending one real value to the model.
- pi's offloaded tool output can be reread without a prompt, because rereads are redacted too.
- Pending placeholder requests show in the status pane, and the session's placeholder store is deleted when Claude exits.

In `shadow` and `off` modes the older sensitive-read prompts above stay in charge.

The hook checks known paths and common commands. It does not provide an operating-system sandbox against arbitrary code, computed paths, or an agent that intentionally bypasses its tools. Use normal harness and machine permissions for stronger isolation.

Task records, prompts, and logs stay local. Remote model inference receives the context sent to the selected provider; no observability service is configured by tmux-pi.

## Folder trust

In a repository you know holds no credentials, the sensitive-read prompts for names that only *look* sensitive are noise. `tp-trust` lets you say so once per repository. A record states exactly this: *"this working tree holds no credentials"*. It is yours alone to write: `grant` and `revoke` need a real terminal and a random four-word code typed back from it, and they refuse inside an agent session (`TP_TASK_ID`, `TP_TASK_DIR` or `TP_MODE` set). Agents cannot touch the trust store either: a tool call that names `~/.config/tmux-pi/trust/` or `state/trust/`, or runs `tp-trust grant|revoke`, is denied (reading with `list`, `show`, `check`, `log` is allowed).

```
tp-trust grant ~/code/site --days 90 --note "static site, no secrets"   # repository root only
tp-trust list                      # id, root, verdict, grant and expiry, skips in the last 7 days
tp-trust check ~/code/site         # why a path is or is not trusted; exit 0 only when trusted
tp-trust show <path or id>         # the record file and the reader's verdict
tp-trust revoke ~/code/site        # or: tp-trust revoke <id from list>; also needs the typed code
tp-trust log -n 20                 # the skip log
```

`grant` prints the statement, the root, whether linked worktrees inherit, the expiry and a names-only scan of the tree, and warns when it finds credential-looking names (the statement would then be false, but you may still grant). Nothing is written unless you type the code. Granting again replaces the record. Records live in `~/.config/tmux-pi/trust/<id>.json` and are checked on every use: a record that is expired, edited, has the wrong owner or permissions, sits in a moved directory, or is not for a repository root is ignored, and `list` says why.

**What a record lifts.** Inside the trusted tree, when every path the call touches is in the tree: weak names (`*secret*` and `*credentials*` that are not on the exact-name list below, such as `notes_secret.txt`), the "directory that may hold credentials" guess for `agent`, `claude`, `config`, searches or copies rooted at the repository root, hidden searches, and git patch output (`git log -p`, `git diff`) when git's names-only listing shows nothing credential-bearing. Lifts after a directory guess depend on a walk of the real tree (names only, symbolic links not followed): any hit prompts and names the hit. One outside path keeps the whole call prompting.

**What still prompts, whatever the record says.**
- Strong credential names anywhere in the tree: `.env`, `.env.*`, `.envrc`, `.netrc`, `.npmrc`, `.pypirc`, `auth.json`, `.mcp.json`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.token`, `id_rsa*`, `*.local.*` (so `settings.local.json`), and exact credential file names: `credentials`, `credentials.json`, `secrets.json|yml|yaml|toml`, `*.tfvars`, `prod.env`.
- Credential directories and `.git` contents: `.aws`, `.ssh`, `.gnupg`, `.kube`, `.docker`, `.config/gcloud`, `.config/gh`, `.git/config`, `.git/credentials`. `grep -r foo .` still prompts because it reads `.git`; `rg foo .` skips it and does not. `rg --hidden` reads it and prompts.
- A symbolic link out of the tree, a nested repository or submodule (it needs its own record), a tree over the walk budget (20,000 entries or 1 second), a ripgrep config (`RIPGREP_CONFIG_PATH`: it can change what is searched), a git configuration that runs programs (filters, diff drivers), a stash patch, and anything outside the repository: your home directory, a parent of the repository, the temporary directory.
- Raw command text that names `~/.ssh` and the like, and everything `redact_hook` and the `tmux-pi-safe` tools decide. A record only touches `sensitive_read`.

**Worktrees and expiry.** Linked git worktrees of a trusted repository inherit its record after git itself confirms the link; `--no-worktrees` turns that off (a worktree holding credential directories or placed in your home or a temporary directory does not inherit). `--days N` (1-3650) makes the record lapse; without it the record never expires. When it lapses prompts return and `list` shows `ignored: expired`; `tp-trust grant` again renews it.

**Revoking.** `tp-trust revoke <path or id>` and type the code. Prompts return on the next call.

**The skip log.** Every call that a record let through writes one `trust_skip` line to `~/.config/tmux-pi/state/trust/skipped.jsonl` (rotated at 5 MB to `skipped.jsonl.1`): the time, harness, task, tool, record id, root, `via` (`root` or `worktree`), and for each lifted prompt its rule (`L1` weak name, `L2` directory guess, `L3` root or hidden search, `L4` git patch), target and the reason it would have prompted with. A record that was refused writes a `trust_ignored` line with the reason. `tp-trust log -n 50` tails it and `tp-trust list` shows each record's skips over seven days. Calls the guard refused are in `guard.jsonl` beside it. Review the log; if it shows reads you did not expect, revoke.

**Every prompt or refusal you can meet.** "Clears itself" means it stops without your action.

| You see | Clears itself? | What clears it |
|---|---|---|
| Sensitive-read prompt: `credential-bearing or sensitive configuration path` on a weak name (`notes_secret.txt`) | Only with a record | Approve it, or `tp-trust grant <repo root>` |
| Same prompt on a strong or exact credential name (`.env`, `prod.env`, `settings.local.json`, `credentials.json`) | No | Approve it for this read. A record never lifts it; move the file out of the tree or use `tmux-pi-safe` |
| `search root is the repository root (reaches repo/<path>) and may include credential files` | If the named path goes away | Search a narrower directory, use `rg` rather than `grep -r` to skip `.git`, remove or exclude the named path, or approve |
| `broad hidden/unignored search` | Only with a record and a clean tree | Drop `--hidden`/`--no-ignore`, or approve |
| `search root is a directory known to hold credentials` (`config`, `agent`, `claude`) | Only with a record and a clean walk | `tp-trust grant`, or search a narrower directory |
| `sensitive path in shell command` (`~/.ssh`, `.aws/credentials`) | No | Approve, or leave the path out |
| `tree too large to verify` or `follows a link out of the tree at ...` | No | Search a smaller directory, or remove the link |
| `unscoped patch output may include credential files`, `patch output covers a credential-bearing path`, `stash contents ...` | Only with a record (not for stash or submodules) | Limit the patch with a path (`git log -p -- src/`), or `tp-trust grant` |
| `search root is a home directory` / `a parent directory` / `a directory outside the working tree` | No | Search inside the repository, or approve |
| `command nesting is too deep to verify`, `unparsed tool call`, `the call could not be analysed` | No | Split the call into simpler ones, or approve |
| Guard: `tmux-pi trust store guard denied this tool call: ... What to do instead: ...` | No (a denial, not a prompt) | Leave the trust store out of the call. Only you grant or revoke, in a terminal |
| Guard: `... could not check this tool call and needs your approval` | No | Approve, or give a narrower directory or fewer paths |
| `tp-trust: TP_TASK_ID is set: this looks like an agent session` | No | Run `tp-trust` yourself in a terminal outside the agent session |
| `tp-trust: stdin and stdout must be a terminal` / `no controlling terminal` | No | Run it in a real terminal; not through a pipe, script or agent tool |
| `code did not match; nothing written` (or `nothing removed`) | No | Run the command again and type the new code exactly |
| `<path> is not a git repository root` / `is inside the repository at <root>; name its root` | No | Name the repository root (`git rev-parse --show-toplevel`) |
| `<path> is a symbolic link; name the real directory` | No | Name the real directory |
| `<root> is your home directory or a parent of it`, `inside a temporary directory`, `inside a known credential location`, `a parent of the config directory` | No | Trust the project directory itself, not a parent, and keep it outside `/tmp` and `~/.config` |
| `--days must be between 1 and 3650` / `--note must be printable ASCII of at most 200 characters` | No | Correct the option |
| `the directory changed while waiting; nothing written` | No | Run `tp-trust grant` again |
| `tp-trust list` shows `ignored: <reason>` (`expired`, `bad permissions`, `wrong owner`, `root moved`, `name does not match root`, `trust directory inside a repository` ...) | No | `tp-trust grant <root>` replaces it, or `tp-trust revoke <id>` removes it, then check with `tp-trust check <root>` |
| `NOT trusted: no trust record for this repository` / `the record excludes linked worktrees` | No | `tp-trust grant <main root>` (without `--no-worktrees` for worktrees) |

The status pane (top right) lists workers live. Click it to focus it, then:

| Key | Action |
|---|---|
| `j` / `k`, `↑` / `↓` | move |
| `enter` | show the selected worker in the viewer |
| `o` | show it and move focus into it, to type to it |
| `-` | clear the viewer |
| `r` / `t` | read its last reply / its context packet in a popup |
| `x` | kill it (asks first) |
| `c` | archive killed workers |

States come from each worker's reporter: `starting`, `working`, `tool`, `waiting`, `permission`, `error`, then `done`, `failed:<code>` or `killed` once the process ends. A second line shows elapsed time and turns, and settled work shows its `result.md` path; a reply with no `RESULT_STATUS:` line shows as `paused`, not settled, and is listed under `paused` in the session index. The line is `RESULT_STATUS: complete` (or `empty`, `blocked`, `incomplete`) on a line of its own; Markdown around it (`### `, `**…**`, backticks, a list marker) is accepted, any other text before it on the line is not. One reader (`lib/result_status.py`) serves `collect`, `tp-review`, the verdict reader and the dispatcher; the paused notice from `collect` states this form and the `tp-agent message` command that asks the worker for it. Token counts include cached input; cost is shown only for Pi providers that price tokens. If the pane looks stale, see `/refresh-status` above.

## Claude as the main agent

`bin/tp --main claude` uses the shared `tp-agent` orchestrator and its Claude prompt overlay; it does **not** expose the separate subagent MCP tools. The older `TP_MAIN=claude bin/tp` path instead appends `claude/main-prompt.md` and exposes `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `review_subagent`, `kill_subagent`, `find_skill`, and `get_skill` through `claude/mcp/subagents.mjs`. Those tools are not exposed inside workers. The tmux-pi plugin adds `/make-qa`, `/counterpoint`, `/product-fit`, `/threat-model`, `/reset`, and `/end` prompt aliases.

The plugin in `claude/plugins/tmux-pi` also loads a main-only module, `hooks/register.tsx`, beside its command hooks. It is active only in an interactive orchestrator main (`TP_TASK_DIR` unset, `TP_ROOT` set, `TP_MODE=orchestrator`); workers, headless runs and legacy mains load it and it does nothing. When active it:

- injects the task index at session start and again after a compaction, so the main no longer runs `tp-agent index` by hand (the injection uses `tp-agent index --no-tick`, which only reads and never launches queued work);
- polls `tp-agent list` every five seconds and, when a worker becomes `needs_input`, `settled`, `paused` (a reply with no result status, or a budget pause) or `failed`, shows a toast and sends one wake-up message naming the task; tasks already waiting when the session starts are the baseline and wake nobody;
- draws one line above the prompt with worker counts and the ids that need input;
- adds `/workers`, `/worker-view <id>|-` and `/worker-collect <id>`, which run without a model turn (named `worker…` because the engine refuses a built-in's name such as `/tasks`, and `/tp` is the built-in alias of `/teleport`).

Sensitive-read enforcement, `report.sh`, the status pane and `tp-agent` are unchanged and stay the shared contract for both harnesses; the Pi main has no equivalent notifier and polls with `task_index`. Check the module with `claude plugin validate claude/plugins/tmux-pi` and `claude plugin test claude/plugins/tmux-pi`.

`claude/mcp.json` also defines YouTrack, airteracode, and optional database integrations; `agent/mcp.json` defines Pi integrations. Availability depends on installed services and local configuration. HTTP auth headers come from the git-ignored `claude/mcp.local.env`, sourced by the launchers when present. Only the legacy subagent MCP tools are pre-allowed; other Claude MCP calls still prompt for permission.

## Validation and legacy path

Run the repository tests with `python3 -m unittest discover -s tests -p 'test_*.py'` and the extension type check with `tests/check_dispatcher_types.sh`, which covers every extension under `agent/extensions`. The Node runtime harnesses in `tests/*.mjs` run through that discovery and need Node with `--experimental-strip-types`. Some tests bind loopback sockets. `tests/run_parallel.py -j2` is an optional faster run: it gives each test module its own interpreter, runs at most four at once, and keeps the pseudo-terminal and loopback-server modules serial. The serial command stays the reference, so rerun it before trusting a parallel-only failure or pass. The old strict dispatcher and related commands remain in the repo for reference and rollback, but `bin/tp` does not load that flow. Its former user guide is preserved in [docs/legacy-workflow.md](docs/legacy-workflow.md).

The product requirements and acceptance scenarios are in [docs/agent-development-environment-prd.md](docs/agent-development-environment-prd.md).
