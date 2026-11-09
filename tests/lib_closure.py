"""Which first-party `lib/` modules a partial install is missing (for tests that copy a fixed list of files into a temp root).

A test that copies some `lib/` and `bin/` files into a scratch install must carry every module those files import, or the
copy fails at run time with `No module named X`, which a refactor that moves code into a new module causes without touching
the test. `missing_imports(source_lib, install)` reads the copy with `ast` and names each module that is missing and who
imports it, so the test fails at once, with a message that says what to add.

What it reads: every `*.py` in `install/lib` (and under its packages), every Python script in `install/bin`, and the
`lib/NAME.py` paths a shell script in `install/bin` names. For each, the top-level and function-level `import X` and
`from X import Y` (any import inside the file, however deep). A name counts only when `source_lib` has it as `NAME.py` or
`NAME/__init__.py`; the standard library and third-party names are ignored. Relative imports stay inside one package and
are not followed. A module present in the install is read in its installed form (a stub is read as the stub).
"""
import ast
from pathlib import Path
import re

SHELL_LIB_PATH = re.compile(r"\blib/([A-Za-z0-9_]+)\.py\b")


def is_first_party(source_lib, name):
    return (source_lib / (name + ".py")).is_file() or (source_lib / name / "__init__.py").is_file()


def installed_files(install_lib, name):
    """The installed files that make up module `name`: its `.py`, or every `.py` under its package; empty when absent."""
    if (install_lib / (name + ".py")).is_file():
        return [install_lib / (name + ".py")]
    if (install_lib / name / "__init__.py").is_file():
        return sorted((install_lib / name).rglob("*.py"))
    return []


def imported_names(path):
    """The top-level module name of every absolute import anywhere in the Python file `path`."""
    names = set()
    for node in ast.walk(ast.parse(Path(path).read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def is_python(path):
    if path.suffix == ".py":
        return True
    try:
        first = path.read_bytes().split(b"\n", 1)[0]
    except OSError:
        return False
    return first.startswith(b"#!") and b"python" in first


def missing_imports(source_lib, install, allowed=()):
    """{module name: sorted labels of the files that import it} for each first-party module the install lacks, transitively.

    `allowed` is a collection of (module, importer label) pairs the install leaves out on purpose (a command of a copied script
    that no test of the install runs). Only that pair is excused: a second importer of the same module is still reported."""
    source_lib, install = Path(source_lib), Path(install)
    install_lib = install / "lib"
    pending = []  # (file, label, is_python)
    for path in sorted(install_lib.rglob("*.py")):
        pending.append((path, path.relative_to(install_lib).with_suffix("").as_posix(), True))
    for path in sorted((install / "bin").glob("*")):
        if path.is_file():
            pending.append((path, "bin/" + path.name, is_python(path)))
    seen, missing = {path for path, _, _ in pending}, {}
    while pending:
        path, label, python = pending.pop()
        if python:
            names = imported_names(path)
        else:
            names = set(SHELL_LIB_PATH.findall(path.read_text(encoding="utf-8", errors="replace")))
        for name in sorted(names):
            if not is_first_party(source_lib, name):
                continue
            files = installed_files(install_lib, name)
            if not files and (name, label) not in allowed:
                missing.setdefault(name, set()).add(label)
            for file in files:
                if file not in seen:
                    seen.add(file)
                    pending.append((file, file.relative_to(install_lib).with_suffix("").as_posix(), True))
    return {name: sorted(who) for name, who in sorted(missing.items())}


def describe(missing):
    """One line per missing module, for an assertion message."""
    return "".join("\n  lib/%s (.py or package) is not in the install; imported by %s" % (name, ", ".join(who)) for name, who in missing.items())
