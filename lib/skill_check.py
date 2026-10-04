"""Strict, stdlib-only checker for new or explicitly named skill directories.

Local policy (not a vendor MUST): portable Agent Skills subset. Frontmatter is a
flat block of single-line `key: value` pairs; only name, description and license.
Values are plain scalars or simple quoted strings ('' escapes in single quotes, no
backslash escapes in double quotes); an unquoted value may not contain ': ' or ' #'.
Limitation: only files linked from SKILL.md are checked for TOC/one-hop rules, and
only link syntaxes (inline, image, reference-style definitions, href/src) are found;
bare paths in prose and unlinked files in the skill directory are not inspected.
"""
import re
from pathlib import Path
from urllib.parse import unquote

ALLOWED_KEYS = {"name", "description", "license"}
FORBIDDEN_HINT = {
    "allowed-tools": "skills grant no tool authority here",
    "hooks": "Claude hooks are provider-specific",
}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINK_RES = [
    re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)"),  # inline links and images
    re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)"),  # reference definitions
    re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", re.I),
]
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
EXTERNAL_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*:|#|//)")
REF_TOC_LINES = 100
TOC_WINDOW = 30
MAX_LEGACY_BYTES = 25000


def parse_value(val, n, errors):
    pre = f"frontmatter line {n}"
    if val[:1] in ("|", ">"):
        errors.append(f"{pre}: block scalars are not supported; use one line")
    elif val[:1] in ("[", "{", "&", "*", "!", "%", "@", "`"):
        errors.append(f"{pre}: values starting with '{val[0]}' are not supported; quote the value")
    elif val[:1] in ("\"", "'"):
        q = val[0]
        i = 1
        while i < len(val):
            if val[i] == q:
                if q == "'" and val[i + 1:i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        if i >= len(val):
            errors.append(f"{pre}: unterminated quote")
            return val[1:]
        if val[i + 1:].strip():
            errors.append(f"{pre}: unexpected text after closing quote; trailing comments are not supported")
        inner = val[1:i]
        if q == "'":
            return inner.replace("''", "'")
        if "\\" in inner:
            errors.append(f"{pre}: backslash escapes in double quotes are not supported; "
                          "use single quotes or remove the backslash")
        return inner
    elif ": " in val or val.endswith(":") or re.search(r"\s#", val):
        errors.append(f"{pre}: unquoted value contains ': ' or ' #' (invalid YAML); wrap the value in quotes")
    return val


def parse_frontmatter(text, errors):
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        errors.append("SKILL.md must begin with a '---' YAML frontmatter block")
        return {}, text
    end = next((i for i, l in enumerate(lines[1:], 1) if l.rstrip() == "---"), None)
    if end is None:
        errors.append("frontmatter is not closed with '---'")
        return {}, text
    meta = {}
    for n, line in enumerate(lines[1:end], 2):
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*?)\s*$", line)
        if not m:
            errors.append(f"frontmatter line {n}: only flat single-line 'key: value' is supported")
            continue
        key, val = m.groups()
        if key in meta:
            errors.append(f"frontmatter line {n}: duplicate key '{key}'")
        meta[key] = parse_value(val, n, errors)
    return meta, "\n".join(lines[end + 1:])


def check_meta(meta, dirname, errors):
    for key in meta:
        if key in FORBIDDEN_HINT:
            errors.append(f"frontmatter key '{key}' is not allowed ({FORBIDDEN_HINT[key]})")
        elif key not in ALLOWED_KEYS:
            errors.append(f"frontmatter key '{key}' is not in the portable subset {sorted(ALLOWED_KEYS)}")
    name = meta.get("name", "")
    if not name:
        errors.append("frontmatter 'name' is required")
    else:
        if len(name) > 64 or not NAME_RE.match(name):
            errors.append("name must be <=64 chars of lowercase letters, digits and single hyphens "
                          "(no leading/trailing/repeated hyphen)")
        if name != dirname:
            errors.append(f"name '{name}' must equal the directory name '{dirname}'")
    desc = meta.get("description", "")
    if not desc.strip():
        errors.append("frontmatter 'description' is required and non-empty")
    else:
        if len(desc) > 1024:
            errors.append(f"description is {len(desc)} chars; maximum is 1024")
        if len(desc.strip()) < 20 or re.search(r"\b(TODO|TBD|FIXME)\b|<[^>]*>", desc):
            errors.append("description must say what the skill does and when to use it "
                          "(>=20 chars, no TODO/TBD placeholders or angle brackets)")


