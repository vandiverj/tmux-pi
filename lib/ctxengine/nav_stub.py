"""Stand-ins for the code navigators until they are installed (CX4 codes against these; CX2 and CX3 replace them).

NavWeb has the constructor and methods of CX2's lib/ctxengine/nav_web.py (the TypeScript language-service helper for the web
packages); NavApi those of CX3's lib/ctxengine/nav_api.py (the Deno import graph for the api packages). Every method answers
`status: "unavailable"` with a `message` and a next step (`next` as CX2 names it, `next_step` as CX3 names it), so the surface can
show what is missing and what to do. Nothing here starts a process, opens a socket or reads a file.

When the real modules exist the surface loads them by name (surface.NAV_MODULES) and never reaches this file; it is the
fallback for a module that is missing or will not load. The method names and parameter lists must stay equal to the real ones:
tests/test_ctx_surface.py compares them with a fake that copies the real answer shapes."""

NEXT = "read the files directly (rg for the name, open the file); the code navigators are not installed yet"


def _unavailable(kind, why):
    message = f"the {kind} navigator is a stub: {why}"
    return {"status": "unavailable", "message": message, "next": NEXT, "next_step": NEXT}


class NavWeb:
    kind = "web"
    why = "CX2's lib/ctxengine/nav_web.py is not installed"

    def __init__(self, root, tsconfig, typescript=None, node=None, server=None, timeout=20.0, guard=None):
        self.root, self.tsconfig, self.guard = root, tsconfig, guard

    def _answer(self):
        return _unavailable(self.kind, self.why)

    def definition(self, file, line=None, column=None, name=None):
        return self._answer()

    def references(self, file, line=None, column=None, name=None, limit=25):
        return self._answer()

    def exports(self, file, limit=200):
        return self._answer()

    def imports(self, file, direction="imports", limit=25, depth=1):
        return self._answer()

    def stats(self):
        return self._answer()

    def stop(self):
        return None


class NavApi:
    kind = "api"
    why = "CX3's lib/ctxengine/nav_api.py is not installed"

    def __init__(self, project_root, root=None, config=None, overlays=(), cache_dir=None, deno="deno", timeout=60,
                 check_path=None, guard=None, mirror_parent=None):
        self.project_root, self.root, self.check_path, self.guard = project_root, root, check_path, guard

    def _answer(self):
        return _unavailable(self.kind, self.why)

    def importers(self, target, limit=10):
        return self._answer()

    def imports(self, target, source="auto"):
        return self._answer()

    def symbols(self, path, limit=50):
        return self._answer()

    def forward_graph(self, *args, **kwargs):
        return self._answer()

    def status(self):
        return self._answer()

    def stop(self):
        return None
