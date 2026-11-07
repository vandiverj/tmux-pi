# E2: measure Claude main sessions and Claude re-reads

Lane `measure`, step 2. Level: `mid` coder. Touches: `bin/tp-improve`, `lib/routines.py`, `tests/test_routines.py`.
Requires E1.

## Why
`tp-improve report` reads only Pi main transcripts (`agent/sessions/*/*.jsonl`); the Claude main pane is unmeasured, so a
change that shifts cost from workers to Main would look like a saving. Its re-read counter matches only the Pi `read` tool, so
1,355 `sed -n` slices and every Claude `Read` were invisible. Token figures use `len/4`.

## Change
1. `tp-improve report`: also read `$CLAUDE_CONFIG_DIR/projects/<encoded cwd>/*.jsonl` (default
   `~/.config/tmux-pi/claude`, the encoding replaces `/` with `-`), grouped by `message.id`, with usage from
   `message.usage` (input, output, cache_read, cache_creation). Rows are labelled `main (claude)`; Pi rows keep their label.
2. Re-reads: count `Read` by `file_path`, `Bash` commands matching `sed -n A,Bp FILE`, `cat FILE`, `head`/`tail FILE`, and
   `safe_read` by path, per task and per main session; report the top repeated paths.
3. `routines`: add a `measured` token column beside the `len/4` estimate: for each tool result, the next assistant message's
   `input + cache_creation` minus the previous one's, when both are present; blank otherwise. Print how many calls had a
   measured value.

## Done means
1. A fixture Claude main transcript (two messages, one tool result) appears in `tp-improve report` as `main (claude)` with the summed usage.
2. Repeated `Read`, `sed -n` and `safe_read` of one path in a fixture task are counted as re-reads of that path.
3. The routines text report shows `measured` beside `est`, and the count of calls measured.
4. Pi-only behaviour is unchanged (existing tests pass).
5. Tests for 1 to 3 fail without the change.
