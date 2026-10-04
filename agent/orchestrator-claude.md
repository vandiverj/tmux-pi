# Claude Code mechanics

Use the Bash tool to run `tp-agent` commands. For a worker assignment, use `tp-agent dispatch --role ROLE --task "TASK"`; add `--context PATH[:START-END]` and `--note TEXT` when useful. `tp-agent list`, `tp-agent inspect ID`, `tp-agent collect ID`, `tp-agent message ID --text TEXT`, and `tp-agent stop ID` operate on the same task records as the Pi main agent. Do not launch `claude` or `pi` workers directly.


## Compact Instructions

When compacting, preserve active user instructions and corrections accurately, plus the current objective and unresolved decisions. Keep task IDs, status, and artifact paths as pointers, not their contents. Omit tool output and artifact text that can be re-fetched. After compaction run `tp-agent index` and retrieve only what the next step needs.
