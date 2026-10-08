"""Deny guard for the trust store (T7, slice 3).

No tool call by a worker or the main agent may touch `~/.config/tmux-pi/trust/` or
`~/.config/tmux-pi/state/trust/`, run `tp-trust grant|revoke`, or (for a worker) type into the
owner's terminal with tmux. The answer is `deny`: the owner is not asked to approve a change of the
trust store that an agent proposes. There is one other answer, `ask`, described below.

Three answers, from `judge` and `check_within` (`ALLOW`, `ASK`, `DENY`; `check` keeps the old shape, a reason or None):
`allow` lets the call go on to the other gates. `deny` ends it. `ask` is given only when the one reason the guard cannot
say is that a budget of its own ran out while looking through a tree or a glob, or while writing out a brace list or range,
that is nowhere near the store (a tree of tens of thousands of files, such as `grep -R foo .` in a large repository, or
`echo {1..100000}`): the walked directory neither holds the store nor lies inside it, and nothing else in the call names it,
reaches it, or is left unjudged. For a brace list the text must also not name the store, the word must hold nothing the
shell decides later (`$`, backticks, `~`, a glob, `..`), and neither the directory in front of the braces nor the working
directory may hold the store or lie inside it. The rest of the call is still judged in full after the budget runs out, so
any later deny wins. Every other budget, and every case where the store is named, resolved to, or held by the walked
directory, stays `deny`. A whole-judgement time limit that arrives while a walk or a glob is under way counts as that walk
running out; one that arrives anywhere else is a `deny`.

Every reason is a `Why`: a string that carries its own `remedy`, what to do instead. The Claude hook and the Pi gate print
`<guard root>: <reason>. What to do instead: <remedy>`. A reason whose cause is the store says that trust records are made
only by the owner, with tp-trust, in a terminal; a reason whose cause is a limit or something the guard could not read says
how to make the call smaller or simpler and does not point at tp-trust. `REASONS` lists them all.

Slices 4 and 5 may not lift a prompt this guard raised. An `ask` is the guard saying "I could not check", and no trusted
record, listing or scan makes the unchecked part of the tree checked. A caller that lifts prompts must look at the answer
(`Answer.kind`, the `ask` key of the Pi gate's output, `permissionDecision` of the Claude hook) and leave an `ask` standing,
whatever it concludes about the classifier's own prompt. The Pi gate marks an `ask` with `"deny": true` as well, so a caller
that has not learned the third answer blocks.

Paths are judged by where they really lead: symlinks are resolved, `..` is followed the way the
kernel follows it, and every existing ancestor is compared by (device, inode) with the protected
directories, so a symlinked parent, a case variant or a Unicode variant cannot hide a target. A
path that does not exist yet is judged by its lower-cased, normalised text as well. The home
directory comes from the password database, never from `$HOME`.

Shell commands are read with the classifier's tokenizer. Every word, redirect target, option value,
here-document body, `eval`/`sh -c` payload and string inside an inline script is a candidate path.
What cannot be resolved fails closed: a command that names the store as a path (`/trust/`, `tmux-pi/` then
a variable or glob, `tmux-pi` next to `.config`) and holds a path word that depends on run-time state is denied.

The working directory is judged before anything else: unplaceable, or inside the store, denies every tool call. Programs
that visit everything below a directory (`find`, `grep -r`, `du`, `rsync -a`, ...) are denied when the directory holds
the store, and, when they follow links (`find -L`, `grep -R`, `cp -RL`, `tar -h`, ...), a bounded look through the tree
for a link that leads to or past the store denies them too; a tree too large to look through is an `ask` when the rule
above holds and a `deny` when it does not. Which words are flags is read after the options that take a value have taken
theirs (`_VALUE_OPTIONS`, `_FIND_VALUES`): in `grep -e -R foo .` the `-R` is the pattern, and nothing follows links.

What the guard does not look at is denied, never passed: its own budgets (values in a tool's parameters, levels of nested
commands, and, unless the `ask` rule above applies, alternatives of a brace list, entries and seconds for a walk or a glob
and seconds for the whole judgement) deny when they run out, and an option is read as a limit on a program only where the
program truly cannot go past it (`find -maxdepth`, and nothing else: `du -d` and `tree -L` limit what is printed, not what
is read).

What later slices may rely on, and what they may not:

Guaranteed. No tool call is allowed when something the call shows resolves into the store: a word, redirect target,
option value, here-document word or tool parameter (at any nesting, under any key the tool is not known to keep file
contents in), after `~`, `$HOME`, braces, globs, `cd`, `..`, links, case and Unicode variants; nor when it names the
store as text; nor when it hands a directory that holds the store to a program that goes below what it is given, or
that the guard does not know to stay out; nor `tp-trust` with anything but a reading subcommand. (`ask` is not an allow: it
is only ever the answer when none of this holds and a budget ran out on a tree far from the store.)

Best effort. Reads reached indirectly (through a link planted in a tree a program walks without being asked to follow
links, a program that walks its working directory on its own, names fed in from a file or a pipe); what a script file
or a program's configuration does; and any path built while the command runs from text that does not name the store.
These are caught where the patterns above see them and not otherwise. The guard is a pattern check, not a sandbox.

Each decision is a single `if` carrying a `check:<name>` marker so a mutation run can switch each one off in turn and
see a test fail.
"""
import collections
import glob
import json
import os
import posixpath
import pwd
import re
import signal
import time
import unicodedata

import sensitive_read as sr
import trust

DENY_ROOT = "tmux-pi trust store guard denied this tool call"
ASK_ROOT = "tmux-pi trust store guard could not check this tool call and needs your approval"
ALLOW, ASK, DENY = "allow", "ask", "deny"
Answer = collections.namedtuple("Answer", "kind reason")


class Why(str):
    """A reason the guard gives, with what to do instead. It is a `str`, so it compares and prints as its text."""

    def __new__(cls, text, remedy):
        self = super().__new__(cls, text)
        self.remedy = remedy
        return self


# Every reason the guard can give is one of these, with its own next step. The step names the trust record (the owner's
# `tp-trust`) only where the cause is the store itself; for a limit of the guard's own it says how to make the call smaller.
_OWNER = "Trust records are made and removed only by the owner, with tp-trust, in a terminal."
_NARROWER = "Give it a narrower directory that does not hold the trust store."
_SPLIT_CALL = "Split the work into several shorter tool calls."
NAMES = Why("names the trust store", "Leave the trust store out of the call. " + _OWNER)
REACHES = Why("reaches the trust store", "Leave the trust store out of the call; check that no path in it leads there. " + _OWNER)
CWD_INSIDE = Why("runs in the trust store", "Change to a directory outside the trust store first. " + _OWNER)
CWD_ROOT_INSIDE = Why("the working directory is inside the trust store", "Run the tool from a directory outside the trust store.")
CWD_UNPLACED = Why("the working directory could not be placed", "Run the tool from an existing directory given by its full path.")
TP_TRUST = Why("tp-trust may only be used to read the trust store from here",
               "Use tp-trust list, show, check or log to read. Only the owner can grant or revoke, in a terminal.")
TRUST_DIR = Why("--trust-dir points a trust command at another directory",
                "Leave out --trust-dir; the trust directory is not yours to change. " + _OWNER)
TMUX_PANE = Why("a worker may not type into, read, or run things in a tmux pane",
                "Report to the main agent with tp-agent instead of driving a tmux pane.")
CDPATH = Why("CDPATH changes where cd goes, so a relative directory cannot be placed",
             "Unset CDPATH, or cd to a full path.")
ENV_SET = Why("sets HOME, XDG_CONFIG_HOME, TMPDIR or CLAUDE_CONFIG_DIR where the trust store can be reached",
              "Run the command without setting those variables.")
