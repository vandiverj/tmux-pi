#!/usr/bin/env python3
"""Where verification repeats: which checks ran more than once on the same content, from the records a session keeps
(VR1 arm A, HB5a).

    python3 tools/checks/repeats.py SESSION_TASKS_DIR [--lane L] [--json]

Reads each task's test-runs/<n>.json (tp-test-run:v1), gate.json and gate-*.json (tp-gate:v1), tests.jsonl (run_bounded,
no tree: counted apart), meta.json (role, lane, lane_step) and review-of. Prints the groups of same-check runs on the same
tree, the runs covered by another run's tests, the retries, the runs never counted (VR1's never-reused list), and per lane
and step the seconds spent on repeats against first runs. The rules (D1-D7) are in docs/plans/det-prep/hub/hb5/hb5a-brief.md
and the HB5a report.

Read-only: it opens files for reading only, writes nothing, never opens a log, skips symlinked records and prints no file
content beyond the recorded cmd, label, tree, exit and seconds. A missing or malformed record is listed under `problems` and
skipped; it never raises. Main's own verification leaves no record in task dirs and is not guessed.
"""
import argparse
import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lib"))
import gate_record  # noqa: E402

TEST_RUN_NAME = re.compile(r"\d+\.json")
GATE_NAME = re.compile(r"gate(-[0-9]{8}-[0-9]{6}-[0-9]+)?\.json")
TASK_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL|AUTH", re.I)
# NAME=VALUE in shell text; VALUE runs to the end of the shell word: quoted parts, backslash escapes and plain characters,
# and an unterminated quote to the end of the text.
ASSIGN_IN_TEXT = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)=((?:\"[^\"]*\"|'[^']*'|\\.|[^\s;&|()<>\"'\\])*"
                            r"(?:\"[^\"]*$|'[^']*$)?)", re.S)
SHELLS = ("bash", "sh", "zsh", "dash", "ksh")
SHELL_C = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
INTERP = re.compile(r"python[0-9.]*")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
ALARM = re.compile(r"\s*alarm\s+(\d+);\s*exec\s+@ARGV;?\s*")
UNITTEST_FLAGS = {"-v", "-q", "-f", "-b", "-c", "--locals", "--buffer", "--failfast", "--catch", "--verbose", "--quiet"}


# ------------------------------------------------------------------ reading (read-only, never raises)

class Reader:
    def __init__(self):
        self.problems = []

    def problem(self, where, what):
        self.problems.append({"where": str(where), "problem": what})

    def _regular(self, path):
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            self.problem(path, "symlink skipped")
            return False
        return stat.S_ISREG(mode)

    def json(self, path, label):
        if not self._regular(path):
            return None
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8", errors="strict"))
        except (OSError, ValueError, UnicodeDecodeError):
            self.problem(path, "malformed " + label)
            return None
        if not isinstance(value, dict):
            self.problem(path, "malformed " + label + " (not an object)")
            return None
        return value

    def text(self, path, limit=200):
        if not self._regular(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                return handle.read(limit)
        except OSError:
            return None

    def mtime(self, path):
        try:
            return os.lstat(path).st_mtime
        except OSError:
            return None

    def jsonl(self, path):
        out, bad = [], 0
        if not self._regular(path):
            return out
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except ValueError:
                        bad += 1
                        continue
                    if isinstance(value, dict):
                        out.append(value)
                    else:
                        bad += 1
        except OSError:
            self.problem(path, "unreadable tests.jsonl")
        if bad:
            self.problem(path, "%d malformed tests.jsonl line(s)" % bad)
        return out


# ------------------------------------------------------------------ canonical command (D1, D3, D4)

def _env_ops(args):
    """(ops, rest) of `env [-u NAME]... [NAME=VALUE]... CMD`; None when a flag other than -u appears (kept opaque)."""
    ops, i = [], 1
    while i < len(args):
        a = args[i]
        if a == "-u" and i + 1 < len(args):
            ops.append(("-u", args[i + 1]))
            i += 2
        elif a.startswith("-"):
            return None
        elif _assign(a):
            ops.append(_assign(a))
            i += 1
        else:
            break
    return ops, args[i:]


def _assign(a):
    """("set", NAME, VALUE) for a NAME=VALUE word, else None. N3: TMUX_TMPDIR is keyed by its presence only."""
    if "=" not in a or not ENV_NAME.fullmatch(a.split("=", 1)[0]):
        return None
    name, value = a.split("=", 1)
    return ("set", name, "<set>" if name == "TMUX_TMPDIR" else value)


def _shell_words(text):
    """The words of a shell string with operators as words of their own (`ls;` is `ls` and `;`); text.split() when the
    quoting is broken."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return text.split()


def _scan_shell(text, depth=0):
    """(words, texts) of a shell program: its words (a nested `sh -c TEXT` is split in turn, so no whole program text is
    ever a word; a redirection target is an output, not an input) and every program text seen."""
    words, texts, out, skip = _shell_words(text), [text], [], set()
    for i, w in enumerate(words):
        if i in skip:
            continue
        if i and words[i - 1] and all(c in "<>&|;()" for c in words[i - 1]) and ">" in words[i - 1]:
            continue
        out.append(w)
        if depth < 4 and os.path.basename(w) in SHELLS and i + 2 < len(words) and SHELL_C.fullmatch(words[i + 1]):
            inner_words, inner_texts = _scan_shell(words[i + 2], depth + 1)
            out += [words[i + 1]] + inner_words
            texts += inner_texts
            skip.update((i + 1, i + 2))
    return out, texts


def _plain_shell(text):
    """The tokens of a `bash -c` string that is one plain command (no operator, redirection or expansion), else None."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        if any(t and all(c in "();<>|&" for c in t) for t in lexer):
            return None
        tokens = shlex.split(text)
    except ValueError:
        return None
    return tokens if tokens and not any("$" in t or "`" in t for t in tokens) else None


def _shell_opaque(args, words):
    """What in a `-c` program the tool does not read, for the reason `not understood`."""
    if len(args) != 3 or args[1] != "-c":
        return "shell options or arguments (%s)" % args[1]
    if "cd" in words:
        return "cd in a shell program"
    for w in words:
        if w and all(c in "();<>|&" for c in w):
            return "shell operator %s" % w
    return "shell expansion in a program"


def _top(arg, cwd):
    """N1: an argument naming the worktree or a path in it, spelt relative to the top."""
    if not cwd or not arg.startswith("/"):
        return arg
    norm = os.path.normpath(arg)
    if norm == cwd:
        return "<top>"
    if norm.startswith(cwd + "/"):
        return "<top>/" + norm[len(cwd) + 1:]
    return arg


def _test_name(arg):
    """N4: a unittest name (dotted) from a module name or a .py path; None if it is neither."""
    name = arg
    if name.endswith(".py") and not name.startswith("/"):
        name = name[:-3].replace("/", ".")
    return name if MODULE.fullmatch(name) else None


def canonical(cmd, cwd):
    """{key, runner, tests, interp, env, tokens, no_interp} of a recorded cmd. key is the D1 identity (without the tree);
    runner is the key without the test list (D3); tests is the sorted test list, or None when the cmd is not a recognised
    test runner; tokens are every token seen (D4); no_interp is the key with the interpreter left out (N2 count only)."""
    cwd = os.path.normpath(cwd) if isinstance(cwd, str) and cwd.startswith("/") else ""
    args = [_top(a, cwd) for a in cmd]
    rel, wrappers, env = ".", [], []
    tokens, shell_texts, opaque = list(args), [], None
    while args:
        base = os.path.basename(args[0])
        if base == "env":
            parsed = _env_ops(args)
            if parsed is None:
                opaque = "an env option the parser does not read"
                break
            env += parsed[0]
            args = parsed[1]
        elif base == "perl" and len(args) >= 3 and args[1] == "-e" and ALARM.fullmatch(args[2]):
            wrappers.append("alarm " + ALARM.fullmatch(args[2]).group(1))
            args = args[3:]
        elif _assign(args[0]):  # a shell prefix assignment: NAME=VALUE CMD
            env.append(_assign(args[0]))
            args = args[1:]
        elif base in SHELLS and len(args) >= 3 and SHELL_C.fullmatch(args[1]):
            text = args[2]
            if text in tokens:
                tokens.remove(text)  # the program text is not a path; its words are the tokens (F3)
            inner = _plain_shell(text) if len(args) == 3 and args[1] == "-c" else None
            words, texts = _scan_shell(text)
            shell_texts += texts
            if inner is None:  # opaque: kept as written; its words still count for D4
                tokens += [_top(w, cwd) for w in words]
                opaque = _shell_opaque(args, words)
                break
            args = [_top(a, cwd) for a in inner]
            tokens += args
        else:
            break
    for tok in [t for t in tokens if re.search(r"\s", t) and t not in shell_texts]:  # text a wrapper passes on (timeout, eval, env -S)
        words, texts = _scan_shell(tok)
        if len(words) > 1:
            tokens += [_top(w, cwd) for w in words]
            shell_texts += texts
    # N1: a path in the worktree, absolute or relative, is spelt relative to the worktree top.
    args = ["." if a == "<top>" else a[len("<top>/"):] if a.startswith("<top>/") else a for a in args]
    interp = ""
    if args and INTERP.fullmatch(os.path.basename(args[0])):
        interp = os.path.normpath(args[0]) if args[0].startswith("/") else args[0]  # N2
        args = [interp] + args[1:]
    tests, opts = _tests(args)
    head = [rel, [list(o) for o in env], wrappers]
    body = list(args) if tests is None else opts + [tests]
    return {"key": json.dumps([head, body]), "runner": json.dumps([head, opts]) if tests is not None else None,
            "tests": tests, "interp": interp, "env": env, "tokens": tokens, "shell_texts": shell_texts, "cwd": cwd,
            "argv": args, "opaque": opaque,
            "no_interp": json.dumps([head, (["<py>"] + body[1:]) if interp else body])}


def _tests(args):
    """(tests, runner options) for `PY -m unittest [flags] NAME...` and `PY tests/run_bounded.py [--timeout S] FILE...`;
    (None, args) for anything else."""
    if len(args) >= 3 and args[1:3] == ["-m", "unittest"] and "discover" not in args:
        opts, names, i = list(args[:3]), [], 3
        while i < len(args):
            a = args[i]
            if a in UNITTEST_FLAGS:
                opts.append(a)
            elif a == "-k" and i + 1 < len(args):
                opts += [a, args[i + 1]]
                i += 1
            else:
                name = _test_name(a)
                if name is None:
                    return None, args
                names.append(name)
            i += 1
        return (sorted(set(names)), opts) if names else (None, args)
    if len(args) >= 3 and args[1] == "tests/run_bounded.py":
        opts, names, i = [args[0], "tests/run_bounded.py"], [], 2
        while i < len(args):
            a = args[i]
            if a in ("--timeout", "--grace", "--durations") and i + 1 < len(args):
                opts += [a, args[i + 1]]
                i += 1
            elif a.startswith("-"):
                return None, args
            else:
                name = _test_name(a)
                if name is None:
                    return None, args
                names.append(name)
            i += 1
        return (sorted(set(names)), opts) if names else (None, args)
    return None, args


NETWORK_CMDS = {"curl", "wget", "gh"}
GIT_NETWORK = {"fetch", "pull", "push", "clone", "ls-remote"}


GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
# Switches that make a run reach LM Studio (F2), and where each is read: TP_LIVE_LMSTUDIO / TP_LIVE_LMSTUDIO_MODEL
# (tests/test_qual_summarize_structured.py LiveLmStudioTests, tests/session_env.py), TP_LMSTUDIO_BUDGET nonzero (lib/router.py
# local_open; session_env sets 20 with TP_LIVE_LMSTUDIO=1), TP_LMSTUDIO_ENDPOINT (lib/router.py ENDPOINT_VAR: an extra LM
# Studio address). TP_LMSTUDIO_BUDGET_FILE is only the counter file and TP_LIVE_FAKE (tests/test_run_bounded.py) a fake.
LIVE_SWITCHES = ("TP_LIVE_LMSTUDIO", "TP_LIVE_LMSTUDIO_MODEL", "TP_LMSTUDIO_ENDPOINT")


def _assignments(canon):
    """Every NAME=VALUE the command can set: parsed env wrappers and prefixes, any assignment word (`env -i X=1 cmd`), and
    any assignment inside shell program text, nested quotes included."""
    found = [(op[1], op[2]) for op in canon["env"] if op[0] == "set"]
    for t in canon["tokens"]:
        op = _assign(t)
        if op:
            found.append((op[1], op[2]))
    for text in canon["shell_texts"]:
        for name, value in ASSIGN_IN_TEXT.findall(text):
            found.append((name, value.strip("\"'")))
    return found


def _git_network(toks):
    """True when a `git` word is followed (after global options, with their values) by a subcommand that reaches the network."""
    for i, t in enumerate(toks):
        if os.path.basename(t) != "git":
            continue
        j = i + 1
        while j < len(toks) and toks[j].startswith("-"):
            j += 2 if toks[j] in GIT_VALUE_OPTS else 1
        if j >= len(toks):
            continue
        if toks[j] in GIT_NETWORK:
            return True
        if toks[j] in ("submodule", "remote"):
            k = j + 1
            while k < len(toks) and toks[k].startswith("-"):
                k += 1
            if k < len(toks) and toks[k] in (("update", "add") if toks[j] == "submodule" else ("update",)):
                return True
    return False


def never_reason(canon, label):
    """D4: why a run is never counted as a duplicate (VR1's never-reused list), or None."""
    words = set(re.findall(r"[a-z0-9_]+", (label or "").lower()))  # `_` joins: test_bench_runner is no bench word
    for name, value in _assignments(canon):
        if name in LIVE_SWITCHES:
            return "live model (%s set)" % name
        if name == "TP_LMSTUDIO_BUDGET" and value not in ("", "0"):
            return "live model (TP_LMSTUDIO_BUDGET set)"
    toks = canon["tokens"]
    # Every word is scanned, whatever wraps it (an unknown wrapper, python -c code, env -S text): a live switch named
    # anywhere is a live run, except where it is unset (`-u NAME`).
    scan = [t for i, t in enumerate(toks) if not (i and toks[i - 1] == "-u")] + [re.sub(r"-u\s+\S+", "", s) for s in canon["shell_texts"]]
    for name in LIVE_SWITCHES:
        if any(name in t for t in scan):
            return "live model (%s named)" % name
    bases = [os.path.basename(t) for t in toks]
    if "tp-lmstudio" in bases or "live" in words:
        return "live model"
    if any(t.startswith(("http://", "https://")) for t in toks) or NETWORK_CMDS & set(bases) or _git_network(toks):
        return "network"
    if "tmux" in bases:
        return "live tmux"
    if "tp-bench" in bases or "bench" in words:
        return "bench"
    if any("tools/smoke/" in t for t in toks) or "smoke" in words:
        return "smoke"
    if any(t.endswith("check_dispatcher_types.sh") for t in toks):
        return "check_dispatcher_types.sh (pi package version not in the key)"
    return None


PY_FLAGS = {"-u", "-B", "-E", "-s", "-S", "-I", "-b", "-q"}  # interpreter options that do not change what a script reads


def outside_reason(canon):
    """D4b and D8: why a run is not comparable at all (class `outside`), or None. The rule is the inverse of a deny-list:
    a run can be a duplicate or covered only when the tool understands all of it. It is understood when (1) every path it
    names is a relative path in the worktree with no `..` component (an absolute path outside is `input outside the tree`;
    shell program text is never a path, its words are), and (2) after the wrappers it reads (env -u/NAME=VALUE, perl alarm,
    a shell prefix assignment, a plain `bash -c` of one command) it is an interpreter (python*) running `-m unittest NAMES`
    or a script in the tree, a shell running a script in the tree, or an executable at a relative path in the tree. Anything
    else (cd, an operator, an unknown env option, an unknown wrapper, python -c, a runner such as node or deno, git, whose
    output reads refs and the index the record does not hold) is `not understood: WHAT`. The never-reused scan runs before
    this, over every word, so a live or network run is `never` even when not understood."""
    for tok in canon["tokens"]:
        if tok.startswith("/") and not tok.startswith("/dev/") and os.path.basename(os.path.dirname(tok)) != "bin":
            return "input outside the tree: " + os.path.basename(tok)
    if canon["opaque"]:
        return "not understood: " + canon["opaque"]
    for tok in canon["tokens"]:
        if any(".." in part.split("/") for part in (tok, tok.split("=", 1)[1] if "=" in tok else "")):
            return "not understood: a .. component in a path"
    args = canon["argv"]
    if not args:
        return "not understood: an empty command"
    head = os.path.basename(args[0])
    i = 1
    if INTERP.fullmatch(head):
        while i < len(args) and args[i] in PY_FLAGS:
            i += 1
        if i >= len(args):
            return "not understood: python without a script"
        if args[i] == "-m":
            return None if canon["tests"] is not None else "not understood: python -m other than unittest with named tests"
    elif head in SHELLS:
        if i >= len(args):
            return "not understood: a shell without a script"
    elif not ("/" in args[0] and not args[0].startswith("/")):
        return "not understood: command %s" % head
    else:
        return None
    a = args[i]
    if a.startswith("-"):
        return "not understood: %s option %s" % (head, a)
    return None if not a.startswith("/") else "not understood: %s script outside the tree" % head


def shown_cmd(cmd):
    """The cmd for printing: the value of a NAME=VALUE with a secret-like name is hidden (D6). A cmd element that is itself
    NAME=VALUE is hidden whole; in shell text the value is hidden through the end of the shell word. Other secret forms
    (the value of a --token flag) are not recognised."""
    def hide(match):
        return match.group(1) + "=<hidden>" if SECRET_NAME.search(match.group(1)) else match.group(0)
    out = []
    for a in cmd:
        name = a.split("=", 1)[0]
        if "=" not in a:
            out.append(a)
        elif ENV_NAME.fullmatch(name) and SECRET_NAME.search(name):
            out.append(name + "=<hidden>")
        else:
            out.append(ASSIGN_IN_TEXT.sub(hide, a))
    return out


# ------------------------------------------------------------------ loading tasks and runs

def _task_info(reader, task_dir):
    meta = reader.json(task_dir / "meta.json", "meta.json") if (task_dir / "meta.json").exists() else None
    review_of = (reader.text(task_dir / "review-of") or "").strip() if (task_dir / "review-of").exists() else ""
    if review_of and not TASK_ID.fullmatch(review_of):
        reader.problem(task_dir / "review-of", "malformed review-of")
        review_of = ""
    meta = meta or {}
    role = "reviewer" if review_of else (meta.get("role") if isinstance(meta.get("role"), str) and meta.get("role") else
                                         ("unknown" if not meta else "worker"))
    lane = meta.get("lane") if isinstance(meta.get("lane"), str) and meta.get("lane") else "(none)"
    step = meta.get("lane_step") if isinstance(meta.get("lane_step"), str) and meta.get("lane_step") else "(none)"
    return {"task": task_dir.name, "role": role, "lane": lane, "step": step, "review_of": review_of}


def _sort_n(path):
    return int(path.stem) if path.stem.isdigit() else 0


def load(tasks_dir, lane=None):
    reader = Reader()
    tasks_dir = Path(tasks_dir)
    runs, jsonl = [], {"lines": 0, "seconds": 0.0, "same_cmd_tree_unknown": 0, "other_shape": 0}
    try:
        entries = sorted(p for p in tasks_dir.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        reader.problem(tasks_dir, "cannot list the tasks dir")
        entries = []
    for task_dir in entries:
        info = _task_info(reader, task_dir)
        if lane is not None and info["lane"] != lane:
            continue
        runs_dir = task_dir / "test-runs"
        if runs_dir.is_dir() and not runs_dir.is_symlink():
            for path in sorted((p for p in runs_dir.iterdir() if TEST_RUN_NAME.fullmatch(p.name)), key=_sort_n):
                record = reader.json(path, "test-run record")
                if record is None:
                    continue
                bad = gate_record.validate(record)
                if bad or record.get("schema") != gate_record.TEST_RUN_SCHEMA:
                    reader.problem(path, "invalid test-run record: " + "; ".join(bad or ["not tp-test-run:v1"]))
                    continue
                canon = canonical(record["cmd"], record["cwd"])
                runs.append(dict(info, kind="test-run", label=record["label"], cmd=record["cmd"], tree=record["tree"],
                                 exit=record["exit"], timed_out=record["timed_out"] or record["exit"] == 124,
                                 seconds=float(record["seconds"]), end=reader.mtime(path), path=str(path), n=_sort_n(path),
                                 canon=canon, never=never_reason(canon, record["label"]), outside=outside_reason(canon)))
        for path in sorted(p for p in task_dir.iterdir() if GATE_NAME.fullmatch(p.name)) if task_dir.is_dir() else []:
            record = reader.json(path, "gate record")
            if record is None:
                continue
            bad = gate_record.validate(record)
            if bad or record.get("schema") != gate_record.GATE_SCHEMA:
                reader.problem(path, "invalid gate record: " + "; ".join(bad or ["not tp-gate:v1"]))
                continue
            for check in record["checks"]:
                if check["status"] not in ("passed", "failed"):
                    continue  # missing / not_applicable: the check did not run
                key, outside = _gate_key(check["name"], record)
                canon = {"key": key, "runner": None, "tests": None, "interp": "", "env": [], "tokens": [], "no_interp": key,
                         "shell_texts": [], "cwd": "", "argv": [], "opaque": None}
                runs.append(dict(info, kind="gate", label=check["name"], cmd=["<gate>", check["name"]] + record["files"],
                                 tree=record["tree"], exit=check["exit"], timed_out=False, seconds=float(check["seconds"]),
                                 end=_iso_epoch(record["finished"]), path=str(path), n=0, canon=canon, never=None,
                                 outside=outside))
        if (task_dir / "tests.jsonl").exists():
            seen = set()
            for line in reader.jsonl(task_dir / "tests.jsonl"):
                if not (isinstance(line.get("cmd"), list) and isinstance(line.get("seconds"), (int, float))
                        and "rc" in line and "files" in line):
                    jsonl["other_shape"] += 1
                    continue
                jsonl["lines"] += 1
                jsonl["seconds"] += float(line["seconds"])
                k = json.dumps(line["cmd"])
                jsonl["same_cmd_tree_unknown"] += k in seen
                seen.add(k)
    if jsonl["other_shape"]:
        reader.problem(tasks_dir, "%d tests.jsonl line(s) of another shape skipped" % jsonl["other_shape"])
    return runs, jsonl, reader.problems


def _gate_key(name, record):
    """(key, outside) of one gate check. Eligible for a duplicate only when every input that decides the check is in the
    record (tools/agent/pre-commit-gate.sh):
      prettier:APP      the changed files of the app and the tree (tool_check, :395-449): files + tree.
      gitleaks          commits BASE..HEAD (:608): head + base.sha.
      gitleaks-dirty    the files that differ from HEAD plus untracked ones, copied and scanned (:617-645): head + tree.
    Every other check is `not understood`: eslint:APP (Deno apps read the index, `git ls-files --others`, to count an
    untracked file as all-new, :325-346, and the name does not say which apps are Deno), typecheck:APP (the files deleted
    since BASE and the app's ts file count, :536-600, are not recorded), and any name this tool does not know. A gitleaks
    record without head or base.sha is not comparable either (F1)."""
    files = sorted(record["files"])
    kind = name.split(":", 1)[0]
    if kind == "prettier":
        return json.dumps(["gate", name, files]), None
    if kind not in ("gitleaks", "gitleaks-dirty"):
        why = {"eslint": "Deno lint reads the index (untracked files count as all-new)",
               "typecheck": "the files deleted since base are not recorded"}.get(kind, "its scope is unknown")
        return json.dumps(["gate", name, files, None]), "not understood: scope of %s (%s)" % (name, why)
    head = record.get("head")
    base = record["base"].get("sha") if isinstance(record.get("base"), dict) else None
    if not (isinstance(head, str) and head and isinstance(base, str) and base):
        return json.dumps(["gate", name, files, None]), "not understood: head/base of %s not recorded" % name
    return json.dumps(["gate", name, files, head, base]), None


def _iso_epoch(text):
    from datetime import datetime, timezone
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).timestamp()


# ------------------------------------------------------------------ classification (D2, D3)

def _order(run):
    return (run["end"] if run["end"] is not None else float("inf"), run["task"], run["n"])


def _covers(big, small):
    """Every test of `small` equals or lies under a test of `big`."""
    return all(any(s == b or s.startswith(b + ".") for b in big) for s in small)


def classify(runs):
    """Sets run['class'] (first | duplicate | retry | never | outside) and, for covered runs, run['covered'] = {by, seconds, how}.
    Returns the groups (key+tree with more than one counted run) and the covered pairs."""
    counted = sorted((r for r in runs if not (r["never"] or r["outside"])), key=_order)
    for r in runs:
        r["class"] = "never" if r["never"] else "outside" if r["outside"] else None
        r["covered"], r["outcome_differs"] = None, False
    groups = {}
    for r in counted:
        groups.setdefault((r["canon"]["key"], r["tree"]), []).append(r)
    for members in groups.values():
        for i, r in enumerate(members):
            if i == 0:
                r["class"] = "first"
            elif members[i - 1]["exit"] != 0:
                r["class"] = "retry"
            else:
                r["class"] = "duplicate"
                r["outcome_differs"] = r["exit"] != members[i - 1]["exit"]
    pairs = []
    for j, b in enumerate(counted):
        if b["class"] != "first" or b["canon"]["tests"] is None:
            continue
        best = None
        for a in counted[:j]:
            if (a["tree"], a["canon"]["runner"]) != (b["tree"], b["canon"]["runner"]) or a["canon"]["tests"] is None:
                continue
            if _covers(a["canon"]["tests"], b["canon"]["tests"]):
                cand = (b["seconds"], "measured")
            elif _covers(b["canon"]["tests"], a["canon"]["tests"]):
                cand = (None, "unknown") if a["timed_out"] else (min(a["seconds"], b["seconds"]), "estimated")
            else:
                continue
            kind = "retry (partial)" if a["exit"] != 0 else "covered"
            pair = {"earlier": a, "later": b, "seconds": cand[0], "how": cand[1], "kind": kind}
            if best is None or (best["kind"] != "covered" and kind == "covered") or (
                    kind == best["kind"] and (cand[0] or 0) > (best["seconds"] or 0)):
                best = pair
        if best:
            pairs.append(best)
            if best["kind"] == "covered":
                b["covered"] = {"by": best["earlier"]["path"], "seconds": best["seconds"], "how": best["how"]}
            else:
                b["class"] = "retry"
    return {k: v for k, v in groups.items() if len(v) > 1}, pairs


def repeated_seconds(run):
    if run["class"] == "duplicate":
        return run["seconds"]
    if run["class"] == "first" and run["covered"]:
        return run["covered"]["seconds"] or 0.0
    return 0.0


def build(tasks_dir, lane=None):
    runs, jsonl, problems = load(tasks_dir, lane)
    groups, pairs = classify(runs)
    per = {}
    for r in runs:
        row = per.setdefault((r["lane"], r["step"]), {"lane": r["lane"], "step": r["step"], "runs": 0, "total": 0.0,
                                                       "first": 0.0, "duplicate": 0.0, "covered": 0.0, "retry": 0.0,
                                                       "never": 0.0, "outside": 0.0, "covered_unknown": 0})
        row["runs"] += 1
        row["total"] += r["seconds"]
        rep = repeated_seconds(r)
        if r["class"] == "duplicate":
            row["duplicate"] += rep
        elif r["class"] == "first":
            row["covered"] += rep
            row["first"] += r["seconds"] - rep
            row["covered_unknown"] += bool(r["covered"] and r["covered"]["seconds"] is None)
        else:
            row[r["class"]] += r["seconds"]
    for row in per.values():
        row["repeat_share"] = (row["duplicate"] + row["covered"]) / row["total"] if row["total"] else 0.0
    total = sum(r["seconds"] for r in runs)
    repeated = sum(repeated_seconds(r) for r in runs)
    spelling = {}
    for r in runs:
        if r["class"] != "never" and r["canon"]["interp"]:
            spelling.setdefault((r["canon"]["no_interp"], r["tree"]), set()).add(r["canon"]["key"])
    top = sorted(groups.values(), key=lambda g: -sum(repeated_seconds(r) for r in g))
    return {
        "runs": runs, "groups": top, "pairs": pairs, "per_lane_step": sorted(per.values(), key=lambda x: (x["lane"], x["step"])),
        "totals": {"runs": len(runs), "seconds": total, "repeated": repeated,
                   "duplicates": sum(r["class"] == "duplicate" for r in runs),
                   "covered": sum(bool(r["covered"]) for r in runs),
                   "retries": sum(r["class"] == "retry" for r in runs),
                   "retry_seconds": sum(r["seconds"] for r in runs if r["class"] == "retry"),
                   "never": sum(r["class"] == "never" for r in runs),
                   "outside": sum(r["class"] == "outside" for r in runs),
                   "outside_seconds": sum(r["seconds"] for r in runs if r["class"] == "outside"),
                   "not_understood": sum(bool(r["outside"] and r["outside"].startswith("not understood")) for r in runs),
                   "repeat_share": repeated / total if total else 0.0,
                   "interpreter_spelling_only": sum(1 for keys in spelling.values() if len(keys) > 1),
                   "main_visible": sum(r["role"] == "main" for r in runs)},
        "tests_jsonl": jsonl,
        "main": "Main's own verification runs outside test-run.sh (or without TP_TASK_DIR) and leaves no record in task "
                "dirs: not counted, not guessed.",
        "problems": problems,
    }


# ------------------------------------------------------------------ output

def _run_json(r):
    return {"task": r["task"], "role": r["role"], "lane": r["lane"], "step": r["step"], "kind": r["kind"], "label": r["label"],
            "cmd": shown_cmd(r["cmd"]), "tree": r["tree"], "exit": r["exit"], "seconds": r["seconds"], "record": r["path"],
            "class": r["class"], "never": r["never"], "outside": r["outside"], "covered": r["covered"], "outcome_differs": r["outcome_differs"]}


def to_json(report):
    out = dict(report)
    out["runs"] = [_run_json(r) for r in report["runs"]]
    out["groups"] = [[_run_json(r) for r in g] for g in report["groups"]]
    out["pairs"] = [{"earlier": p["earlier"]["path"], "later": p["later"]["path"], "seconds": p["seconds"], "how": p["how"],
                     "kind": p["kind"]} for p in report["pairs"]]
    return out


def _line(r):
    return "    %-10s %-30s %-9s %-24s exit=%-3s %8.2fs %s" % (
        r["class"], r["task"][:30], r["role"][:9], (r["label"] or "-")[:24], r["exit"], r["seconds"], r["path"])


def render(report):
    t = report["totals"]
    out = ["recorded runs: %d (%.1f s); repeats: %d duplicate + %d covered = %.1f s (%.1f%% of recorded seconds); "
           "retries: %d (%.1f s); never counted: %d; not comparable: %d (%.1f s; %d of them not understood)" % (
               t["runs"], t["seconds"], t["duplicates"], t["covered"], t["repeated"], 100 * t["repeat_share"], t["retries"],
               t["retry_seconds"], t["never"], t["outside"], t["outside_seconds"], t["not_understood"])]
    out.append("Main: " + report["main"] + " Main-visible records: %d." % t["main_visible"])
    out.append("\nsame check, same content (key + tree), largest repeat first:")
    for g in report["groups"]:
        out.append("  tree %s  cmd: %s" % (g[0]["tree"][:12], " ".join(shown_cmd(g[0]["cmd"]))[:200]))
        out += [_line(r) + ("  (outcome differs)" if r["outcome_differs"] else "") for r in g]
    out.append("\ncovered by another run (same tree, runner, interpreter):")
    for p in (p for p in report["pairs"] if p["kind"] == "covered"):
        sec = "unknown" if p["seconds"] is None else "%.2fs" % p["seconds"]
        out.append("  %s (%s) by %s (%s): %s %s" % (p["later"]["path"], p["later"]["role"], p["earlier"]["path"],
                                                    p["earlier"]["role"], sec, p["how"]))
    out.append("\nretries (failed or timed out, then the same check on the same tree):")
    out += [_line(r) for r in report["runs"] if r["class"] == "retry"]
    out.append("\nnever counted (VR1 never-reused):")
    out += [_line(r) + "  " + r["never"] for r in report["runs"] if r["class"] == "never"]
    out.append("\nnot comparable: input outside the tree (D4b) or not understood (D8):")
    out += [_line(r) + "  " + r["outside"] for r in report["runs"] if r["class"] == "outside"]
    out.append("\nper lane and step (seconds):")
    out.append("  %-20s %-8s %5s %9s %9s %9s %9s %9s %9s %9s %6s" % ("lane", "step", "runs", "total", "first", "duplicate",
                                                                     "covered", "retry", "never", "outside", "share"))
    for row in report["per_lane_step"]:
        out.append("  %-20s %-8s %5d %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f %9.1f %5.1f%%" % (
            row["lane"][:20], row["step"][:8], row["runs"], row["total"], row["first"], row["duplicate"], row["covered"],
            row["retry"], row["never"], row["outside"], 100 * row["repeat_share"]))
    j = report["tests_jsonl"]
    out.append("\ntests.jsonl (run_bounded, no tree, not compared): %d lines, %.1f s, %d repeat a cmd of the same task "
               "(tree unknown)" % (j["lines"], j["seconds"], j["same_cmd_tree_unknown"]))
    out.append("interpreter-spelling-only groups (would merge if the interpreter were ignored; not counted): %d"
               % t["interpreter_spelling_only"])
    if report["problems"]:
        out.append("\nproblems (skipped):")
        out += ["  %s: %s" % (p["where"], p["problem"]) for p in report["problems"]]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tasks_dir", help="a session's tasks directory")
    parser.add_argument("--lane", help="only the tasks of this lane (meta.json lane)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    if not Path(args.tasks_dir).is_dir():
        print("repeats: not a directory: %s" % args.tasks_dir, file=sys.stderr)
        return 2
    report = build(args.tasks_dir, args.lane)
    print(json.dumps(to_json(report), indent=1, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
