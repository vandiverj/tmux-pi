# K1 round 3: committed means the bytes on disk hash to HEAD's blob (route: rule-first)

Review: /Users/jerrickvandiver/Documents/Projects/tmux-pi/.tmux-pi/sessions/dbe2f4f8c1ac4e3fa440dce07e3e8180/tasks/review-coder-3a6e22f84f-172645/result.md

`git diff --raw HEAD` trusts the index's change detection. `--assume-unchanged`, `--skip-worktree` and `core.filemode=false` all hide an edited file from it.

## Rule
A tracked credential-named file counts as committed only when Main's own check of the disk agrees with HEAD. Nothing from git's change detection is used. The check:
- `os.lstat`: a regular file (not a symlink, not any other type);
- the exec bit matches HEAD's mode (100644 or 100755);
- the git blob id, computed in Python from the file's bytes, equals the object id `ls-tree HEAD` gives. The blob id is `hashlib` over `b"blob %d\0" % len(data) + data`, using sha1 or sha256 per `git rev-parse --show-object-format`.

Also keep the prompt when:
- `git ls-files -v` shows the file with an assume-unchanged (lower-case tag) or skip-worktree (`S`) flag;
- the file exceeds a size cap (1 MiB);
- any query fails.

Keep the existing content-filter refusal. Read the file only to hash it; never print or return its bytes.

## Tests
New table rows, all using a non-credential stand-in name where the guard blocks a credential name, with the classifier's credential-name check patched for the stand-in:
- an assume-unchanged edit → prompt;
- a skip-worktree edit → prompt;
- an edit with `core.filemode=false` and a mode change → prompt;
- an edit with the same size and the same mtime → prompt;
- clean → lift;
- a sha256 repository, clean → lift (skip if this git cannot create one).

## Report
Include the exact commands and log paths, and a Site / Disposition / Evidence sweep table.