RUNTIME_PATH = Why("builds a path at run time in a command that names the trust store",
                   "Write each path out in full, without variables or substitutions, and leave the trust store out. " + _OWNER)
LISTED_THEN_PASSED = Why("lists a directory that holds the trust store and passes the names on to another command",
                         "Name the files you want instead of piping a listing, or list a directory that does not hold the trust store.")
INLINE_RUNTIME = Why("inline code that names the trust store builds its paths at run time",
                     "Leave the trust store out of the code. " + _OWNER)
INLINE_GRANT = Why("inline code runs tp-trust grant or revoke", "Only the owner can grant or revoke, in a terminal.")
INLINE_TMUX = Why("inline code uses a tmux command that types into or reads a pane",
                  "Report to the main agent with tp-agent instead of driving a tmux pane.")
TOO_DEEP = Why("command nesting is too deep to verify", "Split the command into several simpler calls.")
EDITS_ABOVE = Why("would remove, move or copy over a directory that holds the trust store",
                  "Name the files to change one by one; do not give the directory that holds the trust store. " + _OWNER)
CD_PATTERN = Why("cd through a pattern next to the trust store cannot be placed",
                 "cd to the directory by its name, without a pattern.")
UNKNOWN_PROGRAM = Why("hands a directory that holds the trust store to a program not known to stay out of it",
                      "Name the files you want, or hand the program a directory that does not hold the trust store.")
WALK_INSIDE = Why("walks a directory in the trust store", "Walk a directory outside the trust store. " + _OWNER)
WALK_ABOVE = Why("walks a directory tree that holds the trust store", _NARROWER)
WALK_LINK = Why("walks through a link into the trust store",
                "Leave out the option that follows links, or walk a directory with no link to the trust store.")
# Over budget with the store near (the call is denied), and the same limit far from the store (the call is asked about).
WALK_REASON = Why("walks a tree too large to check for links into the trust store",
                  "Give it a narrower directory, or leave out the option that follows links.")
GLOB_REASON = Why("a glob with too many matches to check against the trust store",
                  "Use a narrower glob, or one in a directory that does not hold the trust store.")
GLOBSTAR_CLIMB = Why("a recursive glob that climbs back up with `..` cannot be placed",
                     "Write the glob without `..` after `**`, or use the full directory path.")
BRACE_REASON = Why("a brace list or range with too many values to check against the trust store",
                   "Split it into shorter lists or ranges.")
BRACE_NEAR = Why("a brace list or range with too many values to check, where it may reach the trust store",
                 "Split it into shorter lists or ranges, run from a directory that does not hold the trust store, and write "
                 "the path in front of it out in full.")
SEARCH_ABOVE = Why("searches a directory tree that holds the trust store", _NARROWER)
PARAMS_TYPE = Why("the tool call could not be read", "Send the call again with its parameters as an object.")
COMMAND_TYPE = Why("the shell command could not be read", "Send the command as a string.")
PARAM_BUDGET = Why("the tool call holds more values than can be checked against the trust store",
                   "Send fewer paths or values in one call.")
PARAM_KEY = Why("the tool call holds a parameter name that could not be read", "Use plain text for parameter names.")
PARAM_VALUE = Why("the tool call holds a value that could not be read", "Use text, numbers, lists and objects only.")
ANALYSIS_FAILED = Why("the tool call could not be analysed",
                      "Try the call again; if it keeps failing the guard is broken and the owner should be told.")
ANALYSIS_TIMED = Why("the tool call could not be analysed in time",
                     "Make the call smaller (fewer paths, a narrower directory, a shorter command) and try again.")
_FALLBACK_REMEDY = "Make the call smaller or simpler, or ask the owner."
REASONS = [v for v in list(globals().values()) if isinstance(v, Why)]
READ_ONLY_SUBCOMMANDS = {"list", "show", "check", "log", "help", "--help", "-h"}
# Every tmux command that types into, reads from, or runs something in a pane (or its aliases).
TMUX_VERBS = {
    "send-keys", "send", "send-prefix", "paste-buffer", "pasteb", "load-buffer", "loadb", "set-buffer", "setb",
    "save-buffer", "saveb", "show-buffer", "showb", "run-shell", "run", "pipe-pane", "pipep", "respawn-pane",
    "respawnp", "respawn-window", "respawnw", "capture-pane", "capturep", "source-file", "source", "if-shell",
    "if", "command-prompt", "confirm-before", "confirm", "display-popup", "popup", "display-menu", "menu",
    "new-window", "neww", "split-window", "splitw", "new-session", "new", "wait-for", "wait", "bind-key", "bind",
    "set-hook", "choose-buffer", "pipe", "-C", "-CC"}
TMUX_PROGRAM = "tmux"
# Tool parameters that name a file or directory. File contents (`content`, `new_string`, ...) never do.
PATH_KEYS = {"file_path", "path", "notebook_path", "filepath", "filename", "file", "directory", "dir", "folder",
             "cwd", "workdir", "target", "destination", "dest", "source", "src", "from", "to", "output", "root",
             "paths", "files", "glob"}
GLOB_TOOLS = {"Glob", "glob", "find"}
_PATTERN_IS_PATH = GLOB_TOOLS
# Programs that remove or re-own what they are given: an operand that holds the store is as bad as one inside it.
_REMOVERS = {"rm", "rmdir", "unlink", "shred", "srm", "trash", "truncate", "chmod", "chown", "chgrp", "chflags",
             "xattr", "setfacl", "unzip", "rsync", "ditto"}
_PLACERS = {"cp", "ln", "install", "mv"}
_WRITERS = (_REMOVERS | _PLACERS | sr._INTERPRETERS | sr._SHELLS |
            {"tee", "touch", "mkdir", "dd", "sed", "gsed", "awk", "tar", "eval", "mktemp", "patch"})
_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}
_CODE_PROGRAMS = sr._SHELLS | sr._INTERPRETERS | {"eval", "source", ".", "ssh", "xargs", "su", "script", "watch",
                                                   "parallel", "env", "exec", "nohup", "time"}
_MESSAGE_PROGRAMS = {"git", "gh", "tp-agent"}
_MESSAGE_OPTIONS = sr._TEXT_OPTS | {"-b", "-t", "-F"}
_ENV_NAMES = ("HOME", "XDG_CONFIG_HOME", "TMPDIR", "CLAUDE_CONFIG_DIR")
_ENV_ASSIGN = re.compile(r"^(?:export\s+)?(%s)=(.*)$" % "|".join(_ENV_NAMES), re.S)
# A path that names the store by its last two or three components, wherever it starts.
_TAIL = re.compile(r"(?:^|/)tmux-pi/(?:state/)?trust(?:/|$)")
_SPLIT = re.compile(r"""[\s'"`;|&<>=:,]+""")
_WHITESPACE = re.compile(r"\s")
# An argument of an interpreter or shell that is code, not a file name.
_CODE_MARKS = re.compile(r"""[\s()+;'"{}]""")
# `tmux` and one of the verbs, as words, in code the guard cannot follow.
_INLINE_TMUX = re.compile(r"(?<![\w-])tmux(?![\w-]).*(?<![\w-])(?:%s)(?![\w-])" %
                          "|".join(re.escape(v) for v in sorted(TMUX_VERBS) if not v.startswith("-")), re.S)
# Marks of a path assembled while the script runs; only looked for in code the guard cannot follow.
_DYNAMIC = re.compile(r"\+|%|join|format|f['\"]|chr\(|\\x|\\u|base64|decode|bytes|environ|getenv|expanduser|"
                      r"getcwd|\bhome\b|\$|`|glob|walk|listdir|scandir|replace\(|\[::-1\]|reversed", re.I)
