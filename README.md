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

See [docs/plan.md](docs/plan.md) for the design and milestones.
