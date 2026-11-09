# MS1 round 3: each delivery carries its own token (Main, 2026-11-09)

Round 2 (coder-448576c6b9) was checkpoint-committed by Main as 3500782 on lane/msg-submit. Main previewed it merged with
main: 444 tests OK in all four cells, and 123 plugin tests passed. The review is REQUEST_CHANGES:
/Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-448576c6b9-175202/result.md
(read it, and `report.md` beside it).

## Why the design changes
Matching by a hash of the text cannot tell two sends of the same text apart (F1). It also needs the hook and the sender to
normalise text identically in two languages (F2). One change removes both problems: every delivery gets its own token, and
a submit is confirmed by that token.

## Build
1. **Token.**
   - `pane_send.send()` appends a short unique marker to the text it types, for example ` [tp:` + 10 random hex + `]`. Every
     call makes a new one, retries included, since the text is typed once. Name the format once as a constant, with a
     regex beside it.
   - The worker sees the marker. Say in the module docstring that it is a delivery id and has no meaning to the worker.
2. **Hook record.**
   - `report.sh` on `UserPromptSubmit` extracts every marker found in the hook's `prompt` with the regex, in `jq` (`scan`).
   - It records them in the bounded `promptLog` entry, for example `{n, at, tokens: [...]}`, and drops the text hash.
   - No whitespace normalisation is needed on either side.
3. **Confirm.** A submit is confirmed only when a `promptLog` entry carries this send's token. The count snapshot is no
   longer needed for correctness. Keep it only if something else reads it, and say what.
4. **F4: hook-input semantics.**
   - Main found that a worker transcript records the full expanded text of a message `tp-agent message` typed in. An
     example is the 822-character user entry "Main: one more change…" in the transcript named by
     `.../tasks/coder-36ec1ecbe7/transcript-path`.
   - Cite that, and the engine types, for what `UserPromptSubmitHookInput.prompt` holds.
   - Say plainly what is still unverified. If the hook's prompt held only a paste placeholder, the token would be missing,
     and the send would read as unconfirmed (a false "not submitted", never a false "submitted").
5. **F3: fail-before.** Give the base-run outcome of the changed immediate-Enter and contract-reminder tests. Say which
   changes are fixture adjustments and which are changed behaviour.

## Tests
- In `tests/test_pane_send.py` and `tests/fake_tui.py`:
  - **F1, delayed same-text record:** send A of a text reaches the fake worker but its record is held back. Send B of the
    same text has its Enter ignored. Then A's record lands. B is `submitted: false`. It fails before the fix.
  - **F2:** a prompt with U+001C–U+001F, tabs and odd spacing is confirmed by its token.
  - **Token matched in the prompt:** a record whose prompt holds another send's token does not confirm.
  - The round-2 tests still pass, rewritten to the token rule where they used the hash.
- In `tests/test_claude_report.py`: the hook extracts several tokens from one prompt, finds none in a prompt without one,
  and keeps the last N entries in order.
- Fail-before for each new or changed test against 3500782. Put the script and log under `evidence/` in the task dir and
  name them by absolute path.

## Done means
- The round-2 19-module set, with every new file `git add`-ed.
- It passes with `/usr/bin/python3` and `/opt/homebrew/bin/python3`, both outside tmux (`env -u TMUX -u TMUX_PANE -u
  TP_SESSION -u TP_TASK_ID -u TP_STATE_DIR`) and inside (`TMUX=/tmp/x,1,1 TP_SESSION=dummy TP_TASK_ID=dummy`).
- `claude plugin test claude/plugins/tmux-pi` passes.
- Rerun after your last edit and list each file's time.
- Commit nothing.
- `RESULT_STATUS` on its own line, as the last line of your final message.
- Do not start background shells or watchers that outlive your work.

No live run. Do not start Claude Code or pi, and send nothing to 127.0.0.1:1234. Any test that calls tmux sets
`TMUX_TMPDIR` to a fresh `mktemp -d /tmp/ms1.XXXX` and uses a private `-L` socket; never the default server, and never
`kill-server` without `-L`. Never read `.env*`, and never write that string in a shell command. Never search `bin/` as a
directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively; name the files. Never edit the
Claude profile or its trust store.
