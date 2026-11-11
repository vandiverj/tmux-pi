# Agent tools: shared helper scripts and the "Project Tools" block

Workers repeat the same setup routines in every task: orient in the lane, run the pre-commit checks, run web jest. This
repository ships plain scripts for them (`tools/agent/`: `lane-info`, `pre-commit-gate`, `web-jest`, `test-run`), and a project lists the ones its workers should use in
`<project>/.tmux-pi/agent-tools.json`. `lib/preamble.py` renders that list as a "Project Tools" block in the worker's standing
instructions, filtered by the worker's role. Nothing here goes into a tracked path of the project: the scripts live in
this repository and take the worktree as an argument, and `.tmux-pi/` is git-excluded in the project.

## The scripts

Each script is one plain command with absolute-path arguments (the preamble forbids heredocs, `$(...)`, arrays and
loops). Each prints a short summary; its **last line is `exit=N log=PATH`**, with the full output in a log under
`${TMPDIR:-/tmp}/tp-tools/` (`log=-` when it stopped before it made one, e.g. a usage error).

| Script | Arguments | What it does |
|---|---|---|
| `pre-commit-gate.sh` | `WORKTREE [BASE_REF] [--require NAME...]` | Finds the files changed against the base (committed, uncommitted, untracked), then runs prettier `--check`, eslint on changed JS/TS (an app the gate treats as Deno is linted with `deno lint --json` instead, still recorded as `eslint:APP`, and only a diagnostic that starts on a line the change adds or modifies (`git diff -U0` from the merge base to the working tree; an untracked file is all lines) fails it, the count of the others is in the log and the check line; a deno run whose JSON does not parse, or that fails with no diagnostic, is `missing`; a file deno cannot parse fails; an eslint run that ignored every file, by missing config or ignore pattern, is `missing`, not a pass), the typecheck of each touched app, whatever file in it changed (`deno check --allow-import` when it has `deno.json(c)`, on the changed TS files, or on all the app's TS files when something else changed or a file was deleted; a deletion, also the old path of a move, touches its app; else `tsc --noEmit` when it has `tsconfig.json`) and `gitleaks git --log-opts=BASE..HEAD`; when gitleaks is installed it also runs `gitleaks dir` over the uncommitted and untracked changed files (check `gitleaks-dirty`). One `name exit` line per check. `BASE_REF` defaults to env `LANE_BASE`, then the upstream branch. A tool that is not installed is `skipped (not found)` and does not fail the gate, unless `--require NAME...` names it (`typecheck` covers every `typecheck:APP`): then the gate exits 1 and says `required check missing: NAME`. When no check passed or failed the summary says `no check applied`. Writes a record (see Records). |
| `test-run.sh` | `WORKTREE [--timeout S] [--label NAME] -- CMD ARGS...` | Runs CMD in the worktree to completion or to a hard timeout (default 900 s; on timeout the process group is killed, the exit is 124 and the output says so), in the foreground: it never backgrounds and needs no polling. Output goes to the log; stdout gets its last 20 lines and a `record:` line. Exit = CMD's exit. Writes a record (see Records). |
| `lane-info.sh` | `WORKTREE [BASE_REF] [--fetch]` | Branch, HEAD, base ref and sha, merge-base, ahead/behind, `git diff --name-status BASE...HEAD`, uncommitted files, whether `node_modules` is a symlink, the deno path. Fetches only with `--fetch`. Prints the newest file of a `drizzle/` folder when one exists. |
| `web-jest.sh` | `WORKTREE [--repeat N] [--config-hook PATH] TEST_PATH...` | Runs jest from the package that owns each test with `--runTestsByPath --ci` and `TZ=UTC`; paths with parentheses are passed as one argument and logged quoted. Per-file counts; `--repeat N` prints one `run i exit` line per run, with that run's per-file counts under it. `--config-hook` is a project JS file (absolute path, kept in the project's `.tmux-pi/`) that exports `function (config, { worktree, packageDir })` and returns the jest config to use. Nothing project-specific is built in. |

Exit code of a script = the `N` in the last line.

## TypeScript project references

By default the gate runs `tsc --noEmit` without building references. If the app has project references and fails only
with TS6305 (unbuilt declarations), its typecheck is `missing` (setup), with an opt-in hint; `--require typecheck`
makes that missing check fail the gate. Other errors still fail normally.

