# A1: example tools manifest and a standing cheat-sheet for workers

Lane `tools-template`, step 1. Level: `mid` coder. Touches: `templates/agent-tools.example.json` (new), `docs/agent-tools.md`,
`lib/agent_tools.py`, `tests/test_preamble.py`, `tests/fixtures/agent_tools/`.

## Why
A project with no `.tmux-pi/agent-tools.json` gives workers no Tools block at all, so the helper scripts are never advertised
(the work monorepo and battle-soccer both lack one). Workers also run `tp-agent --help` (86 tasks) because the preamble
documents only `tp-agent request`. The standing line "Read a file once ... re-read only after you edit it" is too rigid after a
compaction.

## Change
1. `templates/agent-tools.example.json`: the common tools, valid for `lib/agent_tools.py` as it is. Entries, with `$TP_ROOT`
   paths and roles that exist in `agent/roles/`:
   - `lane-info` (coder, tester, researcher, code-reviewer): `WORKTREE [BASE_REF] [--fetch]` -> branch, HEAD, base, merge-base, changed files.
   - `pre-commit-gate` (coder, tester): `WORKTREE [BASE_REF]` -> one `name exit` line per check; last line `exit=N log=PATH`.
   - `web-jest` (coder, tester): as the current docs example.
   - `test-run` (coder, tester): path `$TP_ROOT/tools/agent/test-run.sh`, usage `WORKTREE [--timeout S] -- CMD...`, output
     `summary line; record in $TP_TASK_DIR/test-runs/; last line exit=N log=PATH`. The script lands in B1; until then the
     manifest loader's "path must exist and be executable" rule would refuse the example, so ship a stub
     `tools/agent/test-run.sh` that prints `test-run: not implemented yet` and exits 2 with the `exit=2 log=-` last line
     (B1 replaces it; B1 owns `tools/agent/` from then on; this step may create only that stub there).
   Keep each entry within the 150-byte rule.
2. `docs/agent-tools.md`: a "Start a project" paragraph: `mkdir -p PROJECT/.tmux-pi && cp $TP_ROOT/templates/agent-tools.example.json PROJECT/.tmux-pi/agent-tools.json`,
   then trim roles or entries; the loader refuses anything invalid. Document the new standing lines.
3. `lib/agent_tools.py` STANDING lines, replacing the "Read a file once" sentence:
   - `Do not re-request an unchanged range you already have; fetch more or refreshed context when you need it (after an edit, after a compaction).`
   - `Read a range with the Read tool (offset, limit) or safe_read, not sed -n. Quote paths with ( ) [ ] or spaces.`
   - `tp-agent request --kind skill|tool|context --what DESCRIPTION --reason REASON` (keep)
   - `tp-agent message TASK --text TEXT replies to a question from Main; tools/agent/test-run.sh WORKTREE -- CMD runs a check and records it.`
   Role-filter nothing here (every role gets them). The block stays under the 8,000-byte cap for every role; print the byte
   total per role in the report.
4. Tests in `tests/test_preamble.py`: (a) the example manifest loads for every role in `agent/roles/` with no error and the
   coder block names the four tools; (b) golden fixture `tests/fixtures/agent_tools/golden-coder.txt` updated to the new
   standing lines; (c) a test that the example's entry bytes are within the limit (this is what fails if someone fattens an entry).

## Done means
1. `templates/agent-tools.example.json` exists, loads through `lib/agent_tools.py` for every role without error, and names lane-info, pre-commit-gate, web-jest and test-run.
2. The stub `tools/agent/test-run.sh` is executable, prints a last line `exit=2 log=-`, and the example manifest's path check passes.
3. The standing lines in the rendered Tools block are the four lines above, byte for byte, and the old "Read a file once" sentence is gone.
4. `tests/test_preamble.py` has a test per item 1 to 3 that fails without the change (name which existing test was extended where possible).
5. `docs/agent-tools.md` documents the copy command and the new standing lines.
6. Report the rendered block byte count per role, each under 8,000, with the command used.
