"""Kill guard: refuse pattern-based process kills in worker shell commands.

Why: on 2026-10-31 a worker ran `pkill -f <text>` to stop its own test run. Other workers' `claude` command lines carry
their brief text, so the pattern matched them and killed four workers, twice. A rule in the brief was not enough.

One parser, two callers (the Claude hook `claude/plugins/tmux-pi/hooks/kill-guard.sh` and the pi extension
`agent/extensions/kill-guard.ts`); both run `python3 lib/kill_guard.py`, so they cannot drift.

Refused, in any segment of the command (`;` `&&` `||` `|` `&` newlines, `( )`, `$( )`, backticks, `<( )`, here-document
bodies that expand, `bash -c '...'`, `sh -c`, `eval`, `find -exec`):
  * `pkill`, `killall`, `killall5`, `skill`, in any form (also behind `sudo`, `env`, `nohup`, a path, quotes, ...);
  * `xargs kill` (and `xargs pkill`...): the PIDs come from a pipe, not from a PID the worker started;
  * `kill` given anything but literal numeric PIDs, `%job` specs, or a plain variable such as `$PID` / `$!`.
    A command substitution (`kill $(pgrep ...)`), a variable assigned from one in the same command, or a loop variable over
    one, is refused. So is a variable `kill` uses in a command that also looks processes up (`pgrep`, `pidof`, `ps`, `lsof`,
    `fuser`, `pstree`): `pgrep -f x | while read p; do kill $p; done`. PID `0` and any negative PID such as `-1` or
    `-- -123` (a whole process group / every process) are refused too.
Allowed: `kill 123`, `kill -TERM 123`, `kill -s TERM 123`, `kill %1`, `kill -0 123`, `kill $PID`, `kill $!`, `kill -l`;
`echo "pkill"`, `grep -n pkill file`, `pgrep -f x` (only the name in command position counts).

A command that cannot be parsed (unbalanced quote, unterminated `$(`, nesting too deep, not text) is refused: fail closed.

Here-documents and here-strings: the body is parsed as commands only when the command that reads it is a shell (bash, sh,
zsh, dash, ksh, ash). That reader is the command word of the segment the `<<` / `<<<` attaches to, after env (also
`env -S 'bash -s'`), exec, nohup, command, sudo -n ... are unwrapped; when it is `cat`, the next pipe stage is the reader.
A shell name elsewhere on the line (`cat <<EOF | grep bash`, `cat <<EOF; echo bash`) is ignored, and the body is data.

Known limits (this guard stops accidents, it is not a sandbox). Not covered: `python -c 'os.kill'`, killing through a
terminal multiplexer, `fuser -k`, a PID taken from a variable set in an earlier tool call, scripts written to a file and
run later, and ambiguous input forms that hide the script from the segment: `echo '...' | bash`, `printf ... | sh`, any
filter between a here-document and a shell other than `cat` (tee, sed, tr, ...), `bash < file`, text built at run time
and fed to `bash <(...)`, a here-document whose `|` ends the line (`cat <<EOF |` with the shell on a later line),
`source` / `.` of a file, and a shell reached through a subshell, a brace group or `xargs` after a here-document:
`cat <<EOF | (bash)`, `{ cat <<EOF; } | bash`, `... | xargs bash`.

Applies to worker sessions only, decided by `trust_guard.is_worker` (TP_TASK_DIR / TP_TASK_ID set, or TP_MODE=strict), the
same test the trust-store guard uses. Main is supervised by the owner and the refusal tells a worker to "ask Main"; a
worker that is told to ask has someone to ask, Main does not. `--all` (tests, or an owner who wants it everywhere) guards
every session.
"""
import json
import os
import re
import shlex
import sys

MAX_DEPTH = 12
MAX_TEXT = 1_000_000

KILLERS = {"pkill", "killall", "killall5", "skill"}
LOOKUPS = {"pgrep", "pidof", "ps", "lsof", "fuser", "pstree"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
# Wrappers that run the command that follows them, with the options that take a value.
WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-r", "-t"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "nohup": set(), "command": set(), "builtin": set(), "exec": {"-a"}, "time": {"-f", "-o"},
    "nice": {"-n"}, "ionice": {"-c", "-n", "-p"}, "setsid": set(), "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"}, "caffeinate": {"-t", "-w"}, "unbuffer": set(),
}
KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done", "esac", "coproc", "function"}
XARGS_VALUES = {"-I", "-n", "-P", "-L", "-s", "-d", "-E", "-a", "-J", "--max-args", "--max-procs", "--delimiter"}
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
PID = re.compile(r"^-?\d+$")
JOB = re.compile(r"^%[A-Za-z0-9_+\-%?]*$")
VAR = re.compile(r"^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*)|[!$])$")
SIGNAL = re.compile(r"^-(?:\d+|[A-Za-z][A-Za-z0-9+\-]*)$")

