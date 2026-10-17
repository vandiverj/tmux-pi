# Persistent work context

Durable, user-level memory for work that outlives one session: workspaces (the objective, decisions made, open decisions,
blockers, next actions and links of one piece of work), notes, and the checkpoints that let a session recover after a
compaction or a restart. This file holds the text meant for `README.md` (the section under "README section") and for the
orchestrator prompts (under "Orchestrator prompt paragraphs"); both are copied from here when `README.md` is next edited.

## README section

### Persistent work context

tmux-pi keeps durable work in a user-level store, `~/.config/tmux-pi/work` (`TP_WORK_DIR` or `store` in `config/work.json`
changes it; nothing in it is committed anywhere). It holds **workspaces** and **notes** as Markdown files with frontmatter,
written atomically under a lock, private to the user (directories 0700, files 0600). No model is called to read or write them.

```
tp-agent work new "CI flakiness" --id ci-flakiness --objective "Stop flaky CI"
tp-agent work attach ci-flakiness                  # this session's index now carries it
tp-agent work decide ci-flakiness "Keep reviewers cross-provider"
tp-agent work decision add ci-flakiness --question "Re-add !1144?" --blocking
tp-agent work next ci-flakiness "rerun the loader"
tp-agent work decisions                            # every open decision, blocking first
tp-agent find TERMS                                # search workspaces, notes, earlier task records
```

A new session finds attached and nearby work in the injected task index without being told; open decisions appear as counts
and ids, never as question text (`tp-agent work decisions` shows the text).

#### Checkpoints and recovery

A **checkpoint** is a validated copy of a workspace's judgement state: the objective head, every `Decisions` line, every open
decision with all its fields, blockers, next actions and links. It is taken by deterministic parsing (no model call), written
atomically (temp file, fsync, rename, fsync of the directory), then read back and checked against its own `sha256`; it is
reported as written only when that check passes (`checkpoint written: store:checkpoints/<id>/<file>.json`; any failure, an
`--auto` one included, exits non-zero, and the hooks that call it ignore the exit code). The newest `checkpoints_keep`
(default 5) valid checkpoints are kept, in `<store>/checkpoints/<workspace-id>/`; pruning counts valid ones, so the last valid
checkpoint is never deleted. The only size limit is `checkpoint_max_bytes` (default 262144) on the whole file; a workspace over
it gets a checkpoint of whole items, oldest first (task rows, the agent summary, then the judgement items in the stated order),
with a `truncated` flag and counts of what was left out in `truncated.sections`, and every message about it says
`truncated: workspace file is the authority`. A limit too small for even an empty checkpoint is an error, never a larger file.

A checkpoint is written:

