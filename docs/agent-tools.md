# Agent tools: shared helper scripts and the "Tools" block

Workers repeat the same setup routines in every task: orient in the lane, run the pre-commit checks, run web jest. This
repository ships plain scripts for them (`tools/agent/`: `lane-info`, `pre-commit-gate`, `web-jest`, `test-run`), and a project lists the ones its workers should use in
`<project>/.tmux-pi/agent-tools.json`. `lib/preamble.py` renders that list as a "Tools" block in the worker's standing
instructions, filtered by the worker's role. Nothing here goes into a tracked path of the project: the scripts live in
this repository and take the worktree as an argument, and `.tmux-pi/` is git-excluded in the project.

## The scripts

Each script is one plain command with absolute-path arguments (the preamble forbids heredocs, `$(...)`, arrays and
loops). Each prints a short summary; its **last line is `exit=N log=PATH`**, with the full output in a log under
`${TMPDIR:-/tmp}/tp-tools/` (`log=-` when it stopped before it made one, e.g. a usage error).

| Script | Arguments | What it does |
|---|---|---|
| `pre-commit-gate.sh` | `WORKTREE [BASE_REF] [--require NAME...]` | Finds the files changed against the base (committed, uncommitted, untracked), then runs prettier `--check`, eslint on changed JS/TS, the typecheck of each touched app, whatever file in it changed (`deno check --allow-import` when it has `deno.json(c)`, on the changed TS files, or on all the app's TS files when something else changed or a file was deleted; a deletion, also the old path of a move, touches its app; else `tsc --noEmit` when it has `tsconfig.json`) and `gitleaks git --log-opts=BASE..HEAD`; when gitleaks is installed it also runs `gitleaks dir` over the uncommitted and untracked changed files (check `gitleaks-dirty`). One `name exit` line per check. `BASE_REF` defaults to env `LANE_BASE`, then the upstream branch. A tool that is not installed is `skipped (not found)` and does not fail the gate, unless `--require NAME...` names it (`typecheck` covers every `typecheck:APP`): then the gate exits 1 and says `required check missing: NAME`. When no check passed or failed the summary says `no check applied`. Writes a record (see Records). |
| `test-run.sh` | `WORKTREE [--timeout S] [--label NAME] -- CMD ARGS...` | Runs CMD in the worktree to completion or to a hard timeout (default 900 s; on timeout the process group is killed, the exit is 124 and the output says so), in the foreground: it never backgrounds and needs no polling. Output goes to the log; stdout gets its last 20 lines and a `record:` line. Exit = CMD's exit. Writes a record (see Records). |
| `lane-info.sh` | `WORKTREE [BASE_REF] [--fetch]` | Branch, HEAD, base ref and sha, merge-base, ahead/behind, `git diff --name-status BASE...HEAD`, uncommitted files, whether `node_modules` is a symlink, the deno path. Fetches only with `--fetch`. Prints the newest file of a `drizzle/` folder when one exists. |
| `web-jest.sh` | `WORKTREE [--repeat N] [--config-hook PATH] TEST_PATH...` | Runs jest from the package that owns each test with `--runTestsByPath --ci` and `TZ=UTC`; paths with parentheses are passed as one argument and logged quoted. Per-file counts; `--repeat N` prints one `run i exit` line per run, with that run's per-file counts under it. `--config-hook` is a project JS file (absolute path, kept in the project's `.tmux-pi/`) that exports `function (config, { worktree, packageDir })` and returns the jest config to use. Nothing project-specific is built in. |

Exit code of a script = the `N` in the last line.

## Records

`pre-commit-gate.sh` and `test-run.sh` each write a JSON record of what ran on which tree, so a reader can tell whether
the evidence still holds. Both name the tree they saw: `head` (the commit) and `tree`, the fingerprint of the working
tree: `git write-tree` of a temporary index filled by `git add -A` (tracked changes plus untracked, non-ignored files; the
real index is not touched; the same content gives the same sha).

- **Gate**, `tp-gate:v1`: `worktree`, `base {ref, sha}`, `head`, `tree`, `files` (the changed files), `checks`
  (`name`, `status` = `passed|failed|missing|not_applicable`, `exit` or null, whole `seconds`), `exit`, `started`,
  `finished`, `log`. `missing` = the tool is not installed; `not_applicable` = no config or no matching files. Written
  beside the log (same name, `.json`) and, when env `TP_TASK_DIR` is set, to `$TP_TASK_DIR/gate.json`; an earlier
  `gate.json` is kept as `gate-<timestamp>.json`. There is no aggregate per-app check: `prettier`, `eslint` and the
  typecheck are each recorded once per changed app (`prettier:APP`, `eslint:APP`, `typecheck:APP`), so an app without an
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

A project with no `.tmux-pi/agent-tools.json` gives its workers no Tools block at all. To start one, copy the example and
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

- **No manifest: nothing changes.** The worker text is byte-identical to what it was before the block existed (golden test).
- **A manifest that cannot be used is an error, never a silent skip**: unreadable, not UTF-8, not JSON, a bad or duplicate
  entry, or a `path` that is missing or not executable. Every entry's path is checked whatever the worker's role, so a
  broken manifest is refused the same way for all roles.
- **Size cap.** The preamble plus the block must stay within the existing 8,000 bytes, checked for every role.

## What the block contains

```
## Tools

Do not re-request an unchanged range you already have; fetch more or refreshed context when you need it (after an edit, after a compaction).
Read a range with the Read tool (offset, limit) or safe_read, not sed -n. Quote paths with ( ) [ ] or spaces.
tp-agent request --kind skill|tool|context --what DESCRIPTION --reason REASON
tp-agent message TASK --text TEXT replies to a question from Main; tools/agent/test-run.sh WORKTREE -- CMD runs a check and records it.
- web-jest: run web jest tests: /…/tmux-pi/tools/agent/web-jest.sh WORKTREE [--repeat N] TEST_PATH... -> per-file counts; last line exit=N log=PATH
Prior result: /…/tasks/coder-1/result.md; review report: /…/tasks/review-1/report.md
```

1. The four standing lines (every worker, whenever a manifest exists; not filtered by role): do not re-request an
   unchanged range (fetch refreshed context after an edit or a compaction); read a range with the Read tool or `safe_read`,
   not `sed -n`, and quote paths with `( ) [ ]` or spaces; how to `tp-agent request` a skill, tool or context; and
   `tp-agent message TASK --text TEXT` to reply to a question from Main plus `tools/agent/test-run.sh` to run and record a check.
2. One line per entry listed for the worker's role.
3. `Prior result: PATH; review report: PATH` only when the task continues an earlier one: the parent's `result.md`, and
   `report.md` of the newest review in the parent's `meta.json` (`reviews`, or `review`), or of its nearest retry ancestor.
   A file that does not exist is not named. This needs the caller to pass `--state-dir DIR --parent ID`
   (`python3 lib/preamble.py WORKDIR ROLE SOURCE --state-dir DIR --parent ID`).

## Airtera

The airtera manifest is `airtera-monorepo/.tmux-pi/agent-tools.json`, untracked and git-excluded. It lists the three
scripts above for the roles that use them. The airtera-specific jest tweak (mapping `zod` to `zod/v4`) is a hook file kept
in the same `.tmux-pi/` and passed with `--config-hook`.
