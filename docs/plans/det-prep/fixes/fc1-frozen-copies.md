# FC1 (FROZEN-COPIES): verified copies of committed files are named, not repeated, in a review diff

Lane `frozen-copies`, step FC1. Level: `mid` coder. Base: `main`.

Touches:
- `lib/review_rounds.py`;
- `bin/tp-review`, only if needed;
- `docs/lanes.md` and the README review section;
- `tests/test_review_rounds.py`, plus `tests/test_tp_review.py` if bin/tp-review changes.

Not touched: `config/`, `agent/`, the LS1b worktree.

## Why (Main, 2026-11-09)
LS1b adds ten test fixtures under `tests/calibration/summarize/fixtures/real/`. Each one is a byte-for-byte copy of a tmux-pi
file at commit `109948a`, which is an ancestor of main; Main checked all ten with `cmp` against `git show 109948a:<src>`.
- Their diff is 288k characters of a 420k lane diff, so `tp-agent review` refuses the packet: 480k against the 300k limit.
- The reviewer gains nothing from reading 4,000 lines that already exist in reviewed history. It only needs proof that they
  are exact copies.
- The limit is not raised and no override is used. Vendored test data copied from history is common on real work, so the
  packet must handle it.

## Change
1. **Manifest.** A file named `frozen-copies.json` in a directory D declares copies under D:
   ```
   {"copies": [{"path": "<relative to D>", "source": "<repo path>", "commit": "<sha>"}]}
   ```
   The manifest is an ordinary changed file and is shown in full.
2. **Verify, in `safe_diff`.** Read the manifest from the reviewed tree, by its blob id from the same `--raw` listing, never
   from a path on disk. An added or modified entry under D is left out of the diff only when every check holds:
   - it is listed in the manifest;
   - `commit` resolves to a commit that is an ancestor of the diff's old side, or of the lane base when that is how the
     diff is scoped. State which you used and why.
   - the blob of `commit:source` equals the entry's new blob id, and the mode is 100644 or 100755 as in the source;
   - neither the path nor the source is flagged by the sensitive-read check. Flagged paths keep their current withholding.

   Any check that fails, or a manifest that cannot be parsed, means the entry gets its normal full diff. In that case add a
   one-line note giving the reason, for example "frozen-copies.json: <path> does not match 109948a:lib/x.py". Never refuse
   the packet, and never leave out content that was not verified.
3. **Note line.** For example: `"<path>" (added): byte-identical to 109948a:lib/lanes.py (blob <id>), contents left out;
   check with git cat-file -p <id> or open the file`. Put it with the existing withheld and generated notes, and make the
   `gate()` checks cover it, so a left-out path can never have a section in the body.
4. **Every diff that goes through `safe_diff`** uses it: the round-1 diff, later-round diffs, and held sections.
5. **Docs.** `docs/lanes.md` gets one paragraph covering the manifest format, the checks, and the fallback.

## Tests (each fails before the change)
- A manifest with a correct copy: left out, with the note line carrying the commit, source and blob id.
- The copy edited by one byte: full diff and a mismatch note.
- A commit that is not an ancestor, or does not exist: full diff and a note.
- A source path flagged as sensitive: withheld exactly as today.
- An entry not in the manifest, beside a listed one: full diff.
- A manifest with invalid JSON, or the wrong shape: full diffs and a note; no exception.
- A path with spaces or non-ASCII characters.
- A later round where only the manifest changes.
- `gate()` refuses a body that has a section for a left-out path.
- An end-to-end `tp-review` packet-size test: a lane with a large verified copy goes under the limit, and the same lane
  without the manifest is refused as before.

## Rules
- Never read `.env*`.
- No live model, no review dispatch.
- Any test that calls tmux: `TMUX_TMPDIR` set to a temp dir first, and a private `-L` socket.

## Done means
- Four cells green: `/usr/bin/python3` and `/opt/homebrew/bin/python3`, each with the session env set and unset, for
  test_review_rounds and every test module that imports lib/review_rounds or runs bin/tp-review. Each file's time is given.
  A file that was already over 60 s on main is reported with its main time, not split in this lane.
- Fail-before shown for each new test.
- The scripts you wrote and ran are listed.
- `RESULT_STATUS` is reported.