# Alternatives one word may stand for through braces, and values one tool call may hold; past either the call is denied.
_BRACE_CHOICES = 4096
_PARAM_VALUES = 20000
# Seconds one judgement may take in all. Both callers stop waiting later than this, and one of them would let the call go.
_JUDGE_SECONDS = 4.0
# Further seconds, once the limit above has ended a walk or a glob, to finish judging the rest of the call without looking
# through any more trees. Both callers still stop waiting after the two together (the Pi gate at 5, the Claude hook at 10).
_GRACE_SECONDS = 0.4
_RANGE = re.compile(r"\{(?:(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z]))(?:\.\.(-?\d+))?\}")
# `trust` names the store only as a path segment (`$DIR/trust/x`, `trust/x`), never as a bare word (a search pattern,
# a test module) or part of a file name (`lib/trust.py`). `tmux-pi` names it when `state`, a variable or a glob follows
# (a directory called tmux-pi in a project does not), or when `.config` is somewhere in the command.
_TRUST_SEGMENT = re.compile(r"(?:^|[^\w.-])trust/|/trust(?:/|$)")
_STORE_PARENT = re.compile(r"(?:^|/)tmux-pi/(?:state/|[^/]*[$*?\[{`])")
# In inline code: `trust` as a quoted string or path segment (`'trust'`, `/trust/`), not `import trust` or `trust.py`.
_INLINE_NAME = re.compile(r"""['"/]trust['"/]""")
# Entries and seconds one judgement may spend on glob matches and on looking through trees for links into the store.
_WALK_ENTRIES = 100000
_WALK_SECONDS = 2.5
# `-d ACTION` may be `recurse`; the action is not read, so the option alone counts.
_GREP_RECURSE = {"-r", "-R", "--recursive", "--dereference-recursive", "-d", "--directories"}
_COPY_RECURSE = {"-r", "-R", "--recursive", "-a", "--archive"}
# Programs that visit everything below a directory: name -> (flags that make it recurse or None when it always does,
# flags that make it follow links met on the way, whether it follows them unless told not to).
_WALKERS = {
    "find": (None, {"-L", "-follow"}, False),
    "rg": (None, {"-L", "--follow"}, False), "ag": (None, {"-f", "--follow"}, False),
    "ack": (None, {"--follow"}, False), "fd": (None, {"-L", "--follow"}, False),
    "tree": (None, {"-l"}, False), "du": (None, {"-L"}, False), "ditto": (None, set(), False),
    "grep": (_GREP_RECURSE, {"-R", "--dereference-recursive", "-S"}, False),
    "ls": ({"-R", "--recursive"}, {"-L", "--dereference"}, False),
    "cp": (_COPY_RECURSE, {"-L", "--dereference"}, False),
    "rsync": ({"-r", "-a", "--recursive", "--archive"},
              {"-L", "--copy-links", "-k", "--copy-dirlinks", "-K", "--keep-dirlinks"}, False),
    "tar": (None, {"-h", "--dereference", "-L"}, False),
    "zip": ({"-r", "-R"}, {"-y"}, True), "scp": ({"-r"}, set(), True),
    "diff": ({"-r", "--recursive"}, set(), True),
    "chmod": ({"-R"}, {"-L"}, False), "chown": ({"-R"}, {"-L"}, False), "chgrp": ({"-R"}, {"-L"}, False),
    "chflags": ({"-R"}, {"-L"}, False),
}
for _alias in ("egrep", "fgrep", "ggrep", "zgrep"):
    _WALKERS[_alias] = _WALKERS["grep"]
_WALKERS["rgrep"] = (None,) + _WALKERS["grep"][1:]
# Options that take a value, per walker: (letters of short options, names of long options). The word after one, or the rest of
# a cluster of letters after one, is its value and not an option, so `grep -e -R foo .` searches for `-R` and follows nothing.
# A value-taking option is listed only where every common version of the program takes a value for it (an option that is a
# flag in one version and takes a value in another is left out, and so is read as a flag: more asks, never fewer). Long
# options whose value is optional (`--color`, `--context`) are not listed; they take a value only as `--color=never`.
_GREP_VALUES = ("efmABCdD", {"--regexp", "--file", "--max-count", "--after-context", "--before-context", "--directories",
                            "--devices", "--include", "--exclude", "--exclude-dir", "--exclude-from", "--label",
                            "--binary-files"})
_VALUE_OPTIONS = {
    "grep": _GREP_VALUES,
    "rg": ("ABCdefgjmMrtTE", {"--after-context", "--before-context", "--context", "--regexp", "--file", "--glob", "--iglob",
                              "--max-count", "--max-columns", "--replace", "--type", "--type-not", "--type-add",
                              "--type-clear", "--threads", "--encoding", "--max-depth", "--maxdepth", "--max-filesize",
                              "--ignore-file", "--sort", "--sortr", "--colors", "--color", "--pre", "--pre-glob",
                              "--engine", "--context-separator", "--path-separator"}),
    "ag": ("Ggm", {"--file-search-regex", "--max-count", "--depth", "--ignore", "--ignore-dir", "--path-to-ignore",
                   "--workers", "--width", "--pager"}),
    "ack": ("m", {"--max-count", "--ignore-dir", "--noignore-dir", "--ignore-file", "--type", "--output", "--pager"}),
    "fd": ("eEtdSjco", {"--extension", "--exclude", "--type", "--max-depth", "--min-depth", "--exact-depth", "--threads",
                        "--size", "--color", "--owner", "--changed-within", "--changed-before", "--max-results",
                        "--base-directory", "--search-path", "--path-separator", "--ignore-file", "--format"}),
    "tree": ("LPIHTo", {"--filelimit", "--charset", "--sort", "--timefmt"}),
    "du": ("dBIt", {"--max-depth", "--block-size", "--exclude", "--exclude-from", "--threshold"}),
    "ditto": ("", {"--arch"}),
    "ls": ("", {"--ignore", "--hide", "--width", "--tabsize", "--block-size", "--format", "--sort", "--time-style",
                "--indicator-style", "--quoting-style"}),
    "cp": ("tS", {"--target-directory", "--suffix"}),
    "rsync": ("eBTf", {"--exclude", "--include", "--exclude-from", "--include-from", "--filter", "--rsh", "--files-from",
                       "--max-size", "--min-size"}),
    "tar": ("fCTXbI", {"--file", "--directory", "--files-from", "--exclude-from", "--exclude", "--blocking-size",
                       "--format", "--use-compress-program"}),
    "zip": ("bntPsOZ", set()),
    "scp": ("cFiJloPSDX", set()),
    "diff": ("xXIUCLSFWD", {"--exclude", "--exclude-from", "--ignore-matching-lines", "--label", "--starting-file",
                            "--ifdef", "--show-function-line"}),
    "chmod": ("", {"--reference"}), "chown": ("", {"--reference", "--from"}), "chgrp": ("", {"--reference"}),
    "chflags": ("", set()),
}
for _alias in ("egrep", "fgrep", "ggrep", "zgrep", "rgrep"):
    _VALUE_OPTIONS[_alias] = _GREP_VALUES
# The primaries of `find` that take a value (`-fprintf` takes two), and the one that follows links.
_FIND_VALUES = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname",
                "-type", "-xtype", "-perm", "-user", "-group", "-uid", "-gid", "-size", "-newer", "-anewer", "-cnewer",
                "-mtime", "-atime", "-ctime", "-mmin", "-amin", "-cmin", "-maxdepth", "-mindepth", "-fstype", "-links",
                "-inum", "-samefile", "-printf", "-fprint", "-fprint0", "-fls", "-regextype", "-context", "-used"}
