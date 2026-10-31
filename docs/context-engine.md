# Context engine: knowledge store (CX1)

The knowledge store holds short, reviewed notes about a project: conventions, decisions, lessons, constraints. A worker finds
the notes that apply to the files it is changing. Search is deterministic: scope globs, tags, then BM25 over title and body.
There is no model and no embedding. Other lanes (navigation, the five operations, wiring) build on this store; this page
covers the store, the call log, the screens and the `tp-ctx` command.

## Where notes live

```
<project>/.tmux-pi/knowledge/
  approved/<id>.md      reviewed by a person; the only notes a worker is briefed with
  candidates/<id>.md    offered, not yet reviewed; never counted in a brief
  superseded/<id>.md    replaced by a later note; kept, readable by id, never returned by a search
  sources.json          {"sources": [{"id": ..., "path": ...}]} documents notes may cite
<project>/.tmux-pi/ctx-cache/knowledge-index.json    a cache, safe to delete
```

A note's status must match its directory. Editing `status:` in place does not approve anything; the note is reported invalid.

## Note format

Markdown with a frontmatter block that is a strict subset of YAML: `key: value` lines, values are plain words, quoted
strings, `[a, b]` lists or `{k: v}` maps; `#` starts a trailing comment. Anything else is rejected, and an error names the rule
and never repeats the value.

| key | rule |
|---|---|
| `id` | `kn-` then lower case words joined by dashes (the tool makes `kn-YYYY-MM-DD-slug`); at most 80 characters |
| `kind` | `convention`, `framework`, `relation`, `decision` or `lesson` |
| `title` | optional, short; the first body line is used when absent |
| `scope` | 1 to 10 globs (below) |
| `tags` | optional, at most 10; lower case letters, digits, `_`, `.`, `-` |
| `versions` | optional `{package: range}`; a range such as `>=0.45 <0.46`, `^1.2`, `~1.2.3`, `1.x`, `a \|\| b` |
| `source` | `human`, or starts with `review:`, `doc:`, `work:` or `worker:` |
| `evidence` | required for an approved note and for every new candidate; up to 10 entries such as `path:line` or `review:<id>#<finding>` |
| `supersedes` | optional ids of approved notes this one replaces |
| `status`, `created`, `last_reviewed` | set by the tool |

The body is at most 1200 characters. An unknown or repeated key is an error.

## Scope globs and ranking

A glob is a relative posix path. `*` and `?` stay inside one segment; `**` as a whole segment stands for any number of
directories (a trailing `**` needs at least one more segment). No `..`, no leading or trailing slash, no control characters,
no `[]{}!\`.

Notes that match are ordered by (1) scope specificity: an exact path, then a pattern anchored in a literal directory, then a
repo-wide pattern, with ties broken by more literal leading directories and then fewer `**`; (2) overlap with the asked tags;
(3) BM25; (4) id. Same store and same question give the same list. Selectors (path, tag, query) are ANDed.

## Freshness

Computed on every read and never stored. A flag does not hide a note; it tells the reader how far to trust it.

| flag | meaning |
|---|---|
| `stale_by_version` | a `versions` range does not hold for the version the lockfile pins |
| `version_unchecked` | no pin was available (the lockfile reader is L5's `localdocs`; without it versions are not checked) |
| `stale_by_path` | a scope glob matches no file, or evidence names a file that is gone or a line past its end |
| `age_days` | more than 180 days since `last_reviewed` |

## Call log

When `TP_TASK_DIR` is set (a worker) or `--log FILE` is given, every `tp-ctx` command except `approve` appends one JSON line to
`ctx-calls.jsonl`: `ts, tool, args_hash, args, status, bytes, ms, notes_returned, index_age_s` and, when there is one,
`reason`. It fails closed: with no usable task directory (unset, relative, not a directory, a symlink), or a log that cannot be
written (including a symlinked `ctx-calls.jsonl`), the command prints nothing from the store and exits 3. Arguments are
redacted before they are logged and before they are hashed. Run by a person with neither set, a command is unlogged.

## Screens

No new patterns. `redact.py` calls the existing ones:

- writes: `work_promote.screen` (private tag or fence, secret-like values). A refusal gives the rule and a count, never the text, and writes nothing;
- output: `work_find.guard`, the same boundary `tp-agent find` uses;
- path arguments (`redact.check_path`): inside the project after symlinks resolve, and not a sensitive path (`.env*`, `config/local.json`, `agent/auth.json`, keys).

## `tp-ctx`

```
tp-ctx status
tp-ctx knowledge list [--status approved|candidate|superseded|all] [--tag T]
tp-ctx knowledge show ID
tp-ctx knowledge add --kind K --scope GLOB... --evidence E... --text T [--title T] [--tag T...] [--version PKG=RANGE...] [--supersedes ID...]
tp-ctx knowledge import --from-work ID --scope GLOB... --evidence E...
tp-ctx knowledge audit
tp-ctx knowledge approve ID
```

`--project DIR`, `--log FILE` and `--json` work anywhere on the line. The project is `--project`, `$TP_CTX_PROJECT`, or found
from the working directory (`.tmux-pi` or `.git`). Exit codes: 0 answered, 1 no answer (not found, invalid), 2 usage or
refusal, 3 not logged.

`add` and `import` only write candidates. A new file is created without overwriting, so an existing id is an error. `import`
accepts a work-store note tagged `lesson` that is not tagged or fenced private.

`approve` is for a person. It refuses when `TP_TASK_DIR` or `TP_TASK_ID` is set, and refuses unless stdin and stdout are a
terminal. It re-screens the file, prints the diff, and moves the candidate only after the note id is typed. Notes listed in
`supersedes` move to `superseded/` in the same step; if any is missing, nothing changes.

## For other lanes

`ctxengine.search.query(layout, query=, paths=, tags=, include_candidates=, limit=)` returns `Result.hits`. Calls go through
`ctxengine.calls.call(tool, args, run)`. Path arguments go through `redact.check_path(project, path)` and output through
`redact.guard`. A new `tp-ctx` command lives in a module named in `PLUGINS` in `bin/tp-ctx` that exposes `COMMANDS` (the first
words it handles) and `main(argv)`.
