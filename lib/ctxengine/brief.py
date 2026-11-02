"""The brief and the relation views (CX4): deterministic text built from the navigators, the note store and the files themselves.

A brief has a fixed shape so a worker can learn where to look: one block per file (facts, tests, exports, importers, imports),
then the shared contracts the files use, the notes whose scope covers them, and library hints. The same inputs give the same bytes:
every list is sorted, nothing carries a time, and each list has a cap with its overflow named in the answer's `left_out`.

An absence ("none", "no X") is worded only from a source that was read completely: every list or search carries a
`surface.Completeness` that `surface.answer_read` made from the whole answer of its read (status, every loss field, any field it does
not know), and `surface.absent` / `surface.list_head` say "not available" or "none can be shown" instead whenever it is not whole.

Nothing here talks to a navigator directly; it asks the surface Engine, which maps every navigator answer, however it failed, to
one `Got`. A navigator that is unavailable leaves a line saying so in its place; the brief is `unavailable` only when every
navigator call it made failed, and it still carries the parts that need no navigator (facts, tests, notes, libraries).
"""
import os
import posixpath
import re

from . import surface

EXPORTS_MAX = 20
IMPORTERS_MAX = 10
IMPORTS_MAX = 10
CONTRACTS_MAX = 5
CONSUMERS_MAX = 8
NOTES_MAX = 5
SCOPES_MAX = 10  # most packages asked who imports a shared lib file
TEST_EXT = tuple(ext.lstrip(".") for ext in surface.CODE_EXT)  # a test is looked for in every code extension the surface reads
USE_FILE_MAX = 200_000
SOURCE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
IMPORT_STMT = re.compile(r"""\bimport\s+(type\s+)?([^;'"]{0,600}?)\s*from\s*(['"])([^'"\n]{1,300})\3""", re.S)
REEXPORT_STMT = re.compile(r"""\bexport\s+(?:type\s+)?(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s*from\s*(['"])([^'"\n]{1,300})\1""")
WRITES = ("extend", "merge", "pick", "omit", "partial", "required", "deepPartial", "and", "or", "transform", "refine", "superRefine", "brand",
          "default", "catch", "keyof", "shape")
READS = ("parse", "safeParse", "parseAsync", "safeParseAsync", "spa")


def first_line(got):
    return got.text.splitlines()[0] if got.text else got.status


more_note = surface.more_note  # one wording for rows a navigator list did not give, shared with the symbol view


def withheld_note(label, refused):
    return f"{label}: {refused} withheld (a path that is refused or outside the project)" if refused else None


def add_left(left, *notes):
    left += [n for n in notes if n]


def strip_ext(path):
    base, ext = posixpath.splitext(path)
    return base if ext in SOURCE_EXT else path


def _names(clause):
    """(local names, namespace names, type-only) bound by the part of an import between `import` and `from`."""
    names, spaces, typed = [], [], True
    clause = clause.strip()
    head, _, block = clause.partition("{")
    for part in head.split(","):
        part = part.strip()
        if part.startswith("*"):
            spaces.append(part.split()[-1])
            typed = False
        elif part:
            names.append(part)
            typed = False
    if block:
        for part in block.split("}")[0].split(","):
            words = part.split()
            if not words:
                continue
            is_type = words[0] == "type" and len(words) > 1
            if is_type:
                words = words[1:]
            names.append(words[-1])
            typed = typed and is_type
    return names, spaces, typed


def read_phrase(got):
    """What was read of an imports list that is not whole: a cut list gives no total, only what was read."""
    read = got.completeness()
    if read.cut:
        return f"only {len(got.rows)} imports were read, the navigator cut its list"
    if got.beyond:
        return f"only {len(got.rows)} of {got.total} imports were read"
    return f"not all imports were returned: {read.left} withheld or unread, {read.why}"


def not_whole_why(got):
    """Why the imports of a briefed file were not read completely, from the record of that read (N1: "cut" only when the navigator
    flagged its list as cut)."""
    read = got.completeness()
    if not read.ok:
        return "not available"
    if read.cut:
        return "cut"
    return f"not all returned: {read.left} withheld or unread, {read.why}"


