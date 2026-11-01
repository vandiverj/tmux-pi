"""Navigation for a Deno project, as the context engine's API half (CX3): who imports a file, what a file imports, and what it
exports.

Three sources, each used for what it is good at:
  - `graph.ImportIndex` (pure Python, incremental, persisted): importers and direct imports. A warm call costs a directory walk;
    a cold one about a second per thousand files.
  - `deno doc --json`: the exported symbols of one file (name, kind, definition `path:line`, the source line as the signature, the
    first doc-comment lines), cached by the content hash of every file the walk reached.
  - `deno info --json`: the forward module graph of one entry, capped at 30 s. Used when the index cannot be built, or when asked for.
    Its graph is cached on disk (`nav_api-info-<key>.json`), keyed by the content hash of every file and config the walk reached,
    the entry, the Deno version, the flags and the format. A hit is filtered as a fresh result is, listed, screened and proved last
    (a changed file gives `stale_index`); with no sandbox there is no hit. An entry (this one and the `deno doc` one) is JUDGED before the final proof and WRITTEN
    after it, and never removed by a call that writes: before the proof (`_final`) the entry's name and a unique temporary name are asked
    of the guard once (the cache folder was judged when the cache was made) (a refusal means no store); the proof runs; only an answer that is still `ok` then writes the temporary file
    and renames it, with no guard and no project code, a failure being swallowed (entries unchanged; a stray `.tmp` is possible
    and is swept by the cache sweep). A refusal, gap, stale answer, error or exception
    therefore writes nothing, and no call that writes deletes an entry. A planted entry can name only files this walk proved and the guard allows.
    The cache folder is judged as for `deno doc`; a refused one means no cache.
  - The cache sweep (`graph.CacheDir.sweep`, run by `_swept` after a call that WROTE an entry, never after a hit, and after the answer
    is final, so nothing it does can change the answer; every failure of it is swallowed): at most once an hour per folder, it removes
    stray `.tmp` files older than 10 minutes, entries unused for 14 days (index files 30), then the oldest while the folder holds more than
    64 MB or 400 files, within 200 deletions or 50 ms. A hit that accepted its payload touches the entry (once an hour at most), which is
    what "used" means. It lists and deletes only through the judged folder's descriptor, only managed names that are regular files, each
    judged by the guard and looked at again just before its unlink. `status()` reports `cache`: entries, bytes, the last sweep and
    `incomplete`. All of it is set out in "Cache" below.

Cache. (Every statement here is about `graph.CacheDir`, `graph.ImportIndex` and this module's `_forward_graph`, `_symbols`,
`_swept` and `status`; the tests that hold them are in tests/test_ctx_nav_api.py: InfoCacheCase, EnvelopeCase, SweepCase, ShapeCase,
PathRuleCase.)
  What is cached. Three things, nothing else. No answer of `importers` or `imports` from the index is stored, and neither are the
  resolved edges (they are made again from the scanned specifiers on every refresh).
    - The index: one file per root, `nav_api-index-<16 hex>.json` (the first 16 hex digits of a digest of the project root, the
      boundary, the root and the config path), holding for each source file its scanned specifiers
      (`[specifier, line, type_only, dynamic]`) under the key `[mtime_ns, size]`. `ImportIndex.save` writes it from `refresh`
      whenever a file was new, changed or went, before the proof, so a call that ends in a non-`ok` answer may still have saved it: what
      it holds is only what a successful read gave, never an answer. If that write fails (no cache, a link at the name, I/O) it is
      swallowed: `save` returns False, the file is as it was, the answer is unchanged and only the next start is cold. It is loaded
      when the index is made (once per `NavApi`).
    - `deno doc`: `nav_api-doc-<40 hex>.json`, the list of symbols (name, kind, path, line, signature, doc) of one file, keyed by a
      digest of the file's name, `Reach.digest()` (the content of every file the walk reached, and the state of the configs and
      `package.json` files) and the Deno version.
    - `deno info`: `nav_api-info-<40 hex>.json`, `{format: INFO_FORMAT, external, modules: [{path, imports}]}`, keyed by a digest of
      the entry, `Reach.digest()`, the Deno version, `INFO_FLAGS` and `INFO_FORMAT`. A key holds content hashes, never a time, a
      size, an inode or a ctime. The Deno version in a key is the first line of `deno --version`, read once per `NavApi`.
  Where. In `cache_dir` (default `<project root>/.tmux-pi/ctx-cache`), a `graph.CacheDir` judged once when the `NavApi` is made: inside
  the project, not the project itself, allowed by the guard (also where a link leads), else there is no cache, and an `ok` answer
  carries `cache_refused`. An entry is a plain `.json` name (no `/`, not hidden) directly in that folder. The folder is made, mode
  0700, by a write attempt and by nothing else (`CacheDir.put_json` opens it with make=True, so an attempt that then fails to write
  can still leave the folder; a read, a touch, a sweep and `status()` make nothing); it is opened one component at a
  time below the boundary with no link followed, and must be the judged folder by the kernel's name each time.
  The envelope. Every entry is `{"format": ENVELOPE_FORMAT, "name": <its file name>, "sha256": <hash of the payload as compact,
  key-sorted JSON>, "payload": ...}` with those four keys and no others (`CacheDir.read_json`, `put_json`: the only reader and writer
  of an entry). It is read with `graph.read_bounded` (like any file, and at most MAX_CACHE_BYTES, 64 MB). Anything else is a MISS (None,
  never an error): not there, a link or a folder at the name, not UTF-8 or JSON, too deep to read, over the size, another format, another
  name, a payload that does not hash to `sha256`. The checksum guards against corruption, not a writer who can compute it. A payload is
  then held to the shape its reader writes, and a payload of another shape is a miss too, replaced by the next write: an index entry
  (`ImportIndex._entry_shape`: a key of two whole numbers, a modification time and a size from 0, both within 64 bits, and rows of text, a line from 1
  and two flags; one entry that is not is read again, the others stay), a doc list (`NavApi._doc_item_shape`: text for name, kind,
  path, signature and doc, with name, kind and path not empty and signature and doc no longer than their cut, and a line from 1 within 64 bits), an
  info graph (`NavApi._info_local`: the whole-number format, a whole `external` from 0, a list of modules). A value of the right type
  outside the range its writer can make (a negative size, line 0 or -1, an empty path, a float or a bool for a whole number) is a miss. Inside an info graph or a doc list,
  a module or symbol that names a file this walk did not prove, or one the guard refuses now, is dropped, and so is an info module
  record that is not a path with a list of imports, and an import that is not a proven name.
  The freshness proof. A stored `deno doc` or `deno info` entry is not an answer. To find its key `prove_reach` reads every file the
  walk reaches and hashes it with the configs; a hit is then filtered as a fresh result is (only names this walk proved and the guard
  allows), listed, screened, and ended by `Reach.holds()` as the last step, so a file or config that moved after the key gives
  `stale_index`; with no sandbox there is no hit (`sandbox_unavailable`) and with no `deno` there is none either. An index answer
  does not rest on the cache: the walk at its start and the walk of its final proof are made every time, a loaded entry is used only
  for a file whose `(mtime_ns, size)` is the walk's and that is then opened, and a file that changed is read again.
  Writing. An entry is JUDGED before the final proof (its name and a unique temporary name, `<name>.<16 hex>.tmp`, are asked of the
  guard) and WRITTEN after it, only if the answer is still `ok` (the index file excepted, see above): a temporary file made with
  O_EXCL, never through a link, then renamed over the name. The rename is the commit point: before it any failure leaves every entry
  as it was (a stray `.tmp` is possible), after it nothing can fail the write. A writer never deletes anything, and two writers of one
  key write the same bytes. A write that fails is swallowed and the answer is the same.
  Using an entry. The caller that accepted the shape of a payload touches the entry (`CacheDir.touch`: the time of the last use, for
  the age rule), after it was read and before the final proof, so an answer that then ends `stale_index` has touched it. A touch is
  made only when the entry's modification time is at least an hour old or in the future (TOUCH_EVERY_S); it looks at the name in the
  folder's descriptor, sets the time with `follow_symlinks=False` only on a regular file, and fails silently, changing nothing and
  not affecting the answer (a link, a folder, a name the guard refuses, no such file, no permission). Reading touches nothing, so an entry no caller accepts ages out.
  The sweep (`CacheDir.sweep`, run by `NavApi._swept` after `importers`, `imports`, `forward_graph` and `symbols`, after the answer is
  final). Due only if this folder's `CacheDir` wrote an entry since it last asked (`written`; a hit never writes), the folder is judged
  and exists, this object has not swept within an hour, and the marker `nav_api-sweep.json` does not show a sweep within an hour (a
  marker that is absent, not an envelope of this format, a time that is not a finite number of at least 0, or dated in the future holds nothing
  back and is written again). A sweep removes: a plain `*.tmp` file older than 10 minutes (TEMP_MAX_AGE_S, by age only, whoever made
  it); a doc or info entry whose modification time is older than 14 days (ENTRY_MAX_AGE_S); an index file older than 30 days
  (INDEX_MAX_AGE_S); then, while the entries that are left (index, doc and info files: temporaries, the marker and every other name
  are not counted) are more than 400 (SWEEP_CAP_FILES) or hold more than 64,000,000 bytes (SWEEP_CAP_BYTES), the oldest by
  modification time (then by name) first. It stops after 200 deletions or 50 ms (SWEEP_MAX_DELETIONS, SWEEP_BUDGET_S), counted over
  listing, judging and deleting together. After the deletions the sweep tries to write the marker, which records
  `{version, last_sweep, deleted, skipped, complete}`; `complete` is False when the budget stopped it or a name could not be looked
  at. The marker write is best effort, like any entry write: if the guard refuses `nav_api-sweep.json` (or its temporary name) or the
  write fails (I/O, a link at the name), it is swallowed, the sweep still returns its result and keeps what it deleted, no marker is
  made or changed (`last_sweep` stays None or at its earlier value), and the next call that writes sweeps again (this object waits an
  hour, another process may not). The marker write does not make a sweep due again. The safety rule: it opens the folder only by the
  judged name with nothing made, lists and deletes only through that descriptor, takes names only from the listing and the kind of
  each from its own `lstat` there, touches only the names `nav_api-(index|doc|info)-*.json` and `*.tmp` and only if they are regular
  files (a link, a folder, a FIFO, a hidden or foreign name and the marker are neither counted nor deleted), asks the guard about each
  name before it is deleted, and looks again just before each unlink: the name must still be a regular file with the device, inode
  and modification time that were listed, else it is skipped. A sweep never raises and never changes the answer it follows. A folder
  that is refused or missing is not swept.
  `status()["cache"]`: `{"entries": int or None, "bytes": int or None, "last_sweep": float or None, "incomplete": bool}`. Read-only
  (nothing is made, a missing folder is `0`, `0`, `incomplete` False), and each field comes from its own source, so one failing
  never hides another.
    - `entries` and `bytes`: the index, doc and info files that are regular files (temporaries, the marker, links, folders and other
      names are not entries), in the same listing as the sweep's but with no time limit. They are None, and `incomplete` is True,
      whenever they are not all known: the cache is refused, the folder cannot be opened or is not the judged one, it cannot be
      listed, or a listed name could not be looked at for a reason other than having gone since the listing. A name that only went
      is not an error and is not counted. A count of what could be seen is never reported as the whole.
    - `last_sweep`: the time (seconds since the epoch, always a float: a whole number in the marker is read as one) in the marker. None only when the marker cannot be read: no cache, no marker
      (never swept, or swept but the best-effort marker write was refused or failed), a link or a folder at its name, not an envelope of this format, a time that is not a finite number of at least 0, or a folder
      that cannot be read (a folder of mode 0 hides its marker, so it reports None, with `entries` and `bytes` None as well). A time
      in the future is reported as it is.
  Known limits of the cache (as the code behaves; see also "Known limits" below).
    - A change after the final proof returns, or one that keeps the mtime, size, inode and ctime of a file, is not seen. A source
      rewritten with the same mtime and size is not seen by the index (its key has no content hash).
    - An entry is only as safe as the cache folder. The folder is judged and the content is filtered again on every read, but another
      process running as the same user can still plant a wrong list that names only files inside the proven set (or a wrong set of
      specifiers in an index file), with a checksum it computed, and be believed.
    - A time cap or an eviction by the size cap can cost one cold answer: a sweep can remove an entry a call is about to read (a miss),
      and a long-lived instance keeps using the index it holds in memory when another process's sweep removed its file, until its next
      write.
    - A clock that was ahead leaves an entry dated in the future. It is never old, so the age rule does not remove it and it goes last
      under the cap (the cap still bounds it); the first hit that touches it sets its time to now.
    - The residual replace race: between the look just before an unlink and the unlink itself nothing closes the window, so a name
      replaced then can be removed. It costs a cache miss, never a wrong answer (every entry is verified when read and every answer is
      proved when it is made).
    - Concurrent sweeps (two processes) can both report success for one unlink on macOS, so `deleted` in the marker can be over-counted.
    - Only a `NavApi` sweeps. An `ImportIndex` made with its own `cache_path` writes and touches its file but never sweeps its folder.
    - When the 50 ms budget stops the listing (a folder of thousands of files) the age and size rules apply only to the names listed
      so far, `complete` is False, and the rest waits for the next sweep, which is due at the earliest an hour later and only after a
      write.
    - A call that raises after writing an entry and before `_swept` leaves `written` set, so the next call, a hit included, sweeps once
      (still held by the hourly throttle). A process that only ever hits never sweeps: entries age out only while something writes.
    - `last_sweep` is None for a cache folder of mode 0 (the marker cannot be read there).
    - A plain `*.tmp` file of any name that is a regular file in the cache folder and older than 10 minutes is removed, not only the
      temporaries this module makes.
    - An entry over MAX_CACHE_BYTES can be written but is never read back (a miss each time). A doc entry has no format number of
      its own: only its types are checked, so a change of what `_parse_doc` stores that keeps the types does not change its key.

Gaps. An index answer (importers, imports from the index) is never `ok` when the index could not get part of its input. The split:
a file or folder the guard refuses BY ITS NAME is outside the index by policy (not listed, read or counted; its edges are not
named), and is not a gap. A refusal at read time, a path that turns out to be outside the root, a folder, file, entry or import
target that cannot be read or looked at, a name the guard could not judge because it could not look (`unreadable_path`: not a
refusal by name; `graph.CannotJudge`), a config or overlay that is refused, outside, unreadable, not JSON or not modelled (the
same configs the Deno side refuses), and a root that is refused are gaps (`graph.GAPS`). A gap that is a refusal gives `refused`
(with the Deno side's `reason`), a race (a file, folder or config that changed, went or was replaced between the walk, the read and
the answer) `stale_index`, anything else `error` with `reason`; each carries `gaps` ({code: count}, no paths) and no data. One gate
makes every index answer (`_index_answer`): ONE START SNAPSHOT AND ONE FINAL PROOF. The start is the index's refresh: one walk of the
tree (each source with its mtime, size, ino and ctime), the reading of the sources whose key changed, and an open for reading (no
byte read) of those kept by key that this instance has not already read or opened at exactly that fingerprint (so a warm answer
opens nothing, a cache-loaded one opens each source once); the answer is built from it. THE LAST PROOF IS THE LAST STEP: the
answer is screened (`_out`), and `_final` then runs `graph.ImportIndex.prove` as the very last thing before the answer is returned
(for the index as for `deno info`; the one rule is `_final`): the configs' states, the choice of the boundary, every import target
that was judged, and a new walk that must find the snapshot (the same files with the same fingerprints). Cold build, warm instance
and cache-loaded one alike; only an answer whose proof found nothing is `ok`. A proof that fails gives the gaps and nothing from
the answer. A race with the answer (anything that moves after the snapshot) is therefore `stale_index`, also for `imports` with a
`source` of `auto`, which falls back to `deno info` only when the index could not be built at all.
The guard is asked once per name per answer (a memo that belongs to exactly one public call, a nested or concurrent call having its own, and is dropped on every exit; a
guard that raises is a refusal and is remembered as one, `_permits`). Every name in a listing is asked of the guard BEFORE it is
asked its type (a type question can stat the name), except the names of `graph.SKIP_DIRS`, which are skipped by name first and never asked
of anything; only after the guard allows a name is one that can never be read as a source (not a folder, no source suffix) dropped
without being read or opened. Nothing is remembered between answers.
A source rewritten with the same mtime and size is not seen (the index's key; no content hash).
An input of the boundary decision (`_boundary_input`) is also seen when its mtime is put back, because its ctime and size are part of it. Of
those inputs the regular files inside the boundary are also content-hashed (sha256; `_boundary_hash`, the one exception to the refusal of
`.git` by name, see "The monorepo"), so a rewrite that keeps size, mtime and ctime is seen too. A `.git` FOLDER is a boundary input by
kind and identity only (its contents are never read, and ordinary git work changes them), so a change inside one is not a boundary change by design.
A change after the final proof returns is not detected.
A missing config is no gap (nothing is there; one that was there when its folder was walked and is gone at its read is a race),
nor is a missing import target (`missing`). `imports` with `source` of `auto` still falls back to `deno info`, which proves
its own reach. A link in the tree is not followed or indexed (as before): an edge through one is not in the index; this is a
policy limit of the walk, not a gap.

Every answer is a dict with a `status`: `ok`, `unavailable` (no `deno`, for the operations that need it), `no_project` (no such
directory), `refused` (a path outside the project, or a sensitive one), `bad_argument`, `not_found`, `stale_index` (a file changed
while it was being read; ask again), `timeout`, `error`. Anything other than `ok` carries a one-line `next_step`.

No network, ever: `deno info` and `deno doc` are run with `--no-remote --no-lock`, a registry that cannot be reached (`--no-remote` alone does not stop npm
downloads) and a minimal environment. `--no-lock` keeps `deno.lock` unwritten.

Boundaries. Every path argument goes through `check_path(project, path)`, and so does every file the index would read or an edge
would name: a refused file is never read, never indexed and never named in an answer. A specifier that names a path outside the
project or one the guard refuses, in any spelling Deno's URL parser accepts (`%2e%2e`, backslashes), is blanked wherever it would
be shown, whether or not a file is there. Every string in an answer, including one read back from the cache, passes through
`guard(text)` on its way out; a guard that raises gives an `error` answer.

The monorepo. When the project sits inside a monorepo, the boundary of every read is the monorepo root, not the project: the nearest
folder above the project that holds a `.git` and a workspace marker (`WORKSPACE_MARKERS`; existence only, never read). A root that
is `/`, is or leads to a sensitive folder, or holds the home folder is not taken (the boundary stays the project). Every check
(`check_path`, the path rule, the index, the walk, the mirror, the cache key) then works on names relative to that root, and the
guard is asked as `check_path(root, name)`; a file anywhere in the monorepo that passes the guard and the boundary check is
followed and mirrored like a project file. Paths in and out stay relative to the project (`../../lib/x.ts`). A specifier that
resolves outside the monorepo root stays `outside_project`, and nothing there is looked at. Importers cover the files of the Deno
root only. Only the workspace form the launchpad tree uses is modelled: `package.json` `workspaces` as a list of plain folder paths
inside that package's folder (a `workspaces` key that is present must hold such a non-empty list: `null`, `false`, `0`, `""`, `[]`,
`{}` and a mixed list are `unmodelled_config`, only an absent key is no workspace), no glob, negation, `..` or object form; `pnpm-workspace.yaml`, `lerna.json` or `rush.json` in the
config's folder or any folder up to the root, and any other form, are `unmodelled_config` (nothing is guessed). The `package.json` of
every folder from the config's folder to the root is read, cut down and mirrored, and is an input the final proof checks again. So is
the choice of the root itself (`_probe_boundary`): the `.git` of each folder looked at on the way up, every marker of the folder
chosen (`_boundary_input`: kind, identity, size, mtime, ctime, a link's target; nothing read), the sha256 of each of those that is a regular file
inside the boundary (`_boundary_hash`: a `.git` file of a worktree or submodule, by the one narrow exception to the refusal of `.git`; a marker
only if the guard allows its name): opened with O_NOFOLLOW and O_NONBLOCK, only if it is the regular file the stat saw, at most BOUNDARY_HASH_CAP
(4 KiB) bytes for a `.git` file and MARKER_HASH_CAP (64 KiB) for a marker, read only to be hashed, never returned, logged, mirrored or given to Deno. One that is larger, unreadable or swapped is
`unmodelled_config` for every answer. A link, a FIFO or a folder at such a name is never opened. Above the folder chosen nothing is opened. And
the home folder. A removed, added, replaced or changed one gives `stale_index` and no data
from the index (`ImportIndex.boundary_holds`) and from Deno (`Reach.boundary_holds`), warm, cold or cache-loaded.

The path rule (graph.py states it in full; a test holds every file-system call in both modules to a table): nothing is asked of the
file system about a path until its name is shown inside the project and allowed by `check_path`. That covers the path argument
(`stub_check_path` judges the name before it follows a link), the Deno root and config, every file and folder the index walks or an
import names, and the cache folder: `graph.CacheDir`, judged once when the instance is made, must be inside the project and allowed
(not `.git`, not a link to a refused or outside place); otherwise there is no cache, and `ok` answers say `cache_refused` with the
reason (`cache_dir_outside_project`, `cache_dir_refused`, `cache_dir_unreadable`). Every cache read is a `read_bounded` like any
other. A refusal never depends on whether something is there. What the sandbox grants (Deno's cache folder, the executable) is not a
project path: it is resolved once by `judged_location` (links followed only after each name is judged: nothing in a sensitive folder
is looked at) and refused (`sandbox_grant_sensitive`) when it is, or leads to, a sensitive folder (`.ssh`, `.git`, ...), holds the
home folder, or is not a folder (the cache) or a regular file (the executable); `deno` is looked up only in `PATH` folders judged the
same way. The folder a mirror or probe is made in (`mirror_parent`, else `TMPDIR`, else `/tmp`; never `tempfile.gettempdir()`, which
writes in a folder before anything judges it) is refused (`scratch_refused`) when it is, or leads to, a sensitive folder or is inside
the project; the mirror is made, written and removed with no link followed on the way.

Deno is run on a mirror of an allow-list, not on the project. `deno info` and `deno doc` start only after `graph.prove_reach` has
walked every module Deno would load from the target and shown each one allowed: the walk reads the files itself (so a file the
index does not hold, such as one under the config's `exclude`, is followed like any other), resolves each specifier through the
config's import map and scopes, and checks every path a specifier may name. If one is outside the project or refused, or the walk
meets something it cannot model, the call is refused with a `reason` (`outside_project`, `reaches_refused_path`,
`unmodelled_config`, `unmodelled_import`, `unreadable_file`, `too_many_files`, `precheck_failed`, and `sandbox_unavailable`,
`sandbox_failed`) and Deno is not started. Then
the walk's files are copied (the bytes the walk read, as regular files) into a temporary directory laid out like the project, with
a rewritten config (`imports`, `scopes`, `compilerOptions`, `unstable`; no `exclude`, `lock`, `tasks`, `vendor`) and a `package.json`
cut down to name, version, type and dependency tables, and Deno is run there with `--config` naming that config; the directory is
removed afterwards and the paths in Deno's answer are mapped back. So whatever the walk misses or misreads (a scanner that
misreads a `/`, an import-map entry of a shape it does not model) names a file that is not there: Deno cannot open a refused file
it was not given. The walk stays as the first check, so a refused file is reported by name-free `reason` before Deno starts.

Deno is also run inside an operating-system sandbox, always (`sandbox-exec` with `SANDBOX_PROFILE`): it may read the mirror, its own
executable and cache and the system libraries, may write nothing (its cache is read only), and has no network and cannot start
another program. So a path the mirror does not hold and the walk never saw (an absolute `file:///...` the scanner missed) cannot
be opened either. Every path the profile grants other than the mirror (Deno's cache directory, which the worker's environment names
with `DENO_DIR` or `XDG_CACHE_HOME`, and the Deno executable) is resolved once per launch (`judged_location`: links followed only
after each name is judged, so nothing inside a sensitive folder is looked at), and those resolved
strings are what is checked and what is handed on: the profile's parameters, `DENO_DIR` and the program that is run. Nothing is
resolved again after the check. If it equals, contains or sits inside the project, the Deno root, the config's folder, a monorepo root (the nearest
folder above the project with a `.git`, any with a workspace marker) or the mirror, the call is refused with `sandbox_grant_overlap`,
on every call, before Deno starts. If the sandbox cannot be applied (not macOS, no `sandbox-exec`, this process already in a
sandbox) the call is refused with the reason `sandbox_unavailable` or `sandbox_failed` and Deno is not started: there is no unsandboxed path, and a cached `deno doc`
answer is not served either. Importers and imports (the index) need no Deno and are unaffected.
Afterwards the answer is held to the same list: a module Deno lists that the walk did not reach refuses the answer
(`unproven_module`), and if any proven file, or the config or `package.json` the mirror's were made from (each one absent, unreadable or a content hash;
a change between any two of the three counts), changed while Deno ran there is no answer (`stale_index`). That check is also the
last step of every `ok` answer of `symbols`, `forward_graph` and `imports` from `deno info`, one served from the cache included:
it runs after the output screen, in the public method, and nothing runs between it and the return. A message of Deno's names
only files the walk proved (any other path is `<file>`).

Known limits (what is not proven):
  - A change after the final proof returns is not detected, and neither is a source or config rewritten with the same mtime and size
    (the index's key; no content hash). The boundary inputs are the exception (a `.git` file and the workspace markers are hashed), but a
    `.git` folder whose contents change is not a boundary change, by design, and a `.git` or marker above the folder chosen, or one the
    guard refuses by name, is known by its stat fingerprint only. The answer is a snapshot as of its last proof. A `package.json` or `deno.json` in a
    folder the walk reached outside the chain from the config's folder to the monorepo root is not read as a config.
  - The mirror keeps Deno from opening a project file it was not given; the sandbox keeps it from opening anything else. An import
    the scanner missed, whatever it names (`/etc/x`, `../../x`, `file:///abs/x`), is a path the process is not allowed to open: Deno
    lists the module with an `error` and no content, and the answer is refused (`unproven_module`). The sandbox is not proven
    beyond that: it is `sandbox-exec` (macOS Seatbelt, deprecated by Apple but present), so it exists only on macOS, and its profile
    is `SANDBOX_PROFILE`.
  - A `package.json` below the config's folder that the code imports as data is copied as it is (only the two Deno looks in
    beside the config and at the project root are cut down); the sandbox is what holds if one of its fields names a path.
  - `npm:`, `jsr:` and `node:` packages, and `http(s):` modules, are external: they are not followed, and Deno reads them from its
    own cache, which is not checked (the mirror has no `node_modules`). A `package.json` dependency that is a local path (`file:`,
    `link:`, `workspace:`) is refused; one in a directory above the config or the project root is not looked at.
  - A config with comments (JSONC), `importMap`, `workspace`, `links`, `patch`, `vendor`, a `nodeModulesDir` of `manual`, an unreadable
    map, an import map whose keys are paths (`./x`, `/x`, `file:`) or whose targets are `file:` URLs, or `sloppy-imports` is refused;
    nothing is guessed. Import-map matching is the specifier-prefix rule as implemented in `graph.ImportMap`, not Deno's own code.
  - Any `import(expr)` with an argument that is not a string literal, or `import.meta.resolve`, anywhere in the graph refuses the
    call, though Deno would not follow it either: the walk cannot tell. A source file over 2 MB refuses the call.
  - Modules named only by `new URL(..., import.meta.url)`, `Worker`, `require()` in CommonJS, or runtime reads are not followed; Deno
    does not load them for `info` or `doc` either, but that is Deno's behaviour, not something proven here.
  - There are two ways to read a file's bytes in this module and in `graph`, and a test holds both files to them. The way is
    `graph.read_bounded`: it opens the file, asks the kernel which file is open, checks that it is the path the caller judged,
    inside the project and allowed by the guard, and only then reads; a file that fails a check is never read. That covers
    sources, the Deno config, overlays, `package.json` files and both caches. The one exception is `_boundary_hash`, which reads a
    `.git` file (at most 4 KiB) or a workspace marker (at most 64 KiB) only to hash it, never returns, logs, mirrors or hands the
    bytes to Deno, and opens nothing but a regular file at an exact name in the boundary-input list (see "The monorepo"). A link swapped in between the check of a name and its read gives
    `stale_index` (the walk) or a gap (the index: no answer), and its bytes are not read. Where the kernel cannot name
    an open file (not macOS or Linux), nothing is read as checked. A hard link is one file under two names; the kernel may name
    either, so a hard-linked source file can be taken as changed (a refusal, never a wrong answer).
  - The mirror holds the bytes the walk read, so a file swapped for a link after the walk cannot reach Deno; a change to a file the
    walk read is still noticed afterwards (`stale_index`), so an answer is never given for a tree that moved.
  - Cleaning up a mirror (`graph.remove_own_folder`) holds the parent's descriptor but does not keep the identity of the folder it
    made: a process of the same operating-system user that swaps its own scratch folder for another between the make and the
    removal gets the other one removed. Accepted: that user can remove those files anyway, and nothing is read through it.
  - Case-insensitive file systems: a name that differs from a refused one only by case is the guard's to refuse.
  - The index (importers/imports) is not part of the proof; its edges ignore `@deno-types`/`@ts-types` pragmas and percent-encoded
    spellings, so an importer list can miss an edge Deno would see.

Until CX1 merges, `check_path` and `guard` here are STUBS (`stub_check_path`, `stub_guard`) with the signatures of CX1's
`redact.check_path(project, path) -> relative posix path | raises Refused` and `redact.guard(text) -> text`, so CX4 swaps them in
by passing `check_path=redact.check_path, guard=redact.guard` to `NavApi`. Any exception from `check_path` is a refusal."""
import contextlib
import contextvars
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from urllib.parse import unquote

