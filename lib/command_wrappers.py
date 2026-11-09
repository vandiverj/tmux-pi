"""Programs that run another command, and where that command starts. One table for the two detectors that must see through them:
redact_hook.bash_searches (is this output grep lines?) and sensitive_read._program (is this a read of a credential?).

Every wrapper has its own row in SPECS: its real options, which of them take a value, the operands that stand before the command
(`timeout DURATION`, `flock FILE`, `chroot ROOT`), the options whose value is itself a command line (`su -c`, `script -c`,
`env -S`), and whether the rest of the words is one command line (`ssh host cmd ...`, `sg group cmd`, `watch cmd`). There is no
shared guess about which options take a value: `sudo -n` takes none and `nice -n` takes one, and each row says so.

`unwrap(name, args)` reads one row and answers with one of
  None                 nothing is run (`setsid` alone, `flock FILE`, `sudo -l`, `chrt -p PID`)
  ("words", [...])     the command and its arguments, as words
  ("line", "...")      a command line a shell (or the remote end) will split and run
  UNKNOWN              an option the row does not list: where the command starts cannot be told, so what runs is unknown.
Callers fail closed on UNKNOWN: a search for redact_hook, a read for sensitive_read. `unwrap` never raises.

The table only ever NARROWS. Whatever a row says, a command that starts with a wrapper is also read as a bag of words
(`bag`): every word after the wrapper, and every word of every string inside them, split like the shell does, recursively. A
search or read program anywhere in that bag is looked at by both detectors, so a row that is wrong (an option that takes a value
listed as a flag, a version of the tool with another option) can cost an extra look but can no longer hide a command.
`uncertain` is the same idea for text a wrapper hands to a shell that is not this one (ssh, `su -c`, ...): if it cannot be read
for certain, it is a search and a read; there is no list of safe shapes.
"""
import os
import re
import shlex
from collections import namedtuple

# flags: options without a value.  valued: options that take the next word.  string: options whose value is a command line.
# fills: valued options that stand in for one operand (`runuser -u USER`).  none: options after which nothing is run
# (`chrt -p`).  numeric: valued only when the next word is a number (macOS `script -t SECONDS`; util-linux `-t` takes none).
# operands: words before the command.  rest: the words from the command on are one command line (ssh, sg, watch).
# append: the rest of the words are added to the string's words (`env -S 'sudo -n' rg x`).
# ifnext: options that take the next word when there is one and mean "help" alone (`sudo -h HOST`, `sudo -h`).
# words: the command may be given as words.  digits: `-5` is an option (`nice -5 cmd`).  anylong: every `--name[=value]` is a
# flag (firejail has no option that takes its value as the next word).  stop: words that end the command (`parallel ... :::`).
Spec = namedtuple("Spec", "flags valued string operands fills none numeric rest words digits anylong stop append ifnext")

UNKNOWN = ("unknown", None)


def _spec(flags="", valued="", string="", operands=0, fills="", none="", numeric="", rest=False, words=True, digits=False,
          anylong=False, stop="", append=False, ifnext=""):
    return Spec(frozenset(flags.split()), frozenset(valued.split()), frozenset(string.split()), operands,
                frozenset(fills.split()), frozenset(none.split()), frozenset(numeric.split()), rest, words, digits, anylong,
                tuple(stop.split()), append, frozenset(ifnext.split()))


_HELP = "-h --help -V --version"
_TIMEOUT = _spec("-f --foreground -p --preserve-status -v --verbose", "-k --kill-after -s --signal", operands=1,
                 none="--help --version")

