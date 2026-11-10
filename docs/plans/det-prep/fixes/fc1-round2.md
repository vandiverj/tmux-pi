# FC1 round 2 (Main, 2026-11-09)

Review review-coder-5a7cedd6a7-113443 is REQUEST_CHANGES. Read its result.md and report.md in this task's reviews. Main
checkpointed round 1 as commit 321568c.

For each finding: fix it, then sweep for every other site of the same family. Give one sweep table per family, covering
every site: fixed, already right, or not an instance, each with evidence.

## F1: `gate()` reads hunk text as file headers (family: frozen-gate-hunk-confusion)
`lib/review_rounds.py` `gate()` (lines 374–376 at 321568c) treats a `--- a/X` line followed by `+++ b/X` as a file header
pair. Inside a hunk those are a removed line `-- a/X` and an added line `++ b/X`. With verified frozen path `copy`, an edit in
another file from `-- a/copy` to `++ b/copy` makes `safe_diff()` refuse the packet. The reviewer reproduced it.

**Fix.**
- Parse the diff with hunk context. After an `@@ -a,b +c,d @@` header, the next lines are hunk body until the old and new
  counts are used up (a missing count means 1; `\ No newline at end of file` uses neither). Only lines outside a hunk body
  can be file headers.
- Apply the same parse to the flagged-path check just above it (lines 370–372), which has the same shape.
- A diff the parser cannot follow (a count that runs past the end, a malformed `@@` line) refuses, as now. Never
  attach on a parse error.
- Keep every refusal that exists for a real header.

**Test.**
- The reviewer's case: frozen path `copy`; another file's hunk changes `-- a/copy` to `++ b/copy`. It must be attached, not
  refused. It must fail on 321568c.
- The same for a flagged (withheld) path name in hunk text.
- Guards that still refuse: a real `--- a/copy` / `+++ b/copy` header for a left-out path; a real header for a flagged
  path; a truncated hunk.

**Sweep.** Every place in lib/ and the named bin files that reads diff text line by line and decides on `diff --git `,
`--- `, `+++ ` or `@@` (gate, safe_diff, the timer-wait scan, any packet section splitter). For each: hunk-aware, not an
instance (with the reason), or fixed.

## F2: bounded Git work (note, do it now)
- Batch per diff: resolve each manifest commit once, check each commit's ancestry once, and list each source once, cached
  by (commit, source). State the number of Git calls as a function of manifests and distinct commits, not of copies.
- Skip the whole-tree listing when the diff adds or changes no `frozen-copies.json` and no manifest exists in the
  touched directories; say how that is decided.
- `read_manifest`: stop reading at `MAX_MANIFEST + 1` bytes (check the blob size first with `git cat-file -s`).
- Report `tests.test_review_rounds` time before and after, and each added test's time. If a file stays over 60 s, say why
  and which tests cost it.

## F3: logs the reviewer can read
Put every log and script you cite in this task dir under `evidence/`, and name each by absolute path in your report:
- the four-cell logs of this round, with each file's time;
- your fail-before script and its log against 321568c for each new test (name any guard, with the reason);
- the per-file timing output.

Main will attach its own evidence by absolute path too.

## Done means
1. The F1 test fails on 321568c and passes now; the guards pass on both.
2. The F1 sweep table.
3. Git calls per diff stated and bounded; test_review_rounds time before and after.
4. Four cells over the same 26 modules, with logs under `evidence/`.
5. Fail-before script and log under `evidence/`.
6. `RESULT_STATUS` reported.

Never read `.env*`, and never write the string `.env` in a shell command, not even in an exclusion glob. Never search `bin/`
as a directory; name the bin files. Never search `claude/`, `agent/` or `config/` recursively. No live model. Any test that
calls tmux: set `TMUX_TMPDIR` to a temp dir and use a private `-L` socket. Tests that need an ambient tmux server fail in
an empty `TMUX_TMPDIR`; that is a known environment cause, recorded by Main, not yours to fix here.