try:
    from . import graph
except ImportError:  # run from this directory, not as part of the package
    import graph

DEFAULT_TIMEOUT = 30
MAX_IMPORTERS = 25
MAX_SYMBOLS = 20
MAX_MODULES = 200
INFO_FORMAT = 1  # the stored `deno info` graph (`NavApi._forward_graph`); a change of shape is a change of this number
INFO_FLAGS = ("info", "--json", "--no-remote", "--no-lock")
DOC_LINE_MAX = 400
SIGNATURE_MAX = 200
MAX_DENO_OUTPUT = 40_000_000
CACHE_RELATIVE = os.path.join(".tmux-pi", "ctx-cache")
DENO_ENV_KEEP = ("PATH", "HOME", "DENO_DIR", "TMPDIR", "LANG", "LC_ALL", "XDG_CACHE_HOME", "USER")
OFFLINE_ENV = {"DENO_NO_UPDATE_CHECK": "1", "NO_COLOR": "1", "NPM_CONFIG_REGISTRY": "http://127.0.0.1:1/",
               "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# The profile every Deno process runs under (macOS Seatbelt). Everything not named is allowed except what the first lines deny; the
# reads and writes are denied and then allowed again by name, so a path that is not listed cannot be opened, and nothing can be
# reached over the network. ROOT is the mirror, DENODIR Deno's cache (read only: Deno answers from a cache it cannot write, and a
# cache directory the worker's environment can name is not a place to grant writes), DENOBIN the Deno executable; they are passed
# as parameters (`-D`), never spliced into the text. `NavApi._sandboxed` checks every grant but ROOT before it builds the command.
SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network*)
(deny process-exec*)
(allow process-exec* (literal (param "DENOBIN")))
(deny file-read*)
(deny file-write*)
(allow file-read* (literal "/") (subpath (param "ROOT")) (subpath (param "DENODIR")) (literal (param "DENOBIN"))
    (subpath "/usr/lib") (subpath "/System") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") (literal "/dev/tty"))
(allow file-write* (literal "/dev/null"))
"""
SANDBOX_APPLY_FAILED = 71  # sandbox-exec's exit status when the profile could not be applied
SANDBOX_NEXT_STEP = "Deno is only run inside an operating-system sandbox (macOS sandbox-exec); importers and imports still work without it"
GRANT_NEXT_STEP = ("point DENO_DIR (and XDG_CACHE_HOME) at a directory outside the project and its repository, and keep the deno "
                   "executable outside them; importers and imports still work without Deno")
INDEX_NEXT_STEP = ("make the folders, files and config the index could not read readable, or use a config it can model; `gaps` counts "
                   "what was missed. imports from `deno info` do not need the index")
BOUNDARY_UNMODELLED = "unmodelled_config"  # a boundary input (`.git` file, workspace marker) that cannot be hashed: see `_boundary_hash`
CONFIG_NEXT_STEP = "point config (and overlays) at a file inside the project that the guard allows"
SCRATCH_NEXT_STEP ="pass a mirror_parent outside the project and outside any sensitive folder"
SCRATCH_REASON = "scratch_unavailable"  # internal: the directory for a probe or a mirror could not be made (an error, not a refusal)
WORKSPACE_MARKERS = ("pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json", "rush.json")
NEXT_STEP = {
    "unavailable": "install Deno 2 and put it on PATH; importers and imports still work without it",
    "no_project": "pass the directory that holds the Deno project (the one with deno.json)",
    "refused": "ask about a source file inside the project",
    "bad_argument": "fix the argument named in `detail` and ask again",
    "not_found": "check the path; it is relative to the project root",
    "stale_index": "a file changed while it was being read; ask again",
    "timeout": "ask about a smaller entry point",
    "error": "run `deno check` on the file; the answer needs a file Deno can load",
}
_SENSITIVE_NAMES = ("id_rsa", "id_ed25519", "credentials", "secrets")
_SENSITIVE_DIRS = (".git", ".ssh", ".aws", ".gnupg")
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")


ANSWER_MEMOS = contextvars.ContextVar("ctxengine_answer_memos", default={})  # {id(NavApi): {rel: verdict}} of the answers the running context is inside (`NavApi._answering`)
CANNOT_JUDGE = object()  # what the per-answer guard memo holds for a name the guard could not look at (see `NavApi._permits`)


class Refused(Exception):
    """STUB for CX1's `redact.Refused`. `reason` is a short code; the message carries no path content."""

    def __init__(self, reason):
        super().__init__(f"refused: {reason}")
        self.reason = reason


class GrantOverlap(Exception):
    """A path the sandbox profile would grant is not allowed (see `NavApi._grants`): `reason` is `sandbox_grant_overlap` (it
    overlaps the project, a monorepo root or the mirror) or `sandbox_grant_sensitive` (it is, or is in, a sensitive folder, it
    holds the home folder, or it is not the kind of file it must be)."""

    def __init__(self, reason="sandbox_grant_overlap"):
        super().__init__(reason)
        self.reason = reason


GRANT_REASONS = ("sandbox_grant_overlap", "sandbox_grant_sensitive")
SCRATCH_REFUSED = "scratch_refused"  # the folder a mirror or a probe would be made in is sensitive, or inside the project


def _sensitive_part(part):
    """True for one path component (any case) that the stub guard refuses wherever it is: `.env*`, a key or secret file name, `.git`,
    `.ssh`, `.aws`, `.gnupg`."""
    part = part.lower()
    return part.startswith(".env") or part.endswith(_SENSITIVE_SUFFIXES) or part.startswith(_SENSITIVE_NAMES) \
        or part in _SENSITIVE_DIRS


def _sensitive(rel):
    """True when the project-relative posix path `rel` is sensitive by its name alone (the stub's rules; nothing is looked at): a
    sensitive component anywhere, or a `config/local.json` / `agent/auth.json` tail."""
    parts = rel.split("/")
    return any(_sensitive_part(part) for part in parts) or "/".join(parts[-2:]).lower() in ("config/local.json", "agent/auth.json")


def _sensitive_location(path):
    """True when the absolute path `path` (resolved) has a sensitive component (see `_sensitive_part`), judged by name."""
    return any(_sensitive_part(part) for part in path.split(os.sep) if part)


def judged_location(path):
    """The absolute path `path` (a place outside any project: a sandbox grant, the folder scratch copies are made in) with its
    links followed the way `graph.judged_realpath` follows a project path, with `/` as the boundary and the guard refusing any
    sensitive component (`_sensitive`): the name, and each link's target joined to the rest, is judged by name before the file
    system is asked about it, so resolving never looks inside `.ssh`, `.git` or the like, which `os.path.realpath` would do for
    whatever a link names. Raises graph.Unproven (`reaches_refused_path`, `unreadable_file`)."""
    full = os.path.normpath(os.path.abspath(path))
    if full == os.sep:
        return full
    return os.sep + graph.judged_realpath(os.sep, full, lambda rel: not _sensitive(rel))[1]


_CHECK_REFUSALS = {"outside_project": "outside_project", "reaches_refused_path": "sensitive_path",
                   "unreadable_file": "unreadable_path"}


def stub_check_path(project, value):
    """STUB for CX1's `redact.check_path(project, path)`: the project-relative posix path `value` names, or Refused. Refused:
    `outside_project` (outside `project` by its name or where a symlink in it leads), `sensitive_path` (`.env*`,
    `config/local.json`, `agent/auth.json`, keys, secrets, `.git` and the like, by its name or where a symlink in it leads),
    `project_root`, `unreadable_path` (a link loop, or a name on the way that cannot be looked at).

    The name is judged first, with nothing asked of the file system: inside the project, and not sensitive. Only then are its
    links followed, by `graph.judged_realpath`, which judges each link's target by name before it looks at it. So asking about
    `../../x` or `.git/x` looks at nothing, and the answer is the same whether or not anything is there."""
    spelled, real_root = os.path.abspath(str(project)), os.path.realpath(str(project))  # the boundary itself
    full = graph.respell(value, spelled, real_root)
    if full == real_root:
        raise Refused("project_root")
    rel = graph.lexical_path(real_root, full)
    if rel is None:
        raise Refused("outside_project")
    if _sensitive(rel):
        raise Refused("sensitive_path")
    try:
        _, real = graph.judged_realpath(real_root, rel, lambda name: not _sensitive(name))
    except graph.Unproven as error:
        raise Refused(_CHECK_REFUSALS.get(error.reason, "outside_project"))
    return real


def stub_guard(text):
    """STUB for CX1's `redact.guard(text)`: returns `text` unchanged."""
    return text


def _answer(status, **payload):
    out = {"status": status, **payload}
    if status != "ok" and "next_step" not in out:
        out["next_step"] = NEXT_STEP.get(status, "")
    return out


def _digest(*parts):
    h = hashlib.sha256()
    for part in parts:
        data = part if isinstance(part, bytes) else str(part).encode("utf-8")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def _file_url_path(url):
    return os.path.normpath(unquote(url[len("file://"):])) if isinstance(url, str) and url.startswith("file://") else None


BOUNDARY_HASH_CAP = 4096  # bytes: the most of a `.git` file that is read, only to be hashed (larger: `unmodelled_config`)
MARKER_HASH_CAP = 65536  # bytes: the same for a workspace marker (`WORKSPACE_MARKERS`; a `turbo.json` or `nx.json` is longer than a pointer file)


def _boundary_hash(path, identity, cap=BOUNDARY_HASH_CAP):
    """The ONE exception to the refusal of `.git` by name, and the only place a boundary input is ever read: `("sha256", hex)` of
    the bytes of the regular file `path` (a `.git` file of a worktree or submodule, or a workspace marker), or `("unmodelled",
    reason)` (never an error): it is opened with O_NOFOLLOW and O_NONBLOCK (a link or a FIFO is never followed or waited for),
    must be a regular file with the `(dev, ino)` that `_boundary_input` saw (a swap in between is not hashed), and may hold at
    most `cap` bytes (BOUNDARY_HASH_CAP for a `.git` file, MARKER_HASH_CAP for a workspace marker). The content goes into the digest and nowhere else: it is not returned, logged, mirrored or
    handed to Deno, and `reason` is a code with no path or content. The caller names only a file inside the boundary."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as error:
        return ("unmodelled", f"open_{error.errno}")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != identity:
            return ("unmodelled", "not_the_file_seen")
        if st.st_size > cap:
            return ("unmodelled", "too_large")
        data = b""
        while len(data) <= cap:
            chunk = os.read(fd, cap + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > cap:
            return ("unmodelled", "too_large")
        return ("sha256", hashlib.sha256(data).hexdigest())
    except OSError as error:
        return ("unmodelled", f"read_{error.errno}")
    finally:
        os.close(fd)


def _boundary_input(path):
    """The one fingerprint of an input of the boundary decision (a `.git`, a workspace marker), taken by `os.lstat` and, for a link,
    `os.readlink` (the link itself, never what it leads to); nothing is opened or read, so a refused name (`.git`) is never read.
    A folder: its kind and identity (its mtime moves with ordinary git work, so it is not part of it). Anything else: kind,
    identity, size, mtime and ctime in ns (ctime cannot be set back, so an edit that restores the mtime is still seen), and for a
    link its target text. ("absent",) for nothing there; ("error", errno) when it cannot be looked at. Nothing is read here: the
    content of a regular file is added by `NavApi._probe_boundary` through `_boundary_hash`, for the inputs inside the boundary."""
    try:
        st = os.lstat(path)
    except OSError as error:
        return ("absent",) if isinstance(error, (FileNotFoundError, NotADirectoryError)) else ("error", error.errno)
    kind = stat.S_IFMT(st.st_mode)
    if kind == stat.S_IFDIR:
        return (kind, st.st_dev, st.st_ino)
    target = None
    if kind == stat.S_IFLNK:
        try:
            target = os.readlink(path)
        except OSError as error:
            target = ("error", error.errno)
    return (kind, st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, target)


class NavApi:
    """Navigation questions about the Deno project below `root` (default `project_root`); paths in and out are relative to
    `project_root`. One instance keeps one index; its cache files live in `cache_dir` (default `.tmux-pi/ctx-cache` under the
    project root) and are named after the root, so two projects never share one.

    `check_path(project, path)` and `guard(text)` are the path guard and output screen (see the module docstring); the cache holds
    what Deno said, and both run again on every read of it.

    Every path given here is judged by name, inside `project_root` and allowed by `check_path`, before anything is asked of it:
    `root` (refused, every answer that needs the index is `refused`), the config (judged again on every read) and the cache folder
    (`graph.CacheDir`, judged once, here). A cache folder outside the project, or one the guard refuses (`.git`, or a link to it),
    means no cache, never a cache somewhere else: answers still work, and carry `cache_refused` with the reason."""

    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=DEFAULT_TIMEOUT,
                 check_path=stub_check_path, guard=stub_guard, mirror_parent=None):
        self.check_path = check_path
        self.guard = guard
        spelled = os.path.abspath(project_root)
        self._spelled = spelled
        self.project_root = os.path.realpath(project_root)  # the frame of the paths in and out
        self.boundary = self._boundary()  # the monorepo root around the project, else the project root: nothing outside it is read
        self.root, self._root_refusal = self.project_root, None
        if root is not None and graph.respell(root, spelled, self.project_root) != self.project_root:
            try:
                self.root = self._judged(graph.respell(root, spelled, self.project_root))
            except graph.Unproven as error:
                self.root, self._root_refusal = None, error.reason
        self.config = os.path.join(self.root or self.project_root, "deno.json") if config is None \
            else graph.respell(config, spelled, self.project_root)  # judged on every read (`graph._config_read`)
        self._config_dir = os.path.dirname(self.config)  # by name; resolved below only when it is shown allowed
        if self._config_dir != self.boundary:
            try:
                self._config_dir = self._judged(self._config_dir)
            except graph.Unproven:
                pass  # `prove_reach` refuses it; the name is still a folder no grant may overlap
        self.overlays = [graph.respell(path, spelled, self.project_root) for path in overlays]
        self.cache_dir = cache_dir if cache_dir is not None else os.path.join(self.project_root, CACHE_RELATIVE)
        self._cache = graph.CacheDir(self.boundary, graph.respell(self.cache_dir, spelled, self.project_root), self._permits)
        self.deno = deno
        self.timeout = timeout
        self.mirror_parent = mirror_parent  # where each call's temporary mirror is made (default: the system's temporary directory)
        self._index = None
        self._version = None
        self._sandbox = False  # False: not tried yet; None: Deno starts under the sandbox; else the reason code (see `_check_sandbox`)

    # --- plumbing

    def _boundary(self):
        """The folder nothing outside is ever read: the monorepo root around the project, else the project root. The choice and
        everything it was made from are recorded (`_boundary_token`) and proved again with every answer (`_boundary_holds`)."""
        folder, self._boundary_token = self._probe_boundary()
        self._boundary_unmodelled = any(entry[0] == "content" and entry[2] == "unmodelled" for entry in self._boundary_token)
        return folder

    def _boundary_holds(self):
        """True when the boundary would be chosen again, from the same observations: each `.git` looked for on the way up, the
        workspace markers of the folder chosen, and the home folder. A removed, added, replaced or changed one is False."""
        try:
            return self._probe_boundary()[1] == self._boundary_token
        except Exception:
            return False

    def _probe_boundary(self):
        """(folder, observations): `_choose_boundary`'s choice and what it rests on, plus the content of the inputs that are regular
        files inside the folder chosen: each `.git` (a worktree's or submodule's pointer file; a folder keeps its kind and identity,
        its contents are never read, ordinary git work changes them), by the one exception to the refusal of `.git` (`_boundary_hash`),
        and each workspace marker the guard allows (`check_path(folder, name)`; one it refuses keeps its stat fingerprint). Each is an
        entry `("content", path, "sha256", hex)`, or `("content", path, "unmodelled", reason)` for one that cannot be hashed (too
        large, not the file seen, unreadable): `_fresh` and `_prove` refuse every answer then (`unmodelled_config`) and a probe that
        finds it changed gives `stale_index`. An input above the folder chosen (outside the boundary) is never opened."""
        folder, seen, regular = self._choose_boundary()
        hashed = []
        for path, identity in regular:
            if not path.startswith(folder + os.sep):
                continue  # outside the boundary, or the folder was not taken: stat only, never opened
            name = os.path.basename(path)
            if name != ".git":
                try:
                    if not isinstance(self.check_path(folder, name), str):
                        continue
                except Exception:
                    continue
            hashed.append((path, *_boundary_hash(path, identity, BOUNDARY_HASH_CAP if name == ".git" else MARKER_HASH_CAP)))
        return folder, (*seen, *(("content", *entry) for entry in hashed))

    def _choose_boundary(self):
        """(folder, observations, regular). The folder is the monorepo root around the project: the nearest folder at or above the
        project with a `.git`, when it has a workspace marker (`WORKSPACE_MARKERS`) and is not the project itself; the walk stops at the
        first `.git`, so a repository nested in another is never crossed. Else it is the project root. Existence probes of exact
        names (the closed exception to the path rule, as in `_guarded_roots`); nothing is read. A folder that is `/`, holds the home
        folder, has a sensitive component or cannot be judged is not taken. `observations` is what the choice rests on: for each
        folder visited the fingerprint of its `.git`, for the one that has it the same of each marker (`_boundary_input`, the one
        fingerprint of every input of this decision), and the home folder; two probes that differ chose from a different tree.
        `regular` is [(path, (dev, ino))] of each input that was a regular file."""
        seen, regular = [], []

        def look(path):
            fingerprint = _boundary_input(path)
            seen.append((path, *fingerprint))
            if fingerprint[0] == stat.S_IFREG:
                regular.append((path, (fingerprint[1], fingerprint[2])))
            return fingerprint != ("absent",) and fingerprint[0] not in ("error",)
        folder = self.project_root
        while not look(os.path.join(folder, ".git")):
            parent = os.path.dirname(folder)
            if parent == folder:
                return self.project_root, tuple(seen), regular
            folder = parent
        markers = [look(os.path.join(folder, name)) for name in WORKSPACE_MARKERS]
        if folder == self.project_root or not any(markers):
            return self.project_root, tuple(seen), regular
        try:
            home = judged_location(os.path.expanduser("~"))
        except graph.Unproven:
            seen.append(("home", None))
            return self.project_root, tuple(seen), regular
        seen.append(("home", home))
        if folder == os.sep or _sensitive_location(folder) or os.path.commonpath([folder, home]) == folder:
            return self.project_root, tuple(seen), regular
        return folder, tuple(seen), regular

    def _show_path(self, path):
        """The resolved absolute `path` as a posix path relative to the project root (`..` segments for a place in the monorepo
        outside it; `.` for the project root). Names only."""
        return os.path.relpath(path, self.project_root).replace(os.sep, "/")

    def _show(self, rel):
        """A boundary-relative name (what the index, the walk and the guard use) as the project-relative name an answer shows."""
        if self.boundary == self.project_root or not isinstance(rel, str):
            return rel
        return self._show_path(os.path.join(self.boundary, *rel.split("/")))

    def _judged(self, path):
        """The resolved absolute path of `path` (inside the project), followed by `graph.judged_realpath` with the guard; raises
        Unproven."""
        return os.path.join(self.boundary, *graph.judged_realpath(self.boundary, path, self._permits)[1].split("/"))

    @property
    def cache_refused(self):
        """Why there is no cache (`graph.CacheDir.refusal`), or None."""
        return self._cache.refusal

    def _cache_file(self, kind, key):
        return os.path.join(self._cache.path or self.cache_dir, self._cache_name(kind, key))

    @staticmethod
    def _cache_name(kind, key):
        return f"nav_api-{kind}-{key}.json"

    def _root_key(self):
        return _digest(self.project_root, self.boundary, self.root, self.config)[:16]

    def _allowed(self, rel):
        """The project-relative path `rel` as the guard names it, or None when the guard refuses it (or fails)."""
        try:
            got = self.check_path(self.boundary, rel)
        except Exception:
            return None
        return got if isinstance(got, str) else None

    def _get_index(self):
        if self._index is None:
            self._index = graph.ImportIndex(self.boundary, self.root, self.config, self.overlays,
                                            cache=(self._cache, self._cache_name("index", self._root_key())), allow=self._permits)
            self._index.boundary_holds = self._boundary_holds
        return self._index

    def _fresh(self):
        """(index, stats, None) after a refresh, or (None, None, answer) when there is nothing to navigate. A root that is not
        shown allowed is refused before anything is asked of it."""
        if self._root_refusal:
            return None, None, _answer("refused", reason=self._root_refusal)
        if self._boundary_unmodelled:
            return None, None, _answer("refused", reason=BOUNDARY_UNMODELLED, next_step=CONFIG_NEXT_STEP)
        if not graph.judged_is_dir(self.boundary, self.root, self._permits):
            return None, None, _answer("no_project", detail="project directory not found")
        index = self._get_index()
        try:
            stats = index.refresh(prove=False)  # `_index_answer` proves after the answer is built, `_final` once more after the screen
        except OSError as error:
            return None, None, _answer("error", detail=f"index could not be built: {type(error).__name__}")
        blocked = index.incomplete()
        if blocked:
            return None, None, self._incomplete(*blocked)
        return index, stats, None

    def _index_answer(self, build):
        """(answer, blocked, proof): THE ONE WAY an answer is made from the index. `_fresh` is the start: the snapshot (one walk) with
        everything the index could not get as a gap, and nothing is answered then (`blocked` is True: the caller may fall back to
        `deno info`). `build(index, stats)` makes the answer from the refreshed index (no I/O but the index's own `target_state`).
        A non-`ok` answer from `build` keeps only its status words (`detail`): no answer other than `ok` carries index data, whatever
        `build` put in it. `proof` is the ONE proof, which `_final` makes AFTER the answer is screened, as the last step before it is
        returned (`_proved`: `graph.ImportIndex.prove` against the snapshot, plus the gaps the build itself met; any gap gives
        `_incomplete` instead, with the gaps and nothing else). There is no proof between the build and the screen."""
        index, stats, error = self._fresh()
        if error:
            return error, True, None
        answer = build(index, stats)
        if answer.get("status") != "ok":
            answer = _answer(answer["status"], **{k: v for k, v in answer.items() if k == "detail"})
        return answer, False, lambda: self._proved(index)

    def _proved(self, index):
        """None when `index.prove()` and the gaps the build met are empty, else the answer to give instead (`_incomplete`, or an
        `error` when the proof could not be made). The one definition of "the index answer still holds"."""
        try:
            proof = index.prove()
        except OSError as error:
            return _answer("error", detail=f"index could not be proved: {type(error).__name__}")
        for code, count in index.gaps.items():
            proof[code] = proof.get(code, 0) + count
        blocked = index.incomplete(proof)
        return self._incomplete(*blocked) if blocked else None

    @staticmethod
    def _incomplete(kind, reason, gaps):
        """The answer for an index that could not get everything (`graph.ImportIndex.incomplete`): never `ok`, and no part of what
        it did get. `refused` is the Deno side's answer for the same config; a race is `stale_index`; the rest is an `error` that
        names the reason. `gaps` are codes and counts, never paths."""
        if kind == "refused":
            return _answer("refused", reason=reason, gaps=gaps, next_step=CONFIG_NEXT_STEP)
        if kind == "changed":
            return _answer("stale_index", gaps=gaps, detail="a file or folder changed while the index was being built")
        return _answer("error", reason=reason, gaps=gaps, next_step=INDEX_NEXT_STEP,
                       detail="part of the project could not be read, so nothing is answered from the rest")

    def _target(self, path):
        """(relative path, None) for a path that may be asked about, or (None, answer)."""
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return None, _answer("bad_argument", detail="path must be a non-empty string")
        asked = path
        if self.boundary != self.project_root:  # the path is the project's; the guard and the index name files from the boundary
            full = graph.respell(path, self._spelled, self.project_root)
            asked = graph.lexical_path(self.boundary, full)
            if asked is None:
                return None, _answer("refused", reason="project_root" if full == self.boundary else "outside_project")
            if full == self.project_root:
                return None, _answer("refused", reason="project_root")
        try:
            rel = self.check_path(self.boundary, asked)
        except Exception as error:
            reason = getattr(error, "reason", None) or str(error)[:200] or "refused"
            return None, _answer("refused", reason=reason)
        if not isinstance(rel, str) or graph.project_path(self.boundary, rel, self._permits) is None:
            return None, _answer("refused", reason="outside_project")
        return rel, None

    def _out(self, answer):
        """`answer` with every string passed through the output screen (the status word excepted); an error answer if it fails.
        An `ok` answer says `cache_refused` when the cache folder was refused (see `graph.CacheDir`)."""
        if answer.get("status") == "ok" and self._cache.refusal:
            answer = {**answer, "cache_refused": self._cache.refusal}
        try:
            return self._screened(answer)
        except Exception:
            return _answer("error", detail="the output screen failed; nothing was returned")

    def _screened(self, value):
        if isinstance(value, str):
            screened = self.guard(value)
            if not isinstance(screened, str):
                raise TypeError("guard must return text")
            return screened
        if isinstance(value, dict):
            return {key: (item if key == "status" else self._screened(item)) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._screened(item) for item in value]
        return value

    def _meta(self, index, stats):
        out = {"index_age_s": index.age_s(), "unresolved_total": len(index.unresolved()), "files_indexed": stats["files"],
               "scanned": stats["scanned"]}
        if index.meta["ignored"]:
            out["ignored_config"] = list(index.meta["ignored"])
        return out

    def _slim(self, edge, side):
        out = {"path": self._show(edge[side]), "line": edge["line"], "spec": edge["spec"]}
        for flag in ("type_only", "dynamic"):
            if edge[flag]:
                out[flag] = True
        if edge["config"]:
            out["config"] = edge["config"]
        return out

    # --- importers / imports (the index)

    def importers(self, target, limit=MAX_IMPORTERS):
        """Files that import `target` directly, ordered by (path, line); `limit` is 1 to 25."""
        with self._answering():
            answer = self._final(*self._importers(target, limit))
        return self._swept(answer)

    def _importers(self, target, limit):
        """(answer, proof); the public wrapper makes the final check (`_final`)."""
        rel, error = self._target(target)
        if error:
            return error, None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_IMPORTERS:
            return _answer("bad_argument", detail=f"limit must be a whole number from 1 to {MAX_IMPORTERS}"), None
        def build(index, stats):
            found = index.importers(rel)
            if not found and not index.indexed(rel) and index.target_state(rel) != "":
                return _answer("not_found", detail="no such file in the project")  # an unreadable one is a gap (`_index_answer`)
            return _answer("ok", target=self._show(rel), total=len(found), truncated=len(found) > limit,
                           importers=[self._slim(e, "from") for e in found[:limit]], **self._meta(index, stats))
        answer, _, proof = self._index_answer(build)
        return answer, proof

    def imports(self, target, source="auto"):
        """What `target` imports directly: `files` (project paths), `external` (packages, builtins, URLs) and `unresolved`.
        `source` is `auto` (the index, and `deno info` when no index can be built), `index` or `deno`."""
        with self._answering():
            answer = self._final(*self._imports(target, source))
        return self._swept(answer)

    def _imports(self, target, source):
        """(answer, proof): the check `_final` makes last (see `_index_answer`, `_reach_proof`); None for an answer that carries no
        index or Deno data."""
        rel, error = self._target(target)
        if error:
            return error, None
        if source not in ("auto", "index", "deno"):
            return _answer("bad_argument", detail="source must be auto, index or deno"), None
        if source != "deno":
            def build(index, stats):
                edges = index.imports(rel)
                if edges is None:
                    return _answer("not_found", detail="the file is not an indexed source file")
                return _answer("ok", target=self._show(rel), source="index",
                               files=[self._slim(e, "to") for e in edges if e["kind"] == "file"],
                               external=sorted({e["spec"] for e in edges if e["kind"] == "external"}),
                               unresolved=[{"line": e["line"], "spec": e["spec"], "reason": e["reason"]}
                                           for e in edges if e["kind"] == "unresolved"],
                               **self._meta(index, stats))
            answer, blocked, proof = self._index_answer(build)
            if not blocked or source == "index":
                return answer, proof
        answer, reach, store = self._forward_graph(target)
        return answer, self._reach_proof(reach), store

    # --- deno

    def _deno_exe(self):
        """The Deno executable: `deno` (a path, or a name looked up in the `PATH` folders, in order), with nothing looked at before
        its name is judged: each candidate is first resolved by `judged_location`. A candidate that is, or leads to, a sensitive
        place is returned unlooked-at, by name, for `_grants` to refuse (`sandbox_grant_sensitive`); otherwise the first that is an
        executable regular file. A `PATH` folder that is, or leads to, a sensitive place is skipped; a relative one is ignored."""
        if os.sep in self.deno:
            return self._executable(os.path.abspath(self.deno))
        for folder in os.environ.get("PATH", os.defpath).split(os.pathsep):
            if not os.path.isabs(folder):
                continue  # a relative entry names the current folder, which is nobody's choice here
            try:
                folder = judged_location(folder)
            except graph.Unproven:
                continue
            found = self._executable(os.path.join(folder, self.deno))
            if found:
                return found
        return None

    @staticmethod
    def _executable(candidate):
        """`candidate` when it is refused by `judged_location` (never looked at; `_grants` refuses it) or leads to an executable
        regular file (looked at only where it was judged to lead); else None."""
        try:
            resolved = judged_location(candidate)
        except graph.Unproven:
            return candidate
        return candidate if os.path.isfile(resolved) and os.access(resolved, os.X_OK) else None

    def _base_env(self):
        env = {key: os.environ[key] for key in DENO_ENV_KEEP if key in os.environ}
        env.update(OFFLINE_ENV)
        return env

    def _deno_dir(self):
        """Deno's cache directory as Deno finds it (`DENO_DIR`, else `XDG_CACHE_HOME`, else the platform's cache folder), by name;
        `_grants` resolves it."""
        env = self._base_env()
        if env.get("DENO_DIR"):
            return env["DENO_DIR"]
        if env.get("XDG_CACHE_HOME"):
            return os.path.join(env["XDG_CACHE_HOME"], "deno")
        return os.path.join(env.get("HOME") or os.path.expanduser("~"), "Library", "Caches", "deno")

    def _guarded_roots(self):
        """The resolved directories no sandbox grant may equal, contain or sit inside: the project, the Deno root, the config's folder
        and the monorepo around the project (the nearest folder above it with a `.git`, and every folder above it with a workspace
        marker file)."""
        roots = {path for path in (self.project_root, self.boundary, self.root, self._config_dir) if path}  # each judged in __init__, not resolved here
        folder, git = self.project_root, False
        while True:
            if (not git and os.path.lexists(os.path.join(folder, ".git"))) or \
                    any(os.path.lexists(os.path.join(folder, name)) for name in WORKSPACE_MARKERS):
                roots.add(folder)
                git = git or os.path.lexists(os.path.join(folder, ".git"))
            parent = os.path.dirname(folder)
            if parent == folder:
                return roots
            folder = parent

    @staticmethod
    def _overlaps(a, b):
        return os.path.commonpath([a, b]) in (a, b)

    def _grants(self, exe, mirror=None):
        """{"DENODIR": Deno's cache, "DENOBIN": the executable}, each resolved by `judged_location` exactly once, here, and checked as
        those strings; raises GrantOverlap when one equals, contains or sits inside the project, the Deno root, the config's folder
        or a monorepo root, or contains or sits inside `mirror` (the resolved mirror, checked as the string given). The mirror is
        the one grant that is meant to be a folder of files; every other grant is Deno's own, and a worker's environment can name
        them. The caller hands on these strings and never resolves the paths again (`_sandboxed`), so a link moved after the check
        cannot change what is granted: the profile and `DENO_DIR` name the checked directory, and the sandbox matches the paths Deno
        opens after their links are followed, so a link inside it that is moved later leads to a path that is not granted."""
        try:
            grants = {"DENODIR": judged_location(self._deno_dir()), "DENOBIN": judged_location(exe)}
        except graph.Unproven:
            raise GrantOverlap("sandbox_grant_sensitive")  # it is, or a link on the way leads to, a sensitive place
        guarded = self._guarded_roots() | ({mirror} if mirror else set())
        if any(self._overlaps(grant, root) for grant in grants.values() for root in guarded):
            raise GrantOverlap()
        self._grant_kinds(grants)
        return grants

    @staticmethod
    def _grant_kinds(grants):
        """Raises GrantOverlap(`sandbox_grant_sensitive`) when a resolved grant has a sensitive component (`.ssh`, `.git`, `.env*`,
        a key or secret name: `_sensitive_location`), holds the home folder (or `/`), or is not the kind of file it must be: Deno's
        cache a folder (or not there yet), the executable a regular file. The grants are resolved and judged by name (no sensitive
        component), so `lstat` follows no link and looks at no sensitive place."""
        try:
            home = judged_location(os.path.expanduser("~"))
        except graph.Unproven:
            raise GrantOverlap("sandbox_grant_sensitive")
        for name, grant in grants.items():
            if _sensitive_location(grant) or os.path.commonpath([grant, home]) == grant:
                raise GrantOverlap("sandbox_grant_sensitive")
            try:
                mode = os.lstat(grant).st_mode
            except FileNotFoundError:
                if name == "DENOBIN":
                    raise GrantOverlap("sandbox_grant_sensitive")
                continue
            except OSError:
                raise GrantOverlap("sandbox_grant_sensitive")
            if not (stat.S_ISDIR(mode) if name == "DENODIR" else stat.S_ISREG(mode)):
                raise GrantOverlap("sandbox_grant_sensitive")

    def _sandboxed(self, exe, args, root):
        """(argv, env) that run `exe args` under `SANDBOX_PROFILE`, allowed to read `root` (a resolved directory, the mirror) and
        Deno's own files and nothing else, or None when there is no sandbox to run it in; raises GrantOverlap (`_grants`). The
        command runs the checked executable path (`DENOBIN`), and `DENO_DIR` in `env` is the checked cache path (`DENODIR`): every
        value in the command line and the environment is the very string that was checked. Never a command line without the
        sandbox, and none with an overlapping grant."""
        if sys.platform != "darwin" or not os.access(SANDBOX_EXEC, os.X_OK):
            return None
        grants = self._grants(exe, root)
        env = self._base_env()
        env["DENO_DIR"] = grants["DENODIR"]
        argv = [SANDBOX_EXEC, "-D", f"ROOT={root}", "-D", f"DENODIR={grants['DENODIR']}", "-D", f"DENOBIN={grants['DENOBIN']}",
                "-p", SANDBOX_PROFILE, grants["DENOBIN"], *args]
        return argv, env

    def _check_sandbox(self):
        """None when a process can be started under the sandbox on this machine, else the reason code of the refusal. Tried once:
        `deno --version` in an empty directory (which also gives the version). Only a failure of the sandbox itself counts: no
        `sandbox-exec`, a profile it cannot apply (it exits 71, as it does when this process already runs in a sandbox), or no
        way to start it. What Deno does inside is judged on each call. Every later Deno process is started the same way.
        Two reasons are not remembered, because the next call may be different: `sandbox_grant_overlap` (the environment names a cache
        directory, or the executable is somewhere, that a grant must not be; checked on every call) and `SCRATCH_REASON` (no
        directory for the probe)."""
        exe = self._deno_exe()
        if exe:
            try:
                self._grants(exe)  # a gate only: nothing is handed on from this check (each launch checks its own grants)
            except GrantOverlap as error:
                return error.reason
        if self._sandbox is False:
            parent = self._scratch_parent()
            if parent is None:
                return SCRATCH_REFUSED
            try:
                scratch = graph.make_own_folder(parent, "tp-ctx-probe-")  # made where `parent` was judged, or not at all
            except graph.Unproven:
                return SCRATCH_REFUSED  # the judged folder was swapped for a link
            except OSError:
                return SCRATCH_REASON
            try:
                root = scratch
                try:
                    launch = self._sandboxed(exe, ["--version"], root) if exe else None
                except GrantOverlap as error:
                    return error.reason
                self._sandbox = "sandbox_unavailable" if launch is None else None
                self._version = ""
                if launch is not None:
                    argv, env = launch
                    try:
                        run = subprocess.run(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                             timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        run = None  # it ran, so the sandbox was applied
                    except OSError:
                        self._sandbox = "sandbox_failed"
                    else:
                        if run.returncode == SANDBOX_APPLY_FAILED and run.stderr.startswith(b"sandbox-exec:"):
                            self._sandbox = "sandbox_failed"
                        elif run.returncode == 0 and run.stdout.strip():
                            self._version = run.stdout.decode("utf-8", "replace").splitlines()[0].strip()
            finally:
                graph.remove_own_folder(scratch)
        return self._sandbox

    @staticmethod
    def _sandbox_refusal(reason):
        """The answer for a reason code from `_check_sandbox` or `_run_deno`."""
        if reason == SCRATCH_REASON:
            return _answer("error", detail="the temporary copy for Deno could not be made")
        if reason == SCRATCH_REFUSED:
            return _answer("refused", reason=reason, next_step=SCRATCH_NEXT_STEP)
        return _answer("refused", reason=reason, next_step=GRANT_NEXT_STEP if reason in GRANT_REASONS else SANDBOX_NEXT_STEP)

    def _scratch_parent(self):
        """The resolved folder a mirror or a probe is made in (`mirror_parent`, else `TMPDIR`, else `/tmp`), or None when it has a
        sensitive component or is inside the project (a folder around the project is fine: the mirror is a new folder in it, and
        `graph.Mirror` refuses one that holds the project). Resolved here by `judged_location`, once per use, and handed on as
        this string; `graph.make_own_folder` makes the folder there. Not `tempfile.gettempdir()`: the first time it is called it
        writes a test file in `TMPDIR` (or the next folder that works) before anything has judged it. A `TMPDIR` that is refused
        is not passed over for another folder: the call is refused."""
        candidate = self.mirror_parent or os.environ.get("TMPDIR") or "/tmp"
        if not os.path.isabs(candidate):
            return None  # relative to the current folder, which is nobody's choice here
        try:
            parent = judged_location(candidate)  # a folder for this process's own scratch
        except graph.Unproven:
            return None
        if _sensitive_location(parent) or parent == self.boundary or parent.startswith(self.boundary + os.sep):
            return None
        return parent

    def _run_deno(self, args, cwd, root):
        """(status, stdout, detail): runs `deno` with `args` in `cwd`, under the sandbox, which lets it read `root` (a mirror, as the
        resolved path `graph.Mirror.root`; it is checked and granted as given, never resolved again) and Deno's own
        files and nothing else, never longer than the timeout. When the sandbox cannot be applied nothing is run: the status is `refused` and
        the detail the reason code. The detail is not screened here; `_out` screens the answer it ends up in."""
        exe = self._deno_exe()
        if exe is None:
            return "unavailable", "", "deno was not found on PATH"
        reason = self._check_sandbox()
        if reason:
            return "refused", "", reason
        try:
            launch = self._sandboxed(exe, args, root)
        except GrantOverlap as error:
            return "refused", "", error.reason
        if launch is None:
            return "refused", "", "sandbox_unavailable"
        argv, env = launch
        try:
            run = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return "timeout", "", f"deno took longer than {self.timeout} s"
        except OSError as error:
            return "unavailable", "", f"deno could not be started: {type(error).__name__}"
        if run.returncode == SANDBOX_APPLY_FAILED and run.stderr.startswith(b"sandbox-exec:"):
            return "refused", "", "sandbox_failed"
        if len(run.stdout) > MAX_DENO_OUTPUT:
            return "error", "", "deno output was too large"
        if run.returncode != 0:
            lines = run.stderr.decode("utf-8", "replace").strip().splitlines()
            first = next((line for line in lines if line.strip() and not line.startswith(("Download", "Warning"))), "")
            return "error", "", first.strip()[:1000] or "deno failed"  # cut to 200 after `_scrub`, which needs whole paths
        return "ok", run.stdout.decode("utf-8", "replace"), ""

    def deno_version(self):
        """First line of `deno --version`, or None when Deno cannot be run (or not under the sandbox)."""
        if self._deno_exe() is None or self._check_sandbox():
            return None
        return self._version or None

    @contextlib.contextmanager
    def _answering(self):
        """The length of ONE answer, for the guard memo (`_permits`): every public call that asks the guard is made in one. Each
        call makes its OWN memo, empty, and puts back whatever was current on every way out (a return, an exception): so nothing is
        remembered from one answer to the next, a call made inside another (from the guard, say) never reads or fills the outer
        one's memo and leaves it as it was, and a call in another thread on this instance has a memo of its own (the memo is carried by the
        context, which `graph`'s reader threads copy from the caller). No memo is kept on the instance or between answers."""
        memo, memos = {}, ANSWER_MEMOS.get()
        token = ANSWER_MEMOS.set({**memos, id(self): memo})  # copied, never edited in place: an outer answer's mapping stays as it was
        try:
            yield
        finally:
            ANSWER_MEMOS.reset(token)

    def _permits(self, rel):
        """The guard's verdict as a bool, for `graph`, asked once per name per answer (`_answering`: the walk at the start, the new
        walk of the proof, the probes and the reads all name the same files). A guard that refused because it could not look
        (`unreadable_path`) did not judge the name: `graph.CannotJudge`, which every user but the index takes as a refusal, and the
        index records as a gap. FAIL CLOSED: a guard that raises is a refusal, and so is what the memo holds for the name for the rest
        of the answer (a failure is never turned into an allow by asking again, nor is an allow ever stored for a failure); a name
        the guard could not judge raises `CannotJudge` every time it is asked."""
        memo = ANSWER_MEMOS.get().get(id(self))
        if memo is not None and rel in memo:
            if memo[rel] is CANNOT_JUDGE:
                raise graph.CannotJudge(rel) from None
            return memo[rel]
        try:
            got = self.check_path(self.boundary, rel)
        except Exception as error:
            if getattr(error, "reason", None) == "unreadable_path":
                if memo is not None:
                    memo[rel] = CANNOT_JUDGE
                raise graph.CannotJudge(rel) from None
            verdict = False
        else:
            verdict = isinstance(got, str)
        if memo is not None:
            memo[rel] = verdict
        return verdict

    def _prove(self, rel):
        """(reach, None) when every module Deno would load from `rel` has been shown allowed (`graph.prove_reach`), else
        (None, the refusing answer). The walk reads the files itself and does not use the index, so what the index leaves out (an
        excluded file) and whether the index could be built make no difference. A walk that fails for any other reason is a
        refusal as well."""
        try:
            reach = graph.prove_reach(self.boundary, os.path.join(self.boundary, rel), self.config, self._permits)
            reach.boundary_holds = self._boundary_holds
            if self._boundary_unmodelled:  # after the walk, whose own reason (an unreadable marker it reads, say) is the more precise one
                return None, _answer("refused", reason=BOUNDARY_UNMODELLED, next_step=CONFIG_NEXT_STEP)
            return reach, None
        except graph.Unproven as error:
            if error.reason == "changed_during_walk":  # a link moved while the walk read: ask again
                return None, _answer("stale_index", detail="a file changed while it was being read")
            return None, _answer("refused", reason=error.reason)
        except Exception:
            return None, _answer("refused", reason="precheck_failed")

    def _run_mirrored(self, verb_args, reach):
        """Runs `deno <verb_args> --config <mirror config> <mirror entry>` on a mirror of what the walk proved, never on the project
        (see `graph.Mirror`), and removes the mirror. Returns (None, stdout, mirror) when Deno answered and the proven files are
        unchanged, else (the answer to give, None, None). The detail of a Deno failure is rewritten so it names no file the walk did
        not prove."""
        try:
            parent = self._scratch_parent()
            if parent is None:
                raise graph.Unproven(SCRATCH_REFUSED)
            mirror = reach.materialize(parent)
        except graph.Unproven as error:
            if error.reason == SCRATCH_REFUSED:
                return self._sandbox_refusal(SCRATCH_REFUSED), None, None
            return _answer("refused", reason=error.reason), None, None
        except Exception:
            return _answer("error", detail="the temporary copy for Deno could not be made"), None, None
        try:
            config = mirror.path(reach.config_rel)
            status, out, detail = self._run_deno([*verb_args, "--config", config, mirror.path(reach.entry)],
                                                 cwd=os.path.dirname(config), root=mirror.root)
        finally:
            mirror.cleanup()
        if status == "refused":  # the sandbox could not be applied, or a grant was not allowed: Deno was not run
            return self._sandbox_refusal(detail), None, None
        if status != "ok":
            return _answer(status, detail=self._scrub(detail, mirror, reach.names)[:200]), None, None
        if not reach.holds():
            return _answer("stale_index", detail="a file changed while Deno was reading it"), None, None
        return None, out, mirror

    def _final(self, answer, proof, store=None):
        """What every public operation returns, from the index and from Deno alike: `answer` screened (`_out`), and then, when it
        rests on a proof (`proof()`: None when what it rests on still holds, else the answer to give instead), that proof as the
        very last step: the screen, the filtering and the paging are all done before it, so nothing (the guard included) runs
        between the check and the return. A tree that moved gives that answer (`stale_index`, or the index's gaps), which carries
        nothing from `answer`.
        `store` (None, or a function that JUDGES an entry and returns the function that writes it, or None) is the only thing that
        writes a cache from an answer, and it has no rollback. Before the proof, for an answer that is `ok` before and after the
        screen, `store()` chooses the entry's name and a unique temporary name and asks the guard about both (the folder was judged when the
        cache was made; `graph.CacheDir.prepare_json`); a refusal, or any exception, means no store. The proof then runs as the last thing that
        touches the project. Only when the answer is still `ok` is the returned function called: it writes the temporary file and
        renames it (`graph.CacheDir.put_json`), asks no guard and calls no project code, and a failure of it is swallowed (the
        answer stays `ok`; entries are unchanged and a stray `.tmp` may remain, swept by S5). Nothing is ever removed, so a failed proof, a proof that raises, a refusal, a gap or an error leaves
        no entry because none was written, and no other writer's entry is touched (the entry is keyed by the content it was made
        from, so a second writer of the same key writes the same bytes by an atomic rename)."""
        out = self._out(answer)
        commit = None
        if store is not None and answer.get("status") == "ok" and out.get("status") == "ok":
            try:
                commit = store()
            except Exception:
                commit = None
        failed = proof() if proof is not None else None
        if failed is not None:
            return self._out(failed)
        if commit is not None:
            try:
                commit()
            except Exception:
                pass
        return out

    @staticmethod
    def _reach_proof(reach):
        """The proof an answer from `deno info` or `deno doc` rests on (`reach.holds()`, config files included: `Reach.configs`);
        None when the answer rests on none."""
        if reach is None:
            return None
        return lambda: None if reach.holds() else _answer("stale_index", detail="a file changed while the answer was being made")

    def _swept(self, answer):
        """`answer`, unchanged, after the cache sweep (`graph.CacheDir.sweep`) had its chance. It runs when the call wrote a cache
        entry (a hit writes nothing, so a hit never sweeps), after the answer was proved, screened and made, so nothing it does can
        change it: it has a guard memo of its own (`_answering`), takes at most the sweep's budget, and any failure of it is swallowed."""
        try:
            if self._cache.written:
                with self._answering():
                    self._cache.sweep()
        except Exception:
            pass
        return answer

    @staticmethod
    def _scrub(text, mirror, names):
        """`text` with each path in the mirror shown as its project-relative name when the walk proved that file, else `<file>`:
        Deno's messages quote what the files said, which can be a refused or outside path that is not there."""
        def swap(match):
            raw = match.group()
            path = unquote(raw[len("file://"):]) if raw.startswith("file://") else raw
            rel = mirror.to_real(path)
            return rel if rel in names else "<file>"
        text = re.sub(r"file://[^\s\"'<>)\]]+", swap, text)
        return re.sub(re.escape(mirror.root) + r"[^\s\"'<>)\]]*", swap, text)

    def forward_graph(self, target):
        """The local modules `target` reaches, from `deno info --json` (30 s cap): {"modules": [{"path", "imports": [paths]}]}.
        Only files inside the project are listed; packages and URLs are counted in `external`. When the graph is longer than
        MAX_MODULES, `target` and its direct imports are kept and the rest is cut in path order."""
        with self._answering():
            answer, reach, store = self._forward_graph(target)
            answer = self._final(answer, self._reach_proof(reach), store)
        return self._swept(answer)

    def _forward_graph(self, target):
        """(answer, reach, store); the public wrapper makes the final check (`_final`), for a cached answer as for a fresh one, and
        runs `store` (None, or the write of the entry) only once the answer is ok and proven; this method writes nothing.
        The raw graph (`INFO_FORMAT`: each reached project file and the project files it imports, as proven names, and the count of
        packages and URLs) is cached on disk by the content of everything the walk reached (`_info_key`). A hit is not an answer:
        it goes through the same filter as a fresh result (`_info_local`: only names this walk proved, deps the guard allows), and
        then the same listing, screen and last proof. With no sandbox Deno is not run, so nothing is answered from what it said
        earlier either."""
        rel, error = self._target(target)
        if error:
            return error, None, None
        if not graph.judged_is_file(self.boundary, rel, self._permits):
            return _answer("not_found", detail="no such file in the project"), None, None
        reach, refusal = self._prove(rel)
        if refusal:
            return refusal, None, None
        names = reach.names
        key, store = None, None
        if self._deno_exe() is not None:
            reason = self._check_sandbox()
            if reason:  # not even a cached answer
                return self._sandbox_refusal(reason), None, None
            key = self._info_key(rel, reach)
        stored = self._read_cache("info", key, dict) if key else None
        found = self._info_local(stored, names) if stored is not None else None
        if found is not None:
            self._cache.touch(self._cache_name("info", key))  # the stored graph has the shape asked for: the entry is in use
            local, external = found
        else:
            refusal, out, mirror = self._run_mirrored([*INFO_FLAGS], reach)
            if refusal:
                return refusal, None, None
            try:
                data = json.loads(out)
                modules = data["modules"]
            except (ValueError, KeyError, TypeError):
                return _answer("error", detail="deno info gave output that is not a module graph"), None, None
            if not isinstance(modules, list):
                return _answer("error", detail="deno info gave output that is not a module graph"), None, None
            local, external = {}, 0
            for module in modules:
                if not isinstance(module, dict):
                    continue
                path = _file_url_path(module.get("specifier"))
                if path is None:
                    external += 1
                    continue
                inside = mirror.to_real(path)  # None: a path outside the mirror, so Deno read something that was not given to it
                if inside is None or inside not in names:
                    if module.get("error") and inside is not None:
                        continue  # a module Deno could not load (not in the mirror): nothing was read, nothing to list
                    return _answer("refused", reason="unproven_module"), None, None  # Deno loaded a file the walk did not show allowed
                deps = set()
                for dep in module.get("dependencies") or []:
                    if not isinstance(dep, dict):
                        continue
                    for kind in ("code", "type"):
                        dest = _file_url_path(dep.get(kind, {}).get("specifier")) if isinstance(dep.get(kind), dict) else None
                        dest = mirror.to_real(dest) if dest else None
                        if dest and dest in names and self._allowed(dest) is not None:
                            deps.add(dest)
                local[inside] = sorted(deps)
            if key:  # only a graph Deno gave and the walk accepted whole; `_final` stores it once the answer is ok and proven
                value = {"format": INFO_FORMAT, "external": external,
                         "modules": [{"path": path, "imports": deps} for path, deps in sorted(local.items())]}
                store = lambda: self._prepare_cache("info", key, value)
        keep = [path for path in [rel, *local.get(rel, [])] if path in local]
        chosen = set(keep[:MAX_MODULES])
        for path in sorted(local):
            if len(chosen) >= MAX_MODULES:
                break
            chosen.add(path)
        listed = [{"path": self._show(path), "imports": [self._show(dep) for dep in local[path]]} for path in sorted(chosen)]
        return _answer("ok", target=self._show(rel), source="deno_info", modules=listed, truncated=len(local) > len(listed), total=len(local),
                       external=external), reach, store

    def _info_key(self, rel, reach):
        """Cache key for `deno info` of one entry: the content of every file and config the walk reached (`Reach.digest`), the entry,
        the Deno version, the flags it is run with and the format of what is stored. Never a time or a size."""
        return _digest("info", rel, reach.digest(), self.deno_version() or "", " ".join(INFO_FLAGS), INFO_FORMAT)[:40]

    def _info_local(self, stored, names):
        """({path: [imports]}, external) from a stored graph, filtered as a fresh one is (`names`: the files this walk proved; an
        import must also be allowed now), or None when it is not a graph of the stored shape (then it is a miss). An entry naming
        anything else is dropped, so a planted or old file cannot name what the walk did not prove."""
        modules, external = stored.get("modules"), stored.get("external")
        if type(stored.get("format")) is not int or stored.get("format") != INFO_FORMAT or not isinstance(modules, list) or isinstance(external, bool) \
                or not isinstance(external, int) or external < 0:
            return None
        local = {}
        for module in modules:
            if not isinstance(module, dict) or not isinstance(module.get("path"), str) or module["path"] not in names \
                    or not isinstance(module.get("imports"), list):
                continue
            local[module["path"]] = sorted({dep for dep in module["imports"]
                                            if isinstance(dep, str) and dep in names and self._allowed(dep) is not None})
        return local, external

    def symbols(self, path, limit=MAX_SYMBOLS):
        """The exported symbols of `path` from `deno doc --json`: name, kind, definition path and line, the definition's source
        line as `signature`, and the first doc-comment lines. A re-export is listed at the file that defines it."""
        with self._answering():
            answer, reach, store = self._symbols(path, limit)
            answer = self._final(answer, self._reach_proof(reach), store)
        return self._swept(answer)

    def _symbols(self, path, limit):
        """(answer, reach, store); the public wrapper makes the final check (`_final`) and runs `store` (the write of the entry, or
        None) only once the answer is ok and proven; this method writes nothing."""
        rel, error = self._target(path)
        if error:
            return error, None, None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return _answer("bad_argument", detail="limit must be a whole number from 1 to 100"), None, None
        if not graph.judged_is_file(self.boundary, rel, self._permits):
            return _answer("not_found", detail="no such file in the project"), None, None
        if self._deno_exe() is None:
            return _answer("unavailable", detail="deno was not found on PATH"), None, None
        reach, refusal = self._prove(rel)
        if refusal:
            return refusal, None, None
        reason = self._check_sandbox()
        if reason:  # not even a cached answer: with no sandbox Deno is not run, so nothing is answered from what it said
            return self._sandbox_refusal(reason), None, None
        names = reach.names
        key = self._doc_key(rel, reach)
        cached = self._read_cache("doc", key)
        if cached is not None and not all(self._doc_item_shape(item) for item in cached):
            cached = None  # a list that is not what `_parse_doc` makes (a whole envelope can hold anything): a miss, replaced below
        store = None
        if cached is not None:
            self._cache.touch(self._cache_name("doc", key))  # a symbol list, as asked for: the entry is in use
            found, hit = cached, True
        else:
            refusal, out, mirror = self._run_mirrored(["doc", "--json", "--no-remote", "--no-lock"], reach)
            if refusal:
                return refusal, None, None
            found = self._parse_doc(out, names, mirror, reach)
            if found is None:
                return _answer("error", detail="deno doc gave output that is not a symbol list"), None, None
            store = lambda: self._prepare_cache("doc", key, found)
            hit = False
        shown = [item for item in (self._present(entry, names) for entry in found) if item]
        return _answer("ok", path=self._show(rel), source="deno_doc", cached=hit, total=len(shown), truncated=len(shown) > limit,
                       symbols=shown[:limit]), reach, store

    @staticmethod
    def _doc_item_shape(item):
        """True when `item` has the types `_parse_doc` writes: text for name, kind, path, signature and doc, a whole number for line.
        Text is judged by what `_parse_doc` can emit (a name, kind and path that are not empty; a signature and doc no longer than the cut)
        and the line by its range (from 1, as Deno counts from 0 and one is added, and within 64 bits). A stored symbol is judged by its types here (the whole entry is a miss when one is wrong) and by `_present` by its path (a
        symbol that names a file the walk did not prove is dropped)."""
        return isinstance(item, dict) and all(isinstance(item.get(field), str) for field in ("name", "kind", "path", "signature", "doc")) \
            and all(item[field] for field in ("name", "kind", "path")) and len(item["signature"]) <= SIGNATURE_MAX \
            and len(item["doc"]) <= DOC_LINE_MAX and type(item.get("line")) is int and 1 <= item["line"] < 2 ** 63

    def _present(self, entry, names):
        """One cached or fresh symbol as it may leave the process: None when its path is refused now, or is not one of the files
        the walk showed allowed (`names`). The text itself is screened by `_out` on the way out, so nothing read back from the
        cache skips the screen."""
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or entry["path"] not in names \
                or self._allowed(entry["path"]) is None:
            return None
        shown = {key: entry.get(key, "") for key in ("name", "kind", "path", "line", "signature", "doc")}
        shown["path"] = self._show(shown["path"])
        return shown

    def _parse_doc(self, text, names, mirror, reach):
        """The symbol list for the cache: raw as Deno gave it (cut to length), for files the guard allows now and the walk reached
        (`names`), named by their project paths (Deno saw the mirror's). The signature line is read from what the walk read, the
        files Deno was given. It is screened when it is read, never trusted as already screened."""
        try:
            data = json.loads(text)
            nodes = data["nodes"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(nodes, dict):
            return None
        lines_of, found = {}, []
        for node in nodes.values():
            for symbol in node.get("symbols", []):
                for decl in symbol.get("declarations", []):
                    if decl.get("declarationKind") != "export":
                        continue
                    where = _file_url_path(decl.get("location", {}).get("filename"))
                    rel = mirror.to_real(where) if where else None
                    if rel is None or rel not in names or self._allowed(rel) is None:
                        continue  # defined outside the project (a package), or a file the guard refuses: not navigable here
                    line = int(decl["location"].get("line", 0)) + 1  # deno doc counts lines from 0
                    if rel not in lines_of:
                        lines_of[rel] = reach.contents.get(rel, b"").decode("utf-8", "replace").splitlines()
                    source = lines_of[rel]
                    signature = ""
                    for at in range(line - 1, min(len(source), line + 10)):  # the location can point at the doc comment
                        text_line = source[at].strip()
                        if text_line and not text_line.startswith(("/**", "*", "//", "@")):
                            signature = text_line
                            break
                    doc = ((decl.get("jsDoc") or {}).get("doc") or "").strip()
                    found.append({"name": symbol.get("name", ""), "kind": decl.get("kind", ""), "path": rel, "line": line,
                                  "signature": signature[:SIGNATURE_MAX], "doc": doc[:DOC_LINE_MAX]})
        found.sort(key=lambda s: (s["path"], s["line"], s["name"]))
        return found

    # --- the doc cache

    def _doc_key(self, rel, reach):
        """Cache key for `deno doc` of one file: a hash of the content of every file the walk reached (the target, and whatever a
        re-export is resolved from, an excluded file included), of the config and package files Deno was given, and the Deno version. A size and a modification time
        do not stand for content (a checkout can restore both), so none is used."""
        return _digest(rel, reach.digest(), self.deno_version() or "")[:40]

    def _read_cache(self, kind, key, shape=list):
        """The cached `shape` (a list, or a dict), read by the judged cache folder (`graph.CacheDir.read_json`: `read_bounded`, no
        link, a plain `.json` directly in the folder, the guard judging its name), or None."""
        data = self._cache.read_json(self._cache_name(kind, key))
        return data if isinstance(data, shape) else None

    def _prepare_cache(self, kind, key, value):
        """Judge the entry (`graph.CacheDir.prepare_json`: its name and a unique temporary name, asked of the guard once),
        and return the function that writes it (`graph.CacheDir.put_json`: no guard, no project code, failure swallowed), or None
        when the cache is refused or the guard refuses a name. `_final` calls this before its proof and the function after it."""
        token = self._cache.prepare_json(self._cache_name(kind, key))
        if token is None:
            return None
        return lambda: self._cache.put_json(token, value)

    def _write_cache(self, kind, key, value):
        """Judge and write at once (`graph.CacheDir.write_json`); nothing when the folder or a name was refused. Not used for an
        answer, which goes through `_final`."""
        return self._cache.write_json(self._cache_name(kind, key), value)

    # --- status

    def status(self):
        """What this instance would use: the Deno it found, the root and config, the cache folder (and why it is refused, when it
        is), what it holds (`cache`: {"entries", "bytes", "last_sweep", "incomplete"}; seconds since the epoch; entries and bytes None, and
        incomplete True, where they are not all known), and the index's
        size and age. The config is looked at only when it is shown allowed."""
        exe = self._deno_exe()
        out = {"status": "ok", "deno": exe, "deno_version": self.deno_version() if exe else None,
               "root": None if self.root is None else self._show_path(self.root),
               "config_found": graph.judged_is_file(self.boundary, self.config, self._permits), "cache_dir": self.cache_dir}
        if self.boundary != self.project_root:
            out["boundary"] = self._show_path(self.boundary)
        if self._root_refusal:
            out["root_refused"] = self._root_refusal
        if self._boundary_unmodelled:
            out["boundary_refused"] = BOUNDARY_UNMODELLED
        out["cache"] = self._cache.summary()  # entries, bytes, the time of the last sweep (seconds since the epoch) and whether the count is not all known
        if self._index is not None:
            out.update(files_indexed=len(self._index.entries), index_age_s=self._index.age_s())
            if self._index.gaps:
                out["index_gaps"] = dict(self._index.gaps)
        return self._out(out)
