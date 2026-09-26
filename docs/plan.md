# tmux-pi plan

## Goal

One workspace where a main pi agent runs the work and every subagent is a real
pi session you can watch or take over. A local model decides which model and
effort each task gets, and frontier models receive only the context packet built
for their task.

## Layout

```
┌──────────────────────────────┬──────────────────────────────┐
│ main pi + orchestrator ext   │ status: subagents, model,    │
│                              │ state, current tool, cost    │
│                              ├──────────────────────────────┤
│                              │ viewer: selected subagent's  │
│                              │ live pi session (swapped in) │
└──────────────────────────────┴──────────────────────────────┘
```

Each subagent runs in its own hidden tmux window. Viewing one swaps its pane
into the viewer slot, so what you see is the real session and you can type into it.

## Components

| Piece | What it does |
|---|---|
| `bin/tp` | Creates the tmux session and three slots, records pane ids as `@tp_*` session options. |
| `agent/extensions/orchestrator.ts` | Loaded into main pi. Tools: `spawn_subagent`, `list_subagents`, `send_to_subagent`, `collect_result`, `kill_subagent`. |
| Router | Called by the orchestrator before a spawn. Local model returns `{class, model, thinking, tools, context_hints}` as JSON, validated against `config/models.json`. |
| Context builder | Writes `.tmux-pi/tasks/<id>/packet.md`: the task plus only the selected files, snippets and notes. Subagents start with `--no-context-files @packet.md`. |
| `agent/extensions/reporter.ts` | Loaded into every subagent. Writes `.tmux-pi/state/<id>.json` (state, current tool, tokens, cost) and a final `result.md`. |
| Status pane | Reads the state dir and lists subagents. Selecting one runs `tp-view <id>` (a tmux `swap-pane` into the viewer slot). |

## Self-contained pi config

tmux-pi never loads `~/.pi/agent`. `tp` sets `PI_CODING_AGENT_DIR` to the repo's
`agent/` directory for the whole tmux session, so main and every subagent read
only what this repo ships:

```
agent/
  settings.json   models.json   (committed)
  extensions/  skills/  prompts/  themes/   (committed)
  auth.json  sessions/          (git-ignored; created by pi)
```

Log in once inside a tmux-pi session (`/login`) to create `agent/auth.json`.
The target project's own `.pi/` and `AGENTS.md` files still apply when trusted;
subagents skip them with `--no-context-files` and get their packet instead.

State lives in `<workdir>/.tmux-pi/` (git-ignored). Tasks, packets, state and
results are plain files, so any piece can be inspected or replaced.

## Milestones

- **M0 — skeleton.** Repo, `bin/tp`, three-slot layout, repo-owned agent dir. *(done)*
- **M1 — one subagent, by hand.** `tp-spawn --model X --thinking Y "task"` opens a hidden window running pi; `tp-view <id>` swaps it into the viewer.
- **M2 — status.** Reporter extension plus a real status pane with keyboard selection.
- **M3 — router.** Local model classifies the task and picks model and thinking level from a roster.
- **M4 — context packets.** Orchestrator builds per-task packets; subagents get nothing else.
- **M5 — review loop.** A second subagent on a different provider reviews the first one's result before it returns to main.

## Models available now

- **Local (Ollama, `:11434`):** gpt-oss 20b/120b, qwen3-coder 30b, qwen3.6 35b, llama3.3 70b, qwen3 1.7b.
- **Local (slotstream):** Qwen3.8 Flash Next, OpenAI-compatible. Defaults to `:11434` too, so run it on `--port 11435` (what `agent/models.json` expects).
- **GPT:** pi's `openai-codex` provider (ChatGPT subscription); needs `/login` inside tmux-pi.
- **Claude:** planned via subscription login (see risks).

## Decisions

- Private GitHub repo `vandiverj/tmux-pi`.
- One harness: pi for main and all subagents.
- Self-contained: all pi config, extensions and skills live in this repo, not `~/.pi/agent`.
- TypeScript for extensions and tools; bash for thin tmux wrappers.
- Started clean rather than extending `pi-improvement-workbench`; borrow from it only where it helps.

## Open questions

- **OpenJEV:** where it runs and what API it exposes. Blocks M3 only; M3 can fall back to slotstream Qwen.
- **Main agent model:** local or frontier. Recommended: frontier at low thinking for main, since it writes every packet.

## Risks

- **Claude subscription in a third-party harness.** Anthropic has restricted using Pro/Max logins outside its own apps. Check current terms before relying on it; an API key is the safe fallback, and nothing in M0–M2 depends on it.
- **Port clash** between Ollama and slotstream on `11434`.
