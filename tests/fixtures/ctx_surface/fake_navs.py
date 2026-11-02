"""A fake "real" navigator module for the CX4 tests: NavWeb and NavApi with the constructors, methods and answer shapes of CX2's
lib/ctxengine/nav_web.py and CX3's lib/ctxengine/nav_api.py, answering from canned data about tests/fixtures/ctx_surface/project.

The surface loads navigators by module name (surface.NAV_MODULES); the tests point both names at this module to show that a real
navigator drops in with no change to the surface. CALLS records every method call; BEHAVIOUR replaces one answer (a refusal, a
warming helper) so the tests can see how each status is surfaced. Nothing here starts a process or opens a socket."""

import os

# The answer shapes of the real navigators, read from their source (not from the surface):
#   web: CX2 lane/ctx-nav-web lib/ctxengine/nav_web/host.mjs, op_definition, op_references, op_exports, op_imports, op_stats; the status words
#        and the "withheld" count / "<withheld>" placeholder of nav_web.py (_clean).
#   api: CX3 lane/ctx-nav-api lib/ctxengine/nav_api.py, _importers, _imports, _forward_graph, _symbols, status.
# {(kind, op, variant): (answer keys, {row list key: row keys})}; keys the real code adds only sometimes are in OPTIONAL.
SHAPES = {
    ("web", "exports", None): (("status", "is_module", "exports", "total", "truncated", "index_age_s", "server_ms"),
                               {"exports": ("name", "kind", "line", "reexport", "defined_at")}),
    ("web", "imports", "imports"): (("status", "direction", "target", "edges", "total", "truncated", "unresolved", "index_age_s", "server_ms"),
                                    {"edges": ("specifier", "line", "resolved", "external")}),
    ("web", "imports", "importers"): (("status", "direction", "target", "edges", "total", "truncated", "unresolved", "index_age_s", "server_ms"),
                                      {"edges": ("from", "line", "specifier", "to", "depth")}),
    ("web", "definition", None): (("status", "at", "definitions", "signature", "doc", "omitted_outside_root", "index_age_s", "server_ms"),
                                  {"definitions": ("path", "line", "column", "external", "name", "kind", "container", "text")}),
    ("web", "references", None): (("status", "at", "definitions", "references", "total", "truncated", "omitted_outside_root", "omitted_external",
                                   "index_age_s", "server_ms"),
                                  {"definitions": ("path", "line", "column", "external", "name", "kind"),
                                   "references": ("path", "line", "column", "external", "text", "is_definition", "is_write")}),
    ("api", "importers", None): (("status", "target", "total", "truncated", "importers", "index_age_s", "unresolved_total", "files_indexed", "scanned"),
                                 {"importers": ("path", "line", "spec")}),
    ("api", "imports", None): (("status", "target", "source", "files", "external", "unresolved", "index_age_s", "unresolved_total", "files_indexed",
                                "scanned"),
                               {"files": ("path", "line", "spec")}),
    ("api", "symbols", None): (("status", "path", "source", "cached", "total", "truncated", "symbols"),
                               {"symbols": ("name", "kind", "path", "line", "signature", "doc")}),
}
OPTIONAL = {"api": {"type_only", "dynamic", "config"}}  # on a CX3 edge row only when true / set

CALLS = []
INSTANCES = []   # (kind, constructor arguments) of every navigator the surface built
BEHAVIOUR = {}   # {("web" | "api", method): answer dict or callable(args) -> answer dict}

CART = "apps/launchpad/web/src/cart.ts"
ORDER = "lib/launchpad/types/order.ts"
ORDERS = "apps/launchpad/api/src/orders.ts"
MANY = "apps/launchpad/api/src/many.ts"
WEB_P = [f"apps/launchpad/web/src/p{n:02d}.ts" for n in range(1, 11)]
ROUTES = [f"apps/launchpad/api/src/routes/r{n:02d}.ts" for n in range(1, 15)]