_FIND_NEWER = re.compile(r"^-newer[acBmt][acBmt]$")
# Programs known to stay at the directory they are given: they list it, test it, move into it or put one file in it.
# Any other program handed a directory that holds the store is taken to go below it.
_STAYS_PUT = {"cd", "pushd", "popd", "echo", "printf", "test", "[", "[[", "stat", "file", "basename", "dirname",
              "realpath", "readlink", "pwd", "mkdir", "touch", "df", "which", "type", "export", "set", "unset",
              "true", "false", ":", "read", "local", "declare", "typeset", "alias", "cat", "head", "tail", "wc",
              "less", "more", "tee", "mktemp", "tp-trust"} | _PLACERS | _REMOVERS
# Tools that search below a directory. Every one matches at any depth whatever its pattern looks like (Claude `Glob`
# returns `a/b/x.json` for `*.json`), so no pattern is read as a limit.
_TREE_TOOLS = {"Grep", "Glob", "grep", "glob", "find"}
# Parameters of the tools this guard knows that hold file contents, a search expression or a command: never a path.
_CONTENT_KEYS = {"content", "new_string", "old_string", "new_source", "oldText", "newText", "pattern", "command"}
_KNOWN_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Read", "LS", "write", "edit", "read", "ls"} | _TREE_TOOLS
_ROOT_KEYS = {"path", "directory", "dir", "folder", "root", "workdir", "cwd"}


def fold(text):
    return unicodedata.normalize("NFC", text).casefold()


def is_worker(env=None):
    """A worker session: it has a task directory or id, or runs strict. The launch environment, not the call's."""
    env = os.environ if env is None else env
    return bool(env.get("TP_TASK_DIR") or env.get("TP_TASK_ID") or env.get("TP_MODE") == "strict")


class _Deny(Exception):
    """Something the guard would have to look at and cannot: the call is denied with this reason."""


class _TooLong(Exception):
    """A brace list or range stands for more words than the guard writes out."""


class _OutOfTime(BaseException):
    """The whole-judgement time limit. Not an `Exception` and not an `OSError` (`TimeoutError` is one), so no
    `except OSError: continue` in a walk and no catch-all can swallow it and let the judgement run on without a limit."""


class _Run:
    """What one judgement has seen across a command and the commands nested in it."""

    def __init__(self):
        self.unresolved = False
        self.lost = False   # a `cd` went somewhere run-time state decides
        self.env = False   # HOME, XDG_CONFIG_HOME, TMPDIR or CLAUDE_CONFIG_DIR set somewhere that matters
        self.writes = False  # a redirect, or a program that writes
        self.text = []      # every word, lower-cased at the end
        self.inline = []    # code the guard cannot follow
        self.bounded = False  # a walk above the store was let through because it is told where to stop
        self.fed = False    # a program takes its arguments from a pipe (`xargs`, `parallel`)