def contract_use(engine, contract, consumer):
    """How `consumer` uses the contract file `contract`: `writer` (derives from it with extend/pick/omit/..., or re-exports it),
    `reader` (parses with it or takes its type), both, `uses` (imports it and does neither), `unread` (the file cannot be read).
    A text scan of the consumer's own import statements; it names no file the navigator did not."""
    text = engine.read(consumer, USE_FILE_MAX)
    if text is None:
        return "unread"
    want, here = strip_ext(contract), posixpath.dirname(consumer)

    def is_contract(spec):
        if not spec.startswith("."):
            return posixpath.basename(strip_ext(spec)) == posixpath.basename(want) and strip_ext(spec).endswith(posixpath.basename(want))
        return posixpath.normpath(posixpath.join(here, strip_ext(spec))) == want

    reads = writes = False
    body = IMPORT_STMT.sub("", text)  # the use sites, without the import lines that bind the names
    for match in IMPORT_STMT.finditer(text):
        if not is_contract(match.group(4)):
            continue
        names, spaces, typed = _names(match.group(2))
        if match.group(1) or typed:
            reads = True
        refs = [re.escape(n) for n in names] + [re.escape(n) + r"\.\w+" for n in spaces]
        for ref in refs:
            if re.search(rf"\b{ref}\s*\.\s*(?:{'|'.join(WRITES)})\b", text):
                writes = True
            if re.search(rf"\b{ref}\s*\.\s*(?:{'|'.join(READS)})\s*\(", text) or re.search(rf"\btypeof\s+{ref}\b", text):
                reads = True
            if re.search(rf"[:<,|&]\s*{ref}\b|\b(?:as|satisfies|extends|implements)\s+{ref}\b", body):  # a type annotation takes its type
                reads = True
    if any(is_contract(m.group(2)) for m in REEXPORT_STMT.finditer(text)):
        writes = True
    return "reader, writer" if reads and writes else "writer" if writes else "reader" if reads else "uses"


def edge_block(title, rows, group_by, mark=None):
    """Edge lines `  from:line -> to`, grouped under [web] [api] [mc] [lib] [other] by the package of `rows[i][group_by]`;
    `mark(row)` adds ` [..]` to a line."""
    groups = {}
    for row in rows:
        groups.setdefault(surface.label_for(row[group_by]), []).append(row)
    lines = [title]
    for label in surface.LABEL_ORDER:
        if label in groups:
            lines.append(f"[{label}]")
            lines += [f"  {a}:{b} -> {c}" + (f" [{mark((a, b, c))}]" if mark else "") for a, b, c in sorted(groups[label])]
    return lines


def scope_text(package):
    """How a package is named in an answer: `api package apps/launchpad/api`."""
    kind, where = package
    return f"{kind} package {where or '.'}"


def importer_reads(engine, rel, limit, via=(), left=None):
    """[(scope, Got)]: who imports `rel`. A file outside lib/ is asked of the navigator of its own package (scope ""). A shared lib file is
    asked of the navigators of the packages that import it (G1): those of the files in `via`, else every app package. Its own package
    cannot see the apps (a lib root has no consumers there, and an index of it can fail on the build output beside it), so the answer
    names the scope it covers. With no app package at all the lib file's own package is asked, as for any file. Packages past SCOPES_MAX are
    not asked; they are named in `left` (when given) so the answer does not read as if they had none."""
    if surface.label_for(rel) != "lib":
        return [("", engine.importers(rel, limit))]
    packages = []
    for package in [engine.package_for(v) for v in via] if via else engine.app_packages():
        if package and package not in packages:
            packages.append(package)
    if not packages:
        return [("", engine.importers(rel, limit))]
    packages.sort(key=lambda package: (surface.LABEL_ORDER.index(package[0]) if package[0] in surface.LABEL_ORDER else len(surface.LABEL_ORDER), package[1]))
    if left is not None and len(packages) > SCOPES_MAX:
        left.append(f"importers of {rel}: not asked in {', '.join(scope_text(p) for p in packages[SCOPES_MAX:])} (more than {SCOPES_MAX} packages)")
    return [(scope_text(package), engine.importers(rel, limit, package=package)) for package in packages[:SCOPES_MAX]]


