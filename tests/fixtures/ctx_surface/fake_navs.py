"""A fake "real" navigator module for the CX4 tests: NavWeb and NavApi with the constructors, methods and answer shapes of CX2's
lib/ctxengine/nav_web.py and CX3's lib/ctxengine/nav_api.py, answering from canned data about tests/fixtures/ctx_surface/project.

The surface loads navigators by module name (surface.NAV_MODULES); the tests point both names at this module to show that a real
navigator drops in with no change to the surface. CALLS records every method call; BEHAVIOUR replaces one answer (a refusal, a
warming helper) so the tests can see how each status is surfaced. Nothing here starts a process or opens a socket."""

CALLS = []
INSTANCES = []   # (kind, constructor arguments) of every navigator the surface built
BEHAVIOUR = {}   # {("web" | "api", method): answer dict or callable(args) -> answer dict}

CART = "apps/launchpad/web/src/cart.ts"
ORDER = "lib/launchpad/types/order.ts"
ORDERS = "apps/launchpad/api/src/orders.ts"
MANY = "apps/launchpad/api/src/many.ts"
WEB_P = [f"apps/launchpad/web/src/p{n:02d}.ts" for n in range(1, 11)]
ROUTES = [f"apps/launchpad/api/src/routes/r{n:02d}.ts" for n in range(1, 15)]


def reset():
    CALLS.clear()
    INSTANCES.clear()
    BEHAVIOUR.clear()


def _override(kind, method, args):
    chosen = BEHAVIOUR.get((kind, method))
    if chosen is None:
        return None
    return chosen(args) if callable(chosen) else dict(chosen)


class NavWeb:
    def __init__(self, root, tsconfig, typescript=None, node=None, server=None, timeout=20.0, guard=None):
        self.root, self.tsconfig, self.guard = root, tsconfig, guard
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
        return {"status": "ok", "is_module": bool(rows), "exports": shown[:limit], "total": len(shown), "truncated": len(shown) > limit}

    def imports(self, file, direction="imports", limit=25, depth=1):
        out = self._go("imports", file=file, direction=direction, limit=limit, depth=depth)
        if out:
            return out
        rows = []
        if direction == "importers" and file == ORDER:
            rows = [{"from": CART, "line": 1, "specifier": "../../../../lib/launchpad/types/order", "to": ORDER, "depth": 1}]
            rows += [{"from": ORDERS, "line": 2, "specifier": "order.ts", "to": ORDER, "depth": 1}]
            rows += [{"from": p, "line": 1, "specifier": "order", "to": ORDER, "depth": 1} for p in WEB_P]
        elif direction == "imports" and file == CART:
            rows = [{"from": CART, "line": 1, "specifier": "../../../../lib/launchpad/types/order", "to": ORDER, "depth": 1}]
        elif direction == "importers" and file == CART:
            rows = [{"from": "apps/launchpad/web/src/cart.spec.ts", "line": 1, "specifier": "./cart", "to": CART, "depth": 1}]
        rows.sort(key=lambda r: (r["depth"], r["from"], r["line"]))
        return {"status": "ok", "direction": direction, "target": file, "edges": rows[:limit], "total": len(rows),
                "truncated": len(rows) > limit, "unresolved": 0}

    def definition(self, file, line=None, column=None, name=None):
        out = self._go("definition", file=file, line=line, column=column, name=name)
        if out:
            return out
        if file == CART and name == "addToCart":
            row = {"path": CART, "line": 5, "column": 17, "name": "addToCart", "kind": "function", "container": "",
                   "text": "export function addToCart(item: unknown) {"}
            return {"status": "ok", "at": {"line": line or 5, "column": 1}, "definitions": [row],
                    "signature": "function addToCart(item: unknown): ZodSafeParseResult", "doc": "Adds an item to the cart. " + "x" * 600,
                    "omitted_outside_root": 0}
        return {"status": "not_found", "at": {"line": line or 1, "column": 1}, "definitions": [], "signature": "", "doc": "",
                "omitted_outside_root": 0}

    def references(self, file, line=None, column=None, name=None, limit=25):
        out = self._go("references", file=file, line=line, column=column, name=name, limit=limit)
        if out:
            return out
        if file == CART and name == "addToCart":
            rows = [{"path": p, "line": 1, "column": 10, "text": "addToCart(x)", "is_definition": False, "is_write": False} for p in WEB_P[:7]]
            return {"status": "ok", "at": {"line": 5, "column": 17}, "definitions": [{"path": CART, "line": 5}], "references": rows[:limit],
                    "total": len(rows), "truncated": len(rows) > limit, "omitted_outside_root": 0, "omitted_external": 0}
        return {"status": "not_found", "definitions": [], "references": [], "total": 0, "truncated": False}

    def stats(self):
        return {"status": "ok"}


class NavApi:
    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=60,
                 check_path=None, guard=None, mirror_parent=None):
        self.project_root, self.root, self.check_path, self.guard = project_root, root, check_path, guard
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
        found = [{"path": r, "line": 3, "spec": "../orders.ts"} for r in ROUTES] if target == ORDERS else []
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
