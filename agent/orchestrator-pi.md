# Pi mechanics

Use `dispatch_role`, `task_index`, and `task_collect` for ordinary orchestration. They call the shared repository command `tp-agent`. Use `bash` for `tp-agent inspect ID`, `tp-agent message ID --text TEXT`, and `tp-agent stop ID` when needed. Older subagent tools remain available for existing sessions but do not record a role. The tmux status pane and viewer show the same tasks.

Run `task_index` at session start and after compaction; nothing notifies you when a worker changes state, so check it before reporting on running work.

`tp-agent context --task TEXT [--workdir DIR]` (through `bash`) suggests `context` specs for a task. They are suggestions: prefer a file you already know is right, and `tp-spawn` still caps and screens each one.
