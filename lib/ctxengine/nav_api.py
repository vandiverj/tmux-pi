"""Navigation for a Deno project, as the context engine's API half (CX3): who imports a file, what a file imports, and what it
exports.

Three sources, each used for what it is good at:
  - `graph.ImportIndex` (pure Python, incremental, persisted): importers and direct imports. A warm call costs a directory walk;
    a cold one about a second per thousand files.
  - `deno doc --json`: the exported symbols of one file (name, kind, definition `path:line`, the source line as the signature, the
    first doc-comment lines), cached by the content hash of every file the walk reached.
  - `deno info --json`: the forward module graph of one entry, capped at 30 s. Used when the index cannot be built, or when asked for.

Gaps. An index answer (importers, imports from the index) is never `ok` when the index could not get part of its input. The split:
a file or folder the guard refuses BY ITS NAME is outside the index by policy (not listed, read or counted; its edges are not
named), and is not a gap. A refusal at read time, a path that turns out to be outside the root, a folder, file, entry or import
target that cannot be read or looked at, a name the guard could not judge because it could not look (`unreadable_path`: not a
refusal by name; `graph.CannotJudge`), a config or overlay that is refused, outside, unreadable, not JSON or not modelled (the
same configs the Deno side refuses), and a root that is refused are gaps (`graph.GAPS`). A gap that is a refusal gives `refused`
(with the Deno side's `reason`), a race (a file gone or a link in its place between the walk and the read) `stale_index`, anything
else `error` with `reason`; each carries `gaps` ({code: count}, no paths) and no data. A missing config is no gap (nothing is
there), nor is a missing import target (`missing`). `imports` with `source` of `auto` still falls back to `deno info`, which proves
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
  - There is one way to read a file's bytes in this module and in `graph`: `graph.read_bounded`. It opens the file, asks the
    kernel which file is open, checks that it is the path the caller judged, inside the project and
    allowed by the guard, and only then reads; a file that fails a check is never read. That covers sources, the Deno config,
    overlays, `package.json` files and both caches. A link swapped in between the check of a name and its read gives
    `stale_index` (the walk) or no edges (the index), and its bytes are not read. Where the kernel cannot name
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
        self.project_root = os.path.realpath(project_root)  # the boundary itself
        self.root, self._root_refusal = self.project_root, None
        if root is not None and graph.respell(root, spelled, self.project_root) != self.project_root:
            try:
                self.root = self._judged(graph.respell(root, spelled, self.project_root))
            except graph.Unproven as error:
                self.root, self._root_refusal = None, error.reason
        self.config = os.path.join(self.root or self.project_root, "deno.json") if config is None \
            else graph.respell(config, spelled, self.project_root)  # judged on every read (`graph._config_read`)
        self._config_dir = os.path.dirname(self.config)  # by name; resolved below only when it is shown allowed
        if self._config_dir != self.project_root:
            try:
                self._config_dir = self._judged(self._config_dir)
            except graph.Unproven:
                pass  # `prove_reach` refuses it; the name is still a folder no grant may overlap
        self.overlays = [graph.respell(path, spelled, self.project_root) for path in overlays]
        self.cache_dir = cache_dir if cache_dir is not None else os.path.join(self.project_root, CACHE_RELATIVE)
        self._cache = graph.CacheDir(self.project_root, graph.respell(self.cache_dir, spelled, self.project_root), self._permits)
        self.deno = deno
        self.timeout = timeout
        self.mirror_parent = mirror_parent  # where each call's temporary mirror is made (default: the system's temporary directory)
        self._index = None
        self._version = None
        self._sandbox = False  # False: not tried yet; None: Deno starts under the sandbox; else the reason code (see `_check_sandbox`)

    # --- plumbing

    def _judged(self, path):
        """The resolved absolute path of `path` (inside the project), followed by `graph.judged_realpath` with the guard; raises
        Unproven."""
        return os.path.join(self.project_root, *graph.judged_realpath(self.project_root, path, self._permits)[1].split("/"))

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
        return _digest(self.project_root, self.root, self.config)[:16]

    def _allowed(self, rel):
        """The project-relative path `rel` as the guard names it, or None when the guard refuses it (or fails)."""
        try:
            got = self.check_path(self.project_root, rel)
        except Exception:
            return None
        return got if isinstance(got, str) else None

    def _get_index(self):
        if self._index is None:
            self._index = graph.ImportIndex(self.project_root, self.root, self.config, self.overlays,
                                            cache=(self._cache, self._cache_name("index", self._root_key())), allow=self._permits)
        return self._index

    def _fresh(self):
        """(index, stats, None) after a refresh, or (None, None, answer) when there is nothing to navigate. A root that is not
        shown allowed is refused before anything is asked of it."""
        if self._root_refusal:
            return None, None, _answer("refused", reason=self._root_refusal)
        if not graph.judged_is_dir(self.project_root, self.root, self._permits):
            return None, None, _answer("no_project", detail="project directory not found")
        index = self._get_index()
        try:
            stats = index.refresh()
        except OSError as error:
            return None, None, _answer("error", detail=f"index could not be built: {type(error).__name__}")
        blocked = index.incomplete()
        if blocked:
            return None, None, self._incomplete(*blocked)
        return index, stats, None

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
        try:
            rel = self.check_path(self.project_root, path)
        except Exception as error:
            reason = getattr(error, "reason", None) or str(error)[:200] or "refused"
            return None, _answer("refused", reason=reason)
        if not isinstance(rel, str) or graph.project_path(self.project_root, rel, self._permits) is None:
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

    @staticmethod
    def _slim(edge, side):
        out = {"path": edge[side], "line": edge["line"], "spec": edge["spec"]}
        for flag in ("type_only", "dynamic"):
            if edge[flag]:
                out[flag] = True
        if edge["config"]:
            out["config"] = edge["config"]
        return out

    # --- importers / imports (the index)

    def importers(self, target, limit=MAX_IMPORTERS):
        """Files that import `target` directly, ordered by (path, line); `limit` is 1 to 25."""
        return self._out(self._importers(target, limit))

    def _importers(self, target, limit):
        rel, error = self._target(target)
        if error:
            return error
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_IMPORTERS:
            return _answer("bad_argument", detail=f"limit must be a whole number from 1 to {MAX_IMPORTERS}")
        index, stats, error = self._fresh()
        if error:
            return error
        found = index.importers(rel)
        if not found and not graph.judged_is_file(self.project_root, rel, self._permits):
            return _answer("not_found", detail="no such file in the project")
        status = "stale_index" if stats["stale"] else "ok"
        return _answer(status, target=rel, total=len(found), truncated=len(found) > limit,
                       importers=[self._slim(e, "from") for e in found[:limit]], **self._meta(index, stats))

    def imports(self, target, source="auto"):
        """What `target` imports directly: `files` (project paths), `external` (packages, builtins, URLs) and `unresolved`.
        `source` is `auto` (the index, and `deno info` when no index can be built), `index` or `deno`."""
        return self._final(*self._imports(target, source))

    def _imports(self, target, source):
        """(answer, reach): `reach` is the proof an answer from `deno info` rests on (None for the index)."""
        rel, error = self._target(target)
        if error:
            return error, None
        if source not in ("auto", "index", "deno"):
            return _answer("bad_argument", detail="source must be auto, index or deno"), None
        if source != "deno":
            index, stats, error = self._fresh()
            if error is None:
                edges = index.imports(rel)
                if edges is None:
                    return _answer("not_found", detail="the file is not an indexed source file"), None
                return _answer("stale_index" if stats["stale"] else "ok", target=rel, source="index",
                               files=[self._slim(e, "to") for e in edges if e["kind"] == "file"],
                               external=sorted({e["spec"] for e in edges if e["kind"] == "external"}),
                               unresolved=[{"line": e["line"], "spec": e["spec"], "reason": e["reason"]}
                                           for e in edges if e["kind"] == "unresolved"],
                               **self._meta(index, stats)), None
            if source == "index":
                return error, None
        return self._forward_graph(target)

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
        roots = {path for path in (self.project_root, self.root, self._config_dir) if path}  # each judged in __init__, not resolved here
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
        if _sensitive_location(parent) or parent == self.project_root or parent.startswith(self.project_root + os.sep):
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

    def _permits(self, rel):
        """The guard's verdict as a bool, for `graph`. A guard that refused because it could not look (`unreadable_path`) did not
        judge the name: `graph.CannotJudge`, which every user but the index takes as a refusal, and the index records as a gap."""
        try:
            got = self.check_path(self.project_root, rel)
        except Exception as error:
            if getattr(error, "reason", None) == "unreadable_path":
                raise graph.CannotJudge(rel) from None
            return False
        return isinstance(got, str)

    def _prove(self, rel):
        """(reach, None) when every module Deno would load from `rel` has been shown allowed (`graph.prove_reach`), else
        (None, the refusing answer). The walk reads the files itself and does not use the index, so what the index leaves out (an
        excluded file) and whether the index could be built make no difference. A walk that fails for any other reason is a
        refusal as well."""
        try:
            return graph.prove_reach(self.project_root, os.path.join(self.project_root, rel), self.config, self._permits), None
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

    def _final(self, answer, reach):
        """What a public operation returns: `answer` screened (`_out`), and then, when it is an `ok` answer that rests on a proof
        (`reach`, cached or fresh), `reach.holds()` as the very last step: the screen runs before it, so nothing (the guard
        included) runs between the check and the return of an `ok` answer. Config files count (`Reach.configs`). A tree that moved
        gives `stale_index`, which carries nothing from the answer."""
        out = self._out(answer)
        if reach is None or out["status"] != "ok":
            return out
        if reach.holds():
            return out
        return self._out(_answer("stale_index", detail="a file changed while the answer was being made"))

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
        return self._final(*self._forward_graph(target))

    def _forward_graph(self, target):
        """(answer, reach); the public wrapper makes the final check (`_final`)."""
        rel, error = self._target(target)
        if error:
            return error, None
        if not graph.judged_is_file(self.project_root, rel, self._permits):
            return _answer("not_found", detail="no such file in the project"), None
        reach, refusal = self._prove(rel)
        if refusal:
            return refusal, None
        refusal, out, mirror = self._run_mirrored(["info", "--json", "--no-remote", "--no-lock"], reach)
        if refusal:
            return refusal, None
        try:
            data = json.loads(out)
            modules = data["modules"]
        except (ValueError, KeyError, TypeError):
            return _answer("error", detail="deno info gave output that is not a module graph"), None
        if not isinstance(modules, list):
            return _answer("error", detail="deno info gave output that is not a module graph"), None
        names = reach.names
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
                return _answer("refused", reason="unproven_module"), None  # Deno loaded a file the walk did not show allowed
            deps = set()
            for dep in module.get("dependencies") or []:
                if not isinstance(dep, dict):
                    continue
                for key in ("code", "type"):
                    dest = _file_url_path(dep.get(key, {}).get("specifier")) if isinstance(dep.get(key), dict) else None
                    dest = mirror.to_real(dest) if dest else None
                    if dest and dest in names and self._allowed(dest) is not None:
                        deps.add(dest)
            local[inside] = sorted(deps)
        keep = [path for path in [rel, *local.get(rel, [])] if path in local]
        chosen = set(keep[:MAX_MODULES])
        for path in sorted(local):
            if len(chosen) >= MAX_MODULES:
                break
            chosen.add(path)
        listed = [{"path": path, "imports": local[path]} for path in sorted(chosen)]
        return _answer("ok", target=rel, source="deno_info", modules=listed, truncated=len(local) > len(listed), total=len(local),
                       external=external), reach

    def symbols(self, path, limit=MAX_SYMBOLS):
        """The exported symbols of `path` from `deno doc --json`: name, kind, definition path and line, the definition's source
        line as `signature`, and the first doc-comment lines. A re-export is listed at the file that defines it."""
        return self._final(*self._symbols(path, limit))

    def _symbols(self, path, limit):
        """(answer, reach); the public wrapper makes the final check (`_final`), for a cached answer as for a fresh one."""
        rel, error = self._target(path)
        if error:
            return error, None
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return _answer("bad_argument", detail="limit must be a whole number from 1 to 100"), None
        if not graph.judged_is_file(self.project_root, rel, self._permits):
            return _answer("not_found", detail="no such file in the project"), None
        if self._deno_exe() is None:
            return _answer("unavailable", detail="deno was not found on PATH"), None
        reach, refusal = self._prove(rel)
        if refusal:
            return refusal, None
        reason = self._check_sandbox()
        if reason:  # not even a cached answer: with no sandbox Deno is not run, so nothing is answered from what it said
            return self._sandbox_refusal(reason), None
        names = reach.names
        key = self._doc_key(rel, reach)
        cached = self._read_cache("doc", key)
        if cached is not None:
            found, hit = cached, True
        else:
            refusal, out, mirror = self._run_mirrored(["doc", "--json", "--no-remote", "--no-lock"], reach)
            if refusal:
                return refusal, None
            found = self._parse_doc(out, names, mirror, reach)
            if found is None:
                return _answer("error", detail="deno doc gave output that is not a symbol list"), None
            self._write_cache("doc", key, found)
            hit = False
        shown = [item for item in (self._present(entry, names) for entry in found) if item]
        return _answer("ok", path=rel, source="deno_doc", cached=hit, total=len(shown), truncated=len(shown) > limit,
                       symbols=shown[:limit]), reach

    def _present(self, entry, names):
        """One cached or fresh symbol as it may leave the process: None when its path is refused now, or is not one of the files
        the walk showed allowed (`names`). The text itself is screened by `_out` on the way out, so nothing read back from the
        cache skips the screen."""
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or entry["path"] not in names \
                or self._allowed(entry["path"]) is None:
            return None
        return {key: entry.get(key, "") for key in ("name", "kind", "path", "line", "signature", "doc")}

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

    def _read_cache(self, kind, key):
        """The cached list, read by the judged cache folder (`graph.CacheDir.read_json`: `read_bounded`, no link, a plain `.json`
        directly in the folder, the guard judging its name), or None."""
        data = self._cache.read_json(self._cache_name(kind, key))
        return data if isinstance(data, list) else None

    def _write_cache(self, kind, key, value):
        """Written through the judged cache folder (`graph.CacheDir.write_json`); nothing when the folder was refused."""
        self._cache.write_json(self._cache_name(kind, key), value)

    # --- status

    def status(self):
        """What this instance would use: the Deno it found, the root and config, the cache folder (and why it is refused, when it
        is), and the index's size and age. The config is looked at only when it is shown allowed."""
        exe = self._deno_exe()
        out = {"status": "ok", "deno": exe, "deno_version": self.deno_version() if exe else None,
               "root": None if self.root is None else (graph.lexical_path(self.project_root, self.root) or "."),
               "config_found": graph.judged_is_file(self.project_root, self.config, self._permits), "cache_dir": self.cache_dir}
        if self._root_refusal:
            out["root_refused"] = self._root_refusal
        if self._index is not None:
            out.update(files_indexed=len(self._index.entries), index_age_s=self._index.age_s())
            if self._index.gaps:
                out["index_gaps"] = dict(self._index.gaps)
        return self._out(out)
