"""The brief and the relation views (CX4): deterministic text built from the navigators, the note store and the files themselves.

A brief has a fixed shape so a worker can learn where to look: one block per file (facts, tests, exports, importers, imports),
then the shared contracts the files use, the notes whose scope covers them, and library hints. The same inputs give the same bytes:
every list is sorted, nothing carries a time, and each list has a cap with its overflow named in the answer's `left_out`.

Nothing here talks to a navigator directly; it asks the surface Engine, which maps every navigator answer, however it failed, to
one `Got`. A navigator that is unavailable leaves a line saying so in its place; the brief is `unavailable` only when every
navigator call it made failed, and it still carries the parts that need no navigator (facts, tests, notes, libraries).
"""
import posixpath
import re

from . import surface

EXPORTS_MAX = 20
IMPORTERS_MAX = 10
IMPORTS_MAX = 10
CONTRACTS_MAX = 5
CONSUMERS_MAX = 8
NOTES_MAX = 5
TEST_EXT = ("ts", "tsx", "js", "jsx", "mts")


def first_line(got):
    return got.text.splitlines()[0] if got.text else got.status


def edge_block(title, rows, group_by):
    """Edge lines `  from:line -> to`, grouped under [web] [api] [mc] [lib] [other] by the package of `rows[i][group_by]`."""
    groups = {}
    for row in rows:
        groups.setdefault(surface.label_for(row[group_by]), []).append(row)
    lines = [title]
    for label in surface.LABEL_ORDER:
        if label in groups:
            lines.append(f"[{label}]")
            lines += [f"  {a}:{b} -> {c}" for a, b, c in sorted(groups[label])]
    return lines


def contract_block(engine, rel, contracts, limit, left, exclude):
    """Lines for the shared (lib) files in `contracts` ({file: [places that import it]}) and who else imports each."""
    lines, failed = [], []
    ordered = sorted(contracts)
    if len(ordered) > CONTRACTS_MAX:
        left.append(f"contracts: {len(ordered) - CONTRACTS_MAX} more")
    for contract in ordered[:CONTRACTS_MAX]:
        lines.append(f"{contract} — imported at {', '.join(sorted(contracts[contract]))}")
        got = engine.importers(contract, 25)
        if not got.ok:
            lines.append(f"  consumers unavailable — {first_line(got)}")
            failed.append(got)
            continue
        consumers = sorted({(a, b) for a, b, _ in got.rows if a not in exclude})
        total = max(got.total - (len(got.rows) - len(consumers)), len(consumers))
        lines.append(f"  consumers ({total}):")
        lines += [f"  {a}:{b}" for a, b in consumers[:limit]]
        if total > limit:
            left.append(f"contract consumers of {contract}: {total - limit} more")
    return lines, failed


def relations(engine, rel, direction, limit, lines):
    """The answer of ctx_relations for the file `rel`."""
    left = []
    if direction == "importers":
        got = engine.importers(rel, limit)
        if not got.ok:
            return engine.finish(got.status, lines + [got.text], reason=first_line(got))
        shown = got.rows[:limit]
        lines += edge_block(f"importers of {rel} ({len(shown)} of {got.total}):", shown, 0) if shown else [f"nothing imports {rel}"]
        if got.total > len(shown):
            left.append(f"importers: {got.total - len(shown)} more ({got.total} total; raise limit up to 25)")
        if got.unresolved:
            lines.append(f"unresolved imports: {got.unresolved}")
        return engine.finish("ok", lines, left, age=got.age)
    got = engine.imports(rel, 25)
    if not got.ok:
        return engine.finish(got.status, lines + [got.text], reason=first_line(got))
    if direction == "imports":
        shown = got.rows[:limit]
        lines += edge_block(f"{rel} imports ({len(shown)} of {len(got.rows)}):", shown, 2) if shown else [f"{rel} imports no project file"]
        if len(got.rows) > len(shown):
            left.append(f"imports: {len(got.rows) - len(shown)} more ({len(got.rows)} total; raise limit up to 25)")
        if got.external:
            lines.append("external: " + ", ".join(sorted(got.external)))
        if got.unresolved:
            lines.append(f"unresolved imports: {got.unresolved}")
        return engine.finish("ok", lines, left, age=got.age)
    contracts = {}
    for _, line, to in got.rows:
        if surface.label_for(to) == "lib":
            contracts.setdefault(to, []).append(f"{rel}:{line}")
    if not contracts:
        return engine.finish("ok", lines + [f"{rel} imports no shared (lib) contract file"], left, age=got.age)
    block, failed = contract_block(engine, rel, contracts, limit, left, {rel})
    status = "ok" if len(failed) < len(contracts) else failed[0].status
    return engine.finish(status, lines + [f"contracts used by {rel}:"] + block, left, age=got.age)


def kind_of(rel):
    name = posixpath.basename(rel)
    if re.search(r"\.(test|spec)\.", name) or "/__tests__/" in f"/{rel}":
        return "test"
    return "source" if rel.endswith(surface.CODE_EXT) else "other"


