# HB5c (rule-first): the repeat report reads TMUX as present or absent

Found in HB5b decision A's first lane (`hb5b-decision.md`, last section). Main's in-tmux cell runs set `TMUX=/tmp/x,1,1`.
`tools/checks/repeats.py` marks every one of them "not understood: an environment value outside the grammar"; that was 26 of
Main's 52 runs. The R4 grammar allows a value that starts with "/" only for `TMUX_TMPDIR`, and N3 keys TMUX_TMPDIR by its
presence alone (`repeats.py:144-148, 157-162`).

## Rule
`TMUX` is keyed the way TMUX_TMPDIR is, by presence only, and for the same reason. Its value is a socket path, a server pid
and a session index, which say which tmux server a run sat under, not what it checked. Present versus absent still separates
an in-tmux run from an out-of-tmux run. Its value may be absolute and is checked as TMUX_TMPDIR's is: no `..` component, and
otherwise the grammar characters, with `,` allowed. Name every other variable you find that is keyed the same way, or say none.
Do not widen the grammar for any other name.

## Tests
Extend `tests/test_check_repeats.py` where it covers N3 and R4 TMUX_TMPDIR; add no new file. Rows to cover:
- two runs differing only in TMUX value: same key;
- TMUX present versus absent: different keys;
- an absolute TMUX value: understood;
- a TMUX value with a `..` component: not understood;
- `env -u TMUX`: still read.

Fail-before: a `.py` in evidence/, run with `python3 -I` against main extracted to /tmp. It names the failing assertions.
Attack table in report.md over TMUX spellings: absent, empty, `-u`, absolute, relative, with `..`, with spaces, quoted. Mark
each row realistic or contrived.

Run once on `/usr/bin/python3` through `tools/agent/test-run.sh`: `tests.test_check_repeats` and `tests.test_session_env`.
Run outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short
`TMUX_TMPDIR` under /tmp, in the foreground.

Touch only tools/checks/repeats.py and tests/test_check_repeats.py. Do not edit config/. Do not commit. No git notes. No
background shells. `RESULT_STATUS: complete` as the last line.