class Guard:
    def __init__(self, home=None, worker=None, cwd=None):
        self.home = home or trust.home_dir()
        self.worker = is_worker() if worker is None else worker
        config = os.path.join(self.home, ".config", "tmux-pi")
        self.dirs = (os.path.join(config, "trust"), os.path.join(config, "state", "trust"))
        self.keys = tuple(sorted({fold(d) for d in self.dirs} | {fold(os.path.realpath(d)) for d in self.dirs}))
        # The protected directories themselves, and every directory above them.
        self.ids = {i for d in self.dirs for i in self._ids(d, False)}
        self.chain = {i for d in self.dirs for i in self._ids(posixpath.dirname(d))}
        # A cwd that is not an absolute path cannot be placed: relative words are then unresolved.
        self.cwd = cwd if isinstance(cwd, str) and cwd.startswith("/") else (os.getcwd() if cwd is None else None)
        # What one judgement may spend looking through trees for links that lead into the store.
        self.entries = _WALK_ENTRIES
        self.deadline = time.monotonic() + _WALK_SECONDS
        # Why the guard could not look through a tree far from the store; the call is `ask` if nothing denies it.
        self.asks = []
        self.timed_out = False    # the whole-judgement limit has ended a walk or a glob
        self.opaque = False       # a brace list too long to write out stands for itself, as a word whose value is not known

    def _ran_out(self, root, reason, timed=False):
        """A budget ran out in a walk or glob that starts at `root`: the reason to deny, or None after noting an `ask`.

        Only a root that neither holds the store nor lies inside it (and that the guard can place) may be asked about.
        Judging goes on after an ask, so anything else in the call that reaches the store still denies. Each walk after
        the first gets its own budget, so a known link into the store is still found in a later walk.
        """
        if root is None or self.where(root) is not None:                      # check:ask_root
            return reason
        self.asks.append(reason)
        if timed:                                                           # check:ask_timed
            # The limit that ended this walk is spent: nothing else is looked through, and what is left of the call is
            # judged under a short second timer. A second expiry anywhere is a deny.
            self.timed_out = True
            self.entries = -1
            if signal.getsignal(signal.SIGALRM) is _out_of_time:            # check:grace
                signal.setitimer(signal.ITIMER_REAL, _GRACE_SECONDS)
        elif not self.timed_out:                                            # check:ask_reset
            self.entries = _WALK_ENTRIES
            self.deadline = time.monotonic() + _WALK_SECONDS
        return None

    @staticmethod
    def _ids(path, above=True):
        """(device, inode) of the path and, with `above`, of every directory over it up to the root."""
        out = set()
        while True:
            try:
                st = os.stat(path)
                out.add((st.st_dev, st.st_ino))
            except OSError:
                pass
            parent = posixpath.dirname(path)
            if parent == path or not above:
                break
            path = parent
        return out

    # ---- where a path leads -------------------------------------------------------------------

    def where(self, path):
        """'inside' the store, 'above' it (the path is a directory that holds it), or None."""
        real = os.path.realpath(path)
        text = fold(real)
        for key in self.keys:
            if text == key or text.startswith(key + "/"):          # check:text_inside
                return "inside"
        probe = real
        while True:     # every ancestor, up to the root
            try:
                st = os.stat(probe)
                if (st.st_dev, st.st_ino) in self.ids:             # check:inode_inside
                    return "inside"
                if probe is real and (st.st_dev, st.st_ino) in self.chain:   # check:inode_above
                    return "above"
            except OSError:
                pass
            parent = posixpath.dirname(probe)
            if parent == probe:
                break
            probe = parent
        base = text.rstrip("/")
        for key in self.keys:
            if key.startswith(base + "/"):                          # check:text_above
                return "above"
        return None

    def _expand(self, word):
        for prefix in ("${HOME}", "$HOME", "~"):
            if word == prefix or word.startswith(prefix + "/"):
                return self.home + word[len(prefix):]
        match = re.match(r"~([A-Za-z_][\w.-]*)(/.*)?$", word)
        if match:
            try:
                return pwd.getpwnam(match.group(1)).pw_dir + (match.group(2) or "")
            except KeyError:
                return word
        return word

    def _braces(self, word, base):
        """Every word the shell makes of this one: brace lists (`{a,b}`) and ranges (`{1..3}`, `{a..c}`) written out.

        `base` is where a relative word starts (`""` is the working directory). A list or range with more values than are
        written out leaves the word as it is, after `_too_long` has noted an `ask` or raised a deny."""
        out, changed = [word], "{" in word
        while changed:
            grown, changed = [], False
            for item in out:
                match = _RANGE.search(item) or sr._BRACE_LIST.search(item)
                if not match:
                    grown.append(item)
                    continue
                changed = True
                try:
                    choices = _range(match) if match.re is _RANGE else match.group(1).split(",")
                    grown.extend(item[:match.start()] + choice + item[match.end():] for choice in choices)
                    if len(grown) > _BRACE_CHOICES:                          # check:brace_budget
                        raise _TooLong()
                except _TooLong:
                    return self._too_long(word, base)
            out = grown
        return out

    def _too_long(self, word, base):
        """A brace list or range in `word` has more values than the guard writes out: `[word]` after noting an `ask`, or a deny.

        The ask needs all of these, as for a tree that is too large (see `_ran_out`): the text of the word names no part of
        the store (`trust`, `tmux-pi`, wherever it sits in a list); nothing in it depends on run-time state or climbs with
        `..`; and neither the directory the command runs in nor the directory in front of the braces holds the store or
        lies inside it. The word then stands for itself: the call is judged as if it were a word that cannot be resolved."""
        text = fold(word)
        if "trust" in text or "tmux-pi" in text:                            # check:brace_names
            raise _Deny(NAMES)
        rest = _RANGE.sub("", word)
        front = word.split("{", 1)[0]
        place = self._at(base)
        if place is None or re.search(r"[$`\\~*?\[]|\.\.", rest):          # check:brace_opaque
            raise _Deny(BRACE_NEAR)
        directory = self._expand(front)
        directory = directory if directory.startswith("/") else posixpath.join(place, directory)
        directory = directory if directory.endswith("/") else posixpath.dirname(directory)
        if self.where(place) is not None or self.where(directory) is not None:     # check:brace_root
            raise _Deny(BRACE_NEAR)
        self.asks.append(BRACE_REASON)
        self.opaque = True
        return [word]

    def resolve(self, piece, base, shell=True):
        """Absolute paths a word can stand for (globs and braces expanded), or None when run-time state decides."""
        out = []
        for choice in self._braces(piece, base):
            choice = self._expand(choice)
            if base is not None:
                choice = sr._join(base, choice)
            # `{}` is the name `find -exec` or `xargs` fills in while the command runs.
            if "$" in choice or "`" in choice or "{}" in choice or choice.startswith("~") or \
                    (base is None and not choice.startswith("/")):
                return None
            if not choice.startswith("/"):
                if self.cwd is None:
                    return None
                choice = posixpath.join(self.cwd, choice)
            if sr._GLOB.search(choice):
                prefix = re.split(r"[*?\[]", choice, 1)[0]
                out.append(prefix)
                if "**" in choice:
                    self._globstar(prefix, choice[len(prefix):], shell)
                out.extend(self._glob(choice))
            else:
                out.append(choice)
        return out

    def _glob(self, pattern):
        """Every existing path the pattern matches, hidden names too (a shell can be told to match them)."""
        found = []
        # Where the matches start from: the directory in front of the first wildcard. A `..` after it can lead anywhere.
        front = re.split(r"[*?\[]", pattern, 1)[0]
        root = front if front.endswith("/") else posixpath.dirname(front)
        if ".." in pattern.split("/"):                                      # check:glob_dotdot
            root = None
        try:
            for hit in glob.iglob(pattern, include_hidden=True):
                self.entries -= 1
                if self.entries < 0 or time.monotonic() > self.deadline:        # check:glob_budget
                    reason = self._ran_out(root, GLOB_REASON)
                    if reason:
                        raise _Deny(reason)
                    break
                found.append(hit)
        except _OutOfTime:
            if self.timed_out:                                              # check:second_expiry_glob
                raise
            reason = self._ran_out(root, GLOB_REASON, timed=True)
            if reason:
                raise _Deny(reason)
        return found

    def _globstar(self, prefix, rest, shell):
        """`**` goes down any number of levels (zsh, bash with globstar, every glob tool): it is a walk of the tree."""
        root = prefix if prefix.endswith("/") else posixpath.dirname(prefix)
        if ".." in rest.split("/"):                                         # check:globstar_climb
            raise _Deny(GLOBSTAR_CLIMB)
        # zsh follows links for `***`; `**` does not in zsh, nor in bash from 4.3 on.
        reason = self._tree(root or "/", shell and "***" in rest, None, None)
        if reason:                                                          # check:globstar
            raise _Deny(reason)

    # ---- one word -----------------------------------------------------------------------------

    def _piece(self, piece, base, run, prose, shell=True):
        if _TAIL.search(fold(piece).replace("\\", "/")):                  # check:tail
            return NAMES
        paths = self.resolve(piece, base, shell)
        if paths is None:
            if "$" in piece or "`" in piece or "{}" in piece or piece.startswith("~") or base is None:
                run.unresolved = run.unresolved or not prose or "/" in piece
            elif self.cwd is None:
                run.unresolved = True
            return None
        for path in paths:
            if self.where(path) == "inside":                               # check:word_inside
                return REACHES
        return None

    def _word(self, word, base, run, shell=True):
        prose = bool(_WHITESPACE.search(word))
        pieces = [word]
        if "{" in word and not prose:
            # What the braces spell out is what the closing rules must read: `tr{u,u}st/x` names the store too.
            spelled = self._braces(word, base)
            run.text.extend(spelled)
            if any(_TAIL.search(fold(s).replace("\\", "/")) for s in spelled):     # check:brace_tail
                return NAMES
        if prose or any(c in word for c in "=:,"):
            pieces += [p for p in _SPLIT.split(word) if p]
        if "$(" in word or "`" in word:
            pieces += [p for p in re.split(r"[\s'\";|&<>=:,()`]+", word) if p]
        for piece in dict.fromkeys(pieces):
            reason = self._piece(piece, base, run, prose and piece is not word, shell)
            if reason:
                return reason
        return None

    # ---- a shell command ----------------------------------------------------------------------

    def command(self, command, cwd_base="", depth=0, run=None, top=True):
        run = run or _Run()
        reason = self._walk(command, cwd_base, depth, run)
        if reason:
            return reason
        if top:
            return self._closing(run)
        return None

    def _closing(self, run):
        words = [w.lower() for w in run.text]
        text = " ".join(words)
        # A bare `trust` is a search pattern or a module name, unless the shell no longer knows where it is.
        named = (any(_TRUST_SEGMENT.search(w) or _STORE_PARENT.search(w) for w in words) or
                 ("tmux-pi" in text and ".config" in text) or (run.lost and "trust" in words))
        if run.env and run.writes:                                          # check:env
            return ENV_SET
        if run.unresolved and named:                                       # check:unresolved
            return RUNTIME_PATH
        if self.opaque and named:                                          # check:opaque_named
            return RUNTIME_PATH
        # `find DIR -maxdepth 1` stops short of the store; what is then done with the names it prints does not.
        if run.bounded and (run.unresolved or run.fed):                    # check:bounded_feed
            return LISTED_THEN_PASSED
        for code in run.inline:
            low = code.lower()
            if (_INLINE_NAME.search(low) or "tmux-pi" in low) and _DYNAMIC.search(low):   # check:inline_code
                return INLINE_RUNTIME
            if "tp-trust" in low and re.search(r"\b(grant|revoke)\b", low):     # check:inline_grant
                return INLINE_GRANT
            if self.worker and _INLINE_TMUX.search(low):                        # check:inline_tmux
                return INLINE_TMUX
        return None

    def _walk(self, command, base, depth, run):
        if depth > 6:                                                       # check:depth
            return TOO_DEEP
        previous, pushed, scopes = "$OLDPWD", [], []
        for cmd in sr._commands(command):
            if cmd == "(":
                scopes.append((base, previous, list(pushed)))
                continue
            if cmd == ")":
                if scopes:
                    base, previous, pushed = scopes.pop()
                continue
            here = self._at(base)
            if here and self.where(here) == "inside":                       # check:cd_inside
                return CWD_INSIDE
            words = cmd["words"]
            prog, args = sr._program(words)
            where_prog = len(words) - len(args) - 1 if prog else -1
            checked = [w for i, w in enumerate(words) if not (i == where_prog and "/" not in w)]
            run.text.extend(words + cmd["outputs"] + cmd["inputs"] + cmd["strings"])
            run.writes = run.writes or bool(cmd["outputs"])
            for inner in cmd["subs"]:
                reason = self._walk(inner, base, depth + 1, run)
                if reason:
                    return reason
            reason = self._programs(words, args, prog, run)
            if reason:
                return reason
            exempt = self._message_values(prog, words)
            for word in checked + cmd["outputs"] + cmd["inputs"] + cmd["strings"]:
                if id(word) in exempt and _WHITESPACE.search(word):
                    # Prose is not split into words, but `-F "a b/file"` is one path with a space in it.
                    if any(self.where(p) == "inside" for p in self.resolve(word, base) or ()):   # check:message_path
                        return REACHES
                    continue
                reason = self._word(word, base, run)
                if reason:
                    return reason
                if prog in _CODE_PROGRAMS and word in words and _CODE_MARKS.search(word):
                    run.inline.append(word)
                    reason = None if prog in sr._INTERPRETERS else self._walk(word, base, depth + 1, run)
                    if reason:
                        return reason
            for holder, _unquoted in cmd["heredocs"]:
                for body in holder:
                    reason = self._body(body, prog, base, depth, run)
                    if reason:
                        return reason
            reason = self._edits(prog, args, base, run) or self._trees(prog, args, base, run)
            if reason:
                return reason
            if prog == "cd":
                target = next((a for a in args if a == "-" or not a.startswith("-")), "~")
                base, previous = (previous if target == "-" else self._chdir(base, target)), base
            elif prog == "pushd":
                target = next((a for a in args if not a.startswith(("-", "+"))), None)
                pushed.append(base)
                base = self._chdir(base, target) if target else None
            elif prog == "popd":
                base = pushed.pop() if pushed else None
            run.lost = run.lost or base is None
        return None

    def _at(self, base):
        """The absolute directory commands run in after `cd`s; None when run-time state decides it."""
        if base is None or self.cwd is None:
            return None
        return posixpath.join(self.cwd, base) if base else self.cwd

    def _chdir(self, base, target):
        """The directory after `cd target`; None when run-time state decides, which makes relative words unresolved."""
        joined = sr._cd(base, self._expand(target))
        if "$" in joined or "`" in joined or joined.startswith("~") or joined == "$OLDPWD":
            return None
        if sr._GLOB.search(joined) or "{" in joined:
            # The shell goes to the one directory the pattern matches. Any other count leaves the place unknown.
            found, asked = [], len(self.asks)
            for choice in self._braces(joined, ""):
                if not choice.startswith("/"):
                    if self.cwd is None:
                        return None
                    choice = posixpath.join(self.cwd, choice)
                found.extend(self._glob(choice) if sr._GLOB.search(choice) else [choice])
            if len(self.asks) > asked:                                      # check:cd_unplaced_ask
                return None     # the matches are not all known, so where the shell lands is not either
            found = [p for p in dict.fromkeys(found) if os.path.isdir(p)]
            if len(found) == 1:
                return found[0]
            if any(self.where(p) is not None for p in found):               # check:cd_glob
                raise _Deny(CD_PATTERN)
            return None
        return joined

    def _body(self, body, prog, base, depth, run):
        if prog in _MESSAGE_PROGRAMS:
            return None
        for piece in [p for p in _SPLIT.split(body) if p]:
            reason = self._piece(piece, base, run, True)
            if reason:
                return reason
        if prog in _CODE_PROGRAMS or prog is None:
            run.inline.append(body)
            if prog in sr._SHELLS or prog in (None, "source", "."):
                return self._walk(body, base, depth + 1, run)
        return None

    @staticmethod
    def _message_values(prog, words):
        """Words that are the text of a commit, pull request or request message: prose, not a path."""
        if prog not in _MESSAGE_PROGRAMS:
            return set()
        out = set()
        for i, word in enumerate(words):
            if word in _MESSAGE_OPTIONS and i + 1 < len(words):
                out.add(id(words[i + 1]))
            elif word.startswith("--") and "=" in word and word.split("=", 1)[0] in _MESSAGE_OPTIONS:
                out.add(id(word))
        return out

    # ---- named programs -----------------------------------------------------------------------

    def _programs(self, words, args, prog, run):
        # A word is the program when it is the command or comes after a program that runs its arguments.
        runs = prog in _CODE_PROGRAMS
        position = len(words) - len(args) - 1 if prog else -1
        for i, word in enumerate(words):
            name = posixpath.basename(word).lower()
            if word == "--trust-dir" or word.startswith("--trust-dir="):     # check:trust_dir
                return TRUST_DIR
            called = i == position or (runs and i > position)
            rest = words[i + 1:]
            if name.startswith("tp-trust") and called:
                sub = next((a for a in rest if not a.startswith("-")), None)
                if sub not in READ_ONLY_SUBCOMMANDS or any(a in ("grant", "revoke") for a in rest):   # check:tp_trust
                    return TP_TRUST
            if name == TMUX_PROGRAM and called and self.worker:
                if any(a in TMUX_VERBS or "$" in a or "`" in a for a in rest):   # check:tmux
                    return TMUX_PANE
            if re.match(r"^(?:export\s+)?CDPATH=", word):                    # check:cdpath
                return CDPATH
            match = _ENV_ASSIGN.match(word)
            if match and i <= position + (1 if prog == "export" else 0):
                value = self.resolve(match.group(2), "")
                if any(self.where(v) is not None for v in value or ()):
                    run.env = True
        run.writes = run.writes or prog in _WRITERS
        run.fed = run.fed or prog in ("xargs", "parallel")
        return None

    def _edits(self, prog, args, base, run):
        """Programs that remove, re-own, move or copy over a directory that holds the store."""
        if prog not in _REMOVERS and prog not in _PLACERS and prog not in ("tar", "find"):
            return None
        operands = [a for a in args if not a.startswith("-") or a == "-"]
        if prog == "find":
            if not any(a in _FIND_ACTIONS for a in args):
                return None
            operands = _find_parts(args)[1]
        elif prog == "tar" and not (any(re.match(r"^-[A-Za-z]*x|^--(extract|get)$", a) for a in args) or
                                    (args and re.match(r"^[A-Za-z]*x", args[0]))):
            return None
        checked = operands
        if prog in _PLACERS and operands:
            dest, sources = operands[-1], operands[:-1]
            targets = self.resolve(dest, base)
            if targets is None:
                return None
            directory = len(targets) == 1 and os.path.isdir(targets[0]) and "-T" not in args
            checked = list(sources) if prog == "mv" else []
            if not directory:
                checked.append(dest)
            for src in sources if directory else ():
                leaf = posixpath.basename(posixpath.normpath(src))
                checked.append(posixpath.join(dest, leaf or "."))
        for operand in checked:
            for path in self.resolve(operand, base) or ():
                if self.where(path) in ("inside", "above"):                  # check:above
                    return EDITS_ABOVE
        return None

    # ---- trees ----------------------------------------------------------------------------------

    def _trees(self, prog, args, base, run):
        """Programs that visit everything below the directories they are given, and the links they meet on the way."""
        spec = _WALKERS.get(prog)
        if not spec:
            return self._unknown(prog, args, base)
        recurse, follow_flags, follows = spec
        if prog == "find":
            flags, operands = _find_parts(args)
        else:
            flags, operands = _flags(prog, args), [a for a in args if not a.startswith("-") or a == "-"]
        if recurse is not None and not flags & recurse:
            return None
        follow = not flags & follow_flags if follows else bool(flags & follow_flags)
        depth = _depth(prog, args)
        sources = operands
        # A copy writes into what is already at its destination, through any link it finds there.
        landing = []
        if prog in ("cp", "rsync", "ditto") and len(operands) >= 2:              # check:landing
            sources = operands[:-1]
            landing = self._landing(sources, operands[-1], base)
        roots = [p for operand in sources for p in self.resolve(operand, base) or ()]
        # With no directory given the program starts where it is run.
        if prog in _STARTS_HERE and not any(os.path.isdir(p) for p in roots):
            roots.append(self._at(base) or self.cwd or ".")
        for path in roots:
            reason = self._tree(path, follow, depth, run)
            if reason:
                return reason
        for path in landing:
            reason = self._links(path, None)
            if reason:
                return reason
        return None

    def _unknown(self, prog, args, base):
        """A program this guard has no rule for, handed a directory that holds the store: what it does there is not known."""
        if not prog or prog in _STAYS_PUT:
            return None
        for arg in args:
            value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
            if value.startswith("-"):
                continue
            for path in self.resolve(value, base) or ():
                if os.path.isdir(path) and self.where(path) == "above":       # check:unknown_program
                    return UNKNOWN_PROGRAM
        return None

    def _landing(self, sources, dest, base):
        """Existing directories a recursive copy of `sources` into the directory `dest` writes into."""
        targets = self.resolve(dest, base) or ()
        if len(targets) != 1 or not os.path.isdir(targets[0]):
            return []
        out = []
        for src in sources:
            contents = src.endswith(("/", "/."))      # `src/` is copied into `dest`, `src` as `dest/src`
            leaf = posixpath.basename(posixpath.normpath(src))
            out.append(targets[0] if contents else posixpath.join(targets[0], leaf))
        return [p for p in dict.fromkeys(out) if os.path.isdir(p)]

    def _tree(self, path, follow, depth, run):
        """Why a walk from this directory (down to `depth` levels, through links if `follow`) would reach the store."""
        place = self.where(path)
        if place == "inside":
            return WALK_INSIDE
        if place == "above":
            reach = self._distance(path)
            # The walk must stop above the store directory itself: reaching it is already a look at the store.
            if depth is None or reach is None or depth >= reach:            # check:walk_above
                return WALK_ABOVE
            if run is not None:
                run.bounded = True
        if follow and os.path.isdir(path):
            return self._links(path, depth)
        return None

    def _distance(self, path):
        """How many levels below this directory the nearest store directory is; None when the text cannot say."""
        base = fold(os.path.realpath(path)).rstrip("/")
        found = [key[len(base):].count("/") for key in self.keys if key.startswith(base + "/")]
        return min(found) if found else None

    def _links(self, root, depth):
        """Look through a tree, the way a program that follows links would, for a link that leads to or past the store.

        Returns a reason to deny, or None: nothing found, or the tree was too large and `_ran_out` noted an `ask`."""
        try:
            return self._look(root, depth)
        except _OutOfTime:
            if self.timed_out:                                              # check:second_expiry_walk
                raise
            return self._ran_out(root, WALK_REASON, timed=True)

    def _look(self, root, depth):
        seen, stack = set(), [(root, 0)]
        while stack:
            here, level = stack.pop()
            try:
                st = os.stat(here)
                if (st.st_dev, st.st_ino) in seen or (depth is not None and level >= depth):
                    continue
                seen.add((st.st_dev, st.st_ino))
                with os.scandir(here) as entries:
                    for entry in entries:
                        self.entries -= 1
                        if self.entries < 0 or time.monotonic() > self.deadline:      # check:walk_budget
                            return self._ran_out(root, WALK_REASON)
                        if entry.is_symlink():
                            target = os.path.realpath(entry.path)
                            if self.where(target) in ("inside", "above"):             # check:walk_links
                                return WALK_LINK
                            if os.path.isdir(target):
                                stack.append((target, level + 1))
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append((entry.path, level + 1))
            except OSError:
                continue
        return None


