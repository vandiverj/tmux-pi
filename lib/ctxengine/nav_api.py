"""Navigation for a Deno project, as the context engine's API half (CX3): who imports a file, what a file imports, and what it
exports.

Three sources, each used for what it is good at:
  - `graph.ImportIndex` (pure Python, incremental, persisted): importers and direct imports. A warm call costs a directory walk;
    a cold one about a second per thousand files.
  - `deno doc --json`: the exported symbols of one file (name, kind, definition `path:line`, the source line as the signature, the
    first doc-comment lines), cached by the file's content hash plus the stat keys of the local files it reaches.
  - `deno info --json`: the forward module graph of one entry, capped at 30 s. Used when the index cannot be built, or when asked for.

Every answer is a dict with a `status`: `ok`, `unavailable` (no `deno`, for the operations that need it), `no_project` (no such
directory), `refused` (a path outside the project, or a sensitive one), `bad_argument`, `not_found`, `stale_index` (a file changed
while it was being read; ask again), `timeout`, `error`. Anything other than `ok` carries a one-line `next_step`.

No network, ever: `deno info` and `deno doc` are run with `--no-remote --no-lock`, a registry that cannot be reached (`--no-remote` alone does not stop npm
downloads) and a minimal environment. `--no-lock` keeps `deno.lock` unwritten.

Boundaries. Every path argument goes through `check_path(project, path)`, and so does every file the index would read or an edge
would name: a refused file is never read, never indexed and never named in an answer. Before `deno info` or `deno doc` runs, the
files the target reaches through the index are checked; if one of them leaves the project or is refused, the call is refused
(`reaches_refused_path`) and Deno does not run. What Deno reports is filtered again by `check_path`. Every string in an answer,
including one read back from the cache, passes through `guard(text)` on its way out; a guard that raises gives an `error` answer.

Until CX1 merges, `check_path` and `guard` here are STUBS (`stub_check_path`, `stub_guard`) with the signatures of CX1's
`redact.check_path(project, path) -> relative posix path | raises Refused` and `redact.guard(text) -> text`, so CX4 swaps them in
by passing `check_path=redact.check_path, guard=redact.guard` to `NavApi`. Any exception from `check_path` is a refusal."""
import hashlib
import json
import os
import shutil
import subprocess
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


def stub_check_path(project, value):
    """STUB for CX1's `redact.check_path(project, path)`: the project-relative posix path `value` names, or Refused. Refused:
    `outside_project` (outside `project` by its name or where a symlink in it leads), `sensitive_path` (`.env*`,
    `config/local.json`, `agent/auth.json`, keys, secrets), `project_root`."""
    real_root = os.path.realpath(str(project))
    full = os.path.normpath(value if os.path.isabs(value) else os.path.join(real_root, value))
    real = os.path.realpath(full)
    if real == real_root:
        raise Refused("project_root")
    if not real.startswith(real_root + os.sep):
        raise Refused("outside_project")
    for candidate in (full, real):
        parts = os.path.relpath(candidate, real_root).split(os.sep)  # the names inside the project, not the ones above it
        name = parts[-1].lower()
        tail = "/".join(parts[-2:]).lower()
        if name.startswith(".env") or tail in ("config/local.json", "agent/auth.json") or name.endswith(_SENSITIVE_SUFFIXES) \
                or name.startswith(_SENSITIVE_NAMES) or any(part in _SENSITIVE_DIRS for part in parts):
            raise Refused("sensitive_path")
    return os.path.relpath(real, real_root).replace(os.sep, "/")


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


