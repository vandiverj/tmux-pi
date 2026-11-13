# HB6c round 7 (mechanical): the fallback keeps healthy rows and every diagnostic; failed records are listed by file

Review review-coder-bb5162eb52-183113 (read its report.md, sections F1 and F2, and its evidence/isolation.py and evidence/paths.py). Rounds 1–6 and round 4 F2–F3 are fixed; these two remain.

**F1** (`tools/waits/status.py:507–530`, consumed at :667–673). When the per-task probe finds a culprit, the fallback returns `rows={}`, so every healthy lane loses its timeline state ("unknown", plus a false "no timeline" problem). It also keeps `reader.problems` only when `reader.undecodable`, so a second task's ordinary malformed record drops out of `problems`.
- Fix: rebuild the timeline over every task except the culprits, so healthy tasks keep their rows. Keep every problem the probe found, decodable or not, through the same one-entry-per-record helper the main build uses (the per-ID branch currently bypasses it).
- A culprit's own lanes stay unknown.

**F2** (`:298–303`, `:398–409`). `_read_review` reduces a failed `acceptance-items.json`, `sweep-rows.json` or unparseable `result.md` to the review directory. `_problem_dirs` reduces a missing or malformed `meta.json` (tasks and archive) to the task directory.
- Fix: list the failing file's path. The directory may be listed as well, but the file must be listed.

Tests: extend `StatusTest.test_next_action_table` with in-process rows only; add no new method, worktree or process. Rows to add:
- **F1:** one healthy open lane, a foreign task whose `state.json` is 1100-deep JSON, and another foreign task whose `state.json` is `{bad`. Assert the healthy lane's whole `state` (`{state, since}`), its rule, and no "no timeline" problem for it. Assert both foreign paths are listed. The 1100-deep JSON is a decoder error on py3.9 and a wrong shape on py3.14, so the row must hold on both.
- **F2:** an approving settled review whose `acceptance-items.json` is `{bad`, and the same with `sweep-rows.json`. Both rows list the file's path.
- **F2:** the only writer has a `{broken` meta.json and a valid dispatch tag. The row lists `meta.json`'s path.

Fail-before: `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py` against bdd88dc extracted to /tmp. Do not edit any tracked file. It exits nonzero on any mismatch.

Run `tests.test_wait_timeline` and `tests.test_session_env` through `tools/agent/test-run.sh` once on `/usr/bin/python3` and once on `/opt/homebrew/bin/python3.14`. Run them outside tmux (`env -u TMUX -u TMUX_PANE -u TP_SESSION -u TP_TASK_ID -u TP_TASK_DIR -u TP_STATE_DIR`), with a fresh short `TMUX_TMPDIR` under /tmp, in the foreground.

Touch only tools/waits/status.py and tests/test_wait_timeline.py. Do not touch the default tmux server. Do not edit config/. Do not commit. No git notes. No background shells. `RESULT_STATUS: complete` as the last line.