# Programs whose first plain operand is a pattern, not a directory, and that search `.` when given no directory.
_STARTS_HERE = {"find", "rg", "ag", "ack", "fd", "tree", "du", "ls", "grep", "egrep", "fgrep", "ggrep", "zgrep", "rgrep"}


def _range(match):
    """What `{1..5}`, `{a..e}` or `{1..9..2}` stands for."""
    first, last, a, z, step = match.groups()
    step = abs(int(step)) if step and int(step) else 1
    low, high = (int(first), int(last)) if first is not None else (ord(a), ord(z))
    count = abs(high - low) // step + 1
    if count > _BRACE_CHOICES:                                               # check:range_budget
        raise _TooLong()
    values = [low + i * step * (1 if high >= low else -1) for i in range(count)]
    if first is None:
        return [chr(v) for v in values]
    width = max(len(first.lstrip("-")), len(last.lstrip("-"))) if first.lstrip("-")[:1] == "0" or last.lstrip("-")[:1] == "0" else 0
    return list(dict.fromkeys(text for v in values for text in (str(v), ("-" if v < 0 else "") + str(abs(v)).zfill(width))))


def _flags(prog, args):
    """Every option an argument list holds, with clustered letters (`-rn`) split and old-style `tar czf` letters added.

    The value of an option that takes one is read first and is not an option: `grep -e -R foo .` has `-e` and no `-R`, and
    `grep -eR foo .` searches for `R`. Which options take a value is `_VALUE_OPTIONS`."""
    shorts, longs = _VALUE_OPTIONS.get(prog, ("", ()))
    out, i = set(), 0
    while i < len(args):
        arg, first = args[i], i == 0
        i += 1
        values = 0      # words after this one that are values of options in it
        if arg == "--":
            break
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            out.update((arg, name))
            values = int(name == arg and name in longs)
        elif arg.startswith("-") and len(arg) > 1:
            out.add(arg)
            for place, letter in enumerate(arg[1:], 1):
                if letter in shorts:      # the rest of the word is its value, or the next word is
                    out.add("-" + letter)
                    values = int(place == len(arg) - 1)
                    break
                if letter.isalpha():
                    out.add("-" + letter)
        elif prog == "tar" and first and arg.isalpha():
            out.update("-" + c for c in arg)
            values = sum(c in shorts for c in arg)     # old style: each such letter takes a word, in the order of the letters
        if values:                                                          # check:option_values
            i += values
    return out


