"""A small JSON Schema (draft 2020-12) subset interpreter, standard library only.

The package schema (templates/product-design/schema/package-v1.schema.json) is an ordinary JSON Schema document so other
tools can read it. This module interprets only the keywords listed in KEYWORDS and refuses a schema that uses any other
keyword, so a keyword it does not implement is never silently ignored.

Three extension keywords carry what JSON Schema cannot say and the package rules need (see docs/product-design.md):

  x-id: KIND          the string is a record id of that kind (`KIND.slug`); the id index is built from these.
  x-ref: [KIND...]    the string names an existing record of one of these kinds ("*" for any kind).
  x-dep: true         (with x-ref) the reference is a semantic dependency: it is followed for a slice fingerprint.
  x-bookkeeping: true (on a property of a record) the field is review/readiness bookkeeping, not semantic content: it is
                      left out of the record's fingerprint, so changing it never stales anything.

`validate(schema, instance)` returns (issues, ids, refs): issues are Issue tuples; ids are (path, kind, value) for every
x-id string; refs are (path, kinds, dep, value) for every x-ref string. Paths are tuples of keys and indexes.
"""
from __future__ import annotations

import re
from typing import Any, List, NamedTuple, Tuple

KEYWORDS = frozenset({
    "$schema", "$id", "$defs", "$ref", "$comment", "title", "description",
    "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "pattern", "minLength", "maxLength", "minItems", "minimum",
    "x-id", "x-ref", "x-dep", "x-bookkeeping",
})

# `<kind>.<slug>[.<slug>...]`, lower case, words joined by single hyphens, at most 96 characters in all.
SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"
ID_RE = re.compile(r"(" + SLUG + r")\.(" + SLUG + r"(?:\." + SLUG + r")*)")
ID_MAX = 96


class Issue(NamedTuple):
    code: str
    path: Tuple
    message: str


class SchemaError(ValueError):
    """The schema itself is outside the supported subset."""


def format_path(path) -> str:
    out = ""
    for part in path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(root)"


def id_kind(value: str):
    """The kind of a well-formed record id, else None."""
    if not isinstance(value, str) or len(value) > ID_MAX:
        return None
    m = ID_RE.fullmatch(value)
    return m.group(1) if m else None


def check_schema(schema: Any, where: str = "#") -> None:
    """Raise SchemaError if any (sub)schema uses a keyword outside KEYWORDS."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise SchemaError(f"{where}: a schema must be an object or boolean")
    unknown = sorted(set(schema) - KEYWORDS)
    if unknown:
        raise SchemaError(f"{where}: unsupported keyword(s) {', '.join(unknown)}")
    for name, sub in schema.get("$defs", {}).items():
        check_schema(sub, f"{where}/$defs/{name}")
    for name, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{where}/properties/{name}")
    for key in ("items", "additionalProperties"):
        if key in schema:
            check_schema(schema[key], f"{where}/{key}")


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class _Walker:
    def __init__(self, root: dict):
        self.root = root
        self.issues: List[Issue] = []
        self.ids: List[tuple] = []
        self.refs: List[tuple] = []

    def resolve(self, ref: str) -> dict:
        if not ref.startswith("#/$defs/"):
            raise SchemaError(f"unsupported $ref {ref!r}: only #/$defs/NAME")
        try:
            return self.root["$defs"][ref[len("#/$defs/"):]]
        except KeyError:
            raise SchemaError(f"unresolved $ref {ref!r}") from None

    def add(self, code, path, message):
        self.issues.append(Issue(code, tuple(path), message))

    def walk(self, schema, value, path):
        if schema is True:
            return
        if schema is False:
            self.add("schema.forbidden", path, "no value is allowed here")
            return
        if "$ref" in schema:
            self.walk(self.resolve(schema["$ref"]), value, path)
        if "type" in schema:
            types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
            if not any(_TYPES[t](value) for t in types):
                self.add("schema.type", path, f"expected {' or '.join(types)}, got {_name(value)}")
                return
        if "const" in schema and not _same(value, schema["const"]):
            self.add("schema.const", path, f"must be {schema['const']!r}")
        if "enum" in schema and not any(_same(value, option) for option in schema["enum"]):
            self.add("schema.enum", path, f"{value!r} is not one of {', '.join(map(str, schema['enum']))}")
        if isinstance(value, str):
            if "minLength" in schema and len(value) < schema["minLength"]:
                self.add("schema.min-length", path, f"shorter than {schema['minLength']} character(s)")
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                self.add("schema.max-length", path, f"longer than {schema['maxLength']} characters")
            if "pattern" in schema and not re.search(schema["pattern"], value):
                self.add("schema.pattern", path, f"does not match {schema['pattern']}")
            if "x-id" in schema:
                kind = id_kind(value)
                if kind != schema["x-id"]:
                    self.add("id.format", path, f"{value!r} is not a {schema['x-id']}.<slug> id")
                else:
                    self.ids.append((tuple(path), kind, value))
            if "x-ref" in schema:
                self.refs.append((tuple(path), tuple(schema["x-ref"]), bool(schema.get("x-dep")), value))
        if _TYPES["integer"](value) and "minimum" in schema and value < schema["minimum"]:
            self.add("schema.minimum", path, f"less than {schema['minimum']}")
        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                self.add("schema.min-items", path, f"needs at least {schema['minItems']} item(s)")
            if "items" in schema:
                for i, item in enumerate(value):
                    self.walk(schema["items"], item, path + (i,))
        if isinstance(value, dict):
            props = schema.get("properties", {})
            for name in schema.get("required", []):
                if name not in value:
                    self.add("schema.required", path + (name,), "required field is missing")
            extra = schema.get("additionalProperties", True)
            for name, item in value.items():
                if name in props:
                    self.walk(props[name], item, path + (name,))
                elif extra is False:
                    self.add("schema.additional", path + (name,), "unknown field")
                elif isinstance(extra, dict):
                    self.walk(extra, item, path + (name,))


def _same(a, b) -> bool:
    """Equal and of the same JSON type: True is not 1 and 1.0 is not 1 here (Python says they are equal)."""
    return type(a) is type(b) and a == b


def _name(value) -> str:
    for name, test in _TYPES.items():
        if test(value):
            return name
    return type(value).__name__


def validate(schema: dict, instance: Any, entry: str = None, path: Tuple = ()):
    """Validate `instance` against `schema` (or its `#/$defs/<entry>`). Returns (issues, ids, refs)."""
    walker = _Walker(schema)
    target = walker.resolve("#/$defs/" + entry) if entry else schema
    walker.walk(target, instance, tuple(path))
    return walker.issues, walker.ids, walker.refs


def bookkeeping_fields(schema: dict, entry: str) -> frozenset:
    """The top-level properties of record type `entry` marked x-bookkeeping."""
    props = schema["$defs"][entry].get("properties", {})
    return frozenset(name for name, sub in props.items() if isinstance(sub, dict) and sub.get("x-bookkeeping"))
