# Frozen real files (split `real`, LS1b)

These ten files are byte-for-byte copies of tmux-pi files, taken from commit `109948a` (`git show 109948a:<source path>`).
They are test data for the summarize_files qualification chores (`chores.jsonl`, split `real`) and nothing reads or runs them as code:
there is no `__init__.py` on the way down, no copy keeps an exec bit (`bin/tp-spawn` is mode 644), and the copies must not be edited,
because the answers cite their line numbers. To refresh one, copy it again from a newer commit, change this file, and redo the chores'
`support_lines` and `anchors` (`tests/test_qual_summarize.py` checks that every anchor is still on its line).

None of them is, or was copied from, a `.env*` file, anything under `config/` or `agent/`, or any credential or private data.

| fixture | source path | commit |
| --- | --- | --- |
| `real/lib/lanes.py` | `lib/lanes.py` | `109948a` |
| `real/lib/lane_remove.py` | `lib/lane_remove.py` | `109948a` |
| `real/lib/lane_commands.py` | `lib/lane_commands.py` | `109948a` |
| `real/lib/lane_lease.py` | `lib/lane_lease.py` | `109948a` |
| `real/lib/lane_deps.py` | `lib/lane_deps.py` | `109948a` |
| `real/bin/tp-spawn` | `bin/tp-spawn` | `109948a` |
| `real/docs/plans/det-prep/ls1-local-summarizer.md` | `docs/plans/det-prep/ls1-local-summarizer.md` | `109948a` |
| `real/docs/plans/det-prep/README.md` | `docs/plans/det-prep/README.md` | `109948a` |
| `real/README.md` | `README.md` | `109948a` |
| `real/tests/test_lane_lease.py` | `tests/test_lane_lease.py` | `109948a` |