def _find_parts(args):
    """`find`'s leading options (as flags), its directories, and `-follow` where it is a primary (not the value of one)."""
    flags, i = set(), 0
    operands = []
    while i < len(args) and args[i] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x", "-f", "-D"):
        if args[i] in ("-f", "-D"):    # BSD: the directory follows; GNU: debug options follow
            operands.extend(args[i + 1:i + 2] if args[i] == "-f" else ())
            i += 2
            continue
        flags.add(args[i])
        i += 1
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", "!", ","):
        operands.append(args[i])
        i += 1
    while i < len(args):
        arg = args[i]
        i += 1
        values = 2 if arg == "-fprintf" else int(arg in _FIND_VALUES or bool(_FIND_NEWER.match(arg)))
        if values:                                                          # check:find_values
            i += values
        elif arg == "-follow":
            flags.add("-follow")
    return flags, operands


def _depth(prog, args):
    """How many levels below its directories the program can go at most; None when nothing holds it to a number.

    Only `find -maxdepth N` is read: find does not descend past it. `du -d`, `du --max-depth` and `tree -L` limit what
    is printed and still read the whole tree. `rg`, `fd` and `ag` do stop, but take the last of several depth options,
    accept them inside a cluster of letters, and count levels differently, so the number that holds is not read here.
    """
    if prog != "find":                                                       # check:depth_find_only
        return None
    # A program find runs (`-exec`) is not held to find's depth; `_edits` denies those above the store whatever the depth.
    found = []
    for i, arg in enumerate(args):
        if arg == "-maxdepth":
            # The last one given wins, so every one must be a plain number, and none may be another option's value.
            value = args[i + 1] if i + 1 < len(args) else ""
            if not value.isdigit() or (i > 0 and args[i - 1].startswith("-")):   # check:depth_unread
                return None
            found.append(int(value))
    return max(found) if found else None                                     # check:depth_largest


def _path_shaped(text):
    return bool(text) and not _WHITESPACE.search(text) and ("/" in text or text.startswith("~"))


