# Task lifecycle notes (frozen excerpt for tp-bench)

A tmux-pi worker task is always in exactly one lifecycle state.

## Lifecycle states

- starting
- working
- needs_input
- paused
- settled
- completed
- failed
- killed
- stopped
- escalated

## Terminal states

A runner that polls a task treats settled, completed, paused, failed, killed, stopped, escalated and needs_input as the end of
the wait. Only starting and working mean the worker is still busy.

## Reviews

A reviewer reply starts with APPROVED, APPROVED_WITH_NOTES or REQUEST_CHANGES. Each finding is numbered F1, F2 and so on, and ends
with a line `Closes by: code`, `Closes by: check`, `Closes by: decision` or `Closes by: none`.
