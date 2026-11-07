# G1 (local-model) parked after round 3

Round 3 reviewer (review-coder-d7ce94da2b-160710): REQUEST_CHANGES.
- F1, family exit-unload (the same family three rounds running): `bin/tp-exit:28` calls `tp-reset` before the unload. `tp-reset` kills worker panes, so `tp-exit` run from a worker pane dies before it unloads. Suggested fix: do the whole unload decision first, before reset and before kill (or detach with nohup), and test it with a reset stub that kills the caller.
- F2 (new): `status` prints the configured TTL (`bin/tp-lmstudio:109,199`) even for a model loaded manually with another TTL or none.

Live check on this Mac: the engine RSS filter reports 21623 MB with Qwen loaded, which is correct.
State: uncommitted in ../tmux-pi-wt-local-model. 14 tests OK. The owner decides.
