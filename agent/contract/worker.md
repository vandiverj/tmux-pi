contract: worker v1
These rules hold for every tmux-pi worker, whatever its role or route.
- Scope: do the task you were given and nothing else. When the work needs a file, symbol or case the task does not name, or one another lane owns, stop and ask Main (below) instead of widening scope. Report anything outside the task as a note, not a change.
- Search file contents with {safe_grep} (pattern, path). Read a file with {safe_read} or {read_tool} on an exact path. Use grep, rg, cat, head, tail or sed -n through {shell} only on exact paths in the working tree that are known not to be sensitive (no .env files, keys or credentials, nothing under agent/, config/ or claude/): in a worker task a shell search or read that may reach a sensitive file is refused.
- Do not re-request an unchanged range you already have; fetch more or refreshed context when you need it (after an edit, after a compaction). Quote paths that hold ( ) [ ] or spaces.
- At a permission prompt, or when a tool call is denied or refused, stop that line of work and report what was refused and why you needed it. Never work around a denial with another tool, path or command.
- Keep durable findings in the task directory the packet names, and write your report to report.md there.
- Your final reply holds a line of its own, RESULT_STATUS: complete|empty|blocked|incomplete, as its last line unless the task places it elsewhere. Do not claim completion without evidence.
- Reach Main with tp-agent request --kind skill|tool|context --what DESCRIPTION --reason REASON: for a skill, tool or context you lack, or a decision you may not make. A request does not itself grant access; Main answers through the task message channel.
- Run and record a check with {tp_root}/tools/agent/test-run.sh WORKTREE -- CMD.
