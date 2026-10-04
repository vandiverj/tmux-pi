# tmux-pi

A local tmux workspace for a main agent that coordinates visible specialist workers. Pi or Claude Code can sit in the main pane. Workers run in their own tmux windows, and the status and viewer panes let you inspect or enter a live worker session.

The normal path is deliberately light: the main agent answers simple questions, delegates substantial work by role, checks evidence, and chooses the next action. There is no required contract, stage sequence, or universal review gate.

## Start

Requirements: tmux 3.3+, Pi or Claude Code for the chosen main harness, Python 3, and jq. Install the worker harnesses and model services you intend to use.

```sh
bin/tp --main pi /path/to/project
bin/tp --main claude /path/to/project
```

The default main harness is Pi. With no directory argument, `agent` (or `tp`) uses the current directory. Every launch starts a fresh workspace with an empty subagent list and its own `.tmux-pi/sessions/<run-id>/` records. Existing runs and their history remain available. Use `agent --resume` to attach to the most recently created running workspace for the current project, or `TP_SESSION=<name> agent --resume` for a specific run. `TP_SESSION` can also name a new run, but an existing name requires `--resume`. A stopped run cannot be reattached; its records can be read with `bin/tp-task --state-dir /path/to/project/.tmux-pi/sessions/<run-id> list`. Older project-level records remain at `.tmux-pi/` and can be read the same way. Outside tmux, set `TP_SESSION` when targeting a running workspace with other `tp-*` commands. The main pane is left, the status pane is above the viewer on the right. Select a worker in status and press Enter to view it; press `o` to enter its live pane. `r` and `t` show its result and packet.

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

## Sensitive reads

A worker's supported read tools and common shell read/search commands ask before exposing a credential-bearing file or sensitive config. The permission appears in its live pane and in the status/task record; ordinary reads proceed without that prompt. A sensitive file selected for a context packet is omitted so the task can start, and the worker can request the specific information if needed.

The hook checks known paths and common commands. It does not provide an operating-system sandbox against arbitrary code, computed paths, or an agent that intentionally bypasses its tools. Use normal harness and machine permissions for stronger isolation.

Task records, prompts, and logs stay local. Remote model inference receives the context sent to the selected provider; no observability service is configured by tmux-pi.

## Validation and legacy path

Run the repository tests with `python3 -m unittest discover -s tests -p 'test_*.py'` and the extension type check with `tests/check_dispatcher_types.sh`. Some tests bind loopback sockets. The old strict dispatcher and related commands remain in the repo for reference and rollback, but `bin/tp` does not load that flow. Its former user guide is preserved in [docs/legacy-workflow.md](docs/legacy-workflow.md).

The product requirements and acceptance scenarios are in [docs/agent-development-environment-prd.md](docs/agent-development-environment-prd.md).