# Answers captured from the REAL navigators (nav_web and nav_api, run on a large private Deno + Next.js monorepo for lane ctx-surface-real),
# trimmed to two rows per list and renamed to the fixture's files. Every key and row key is as the navigator sent it, including web's `server_ms`
# (a timing field the surface once did not know, which made every web answer read "not all returned") and api's index-wide `unresolved_total`.
# Keyed like SHAPES. The tests feed them to the surface unchanged: a field the surface cannot place, or a shape that drifts from SHAPES, fails there.
REAL = {('web', 'imports', 'imports'): {'status': 'ok',
                                 'direction': 'imports',
                                 'target': CART,
                                 'edges': [{'specifier': '@/actions/totals',
                                            'line': 2,
                                            'resolved': 'apps/launchpad/web/src/totals.ts',
                                            'external': False},
                                           {'specifier': '@/actions/pricing',
                                            'line': 3,
                                            'resolved': 'apps/launchpad/web/src/pricing.ts',
                                            'external': False}],
                                 'total': 2,
                                 'truncated': False,
                                 'unresolved': 0,
                                 'index_age_s': 0,
                                 'server_ms': 60},
 ('web', 'imports', 'importers'): {'status': 'ok',
                                   'direction': 'importers',
                                   'target': CART,
                                   'edges': [{'from': 'apps/launchpad/web/src/page.tsx',
                                              'line': 2,
                                              'specifier': '@/components/users/Cart',
                                              'to': CART,
                                              'depth': 1},
                                             {'from': 'apps/launchpad/web/src/list.tsx',
                                              'line': 2,
                                              'specifier': '@/components/users/Cart',
                                              'to': CART,
                                              'depth': 1}],
                                   'total': 2,
                                   'truncated': False,
                                   'unresolved': 0,
                                   'index_age_s': 1,
                                   'server_ms': 1394},
 ('web', 'exports', None): {'status': 'ok',
                            'is_module': True,
                            'exports': [{'name': 'CartContent', 'kind': 'variable', 'line': 107, 'reexport': False, 'defined_at': None},
                                        {'name': 'default', 'kind': 'variable', 'line': 596, 'reexport': False, 'defined_at': None}],
                            'total': 2,
                            'truncated': False,
                            'index_age_s': 2,
                            'server_ms': 59},
 ('web', 'definition', None): {'status': 'ok',
                               'at': {'line': 577, 'column': 7},
                               'definitions': [{'path': CART,
                                                'line': 577,
                                                'column': 7,
                                                'external': False,
                                                'name': 'Cart',
                                                'kind': 'const',
                                                'container': '',
                                                'text': 'const f = 1;'}],
                               'signature': 'const f: () => number',
                               'doc': '',
                               'omitted_outside_root': 0,
                               'index_age_s': 2,
                               'server_ms': 157},
 ('web', 'references', None): {'status': 'ok',
                               'at': {'line': 577, 'column': 7},
                               'definitions': [{'path': 'apps/launchpad/web/src/page.tsx',
                                                'line': 2,
                                                'column': 8,
                                                'external': False,
                                                'name': 'Cart',
                                                'kind': 'alias'},
                                               {'path': 'apps/launchpad/web/src/list.tsx',
                                                'line': 2,
                                                'column': 8,
                                                'external': False,
                                                'name': 'Cart',
                                                'kind': 'alias'}],
                               'references': [{'path': 'apps/launchpad/web/src/page.tsx',
                                               'line': 2,
                                               'column': 8,
                                               'external': False,
                                               'text': 'x',
                                               'is_definition': False,
                                               'is_write': True},
                                              {'path': 'apps/launchpad/web/src/list.tsx',
                                               'line': 12,
                                               'column': 38,
                                               'external': False,
                                               'text': 'x',
                                               'is_definition': False,
                                               'is_write': False}],
                               'total': 2,
                               'truncated': False,
                               'omitted_outside_root': 0,
                               'omitted_external': 0,
                               'index_age_s': 2,
                               'server_ms': 234},
 ('api', 'importers', None): {'status': 'ok',
                              'target': ORDERS,
                              'total': 4,
                              'truncated': True,
                              'importers': [{'path': 'apps/launchpad/api/src/orders_a_test.ts', 'line': 4, 'spec': '@/orders.ts'},
                                            {'path': 'apps/launchpad/api/src/orders_b_test.ts', 'line': 4, 'spec': '@/orders.ts'}],
                              'index_age_s': 0.0,
                              'unresolved_total': 11,
                              'files_indexed': 1042,
                              'scanned': 0},
 ('api', 'imports', None): {'status': 'ok',
                            'target': ORDERS,
                            'source': 'index',
                            'files': [{'path': 'apps/launchpad/api/src/index.ts', 'line': 1, 'spec': '@/db/index.ts'},
                                      {'path': 'apps/launchpad/api/src/registry.ts', 'line': 2, 'spec': '@/orders/registry.ts'}],
                            'external': [],
                            'unresolved': [],
                            'index_age_s': 0.0,
                            'unresolved_total': 11,
                            'files_indexed': 1042,
                            'scanned': 0},
 ('api', 'symbols', None): {'status': 'ok',
                            'path': ORDERS,
                            'source': 'deno_doc',
                            'cached': True,
                            'total': 1,
                            'truncated': False,
                            'symbols': [{'name': 'getItems',
                                         'kind': 'function',
                                         'path': ORDERS,
                                         'line': 20,
                                         'signature': 'export const f = () => 1;',
                                         'doc': 'Does a thing.'}]}}