- by the Claude `PreCompact` hook (`manual` and `auto` compactions), which fails open: a compaction is never blocked;
- by any `work` change to an attached workspace (every mutating subcommand; the run is found the way the rest of `tp-agent`
  finds it, an exported `TP_STATE_DIR` or the session's own state directory) when the newest valid checkpoint is missing or older than ten minutes;
- by `tp-exit`, before it stops anything;
- by hand: `tp-agent work checkpoint [ID] [--summary TEXT | --summary-file FILE]` (your own short summary of where the work
  stands is stored with it and labelled as agent-authored). `--auto` takes no summary (`--auto` with `--summary` or
  `--summary-file` is a usage error and writes nothing). An automatic checkpoint brings no summary of its own, so it carries
  forward the newest valid checkpoint's agent summary, marked `carried_from` the checkpoint that first held it; `work recover`
  prints it as "carried forward from checkpoint seq N". With no summary to carry it is a plain snapshot.

```
tp-agent work checkpoint [ID] [--auto] [--trigger manual|precompact|session-end]
tp-agent work recover [ID]        # read-only: what is known now, from the workspace file or else the latest valid checkpoint
tp-agent work compacted [--reason TEXT]   # marks this session's recovery pending (for the hooks; each call adds a recovery generation, so one landing during `work recovered` stays pending)
```

`work recover` prints the live workspace state, the checkpoint (age, validity, truncation, agent summary), the tasks that
changed since the checkpoint (read from the live task records, only when the checkpoint is from this run; when the byte bound
cut task rows it compares only the rows kept and says `task diff partial: N task rows were not checkpointed`), every
checkpointed blocker and next action verbatim (done `[x]` ones included), and says what to do next. If the workspace file is missing or unreadable it prints the latest valid checkpoint's copy and says so. If the file holds
fewer items than a complete checkpoint copied, it says so and prints the checkpoint's copy. If the newest checkpoint is invalid
it falls back to the newest valid one and says that; with none valid it says "none valid".

What a checkpoint cannot capture: a `PreCompact` hook runs no model, so a decision made in the current turn and never written to
the workspace is not in it. Write decisions through `work decide|next|block|decision add` as they are made; the injection also
says how long ago the workspace was last updated and asks for re-confirmation with the user.

After a compaction the injected task index (Claude: `SessionStart` with source `compact`; Pi: `task_index`) names the attached
workspace, the checkpoint's age and validity, any task that changed since, and `tp-agent work recover`. Building that text reads
files and writes none, so injecting twice gives the same text. The recovery-pending marker in the run's state directory is a
generation: the `PreCompact` hook, the `SessionStart(compact)` handler (whether or not a checkpoint could be written), `work
attach` in a run (a restart that attaches a workspace) and `autonomy on` each add one under `session-recovery/` without taking a
lock. `tp-agent work recovered --evidence "..."` (after the same reconciliation `work recover` prints; `--accept T1,T2` for tasks
that changed, `--no-checkpoint` when this run has none; it refuses when the live task records cannot be read) records in
`session-recovery.json` the generation it validated, so a compaction during or after it stays pending. Nothing clears it by
time. With `tp-agent autonomy on --workspace ID` (Claude orchestrator main only) a pending marker refuses every new worker start
except a `tp-review` of a finished task; `autonomy off` is the user's way out. With recovery clear, an open `blocking` decision in an attached workspace
refuses only the dispatch its `Affects` covers (`workspace` all, `lane:<n>` that lane, `task:<id>` that parent); `autonomy status` and the
index name the decision id and say covered dispatch is refused. It clears only by `tp-agent work decision answer ID --text ... --by user
--quote "their exact words"` (an agent answer, or `--by user` with no quote, is recorded but leaves it open) or a withdrawal; this is
policy, not authentication. An unreadable attachment record refuses with autonomy on. An attached workspace whose file is missing or corrupt is still named in the injected text, with
`(file missing)` or `(corrupt: ...)`, and `work recover` prints the checkpoint's copy of it.

Settings (`config/work.json`): `checkpoints_keep`, `checkpoint_max_bytes` (and the Phase A/B ones: `inject_*`, `recent_days`,
`find_line_max`, `shared`, `projects`).

## Orchestrator prompt paragraphs

These are the two sentences that ship with Phase C, already in `agent/orchestrator-claude.md` (Compact Instructions) and
`agent/orchestrator-pi.md`. The paragraphs describing `work` itself belong in `agent/orchestrator.md`, `-claude.md` and `-pi.md`
once Phase A's documentation is added there; suggested text:

> Durable work lives in workspaces (`tp-agent work ...`). When one is attached, record each decision the moment the user makes it
> (`tp-agent work decide ID "..."`), each open question as `work decision add`, and each blocker and next action as they arise:
> a compaction summary is lossy, the workspace file is not. Never answer a blocking decision yourself; the user answers it.
> After a compaction or restart, run `tp-agent work recover`, re-verify any "done" claim against `tp-agent list` and
> `tp-agent inspect`, tell the user each open decision, then resume from verified progress.

Compact Instructions sentence (Claude): "If a workspace is attached, keep the attached workspace id and any open decision ids;
before a manual /compact run `tp-agent work checkpoint --summary-file FILE` (FILE holds a short note of what you are in the
middle of). After compaction run `tp-agent work recover`."

Pi sentence: "After compaction, if a workspace is attached, run `tp-agent work recover` (through `bash`) whether or not
`task_index` shows a pending recovery (Pi sets no marker), re-verify any "done" claim against live records, and tell the user
each open decision it lists."

Pi shows the same checkpoint entry as Claude: `task_index` returns `index --bounded`, whose `work.checkpoint` carries the line.
Pi has no verified compaction event, so no marker is set from Pi; the advisory works through the prompt sentence above.
