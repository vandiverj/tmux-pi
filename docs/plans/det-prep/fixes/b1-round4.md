# B1 round 4: one JSON serializer, proven by a byte round-trip (route: design)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-a7350b4c4b-172004/result.md and report.md beside it.

`tp_json_str` in `tools/agent/_common.sh` deletes control bytes, so a recorded file name or argument can differ from the real one.

## Rule
No shell script builds JSON by hand. Every record writer (`pre-commit-gate.sh`, `test-run.sh`, and any other user of `tp_json_str`) passes its values to one serializer: `python3` with `json.dumps`, reading NUL-separated input or argv, never text interpolated into code. Values are decoded with `os.fsdecode` (surrogateescape), so non-UTF-8 bytes survive as `\udcXX`, and the reader in `lib/gate_record.py` encodes them back the same way. Delete `tp_json_str` or make it a thin wrapper over that serializer. Sweep every caller.

## Tests
A generated round-trip table: for every byte 0x01–0xFF, a file name and a test-run argument containing it are written by the scripts. Reading the record back yields the exact bytes. Add rows for a name with a newline, with a quote and a backslash, and with invalid UTF-8.

## Evidence decision (Main)
`lib/gate_record.py` did not exist at the base, so the base's ImportError is the accepted before-state for `test_gate_record`. No further comparison is owed.

## Report
Include the exact commands and log paths, and a Site / Disposition / Evidence sweep table.
