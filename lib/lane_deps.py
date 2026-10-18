"""Lane dependencies: share the main checkout's node_modules into a lane's worktree with guarded symlinks.

A lane is a fresh worktree, and installing node_modules into it costs minutes of disk and, on removal, minutes of unlinking.
When the lane's base has the same dependency manifests as the main checkout, the main checkout's installed node_modules
directories are linked in instead (absolute targets), and the lane record says `deps: shared`; otherwise the lane has `deps: own`
and nothing is linked.

Which directories: the root and every directory that has a `package.json` at the lane's base commit, is not a Deno project (a
`deno.json` or `deno.jsonc` beside it: Deno manages its own node_modules and may rewrite it) and has a real `node_modules`
directory in the main checkout. Nothing is named for one repository; the repository's own layout decides.

When: the lane's base must match what main has INSTALLED, not main's branch (main may be on any branch). For each directory to share,
package.json must equal main's by git blob hash, and package-lock.json must agree with npm's hidden lockfile
(node_modules/.package-lock.json) in main: every installed package is in the lane's lockfile with the same version, resolved
and integrity, and every non-optional package of the lane's lockfile is installed. Only the "packages" tables are compared,
because the hidden lockfile has no root entry and leaves out packages that were not installed. With no hidden lockfile, main's
checked-out package-lock.json is compared by hash instead. deno.json and deno.lock are never compared: Deno's node_modules is
never linked. A stale install (main's node_modules older than its lockfile) therefore leaves the lane `own`. A main
checkout with no node_modules (but with a package.json) leaves the lane `own` with a warning. A link is also skipped, with a
warning, when the lane already has something at that path or when git would not ignore the link (a `node_modules/` ignore rule
matches directories only, and a symlink would then show up as an untracked file and make the lane look dirty).

Never run `npm ci` or `npm install` in a lane whose node_modules are linked: npm deletes node_modules first, and through the
link that empties the MAIN checkout's. Unlink first (`unlink_shared`; `python3 lib/lane_deps.py unlink WORKTREE`), which turns the
lane into one with its own dependencies. Lane removal unlinks first too, and only with os.unlink: a link is never followed.
Only links that point at a node_modules directory inside the main checkout are touched; a link that someone else made is left."""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lanes  # noqa: E402

SHARED, OWN = "shared", "own"
DENO_FILES = ("deno.json", "deno.jsonc")
MODULES = "node_modules"
_PRUNE = (".git", MODULES)


def _warning(note, path=""):
    return {"kind": "deps", "path": path, "note": note}


def _base_files(repo, base_commit):
    """{repo-relative path: blob id} of every file the base commit tracks."""
    _, out = lanes._git(["ls-tree", "-r", "-z", base_commit], repo)
    files = {}
    for entry in out.split("\0"):
        if entry:
            meta, _, path = entry.partition("\t")
            files[path] = meta.split()[2]
    return files


def _blob_differs(repo, path, wanted):
    """True when main's file at `path` is not the file with git blob id `wanted`."""
    target = os.path.join(repo, path)
    if not os.path.isfile(target) or os.path.islink(target):
        return True
    _, blob = lanes._git(["hash-object", "--", path], repo)
    return blob.strip() != wanted


def _packages(text):
    """The "packages" table of an npm lockfile text, or None when it is not one."""
    try:
        table = json.loads(text).get("packages")
    except (ValueError, AttributeError):
        return None
    return table if isinstance(table, dict) else None


def _installed_differs(installed, wanted):
    """What differs between npm's hidden lockfile (the packages npm really installed) and the lane's package-lock.json, as a
    short phrase, or None when the install satisfies the lockfile. The two files are not the same text: the hidden one has
    no root entry and omits packages that were not installed (optional ones for another platform), so the comparison is
    on the "packages" tables. Every installed package must be in the lockfile with the same version, resolved and
    integrity, and every package the lockfile lists must be installed unless it is optional."""
    for key, entry in sorted(installed.items()):
        want = wanted.get(key)
        if want is None:
            return f"{key} is installed but not in the lockfile"
        for field in ("version", "resolved", "integrity", "link"):
            if entry.get(field) != want.get(field):
                return f"{key} is installed at {entry.get('version')} but the lockfile has {want.get('version')}"
    for key, want in sorted(wanted.items()):
        if key and key not in installed and not want.get("optional") and not want.get("link"):
            return f"{key} is in the lockfile but not installed"
    return None


def _same_manifests(repo, files, dirs):
    """Why the lane's dependencies are not what main has installed (a path or phrase), or None when they agree. Only the
    directories in `dirs` count (not Deno projects: a deno.lock is Deno's own business and its node_modules is never linked).
    package.json must equal main's file by git blob hash. A package-lock.json is compared with what npm installed in main (the
    hidden lockfile node_modules/.package-lock.json) and, when main has none, with main's checked-out lockfile by hash."""
    for where in dirs:
        path = os.path.join(where, "package.json")
        if path in files and _blob_differs(repo, path, files[path]):
            return path
        lock = os.path.join(where, "package-lock.json")
        if lock not in files:
            continue
        hidden = os.path.join(repo, where, MODULES, ".package-lock.json")
        if not os.path.isfile(hidden):
            if _blob_differs(repo, lock, files[lock]):
                return lock
            continue
        _, text = lanes._git(["cat-file", "blob", files[lock]], repo)
        with open(hidden, encoding="utf-8") as handle:
            installed, wanted = _packages(handle.read()), _packages(text)
        if installed is None or wanted is None:
            return f"{lock} (the installed lockfile cannot be read)"
        problem = _installed_differs(installed, wanted)
        if problem:
            return f"{lock}: {problem}"
    return None


