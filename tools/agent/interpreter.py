"""The python a test command starts (called by test-run.sh; never run by hand).

usage: python3 -I interpreter.py WORKTREE -- CMD ARGS...

Prints, as the NUL-separated tokens tools/agent/json_record.py reads, the field `interpreter` of the test-run record: an
object {path, version}, or null when the command starts no python or it cannot be resolved. The head of CMD is read the
way tools/checks/repeats.py reads wrappers: leading NAME=VALUE words, `env [-i] [-u NAME]... [NAME=VALUE]...` and
`perl -e 'alarm N; exec @ARGV'` are skipped (a PATH they set or remove is the PATH the command sees); the next word is
the interpreter when its name is python, python3, python3.14 ... Without a `/` it is looked up on that PATH (os.defpath
when PATH is unset, as exec does), with a `/` it is relative to WORKTREE. path is the real path (links followed); version
is what that interpreter reports (one `python -I -c` start, about 20-40 ms). A shell, a `bash -c` text or any other head
is not read: null.
"""
import os
import re
import subprocess
import sys

INTERP = re.compile(r"python[0-9.]*")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
ALARM = re.compile(r"\s*alarm\s+(\d+);\s*exec\s+@ARGV;?\s*")
VERSION = "import sys; print('%d.%d.%d' % sys.version_info[:3])"


def head_and_env(cmd, env):
    """(argv after the wrappers, the env the command sees); None when a wrapper form is not read."""
    args, env = list(cmd), dict(env)
    while args:
        word = args[0]
        if "=" in word and ENV_NAME.fullmatch(word.split("=", 1)[0]):
            name, value = word.split("=", 1)
            env[name] = value
            args = args[1:]
        elif os.path.basename(word) == "env":
            i = 1
            while i < len(args):
                a = args[i]
                if a == "-i":
                    env = {}
                elif a == "-u" and i + 1 < len(args):
                    env.pop(args[i + 1], None)
                    i += 1
                elif a.startswith("-"):
                    return None, env
                elif "=" in a and ENV_NAME.fullmatch(a.split("=", 1)[0]):
                    env[a.split("=", 1)[0]] = a.split("=", 1)[1]
                else:
                    break
                i += 1
            args = args[i:]
        elif os.path.basename(word) == "perl" and len(args) >= 3 and args[1] == "-e" and ALARM.fullmatch(args[2]):
            args = args[3:]
        else:
            break
    return args, env


def resolve(worktree, cmd, base_env):
    """{path, version} of the python `cmd` starts, or None."""
    args, env = head_and_env(cmd, base_env)
    if not args or not INTERP.fullmatch(os.path.basename(args[0])):
        return None
    head = args[0]
    if "/" in head:
        found = [head if os.path.isabs(head) else os.path.join(worktree, head)]
    else:
        found = [os.path.join(d or ".", head) for d in env.get("PATH", os.defpath).split(os.pathsep)]
    path = next((p for p in found if os.path.isfile(p) and os.access(p, os.X_OK)), None)
    if path is None:
        return None
    path = os.path.realpath(path)
    try:
        out = subprocess.run([path, "-I", "-c", VERSION], capture_output=True, text=True, timeout=10, env=env, cwd=worktree)
    except (OSError, subprocess.SubprocessError):
        return None
    version = out.stdout.strip()
    return {"path": path, "version": version} if out.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", version) else None


def main(argv):
    if len(argv) < 3 or argv[1] != "--":
        return 2
    found = resolve(argv[0], argv[2:], os.environ)
    tokens = ["r", "interpreter", "null"] if found is None else ["s", "interpreter.path", found["path"], "s", "interpreter.version", found["version"]]
    sys.stdout.buffer.write(b"".join(os.fsencode(t) + b"\0" for t in tokens))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