SPECS = {
    "env": _spec("-i -0 -v --ignore-environment --null --debug --list-signal-handling --block-signal --default-signal "
                 "--ignore-signal", "-u --unset -C --chdir -a --argv0 -P", "-S --split-string", none="--help --version",
                append=True),
    "command": _spec("-p", none="-v -V"),
    "builtin": _spec(),
    "exec": _spec("-c -l", "-a"),
    "nice": _spec(valued="-n --adjustment", digits=True, none="--help --version"),
    "ionice": _spec("-t --ignore", "-c --class -n --classdata", none="-p --pid -P --pgid -u --uid " + _HELP),
    "timeout": _TIMEOUT,
    "gtimeout": _TIMEOUT,
    "sudo": _spec("-A -B -b -E -H -i -k -n -P -S -s --askpass --bell --background --preserve-env --set-home --login "
                  "--reset-timestamp --non-interactive --preserve-groups --stdin --shell",
                  "-a -C -c -D -g -p -R -r -T -t -U -u --auth-type --close-from --login-class --chdir --group --prompt "
                  "--chroot --role --type --command-timeout --user --other-user --host",
                  none="--help -V --version -K --remove-timestamp -v --validate -l --list", ifnext="-h"),
    "doas": _spec("-L -n -s", "-C -u"),
    "nohup": _spec(none="--help --version"),
    "stdbuf": _spec(valued="-i -o -e --input --output --error", none="--help --version"),
    "caffeinate": _spec("-d -i -m -s -u", "-t -w"),
    "noglob": _spec(),
    "nocorrect": _spec(),
    "time": _spec("-a -p -v -l -h -q --append --portability --verbose --quiet", "-f -o --format --output", none="-V --version"),
    "setsid": _spec("-c --ctty -f --fork -w --wait", none=_HELP),
    "unbuffer": _spec("-p"),
    "busybox": _spec(none="--list --list-full --install " + _HELP),   # `busybox APPLET args`: the applet is the command
    "firejail": _spec(anylong=True),                                    # its options carry values as --opt=value
    "chrt": _spec("-b --batch -f --fifo -i --idle -o --other -r --rr -d --deadline -R --reset-on-fork -v --verbose -a "
                  "--all-tasks", "-T --sched-runtime -P --sched-period -D --sched-deadline", operands=1,
                  none="-p --pid -m --max " + _HELP),
    "taskset": _spec("-a --all-tasks -c --cpu-list", operands=1, none="-p --pid " + _HELP),
    "sg": _spec(string="-c", operands=1, rest=True),
    "runuser": _spec("-l --login -m -p --preserve-environment -P --pty",
                     "-u --user -g -G -s --group --supp-group --shell -w --whitelist-environment", "-c --command", 1,
                     "-u --user", none=_HELP),
    "chroot": _spec("--skip-chdir", "--userspec --groups -u -g -G", operands=1, none="--help --version"),
    "nsenter": _spec("-m -u -i -n -p -C -U -T -r -w -F -Z -a -W --mount --uts --ipc --net --pid --cgroup --user --time --root "
                     "--wd --no-fork --follow-context --all --wdns --preserve-credentials --env",
                     "-t --target -S --setuid -G --setgid", none=_HELP),
    "unshare": _spec("-i -m -n -p -u -U -C -T -f -r -c --ipc --mount --net --pid --uts --user --cgroup --time --fork "
                     "--map-root-user --map-current-user --kill-child --mount-proc --keep-caps --map-auto",
                     "-S -G -R -w --setuid --setgid --root --wd --propagation --setgroups --map-user --map-group "
                     "--map-users --map-groups --monotonic --boottime --load-interp", none=_HELP),
    "sandbox-exec": _spec(valued="-f -p -n -D"),
    # macOS `arch -arm64 cmd`: the architecture is a one-dash word
    "arch": _spec("-c -32 -64 -arm64 -arm64e -x86_64 -x86_64h -i386 -ppc -ppc64", "-arch -e -d", none="-h"),
    "flock": _spec("-s --shared -x --exclusive -e -u --unlock -n --nonblock -o --close -F --no-fork --verbose",
                   "-w --timeout --wait -E --conflict-exit-code", "-c --command", 1, none=_HELP),
    "script": _spec("-a --append -e --return -f --flush --force -q --quiet -d -k -p -r --timing -F",
                    "-E --echo -I --log-in -O --log-out -B --log-io -T --log-timing -m --logging-format -o --output-limit",
                    "-c --command", 1, numeric="-t", none=_HELP),
    "su": _spec("-f --fast -l --login -m -p --preserve-environment -P --pty",
                "-g -G -s -w --group --supp-group --shell --whitelist-environment", "-c --command", 1, none=_HELP,
                words=False),
    "ssh": _spec("-1 -2 -4 -6 -A -a -C -f -G -g -K -k -M -N -n -q -s -T -t -v -X -x -Y -y",
                 "-B -b -c -D -E -e -F -I -i -J -L -l -m -O -o -p -Q -R -S -W -w", operands=1, rest=True, none="-V"),
    "xargs": _spec("-0 --null -o --open-tty -p --interactive -r --no-run-if-empty -t --verbose -x --exit --show-limits -e -i -l "
                   "--eof --replace --max-lines",
                   "-a --arg-file -d --delimiter -E -I -L -n --max-args -P --max-procs -s --max-chars -J -R -S -B "
                   "--process-slot-var", none="--help --version"),
    # `watch cmd args`: the words are joined and run by `sh -c`
    "watch": _spec("-b --beep -c --color -C --no-color -e --errexit -g --chgexit -p --precise -r --no-rerun -t --no-title "
                   "-w --no-wrap -x --exec -d --differences", "-n --interval -q --equexit", rest=True,
                   none="-h --help -v -V --version"),
    "parallel": _spec("-k --keep-order -m -X -x --pipe --tag --dry-run --dryrun --bar --progress --eta -q --quote -v "
                      "--verbose -t --tty -u --ungroup --lb --line-buffer -r --no-run-if-empty --will-cite --citation "
                      "--shellquote --nonall --onall --group -g --plus --link --xapply --shebang --transfer --cleanup --pipepart",
                      "-j --jobs -n --max-args -N -a --arg-file -I -S --sshlogin -L --max-lines -d --delimiter -E --eof -s "
                      "--max-chars --colsep -C -l --load --results --retries --timeout -P --joblog --header --workdir --wd "
                      "--halt --tagstring --sshloginfile --slf --trc --return --block --recend --recstart --rpl --delay", rest=True,
                      stop="::: :::: :::+ ::::+", none="--help --version"),
}
NAMES = frozenset(SPECS)
# Judged by a branch of their own in sensitive_read._command_hit (xargs feeds files to a reader; ssh's command runs elsewhere).
# Everything else in NAMES is replaced by the command it runs in sensitive_read._program.
OWN = frozenset({"xargs", "ssh"})


