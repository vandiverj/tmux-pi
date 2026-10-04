# Pi mechanics

Use `dispatch_role`, `task_index`, and `task_collect` for ordinary orchestration. They call the shared repository command `tp-agent`. Use `bash` for `tp-agent inspect ID`, `tp-agent message ID --text TEXT`, and `tp-agent stop ID` when needed. Older subagent tools remain available for existing sessions but do not record a role. The tmux status pane and viewer show the same tasks.
