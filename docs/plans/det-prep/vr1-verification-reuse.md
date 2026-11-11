# VR1: reuse valid verification results across agents (loop idea H33)

Owner, 2026-11-10: the coder, tester, reviewer and Main should share existing verification evidence and not rerun checks whose
results still apply. An agent requests a check and gets an existing valid result, joins an equivalent run in progress, or runs
it fresh; the answer carries the original command, scope, outcome, logs and why the evidence is reusable. **Priority: high**
(owner's first priority with WT1). Cost tracking is out of scope. Status: **open**. Advisory: the owner decides on adoption.

Overlap: **H19** (requirement change invalidates evidence) needs the same key; VR1 builds the source/config part of it, H19 adds
criteria. **H8** (runner fills counts and log paths from records) would read the same records. The deterministic-check hooks in
the code-quality eval ("Where the checks run") reuse results by tree id, so they would call this instead of their own cache.

## What exists today (inventory 2026-11-10, refs at main fd2321d)
- `tp-gate:v1` (tools/agent/pre-commit-gate.sh:526-546; lib/gate_record.py:48-63): worktree, base, head, tree, files, per-check
  `{name, status, exit, seconds}`, `started`/`finished`, log. No per-check command or args, no tool versions, env or config hash.
- `tp-test-run:v1` (tools/agent/test-run.sh:111-136): label, cmd[], cwd, head, tree, exit (124 timeout), seconds, log, summary.
  No start/end, timeout value, env or versions. Logs live in `$TMPDIR/tp-tools/` and can vanish before the record does.
- `tests/run_bounded.py` appends `tests.jsonl` with cmd, files, seconds, rc, load; no tree.
- Freshness: `gate_record.status()` (fresh only if head and tree equal the current fingerprint) exists, but **nothing in
  production calls it**; only tests import it. The lane review gate checks `reviewed-state.tree` against the current tree
  (lib/lane_steps.py:324-342), using `review_rounds.snapshot`, a **different tree algorithm** from the gate/test fingerprint
  (it does not refresh ignored staged files), so the same content can give two trees.
- Source changes during a run are not detected: test-run and the gate fingerprint only before the command.
- No dedupe or join of identical runs; an interrupted test-run or gate leaves no record (the EXIT trap runs before the write).
- `tp-review --evidence` attaches files as given, with no provenance or freshness check.
- Main's own SG1 verification (16 modules x 4 cells, ~25 min per round) reruns what the worker's four cells ran on the same
  tree, which is the duplicate this eval targets first.

## Equivalence key
sha256 of: canonical `cmd[]`; cwd relative to the repo top; source tree from the one shared fingerprint (tracked + untracked
non-ignored); config and lockfile contents named per check (for example `package-lock.json`, `tsconfig.json`, eslint/prettier
config); tool versions (python, node, tsc, eslint, pi package); an allowlist of env values that change outcomes (PATH-resolved
interpreter, `TMUX_TMPDIR` presence, test skip switches such as `QUAL_ATTACK_COUNTS`). Anything outside the key is documented as
"not covered".

**Never reused (external state, fresh run every time):** anything that talks to a model or LM Studio, the network, a live tmux
session, the clock beyond a timeout (tests marked timing-sensitive), `tests/check_dispatcher_types.sh` until the pi package
version is in the key, bench and smoke runs.

## Smallest implementation (candidate arm B)
`tp-check run WORKTREE --label L -- CMD` around test-run.sh and gate_record.py:
1. Index at `<git common dir>/tmux-pi/checks/<key>.json`: the record plus `started`, `finished`, `tree_after`, the key inputs and
   a durable copy of the log.
2. Lookup: exit recorded and `tree == tree_after == current fingerprint` and key equal → **reused**, with the reason. A live
   `<key>.lock` (O_EXCL, pid and start time) → **joined** (wait for its record); a dead holder's lock is dropped. Otherwise
   **fresh**, with a fingerprint after the run; if the tree moved during the run the record is `unstable` and never reused.
3. Returns `{cmd, scope (cwd, base, files), outcome, exit, log, record, reuse: fresh|joined|reused, reason}`.
4. Review tree and gate tree use the same fingerprint first (prerequisite fix, its own small step).
5. `tp-review --evidence` accepts a record and states its freshness; lane close may require a fresh record (owner decides).

## Arms (same tasks and conditions)
- **A, current workflow:** each agent runs its own checks.
- **B, evidence reuse** through `tp-check`.

## Cases
1. Same-task replays: a coder finishes, then tester, reviewer and Main each request the same checks (focused unittest modules,
   `py_compile`/syntax, the gate's prettier/eslint/tsc on battle-soccer).
2. Deliberate changes between requests, each must force a fresh run: source file, test file, config file, lockfile, tool
   version, an allowlisted env value; plus a change outside the key that must not matter (a doc file outside the check's scope
   when scope is per-file).
3. Two agents requesting the same check at once (join); a run killed mid-way (no reusable record, next request runs fresh);
   an incomplete or corrupt record (treated as missing); a source edit during a run (`unstable`).
4. Real replay: SG1 round 9's worker cells and Main's preview verification (same modules), counted after the fact.

## Measures
Duplicate executions avoided; elapsed time saved; false cache misses (a fresh run whose key differed for no outcome-relevant
reason); invalid reuse (a reused result that a fresh run contradicts: **must be 0**); join waits; overhead of the lookup.

## Decision
Incorporate the smallest piece that avoids at least one duplicate per reviewed task with 0 invalid reuse across all case-2/3
changes, and list the checks it supports reliably. Keep on demand if savings are real but only for some agents. Defer if any
invalid reuse appears that the key cannot fix.

## Missing before it can run
The shared fingerprint (one tree algorithm); records for tool versions; a lane branched from `main`. No access needed beyond
this Mac.