def _candidates(repo, files):
    """The repo-relative directories ("" is the root) whose node_modules main has installed and that may be shared, and
    whether the base has any non-Deno package.json at all."""
    found, wanted = [], False
    for path in sorted(files):
        if os.path.basename(path) != "package.json":
            continue
        where = os.path.dirname(path)
        if any(os.path.join(where, name) in files for name in DENO_FILES):
            continue
        wanted = True
        modules = os.path.join(repo, where, MODULES)
        if os.path.isdir(modules) and not os.path.islink(modules):
            found.append(where)
    return found, wanted


def share(repo, worktree, base_commit):
    """Link main's node_modules into the new `worktree` when the module text's conditions hold. Returns {"deps": "shared"|"own",
    "links": [path of each link made], "warnings": [...]}. It never raises for a reason of its own (the lane is then `own`
    and says why); a link made before a failure is removed again."""
    repo, worktree = os.path.realpath(repo), os.path.realpath(worktree)
    try:
        files = _base_files(repo, base_commit)
        dirs, wanted = _candidates(repo, files)
        if not dirs:
            return {"deps": OWN, "links": [], "warnings": [_warning(f"the main checkout {repo} has no node_modules to share", repo)] if wanted else []}
        differs = _same_manifests(repo, files, dirs)
    except ValueError as error:
        return {"deps": OWN, "links": [], "warnings": [_warning(f"dependencies were not shared ({error})")]}
    if differs:
        return {"deps": OWN, "links": [], "warnings": [_warning(f"the lane's base does not match what the main checkout has installed ({differs}), so the "
                                                               "lane gets its own dependencies (nothing is linked)", differs)]}
    links, warnings = [], []
    try:
        for where in dirs:
            link, target = os.path.join(worktree, where, MODULES), os.path.join(repo, where, MODULES)
            if os.path.lexists(link):
                warnings.append(_warning(f"{os.path.join(where, MODULES)} already exists in the lane; it is not replaced", link))
                continue
            if not os.path.isdir(os.path.dirname(link)):
                continue
            os.symlink(target, link)
            if lanes._git(["check-ignore", "-q", "--no-index", "--", os.path.join(where, MODULES)], worktree, ok=(0, 1))[0] != 0:
                os.unlink(link)
                warnings.append(_warning(f"git would not ignore a link at {os.path.join(where, MODULES)} (its ignore rule matches "
                                         "directories only), so it is not linked", link))
                continue
            links.append(link)
    except (OSError, ValueError) as error:
        for link in links:
            _unlink(link)
        return {"deps": OWN, "links": [], "warnings": warnings + [_warning(f"dependencies were not shared ({error})")]}
    return {"deps": SHARED if links else OWN, "links": links, "warnings": warnings}


def _unlink(link):
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass


def shared_links(repo, worktree):
    """The node_modules symlinks in `worktree` that point at a node_modules directory of the main checkout `repo`, found by
    walking the tree without following any link and without entering .git or a node_modules."""
    repo, worktree = os.path.realpath(repo), os.path.realpath(worktree)
    found = []
    for top, dirs, _ in os.walk(worktree, followlinks=False):
        for name in list(dirs):
            path = os.path.join(top, name)
            if name == MODULES and os.path.islink(path):
                target = os.path.normpath(os.path.join(top, os.readlink(path)))
                if os.path.basename(target) == MODULES and lanes._inside(os.path.realpath(os.path.dirname(target)), repo):
                    found.append(path)
            if name in _PRUNE or os.path.islink(path):
                dirs.remove(name)
    return found


def unlink_shared(repo, worktree):
    """Remove (os.unlink, never through the link) every shared node_modules link of `worktree`. Returns the paths unlinked.
    Call it before `git worktree remove` and before any `npm ci` / `npm install` in a lane."""
    links = shared_links(repo, worktree)
    for link in links:
        _unlink(link)
    return links


def unshare_lane(worktree):
    """The install guard: unlink the shared links of the lane or checkout at `worktree` and, for a recorded lane, record
    `deps: own`. Run it before any `npm ci` / `npm install` there. Returns the paths unlinked."""
    common = lanes.git_common_dir(worktree)
    repo = lanes.main_worktree(common)
    links = unlink_shared(repo, worktree)
    record, _ = lanes.lane_containing(worktree)
    if record is not None and record["deps"] == SHARED and os.path.realpath(worktree) == record["worktree"]:
        with lanes.lane_lock(common):
            current = lanes.read_lane(common, record["name"])
            if current["deps"] == SHARED:
                lanes.write_lane(common, dict(current, deps=OWN))
    return links


if __name__ == "__main__":  # lane_deps.py unlink WORKTREE: the install guard
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "unlink":
            raise ValueError("usage: lane_deps.py unlink WORKTREE   (run before npm ci / npm install in a lane)")
        for gone in unshare_lane(sys.argv[2]):
            print(f"unlinked {gone}")
    except ValueError as error:
        print(f"lane_deps: {error}", file=sys.stderr)
        sys.exit(1)