def _number(word):
    try:
        float(word)
        return True
    except (TypeError, ValueError):
        return False


def unwrap(name, args):
    """Where the command of `name args...` starts: ("words", [command, ...]), ("line", "a command line"), None or UNKNOWN.

    "line" is the value of a command-string option (`su -c 'cmd'`, `su --command=cmd`, `su -ccmd`), or the joined words after
    the operands for a wrapper whose remote shell takes one string (ssh, sg, watch). A wrapper given both -c and words runs the
    string. `--` ends the options; the operands after it are still taken, then the command starts. An option the row does not
    list gives UNKNOWN."""
    spec = SPECS[name]
    args = list(args)
    n, i, operands, line, ended = len(args), 0, spec.operands, None, False
    known = spec.flags | spec.valued | spec.string | spec.none | spec.numeric | spec.ifnext
    while i < n:
        arg = args[i]
        if arg == "--" and not ended:  # ends the options, not the operands: `flock -- LOCK cmd`, `ssh -- host cmd`
            ended = True
            i += 1
            continue
        if arg == "-" and not ended:  # `su - user`, `env - cmd`, `sg - group`: a login or empty-environment flag
            i += 1
            continue
        if ended or not arg.startswith("-"):
            if operands <= 0:
                break
            operands -= 1
            i += 1
            continue
        i += 1
        if spec.digits and re.fullmatch(r"-[0-9]+", arg):  # `nice -5 cmd`
            continue
        if arg.startswith("--"):
            flag, equals, attached = arg.partition("=")
            if flag not in known and not spec.anylong:
                return UNKNOWN
            steps = [(flag, attached if equals else None)]
        elif arg in known:
            steps = [(arg, None)]
        else:  # a bundle of short options; a letter that takes a value takes the rest of the bundle, else the next word
            steps = [("-" + c, arg[2 + k:] or None) for k, c in enumerate(arg[1:])]
        for flag, attached in steps:
            if flag in spec.none or (flag in spec.ifnext and not attached and i >= n):
                return None
            if flag in spec.valued or flag in spec.string or flag in spec.ifnext or (
                    flag in spec.numeric and _number(args[i] if i < n else None)):
                text = attached
                if text is None:
                    text = args[i] if i < n else None
                    i += 1
                if flag in spec.string:
                    line = text
                if flag in spec.fills:
                    operands -= 1
                break
            if flag not in known and not (spec.anylong and flag.startswith("--")):
                return UNKNOWN
    if line is not None:
        if spec.append and i < n:  # `env -S 'sudo -n' rg x`: env splits the string and puts the other words after its words
            line += " " + " ".join(shlex.quote(word) for word in args[i:])
        return ("line", line)
    if i >= n:
        return None
    if spec.rest:
        rest = args[i:]
        for k, word in enumerate(rest):
            if word in spec.stop:
                rest = rest[:k]
                break
        return ("line", " ".join(rest)) if rest else None
    return ("words", args[i:]) if spec.words else None


