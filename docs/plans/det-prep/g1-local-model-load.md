# G1: tmux-pi controls the local model's load size and lifetime

Lane `local-model`, step 1. Level: `small` coder (`--level small`; one script and two config files). Touches:
`bin/tp-lmstudio`, `bin/tp-exit`, `config/orchestration.json`, `agent/models.json`, `tests/test_tp_lmstudio.py` (new or the
existing lmstudio test), `docs/` (the local model paragraph in README).

## Why
The owner's LM Studio grows until the work Mac freezes. On this Mac the model is resident at a 262,144-token context
(its maximum), parallel 1, no TTL, while tmux-pi expects 131,072. `bin/tp-lmstudio` loads with `--context-length` only
when the model is not loaded, warns only when the loaded context is SMALLER than expected, and never unloads or sets a
TTL. The MLX engine keeps memory at the peak context it has served and its prompt cache can grow (LM Studio bug tracker
#2574, mlx-lm #883 and #1390), so an over-sized load plus a long-lived process is the likely cause.

## Change
1. Config `local_model` in `config/orchestration.json`: `context` (default: the model's `contextWindow` in
   `agent/models.json`), `ttl_seconds` (default 1800), `reload_if_larger` (default true), `unload_on_exit` (default false).
2. `tp-lmstudio ensure`: when the model is loaded with a context larger than `context` by more than 10% and
   `reload_if_larger` is true and no local worker of this session is running, unload and load at `context`; otherwise warn
   naming the command. Every load passes `--ttl ttl_seconds`. `status` prints context, TTL and the engine RSS
   (`ps -o rss` of the LM Studio engine process) so the growth is visible.
3. `agent/models.json`: lower the Qwen `contextWindow` to 65536 (the routing rules give local work only short shapes:
   locate_quote, count_enumerate, classify_short, summarize_files, small_edit, run_named_checks); keep `maxTokens` within it.
4. `tp-exit`: when `unload_on_exit` is true and no other tmux-pi session uses the model, `lms unload MODEL`.

## Done means
1. With a stub `lms` reporting the model at 262144, `ensure` unloads and reloads at the configured context with `--ttl`.
2. With a local worker of the session running, it only warns and names the command.
3. A fresh load passes `--context-length` and `--ttl`; `status` prints context, TTL and RSS.
4. `unload_on_exit` true calls `lms unload` only when no other session's state lists a live local worker.
5. Tests for 1 to 4 fail without the change; README documents the keys.

## Verify (V-G)
Here: `bin/tp-lmstudio status` before and after a `--local-only` dispatch; record context and RSS in evidence.md. On the
work Mac: the same `status` line every hour of a work day, to see whether RSS still climbs at the smaller context.