def contract_block(engine, rel, contracts, limit, left, exclude):
    """Lines for the shared (lib) files in `contracts` ({file: [places that import it]}) and who else imports each, every consumer
    marked reader, writer, both or uses."""
    lines, failed = [], []
    ordered = sorted(contracts)
    if len(ordered) > CONTRACTS_MAX:
        left.append(f"contracts: {len(ordered) - CONTRACTS_MAX} more")
    for contract in ordered[:CONTRACTS_MAX]:
        lines.append(f"{contract} — imported at {', '.join(sorted(contracts[contract]))}")
        reads = importer_reads(engine, contract, 25, [place.rsplit(":", 1)[0] for place in contracts[contract]], left)
        if all(not got.ok for _, got in reads):
            failed.append(reads[0][1])
        for scope, got in reads:
            tag = f" in {scope}" if scope else ""
            if not got.ok:
                lines.append(f"  consumers{tag} unavailable — {first_line(got)}")
                continue
            consumers = sorted({(a, b) for a, b, _ in got.rows if a not in exclude})
            total = max(got.total - got.refused - (len(got.rows) - len(consumers)), len(consumers))
            lines.append("  " + surface.list_head(f"consumers{tag}", len(consumers[:limit]), total, got.completeness()))
            lines += [f"  {a}:{b} [{contract_use(engine, contract, a)}]" for a, b in consumers[:limit]]
            add_left(left, withheld_note(f"consumers of {contract}{tag}", got.refused),
                     more_note(f"contract consumers of {contract}{tag}", total - limit, got.cut, "ctx_relations importers shows more"))
    return lines, failed


def relations(engine, rel, direction, limit, lines):
    """The answer of ctx_relations for the file `rel`."""
    left = []
    if direction == "importers":
        reads = importer_reads(engine, rel, limit, left=left)
        if all(not got.ok for _, got in reads):
            got = reads[0][1]
            return engine.finish(got.status, lines + [got.text], reason=first_line(got))
        mark = (lambda row: contract_use(engine, rel, row[0])) if surface.label_for(rel) == "lib" else None
        age = None
        for scope, got in reads:
            tag = f" in {scope}" if scope else ""
            if not got.ok:
                lines.append(f"importers of {rel}{tag}: not available — {first_line(got)}")
                continue
            age = got.age if age is None else age
            shown = got.rows[:limit]
            lines += edge_block(surface.list_head(f"importers of {rel}{tag}", len(shown), got.total, got.completeness(),
                                                  empty=f"nothing imports {rel}{tag}", of=True), shown, 0, mark)
            add_left(left, more_note(f"importers{tag}", got.total - len(shown) - got.refused, got.cut, "raise limit up to 25", got.total),
                     withheld_note(f"importers{tag}", got.refused))
            if got.unresolved:
                lines.append(f"unresolved imports{tag}: {got.unresolved}")
        return engine.finish("ok", lines, left, age=age)
    got = engine.imports(rel)
    if not got.ok:
        return engine.finish(got.status, lines + [got.text], reason=first_line(got))
    if direction == "imports":
        shown = got.rows[:limit]
        lines += edge_block(surface.list_head(f"{rel} imports", len(shown), got.total, got.completeness(), empty=f"{rel} imports no project file", of=True),
                            shown, 2)
        add_left(left, more_note("imports", got.total - len(shown) - got.refused, got.cut, "raise limit up to 25", got.total),
                 withheld_note("imports", got.refused))
        if got.external:
            lines.append("external: " + ", ".join(sorted(got.external)))
        if got.unresolved:
            lines.append(f"unresolved imports: {got.unresolved}")
        return engine.finish("ok", lines, left, age=got.age)
    contracts = {}
    for _, line, to in got.rows:
        if surface.label_for(to) == "lib":
            contracts.setdefault(to, []).append(f"{rel}:{line}")
    if got.incomplete:  # the shared contracts may be among the imports that were not read
        add_left(left, more_note("imports not read", got.beyond, got.cut, "shared contracts among them are missing from this answer")
                 or f"contracts: imports of {rel} not read completely ({got.completeness().why}); shared contracts among them may be missing")
        lines.append(f"warning: {read_phrase(got)}, so the contracts below may be incomplete")
    add_left(left, withheld_note("imports", got.refused))
    if not contracts:
        return engine.finish("ok", lines + [surface.list_head("shared (lib) contracts", 0, 0, got.completeness(),
                                                              empty=f"{rel} imports no shared (lib) contract file")], left, age=got.age)
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
    """(test files next to `rel`, Completeness): the record's `withheld` counts matching test files left out because their path is refused.
    Each goes through Engine.allowed."""
    folder, name = posixpath.split(rel)
    stem = name.rsplit(".", 1)[0]
    if kind_of(rel) == "test":
        return [], surface.answer_read({"status": "ok"})
    found, refused = [], 0
    for base in (folder, posixpath.join(folder, "__tests__")):
        for suffix in ("test", "spec"):
            for ext in TEST_EXT:
                candidate = posixpath.join(base, f"{stem}.{suffix}.{ext}")
                if os.path.lexists(engine.project / candidate):
                    allowed = engine.allowed(candidate)
                    if allowed is None:
                        refused += 1
                    elif (engine.project / allowed).is_file():
                        found.append(allowed)
    return sorted(found), surface.answer_read({"status": "ok", "withheld": refused})


