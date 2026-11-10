# SG1 round 5: inline code given a sensitive argument is not a known non-search (Main, 2026-11-10)

Review review-coder-757b236bbd-145633 (REQUEST_CHANGES), report and reproductions:
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-757b236bbd-145633/`.
Round 4's design stands (the review confirmed the inversion, every consumer, and all earlier repros fixed); this round narrows one
cause. Rounds 1-4 briefs still apply. Bar: `/Users/jerrickvandiver/Documents/Projects/tmux-pi/docs/plans/realistic-use-bar.md`.

## The rule (route rule-first)
`RUNS_CODE` is a claim that the command runs code and not a search or read of the sensitive file. Give it only where that is shown:
- **Inline code** (`python3 -c`, `node -e`, `perl -e`, `ruby -e`, … and the other interpreters at the same sites) whose sensitive
  name reaches it **as an argument** (an operand after the code, stdin, or an env var the classifier sees) gets **no cause** →
  refused in a worker. The code receives the file to do something with it; the classifier cannot show that something is not a read.
- Inline code whose sensitive name is a literal in the code keeps round 4's rule (RUNS_CODE only when it does not shell out).
- A **script file** given a sensitive argument (`python3 tools/x.py .env`) keeps RUNS_CODE (project code; known limit), unchanged.
Apply it at every RUNS_CODE site (`lib/sensitive_read.py` ~2175-2190 and any other site `test_non_search_sites` lists), and extend
that test so a RUNS_CODE site must state which of the three forms it covers.

## F1 (Closes by: code)
The review's rows refuse in a worker (Claude shadow, "on", pi): python `subprocess.run([... sys.argv[1]])` with a sensitive operand,
the Node and `os.system` variants, plus `python3 -c 'print(open(sys.argv[1]).read())' config/local.json`. Main unchanged.

## F3 (Closes by: code): the refusal says what to do for a non-search
When a worker is refused on a command that is not a search/read the safe tools can serve (copy, move, edit, upload, code run), the
message says so in one sentence: ask Main (a capability request / message to Main) instead of retrying the command. Search/read
refusals keep naming the safe calls. Keep the text short; test the wording in one row per consumer.

F2 (safe_grep `include_ignored` parity) is a follow-up, not this round.

## Report
Fail-before against 46990d8 for each new row; the attack extended with the review's rows and an inline-code-with-argument row per
interpreter at each site; the four cells as in round 4 (same 10 modules), one after another, each log starting with `date -u`,
`TMUX_TMPDIR` and `git add -A && git write-tree`, then `git reset -q`; per-module times. Make the fail-before cause-table test report
its rows (it errored before evaluating them in round 4). Update known limits. Search with safe_grep and read with Read/safe_read;
never search `bin/` as a directory or `config/`, `agent/`, `claude/` recursively; never read `claude/mcp.json`, `.env*` or auth files.
Test strings that name `config/` or `.env` paths go in a script file. Nothing sent to 127.0.0.1:1234 or any network address; no
Claude or pi start. Commit nothing. `RESULT_STATUS: complete` as the last line, exactly. No background shells.