To build references first, add this key to `.tmux-pi/routines.json` (preserving any existing routine settings). The gate
reads the **worktree's** file first; a worktree without one (a lane worktree usually has none) uses the **main
checkout's** file (the parent of `git rev-parse --git-common-dir`). A file that is present wins whole:

```json
{"build_tsc_references": true}
```

Only JSON `true` enables it; absent/false/other values do not. An unreadable, malformed, or non-object config stops the
gate with an error. This is a project-wide opt-in for touched TypeScript apps.
The gate runs `tsc -b <references>` before `tsc --noEmit`. It compares the tree fingerprint (which includes tracked
files even when they match an ignore pattern) before and after each reference build, even a failed build. Changes to
tracked files or unignored outputs fail the typecheck and the summary and log list the paths whose content differs.
Existing unrelated dirty files are allowed, but modifying them is not. **The gate does not undo writes**: opt in only when the project's build outputs are gitignored.
Ignored outputs remain on disk for subsequent checks; output paths are not predicted by the gate.

## Records

`pre-commit-gate.sh` and `test-run.sh` each write a JSON record of what ran on which tree, so a reader can tell whether
the evidence still holds. Both name the tree they saw: `head` (the commit) and `tree`, the fingerprint of the working
tree: `git write-tree` of a temporary index seeded from HEAD plus every path of the real index (flags such as assume-unchanged are not inherited), then filled by `git add -A` (every tracked file, ignored or not, plus
untracked, non-ignored files; the real index is not touched; the same content gives the same sha).

