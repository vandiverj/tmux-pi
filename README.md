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

Re-running `tp` reattaches to the existing session. Set `TP_SESSION` to run more than one.

pi runs with this repo's `agent/` directory as its config (`PI_CODING_AGENT_DIR`), so nothing
from `~/.pi/agent` is loaded. Run `/login` once inside the workspace for any subscription providers.

See [docs/plan.md](docs/plan.md) for the design and milestones.