def _paths(tool, params):
    """Strings in a tool call that name a file or directory, at any nesting.

    Under a key that names a path every string counts. Under a key a known tool keeps file contents, a search
    expression or its command in, none does. Anywhere else (any key of a tool this guard does not know, an unexpected
    key of one it does, the keys of a nested object) a string that looks like one path or a `file:` address counts.
    A value that is not text, a number, a list or an object, and a call with more values than the guard looks through,
    are not looked at and so denied.
    """
    known = tool in _KNOWN_TOOLS or tool in sr._SHELL_TOOLS
    out, stack, seen = [], [(None, params)], 0
    while stack:
        key, value = stack.pop()
        seen += 1
        if seen > _PARAM_VALUES:                                             # check:param_budget
            raise _Deny(PARAM_BUDGET)
        if isinstance(value, dict):
            for name, inner in value.items():
                if not isinstance(name, str):                                # check:param_key_type
                    raise _Deny(PARAM_KEY)
                if key is not None and _path_shaped(name.strip()):           # check:param_key_path
                    out.append(name.strip())
                # A path key covers everything under it; otherwise the nearest name decides.
                stack.append((key if key in PATH_KEYS else name, inner))
        elif isinstance(value, (list, tuple)):
            stack.extend((key, inner) for inner in value)
        elif isinstance(value, str):
            text = value.strip()
            if key in PATH_KEYS or (key == "pattern" and tool in _PATTERN_IS_PATH):
                if text:
                    out.append(text)
            elif not (known and key in _CONTENT_KEYS):                       # check:unknown_values
                if text.startswith("file://"):
                    text = text[len("file://"):]
                if _path_shaped(text):
                    out.append(text)
        elif value is not None and not isinstance(value, (bool, int, float)):   # check:param_type
            raise _Deny(PARAM_VALUE)
    return out


def _search_roots(guard, tool, params):
    """Where a search tool starts: the directory it is given, the fixed front of its glob, or the working directory.

    These tools run `rg` or `fd` without their follow option (Claude Grep and Glob were tried; Pi's grep and find were
    read), so links are not followed and only a root that holds the store counts. The pattern is no limit: `*.json`
    matches at every depth.
    """
    roots = [v.strip() for k, v in params.items() if k in _ROOT_KEYS and isinstance(v, str) and v.strip()]
    pattern = params.get("pattern") if tool in _PATTERN_IS_PATH else None
    if isinstance(pattern, str) and pattern.strip():
        pattern = pattern.strip()
        front = re.split(r"[*?\[{]", pattern, 1)[0]
        if pattern.startswith(("/", "~", "$")) and "/" in front:       # a glob that names its own directory
            roots = [front[:front.rindex("/")] or "/"]
    for root in roots or [guard.cwd]:
        for path in guard.resolve(root, "") or ():
            if guard.where(path) == "above":                              # check:tool_walk_above
                return SEARCH_ABOVE
    return None


def judge(tool, params, cwd=None, home=None, worker=None):
    """The three-way answer: `Answer(ALLOW, None)`, `Answer(ASK, reason)` or `Answer(DENY, reason)`.

    `ASK` only when nothing denies the call and a budget ran out on a tree far from the store (see the module docstring).
    Anything else that cannot be judged is a `DENY`."""
    held = []
    reason = _decide(tool, params, cwd, home, worker, held)
    if reason:
        return Answer(DENY, reason)
    if held and held[0].asks:                                               # check:ask_answer
        return Answer(ASK, held[0].asks[0])
    return Answer(ALLOW, None)


def check(tool, params, cwd=None, home=None, worker=None):
    """None when the call may go on; otherwise a short reason. An `ask` gives its reason too: do not go on without the
    owner. Use `judge` to tell a deny from an ask."""
    return judge(tool, params, cwd, home, worker).reason


def _decide(tool, params, cwd, home, worker, held):
    """A reason to deny, or None. The guard is left in `held` so the caller can read the asks it noted."""
    try:
        guard = Guard(home=home, worker=worker, cwd=cwd)
        held.append(guard)
        if not isinstance(params, dict):                                   # check:params_type
            return PARAMS_TYPE
        # Every tool acts on the working directory by default, so where it is matters before anything else does.
        if guard.cwd is None:                                               # check:cwd_unplaced
            return CWD_UNPLACED
        if guard.where(guard.cwd) == "inside":                              # check:cwd_inside
            return CWD_ROOT_INSIDE
        if tool in sr._SHELL_TOOLS:
            command = params.get("command")
            if not isinstance(command, str):                                # check:command_type
                return COMMAND_TYPE
            if tool.lower() == "powershell":
                command = command.replace("\\", "/")
            reason = guard.command(command)
            if reason:
                return reason
        run = _Run()
        for value in _paths(tool, params):
            run.text.append(value)
            reason = guard._word(value.strip(), "", run, shell=False)
            if reason:
                return reason
        if tool in _TREE_TOOLS:
            reason = _search_roots(guard, tool, params)
            if reason:
                return reason
        return guard._closing(run)
    except _Deny as stop:
        return stop.args[0]
    except Exception:   # a guard that cannot judge the call must not wave it through
        return ANALYSIS_FAILED


def _out_of_time(_signum, _frame):
    raise _OutOfTime("the trust guard ran out of time")


def check_within(seconds, tool, params, cwd=None, home=None, worker=None):
    """`judge`, denied when it is not done in `seconds` (an `Answer`, not a reason). A caller that stops waiting must
    never mean the call goes on. The limit arriving in the middle of a walk or a glob far from the store ends that walk
    like a spent budget (an `ask`); arriving anywhere else, or a second time, it is a deny."""
    previous = signal.signal(signal.SIGALRM, _out_of_time)
    signal.setitimer(signal.ITIMER_REAL, seconds)                           # check:judge_time
    try:
        return judge(tool, params, cwd, home, worker)
    except _OutOfTime:    # not inside a walk or a glob, or the second expiry
        return Answer(DENY, ANALYSIS_TIMED)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def log_denial(reason, tool, harness, home=None, env=None, kind=DENY):
    """One line per denial (or ask) in the owner's trust state directory. Best effort; never changes the decision."""
    env = os.environ if env is None else env
    try:
        home = home or trust.home_dir()
        # Where the log goes only; no decision reads this. Tests that run the real hook point it at a temp directory.
        directory = env.get("TP_TRUST_GUARD_LOG_DIR") or os.path.join(home, ".config", "tmux-pi", "state", "trust")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = os.path.join(directory, "guard.jsonl")
        if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".1")
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": "trust_guard_" + kind,
                  "harness": harness, "task": env.get("TP_TASK_ID") or None, "tool": str(tool)[:40],
                  "worker": is_worker(env), "reason": reason}
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (json.dumps(record) + "\n").encode())
        finally:
            os.close(fd)
    except (OSError, ValueError):
        pass


def message(answer):
    """What a refused call is told: the guard, the reason, and that reason's own next step."""
    root = ASK_ROOT if answer.kind == ASK else DENY_ROOT
    remedy = getattr(answer.reason, "remedy", None) or _FALLBACK_REMEDY
    return "%s: %s. What to do instead: %s" % (root, answer.reason, remedy)


def hook_response(answer):
    """The Claude PreToolUse output for a non-allow `Answer`: `deny`, or `ask` with the reason the call could not be checked."""
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": answer.kind,
                                   "permissionDecisionReason": message(answer)}}


def gate_response(answer):
    """The Pi gate's output. `deny` is true for an ask too, so a caller that does not know the third answer blocks."""
    if answer.kind == ALLOW:
        return {"deny": False, "reason": None}
    out = {"deny": True, "reason": message(answer)}
    if answer.kind == ASK:
        out["ask"] = True
    return out
