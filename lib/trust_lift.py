"""The classifier's view of the owner's trust record, and the log of what it lifted (T7).

A lift is a prompt that a tree walk or git's own listing proved empty of credentials inside a trusted tree:
L1 a weak name, L2 a credential-directory guess, L3 a search of the repository root or a hidden search, L4 a
git patch judged by the names-only listing. Each is logged under its rule.

`lib/sensitive_read.py` knows nothing of trust. A caller that prompts the owner builds a `Resolver` and
passes it to `classify`; the classifier asks it two things while it judges one call:

  tree(path)   the trusted tree that holds this path, or None
  note(...)    a prompt that the owner's statement lifted

`tree` answers only when all of these hold, and None otherwise, so that anything missing, unreadable or
doubtful leaves the call to prompt as it always did:

  - the path lies in a repository with a valid record (lib/trust.py, the reader);
  - the trust store guard (lib/trust_guard.py) answers `allow` for this very call. A call the guard would
    deny or ask about is never lifted, however the record reads.

The guard runs lazily, the first time a lift is wanted, so a call that prompts for nothing, and a call
with no record, cost nothing. It runs in this process rather than trusting a sibling hook, because the
two hooks are not ordered.

After the call is judged, `finish(sensitive, plain)` writes one `trust_skip` line when prompts were lifted and
the call no longer prompts (a call that prompts for another reason lifted nothing the owner sees), and one
`trust_ignored` line when a record was refused. Logging is best effort and never changes the decision.
"""
import json
import os
import time

import sensitive_read
import trust
import trust_guard

LOG_NAME = "skipped.jsonl"
LOG_LIMIT = 5 * 1024 * 1024
LISTED = 20
GUARD_SECONDS = 1.5
TARGET_LIMIT = 200


class Tree:
    """A repository the owner vouched for. `top` is its work tree (a linked worktree's own), as a real path."""
    __slots__ = ("top", "root", "kind", "record")

    def __init__(self, top, root, kind, record):
        self.top, self.root, self.kind, self.record = top, root, kind, record


def trust_directory(home=None):
    return os.path.join(home or trust.home_dir(), ".config", "tmux-pi", "trust")


def log_directory(home=None):
    return os.path.join(home or trust.home_dir(), ".config", "tmux-pi", "state", "trust")


class Resolver:
    """One per process. `begin` starts a call, `finish` ends it; between them `tree` and `note` answer the classifier.

    `home`, `directory`, `temp_roots`, `now`, `log_dir` and `guard` exist for tests; by default the home is the
    password database's, the record is read from its trust directory and the guard is `check_within`.
    """

    def __init__(self, harness, home=None, directory=None, temp_roots=None, now=None, log_dir=None, guard=None,
                 env=None, seconds=GUARD_SECONDS):
        self.harness = harness
        self.home = os.path.realpath(home) if home else None
        self.directory = directory or trust_directory(self.home)
        self.log_dir = log_dir or log_directory(self.home)
        self.temp_roots, self.now, self.guard, self.seconds = temp_roots, now, guard, seconds
        self.env = os.environ if env is None else env
        self.begin("", {}, None)

    def begin(self, tool, params, cwd):
        self.call = (tool, params, cwd)
        self.allowed = None
        self.notes = {}
        self.ignored = {}
        self.looked = {}
        self.started = time.monotonic()

    # --- what the classifier asks -----------------------------------------------------------

    def _lookup(self, real, top):
        if top not in self.looked:
            self.looked[top] = trust.lookup(real, self.directory, home=self.home, now=self.now,
                                            temp_roots=self.temp_roots)
        return self.looked[top]

    def _guard_allows(self):
        if self.allowed is None:
            tool, params, cwd = self.call
            check = self.guard or trust_guard.check_within
            try:
                answer = check(self.seconds, tool, params, cwd, home=self.home)
                self.allowed = answer.kind == trust_guard.ALLOW
            except (Exception, trust_guard._OutOfTime):
                self.allowed = False
        return self.allowed

    def tree(self, path):
        """The trusted tree holding `path`, or None. Never raises."""
        try:
            real = os.path.realpath(path)
            top, kind, main = trust.tree_info(real)
            if top is None:
                return None
            result = self._lookup(real, top)
            if not result.trusted:
                if not trust.is_silent(result):
                    self.ignored[top] = (result.reason, main)
                return None
            if not self._guard_allows():
                return None
            return Tree(top, result.root, kind, trust.record_name(result.root)[:8])
        except Exception:
            return None

    def note(self, rule, path, reason):
        """A prompt of kind `rule` at this real path, which the owner's statement lifted; `reason` is what it would have said."""
        self.notes.setdefault((rule, path), str(reason))

    # --- what is written ----------------------------------------------------------------------

    def finish(self, sensitive, plain=None):
        """Write this call's log lines. `sensitive` is the classifier's final answer for the call.

        `plain()` is the reason the classifier gives for the same call without a record (None when it gives
        none). That reason is what each lifted entry says it would have been, so the log can never word it
        differently from the prompt; a call that would not have prompted anyway lifted nothing and logs nothing.
        Without `plain`, the reason the predicate noted is used."""
        try:
            lines = []
            if self.notes and not sensitive:
                would = plain() if plain else None
                if plain is None or would:
                    lines.append(self._skip(would))
            for top, (reason, main) in self.ignored.items():
                lines.append(self._record("trust_ignored", {
                    "record": trust.record_name(main)[:8] if main else None,
                    "root": self._shown(main) if main else None, "reason": reason}))
            if lines:
                self._write(lines)
        except Exception:
            pass
        finally:
            self.notes, self.ignored, self.allowed = {}, {}, None

    def _shown(self, path):
        if not isinstance(path, str) or len(path) > TARGET_LIMIT or not sensitive_read._PATHISH.match(path):
            return "a path"
        return path

    def _record(self, event, fields):
        tool = self.call[0]
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "harness": self.harness,
               "task": self.env.get("TP_TASK_ID") or None, "tool": str(tool)[:40]}
        out.update(fields)
        return json.dumps(out)

    def _skip(self, would=None):
        tops = {}
        listed = []
        for (rule, path), reason in sorted(self.notes.items()):
            tree = self.tree(path)
            if tree is None:        # the path moved out from under the record since it was judged; say nothing of it
                continue
            tops[tree.top] = tree
            rel = os.path.relpath(path, tree.top)
            listed.append({"rule": rule, "target": self._shown(rel), "would_have": would or reason})
        tree = next(iter(tops.values()), None)
        fields = {"record": tree.record if tree else None, "root": self._shown(tree.root) if tree else None,
                  "via": "worktree" if tree and tree.kind == "worktree" else "root",
                  "worktree": self._shown(tree.top) if tree and tree.kind == "worktree" else None,
                  "lifted": listed[:LISTED], "lifted_total": len(listed),
                  "ms": int((time.monotonic() - self.started) * 1000)}
        return self._record("trust_skip", fields)

    def _write(self, lines):
        os.makedirs(self.log_dir, mode=0o700, exist_ok=True)
        path = os.path.join(self.log_dir, LOG_NAME)
        try:
            if os.path.getsize(path) > LOG_LIMIT:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, ("\n".join(lines) + "\n").encode())
        finally:
            os.close(fd)


class Quiet(Resolver):
    """A resolver for re-classifying a call that was already judged: it lifts as the first did but writes nothing."""

    def finish(self, sensitive, plain=None):
        self.notes, self.ignored, self.allowed = {}, {}, None
