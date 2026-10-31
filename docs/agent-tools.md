# Agent tools: shared helper scripts and the "Tools" block

Workers repeat the same setup routines in every task: orient in the lane, run the pre-commit checks, run web jest. This
repository ships three plain scripts for them (`tools/agent/`), and a project lists the ones its workers should use in
`<project>/.tmux-pi/agent-tools.json`. `lib/preamble.py` renders that list as a "Tools" block in the worker's standing
instructions, filtered by the worker's role. Nothing here goes into a tracked path of the project: the scripts live in
this repository and take the worktree as an argument, and `.tmux-pi/` is git-excluded in the project.

## The scripts

Each script is one plain command with absolute-path arguments (the preamble forbids heredocs, `$(...)`, arrays and
loops). Each prints a short summary; its **last line is `exit=N log=PATH`**, with the full output in a log under
`${TMPDIR:-/tmp}/tp-tools/` (`log=-` when it stopped before it made one, e.g. a usage error).

| Script | Arguments | What it does |
|---|---|---|
| `pre-commit-gate.sh` | `WORKTREE [BASE_REF]` | Finds the files changed against the base (committed, uncommitted, untracked), then runs prettier `--check`, eslint on changed JS/TS, the typecheck of each touched app (`deno check` when it has `deno.json(c)`, else `tsc --noEmit` when it has `tsconfig.json`) and `gitleaks git --log-opts=BASE..HEAD`. One `name exit` line per check. `BASE_REF` defaults to env `LANE_BASE`, then the upstream branch. A tool that is not installed is `skipped (not found)` and does not fail the gate. |
| `lane-info.sh` | `WORKTREE [BASE_REF] [--fetch]` | Branch, HEAD, base ref and sha, merge-base, ahead/behind, `git diff --name-status BASE...HEAD`, uncommitted files, whether `node_modules` is a symlink, the deno path. Fetches only with `--fetch`. Prints the newest file of a `drizzle/` folder when one exists. |
| `web-jest.sh` | `WORKTREE [--repeat N] [--config-hook PATH] TEST_PATH...` | Runs jest from the package that owns each test with `--runTestsByPath --ci` and `TZ=UTC`; paths with parentheses are passed as one argument and logged quoted. Per-file counts; `--repeat N` prints one `run i exit` line per run. `--config-hook` is a project JS file (absolute path, kept in the project's `.tmux-pi/`) that exports `function (config, { worktree, packageDir })` and returns the jest config to use. Nothing project-specific is built in. |

Exit code of a script = the `N` in the last line.

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

Read a file once and keep notes; re-read only after you edit it.
To ask for a skill, tool or context you lack: tp-agent request --kind skill|tool|context --what DESCRIPTION --reason REASON
- web-jest: run web jest tests: /…/tmux-pi/tools/agent/web-jest.sh WORKTREE [--repeat N] TEST_PATH... -> per-file counts; last line exit=N log=PATH
Prior result: /…/tasks/coder-1/result.md; review report: /…/tasks/review-1/report.md
```

1. The two standing lines (every worker, whenever a manifest exists).
2. One line per entry listed for the worker's role.
3. `Prior result: PATH; review report: PATH` only when the task continues an earlier one: the parent's `result.md`, and
   `report.md` of the newest review in the parent's `meta.json` (`reviews`, or `review`), or of its nearest retry ancestor.
   A file that does not exist is not named. This needs the caller to pass `--state-dir DIR --parent ID`
   (`python3 lib/preamble.py WORKDIR ROLE SOURCE --state-dir DIR --parent ID`).

## Airtera

The airtera manifest is `airtera-monorepo/.tmux-pi/agent-tools.json`, untracked and git-excluded. It lists the three
scripts above for the roles that use them. The airtera-specific jest tweak (mapping `zod` to `zod/v4`) is a hook file kept
in the same `.tmux-pi/` and passed with `--config-hook`.