def line_count(engine, rel):
    text = engine.read(rel)
    if text is None:
        return "unknown (over 1 MB)"
    return str(text.count("\n") + (1 if text and not text.endswith("\n") else 0))


def tests_for(engine, rel):
    folder, name = posixpath.split(rel)
    stem = name.rsplit(".", 1)[0]
    if kind_of(rel) == "test":
        return []
    found = []
    for base in (folder, posixpath.join(folder, "__tests__")):
        for suffix in ("test", "spec"):
            for ext in TEST_EXT:
                candidate = posixpath.join(base, f"{stem}.{suffix}.{ext}")
                if (engine.project / candidate).is_file():
                    found.append(candidate)
    return sorted(found)


def build(engine, paths):
    """The answer of ctx_brief for the project-relative `paths` (already checked)."""
    lines, left, tried, good = [], [], [], []
    libraries = set()
    contracts, nexts = {}, set()

    def track(section, got):
        tried.append(got)
        if got.ok:
            good.append(got)
            return True
        lines.append(f"{section}: not available — {first_line(got)}")
        nexts.update(line[6:] for line in got.text.splitlines() if line.startswith("Next: "))
        return False

    for rel in paths:
        lines.append(f"== {rel}")
        exists = (engine.project / rel).is_file()
        if not exists:
            lines.append("no such file")
            continue
        package = engine.package_for(rel)
        facts = f"kind: {kind_of(rel)} | lines: {line_count(engine, rel)} | package: "
        facts += f"{surface.label_for(rel)} ({package[1] or '.'})" if package else "none"
        lines.append(facts)
        text = engine.read(rel) or ""
        libraries |= surface.libraries_in(text)
        tests = tests_for(engine, rel)
        lines.append("tests: " + (", ".join(tests) if tests else "none found next to it"))
        if package is None:
            lines.append("no package covers this path (no deno.json or tsconfig.json above it), so there is no export or import graph for it")
            continue
        got = engine.exports(rel)
        if track("exports", got):
            lines.append(f"exports ({got.total}):")
            lines += [f"  {name} ({kind}) :{line}" for name, kind, line in got.rows[:EXPORTS_MAX]]
            if got.total > EXPORTS_MAX:
                left.append(f"exports of {rel}: {got.total - EXPORTS_MAX} more ({got.total} total)")
        got = engine.importers(rel, IMPORTERS_MAX)
        if track("importers", got):
            shown = sorted(got.rows)[:IMPORTERS_MAX]
            lines.append(f"importers ({got.total}):")
            lines += [f"  {a}:{b}" for a, b, _ in shown]
            if got.total > len(shown):
                left.append(f"importers of {rel}: {got.total - len(shown)} more ({got.total} total)")
        got = engine.imports(rel, 25)
        if track("imports", got):
            rows = sorted(got.rows, key=lambda r: (r[2], r[1]))
            lines.append(f"imports ({len(rows)}):")
            lines += [f"  {to}  (line {line})" for _, line, to in rows[:IMPORTS_MAX]]
            if len(rows) > IMPORTS_MAX:
                left.append(f"imports of {rel}: {len(rows) - IMPORTS_MAX} more ({len(rows)} total)")
            libraries |= {lib for name in got.external if (lib := surface.library_of(name))}
            for _, line, to in rows:
                if surface.label_for(to) == "lib":
                    contracts.setdefault(to, []).append(f"{rel}:{line}")

    lines.append("== contracts")
    if contracts:
        block, failed = contract_block(engine, None, contracts, CONSUMERS_MAX, left, set(paths))
        for got in failed:
            tried.append(got)
            nexts.update(line[6:] for line in got.text.splitlines() if line.startswith("Next: "))
        lines += block
    else:
        lines.append("none: the files import no shared (lib) contract")

    lines.append("== knowledge")
    result, note_lines = engine.find_notes(paths, limit=NOTES_MAX)
    lines += note_lines or ["none: no approved note covers these files"]
    if result.total > len(result.hits):
        left.append(f"notes: {result.total - len(result.hits)} more (ctx_knowledge with paths shows them)")

    lines.append("== libraries")
    if libraries:
        for lib in sorted(libraries):
            version = engine.pinned(lib)
            if version:
                lines.append(f"  {lib} {version} — use api_signature({lib}, <symbol>) for exact signatures")
            else:
                why = "no lockfile reader" if engine.pin is None else "not in the lockfile"
                lines.append(f"  {lib} (version not read: {why})")
    else:
        lines.append("  none")

    status = "ok"
    if tried and not good:
        status = tried[0].status
    if status != "ok" or any(not t.ok for t in tried):
        lines.append("Next: " + ("; ".join(sorted(nexts)) or surface.NEXT_DEFAULT["unavailable"]))
    return engine.finish(status, lines, left, [h.note.id for h in result.hits], result.index_age_s,
                         reason=None if status == "ok" else first_line(tried[0]))
