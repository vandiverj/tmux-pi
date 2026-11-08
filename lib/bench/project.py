"""The bench project (battle-soccer): the base tag, and a fresh git-worktree copy per repeat.

Nothing here edits the project's own working tree or index: the tag is built with git plumbing on a temporary index,
copies are separate worktrees, and cleanup removes only the copy it made (the project has other, unrelated worktrees)."""
import os
import shutil
import tempfile
from pathlib import Path

from .shell import ShellError

BASE_COMMIT = "dfea0c8"
TAG = "bench-base-v1"
MANIFEST_PATH = ".tmux-pi/agent-tools.json"
AUTHOR = {"GIT_AUTHOR_NAME": "tp-bench", "GIT_AUTHOR_EMAIL": "tp-bench@localhost", "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
          "GIT_COMMITTER_NAME": "tp-bench", "GIT_COMMITTER_EMAIL": "tp-bench@localhost", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000"}


class ProjectError(RuntimeError):
    pass


def git(shell, project, *args, env=None, timeout=60, tolerate=None):
    return shell.run(["git", "-C", str(project), *args], env=env, timeout=timeout, tolerate=tolerate)


def _ok(shell, project, what, *args, env=None, timeout=60):
    """git's stdout, stripped; a nonzero exit is a ProjectError naming `what`."""
    try:
        return git(shell, project, *args, env=env, timeout=timeout).stdout.strip()
    except ShellError as error:
        raise ProjectError(f"{what} failed: {error}") from error


def ensure_base(shell, project, manifest_file):
    """Create the tag bench-base-v1 if it is missing: the commit BASE_COMMIT plus a committed .tmux-pi/agent-tools.json
    (a frozen copy of A1's template, `manifest_file`). Idempotent. Returns (tag commit, created)."""
    base = _ok(shell, project, f"resolving {BASE_COMMIT}", "rev-parse", "--verify", f"{BASE_COMMIT}^{{commit}}")
    have = git(shell, project, "rev-parse", "--verify", "--quiet", f"refs/tags/{TAG}^{{commit}}",
               tolerate="exit 1 with --quiet means the tag does not exist yet, which is the question asked")
    if have.returncode == 0:
        tagged = have.stdout.strip()
        parent = _ok(shell, project, f"reading the parent of {TAG}", "rev-parse", f"{tagged}^")
        if parent != base:
            raise ProjectError(f"tag {TAG} exists but is not {BASE_COMMIT} plus the manifest commit (parent {parent[:10]}); not touching it")
        return tagged, False
    with tempfile.TemporaryDirectory(prefix="tp-bench-index-") as tmp:
        env = {**AUTHOR, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _ok(shell, project, "read-tree", "read-tree", base, env=env)
        blob = _ok(shell, project, "hash-object", "hash-object", "-w", str(manifest_file), env=env)
        _ok(shell, project, "update-index", "update-index", "--add", "--cacheinfo", f"100644,{blob},{MANIFEST_PATH}", env=env)
        tree = _ok(shell, project, "write-tree", "write-tree", env=env)
        commit = _ok(shell, project, "commit-tree", "commit-tree", tree, "-p", base, "-m",
                     f"bench-base-v1: {BASE_COMMIT} plus {MANIFEST_PATH}", env=env)
    _ok(shell, project, f"tagging {TAG}", "tag", TAG, commit)
    return commit, True


def make_copy(shell, project, dest, fixtures=()):
    """A fresh detached worktree of the project at the tag in `dest`. The project's node_modules is linked in (untracked there).
    `fixtures` is [(source file, relative target)] copied into the copy (frozen docs a local task asks about)."""
    dest = Path(dest)
    _ok(shell, project, "worktree add", "worktree", "add", "--detach", str(dest), TAG, timeout=120)
    modules = Path(project) / "node_modules"
    if modules.is_dir() and not (dest / "node_modules").exists():
        os.symlink(modules, dest / "node_modules")
    for source, target in fixtures:
        out = dest / target
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, out)
    return dest


def _admin_dir(dest):
    """The .git/worktrees/<name> directory of a copy, from the `gitdir:` line of its .git file; None when it cannot be read."""
    try:
        line = (Path(dest) / ".git").read_text().strip()
    except OSError:
        return None
    return Path(line.split(":", 1)[1].strip()) if line.startswith("gitdir:") else None


def remove_copy(shell, project, dest):
    """Remove the copy and its worktree registration, and nothing else (no `git worktree prune`: other worktrees
    of the project may be stale on purpose). Never raises; returns True when the directory is gone."""
    dest = Path(dest)
    admin = _admin_dir(dest)
    if (dest / "node_modules").is_symlink():
        (dest / "node_modules").unlink()  # never let a removal follow the link into the project's real modules
    git(shell, project, "worktree", "remove", "--force", str(dest), timeout=120,
        tolerate="the directory and the worktree admin dir are removed by hand next; the result is whether the directory is gone")
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    if admin is not None and admin.is_dir() and admin.parent.name == "worktrees":
        shutil.rmtree(admin, ignore_errors=True)
    return not dest.exists()
