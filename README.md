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
tp-agent retry <id> --reason "Classifier was unavailable; try a different model" --worker pi --model openai-codex/gpt-6-sol
tp-agent message <id> --text "Please check the empty input case"
tp-agent stop <id>
tp-agent index
```

A dispatch records the role, assignment, expected result, model recommendation and actual selection. Use `--worker`, `--model`, and `--thinking` to make an explicit choice. Without a model, the local classifier recommends a route; if it is unavailable, dispatch falls back visibly to a configured capable model. `--after <id>` queues a task until that result is ready. The initial limits are two local-model workers and four active workers total, configurable in [config/orchestration.json](config/orchestration.json). Queued tasks start as slots and dependencies clear.

Roles are defined under [agent/roles](agent/roles): coder, researcher, code reviewer, tester, vulnerability reviewer, data-flow reviewer, architect, and infrastructure. Role and harness are separate choices. The orchestrator instructions live in [agent/orchestrator.md](agent/orchestrator.md), with thin Pi and Claude Code overlays.

Each task has a local record under the project's ignored `.tmux-pi/sessions/<run-id>/tasks/<id>/`. The packet, metadata, state, result and transcript pointer remain inspectable. `tp-agent index` writes a short session index with the current objective, active work, pending results and artifact pointers. The main agent can recover from compaction or restart without loading all transcripts.

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

## Status pane

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

States come from each worker's reporter: `starting`, `working`, `tool`, `waiting`, `permission`, `error`, then `done`, `failed:<code>` or `killed` once the process ends. A second line shows elapsed time and turns, and settled work shows its `result.md` path. Token counts include cached input; cost is shown only for Pi providers that price tokens. If the pane looks stale, see `/refresh-status` above.

## Claude as the main agent

`bin/tp --main claude` uses the shared `tp-agent` orchestrator and its Claude prompt overlay; it does **not** expose the separate subagent MCP tools. The older `TP_MAIN=claude bin/tp` path instead appends `claude/main-prompt.md` and exposes `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `review_subagent`, `kill_subagent`, `find_skill`, and `get_skill` through `claude/mcp/subagents.mjs`. Those tools are not exposed inside workers. The tmux-pi plugin adds `/make-qa`, `/counterpoint`, `/product-fit`, `/threat-model`, `/reset`, and `/end` prompt aliases.

The plugin in `claude/plugins/tmux-pi` also loads a main-only module, `hooks/register.tsx`, beside its command hooks. It is active only in an interactive orchestrator main (`TP_TASK_DIR` unset, `TP_ROOT` set, `TP_MODE=orchestrator`); workers, headless runs and legacy mains load it and it does nothing. When active it:

- injects the task index at session start and again after a compaction, so the main no longer runs `tp-agent index` by hand (the injection uses `tp-agent index --no-tick`, which only reads and never launches queued work);
- polls `tp-agent list` every five seconds and, when a worker becomes `needs_input`, `settled` or `failed`, shows a toast and sends one wake-up message naming the task; tasks already waiting when the session starts are the baseline and wake nobody;
- draws one line above the prompt with worker counts and the ids that need input;
- adds `/workers`, `/worker-view <id>|-` and `/worker-collect <id>`, which run without a model turn (named `worker…` because the engine refuses a built-in's name such as `/tasks`, and `/tp` is the built-in alias of `/teleport`).

Sensitive-read enforcement, `report.sh`, the status pane and `tp-agent` are unchanged and stay the shared contract for both harnesses; the Pi main has no equivalent notifier and polls with `task_index`. Check the module with `claude plugin validate claude/plugins/tmux-pi` and `claude plugin test claude/plugins/tmux-pi`.

`claude/mcp.json` also defines YouTrack, airteracode, and optional database integrations; `agent/mcp.json` defines Pi integrations. Availability depends on installed services and local configuration. HTTP auth headers come from the git-ignored `claude/mcp.local.env`, sourced by the launchers when present. Only the legacy subagent MCP tools are pre-allowed; other Claude MCP calls still prompt for permission.

## Validation and legacy path

Run the repository tests with `python3 -m unittest discover -s tests -p 'test_*.py'` and the extension type check with `tests/check_dispatcher_types.sh`, which covers every extension under `agent/extensions`. The Node runtime harnesses in `tests/*.mjs` run through that discovery and need Node with `--experimental-strip-types`. Some tests bind loopback sockets. `tests/run_parallel.py -j2` is an optional faster run: it gives each test module its own interpreter, runs at most four at once, and keeps the pseudo-terminal and loopback-server modules serial. The serial command stays the reference, so rerun it before trusting a parallel-only failure or pass. The old strict dispatcher and related commands remain in the repo for reference and rollback, but `bin/tp` does not load that flow. Its former user guide is preserved in [docs/legacy-workflow.md](docs/legacy-workflow.md).

The product requirements and acceptance scenarios are in [docs/agent-development-environment-prd.md](docs/agent-development-environment-prd.md).
