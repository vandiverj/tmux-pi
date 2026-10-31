"""TypeScript navigation for the context engine (CX2 ctx-nav-web): definition, references, exports and import edges.

One long-lived Node child per `NavWeb` runs a TypeScript language service built from one tsconfig (`paths`, `references`), using the
project's own `typescript` from its `node_modules` (never a copy shipped here). It reads the working tree, so an edited file changes
the next answer without a restart. Python and Node talk JSON lines over stdio; nothing opens a socket and nothing is written.

Every answer is a dict with a `status`: `ok`, `not_found`, or one of
  refused        the path is outside the root, a symlink leaves it, or the path is sensitive (the guard decides)
  no_project     no tsconfig, or it cannot be parsed
  no_typescript  no node_modules/typescript between the tsconfig folder and the root
  not_in_project the file is not in this tsconfig's program (ask with the tsconfig that owns it)
  bad_request    a missing or wrong argument
  stale_index    the file changed while the answer was computed (retried once before this is returned)
  warming        the first question to a new helper builds the whole program and is still running after `timeout` seconds; the
                 helper keeps working and answers the next call when done (a helper that has answered nothing for WARM_LIMIT is hung)
  timeout        a call took longer than `timeout` seconds on a helper that has answered before; the helper is stopped and
                 restarts on the next call
  crashed        the helper died (retried once on a fresh helper before this is returned)
  unavailable    node is missing, or the output screen could not run (fail closed: nothing is returned unscreened)
and, except for `ok`/`not_found`, a `message` and a one-line `next` step. Paths in answers are relative to the root, `path:line`
style (`path`, `line`, `column`, all 1-based). Locations outside the root are dropped and counted (`omitted_outside_root`);
locations in `node_modules` carry `external: true` (definitions) or are dropped and counted (references).

Guard: `StubGuard` below stands in for CX1's `lib/ctxengine/redact.py` until that merges. It has the two entry points the engine
needs, `check_path(path, root)` -> reason or None and `screen(text)` -> text, and is built from the existing `sensitive_read` and
`redact` modules (no new patterns). When CX1 lands, pass its guard as `NavWeb(..., guard=...)` or replace `StubGuard`.

The guard is applied twice. Before the helper reads: it asks this process (two inherited pipes, see `_serve_guard`) about every file, folder
and listing entry TypeScript touches, module resolution included, and a refused path is treated as absent (an import of it is unresolved
and its text is never loaded); anything outside the root is absent too, except TypeScript's own `lib` folder. Before an answer leaves: the
whole answer is walked, rows and path values the guard refuses are withheld, and every other string is screened.
"""
from collections import deque
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

CALL_TIMEOUT = 20.0   # seconds per call (design §8 CX2 step 3)
START_TIMEOUT = 20.0
# The first question to a new helper builds the whole program (measured on apps/launchpad/web: far over 20 s, see the lane report), so it is
# not killed at the call timeout: the caller gets `warming`, the helper keeps working, and the next call is answered once it is done.
# Only a helper that has answered nothing for this long is treated as hung.
WARM_LIMIT = 300.0
NODE_FLAGS = ("--max-old-space-size=2048",)
CHILD_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")  # nothing else (tokens, keys) reaches the child
SERVER = Path(__file__).resolve().parent / "nav_web" / "server.mjs"
PATH_KEYS = frozenset({"resolved", "defined_at", "to", "target"})  # values that name a file: withheld when the guard refuses it
# Every other string in an answer is screened before it leaves, whatever its key.
OPS = ("definition", "references", "exports", "imports", "stats")
WITHHELD = "<withheld>"
STDERR_TAIL = 8


class StubGuard:
    """STUB for CX1's lib/ctxengine/redact.py guard (same entry points). Replace when CX1 merges."""

    def __init__(self):
        self._redactor = None

    @staticmethod
    def _lib():
        lib = str(Path(__file__).resolve().parent.parent)
        if lib not in sys.path:
            sys.path.insert(0, lib)

    def check_path(self, path, root):
        """None when `path` (resolved through symlinks) is inside `root` and not sensitive, else a short reason."""
        reason = None
        real_root = os.path.realpath(root)
        real = os.path.realpath(path)
        if real != real_root and not real.startswith(real_root + os.sep):
            reason = "outside_root"
        else:
            self._lib()
            import sensitive_read  # noqa: PLC0415
            if sensitive_read.sensitive_path(real) or sensitive_read.sensitive_path(str(path)):
                reason = "sensitive_path"
        return reason

    def screen(self, text):
        """`text` with secrets replaced. Raises if the screen cannot run: the caller fails closed."""
        if self._redactor is None:
            self._lib()
            from redact import Redactor  # noqa: PLC0415
            from redaction_store import Store  # noqa: PLC0415
            self._redactor = Redactor(Store(persist=False))
        return self._redactor.redact(str(text))[0]


