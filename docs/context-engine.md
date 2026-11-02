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
| `evidence_hashes` | optional `{entry: hash}`: a hash of the cited line for each `path:line` entry, taken by the tool when a candidate is written; never set by the caller (see `add` under `tp-ctx`) |
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
| `stale_by_path` | a scope glob matches no file, or evidence names a file that is gone (`evidence_missing`), a line past its end (`evidence_moved`), or a line whose text is no longer the cited text (`evidence_changed`) |
| `age_days` | more than 180 days since `last_reviewed` |

## Call log

Every `tp-ctx` command except `approve` appends one JSON line to `$TP_TASK_DIR/ctx-calls.jsonl`: `ts, tool, args_hash, args,
status, bytes, ms, notes_returned, index_age_s` and, when there is one, `reason`. It fails closed: with no usable task
directory, or a log that cannot be written, the command prints nothing from the store and exits 3. Unusable means: unset,
relative, not spelled as its own normalized path (`..`, `.`, a repeated or trailing slash), not a directory, or a symlink; or a symlink anywhere in its path (for `<state>/tasks/<id>`, the state directory, `tasks`
and `<id>` must be real directories; for any other spelling, no ancestor may be a symlink owned by the calling user). The path
is examined again when the line is written. A symlinked `ctx-calls.jsonl` is refused. Arguments are redacted before they are
logged and before they are hashed.

Only a person's commands may do otherwise: `list`, `show`, `audit` and `status` (and `approve`, which logs nothing). With
neither `TP_TASK_DIR` nor `TP_TASK_ID` set, and only when stdin and stdout are a terminal, they may run unlogged, and `--log
FILE` writes their line to a file of the person's choice. With no terminal they need a usable `TP_TASK_DIR` like every other
command. `add`, `import` and the query operations (brief, symbol, relations, knowledge search, note) always need a usable
`TP_TASK_DIR`. With either variable set the shell is a worker's: `--log` is refused
and the call is logged in the task directory like any other.

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

`--project DIR`, `--log FILE` (operator commands on a terminal only, see above) and `--json` work anywhere on the line. The project is `--project`, `$TP_CTX_PROJECT`, or found
from the working directory (`.tmux-pi` or `.git`). Exit codes: 0 answered, 1 no answer (not found, invalid), 2 usage or
refusal, 3 not logged.

`add` and `import` only write candidates. A new file is created without overwriting, so an existing id is an error. Each
`file:line` evidence entry is stored with `evidence_hashes`, a hash of the cited line taken from the tree when the candidate is
written (not taken from the caller); later reads flag the note `stale_by_path` when that line's text changes. A note without
hashes (written by hand or before this field) is checked for the file and line count only. `supersedes` lists each id once. `import`
accepts a work-store note tagged `lesson` that is not tagged or fenced private.

`approve` is for a person. It refuses when `TP_TASK_DIR` or `TP_TASK_ID` is set, and refuses unless stdin and stdout are a
terminal. It re-screens the file, prints the diff, and moves the candidate only after the note id is typed. Notes listed in
`supersedes` move to `superseded/` in the same step; if any step fails, every step already taken is undone and the
store is as it was.

## The context tools (`ctx_brief`, `ctx_symbol`, `ctx_relations`, `ctx_knowledge`, `ctx_note`)

`tp-ctx --json <tool> --input <json>` answers `{status, text, truncated, left_out}` in at most 6144 bytes; `agent/extensions/ctx.ts` (pi) and
`claude/mcp/ctx.mjs` (Claude) are thin wrappers over it, loaded only when `TP_CTX_TOOLS=1`. What a worker can rely on:

**Turning them on.** `export TP_CTX_TOOLS=1` before starting main. `bin/tp-spawn` then writes `export TP_CTX_TOOLS=1` into the run.sh of
every worker main spawns (there is no per-task flag, and `--worker-env` refuses the key), and a worker's `bin/tp-pi` loads
`agent/extensions/ctx.ts` / `bin/tp-claude` adds `claude/mcp.ctx.json` (shared settings pre-allow the five `mcp__tmux-pi-ctx__*` tools). Only a
task gets them: both launchers need `TP_TASK_DIR` too, because every call is logged to `$TP_TASK_DIR/ctx-calls.jsonl`; main itself does not.
Known limit: `ctx_note` writes a candidate into the worker's own project store; a worker in a git worktree writes to that worktree's
`.tmux-pi`, not main's, so its candidates stay there until someone imports or approves them from that worktree.

- **Paths.** Every path a navigator returns (importers, imports, contract consumers, definitions, references, listed symbols) goes through
  the same path policy as the arguments before it is shown. One that is sensitive, outside the project, a link out of it, or a `<withheld>`
  placeholder is never named: it is counted in `left_out` (`N withheld`).
