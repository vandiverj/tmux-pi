"""The routing policy used when no model catalog can be used: lib/router_catalog.load_catalog raised CatalogError.

It is fixed here, in code, so a catalog that was rejected is never read again, leniently or otherwise
(lib/model_router.py _builtin_route and route_problem read it). It is data only, kept apart from the routing code,
which names no model. tests/test_model_routing.py holds it to the shipped catalog: the same effort order, ceiling and
default, and every model/effort pair below allowed there, by the same worker, at a shown, unrestricted level that no
role's min_level is above.
"""

BUILTIN_POLICY = {
    "effort_order": ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
    "ceiling": "high",
    "default_effort": "medium",
    "default_worker": "pi",
    "models": {"pi": ("openai/gpt-6.1-sol", ("low", "medium", "high")),
               "claude": ("claude-sonnet-5-5", ("low", "medium", "high"))},
}
