"""Per-session placeholder registry for redacted values; never inside a project tree."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets

SESSION_NAME = re.compile(r"[^A-Za-z0-9_.-]")
RECENT = 200  # calls (and run records) kept by recency; see Store.trim_calls for what else is kept and the bound


def default_root():
    return Path(os.getenv("TP_REDACTION_DIR") or Path.home() / ".config/tmux-pi/state/redaction")


class Store:
    """Maps each distinct value to one stable `<REDACTED:kind_n>` placeholder for a session.

    Values that came from a re-readable file are kept as path:line pointers plus a hash;
    only values without a source (command or MCP output) are stored, in a 0600 file.
    """

    def __init__(self, session="default", root=None, persist=True):
        self.persist = persist
        self.path = Path(root or default_root()) / (SESSION_NAME.sub("_", session) + ".json")
        self.data = {"key": secrets.token_hex(32), "index": {}, "counters": {}, "entries": {}}
        self.depth = 0

    @contextlib.contextmanager
    def locked(self):
        """Hold the session lock; nested use (one hook redacting many lines) shares the outer lock."""
        if not self.persist or self.depth:
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.path.is_file():
                self.data = json.loads(self.path.read_text())
            self.depth = 1
            try:
                yield
            finally:
                self.depth = 0
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)
        finally:
            os.close(fd)

    def placeholder(self, value, kind, source=None, line=None):
        """Return the placeholder for value; call inside locked()."""
        digest = hmac.new(bytes.fromhex(self.data["key"]), value.encode(), hashlib.sha256).hexdigest()
        name = self.data["index"].get(digest)
        if not name:
            n = self.data["counters"].get(kind, 0) + 1
            self.data["counters"][kind] = n
            name = "<REDACTED:{}_{}>".format(kind, n)
            self.data["index"][digest] = name
            self.data["entries"][name] = {"kind": kind, "sha256": hashlib.sha256(value.encode()).hexdigest(), "sources": []}
        entry = self.data["entries"][name]
        if source:
            pointer = {"path": str(source), "line": line}
            if pointer not in entry["sources"]:
                entry["sources"].append(pointer)
        elif "value" not in entry:
            entry["value"] = value
        return name

    def entry(self, name):
        with self.locked():
            found = self.data["entries"].get(name)
        return dict(found) if found else None

    def peek(self):
        """Current persisted data without taking the lock or writing; atomic replace keeps reads whole."""
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return self.data

    def record_use(self, call, names, status, command=None, agent=None):
        """Note that tool call `call` uses `names`; call inside locked(). Grants never downgrade.

        `command` is the Bash command the hook wrapped: tp-secret-exec runs only a command recorded here, once.
        `agent` is the subagent (agent_id) that made the call; absent for the main agent. drop_pending reads it."""
        grants, calls = self.data.setdefault("grants", {}), self.data.setdefault("calls", {})
        for name in names:
            if grants.get(name) != "granted":
                grants[name] = status
        calls.pop(call, None)  # a repeat of the id is the newest call
        calls[call] = sorted(names)
        if agent:
            self.data.setdefault("agents", {})[call] = str(agent)
        else:
            self.data.get("agents", {}).pop(call, None)
        if command is not None:
            runs = self.data.setdefault("runs", {})
            runs.pop(call, None)
            runs[call] = command_hash(command)
            for old in list(runs)[:-RECENT]:
                del runs[old]
        self.trim_calls()

    def trim_calls(self):
        """Bound `calls` and `agents` without losing who owns an outstanding call; call inside locked().

        A call is outstanding while it has a recorded run, or uses a name whose grant is still pending: drop_pending and
        settle_use need its names and its agent. Kept: the last RECENT calls, every call with a run (`runs` is itself cut
        to its last RECENT), and the newest RECENT calls that use a pending name. So `calls` and `agents` hold at most
        3 * RECENT entries, `runs` RECENT. `agents` is cut with `calls`, never by its own count: its entry says who owns the
        call's names. A call that falls out (more than RECENT pending-name calls) leaves its pending grants of unknown
        owner, which drop_pending keeps."""
        calls, runs = self.data.get("calls", {}), self.data.get("runs", {})
        if len(calls) > RECENT:
            pending = {n for n, status in self.data.get("grants", {}).items() if status == "pending"}
            order = list(calls)
            waiting = [c for c in order[:-RECENT] if c not in runs and pending.intersection(calls[c])]
            keep = set(order[-RECENT:]) | {c for c in order if c in runs} | set(waiting[-RECENT:])
            for call in [c for c in order if c not in keep]:
                del calls[call]
        agents = self.data.get("agents", {})
        for call in [c for c in agents if c not in calls]:
            del agents[call]

    def claim_run(self, command):
        """tp-secret-exec: consume the hook's record of exactly this command; False if there is none."""
        digest = command_hash(command)
        if digest not in self.peek().get("runs", {}).values():
            return False
        with self.locked():
            runs = self.data.setdefault("runs", {})
            call = next((c for c, h in runs.items() if h == digest), None)
            if call is None:
                return False
            del runs[call]
            return True

    def settle_use(self, call, granted):
        """After the call ran (granted) or was refused: pending grants become granted or are dropped."""
        if call not in self.peek().get("calls", {}):
            return
        with self.locked():
            self.data.get("runs", {}).pop(call, None)  # ran (and was claimed) or refused: either way it is spent
            grants = self.data.setdefault("grants", {})
            for name in self.data.get("calls", {}).get(call, []):
                if grants.get(name) == "pending":
                    if granted:
                        grants[name] = "granted"
                    else:
                        del grants[name]

    def drop_pending(self, agent=""):
        """Drop the pending grants and recorded commands of calls made by `agent` ("" is the main agent). Call only when
        that agent has no tool call awaiting approval (its turn ended, or the user sent a new prompt); this covers a
        rejected ask that fired no PermissionDenied. A background subagent's own live ask is left alone: its calls are
        in `agents`. A call recorded before `agents` existed counts as the main agent's. A pending grant that no call in
        `calls` names has an owner that cannot be told, and is kept (nobody's turn end proves it was rejected): it is
        cleared by the next use of its placeholder (the ask sets it again and the call settles it), or by SessionEnd's
        sweep. It authorizes nothing by itself: tp-secret-exec runs only a command recorded in `runs`."""
        data = self.peek()
        if "pending" not in data.get("grants", {}).values() and not data.get("runs"):
            return
        agent = str(agent or "")
        with self.locked():
            agents, calls, runs = self.data.get("agents", {}), self.data.get("calls", {}), self.data.get("runs", {})
            mine = lambda call: str(agents.get(call) or "") == agent
            other_live = {n for call, names in calls.items() if not mine(call) and call in runs for n in names}
            ours = {n for call, names in calls.items() if mine(call) for n in names}
            for call in [c for c in runs if mine(c)]:
                del runs[call]  # nothing of this agent is in flight, so no command it recorded may run any more
            grants = self.data.setdefault("grants", {})
            for name in [n for n, status in grants.items() if status == "pending" and n in ours and n not in other_live]:
                del grants[name]


def command_hash(command):
    return hashlib.sha256(command.encode("utf-8", "surrogateescape")).hexdigest()


def current_session():
    """Store name for code outside the hooks (the MCP server): the tp-claude launch id, if any."""
    return os.getenv("TP_REDACTION_SESSION") or os.getenv("TP_REDACTION_LAUNCH") or None


def store_holding(placeholder, root=None):
    """The current session's store, else the newest store that knows `placeholder` (Store or None)."""
    session = current_session()
    if session:
        return Store(session, root)
    base = Path(root or default_root())
    files = sorted((p for p in base.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files:
        try:
            if placeholder in json.loads(path.read_text()).get("entries", {}):
                return Store(path.stem, root)
        except (OSError, ValueError):
            continue
    return None


def sweep(root=None, session=None, max_age=7 * 86400):
    """Delete one session's store files, and every store file older than max_age."""
    base, now = Path(root or default_root()), __import__("time").time()
    names = {SESSION_NAME.sub("_", session)} if session else set()
    for path in base.glob("*") if base.is_dir() else []:
        if path.name in {"shadow.log", "shadow.log.1"} or not path.is_file():
            continue
        try:
            if path.stem in names or now - path.stat().st_mtime > max_age:
                path.unlink()
        except OSError:
            pass
