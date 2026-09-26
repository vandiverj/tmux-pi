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

## Subagents

Inside a tmux-pi session the `tp-*` commands are on PATH (for you and the agents):

```sh
tp-spawn -w claude -m sonnet -t high "Refactor the auth module"   # prints an id
tp-spawn -m slotstream/qwen3.8-flash-next:4bit -f task.md -v       # pi worker, view now
tp-list            # subagents, state, tokens, cost, current tool
tp-view <id>       # swap its live session into the viewer (click in to type)
tp-view -          # put the placeholder back
tp-kill <id>       # stop it; task files stay in .tmux-pi/tasks/<id>/
tp-clean           # archive killed subagents to .tmux-pi/archive/
```

The status pane (top right) is the same list, live. Click it, then:

| Key | Action |
|---|---|
| `j` / `k`, `↑` / `↓` | move |
| `enter` | show the selected subagent in the viewer |
| `o` | show it and move focus into it, to type to it |
| `-` | clear the viewer |
| `r` / `t` | read its last reply / its task in a popup |
| `x` | kill it (asks first) |
| `c` | archive killed subagents |

States come from each worker's reporter: `starting`, `working`, `tool` (the tool and
its argument show below the row), `waiting` (finished its turn, ready for more),
`permission` (Claude is asking to use a tool), `error`. Once the process ends:
`done`, `failed:<code>` or `killed`.

Workers skip context files: the task text is their only context. Each task dir holds
`task.md`, `meta.json`, the generated `run.sh`, and the reporter's `state.json`
and `result.md` (the last reply). pi workers report through
`agent/extensions/reporter.ts`, Claude workers through the tmux-pi plugin's hooks
(`claude/plugins/tmux-pi/hooks/`). Token counts include cached input; cost is shown
only for pi providers that price tokens.

See [docs/plan.md](docs/plan.md) for the design and milestones.