def links_in(text):
    out, fence = [], None
    for line in text.split("\n"):
        m = FENCE_RE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            continue
        if fence is None:
            line = re.sub(r"`[^`]*`", "", line)
            for rx in LINK_RES:
                for g in rx.findall(line):
                    raw = g if isinstance(g, str) else next((x for x in g if x), "")
                    raw = raw.strip()
                    if raw.startswith("<") and raw.endswith(">"):
                        raw = raw[1:-1].strip()
                    if raw:
                        out.append(raw)
    return out


def local_targets(text):
    for raw in links_in(text):
        if EXTERNAL_RE.match(raw) and not raw.startswith("//"):
            continue
        target = unquote(raw.split("#", 1)[0].split("?", 1)[0]).replace("\\", "/")
        yield raw, target


def resolve(root, target, errors, where):
    """Return a confined, symlink-free file path or None (appending an error)."""
    if not target:
        return None
    p = Path(target)
    if p.is_absolute() or target.startswith(("//", "~")) or ".." in p.parts:
        errors.append(f"{where}: link '{target}' must be relative and stay inside the skill directory")
        return None
    cur = root
    for part in p.parts:
        cur = cur / part
        if cur.is_symlink():
            errors.append(f"{where}: link '{target}' passes through a symlink")
            return None
    if not cur.is_file():
        errors.append(f"{where}: linked file '{target}' does not exist in the skill directory")
        return None
    return cur


def has_toc(lines):
    return any(re.match(r"^#{1,6}\s+(table of contents|contents)\s*$", l.strip(), re.I)
               for l in lines[:TOC_WINDOW])


def check_skill(path, single_file=False, legacy=False):
    errors = []
    root = Path(path)
    if root.is_symlink() or not root.is_dir():
        return [f"{path}: must be a real skill directory (not a symlink)"]
    skill = root / "SKILL.md"
    if skill.is_symlink() or not skill.is_file():
        return [f"{path}: SKILL.md is missing or a symlink"]
    for p in root.rglob("*"):
        if p.is_symlink():
            errors.append(f"{p.relative_to(root)}: symlinks are not allowed in a skill directory")
    try:
        text = skill.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return errors + ["SKILL.md is not valid UTF-8"]
    if legacy:
        # Existing catalogue skills: single file, no frontmatter, packet size cap.
        if len(text.encode()) > MAX_LEGACY_BYTES:
            errors.append(f"SKILL.md exceeds {MAX_LEGACY_BYTES} bytes (catalogue inline cap)")
        single_file = True
        body = text
    else:
        meta, body = parse_frontmatter(text, errors)
        check_meta(meta, root.name, errors)
    refs = list(local_targets(body))
    if single_file and refs:
        errors.append("catalogue-only skills are single-file: the packet inlines SKILL.md alone, so "
                      "relative links cannot be followed. Inline the content, or drop --single-file/--legacy "
                      f"for a plugin-style skill. Offending links: {[r for r, _ in refs]}")
        return errors
    for raw, target in refs:
        ref = resolve(root, target, errors, "SKILL.md")
        if ref is None or ref == skill:
            continue
        if ref.suffix.lower() != ".md":
            continue
        try:
            rtext = ref.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"{ref.relative_to(root)}: not valid UTF-8")
            continue
        rlines = rtext.split("\n")
        rel = ref.relative_to(root)
        if len(rlines) > REF_TOC_LINES and not has_toc(rlines):
            errors.append(f"{rel}: {len(rlines)} lines; references over {REF_TOC_LINES} lines must "
                          f"start with a 'Contents' heading within the first {TOC_WINDOW} lines (local policy)")
        for rraw, rtarget in local_targets(rtext):
            if rtarget:
                errors.append(f"{rel}: reference files must not link to further files ('{rraw}'); "
                              "keep references one hop from SKILL.md")
    return errors
