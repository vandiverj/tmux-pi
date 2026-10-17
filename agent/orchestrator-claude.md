# Claude Code mechanics

Use the Bash tool to run `tp-agent` commands. For a worker assignment, use `tp-agent dispatch --role ROLE --task "TASK"`; add `--context PATH[:START-END]` and `--note TEXT` when useful. Add `--worker`, `--model`, `--thinking`, `--level KEY`, `--local-only` or `--approve-effort` only to make an explicit choice. `tp-agent retry ID --reason TEXT [--escalate]`, `tp-agent review ID [--force]`, `tp-agent list`, `tp-agent inspect ID`, `tp-agent collect ID`, `tp-agent message ID --text TEXT`, and `tp-agent stop ID` operate on the same task records as the Pi main agent. Do not launch `claude` or `pi` workers directly.

The task index is injected at session start and after compaction; run `tp-agent index` only if it is missing. When a worker settles, fails, or needs input, a message from the tmux-pi plugin starts a turn naming the task and the command to run; a worker waiting at a permission prompt needs the user, so say so and do not answer it yourself.

`tp-agent context --task TEXT [--workdir DIR]` suggests `--context` specs for a task. They are suggestions: prefer a file you already know is right, and `tp-spawn` still caps and screens each one.


## Compact Instructions

When compacting, preserve active user instructions and corrections accurately, plus the current objective and unresolved decisions. Keep task IDs, status, and artifact paths as pointers, not their contents. Omit tool output and artifact text that can be re-fetched. After compaction the task index is injected again; retrieve only what the next step needs. If a workspace is attached, keep the attached workspace id and any open decision ids; before a manual /compact run `tp-agent work checkpoint --summary-file FILE` (FILE holds a short note of what you are in the middle of). After compaction run `tp-agent work recover`.