def reset():
    CALLS.clear()
    INSTANCES.clear()
    BEHAVIOUR.clear()


def _lib_root(scope):
    """The answer a navigator rooted in lib/ gives about who imports one of its files: the real one is blind to the apps that import the
    shared lib files, and in a checkout with oversized build output beside them its index fails (index_incomplete)."""
    return {"status": "error", "reason": "index_incomplete", "message": "part of the project could not be read, so nothing is answered from the rest",
            "scope": scope} if scope == "lib" or scope.startswith("lib/") else None


def _override(kind, method, args):
    chosen = BEHAVIOUR.get((kind, method))
    if chosen is None:
        return None
    return chosen(args) if callable(chosen) else dict(chosen)


class NavWeb:
    def __init__(self, root, tsconfig, typescript=None, node=None, server=None, timeout=20.0, guard=None):
        self.root, self.tsconfig, self.guard = root, tsconfig, guard
        self.scope = os.path.dirname(os.path.relpath(os.path.join(root, tsconfig), root))  # the package directory this navigator can see into
        self.stopped = False
        INSTANCES.append(("web", {"root": root, "tsconfig": tsconfig, "guard": guard, "self": self}))

    def stop(self):
        self.stopped = True

    def _go(self, method, **args):
        CALLS.append(("web", method, args))
        return _override("web", method, args)

    def exports(self, file, limit=200):
        out = self._go("exports", file=file, limit=limit)
        if out:
            return out
        rows = {CART: [("addToCart", "function", 5), ("view", "const", 9)], ORDER: [("OrderSchema", "const", 3), ("Order", "type", 4)]}.get(file, [])
        shown = [{"name": n, "kind": k, "line": line, "reexport": False, "defined_at": None} for n, k, line in rows]
        return {"status": "ok", "is_module": bool(rows), "exports": shown[:limit], "total": len(shown), "truncated": len(shown) > limit,
                "index_age_s": 3, "server_ms": 4}

    def imports(self, file, direction="imports", limit=25, depth=1):
        """Rows as CX2's host.mjs op_imports emits them: direction "imports" rows are {specifier, line, resolved, external} (no from, to or
        depth), sorted by line; direction "importers" rows are {from, line, specifier, to, depth}, sorted by depth, from, line."""
        out = self._go("imports", file=file, direction=direction, limit=limit, depth=depth)
        if out:
            return out
        rows, unresolved = [], 0
        if direction == "importers":
            if file == ORDER:
                blind = _lib_root(self.scope)
                if blind:
                    return blind
                rows = [{"from": CART, "line": 1, "specifier": "../../../../lib/launchpad/types/order", "to": ORDER, "depth": 1}]
                rows += [{"from": p, "line": 1, "specifier": "order", "to": ORDER, "depth": 1} for p in WEB_P]
                if self.scope not in ("", "."):
                    rows = [r for r in rows if r["from"].startswith(self.scope + "/")]  # a root sees only its own package
            elif file == CART:
                rows = [{"from": "apps/launchpad/web/src/cart.spec.ts", "line": 1, "specifier": "./cart", "to": CART, "depth": 1}]
            rows.sort(key=lambda r: (r["depth"], r["from"], r["line"]))
        elif file == CART:
            rows = [{"specifier": "../../../../lib/launchpad/types/order", "line": 1, "resolved": ORDER, "external": False}]
            unresolved = sum(1 for r in rows if r["resolved"] is None)  # host.mjs counts every row without a resolved path
            rows.sort(key=lambda r: (r["line"], r["specifier"]))
        return {"status": "ok", "direction": direction, "target": file, "edges": rows[:limit], "total": len(rows),
                "truncated": len(rows) > limit, "unresolved": unresolved, "index_age_s": 3, "server_ms": 4}

    def definition(self, file, line=None, column=None, name=None):
        out = self._go("definition", file=file, line=line, column=column, name=name)
        if out:
            return out
        if file == CART and name == "addToCart":
            row = {"path": CART, "line": 5, "column": 17, "external": False, "name": "addToCart", "kind": "function", "container": "",
                   "text": "export function addToCart(item: unknown) {"}
            return {"status": "ok", "at": {"line": line or 5, "column": 1}, "definitions": [row],
                    "signature": "function addToCart(item: unknown): ZodSafeParseResult", "doc": "Adds an item to the cart. " + "x" * 600,
                    "omitted_outside_root": 0, "index_age_s": 3, "server_ms": 4}
        return {"status": "not_found", "at": {"line": line or 1, "column": 1}, "definitions": [], "signature": "", "doc": "",
                "omitted_outside_root": 0, "index_age_s": 3, "server_ms": 4}

    def references(self, file, line=None, column=None, name=None, limit=25):
        out = self._go("references", file=file, line=line, column=column, name=name, limit=limit)
        if out:
            return out
        at = {"line": 5, "column": 17}
        if file == CART and name == "addToCart":
            rows = [{"path": p, "line": 1, "column": 10, "external": False, "text": "addToCart(x)", "is_definition": False, "is_write": False}
                    for p in WEB_P[:7]]
            return {"status": "ok", "at": at, "definitions": [{"path": CART, "line": 5, "column": 17, "external": False, "name": "addToCart",
                                                               "kind": "function"}],
                    "references": rows[:limit], "total": len(rows), "truncated": len(rows) > limit, "omitted_outside_root": 0,
                    "omitted_external": 0, "index_age_s": 3, "server_ms": 4}
        return {"status": "not_found", "at": at, "definitions": [], "references": [], "total": 0, "truncated": False, "omitted_outside_root": 0,
                "omitted_external": 0, "index_age_s": 3, "server_ms": 4}

    def stats(self):
        return {"status": "ok", "typescript": "5.4.5", "tsconfig": "apps/launchpad/web/tsconfig.json", "root_files": 12, "program_files": 40,
                "program_builds": 1, "cold_build_ms": 900, "last_build_ms": 900, "import_edges_files": 12, "rss_mb": 200, "heap_mb": 90,
                "index_age_s": 3, "server_ms": 4}


