# HB2b round 3: Main's record names what the caller passed (Main, 2026-11-11)

The HB2b brief and round 2 (`hb2b-brief.md`, `hb2b-round2.md`, this dir) still apply. Kind of round: mechanical (Sonnet). Lane base for fail-before: bb3a463.

## Review F1 (review-coder-d7af7eabdd-171109, realistic)
`tp-main --append-system-prompt-file /tmp/extra.md --tools Read` reaches the launcher unchanged, but `main-launch.json` omits
the instruction file and records tools as null "because tp-main passes no --tools". `lib/launch_record.py` `main_record`
rebuilds Main's defaults instead of reading the arguments. **Diagnosis:** brief rule 2 said "naming what Main was started with"
but did not say the caller's own arguments count; the worker recorded only tp-main's own additions. Same cause on both harnesses.

## The rule
Main's record is read from the argv the harness actually receives, for every argument that changes instructions, tools,
settings or plugins. For each such argument (both `--x value` and `--x=value` forms; repeated ones each recorded):
- instruction files (claude: `--append-system-prompt-file`, `--system-prompt-file`; text forms `--append-system-prompt`,
  `--system-prompt` by sha only; pi: its equivalents in bin/tp-pi's option list): an instructions entry with path and sha256
  of the file, or null plus a reason when it cannot be read;
- tools (`--tools`, `--allowedTools`/`--allowed-tools`, `--disallowedTools`/`--disallowed-tools`, pi's tools option): recorded
  as given; `tools.exposed` is the caller's `--tools` list when present and only otherwise the default with its reason;
- `--settings`, `--plugin-dir`, `--mcp-config`: path recorded (sha256 for a file); never read the contents of an mcp config
  beyond hashing it, and never print it.
Find the full option list from `claude --help`-free sources: the options bin/tp-main, bin/tp-claude and bin/tp-pi already
handle, plus the ones above. An option the code does not recognise is not guessed at. The launch argv stays byte-identical
(the existing test proves it; extend it with these inputs).

## Test (extend, do not add files)
Extend the existing Main record test with the reviewer's case and an attack table over: each option above in both forms,
repeated options, an unreadable file, a resume plus extra options, and no extra options (unchanged record). Fail-before on
bb3a463. Review F2 (error summary shows "none" for unknown fields) is a note: fix it only if it is one line in the reader
(`unknown` instead of `none` for an error record).

## Verification (the worker tests once)
Run once, `/usr/bin/python3` outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u
TP_STATE_DIR`) through `tools/agent/test-run.sh`, fresh short `TMUX_TMPDIR` under /tmp, `perl -e 'alarm 600; exec @ARGV'`:
tests.test_main_notify tests.test_work_brief tests.test_worker_env tests.test_session_env plus any module holding the test you
extend. Main runs the cells and discovery. No Claude or pi start; nothing to 127.0.0.1:1234 or any network; never the default
tmux server. Edit only lib/launch_record.py, lib/work_brief.py (F2 only) and the test file you extend. Search with safe_grep,
read with Read/safe_read; never read claude/mcp.json, .env*, auth files or config/local.json. Guard refusals: if a guard hook
refuses a shell or inline-script edit, make the same edit with Edit/Write and list it in the report; any other refusal means
stop and report. Do not approve any permission or placeholder prompt. Commit nothing. `RESULT_STATUS: complete` last.