- **Gate**, `tp-gate:v1`: `worktree`, `base {ref, sha}`, `head`, `tree`, `files` (the changed files), `checks`
  (`name`, `status` = `passed|failed|missing|not_applicable`, `exit` or null, whole `seconds`), `exit`, `started`,
  `finished`, `log`. `missing` = the tool is not installed or references need setup (see above); `not_applicable` = no config or no matching files. Written
  beside the log (same name, `.json`) and, when env `TP_TASK_DIR` is set, to `$TP_TASK_DIR/gate.json`; an earlier
  `gate.json` is kept as `gate-<timestamp>.json`. There is no aggregate per-app check: `prettier`, `eslint` and the
  typecheck are each recorded once per changed app (`prettier:APP`, `eslint:APP`, `typecheck:APP`), so (a Deno app's `eslint:APP` is `deno lint`) an app without an
  eslint config is `not_applicable` on its own and never hides behind another app's pass; an app with no changes has no
  check. `--require prettier` covers every `prettier:APP`. When no app has a matching file the check is recorded once under
  its bare name as `not_applicable`. The gate fails closed: a listing, copy or fingerprint it
  could not complete (or a changed file that vanished while it collected) fails the check `collect` (or `typecheck:APP`,
  `gitleaks-dirty`) with the reason; it never passes on a partial set.
- **Test run**, `tp-test-run:v1`: `label`, `cmd` (array), `cwd`, `head`, `tree`, `exit`, `timed_out`, `seconds`, `log`,
  `summary` (the last line a known runner printed: `Ran N tests in …`, `Tests: …`, `Test Files …` or vitest's `Tests …`).
  Written to `$TP_TASK_DIR/test-runs/<n>.json` (n counts up from 1), or beside the log when `TP_TASK_DIR` is unset. The number is claimed atomically (`mkdir` of `<n>.lock`) and the record
  is renamed into place, so runs started at the same time get distinct numbers and no record is lost. When the HEAD
  or the tree fingerprint cannot be read, the script exits 2 before it starts CMD and writes no record.

No script builds JSON by hand: both pass their values as NUL-separated tokens to `tp_json_write` (`_common.sh`), which runs
`tools/agent/json_record.py` (python `json.dumps`). Values are decoded with `os.fsdecode`, so every byte of a file name or
argument survives; a byte that is not UTF-8 is stored as the escape `\udcXX`, and `gate_record.as_bytes(value)`
(`os.fsencode`) gives the exact bytes back.

`lib/gate_record.py` reads them: `load`, `validate` (one type table, `KINDS` / `RECORD_FIELDS` / `CHECK_FIELDS`, gives each
field's type and allowed values, at the top level and per check status; any mismatch makes the record `invalid`, and `validate` and `status` never raise on any JSON), `current(worktree)` (the same fingerprint), `status(record,
worktree)` = `fresh` (same head and tree), `stale` (anything changed since) or `invalid`, and `missing_required(record,
names)`.

## Start a project

A project with no `.tmux-pi/agent-tools.json` gives its workers no Project Tools block at all. To start one, copy the example and
trim it:

```
mkdir -p PROJECT/.tmux-pi && cp $TP_ROOT/templates/agent-tools.example.json PROJECT/.tmux-pi/agent-tools.json
```

The example lists `lane-info`, `pre-commit-gate`, `web-jest` and `test-run`. Remove the roles or entries the project does
not want; the loader refuses anything invalid.

## The manifest

`<workdir>/.tmux-pi/agent-tools.json`; when the working tree has none, the task's `preamble_from` repository is read, the
same fallback as `worker-preamble.md`. A JSON list of entries, or `{"tools": [...]}`:

```json
{
  "tools": [
    {
      "name": "web-jest",
      "path": "$TP_ROOT/tools/agent/web-jest.sh",
      "roles": ["coder", "tester"],
      "use": "run web jest tests",
      "usage": "WORKTREE [--repeat N] TEST_PATH...",
      "output": "per-file counts; last line exit=N log=PATH"
    }
  ]
}
```

| Field | Rule |
|---|---|
| `name` | lowercase letters, digits, dashes; unique |
| `path` | absolute, or `$TP_ROOT/...` (the root of this repository, wherever it is checked out). Must exist and be an executable file. |
| `roles` | roles that get the entry (names of `agent/roles/*.md`); an unknown role is an error |
| `use`, `usage`, `output` | one line each, no control characters. `name + use + usage + output` may not exceed 150 bytes (aim for about 120). |

All fields are required; unknown fields are an error.

Rules:

- **No manifest: no block.** The repository text is byte-identical to the preamble alone (golden test).
- **A manifest that cannot be used is an error, never a silent skip**: unreadable, not UTF-8, not JSON, a bad or duplicate
  entry, or a `path` that is missing or not executable. Every entry's path is checked whatever the worker's role, so a
  broken manifest is refused the same way for all roles.
- **Size cap.** The preamble plus the block must stay within the existing 8,000 bytes, checked for every role.

## What the block contains

```
# Project Tools

- web-jest: run web jest tests: /…/tmux-pi/tools/agent/web-jest.sh WORKTREE [--repeat N] TEST_PATH... -> per-file counts; last line exit=N log=PATH
```

One line per entry listed for the worker's role, and nothing else; with no entry for the role there is no block. The
standing rules every worker needs (scope, the read/search rule, permission prompts, report and RESULT_STATUS,
`tp-agent request`, `tools/agent/test-run.sh`) are not here: they are the worker contract, `agent/contract/worker.md`,
which every worker gets on every route, manifest or not.

Known portability limit: `safe_grep` uses `rg` and skips `.git` and gitignored files; on a machine without `rg` its
fallback grep does not exclude `.git` or gitignored files.

## The worker's system prompt

`python3 lib/preamble.py WORKDIR ROLE SOURCE --harness pi|claude [--state-dir DIR --parent ID]` prints what tp-spawn
puts in the worker's system prompt, in this order:

1. `# Worker Contract`: `agent/contract/worker.md` (first line `contract: worker vN`, at most 20 lines), with its tool
   names filled from `HARNESS_TOOLS` in `lib/preamble.py` for the harness.
2. `# Repository Instructions`: the project's `worker-preamble.md` and the role's section, when there is one.
3. `# Project Tools`: the block above, when the manifest lists an entry for the role.
4. `# Prior Task`: `Prior result: PATH; review report: PATH` only when the task continues an earlier one (`--parent`):
   the parent's `result.md`, and `report.md` of the newest review in the parent's `meta.json` (`reviews`, or `review`),
   or of its nearest retry ancestor. A file that does not exist is not named. Manifest or not.

tp-spawn then adds the machine's prompt for the model (`config/local.json` `prompts`), if any.

## Airtera

The airtera manifest is `airtera-monorepo/.tmux-pi/agent-tools.json`, untracked and git-excluded. It lists the three
scripts above for the roles that use them. The airtera-specific jest tweak (mapping `zod` to `zod/v4`) is a hook file kept
in the same `.tmux-pi/` and passed with `--config-hook`.