HOW = ("What to do instead: stop only a process you started yourself, by its PID (save `$!` when you background it: "
       "`cmd & PID=$!` ... `kill $PID`), or ask Main with tp-agent to stop it. Do not look a process up by name or "
       "command-line text; other workers' command lines contain their briefs, and a pattern matches and kills them.")


class Unparseable(Exception):
    """The command cannot be read; the caller refuses it."""


class Word:
    __slots__ = ("text", "subst", "dynamic")

    def __init__(self, text="", subst=False, dynamic=False):
        self.text, self.subst, self.dynamic = text, subst, dynamic   # dynamic: has an expansion we cannot resolve


class Parser:
    """Splits shell text into simple commands (lists of Word). Substitutions are parsed too, their commands placed before
    the command that holds them. Quote removal is applied, so `"pkill"` and `\\pkill` read as pkill."""

    def __init__(self, text):
        self.s = text
        self.n = len(text)

    def parse(self, depth=0):
        cmds, end = self._commands(0, None, depth)
        return cmds

    # -- helpers --------------------------------------------------------------------------------------------------
    def _commands(self, i, closer, depth):
        """Parse commands from i until `closer` (')' ) or end of text. Returns (commands, index after closer)."""
        if depth > MAX_DEPTH:
            raise Unparseable("commands nested too deeply")
        s, n = self.s, self.n
        out, words, pending = [], [], []   # pending: here-documents to read at next newline (dicts, see `<<` below)
        records = []                     # the simple commands of the current line, to find which one reads a here-document
        cmd_docs = []                    # here-documents attached to the command being read
        cur, have = [], False            # current word pieces, whether a word has started
        flags = {"subst": False, "dynamic": False}
        redirect_next = False
        herestring = False
        strings = []                     # here-string texts of the current command

        def end_word():
            nonlocal cur, have, redirect_next, herestring
            if have:
                w = Word("".join(cur), flags["subst"], flags["dynamic"])
                if redirect_next:
                    if herestring:
                        strings.append(w.text)
                    redirect_next = herestring = False
                else:
                    words.append(w)
            cur, have = [], False
            flags["subst"] = flags["dynamic"] = False

        def end_cmd():
            nonlocal words, cmd_docs
            end_word()
            rec = None
            if words:
                out.append(words)
                rec = {"words": words, "piped": False, "strings": list(strings)}
                records.append(rec)
                for d in cmd_docs:
                    d["rec"] = rec
            strings.clear()
            cmd_docs = []
            words = []
            return rec

        def flush():
            # here-strings go to the command they attach to; they are a script only when a shell reads them
            for k, rec in enumerate(records):
                if rec["strings"] and _reads_shell(records, k):
                    for text in rec["strings"]:
                        out.extend(Parser(text).parse_nested(depth + 1))
            records.clear()

        def add_inner(cmds):
            # commands inside a substitution run before the word's command
            out.extend(cmds)

        while i < n:
            c = s[i]
            if c == "\n":
                end_cmd()
                i += 1
                for d in pending:
                    shell = d["rec"] is not None and _reads_shell(records, records.index(d["rec"]))
                    i = self._heredoc(i, d["delim"], d["expands"], d["strip"], out, depth, shell)
                pending = []
                flush()
                continue
            if c == "\\":
                if i + 1 < n and s[i + 1] == "\n":
                    i += 2
                    continue
                if i + 1 < n:
                    cur.append(s[i + 1])
                    have = True
                    i += 2
                    continue
                raise Unparseable("trailing backslash")
            if c == "'":
                j = s.find("'", i + 1)
                if j < 0:
                    raise Unparseable("unterminated single quote")
                cur.append(s[i + 1:j])
                have = True
                i = j + 1
                continue
            if c == '"':
                i = self._dquote(i + 1, cur, flags, out, depth)
                have = True
                continue
            if c == "`":
                j = self._backtick_end(i + 1)
                inner = self._unescape_backtick(s[i + 1:j])
                add_inner(Parser(inner).parse_nested(depth + 1))
                flags["subst"] = flags["dynamic"] = True
                cur.append(s[i:j + 1])
                have = True
                i = j + 1
                continue
            if c == "$":
                nxt = s[i + 1] if i + 1 < n else ""
                if nxt == "(":
                    cmds, i = self._commands(i + 2, ")", depth + 1)
                    add_inner(cmds)
                    flags["subst"] = flags["dynamic"] = True
                    cur.append("$(...)")
                    have = True
                    continue
                if nxt == "'":
                    j = i + 2
                    while j < n and s[j] != "'":
                        j += 2 if s[j] == "\\" else 1
                    if j >= n:
                        raise Unparseable("unterminated $'...'")
                    body = s[i + 2:j]
                    if "\\" in body:
                        flags["dynamic"] = True
                    cur.append(body)
                    have = True
                    i = j + 1
                    continue
                if nxt == '"':
                    i = self._dquote(i + 2, cur, flags, out, depth)
                    have = True
                    continue
                if nxt == "{":
                    j = s.find("}", i + 2)
                    if j < 0:
                        raise Unparseable("unterminated ${")
                    body = s[i:j + 1]
                    cur.append(body)
                    if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", body):
                        flags["dynamic"] = True
                    have = True
                    i = j + 1
                    continue
                m = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[!$?#@*0-9-])", s[i:])
                if m:
                    cur.append(m.group(0))
                    i += len(m.group(0))
                else:
                    cur.append("$")
                    i += 1
                have = True
                continue
            if c in " \t":
                end_word()
                i += 1
                continue
            if c == "#" and not have:
                j = s.find("\n", i)
                i = n if j < 0 else j
                continue
            if c == ")":
                if closer == ")":
                    end_cmd()
                    flush()
                    return out, i + 1
                end_cmd()          # a stray ) (case pattern, subshell end): a separator
                i += 1
                continue
            if c == "(":
                end_cmd()
                cmds, i = self._commands(i + 1, ")", depth + 1)
                out.extend(cmds)
                continue
            if c in ";|&":
                if c == "&" and i + 1 < n and s[i + 1] == ">":
                    end_word()
                    i += 2 + (1 if s[i + 2:i + 3] == ">" else 0)
                    redirect_next = True
                    continue
                rec = end_cmd()
                if rec and c == "|" and s[i + 1:i + 2] != "|":
                    rec["piped"] = True        # its output is the next command's input
                i += 1
                while i < n and s[i] in ";|&":
                    i += 1
                continue
            if c in "<>":
                if have and "".join(cur).isdigit() and not flags["dynamic"]:
                    cur, have = [], False          # a file descriptor number
                else:
                    end_word()
                if c == "<" and s.startswith("<(", i) or c == ">" and s.startswith(">(", i):
                    cmds, i = self._commands(i + 2, ")", depth + 1)
                    out.extend(cmds)
                    continue
                if s.startswith("<<<", i):
                    i += 3
                    redirect_next = herestring = True
                    continue
                if s.startswith("<<", i):
                    i += 2
                    strip = s.startswith("-", i)
                    if strip:
                        i += 1
                    while i < n and s[i] in " \t":
                        i += 1
                    delim, quoted, i = self._heredoc_word(i)
                    d = {"delim": delim, "expands": not quoted, "strip": strip, "rec": None}
                    pending.append(d)
                    cmd_docs.append(d)
                    continue
                i += 1
                while i < n and s[i] in "<>&|":
                    i += 1
                redirect_next = True
                continue
            cur.append(c)
            have = True
            i += 1
        if closer is not None:
            raise Unparseable("unterminated $(")
        end_cmd()
        flush()
        return out, i

    def parse_nested(self, depth):
        cmds, _ = self._commands(0, None, depth)
        return cmds

    def _dquote(self, i, cur, flags, out, depth):
        s, n = self.s, self.n
        buf = []
        while i < n:
            c = s[i]
            if c == '"':
                cur.append("".join(buf))
                return i + 1
            if c == "\\" and i + 1 < n:
                if s[i + 1] in '"\\$`\n':
                    if s[i + 1] != "\n":
                        buf.append(s[i + 1])
                    i += 2
                    continue
                buf.append(c)
                i += 1
                continue
            if c == "`":
                j = self._backtick_end(i + 1)
                out.extend(Parser(self._unescape_backtick(s[i + 1:j])).parse_nested(depth + 1))
                flags["subst"] = flags["dynamic"] = True
                buf.append(s[i:j + 1])
                i = j + 1
                continue
            if c == "$" and s.startswith("$(", i):
                cmds, i = self._commands(i + 2, ")", depth + 1)
                out.extend(cmds)
                flags["subst"] = flags["dynamic"] = True
                buf.append("$(...)")
                continue
            if c == "$" and s.startswith("${", i):
                j = s.find("}", i + 2)
                if j < 0:
                    raise Unparseable("unterminated ${")
                body = s[i:j + 1]
                buf.append(body)
                if not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", body):
                    flags["dynamic"] = True
                i = j + 1
                continue
            buf.append(c)
            i += 1
        raise Unparseable("unterminated double quote")

    def _backtick_end(self, i):
        s, n = self.s, self.n
        while i < n:
            if s[i] == "\\":
                i += 2
                continue
            if s[i] == "`":
                return i
            i += 1
        raise Unparseable("unterminated backtick")

    @staticmethod
    def _unescape_backtick(text):
        return re.sub(r"\\([\\$`])", r"\1", text)

    def _heredoc_word(self, i):
        """Read a here-document delimiter word at i. Returns (delimiter, quoted, index after it)."""
        s, n = self.s, self.n
        out, quoted = [], False
        while i < n and s[i] not in " \t\n;|&<>()":
            c = s[i]
            if c in "'\"":
                j = s.find(c, i + 1)
                if j < 0:
                    raise Unparseable("unterminated quote in here-document word")
                out.append(s[i + 1:j])
                quoted = True
                i = j + 1
            elif c == "\\" and i + 1 < n:
                out.append(s[i + 1])
                quoted = True
                i += 2
            else:
                out.append(c)
                i += 1
        if not out:
            raise Unparseable("here-document without a delimiter")
        return "".join(out), quoted, i

    def _heredoc(self, i, delim, expands, strip, out, depth, shell=False):
        """Read a here-document body starting at i. A body fed to a shell is a script and is parsed as commands; else it
        is data, except that its `$( )` and backticks run when the delimiter is unquoted."""
        s, n = self.s, self.n
        body = []
        while i < n:
            j = s.find("\n", i)
            line = s[i:] if j < 0 else s[i:j]
            i = n if j < 0 else j + 1
            if (line.lstrip("\t") if strip else line) == delim:
                break
            body.append(line)
        if shell and body:
            out.extend(Parser("\n".join(body)).parse_nested(depth + 1))
        elif expands and body:
            text = "\n".join(body)
            if "$(" in text or "`" in text:
                out.extend(_scan_expansions(text, depth + 1))
        return i


def _reads_shell(records, k):
    """True when the command that reads standard input for records[k] is a shell. That is the command word of the
    segment (after env, env -S, exec, nohup, command, sudo ... are unwrapped); `cat` passes its input on, so the next
    pipe stage is the reader then. A shell name anywhere else on the line does not count."""
    while k < len(records):
        rec = records[k]
        name = _effective(rec["words"], 0)
        if name in SHELLS:
            return True
        if name == "cat" and rec["piped"]:
            k += 1
            continue
        return False
    return False


def _effective(words, depth):
    """Base name of the program a simple command finally runs, or None when it is not clear."""
    if depth > MAX_DEPTH:
        raise Unparseable("commands nested too deeply")
    i = 0
    while i < len(words) and ((ASSIGN.match(words[i].text) and not words[i].text.startswith("$"))
                              or words[i].text in KEYWORDS):
        i += 1
    rest = words[i:]
    if not rest or rest[0].dynamic:
        return None
    name = _base(rest[0].text)
    if name not in WRAPPERS:
        return name
    if name == "command" and any(w.text in ("-v", "-V") for w in rest[1:2]):
        return None
    target = _wrapper_target(name, rest[1:])
    if target is None:
        return None
    if isinstance(target, str):
        cmds = Parser(target).parse_nested(depth + 1)
        return _effective(cmds[-1], depth + 1) if cmds else None
    return _effective(target, depth + 1)


def _wrapper_target(name, args):
    """What a wrapper (sudo, env, nohup, ...) runs: a list of Words, a command-line string (env -S), or None."""
    values = WRAPPERS[name]
    i = 0
    if name == "timeout":
        while i < len(args) and args[i].text.startswith("-"):
            i += 2 if args[i].text in values else 1
        i += 1     # the duration
    else:
        while i < len(args) and (args[i].text.startswith("-") or ASSIGN.match(args[i].text)
                                 or (name == "nice" and PID.match(args[i].text))):
            if name == "env":
                split = _env_split(args, i)
                if split is not None:
                    return split
            i += 2 if args[i].text in values else 1
    return args[i:] if i < len(args) else None


def _env_split(args, i):
    """`env -S STRING` / `--split-string=STRING` runs STRING (then the rest of the arguments) as a command line.
    Returns that text when args[i] is such an option, else None."""
    t = args[i].text
    m = re.match(r"^--split-string(?:=(.*))?$", t, re.S) or re.match(r"^-[A-Za-z]*S(.*)$", t, re.S) if t.startswith("-") else None
    if not m:
        return None
    if t.startswith("--"):
        value, used = (m.group(1), 1) if m.group(1) is not None else (args[i + 1].text if i + 1 < len(args) else "", 2)
    else:
        value, used = (m.group(1), 1) if m.group(1) else (args[i + 1].text if i + 1 < len(args) else "", 2)
    rest = " ".join(shlex.quote(w.text) for w in args[i + used:])
    return value + " " + rest


def _scan_expansions(text, depth):
    """Commands run by `$( )` and backticks inside text that is otherwise data (an unquoted here-document body)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c == "$" and text.startswith("$(", i):
            p = Parser(text)
            cmds, i = p._commands(i + 2, ")", depth + 1)
            out.extend(cmds)
            continue
        if c == "`":
            p = Parser(text)
            j = p._backtick_end(i + 1)
            out.extend(Parser(Parser._unescape_backtick(text[i + 1:j])).parse_nested(depth + 1))
            i = j + 1
            continue
        i += 1
    return out


# -- judging --------------------------------------------------------------------------------------------------------
def _base(name):
    return name.rsplit("/", 1)[-1]


def _refusal(what):
    return f"{what}. {HOW}"


class Judge:
    def __init__(self):
        self.tainted = set()
        self.lookup = False

    def run(self, commands):
        self.lookup = any(self._name(c) in LOOKUPS for c in commands if c)
        for words in commands:
            reason = self.command(words, False, 0)
            if reason:
                return reason
        return None

    def _name(self, words):
        """The program a simple command runs, after assignments, keywords and wrappers; None when not clear."""
        rest = self._strip(words, None)
        return _base(rest[0].text) if rest else None

    def _strip(self, words, record):
        """Drop leading assignments and shell keywords. Records tainted assignments when `record` is a set."""
        i = 0
        while i < len(words):
            w = words[i]
            m = ASSIGN.match(w.text)
            if m and not w.text.startswith("$"):
                if record is not None and (w.subst or self._uses_tainted(w.text)):
                    record.add(m.group(1))
                i += 1
            elif w.text in KEYWORDS:
                i += 1
            else:
                break
        return words[i:]

    def _uses_tainted(self, text):
        return any(re.search(r"\$\{?" + re.escape(v) + r"\b", text) for v in self.tainted)

    def command(self, words, xargs, depth):
        if depth > MAX_DEPTH:
            raise Unparseable("commands nested too deeply")
        rest = self._strip(words, self.tainted)
        if not rest:
            return None
        head = rest[0]
        name = _base(head.text)
        if (head.dynamic or head.text.startswith("$")) and "/" not in head.text:
            return _refusal(f"`{head.text}` runs a command named by an expansion, which cannot be checked for pkill/kill")
        args = rest[1:]
        if name in KEYWORDS:
            return None
        if name in KILLERS:
            return _refusal(f"`{name}` is a pattern-based kill")
        if name == "kill":
            return self._kill(args, xargs)
        if name in ("declare", "local", "export", "readonly", "typeset"):
            for w in args:
                m = ASSIGN.match(w.text)
                if m and (w.subst or self._uses_tainted(w.text)):
                    self.tainted.add(m.group(1))
            return None
        if name == "for" and len(args) >= 3 and args[1].text == "in":
            if any(w.subst or self._uses_tainted(w.text) for w in args[2:]):
                self.tainted.add(args[0].text)
            return None
        if name == "xargs":
            return self._xargs(args, depth)
        if name in SHELLS:
            return self._shell(args, xargs, depth)
        if name == "eval":
            return self._text(" ".join(w.text for w in args), xargs, depth)
        if name == "find":
            return self._find(args, depth)
        if name in WRAPPERS:
            if name == "command" and any(w.text in ("-v", "-V") for w in args[:1]):
                return None
            return self._wrapped(name, args, xargs, depth)
        return None

    def _wrapped(self, name, args, xargs, depth):
        target = _wrapper_target(name, args)
        if target is None:
            return None
        if isinstance(target, str):
            return self._text(target, xargs, depth)
        return self.command(target, xargs, depth + 1)

    def _text(self, text, xargs, depth):
        commands = Parser(text).parse_nested(depth + 1)
        self.lookup = self.lookup or any(self._name(c) in LOOKUPS for c in commands if c)
        for words in commands:
            reason = self.command(words, xargs, depth + 1)
            if reason:
                return reason
        return None

    def _shell(self, args, xargs, depth):
        for i, w in enumerate(args):
            t = w.text
            if t.startswith("-") and not t.startswith("--") and "c" in t[1:] and i + 1 < len(args):
                return self._text(args[i + 1].text, xargs, depth)
            if not t.startswith("-"):
                break
        return None

    def _xargs(self, args, depth):
        i = 0
        while i < len(args) and args[i].text.startswith("-"):
            t = args[i].text
            i += 2 if t in XARGS_VALUES else 1
        if i >= len(args):
            return None
        return self.command(args[i:], True, depth + 1)

    def _find(self, args, depth):
        for i, w in enumerate(args):
            if w.text in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < len(args) and args[j].text not in (";", "+"):
                    j += 1
                if j > i + 1:
                    reason = self.command(args[i + 1:j], True, depth + 1)
                    if reason:
                        return reason
        return None

    def _kill(self, args, xargs):
        if xargs:
            return _refusal("`xargs kill` kills whatever PIDs arrive on a pipe, so they are not PIDs you started")
        i = 0
        while i < len(args):
            t = args[i].text
            if t == "--":
                i += 1
                break
            if t in ("-l", "-L", "-v"):
                return None
            if t in ("-s", "-n"):
                i += 2
            elif SIGNAL.match(t) and not args[i].dynamic:
                i += 1
            if i < len(args) and args[i].text == "--":
                i += 1
            break
        for w in args[i:]:
            t = w.text
            if w.subst:
                return _refusal(f"`kill {t}` takes its PIDs from a command, which can match processes that are not yours")
            if PID.match(t):
                if t == "0" or t.startswith("-"):
                    return _refusal(f"`kill {t}` signals a whole process group or every process, not a PID you started")
                continue
            if JOB.match(t):
                continue
            m = VAR.match(t)
            if m and not w.dynamic:
                var = m.group(1) or m.group(2)
                if var in self.tainted:
                    return _refusal(f"`kill {t}`: ${var} was set from a command's output, which can match processes that are not yours")
                if self.lookup:
                    return _refusal(f"`kill {t}` in a command that also looks processes up (pgrep, ps, ...); its PIDs are not ones you started")
                continue
            return _refusal(f"`kill {t}` is not a literal numeric PID, %job or plain variable")
        return None


def check(command):
    """None when the command may run; otherwise the refusal text, with what to do instead. Fails closed."""
    if not isinstance(command, str):
        return _refusal("the command is not text, so it cannot be checked")
    if len(command) > MAX_TEXT or "\0" in command:
        return _refusal("the command is too large or holds a NUL byte, so it cannot be checked")
    try:
        return Judge().run(Parser(command).parse())
    except Unparseable as e:
        return _refusal(f"the command cannot be parsed ({e}), so it cannot be checked for pkill/kill")
    except RecursionError:
        return _refusal("the command is nested too deeply to check")


def message(reason):
    return f"tmux-pi kill guard denied this command: {reason}"


def applies(env=None, everywhere=False):
    if everywhere:
        return True
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import trust_guard
    return trust_guard.is_worker(env)


def main(argv):
    """--claude-hook: PreToolUse event on stdin, prints a deny decision or nothing; exit 2 when it cannot decide.
    --pi: {tool_name, tool_input} on stdin, prints {"deny": bool, "reason": ...}; exit non-zero when it cannot decide."""
    everywhere = "--all" in argv
    claude = "--claude-hook" in argv
    try:
        if not applies(everywhere=everywhere):
            if not claude:
                print(json.dumps({"deny": False, "reason": None}))
            return 0
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("event is not an object")
    except Exception as e:     # a worker's event we cannot read: fail closed
        print(f"tmux-pi kill guard could not read the tool call: {e}", file=sys.stderr)
        return 2
    if claude and event.get("hook_event_name") not in (None, "PreToolUse"):
        return 0
    tool = event.get("tool_name")
    if not isinstance(tool, str) or tool.lower() != "bash":
        if not claude:
            print(json.dumps({"deny": False, "reason": None}))
        return 0
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    reason = check(command)
    if claude:
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": message(reason)}}))
    else:
        print(json.dumps({"deny": bool(reason), "reason": message(reason) if reason else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