- **Truncation.** When a navigator says it cut a list (`truncated`), the answer text for that list never states a total, a zero or an
  absence, in any operation and for every list (definitions, references, importers, imports, exports, symbols, contract consumers, the
  shared contracts among the imports). One helper words it (`surface.list_head`, `surface.more_note`): "label (at least N shown, list cut by
  the navigator)" when rows were shown, "label: not found in the part the navigator returned (list cut)" when none were, and a `left_out`
  entry that says the list was cut and gives no count. A list that was not cut states its total ("5 of 7") and its absence ("nothing imports
  X") only when the source was read completely. **The rule: an absence is stated only when the read succeeded, nothing was withheld and
  nothing was cut.** Every list or search carries a `surface.Completeness` (`ok`, `withheld`, `cut`, `unread`, `why`, `status`) that one
  mapper, `surface.answer_read`, derives from the whole navigator or search answer, never from fields picked at a call site: the status
  must be `ok`; every field that signals loss (unresolved, refused, withheld, omitted, truncated/cut, skipped, errors, invalid, ignored
  config) makes the read incomplete; any non-empty field not on the explicit `surface.HARMLESS` list counts as incomplete, so a field a
  navigator adds later fails closed. `Got.completeness()`, `surface.notes_read`, `locate` and the brief's reads all go through it; a test
  fails if `Completeness(...)` is built anywhere but the mapper. Every row list is read through `surface.listed_rows`: a row that
  is not an object, or a list that is missing, null or not a list, is counted as unreadable; a `total` that is not a whole number,
  or above the rows handed over, makes the read not whole; a row path that is not text is withheld, never shown. An ok answer
  whose `target` or `path` names another file, or that does not echo the `direction` asked for, is an `error`. The text scan
  (`surface.declaration_pattern`) also finds export renames, destructured bindings, `export * as`, `import x =`, a first line with
  a byte order mark and names with `$`; test files are looked for in every code extension; an `import(...)`/`require(...)` whose
  module is not a plain string makes the libraries line "none can be shown". A status other than `ok` reads "not available (the navigator answered
  STATUS)"; a list the navigator did not flag as cut but that was not whole reads "not all returned", never "cut". One helper (`surface.absent`, used by
  `surface.list_head`) words every absence in `brief.py` and `surface.py` from it. A read that failed, was unavailable or never ran (a
  navigator down, a file no package covers) says "not available", never "none". If the read succeeded but items were refused, withheld or
  not read, the line says "none can be shown (N withheld, ...)" and never an absence, a zero or "none found". This covers lists (importers,
  imports, exports, contract consumers, definitions, references), the shared contracts of `ctx_brief` (the imports of every briefed file
  must have been read; one that was not gives "shared (lib) contracts: not available" or, when a contract was found, a warning naming the
  file), the `tests:` line (a test file at a refused path), the name search of `ctx_symbol` and `ctx_relations` (code files the scan could not
  read), the notes (candidates not shown, note files that failed to parse) and the libraries (a briefed file too large to read, or a cut import
  list). A test fails if either file holds an absence string outside the helper (the allowlist holds whole strings, not fragments), or if a
  helper call's completeness argument is not built from a read result. The contracts view says "only N imports were read, the navigator cut its list" instead of "no shared contracts". `ctx_symbol` with no
  `path` gives the details of each file that declares the name, up to 5; `ctx_relations` with a bare symbol declared in more than one file is
  `invalid` with a next step (pass a path).
- **Contract consumers** are marked `[reader]`, `[writer]`, `[reader, writer]`, `[uses]` (imports it, does neither) or `[unread]` (the file
  could not be read). A text scan of the consumer's own import statements (calls such as `.parse(`, `.extend(`, `import type`, `export * from`), not
  type analysis.
- **Notes.** `ctx_knowledge` shows each note's whole body (up to `store.BODY_MAX`, 1200); `ctx_brief` shows the first 300 characters of the body
  after the title, and names the notes it cut in `left_out`.
- **`ctx_note`** writes a candidate and answers `result: {id, status: "candidate", path}` in the `--json` payload (pi: the tool's `details`; MCP:
  `structuredContent`). The five-per-task limit, the write and the log line are one step under an exclusive lock on
  `$TP_TASK_DIR/ctx-calls.lock` (`calls.call(..., exclusive=True)`; `knowledge add` and `import` use it too). The log is checked to be appendable
  before the write, and a line that still cannot be written takes the candidate back (`Answer.undo`), so a candidate never exists without its
  line. A crash between the write and the line can still leave a candidate that is not logged; `knowledge list --status candidate` shows what is waiting.
- **`ctx_brief`** with no requested path present is `invalid` with a next step; with some missing it still briefs the rest and ends with a
  `Next:` line naming the missing ones.

## For other lanes

`ctxengine.search.query(layout, query=, paths=, tags=, include_candidates=, limit=)` returns `Result.hits`; `Result.candidates_counted` is the number of candidates that passed every selector (path, tag, text) but were not asked for. Calls go through
`ctxengine.calls.call(tool, args, run)`, which needs a usable `TP_TASK_DIR` (pass `operator=True` only for a person's command). Path arguments go through `redact.check_path(project, path)` and output through
`redact.guard`. A new `tp-ctx` command lives in a module named in `PLUGINS` in `bin/tp-ctx` that exposes `COMMANDS` (the first
words it handles) and `main(argv)`.
