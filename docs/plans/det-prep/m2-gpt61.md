# M2: route OpenAI mid/coding work to gpt-6.1-sol

Lane `gpt61`, step M2. Level: mid coder. Base: `lane/integrate` (68973cb). Touches: `config/router.json`,
`config/model-catalog.json`, the tests that read the real config, and `docs/plan.md` if it names the routed model.

## Why
The owner (2026-11-08) said: "we should be using gpt 6.1 models". Pi lists `openai-codex/gpt-6.1-sol` with a
500K context window. There is no 6.1 version of luna or astra. Committed config uses the `openai/` prefix; keep that prefix.

## Change
1. Change every `openai/gpt-6-sol` in `config/router.json` and `config/model-catalog.json` to `openai/gpt-6.1-sol`:
   the mid level, router chain, `coding` and `complex` routes, and any reviewer default. Leave luna (small) and astra (top) as they are.
2. In the catalog's `strong` level entry for gpt-6.1-sol, remove the `unverified` note only if it no longer applies
   (it is now routed). Set `context_window` to what `pi --list-models` reports. Record the command output in your report.
   Also decide whether `strong` and `mid` now name the same model. If they do, keep `strong` and say why in one line;
   do not remove a level.
3. Update tests that assert the real config's model ids (e.g. tests/test_routing_evals.py:80-81; find the others with
   grep for tests that load config/router.json or config/model-catalog.json). Tests that use `gpt-6-sol` only as a
   fixture value do not change.
4. Before your edit, show that the updated assertions fail on the old config. Then run test_routing_evals,
   test_router_catalog, test_model_routing, test_route_resolve and test_llm_router in both modes.

## Done means
The config names gpt-6.1-sol wherever it named gpt-6-sol. The JSON is valid. The changed assertions fail before the
change and pass after it. Do not commit. End with RESULT_STATUS: complete.