# ---------------------------------------------------------------------------------------------------------------------------
# The floor: read a wrapper's tail as one flat list of words, whatever the table says.
# ---------------------------------------------------------------------------------------------------------------------------
SPLIT_DEPTH = 8  # strings inside strings; deeper is "too deep" and the caller fails closed
SEP = ";"  # stands between the simple commands of a split string
_STRUCTURE = re.compile(r"[\s;&|()<>`'\"\\]")  # a word holding any of these may be a whole command line
# An expansion nobody has resolved: `$X` `${X}` `$(...)` `$1` `$@` `$*` `$#` `$?` `$$` `$!` `$-` and backticks. A `$` at the end of
# a word or before a space is a dollar sign. What runs is chosen later, by someone else.
_EXPANSION = re.compile(r"\$[A-Za-z_{(0-9@*#?$!-]|`")
_SHELL_NAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish"})
# Programs that run the words after them. In text a wrapper hands on, each one makes it uncertain: `find -exec` and `docker exec`
# start something else, whose command is not in this text (or not for certain).
_DISPATCHERS = frozenset({"xargs", "nsenter", "chroot", "parallel", "watch", "eval", "exec", "command", "builtin"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
_CONTAINER = frozenset({"docker", "podman", "nerdctl", "kubectl", "oc"})


def _commands(text):
    """The word lists of the simple commands in `text` (operators and newlines separate them), quotes removed as a shell removes
    them. A text that does not split (an open quote) is split on white space and operators instead: more words, never fewer."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>\n")
        lex.whitespace, lex.commenters, lex.whitespace_split = " \t\r", "", True
        tokens = list(lex)
    except ValueError:
        return [[word.strip("'\"") for word in re.split(r"[\s;&|()<>]+", text) if word.strip("'\"")]]
    lists, current = [], []
    for token in tokens:
        if token and not token.strip(";&|()<>\n"):
            lists.append(current)
            current = []
        else:
            current.append(token)
    return [words for words in lists + [current] if words]


def string_option(spec, word):
    """How the option word `word` of a wrapper with the row `spec` hands on a command line, read from `spec.string` and nothing else
    (the field `unwrap` reads): None when it is not a string option, else (text, next). `text` is the value inside the word, or None;
    `next` says the value is the next word. Every spelling the row allows:
      -S STR        separate                       -SSTR      attached
      -xS STR       last letter of a cluster       -vSSTR     a cluster, the value attached
      --split-string=STR   --split-string STR      and an abbreviation of a long form (--split=STR), which getopt accepts
    A letter that takes a value and is not a string option ends the word (`su -sc` gives `c` to -s): it is not a string. A letter the
    row does not list is passed over, as one more flag: more of the text is looked at, never less."""
    if word.startswith("--"):
        flag, equals, attached = word.partition("=")
        if len(flag) > 2 and any(long.startswith(flag) for long in spec.string if long.startswith("--")):
            return (attached, False) if equals else (None, True)
        return None
    if len(word) > 1 and word.startswith("-"):
        for k, letter in enumerate(word[1:], 2):
            flag = "-" + letter
            if flag in spec.string:
                return (word[k:], False) if word[k:] else (None, True)
            if flag in spec.valued or flag in spec.ifnext:
                return None
    return None


def inside(word):
    """The text an option word carries inside it as a command line (`-Srg`, `-qcSTR`, `--command=STR`), or None. Read from the string
    options of every row (`string_option`): `flatten` does not know which wrapper a word belongs to, so a word is a string when any
    row says it is. That can add a look at the value of another program's option (`sort -S1G`), never hide one."""
    if not word.startswith("-"):
        return None
    for spec in SPECS.values():
        found = string_option(spec, word) if spec.string else None
        if found and found[0]:
            return found[0]
    return None


def _join(parts, depth):
    flat, ends, deep = [], [], False
    for n, part in enumerate(parts):
        if n:
            flat.append(SEP)
            ends.append(len(flat))
        sub, sub_ends, sub_deep = flatten(part, depth)
        flat += sub
        ends += [end + len(flat) - len(sub) for end in sub_ends]
        deep = deep or sub_deep
    return flat, ends, deep


def flatten(words, depth=0):
    """(flat, ends, too_deep): `words` in order, each word that is a string put through the shell's split and the words of it
    placed right after it, and so on to SPLIT_DEPTH (SEP stands between the commands of a split). A word is a string when it
    holds white space, a quote, an operator or a backslash (a quoted `'rg --hidden x'` is one word to the shell that read the outer
    line; the shell that runs it splits it again), or when it is a string option of a wrapper with its value inside the word
    (`-Srg`, `-qcSTR`, `--command=STR`: `inside`). The words after a string stay after its words, so
    `env -S 'rg --hidden' TOKEN .` reads `rg --hidden TOKEN .`.

    `ends[i]` is where the words that follow flat[i] start: i + 1, or the end of the words of flat[i] when it is a string (the
    program `'/tmp/a b/rg'` is the basename of the whole word, and its arguments are the words after it)."""
    flat, ends, deep = [], [], False
    for word in words:
        at = len(flat)
        flat.append(word)
        ends.append(at + 1)
        text = inside(word) or (word if _STRUCTURE.search(word) else None)
        if not text:
            continue
        if depth >= SPLIT_DEPTH:
            deep = True
            continue
        parts = _commands(text)
        if parts == [[word]]:
            continue
        sub, sub_ends, sub_deep = _join(parts, depth + 1)
        flat += sub
        ends += [end + at + 1 for end in sub_ends]
        ends[at] = len(flat)
        deep = deep or sub_deep
    return flat, ends, deep


def bag(tail):
    """(entries, too_deep) for the words after a wrapper: each entry is (name, words after it) for one word of the flat list
    from `flatten`. The name is the word's base name lower-cased; option words (`-x`) are not names."""
    flat, ends, deep = flatten(tail)
    return [(os.path.basename(word).lower(), flat[ends[i]:]) for i, word in enumerate(flat)
            if word and word != SEP and not word.startswith("-")], deep


def starts_with_wrapper(words, assignments, keywords):
    """(wrapper name, its tail) when the command in `words` starts with a wrapper (after variable assignments and keywords)."""
    i = 0
    while i < len(words) and (assignments.match(words[i]) or words[i] in keywords):
        i += 1
    if i < len(words) and os.path.basename(words[i]).lower() in NAMES:
        return os.path.basename(words[i]).lower(), list(words[i + 1:])
    return None


def _uncertain_words(flat):
    """`uncertain` over words already flattened."""
    for word in flat:
        if _EXPANSION.search(word):
            return True
    for k, word in enumerate(flat):
        if _STRUCTURE.search(word) or word == SEP:
            continue  # a string: its own words are in the list too
        name, rest = os.path.basename(word).lower(), flat[k + 1:]
        if name in _SHELL_NAMES and not (len(rest) >= 2 and rest[0] == "-c"):
            return True
        if name in _DISPATCHERS or (name == "find" and _FIND_EXEC & set(rest)) or (
                name in _CONTAINER and {"exec", "run"} & set(rest)):
            return True
    return False


def uncertain(line):
    """Whether a command line a wrapper hands on (ssh's remote command, `su -c`, `flock -c`, `env -S`, ...) cannot be read for
    certain: an expansion that is not resolved (`$X`, `${X}`, `$(...)`, `$1`, `$@`, `$?`, backticks); a shell with no command
    string, or with anything but `-c` before it (`-cx`, `-xc`, `-ec`); a program that runs other words (`find -exec`, `xargs`,
    `docker exec`, `kubectl exec`, `eval`, ...). Every shell and line nested inside it counts. There is no list of safe shapes."""
    flat, _, deep = _join(_commands(line), 0)
    return deep or _uncertain_words(flat)


def handed_text(name, flat, ends, start):
    """The words of `flat` that the wrapper `name`, whose own words start at `start`, hands on as text: everything after it for a
    wrapper that runs the rest of its words as one line (ssh, sg, watch, parallel); for any wrapper with a command-string option
    among its words (`su -c STR`, `flock --command=STR`, `script -qcSTR`, `env -S STR`) that string, and for `env -S` the words
    after it too. Found in the words by the wrapper's name, not through the option table of whatever stands before it.
    Returns a list of word lists."""
    spec = SPECS.get(name)
    if spec is None:
        return []
    if spec.rest:
        return [flat[start:]]
    texts = []
    for j in range(start, len(flat)):
        found = string_option(spec, flat[j]) if flat[j].startswith("-") else None
        if found is None:
            continue
        inline = found[0] is not None
        end = ends[j] if inline else (ends[j + 1] if j + 1 < len(flat) else j + 1)
        texts.append(flat[j:end] if inline else flat[j + 1:end])
        if spec.append:
            texts.append(flat[j:])
    return texts


def floor_uncertain(name, tail):
    """The uncertainty rule with no option table before it: whether the command that starts with the wrapper `name` and has the
    words `tail` hands any text on that cannot be read for certain (`uncertain`). Every wrapper in it, at any depth, is looked at
    by name over all the words after it (`handed_text`). So `sudo -h localhost su -c '$X' nobody` is uncertain whatever sudo's own
    row says of `-h`. A plain command (`sudo docker exec c ls`, `nohup sh -c "$CMD" &`, `su -s /bin/sh nobody -c ls`) holds no
    handed-on text that is uncertain and is not made so."""
    flat, ends, deep = flatten(tail)
    if deep:
        return True
    if any(_uncertain_words(text) for text in handed_text(name, flat, ends, 0)):
        return True
    return any(_uncertain_words(text) for i, word in enumerate(flat) if word and word != SEP and not word.startswith("-")
               for text in handed_text(os.path.basename(word).lower(), flat, ends, ends[i]))
