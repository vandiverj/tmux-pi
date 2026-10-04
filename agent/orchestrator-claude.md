# Claude Code mechanics

Use the Bash tool to run `tp-agent` commands. For a worker assignment, use `tp-agent dispatch --role ROLE --task "TASK"`; add `--context PATH[:START-END]` and `--note TEXT` when useful. `tp-agent list`, `tp-agent inspect ID`, `tp-agent collect ID`, `tp-agent message ID --text TEXT`, and `tp-agent stop ID` operate on the same task records as the Pi main agent. Do not launch `claude` or `pi` workers directly.
