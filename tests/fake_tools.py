"""Shared test helpers: a fake `tmux` executable and a hermetic `git` runner. Import with the tests directory on sys.path."""
import os
from pathlib import Path
import shutil
import subprocess

GIT = shutil.which("git")
# No GIT_* variable of the caller, no user or system git config, a fixed author and committer.
GIT_ENV = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
GIT_ENV.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_COMMITTER_NAME="t",
               GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_EMAIL="t@example.invalid")


def fake_tmux(directory, script='echo "%1 1 99999999"\n', name="tmux"):
    """Write an executable fake `tmux` into `directory` and return its path.

    `script` is the shell text after the shebang; a script that starts with `#!` is written as given."""
    path = Path(directory) / name
    path.write_text(script if script.startswith("#!") else "#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


def git(repo, *args, timeout=60):
    """Run git in `repo` with GIT_ENV; fail the test with git's stderr on a nonzero exit; return stripped stdout."""
    run = subprocess.run([GIT or "git", "-C", str(repo), *args], env=GIT_ENV, capture_output=True, text=True, timeout=timeout)
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()
