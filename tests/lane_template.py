"""A tree built once per class, given to each test as its own copy (TQ1 Q4a).

`copy_template(template, origin, dest)` clones `template` (a saved tree that was built at the path `origin`) to `dest`, then
rewrites every absolute path it holds from `origin` to `dest` (`rebase_paths`, the one function that does that), then fails loudly
if any copied file or link still names `origin` or `template` (`leaks`). A copy is the test's own: writes to it never reach the
template or another test's copy.

How a tree is copied: on macOS, one clonefile(2) of the whole folder (APFS copy-on-write: no data is copied until a side writes);
elsewhere, or where clonefile fails (a non-APFS volume), a plain `shutil.copytree` that keeps links as links."""
import ctypes
import os
from pathlib import Path
import re
import shutil
import sys

CLONE_NOFOLLOW = 0x0001  # sys/clonefile.h: a link at the top is cloned as a link, not followed


def _clonefile():
    if sys.platform != "darwin":
        return None
    try:
        func = ctypes.CDLL(None, use_errno=True).clonefile
    except (OSError, AttributeError):
        return None
    func.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    func.restype = ctypes.c_int
    return func


_CLONE = _clonefile()


def clone_tree(src, dst):
    """Copy the folder `src` to `dst` (which must not exist). Returns "clonefile" or "copy": how it was done."""
    src, dst = os.fspath(src), os.fspath(dst)
    if _CLONE is not None and _CLONE(os.fsencode(src), os.fsencode(dst), CLONE_NOFOLLOW) == 0:
        return "clonefile"
    shutil.rmtree(dst, ignore_errors=True)  # a failed clonefile leaves nothing, but never copy over a part
    shutil.copytree(src, dst, symlinks=True)
    return "copy"


def path_forms(path):
    """The spellings of `path` a tool may have written: as given and, on macOS, without or with the /private prefix of /var, /tmp
    and /etc (tempfile hands out /var/folders/..., which resolves to /private/var/folders/...)."""
    path = os.fspath(path)
    forms = {path}
    for top in ("/var/", "/tmp/", "/etc/"):
        if path.startswith("/private" + top):
            forms.add(path[len("/private"):])
        elif path.startswith(top):
            forms.add("/private" + path)
    return sorted(forms, key=len, reverse=True)  # the longest first: "/private/var/x" is replaced before "/var/x" inside it


def _entries(root):
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            yield Path(folder) / name


def rebase_paths(root, old, new):
    """Rewrite every mention of the folder `old` (in each spelling of path_forms) under `root` to `new`: in the contents of every
    regular file and in the target of every link. Returns the rewritten paths (relative to `root`)."""
    old_forms = path_forms(old)
    new = os.fspath(new)
    if any(form in new for form in old_forms) or any(new in form for form in old_forms):
        raise ValueError(f"cannot rebase {old} to {new}: one path holds the other, so a rewrite would not be one-way")
    changed = []
    for path in _entries(root):
        if path.is_symlink():
            target = os.readlink(path)
            fixed = _replace(target, old_forms, new)
            if fixed != target:
                path.unlink()
                os.symlink(fixed, path)
                changed.append(str(path.relative_to(root)))
        elif path.is_file():
            data = path.read_bytes()
            fixed = _replace(data, [os.fsencode(form) for form in old_forms], os.fsencode(new))
            if fixed != data:
                path.write_bytes(fixed)  # same inode: the file keeps its mode
                changed.append(str(path.relative_to(root)))
    return changed


def _replace(text, olds, new):
    """One pass over `text`: each spelling in `olds` (longest first) becomes `new`, and what was written is never read again (so
    a new path that holds an old spelling, as /private/var/x holds /var/x, is not rewritten twice). A spelling counts only where
    its last name ends (not inside a longer name: /t/ab is not rewritten in /t/abc; leaks still finds that and fails)."""
    pattern = "(?:" + "|".join(re.escape(os.fsdecode(old)) for old in olds) + r")(?![\w.-])"
    if isinstance(text, bytes):
        return re.sub(os.fsencode(pattern), lambda _: new, text)
    return re.sub(pattern, lambda _: new, text)


def rebase_text(text, old, new):
    """`text` (a command's output saved with the template) with `old` rewritten to `new`, as rebase_paths rewrites a file; as
    copy_template, nothing when `new` is `old` in another spelling or the same."""
    if os.fspath(new) in path_forms(old):
        return text
    return _replace(text, path_forms(old), os.fspath(new))


def leaks(root, *paths):
    """Every file or link under `root` whose contents (or target) still name one of `paths` in any spelling: relative paths."""
    forms = [form for path in paths for form in path_forms(path)]
    found = []
    for path in _entries(root):
        if path.is_symlink():
            hit = any(form in os.readlink(path) for form in forms)
        elif path.is_file():
            data = path.read_bytes()
            hit = any(os.fsencode(form) in data for form in forms)
        else:
            hit = False
        if hit:
            found.append(str(path.relative_to(root)))
    return found


def copy_template(template, origin, dest):
    """Clone `template` (built at `origin`) to `dest` and make it `dest`'s own. Fails if any copied file still names `origin` or
    `template`. Returns how it was copied ("clonefile" or "copy"). A copy back to `origin` itself (a test that rebuilt its own root)
    rewrites nothing and checks only for `template`."""
    how = clone_tree(template, dest)
    same = os.fspath(dest) in path_forms(origin)
    if not same:
        rebase_paths(dest, origin, dest)
    left = leaks(dest, template) if same else leaks(dest, origin, template)
    if left:
        raise AssertionError(f"the copy at {dest} still names the template's path {origin} in: {', '.join(left)}")
    return how