class NavApi:
    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=60,
                 check_path=None, guard=None, mirror_parent=None):
        self.project_root, self.root, self.check_path, self.guard = project_root, root, check_path, guard
        self.scope = os.path.relpath(os.path.join(project_root, root), project_root) if root else "."  # the package directory this navigator can see into
        self.stopped = False
        INSTANCES.append(("api", {"project_root": project_root, "root": root, "check_path": check_path, "guard": guard, "self": self}))

    def stop(self):
        self.stopped = True

    def _go(self, method, **args):
        CALLS.append(("api", method, args))
        return _override("api", method, args)

    def importers(self, target, limit=10):
        out = self._go("importers", target=target, limit=limit)
        if out:
            return out
        blind = _lib_root(self.scope) if target == ORDER else None
        if blind:
            return blind
        found = [{"path": r, "line": 3, "spec": "../orders.ts"} for r in ROUTES] if target == ORDERS else []
        if target == ORDER:  # the api package's own importers of the shared file
            found = [{"path": ORDERS, "line": 2, "spec": "../../../../lib/launchpad/types/order.ts"}]
        return {"status": "ok", "target": target, "total": len(found), "truncated": len(found) > limit, "importers": found[:limit],
                "index_age_s": 3, "unresolved_total": 0, "files_indexed": 40, "scanned": 0}

    def imports(self, target, source="auto"):
        out = self._go("imports", target=target, source=source)
        if out:
            return out
        if target != ORDERS:
            return {"status": "ok", "target": target, "source": "index", "files": [], "external": [], "unresolved": [], "index_age_s": 3,
                    "unresolved_total": 0, "files_indexed": 40, "scanned": 0}
        files = [{"path": ORDER, "line": 2, "spec": "../../../../lib/launchpad/types/order.ts"},
                 {"path": "apps/launchpad/api/src/util.ts", "line": 3, "spec": "./util.ts"}]
        return {"status": "ok", "target": target, "source": "index", "files": files, "external": ["zod"], "unresolved": [],
                "index_age_s": 3, "unresolved_total": 0, "files_indexed": 40, "scanned": 0}

    def symbols(self, path, limit=50):
        out = self._go("symbols", path=path, limit=limit)
        if out:
            return out
        if path == ORDERS:
            found = [{"name": "createOrder", "kind": "function", "path": ORDERS, "line": 5,
                      "signature": "export function createOrder(input: unknown) {", "doc": "Creates an order. " + "y" * 600},
                     {"name": "cancelOrder", "kind": "function", "path": ORDERS, "line": 9,
                      "signature": "export function cancelOrder(id: string) {", "doc": ""},
                     {"name": "OrderStatus", "kind": "variable", "path": ORDERS, "line": 13,
                      "signature": 'export const OrderStatus = z.enum(["open", "closed"]);', "doc": ""}]
        elif path == MANY:
            found = [{"name": f"s{n:02d}", "kind": "variable", "path": MANY, "line": n, "signature": f"export const s{n:02d} = {n};", "doc": ""}
                     for n in range(1, 31)]
        else:
            found = []
        return {"status": "ok", "path": path, "source": "deno_doc", "cached": False, "total": len(found), "truncated": len(found) > limit,
                "symbols": found[:limit]}
