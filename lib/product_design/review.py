"""The review build: one offline HTML file a reviewer opens from disk (docs/product-design.md, "The review build").

`build` reads a committed, trusted package and its optional prototype adapter (prototype/adapter.json with its script and
style), renders one self-contained HTML file (the runtime, the prototype and the package data inlined; no fetch, no
CDN, a Content-Security-Policy that allows only the inlined scripts and style), writes it under builds/ and records it in
the manifest through the same transaction as every other change.

The build has two fingerprints. `content_fingerprint` is the package's semantic design content (package.py): notes carry
it, and an imported note is `current` when it matches. `source_fingerprint` is everything the page shows: the embedded
data without the revision counter, the runtime and the prototype. It is stable: building an unchanged package again
gives the same source fingerprint, and the build is then reported up to date without writing anything.

The target registry is the one list of what a note may be anchored to: the runtime's view targets, every design record
id and the adapter's targets (a target with `instances` is a prefix: queue.row matches queue.row.s-1). The runtime and
`changes.ingest` use the same rule; a note on an id outside it is kept with the anchor `unresolved`.

Data is inert. Package text reaches the page only inside one JSON block whose `<`, `>`, `&`, U+2028 and U+2029 are
escaped, read with JSON.parse and rendered as text nodes; the title is HTML-escaped.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import html
import os
import re
from pathlib import Path

from . import changes as C
from . import package as P
from . import schema as S
from . import store as T

import safe_write  # noqa: E402  (store.py puts lib/ on sys.path)

FORMAT = "tp-product-review-build"
GENERATOR = "tp-product build (format 1)"
RUNTIME_DIR = P.ROOT / "templates/product-design/runtime"
ADAPTER_FILE = Path("prototype") / "adapter.json"
BUILDS_DIR = "builds"
MAX_ASSET_BYTES = 2 * 1024 * 1024
# The review views, in the runtime's order. Each is a note target (a note on a view as a whole).
VIEW_TARGETS = (("view.overview", "Overview"), ("view.journeys", "Journeys"), ("view.experience", "Product experience"),
                ("view.questions", "Questions and decisions"), ("view.engineering", "Engineering"),
                ("view.feedback", "Feedback"))
SCHEMA_ENTRIES = ("feedback", "note", "input", "review_session")  # what the page validates an import against
MANIFEST_SHOWN = ("id", "title", "owner", "objective", "activity", "next_action", "source_roots")
# Text that would end or confuse an inlined element. A script is refused, not rewritten: its bytes are what is hashed.
CLOSERS = {"script": re.compile(r"</script|<!--", re.I), "style": re.compile(r"</style|<!--", re.I)}


def _refuse(message, details=None):
    return T.StoreError("adapter.invalid", f"{message}. Nothing was written.", exit=1, details=details or {})


# ---------------------------------------------------------------- the adapter

def _asset(package_dir, rel, kind):
    """The text of one prototype or runtime file: below the package (no symlink), bounded, UTF-8, safe to inline."""
    root = Path(package_dir)
    path = root / rel
    try:
        safe_write.check_below(root, path, "prototype file")
        if path.is_symlink():
            raise OSError("is a symbolic link")
        size = path.stat().st_size
        if size > MAX_ASSET_BYTES:
            raise OSError(f"larger than {MAX_ASSET_BYTES} bytes ({size})")
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _refuse(f"{rel}: cannot be read: {getattr(exc, 'strerror', None) or exc}", {"file": str(rel)}) from None
    if CLOSERS[kind].search(text):
        raise _refuse(f"{rel} contains '</{kind}' or '<!--', which would end the inlined {kind} element", {"file": str(rel)})
    return text


def read_adapter(package_dir, docs):
    """(adapter, script text, style text), or (None, None, None) when the package has no prototype/adapter.json.
    Raises StoreError adapter.invalid when the adapter is unreadable, outside its schema, names an unknown page or
    contract, repeats an id, or its script or style cannot be inlined."""
    root = Path(package_dir)
    path = root / ADAPTER_FILE
    try:
        safe_write.check_below(root, path, "prototype adapter")
        value = P.read_file(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _refuse(f"{ADAPTER_FILE}: cannot be read: {getattr(exc, 'strerror', None) or exc}") from None
    if value is P.ABSENT:
        return None, None, None
    issues, _, _ = S.validate(P.load_schema(), value, entry="adapter")
    problems = [f"{S.format_path(i.path)}: {i.message}" for i in issues]
    if not problems:
        for coll in ("pages", "targets", "actions", "seeds"):
            ids = [r["id"] for r in value[coll]]
            problems += [f"{coll}: {i} is declared twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
        pages = {p["id"] for p in value["pages"]}
        problems += [f"targets: {t['id']} is on {t['page']}, which is not a declared page"
                     for t in value["targets"] if t["page"] not in pages]
        idx = P.index(docs)
        for a in value["actions"]:
            kind, rec = idx.get(a.get("contract"), (None, None))
            if "contract" in a and kind != "contract":
                problems.append(f"actions: {a['id']} names {a['contract']}, which is not a contract of this package")
            elif "operation" in a and "contract" not in a:
                problems.append(f"actions: {a['id']} names an operation without a contract")
            elif "operation" in a and a["operation"] not in [o["name"] for o in rec.get("operations", [])]:
                problems.append(f"actions: {a['id']}: {a['contract']} has no operation named {a['operation']!r}")
    if problems:
        raise _refuse(f"{ADAPTER_FILE} is not a valid prototype adapter: " + "; ".join(problems), {"problems": problems})
    script = _asset(root, Path("prototype") / value["script"], "script")
    style = _asset(root, Path("prototype") / value["style"], "style") if "style" in value else ""
    return value, script, style


# ---------------------------------------------------------------- the target registry

def registry(docs, adapter):
    """(exact ids, instance prefixes): what a note may be anchored to in a build of `docs` with `adapter`."""
    exact = {t for t, _ in VIEW_TARGETS}
    exact |= {rec["id"] for coll, _, _, rec in P.records(docs)
              if P.COLLECTIONS[coll][0] == "design" and isinstance(rec, dict) and isinstance(rec.get("id"), str)}
    prefixes = set()
    for t in (adapter or {}).get("targets", []):
        (prefixes if t.get("instances") else exact).add(t["id"])
    return frozenset(exact), frozenset(prefixes)


def known(reg, target_id) -> bool:
    exact, prefixes = reg
    if target_id in exact:
        return True
    return any(target_id.startswith(p + ".") and len(target_id) > len(p) + 1 for p in prefixes)


def package_registry(package_dir, docs):
    """The registry of the package as it stands, its adapter included (changes.ingest anchors notes with it)."""
    return registry(docs, read_adapter(package_dir, docs)[0])


# ---------------------------------------------------------------- the payload

def _schema_closure(schema):
    """The $defs the page needs to validate a feedback bundle: SCHEMA_ENTRIES and every def they reference."""
    defs, keep, todo = schema["$defs"], {}, list(SCHEMA_ENTRIES)
    while todo:
        name = todo.pop()
        if name in keep:
            continue
        keep[name] = defs[name]
        stack = [defs[name]]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                if isinstance(cur.get("$ref"), str):
                    todo.append(cur["$ref"].rsplit("/", 1)[1])
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)
    return {"$defs": {k: keep[k] for k in sorted(keep)}}


def warnings(docs, adapter):
    """What a reviewer will find missing in the prototype: scenario seeds, actions or targets the adapter lacks. Shown
    in the build report and on the page; not a refusal (the package may be ahead of its prototype)."""
    if adapter is None:
        return ["The package has no prototype adapter (prototype/adapter.json): the Product experience view shows no "
                "prototype, and notes on prototype targets will be unresolved."]
    seeds = {s["id"] for s in adapter["seeds"]}
    actions = {a["id"] for a in adapter["actions"]}
    reg = registry(docs, adapter)
    out = []
    for sc in docs["design"]["scenarios"]:
        if sc["seed"] not in seeds:
            out.append(f"{sc['id']} starts from seed {sc['seed']!r}, which the prototype does not declare")
        for n, step in enumerate(sc["actions"]):
            if step["action"] not in actions:
                out.append(f"{sc['id']} step {n + 1}: the prototype has no action {step['action']!r}")
            if not known(reg, step["target"]):
                out.append(f"{sc['id']} step {n + 1}: the prototype declares no target {step['target']!r}")
    return out


def _digest_texts(*texts):
    h = hashlib.sha256()
    for t in texts:
        data = t.encode("utf-8")
        h.update(len(data).to_bytes(8, "big") + data)
    return "sha256:" + h.hexdigest()


def runtime_assets():
    """(runtime script, runtime style): the files every build inlines."""
    return tuple(_asset(RUNTIME_DIR, name, kind) for name, kind in (("runtime.js", "script"), ("runtime.css", "style")))


def payload(docs, revision, adapter, script, style, runtime_js, runtime_css):
    """The data the page embeds, with build.source_fingerprint filled in. Notes, inputs and reviews are not embedded:
    the page is for capturing new feedback against this content."""
    report = P.readiness(docs)
    m = docs["manifest"]
    data = {
        "format": FORMAT, "format_version": 1,
        "package": {k: copy.deepcopy(m[k]) for k in MANIFEST_SHOWN},
        "build": {"revision": revision, "content_fingerprint": P.content_fingerprint(docs),
                  "runtime": _digest_texts(runtime_js, runtime_css),
                  "prototype": None if adapter is None else _digest_texts(P.canonical(adapter).decode("utf-8"), script, style),
                  "generator": GENERATOR},
        "design": copy.deepcopy(docs["design"]),
        "readiness": {sid: {k: r[k] for k in ("state", "effective", "stale", "blockers")} for sid, r in report.items()},
        "adapter": copy.deepcopy(adapter),
        "view_targets": [{"id": t, "label": label} for t, label in VIEW_TARGETS],
        "schema": _schema_closure(P.load_schema()),
        "limits": {"bytes": C.MAX_BUNDLE_BYTES, "records": C.MAX_BUNDLE_RECORDS, "depth": P.MAX_DEPTH},
        "warnings": warnings(docs, adapter),
    }
    unversioned = copy.deepcopy(data)
    del unversioned["build"]["revision"]  # the counter moves on every commit; what is shown does not
    data["build"]["source_fingerprint"] = P.digest(unversioned)
    return data


# ---------------------------------------------------------------- the page

def embed_json(value) -> str:
    """JSON safe inside <script type="application/json">: the characters that could end the element, start a comment
    or a character reference, or break a line terminator are \\u escapes, which JSON.parse reads back unchanged."""
    text = P.encode(value).decode("utf-8")
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        text = text.replace(ch, esc)
    return text


def _csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def render_html(data, runtime_js, runtime_css, script, style) -> bytes:
    """The page. Deterministic: the same data and assets give the same bytes."""
    prototype_js = script or "/* this package has no prototype */\n"
    css = runtime_css + "\n" + (style or "")
    csp = (f"default-src 'none'; script-src {_csp_hash(runtime_js)} {_csp_hash(prototype_js)}; "
           f"style-src {_csp_hash(css)}; base-uri 'none'; form-action 'none'")
    title = html.escape(f"{data['package']['title']} - review build, revision {data['build']['revision']}", quote=True)
    page = (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f"<meta http-equiv=\"Content-Security-Policy\" content=\"{csp}\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<meta name=\"generator\" content=\"{html.escape(GENERATOR)}\">\n"
        f"<meta name=\"tp-product-source\" content=\"{data['build']['source_fingerprint']}\">\n"
        f"<title>{title}</title>\n<style>{css}</style>\n</head>\n<body>\n"
        "<div id=\"tp-root\"><noscript>This review build needs JavaScript. It runs offline: nothing is fetched.</noscript></div>\n"
        f"<script type=\"application/json\" id=\"tp-review-data\">{embed_json(data)}</script>\n"
        f"<script>{runtime_js}</script>\n<script>{prototype_js}</script>\n</body>\n</html>\n")
    return page.encode("utf-8")


# ---------------------------------------------------------------- build

def build(package_dir, expected_revision=None, fault=T._noop) -> dict:
    """Build the review page of the committed package and record it in the manifest. Returns {committed, revision,
    file, up_to_date, reused, source_fingerprint, content_fingerprint, warnings}. Refuses (StoreError) a package that
    is not trusted or not valid, an invalid adapter (adapter.invalid) and a build file that exists with other bytes
    (build.exists, exit 3)."""
    root = Path(package_dir)
    runtime_js, runtime_css = runtime_assets()
    with T.lock(package_dir):
        state, current = C._open(package_dir, expected_revision)
        if state["issues"]:
            raise T.StoreError("package.invalid", C._issues_text(state["issues"]))
        docs = state["docs"]
        adapter, script, style = read_adapter(root, docs)
        data = payload(docs, current, adapter, script, style, runtime_js, runtime_css)
        b = data["build"]
        result = {"committed": False, "revision": current, "up_to_date": False, "reused": False,
                  "source_fingerprint": b["source_fingerprint"], "content_fingerprint": b["content_fingerprint"],
                  "warnings": data["warnings"]}
        builds = docs["manifest"].get("builds", [])
        last = builds[-1] if builds else None
        if last and last.get("source") == b["source_fingerprint"] and _regular(root, last["file"]):
            return dict(result, file=last["file"], up_to_date=True)
        page = render_html(data, runtime_js, runtime_css, script, style)
        rel = f"{BUILDS_DIR}/{docs['manifest']['id']}-r{current}.html"
        target = root / rel
        safe_write.ensure_dir(target.parent, what="builds directory", root=root)
        created = False
        if os.path.lexists(target):
            if not _regular(root, rel) or target.read_bytes() != page:
                raise T.StoreError("build.exists", f"{rel} already exists with other content; it was not replaced. Move it "
                                                   "aside and build again. Nothing was written.", exit=3,
                                   details={"file": rel})
            result["reused"] = True
        else:
            T._write_new(root, target, page)
            created = True
        work = copy.deepcopy(docs)
        entry = {"revision": current, "fingerprint": b["content_fingerprint"], "source": b["source_fingerprint"], "file": rel}
        work["manifest"].setdefault("builds", []).append(entry)
        cs = {"actor": "tp-product build", "summary": f"record review build {rel}", "evidence": [],
              "operations": [dict(entry, op="record-build")]}
        try:
            done = C._finish(package_dir, state, work, current, cs, {"accepted": []}, fault, before=docs)
        except BaseException:
            if created:  # nothing records it: a build file exists only with its manifest entry
                os.unlink(target)
            raise
        return dict(result, file=rel, committed=done["committed"], revision=done["revision"])


def _regular(root, rel) -> bool:
    path = Path(root) / rel
    try:
        safe_write.check_below(Path(root), path, "build file")
    except (OSError, ValueError):
        return False
    return path.is_file() and not path.is_symlink()