def find_tsconfig(file, root):
    """The nearest `tsconfig.json` from `file`'s folder up to `root` (inclusive), or None. Never looks above the root."""
    root = Path(os.path.realpath(root))
    here = Path(os.path.realpath(file))
    here = here if here.is_dir() else here.parent
    while True:
        candidate = here / "tsconfig.json"
        if candidate.is_file():
            return candidate
        if here == root or here.parent == here or root not in here.parents:
            return None
        here = here.parent


def _result(status, message="", next_step=""):
    out = {"status": status}
    if message:
        out["message"] = message
    if next_step:
        out["next"] = next_step
    return out


class NavWeb:
    def __init__(self, root, tsconfig, typescript=None, node=None, server=None, timeout=CALL_TIMEOUT, guard=None):
        self.root = os.path.realpath(root)
        self.tsconfig = os.path.abspath(os.path.join(self.root, str(tsconfig)))
        self.typescript = typescript
        self.node = node
        self.server = str(server or SERVER)
        self.timeout = timeout
        self.guard = guard or StubGuard()
        self.launches = 0
        self.ready = None
        self._answered = False      # has this helper answered a request yet (its program is built)
        self._launched_at = 0.0
        self._proc = None
        self._lines = None
        self._stderr = deque(maxlen=STDERR_TAIL)
        self._next_id = 0
        self._lock = threading.Lock()

    # ---- life cycle ------------------------------------------------------------------------------------------------
    @property
    def restarts(self):
        """Helpers started after the first (after a crash, a timeout or a stop)."""
        return max(0, self.launches - 1)

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None and self._proc.poll() is None else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        with self._lock:
            self._kill()

    def _kill(self):
        proc, self._proc, self._lines, self.ready = self._proc, None, None, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, 15)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, 9)
                except (OSError, ProcessLookupError):
                    pass
                proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    @staticmethod
    def _pump(stream, sink):
        try:
            for line in stream:
                sink(line)
        except (OSError, ValueError):
            pass
        finally:
            sink(None)

    def _ensure(self):
        """None when a ready helper is running (starting one if needed), else the error answer."""
        if self._proc is not None and self._proc.poll() is None and self.ready is not None:
            return None
        if self._proc is not None:
            self._kill()
        if not os.path.isfile(self.tsconfig):
            return _result("no_project", "tsconfig not found", "pass the tsconfig.json of the package that owns the file")
        node = self.node or shutil.which("node")
        if not node:
            return _result("unavailable", "node is not installed or not on PATH", "install node (outside this tool)")
        # Two pipes carry the guard to the helper: it asks (a path out), this process answers from `self.guard` (1 = may read, 0 = refused).
        ask_r, ask_w = os.pipe()
        answer_r, answer_w = os.pipe()
        argv = [node, *NODE_FLAGS, self.server, "--root", self.root, "--tsconfig", self.tsconfig, "--guard-fds", f"{ask_w},{answer_r}"]
        if self.typescript:
            argv += ["--typescript", str(self.typescript)]
        env = {k: os.environ[k] for k in CHILD_ENV if k in os.environ}
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                    cwd=self.root, start_new_session=True, close_fds=True, pass_fds=(ask_w, answer_r))
        except OSError as error:
            for fd in (ask_r, answer_w):
                os.close(fd)
            return _result("unavailable", f"cannot start node: {error.strerror or error}", "check the node install")
        finally:
            for fd in (ask_w, answer_r):  # the helper holds its own copies now
                os.close(fd)
        threading.Thread(target=self._serve_guard, args=(ask_r, answer_w), daemon=True).start()
        lines = queue.Queue()
        threading.Thread(target=self._pump, args=(proc.stdout, lines.put), daemon=True).start()
        threading.Thread(target=self._pump, args=(proc.stderr, lambda l: l is not None and self._stderr.append(l)), daemon=True).start()
        self._proc, self._lines = proc, lines
        self.launches += 1
        self._answered, self._launched_at = False, time.monotonic()
        first = self._read(time.monotonic() + START_TIMEOUT)
        if first is None:
            self._kill()
            return _result("timeout", f"the helper did not start within {START_TIMEOUT:g} s", "retry; if it repeats, the tsconfig is too large")
        if first == "eof":
            self._kill()
            return _result("crashed", "the helper exited before it was ready" + self._stderr_note(), "retry once; report if it repeats")
        if first.get("event") != "ready":
            self._kill()
            return {k: v for k, v in first.items() if k in ("status", "message", "next")}
        self.ready = first
        return None

    def _serve_guard(self, ask_r, answer_w):
        """Answer the helper's path questions until it goes away. Any doubt (an error in the guard, a bad line) is a refusal."""
        try:
            with os.fdopen(ask_r, "rb") as asks, os.fdopen(answer_w, "wb", buffering=0) as answers:
                for raw in asks:
                    try:
                        asked = json.loads(raw)
                        allowed = isinstance(asked, str) and bool(asked) and not self.guard.check_path(asked, self.root)
                    except Exception:  # noqa: BLE001 - a guard that fails refuses
                        allowed = False
                    answers.write(b"1\n" if allowed else b"0\n")
        except (OSError, ValueError):
            pass

    def _stderr_note(self):
        tail = " ".join(l.decode("utf-8", "replace").strip() for l in self._stderr)[:300]
        return f" ({tail})" if tail else ""

    def _read(self, deadline):
        """The next JSON line from the helper; None on timeout, 'eof' when it exited."""
        while True:
            try:
                raw = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            if raw is None:
                return "eof"
            try:
                value = json.loads(raw)
            except ValueError:
                continue  # a stray non-protocol line
            if isinstance(value, dict):
                return value

    # ---- calls -----------------------------------------------------------------------------------------------------
    def _roundtrip(self, op, args):
        self._next_id += 1
        ident = self._next_id
        line = json.dumps({**args, "id": ident, "op": op}, ensure_ascii=True) + "\n"
        deadline = time.monotonic() + self.timeout
        try:
            self._proc.stdin.write(line.encode("ascii"))
            self._proc.stdin.flush()
        except (OSError, ValueError):
            self._kill()
            return _result("crashed", "the helper is gone" + self._stderr_note(), "retry")
        while True:
            value = self._read(deadline)
            if value is None:
                if not self._answered and time.monotonic() - self._launched_at < WARM_LIMIT:
                    return _result("warming", "the helper is still reading the project (the first question builds the whole program)",
                                   "ask again in a minute; it answers as soon as the build is done")
                self._kill()
                return _result("timeout", f"the call took longer than {self.timeout:g} s; the helper was stopped",
                               "ask a narrower question; the helper restarts on the next call")
            if value == "eof":
                self._kill()
                return _result("crashed", "the helper exited during the call" + self._stderr_note(), "retry once; report if it repeats")
            if "id" in value:
                self._answered = True  # any answer, even one to an earlier call that gave up waiting
            if value.get("id") == ident:
                value.pop("id", None)
                return value

    def _check_file(self, args):
        file = args.get("file")
        if not isinstance(file, str) or not file:
            return _result("bad_request", "file is required", "pass file as a path string")
        absolute = file if os.path.isabs(file) else os.path.join(self.root, file)
        why = self.guard.check_path(absolute, self.root)
        if why:
            return _result("refused", f"path refused ({why})", "ask about a file inside the project that is not a credential file")
        return None

    def request(self, op, **args):
        if op not in OPS:
            return _result("bad_request", f"unknown op {op!r}", "use " + ", ".join(OPS))
        args = {k: v for k, v in args.items() if v is not None}
        if op != "stats":
            refused = self._check_file(args)
            if refused:
                return refused
            args["file"] = args["file"] if os.path.isabs(args["file"]) else os.path.join(self.root, args["file"])
        with self._lock:
            crash_retry = stale_retry = True
            while True:
                out = self._ensure() or self._roundtrip(op, args)
                status = out.get("status")
                if status == "crashed" and crash_retry:
                    crash_retry = False
                    continue
                if status == "stale_index" and stale_retry:
                    stale_retry = False
                    continue
                break
        return self._finish(out)

    def _finish(self, out):
        """The answer with sensitive locations withheld and free text screened; fail closed if the screen cannot run."""
        try:
            withheld = [0]
            out = self._clean(out, withheld)
            if withheld[0]:
                out["withheld"] = withheld[0]
            return out
        except Exception as error:  # noqa: BLE001 - nothing unscreened may leave
            return _result("unavailable", f"output screen failed ({type(error).__name__})", "report this; no answer was returned")

    def _hidden(self, path):
        return isinstance(path, str) and bool(self.guard.check_path(os.path.join(self.root, path.split(":", 1)[0]), self.root))

    def _clean(self, value, withheld, key=""):
        """Walk the whole answer: drop rows about hidden paths, withhold hidden path values, and screen every other string."""
        if isinstance(value, list):
            rows = []
            for item in value:
                if isinstance(item, dict) and (self._hidden(item.get("path")) or self._hidden(item.get("from"))):
                    withheld[0] += 1
                    continue
                rows.append(self._clean(item, withheld, key))
            return rows
        if isinstance(value, dict):
            return {k: self._clean(v, withheld, k) for k, v in value.items()}
        if isinstance(value, str):
            if key in PATH_KEYS and self._hidden(value):
                withheld[0] += 1
                return WITHHELD
            return self.guard.screen(value)
        return value

    # ---- the operations --------------------------------------------------------------------------------------------
    def definition(self, file, line=None, column=None, name=None):
        return self.request("definition", file=file, line=line, column=column, name=name)

    def references(self, file, line=None, column=None, name=None, limit=25):
        return self.request("references", file=file, line=line, column=column, name=name, limit=limit)

    def exports(self, file, limit=200):
        return self.request("exports", file=file, limit=limit)

    def imports(self, file, direction="imports", limit=25, depth=1):
        return self.request("imports", file=file, direction=direction, limit=limit, depth=depth)

    def stats(self):
        return self.request("stats")
