# PD-B round 3: displaced bytes are kept, and every control record is checked (Main, 2026-11-09)

Review review-coder-be089069b2-210549 (REQUEST_CHANGES). Its report, `probes_r2.py` and logs are in
`/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-be089069b2-210549/`.
Run `probes_r2.py` on both interpreters before you change anything. Rules 1, 4 and 5 from round 2 were confirmed except for
F5 below; do not rework them.

## The defects
- **F1 (external-edit-loss, third time):** an editor save that lands after the digest check and before `os.replace`
  (store.py ~446-470, safe_write.write_atomic) is overwritten silently, in apply and in `--recover`.
- **F2:** with `commit.json` removed and the manifest revision hand-set to 3, adopt commits revision 4 on history `[1]`, and
  the next status is untrusted (`[1, 4]`, gap).
- **F3:** an older history entry replaced by `[]` is trusted; only the newest is validated. A top `base_revision: 999` is
  accepted.
- **F4:** a revision of `10**30` raises `OverflowError` at `list(range(...))` (store.py ~254-256).
- **F5:** an escaped lone surrogate (`\ud800`) in a text changeset, a bytes feedback bundle or the on-disk manifest gives
  `UnicodeEncodeError` instead of a structured refusal; dict input is refused, the other forms are not.
- **F6:** a malformed current history entry gives `control.invalid` and also `"trusted": true, "trust": []`.

## Main's decisions
1. **A commit never destroys bytes it did not write (F1).** Checking then replacing always leaves a window, and an editor
   does not take our lock, so a lock alone does not satisfy this. The rule is about what happens in the window: the inode
   a commit displaces is never unlinked by the commit. Displace each live file by an atomic rename (or `link`) into
   `.tp-product/txn/displaced/` first, then check the displaced bytes against the expected digest, then put the new file
   in place with an operation that fails if the target exists again (`os.link` from the temp file, as `_write_new` does).
   - Displaced bytes differ from the expected digest: put them back (without overwriting anything that has appeared at
     the target since), refuse with `txn.conflict`, name the retained copy.
   - The target has reappeared (an editor saved by rename): refuse with `txn.conflict`, keep the editor's file in place,
     keep the displaced bytes, name both.
   - After the new file is in place, check the displaced copy's digest once more (an in-place writer may have written to
     the old inode). Changed: keep it, report a conflict with its path; status reports it until `--recover` resolves it.
   - Displaced copies that match the expected digest may be removed once the commit is complete. Removing anything else
     needs an explicit user command.
   State the remaining limits in the note (another writer holding the old inode open after the final check). Apply the
   same sequence in `--recover`. Test every interleaving with a fault hook at each step (before displace, after displace,
   after check, before link, after link, after final check) × each package file × apply, adopt, ingest, recover.
2. **Every control record is validated, never only the newest (F2, F3, F4, F6).** One function validates all history
   entries (shape, types, revision equal to the file name, `base_revision` equal to the previous entry's revision) and the
   chain (no gaps). It checks adjacency over the entries that exist, without building a range over revision numbers, so
   any integer is a structured refusal. Any `control.invalid` means `trusted: false` with that reason, in every output.
   Before any command writes a new revision, the existing history must pass that function; a command does not create a
   state its own reader refuses (adopt with a missing `commit.json` reconciles against history or refuses). A history
   that legitimately starts after revision 1 stays allowed if the chain from its first entry is complete.
3. **Every text form is checked for representability the same way (F5).** One function decides whether a decoded value
   can be encoded as canonical UTF-8; text, bytes, dict and file inputs and the on-disk package all call it, and a
   failure is the documented structured input or file issue (`--json` prints the JSON error object).
- F7 stays owed to PD-E. F8: correct the report's helper description (`read_json_file` calls `size_problem` and
  `depth_problem` directly).

## Attack the rule before you report
- Rule 1: the fault-hook interleavings above, plus an editor that saves by rename and one that writes in place. For
  every case: the editor's bytes exist somewhere named in the refusal or are the live file, and status is accurate.
- Rule 2: each control file × missing, empty, wrong JSON type, wrong field types, huge and negative revisions, gap in the
  middle, wrong `base_revision`, an older entry malformed × status, validate, apply, adopt, ingest, recover. Expected: a
  structured refusal or `trusted: false` with a reason, and no write on top of a bad chain.
- Rule 3: lone high and low surrogates, a surrogate pair written as two escapes, and an unescaped valid astral character
  × each input form × each field type × `--json` on and off.
Print each input space by code, run every case on both interpreters, report counts. Keep the worth-keeping rows as tests and
fold `probes_r2.py` in.

## Report
- Fail-before for each new test against 65168d3 (the round-2 checkpoint).
- The four-cell run (`tests.test_product_design_package tests.test_product_design_store tests.test_skill_catalog
  tests.test_session_env`, both interpreters, outside and inside tmux) after your last edit, each file's time per cell, with
  the tree hash each run used and its start time.
- Attack table counts per rule, the review's probes rerun, Self-review, deviations.
- Commit nothing; `git add` new files. Brief rules (pd-brief.md) still apply. `RESULT_STATUS: complete` as the last line.
