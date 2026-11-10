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

## Owner decision 2026-11-08: same model on mid and strong, split by effort
gpt-6.1-sol sits on both levels: mid entry `efforts: ["low", "medium"]`, strong entry `efforts: ["high"]` (owner: "put gpt 6.1 sol at a higher effort on strong"). Strong's Pi entry stays `in_roster: false` as today. The catalog's one-level-per-model rule becomes one level per (model, effort).
Design (lib/router_catalog.py, lib/route_resolve.py, lib/model_router.py; lane py39-union's lib/ claim does not cover these files' behaviour, U1 only adds `from __future__` lines elsewhere):
1. validate_catalog: a model name may appear at several levels only if provider, worker, context_window and max_output agree and the effort lists are disjoint and ascending with the level order; anything else still fails ("listed twice").
2. level_of(cat, model, effort=None): with an effort, the level whose entry lists it; without one, the lowest level that lists the model (today's behaviour for unique models). Audit every caller (router_catalog 361/373, route_resolve 132/167/461, model_router 82/208/215/339) and pass the effort wherever the record has one.
3. Identity is (model, effort) wherever a move is decided: model_router ~202 `if target["model"] == rec["model"]: return` must not drop an escalation from 6.1@medium (mid) to 6.1@high (strong); an escalation onto the same model must raise the effort into strong's list.
4. build_system / prompt prose must show the effort split so the LLM router sees "gpt-6.1-sol (high)" at strong.
Tests first, failing before: duplicate with overlapping efforts still rejected; mismatched context rejected; level_of with and without effort; escalation mid->strong on the same model raises effort and records the level; redirect not dropped; committed catalog loads. Then the five routing modules plus test_model_routing in both modes (Main runs inside mode if the trust guard refuses).