def build(engine, paths):
    """The answer of ctx_brief for the project-relative `paths` (already checked)."""
    lines, left, tried, good = [], [], [], []
    libraries = {}  # library name -> the package directories of the briefed files that import it (where its lockfile and aliases are read)
    contracts, nexts, missing, incomplete = {}, set(), [], []
    unread = []  # briefed files whose text could not be read for the library scan
    import_reads = []  # the Completeness of each briefed file's imports read: "no contract" is said only when all are whole
    not_whole = {}  # briefed file -> why its imports were not read completely (a contract may be among the rest)

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
            missing.append(rel)
            continue
        package = engine.package_for(rel)
        facts = f"kind: {kind_of(rel)} | lines: {line_count(engine, rel)} | package: "
        facts += f"{surface.label_for(rel)} ({package[1] or '.'})" if package else "none"
        lines.append(facts)
        text = engine.read(rel)
        if text is None or surface.unnamed_imports(text):
            unread.append(rel)  # over 1 MB, unreadable, or an import it cannot name: "no library" cannot be said
        here = package[1] if package else ""
        for lib in surface.libraries_in(text or ""):
            libraries.setdefault(lib, []).append(here)
        tests, tests_read = tests_for(engine, rel)
        lines.append("tests: " + (", ".join(tests) if tests else surface.absent("none found next to it", tests_read)))
        add_left(left, withheld_note(f"tests of {rel}", tests_read.withheld))
        if package is None:
            lines.append("no package covers this path (no deno.json or tsconfig.json above it), so there is no export or import graph for it")
            import_reads.append(surface.answer_read(None))  # the imports of this file were never read
            not_whole[rel] = "outside any package"
            continue
        got = engine.exports(rel)
        if track("exports", got):
            lines.append(surface.list_head("exports", len(got.rows[:EXPORTS_MAX]), got.total, got.completeness()))
            lines += [f"  {name} ({kind})" + (f" :{line}" if line else "") for name, kind, line in got.rows[:EXPORTS_MAX]]
            add_left(left, more_note(f"exports of {rel}", got.total - EXPORTS_MAX, got.cut, "ctx_symbol with path shows one name", got.total))
            add_left(left, withheld_note(f"exports of {rel}", got.refused))
        mark = (lambda row, rel=rel: contract_use(engine, rel, row[0])) if surface.label_for(rel) == "lib" else None
        for scope, got in importer_reads(engine, rel, IMPORTERS_MAX, left=left):
            tag = f" in {scope}" if scope else ""
            if track(f"importers{tag}", got):
                shown = sorted(got.rows)[:IMPORTERS_MAX]
                lines.append(surface.list_head(f"importers{tag}", len(shown), got.total, got.completeness()))
                lines += [f"  {a}:{b}" + (f" [{mark((a, b, rel))}]" if mark else "") for a, b, _ in shown]
                add_left(left, more_note(f"importers of {rel}{tag}", got.total - len(shown) - got.refused, got.cut, "ctx_relations importers shows more", got.total),
                         withheld_note(f"importers of {rel}{tag}", got.refused))
        got = engine.imports(rel)
        import_reads.append(got.completeness())
        if not got.completeness().whole:
            not_whole[rel] = not_whole_why(got)
        if track("imports", got):
            rows = sorted(got.rows, key=lambda r: (r[2], r[1]))
            lines.append(surface.list_head("imports", len(rows[:IMPORTS_MAX]), got.total - got.refused, got.completeness()))
            lines += [f"  {to}  (line {line})" for _, line, to in rows[:IMPORTS_MAX]]
            add_left(left, more_note(f"imports of {rel}", got.total - min(len(rows), IMPORTS_MAX) - got.refused, got.cut, "ctx_relations imports shows more", got.total),
                     withheld_note(f"imports of {rel}", got.refused))
            if got.incomplete:
                incomplete.append(rel)
                lines.append(f"  ({read_phrase(got)})")
            for lib in (surface.library_of(name) for name in got.external):
                if lib:
                    libraries.setdefault(lib, []).append(package[1])
            for _, line, to in rows:
                if surface.label_for(to) == "lib":
                    contracts.setdefault(to, []).append(f"{rel}:{line}")

    if len(missing) == len(paths):  # nothing to brief on: a bad argument, not a brief
        lines.append("Next: check the spelling of the path(s) and call ctx_brief again; ctx_symbol finds a file by the name it defines.")
        return engine.finish("invalid", lines, left, reason="no such file: " + ", ".join(missing))

    lines.append("== contracts")
    if contracts:
        block, failed = contract_block(engine, None, contracts, CONSUMERS_MAX, left, set(paths))
        for got in failed:
            tried.append(got)
            nexts.update(line[6:] for line in got.text.splitlines() if line.startswith("Next: "))
        lines += block
    else:
        read = surface.Completeness.of(*import_reads)
        lines.append(surface.list_head("shared (lib) contracts", 0, 0, read, empty="none: the files import no shared (lib) contract"))
    if not_whole:
        named = ", ".join(f"{rel} ({why})" for rel, why in sorted(not_whole.items()))
        lines.append(f"warning: the imports of {named} were not read completely, so shared contracts may be missing here")
        left.append(f"contracts: imports not read completely for {', '.join(sorted(not_whole))}; shared contracts among them may be missing")

    lines.append("== knowledge")
    result, note_lines, cut = engine.find_notes(paths, limit=NOTES_MAX, body_limit=surface.NOTE_BODY_BRIEF)
    lines += note_lines or [surface.absent("none: no approved note covers these files", surface.notes_read(result))]
    add_left(left, surface.notes_left(result))
    if result.total > len(result.hits):
        left.append(f"notes: {result.total - len(result.hits)} more (ctx_knowledge with paths shows them)")
    if cut:
        left.append(f"note text cut at {surface.NOTE_BODY_BRIEF} characters (ctx_knowledge shows it whole): {', '.join(cut)}")

    lines.append("== libraries")
    if libraries:
        for lib in sorted(libraries):
            wheres = list(dict.fromkeys(libraries[lib]))
            if any(engine.alias_of(lib, where) for where in wheres):
                lines.append(f"  {lib} (a project alias for code in this repository, not a library)")
                continue
            for where in wheres:
                version, why = engine.pinned(lib, where)
                if version:
                    break
            if version:
                lines.append(f"  {lib} {version} — use api_signature({lib}, <symbol>) for exact signatures")
            else:
                lines.append(f"  {lib} (version not read: {why})")
    else:
        lines.append("  " + surface.absent("none", surface.answer_read({"status": "ok", "libraries_unread": len(unread) + len(incomplete)})))

    status = "ok"
    if tried and not good:
        status = tried[0].status
    if missing:
        lines.append("Next: no such file: " + ", ".join(missing) + "; check the spelling and ask again for them.")
    if status != "ok" or any(not t.ok for t in tried):
        lines.append("Next: " + ("; ".join(sorted(nexts)) or surface.NEXT_DEFAULT["unavailable"]))
    return engine.finish(status, lines, left, [h.note.id for h in result.hits], result.index_age_s,
                         reason=None if status == "ok" else first_line(tried[0]))
