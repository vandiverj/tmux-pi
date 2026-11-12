# HB7a round 2 (lane hub-friction, on d47b47f)

Review review-coder-044ceb809b-083740: REQUEST_CHANGES (F1, F2), notes F3-F6. Read its report.md first. Main's diagnosis: F1 is a
brief fault (the brief let "a feedback memory whose body quotes the owner" count as the owner's words; that cannot be told from
text). F2 is a code defect. Main also decides F3, F4 and F5 below. Same bar (realistic use), same read-only report.

## Rules (state them in report.md before code; they replace the brief's where they differ)
1. **Owner authority comes from who wrote the source, never from its text.** Exactly two sources can be `owner+quote`: an HB3
   record whose provenance is `by: owner` with a non-empty quote (lib/work.py's own rule), and an H48 label row (the corpus is the
   owner's own messages). A memory file is always `main` (Main writes every memory file, even when it reports the owner's words), a
   doc pointer is whatever the mapping's `by` says and never `owner`, and any item whose mapping entry or source says `by: worker`
   is `worker`. Only `owner+quote` can raise a key to `active`; `main` and `worker` never can. No quote-mark detection anywhere.
   Make a table of every place provenance is set or read (loaders, mapping resolution, ungrouped listing, text and JSON output)
   and show each follows rule 1.
2. **Every filesystem step is inside error handling.** Discovery (`is_dir`, `iterdir`, `stat`, `is_file`, `is_symlink`, `open`,
   `read_text`) for the store, the memory dir, doc pointers and contract files: any `OSError` becomes a listed problem for that
   source and the other sources still report. Make a table of each filesystem call site with the handler that covers it.
3. **F4, decided by Main:** keep the message count, and add `recurrence_sessions` = the number of distinct session ids (`sid`)
   after the session of the first owner correction, for label items; items without a session id are reported apart, never counted.
   The text output shows sessions first. (The owner's question is whether a point came back after Main had it, not how many
   messages one exchange took.)
4. **F3, decided by Main:** brief rule (a) (a `friction:` key on a record) is deferred; keys come from the mapping only. Say so in
   the tool's help text and in report.md.
5. **F5, decided by Main:** keep `agent/contract/worker.md` in the contract population (it is a home the mapping already names),
   and print the brief's population (main-prompt + roles) as its own subtotal next to the total.

## Tests
Extend the existing table in `tests/test_friction_report.py`; no new methods unless a rule cannot be a row. Rows: a memory with a
quoted command and one with a quoted worker remark (both `main`, key stays `candidate`); a mapping entry `by: worker` on a label
or memory pointer (not `active`); an unreadable memory dir and an unreadable doc (problem listed, other sources reported; skip the
row when running as root); two sessions vs one session for `recurrence_sessions`. Fail-before: run the new rows against d47b47f
in the evidence-file form Main allowed (`.py` in your evidence/ dir, `cd "$TP_TASK_DIR" && python3 -I evidence/<f>.py`; copies
under evidence/, never in the lane's tools/). Run `tests.test_friction_report`, `tests.test_session_env`, `tests.test_docs_wiring`
once through `tools/agent/test-run.sh` with a fresh TMUX_TMPDIR. Do not commit. Do not add git notes.
