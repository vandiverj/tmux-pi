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

No network, ever: `deno` is run with `--no-remote --no-lock`, a registry that cannot be reached (`--no-remote` alone does not stop npm
downloads) and a minimal environment. `--no-lock` keeps `deno.lock` unwritten.

Until CX1 merges, the path guard and the output screen here are STUBS (`stub_guard_path`, `stub_screen`) with the shape CX1's
`redact.py` is meant to offer: `guard(path, root) -> None | reason_code` and `screen(text) -> text`. Pass the real ones to `NavApi`."""
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


def stub_guard_path(path, root):
    """STUB for CX1's path guard. None when `path` may be read, else a reason code: `outside_project` (outside `root`, by its
    name or by where a symlink in it leads) or `sensitive_path` (`.env*`, `config/local.json`, `agent/auth.json`, keys, secrets)."""
    real_root = os.path.realpath(root)
    full = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, path))
    real = os.path.realpath(full)
    if real == real_root or not real.startswith(real_root + os.sep):
        return "outside_project"
    for candidate in (full, real):
        parts = candidate.split(os.sep)
        name = parts[-1].lower()
        tail = "/".join(parts[-2:]).lower()
        if name.startswith(".env") or tail in ("config/local.json", "agent/auth.json") or name.endswith(_SENSITIVE_SUFFIXES) \
                or name.startswith(_SENSITIVE_NAMES) or any(part in _SENSITIVE_DIRS for part in parts):
            return "sensitive_path"
    return None


def stub_screen(text):
    """STUB for CX1's output screen: returns `text` unchanged. Everything that comes from a file's comments goes through it."""
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
    project root) and are named after the root, so two projects never share one."""

    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=DEFAULT_TIMEOUT,
                 guard=stub_guard_path, screen=stub_screen):
        self.project_root = os.path.realpath(project_root)
        self.root = os.path.realpath(root or project_root)
        self.config = os.path.join(self.root, "deno.json") if config is None else os.path.realpath(config)
        self.overlays = list(overlays)
        self.cache_dir = cache_dir if cache_dir is not None else os.path.join(self.project_root, CACHE_RELATIVE)
        self.deno = deno
        self.timeout = timeout
        self.guard = guard
        self.screen = screen
        self._index = None
        self._version = None

    # --- plumbing

    def _cache_file(self, kind, key):
        return os.path.join(self.cache_dir, f"nav_api-{kind}-{key}.json")

    def _root_key(self):
        return _digest(self.project_root, self.root, self.config)[:16]

    def _get_index(self):
        if self._index is None:
            self._index = graph.ImportIndex(self.project_root, self.root, self.config, self.overlays,
                                            self._cache_file("index", self._root_key()))
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
        reason = self.guard(path, self.project_root)
        if reason:
            return None, _answer("refused", reason=reason)
        rel = graph.project_path(self.project_root, path)
        if rel is None:
            return None, _answer("refused", reason="outside_project")
        return rel, None

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
        return self.forward_graph(target)

    # --- deno

    def _deno_exe(self):
        return shutil.which(self.deno)

    def _env(self):
        env = {key: os.environ[key] for key in DENO_ENV_KEEP if key in os.environ}
        env.update(OFFLINE_ENV)
        return env

    def _run_deno(self, args):
        """(status, stdout, detail): runs `deno` with `args` in the config's directory, never longer than the timeout."""
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
            return "error", "", self.screen(first.strip()[:200]) or "deno failed"
        return "ok", run.stdout.decode("utf-8", "replace"), ""

    def _config_args(self):
        return ["--config", self.config] if os.path.isfile(self.config) else []

    def deno_version(self):
        """First line of `deno --version`, or None when Deno cannot be run."""
        if self._version is None:
            status, out, _ = self._run_deno(["--version"])
            self._version = out.splitlines()[0].strip() if status == "ok" and out.strip() else ""
        return self._version or None

    def forward_graph(self, target):
        """The local modules `target` reaches, from `deno info --json` (30 s cap): {"modules": [{"path", "imports": [paths]}]}.
        Only files inside the project are listed; packages and URLs are counted in `external`."""
        rel, error = self._target(target)
        if error:
            return error
        full = os.path.join(self.project_root, rel)
        if not os.path.isfile(full):
            return _answer("not_found", detail="no such file in the project")
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
        local, external = {}, 0
        for module in modules:
            if not isinstance(module, dict):
                continue
            path = _file_url_path(module.get("specifier"))
            inside = graph.project_path(self.project_root, path) if path else None
            if inside is None:
                external += 1
                continue
            deps = set()
            for dep in module.get("dependencies") or []:
                if not isinstance(dep, dict):
                    continue
                for key in ("code", "type"):
                    dest = _file_url_path(dep.get(key, {}).get("specifier")) if isinstance(dep.get(key), dict) else None
                    dest = graph.project_path(self.project_root, dest) if dest else None
                    if dest:
                        deps.add(dest)
            local[inside] = sorted(deps)
        listed = [{"path": path, "imports": local[path]} for path in sorted(local)]
        return _answer("ok", target=rel, source="deno_info", modules=listed[:200], truncated=len(listed) > 200, total=len(listed),
                       external=external)

    def symbols(self, path, limit=MAX_SYMBOLS):
        """The exported symbols of `path` from `deno doc --json`: name, kind, definition path and line, the definition's source
        line as `signature`, and the first doc-comment lines. A re-export is listed at the file that defines it."""
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
        key = self._doc_key(rel, full)
        cached = self._read_cache("doc", key) if key else None
        if cached is not None:
            found, hit = cached, True
        else:
            status, out, detail = self._run_deno(["doc", "--json", "--no-lock", *self._config_args(), full])
            if status != "ok":
                return _answer(status, detail=detail)
            found = self._parse_doc(out)
            if found is None:
                return _answer("error", detail="deno doc gave output that is not a symbol list")
            if key:
                self._write_cache("doc", key, found)
            hit = False
        return _answer("ok", path=rel, source="deno_doc", cached=hit, total=len(found), truncated=len(found) > limit,
                       symbols=found[:limit])

    def _parse_doc(self, text):
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
                    if rel is None:
                        continue  # defined outside the project (a package): not navigable here
                    line = int(decl["location"].get("line", 0)) + 1  # deno doc counts lines from 0
                    if rel not in lines_of:
                        try:
                            with open(where, "r", encoding="utf-8", errors="replace") as handle:
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
                                  "signature": self.screen(signature[:SIGNATURE_MAX]), "doc": self.screen(doc[:DOC_LINE_MAX])})
        found.sort(key=lambda s: (s["path"], s["line"], s["name"]))
        return found

    # --- the doc cache

    def _doc_key(self, rel, full):
        """Cache key for `deno doc` of one file: the file's content hash, the stat keys of every local file it reaches through
        the index (a re-export is resolved from those), the config's content, and the Deno version. None when the file cannot
        be read."""
        try:
            with open(full, "rb") as handle:
                content = handle.read()
        except OSError:
            return None
        try:
            with open(self.config, "rb") as handle:
                config = handle.read()
        except OSError:
            config = b""
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
        stats = [(path, *index.entries[path]["key"]) if path in index.entries else (path,) for path in sorted(reached)]
        return _digest(rel, content, config, json.dumps(stats), self.deno_version() or "")[:40]

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
        return out
