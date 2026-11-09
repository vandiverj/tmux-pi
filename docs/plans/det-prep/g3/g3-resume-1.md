# G3, resume 1: never touch the owner's tmux server (Main, 2026-11-09)

Your run ended at about 04:22. The default tmux server (/private/tmp/tmux-501/default) was stopped, and it took every pane with it:
yours, another lane's worker, and any session of the owner's. The cause is in your `tools/smoke/g3_e2e.py`:
- `self.tmux_cmd = ["tmux"]` (line 85) is set to `[tmux, "-L", self.sock]` only inside the fake setup (line 435).
- Cleanup runs `kill-server` when `self.fake` is set (line 511).

So a fake run that failed before line 435 (an injected setup failure, for example) ran `tmux kill-server` on the default server.
Your edits are still in the worktree (tools/smoke/g3_e2e.py; tests/test_g3_e2e.py was not written yet). Keep going from them,
with these changes:

## Must
- **Isolation by construction:** in `--fake` mode, and in every test, set `TMUX_TMPDIR` to a fresh temp directory **before** any tmux
  call. Pass it in the env of every subprocess, and remove `TMUX` and `TMUX_PANE`. With that, even a plain `tmux` call cannot reach
  the owner's server. Set `-L <private socket>` from the constructor, never later.
- `kill-server` only when (a) fake mode, (b) the command carries `-L` with this run's private socket name, and (c) `TMUX_TMPDIR`
  is the run's temp dir. Assert all three in code before the call. Live mode never runs `kill-server`.
- In live mode, kill only the panes and session this run created, named by their ids and the run's own session name.
- **Test it:** in tests/test_g3_e2e.py, put a `tmux` shim first on PATH for every test. The shim records each call's argv and
  env, then runs the real tmux. Assert that across the whole module:
  - no call ran without the private `TMUX_TMPDIR`;
  - no `kill-server` was sent to any socket but the run's own.

  Include the injected failure at the earliest point (before any socket is set) and at each step.
- Until these guards are in place and tested, run only single tests that make no tmux call. Once they are in, run the module.

Everything else in g3-e2e-smoke.md still applies.
