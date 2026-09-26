# tmux-pi plan

## Goal

One workspace where a main pi agent runs the work and every subagent is a real
pi or Claude Code session you can watch or take over. A local model decides which model and
effort each task gets, and frontier models receive only the context packet built
for their task.

## Layout

```
┌──────────────────────────────┬──────────────────────────────┐
│ main pi + orchestrator ext   │ status: subagents, model,    │
│                              │ state, current tool, cost    │
│                              ├──────────────────────────────┤
│                              │ viewer: selected subagent's  │
│                              │ live session (swapped in)    │
└──────────────────────────────┴──────────────────────────────┘
```

Each subagent runs in its own hidden tmux window. Viewing one swaps its pane
into the viewer slot, so what you see is the real session and you can type into it.

## Components

| Piece | What it does |
|---|---|
| `bin/tp` | Creates the tmux session and three slots, records pane ids as `@tp_*` session options. Linked as `agent`. |
| `bin/tp-pi` | Runs pi with only tmux-pi's config and skills. Used for main and pi workers. |
| `bin/tp-claude` | Runs Claude Code with only tmux-pi's settings, plugins and MCP servers. Used for Claude workers. |
| `agent/extensions/orchestrator.ts` | Loaded into main pi. Tools: `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `kill_subagent`. |
| Router | Called by the orchestrator before a spawn. Local model returns `{class, model, thinking, tools, context_hints}` as JSON, validated against `config/models.json`. |
| Context builder | Writes `.tmux-pi/tasks/<id>/packet.md`: the task plus only the selected files, snippets and notes. Subagents start with `--no-context-files @packet.md`. |
| Reporter | pi workers: `agent/extensions/reporter.ts`; Claude workers: hooks in `claude/settings.json`. Both write `.tmux-pi/state/<id>.json` (state, current tool, tokens, cost) and a final `result.md`. |
| Status pane | Reads the state dir and lists subagents. Selecting one runs `tp-view <id>` (a tmux `swap-pane` into the viewer slot). |

## Self-contained config

tmux-pi never loads your personal pi or Claude Code setup. Everything a worker
can use lives in this repo:

```
skills/            shared by both harnesses (SKILL.md format)
agent/             pi agent dir (PI_CODING_AGENT_DIR)
  settings.json  models.json  extensions/  prompts/  themes/
  auth.json  sessions/                     (git-ignored; created by pi)
claude/            Claude Code worker config
  settings.json  mcp.json
  plugins/tmux-pi/ plugin; skills -> ../../../skills
```

**pi (`bin/tp-pi`):** `PI_CODING_AGENT_DIR=agent/` replaces `~/.pi/agent`, and
`--no-skills --skill skills/` stops discovery of `~/.agents/skills` and project
skill dirs. Log in once inside tmux-pi (`/login`) to create `agent/auth.json`.

**Claude Code (`bin/tp-claude`):** `--setting-sources ""` skips user, project and
local settings, so no `~/.claude` plugins, skills, hooks or MCP servers, and no
project `.claude/` or CLAUDE.md. Settings, plugins and MCP servers come only from
`claude/` via `--settings`, `--plugin-dir` and `--strict-mcp-config --mcp-config`.
The normal Claude login is used, so the subscription works. Claude Code's
built-in skills (e.g. `code-review`, `simplify`) and built-in plugins
(`agents-md`, `telemetry`) still load.

Verified 2026-09-26 (pi 0.87.1, Claude Code 2.1.283): a probe skill in `skills/`
loaded in both harnesses; no user skills, plugins or MCP servers loaded; a
project CLAUDE.md was not read by the Claude worker.

The target project's `AGENTS.md` still reaches the main pi agent; workers skip
context files and get their packet instead.

State lives in `<workdir>/.tmux-pi/` (git-ignored). Tasks, packets, state and
results are plain files, so any piece can be inspected or replaced.

## Milestones

- **M0 — skeleton.** Repo, `bin/tp` (`agent`), three-slot layout, self-contained pi and Claude config. *(done)*
- **M1 — one subagent, by hand.** `tp-spawn --worker pi|claude --model X --thinking Y "task"` opens a hidden window running `tp-pi` or `tp-claude`; `tp-view <id>` swaps it into the viewer.
- **M2 — status.** Reporter extension plus a real status pane with keyboard selection.
- **M3 — router.** Local model classifies the task and picks model and thinking level from a roster.
- **M4 — context packets.** Orchestrator builds per-task packets; subagents get nothing else.
- **M5 — review loop.** A second subagent on a different provider reviews the first one's result before it returns to main.

## Models available now

- **Local (Ollama, `:11434`):** gpt-oss 20b/120b, qwen3-coder 30b, qwen3.6 35b, llama3.3 70b, qwen3 1.7b.
- **Local (slotstream):** Qwen3.8 Flash Next, OpenAI-compatible. Defaults to `:11434` too, so run it on `--port 11435` (what `agent/models.json` expects).
- **GPT:** pi's `openai-codex` provider (ChatGPT subscription); needs `/login` inside tmux-pi.
- **Claude:** Claude Code workers via `bin/tp-claude`, on the Claude subscription.

## Decisions

- Private GitHub repo `vandiverj/tmux-pi`.
- pi for the main agent and default workers; Claude Code as a worker type, so Claude runs on the subscription in Anthropic's own client.
- Self-contained: all config, extensions, plugins and skills live in this repo; nothing from `~/.pi`, `~/.agents` or `~/.claude`.
- TypeScript for extensions and tools; bash for thin tmux wrappers.
- Started clean rather than extending `pi-improvement-workbench`; borrow from it only where it helps.

## Open questions

- **OpenJEV:** where it runs and what API it exposes. Blocks M3 only; M3 can fall back to slotstream Qwen.
- **Main agent model:** local or frontier. Recommended: frontier at low thinking for main, since it writes every packet.

## Risks

- **Claude built-ins can't be removed.** `--disable-slash-commands` would drop them but also drops tmux-pi's skills. Denying specific ones via `claude/settings.json` is untested.
- **Port clash** between Ollama and slotstream on `11434`.