def _read_bytes(path):
    """The bytes of `path` (at most graph.MAX_BYTES + 1), or None when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return handle.read(graph.MAX_BYTES + 1)
    except OSError:
        return None


def _file_url_path(url):
    return os.path.normpath(unquote(url[len("file://"):])) if isinstance(url, str) and url.startswith("file://") else None


class NavApi:
    """Navigation questions about the Deno project below `root` (default `project_root`); paths in and out are relative to
    `project_root`. One instance keeps one index; its cache files live in `cache_dir` (default `.tmux-pi/ctx-cache` under the
    project root) and are named after the root, so two projects never share one.

    `check_path(project, path)` and `guard(text)` are the path guard and output screen (see the module docstring); the cache holds
    what Deno said, and both run again on every read of it."""

    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=DEFAULT_TIMEOUT,
                 check_path=stub_check_path, guard=stub_guard):
        self.project_root = os.path.realpath(project_root)
        self.root = os.path.realpath(root or project_root)
        self.config = os.path.join(self.root, "deno.json") if config is None else os.path.realpath(config)
        self.overlays = list(overlays)
        self.cache_dir = cache_dir if cache_dir is not None else os.path.join(self.project_root, CACHE_RELATIVE)
        self.deno = deno
        self.timeout = timeout
        self.check_path = check_path
        self.guard = guard
        self._index = None
        self._version = None

    # --- plumbing

    def _cache_file(self, kind, key):
        return os.path.join(self.cache_dir, f"nav_api-{kind}-{key}.json")

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
                                            self._cache_file("index", self._root_key()),
                                            allow=lambda rel: self._allowed(rel) is not None)
        return self._index

    def _fresh(self):
        """(index, stats, None) after a refresh, or (None, None, answer) when there is nothing to navigate."""
        if not os.path.isdir(self.root):
            return None, None, _answer("no_project", detail="project directory not found")
        index = self._get_index()
        try:
            stats = index.refresh()
        except OSError as error:
            return None, None, _answer("error", detail=f"index could not be built: {type(error).__name__}")
        return index, stats, None

    def _target(self, path):
        """(relative path, None) for a path that may be asked about, or (None, answer)."""
        if not isinstance(path, str) or not path.strip() or "\0" in path:
            return None, _answer("bad_argument", detail="path must be a non-empty string")
        try:
            rel = self.check_path(self.project_root, path)
        except Exception as error:
            reason = getattr(error, "reason", None) or str(error)[:200] or "refused"
            return None, _answer("refused", reason=reason)
        if not isinstance(rel, str) or graph.project_path(self.project_root, rel) is None:
            return None, _answer("refused", reason="outside_project")
        return rel, None

    def _out(self, answer):
        """`answer` with every string passed through the output screen (the status word excepted); an error answer if it fails."""
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
        if not found and not os.path.isfile(os.path.join(self.project_root, rel)):
            return _answer("not_found", detail="no such file in the project")
        status = "stale_index" if stats["stale"] else "ok"
        return _answer(status, target=rel, total=len(found), truncated=len(found) > limit,
                       importers=[self._slim(e, "from") for e in found[:limit]], **self._meta(index, stats))

    def imports(self, target, source="auto"):
        """What `target` imports directly: `files` (project paths), `external` (packages, builtins, URLs) and `unresolved`.
        `source` is `auto` (the index, and `deno info` when no index can be built), `index` or `deno`."""
        return self._out(self._imports(target, source))

    def _imports(self, target, source):
        rel, error = self._target(target)
        if error:
            return error
        if source not in ("auto", "index", "deno"):
            return _answer("bad_argument", detail="source must be auto, index or deno")
        if source != "deno":
            index, stats, error = self._fresh()
            if error is None:
                edges = index.imports(rel)
                if edges is None:
                    return _answer("not_found", detail="the file is not an indexed source file")
                return _answer("stale_index" if stats["stale"] else "ok", target=rel, source="index",
                               files=[self._slim(e, "to") for e in edges if e["kind"] == "file"],
                               external=sorted({e["spec"] for e in edges if e["kind"] == "external"}),
                               unresolved=[{"line": e["line"], "spec": e["spec"], "reason": e["reason"]}
                                           for e in edges if e["kind"] == "unresolved"],
                               **self._meta(index, stats))
            if source == "index":
                return error
        return self._forward_graph(target)

    # --- deno

    def _deno_exe(self):
        return shutil.which(self.deno)

    def _env(self):
        env = {key: os.environ[key] for key in DENO_ENV_KEEP if key in os.environ}
        env.update(OFFLINE_ENV)
        return env

    def _run_deno(self, args):
        """(status, stdout, detail): runs `deno` with `args` in the config's directory, never longer than the timeout. The detail
        is not screened here; `_out` screens the answer it ends up in."""
        exe = self._deno_exe()
        if exe is None:
            return "unavailable", "", "deno was not found on PATH"
        cwd = os.path.dirname(self.config) if os.path.isdir(os.path.dirname(self.config)) else self.root
        try:
            run = subprocess.run([exe, *args], cwd=cwd, env=self._env(), stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return "timeout", "", f"deno took longer than {self.timeout} s"
        except OSError as error:
            return "unavailable", "", f"deno could not be started: {type(error).__name__}"
        if len(run.stdout) > MAX_DENO_OUTPUT:
            return "error", "", "deno output was too large"
        if run.returncode != 0:
            lines = run.stderr.decode("utf-8", "replace").strip().splitlines()
            first = next((line for line in lines if line.strip() and not line.startswith(("Download", "Warning"))), "")
            return "error", "", first.strip()[:200] or "deno failed"
        return "ok", run.stdout.decode("utf-8", "replace"), ""

    def _config_args(self):
        return ["--config", self.config] if os.path.isfile(self.config) else []

    def deno_version(self):
        """First line of `deno --version`, or None when Deno cannot be run."""
        if self._version is None:
            status, out, _ = self._run_deno(["--version"])
            self._version = out.splitlines()[0].strip() if status == "ok" and out.strip() else ""
        return self._version or None

    @staticmethod
    def _reaches_refused(index, rel):
        """True when `rel`, or a file it reaches through the index, imports something that leaves the project or that the guard
        refuses: Deno would read it."""
        seen, queue = {rel}, [rel]
        while queue:
            for edge in index.imports(queue.pop()) or []:
                if edge["config"] is not None:
                    continue
                if edge["kind"] == "unresolved" and edge["reason"] in ("outside", "refused"):
                    return True
                if edge["kind"] == "file" and edge["to"] not in seen:
                    seen.add(edge["to"])
                    queue.append(edge["to"])
        return False

    def _precheck(self, rel):
        """None when Deno may be run on `rel`, else the refusing answer. Without an index the check is skipped (the forward graph is
        the fallback for exactly that case) and only what Deno reports is filtered."""
        index, _, error = self._fresh()
        if error is None and self._reaches_refused(index, rel):
            return _answer("refused", reason="reaches_refused_path")
        return None

    def forward_graph(self, target):
        """The local modules `target` reaches, from `deno info --json` (30 s cap): {"modules": [{"path", "imports": [paths]}]}.
        Only files inside the project are listed; packages and URLs are counted in `external`. When the graph is longer than
        MAX_MODULES, `target` and its direct imports are kept and the rest is cut in path order."""
        return self._out(self._forward_graph(target))

    def _forward_graph(self, target):
        rel, error = self._target(target)
        if error:
            return error
        full = os.path.join(self.project_root, rel)
        if not os.path.isfile(full):
            return _answer("not_found", detail="no such file in the project")
        refusal = self._precheck(rel)
        if refusal:
            return refusal
        status, out, detail = self._run_deno(["info", "--json", "--no-remote", "--no-lock", *self._config_args(), full])
        if status != "ok":
            return _answer(status, detail=detail)
        try:
            data = json.loads(out)
            modules = data["modules"]
        except (ValueError, KeyError, TypeError):
            return _answer("error", detail="deno info gave output that is not a module graph")
        if not isinstance(modules, list):
            return _answer("error", detail="deno info gave output that is not a module graph")
        local, external, refused = {}, 0, 0
        for module in modules:
            if not isinstance(module, dict):
                continue
            path = _file_url_path(module.get("specifier"))
            inside = graph.project_path(self.project_root, path) if path else None
            if inside is None:
                external += 1
                continue
            if self._allowed(inside) is None:
                refused += 1
                continue
            deps = set()
            for dep in module.get("dependencies") or []:
                if not isinstance(dep, dict):
                    continue
                for key in ("code", "type"):
                    dest = _file_url_path(dep.get(key, {}).get("specifier")) if isinstance(dep.get(key), dict) else None
                    dest = graph.project_path(self.project_root, dest) if dest else None
                    if dest and self._allowed(dest) is not None:
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
                       external=external, refused=refused)

    def symbols(self, path, limit=MAX_SYMBOLS):
        """The exported symbols of `path` from `deno doc --json`: name, kind, definition path and line, the definition's source
        line as `signature`, and the first doc-comment lines. A re-export is listed at the file that defines it."""
        return self._out(self._symbols(path, limit))

    def _symbols(self, path, limit):
        rel, error = self._target(path)
        if error:
            return error
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return _answer("bad_argument", detail="limit must be a whole number from 1 to 100")
        full = os.path.join(self.project_root, rel)
        if not os.path.isfile(full):
            return _answer("not_found", detail="no such file in the project")
        if self._deno_exe() is None:
            return _answer("unavailable", detail="deno was not found on PATH")
        refusal = self._precheck(rel)
        if refusal:
            return refusal
        key = self._doc_key(rel, full)
        cached = self._read_cache("doc", key) if key else None
        if cached is not None:
            found, hit = cached, True
        else:
            status, out, detail = self._run_deno(["doc", "--json", "--no-remote", "--no-lock", *self._config_args(), full])
            if status != "ok":
                return _answer(status, detail=detail)
            found = self._parse_doc(out)
            if found is None:
                return _answer("error", detail="deno doc gave output that is not a symbol list")
            if key:
                self._write_cache("doc", key, found)
            hit = False
        shown = [item for item in (self._present(entry) for entry in found) if item]
        return _answer("ok", path=rel, source="deno_doc", cached=hit, total=len(shown), truncated=len(shown) > limit,
                       symbols=shown[:limit])

    def _present(self, entry):
        """One cached or fresh symbol as it may leave the process: None when its path is refused now. The text itself is screened by
        `_out` on the way out, so nothing read back from the cache skips the screen."""
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or self._allowed(entry["path"]) is None:
            return None
        return {key: entry.get(key, "") for key in ("name", "kind", "path", "line", "signature", "doc")}

    def _parse_doc(self, text):
        """The symbol list for the cache: raw as Deno gave it (cut to length), for files the guard allows now. It is screened when it
        is read, never trusted as already screened."""
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
                    rel = graph.project_path(self.project_root, where) if where else None
                    if rel is None or self._allowed(rel) is None:
                        continue  # defined outside the project (a package), or a file the guard refuses: not navigable here
                    line = int(decl["location"].get("line", 0)) + 1  # deno doc counts lines from 0
                    if rel not in lines_of:
                        try:
                            with open(os.path.join(self.project_root, rel), "r", encoding="utf-8", errors="replace") as handle:
                                lines_of[rel] = handle.read().splitlines()
                        except OSError:
                            lines_of[rel] = []
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

    def _doc_key(self, rel, full):
        """Cache key for `deno doc` of one file: a hash of the file's content, of the content of every local file it reaches
        through the index (a re-export is resolved from those), of the config, and the Deno version. A size and a modification
        time do not stand for content (a checkout can restore both), so none is used. None when the file cannot be read."""
        content = _read_bytes(full)
        if content is None:
            return None
        index = self._get_index()
        try:
            index.refresh()
        except OSError:
            return None
        reached, queue = set(), [rel]
        while queue:
            at = queue.pop()
            for edge in index.imports(at) or []:
                if edge["kind"] == "file" and edge["to"] not in reached and edge["to"] != rel and edge["config"] is None:
                    reached.add(edge["to"])
                    queue.append(edge["to"])
        parts = [rel, content, _read_bytes(self.config) or b"", self.deno_version() or ""]
        for path in sorted(reached):
            data = _read_bytes(os.path.join(self.project_root, path))
            parts += [path, "-" if data is None else hashlib.sha256(data).hexdigest()]
        return _digest(*parts)[:40]

    def _read_cache(self, kind, key):
        path = self._cache_file(kind, key)
        if os.path.islink(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, list) else None

    def _write_cache(self, kind, key, value):
        path = self._cache_file(kind, key)
        if os.path.islink(path):
            return
        temp = f"{path}.{os.getpid()}.tmp"
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(value, handle, separators=(",", ":"))
            os.replace(temp, path)
        except OSError:
            try:
                os.unlink(temp)
            except OSError:
                pass

    # --- status

    def status(self):
        """What this instance would use: the Deno it found, the root and config, and the index's size and age."""
        exe = self._deno_exe()
        out = {"status": "ok", "deno": exe, "deno_version": self.deno_version() if exe else None, "root": os.path.relpath(
            self.root, self.project_root).replace(os.sep, "/"), "config_found": os.path.isfile(self.config),
            "cache_dir": self.cache_dir}
        if self._index is not None:
            out.update(files_indexed=len(self._index.entries), index_age_s=self._index.age_s())
        return self._out(out)
