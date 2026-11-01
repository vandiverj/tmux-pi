"""api_signature(package, symbol): the declaration of `symbol` in a pinned package's own type declarations.

Offline and read-only. The package is found in the project's node_modules, or (for `npm:` specifiers and for a
package only deno.lock pins) in Deno's npm cache. Its entry declaration file comes from package.json `types` /
`typings` / `exports["."]` / `main`; relative re-exports are followed inside the package. Every read goes through
guard.py (allowlisted extensions, links resolved, nothing outside the package directory, never .env*).

`jsr:@scope/name` and `https://` specifiers are read from Deno's remote cache (remote.py), pinned by deno.lock. Those files
may be TypeScript source; declaration_view() then returns only exported declarations, with bodies, initializers, decorator arguments and code in
parameter defaults, heritage clauses and enum initializers cut (see Known limits). jsr files are checked against deno.lock's integrity when it
records one (remote.py).

Every text taken from a source file leaves this module through one gate, _gate(), which find_declarations, find_members and declaration_view
apply to what they return: an allow-list of declaration shapes (shape.py). A text that is not only declaration words, names, types, literal
defaults and omission markers is replaced by its name and `/* declaration omitted */`, or by the marker alone. The per-site cuts keep the output
useful; the gate is what guarantees that no code is returned when the scanner or a cut gets a shape wrong.

The declaration scanner is a text scanner, not a TypeScript parser: it splits a declaration file into top-level
statements (comments, strings, regex literals and brackets skipped) and matches the name. It returns the statement with the
JSDoc comment just above it. `Ns.member` finds a member of a namespace, class, interface or enum.

Known limits (TypeScript source files only; .d.ts files carry no code and are returned as written, without the gate):
- The gate is a recogniser, not a parser: a declaration it does not know (an unusual type form, a class member not ended by `;`,
  a member named by a statement word without full types, a non-ASCII name) is withheld although it holds no code. Comments are
  documentation and pass it as written; an unclosed one does not.
- A class or namespace header that is not type-shaped keeps its name and type parameters and shows `/* heritage omitted */`; when the
  clause opens a block of its own (`extends class {`, `extends function () {`) the whole declaration is withheld. Enum members keep only
  string and number initializers. An initializer is shown as a function only when it starts with an arrow head or `function`.
- A parameter list is shown only up to its first default that is not a plain literal; the rest of the list is withheld as
  `/* defaults omitted */)`, then the declared return type. The scanner never tries to find where such a default ends.
  The same happens for an `=` that is not a default, such as a type parameter default in a callback type
  (`cb: <T = string>(x: T) => void`). Less is shown, never code.
- What follows the cut is shown only if it passes an allow-list (empty, or `: <type>` made of type words, `<>`, `[]`, `|`, `&`,
  `,`, `?`, grouping parens and `{ name: type, ... }`). A return type with a quote, a `=>` (a function type), a `;` or `(` in
  braces, or a statement word is replaced by `: /* type omitted */`; the scanner may also have misread the list.
- Decorator arguments are cut everywhere in a declaration (`@Inject(/* args omitted */)`). A decorator the scanner cannot read
  (type arguments, `@(expr)`, a second call, arguments that do not close) withholds the declaration or member, so does a
  stray `@` outside strings and comments in a body. A template literal nested more than 24 levels ends the scan of the file.
- A regex literal right after `)` on a line (`if (a) /'/.test(a); }`) is read as division, so a quote in it opens a string that
  runs to the end of the line and can swallow a closing `}`; later exports in that file may then report "not found". No code is
  returned for it.
"""
import json
import os
import re
from pathlib import Path

from . import guard, lock, remote, shape

NAME_RE = re.compile(r"^(@[A-Za-z0-9][A-Za-z0-9~._-]*/)?[A-Za-z0-9][A-Za-z0-9~._-]*$")
SYMBOL_RE = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")
MAX_FILES = 40
MAX_NEST = 8
KEYWORDS = ("export", "declare", "import", "interface", "type", "function", "class", "const", "let", "var", "enum",
            "namespace", "abstract", "module", "global")
CONTAINERS = ("interface", "class", "enum", "namespace", "module", "global")
CONTAINER_HEAD = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?(?:%s)\b" % "|".join(CONTAINERS))
DECL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:const\s+)?(?:async\s+)?"
    r"(?P<kind>function|class|interface|type|enum|namespace|module|const|let|var)(?:\s*\*\s*|\s+)(?P<name>[A-Za-z_$][\w$]*)")
DECORATOR_PREFIX = r"(?:@[A-Za-z_$][\w$.]*(?:\(/\* args omitted \*/\))?\s*)*"  # decorators as _cut_decorators leaves them
MEMBER = re.compile(
    r"^" + DECORATOR_PREFIX + r"(?:(?:public|protected|private|static|readonly|abstract|declare|async|override|export)\s+)*"
    r"(?:(?:get|set)\s+(?=[A-Za-z_$]))?(?P<name>[A-Za-z_$][\w$]*|'[^']+'|\"[^\"]+\")\s*[?!]?\s*(?:[(<:=;,]|$)")
OPEN_ENDED = ("=", "|", "&", ",", ":", "<", "(", "?", "{", "[")
OPEN_WORDS = re.compile(r"\b(?:extends|implements|keyof|typeof|in|is|as|infer|readonly|new|unique)$")
LINE_CONTINUES = re.compile(r"[|&?:=.)\]>,{]|extends\b|implements\b|\bin\b|=>")


class Miss(Exception):
    """A valid request with no answer (not found, no pin, not installed). The message is safe to show the model."""

    def __init__(self, message, version=None, no_types=False):
        super().__init__(message)
        self.version = version
        self.no_types = no_types


class Found:
    def __init__(self, text, version, package, files, provenance=None):
        self.text, self.version, self.package, self.files, self.provenance = text, version, package, files, provenance


# ---- statement scanner ---------------------------------------------------------------------------------------------

MAX_TEMPLATE_NEST = 24


def _skip_string(text, i, _nest=0):
    """End of the string that starts at `i`. A ' or " string ends at its line: a quote that opens no string (one a scanner
    misread) then costs one line and not the rest of the file. A template literal may span lines, and its `${ ... }` holds an
    expression that can hold strings and templates of its own. Nesting deeper than MAX_TEMPLATE_NEST ends the text."""
    quote, i = text[i], i + 1
    while i < len(text) and text[i] != quote:
        c = text[i]
        if c == "\n" and quote != "`":
            return i
        if c == "$" and quote == "`" and text.startswith("${", i):
            i = _skip_template_expression(text, i + 2, _nest + 1)
            continue
        i += 2 if c == "\\" else 1
    return i + 1


def _skip_template_expression(text, i, nest):
    """Index after the `}` that closes a `${` whose expression starts at `i`."""
    n = len(text)
    if nest > MAX_TEMPLATE_NEST:
        return n
    depth = 1
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in "'\"`":
            i = _skip_string(text, i, nest)
            continue
        if c == "/":
            end = _skip_regex(text, i)
            if end >= 0:
                i = end
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


REGEX_AFTER = "(,=:[!&|?{};+-*%<>~^"
REGEX_KEYWORD = re.compile(r"(?:^|[^\w$.])(?:return|typeof|case|in|of|delete|void|throw|new|else|do|yield|await|instanceof)$")


def _skip_regex(text, i):
    """End of the regular-expression literal that starts at `i` (a `/` after an operator or keyword and closed on the same
    line), else -1. Division, a comment or an unclosed `/` is not one; a quote inside a regex must not open a string."""
    if text[i + 1:i + 2] in ("/", "*", ""):
        return -1
    j = i - 1
    while j >= 0 and text[j] in " \t\r\n":
        j -= 1
    if j >= 0 and text[j] not in REGEX_AFTER and not REGEX_KEYWORD.search(text[max(0, j - 12):j + 1]):
        return -1
    k, in_class = i + 1, False
    while k < len(text) and text[k] != "\n":
        c = text[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            k += 1
            while k < len(text) and (text[k].isalnum() or text[k] in "_$"):
                k += 1
            return k
        k += 1
    return -1


def _newline_ends(text, start, i, members):
    """A line break ends a statement when the line is complete and the next one begins something new."""
    before = text[start:i].rstrip()
    if not before or before.endswith(OPEN_ENDED) or OPEN_WORDS.search(before):
        return False
    after = text[i + 1:i + 80].lstrip()
    if members:
        return bool(after) and not LINE_CONTINUES.match(after)
    return after.startswith(("/**", "/*", "//", "@")) or bool(re.match(r"(?:%s)\b" % "|".join(KEYWORDS), after))


def split_statements(text, members=False):
    """[(start, end, doc)] for the statements at the top level of `text`; `text[start:end]` is the statement and `doc`
    the (start, end) of the JSDoc comment right above it, or None. `members` splits a container body: a `,` or a
    complete line also ends a member."""
    out = []
    n = len(text)
    i = 0
    start = doc = pending = None
    depth = angle = 0

    def finish(end):
        nonlocal start, doc, depth, angle
        if start is not None and text[start:end].strip():
            out.append((start, end, doc))
        start = doc = None
        depth = angle = 0

    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            if start is None:
                pending = (i, end) if text.startswith("/**", i) and not text.startswith("/**/", i) else None
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if start is None:
            if c.isspace():
                i += 1
                continue
            if c in ";,":
                pending = None
                i += 1
                continue
            start, doc, pending = i, pending, None
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if c == "/":
            end = _skip_regex(text, i)
            if end >= 0:
                i = end
                continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if c == "}" and depth <= 0 and angle <= 0 and CONTAINER_HEAD.match(text[start:i].lstrip()):
                end = i + 1
                tail = re.match(r"[ \t]*;", text[end:end + 40])
                finish(end + tail.end() if tail else end)
                i = max(i + 1, end)
                continue
        elif c == "<" and depth <= 0:
            angle += 1
        elif c == ">" and depth <= 0 and angle > 0 and text[i - 1] != "=":
            angle -= 1
        elif depth <= 0 and (c == ";" or (members and c == "," and angle <= 0)):
            finish(i + 1)
        elif c == "\n" and depth <= 0 and angle <= 0 and _newline_ends(text, start, i, members):
            finish(i)
        i += 1
    finish(n)
    return out


def _with_doc(text, doc, statement):
    return (text[doc[0]:doc[1]] + "\n" if doc else "") + statement


def _scan(text):
    """(index, char, parens, angles, braces) for each character outside strings and comments, counted before the character."""
    paren = angle = brace = 0
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if c in "'\"`":
            i = _skip_string(text, i)
            continue
        if c == "/":
            end = _skip_regex(text, i)
            if end >= 0:
                i = end
                continue
        yield i, c, paren, angle, brace
        if c in "([":
            paren += 1
        elif c in ")]":
            paren -= 1
        elif c == "{":
            brace += 1
        elif c == "}":
            brace -= 1
        elif c == "<" and paren <= 0:
            angle += 1
        elif c == ">" and paren <= 0 and angle > 0 and text[i - 1] != "=":
            angle -= 1
        i += 1


def _body_open(statement):
    """Index of the `{` that opens a container's body: the first one outside (), [] and <>, strings and comments. A `{` in
    the header, such as `<T extends { id: string }>` or `extends Base<{ a: 1 }>`, is a type and not the body."""
    for i, c, paren, angle, brace in _scan(statement):
        if c == "{" and paren <= 0 and angle <= 0 and brace <= 0:
            return i
    return -1


def _body_span(statement):
    """(open, close) of a container's body: the `{` _body_open finds and the `}` that closes it, which must be the statement's
    last one. None otherwise: a `{` in the header that is not in brackets (`extends class { ... } { ... }`) was taken for the
    body, and what follows it is not the container's members."""
    open_at, close_at = _body_open(statement), statement.rfind("}")
    if not 0 <= open_at < close_at:
        return None
    match = next((i for i, c, _, _, brace in _scan(statement) if i > open_at and c == "}" and brace == 1), None)
    return (open_at, close_at) if match == close_at else None


def _body(statement):
    span = _body_span(statement)
    return statement[span[0] + 1:span[1]] if span else ""


# ---- source files: exported declarations without code --------------------------------------------------------------

PRIVATE_MEMBER = re.compile(r"^" + DECORATOR_PREFIX + r"(?:(?:public|protected|static|readonly|abstract|declare|async|override|export)\s+)*(?:private\b|#)")
LITERAL = re.compile(r"""^(?:"[^"\n]{0,80}"|'[^'\n]{0,80}'|-?\d[\w.]{0,30}|true|false|null)$""")
KEEP_WHOLE = ("interface", "type", "enum")
BLOCKS = ("class", "namespace", "module")
OMITTED = shape.BODY
INIT_OMITTED = shape.INIT
HERITAGE_OMITTED = shape.HERITAGE


DECORATOR_ARGS = shape.ARGS
DECORATOR_NAME = re.compile(r"@\s*([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)")
AFTER_DECORATOR = re.compile(r"(?:\s|/\*.*?\*/|//[^\n]*)*([A-Za-z_$@#\[*'\"])", re.S)
WITHHELD = shape.WITHHELD


def _cut_decorators(text):
    """`text` with the arguments of every decorator replaced (`@Inject(() => { CODE })` -> `@Inject(/* args omitted */)`),
    on members and on parameters alike. None when a decorator has a shape this does not read: type arguments, a call
    after the call, `@(expression)`, an argument list that does not close, or something after it that no declaration starts
    with (a sign that the scanner misread the arguments). The caller then withholds the declaration."""
    out, pos = [], 0
    for at in [i for i, c, *_ in _scan(text) if c == "@"]:
        if at < pos:
            continue  # inside arguments already cut
        match = DECORATOR_NAME.match(text, at)
        if not match:
            return None
        name_end = j = match.end()
        while j < len(text) and text[j] in " \t":
            j += 1
        if text[j:j + 1] == "<":
            return None
        if text[j:j + 1] == "(":
            close = next((k for k, d, depth, _, _ in _scan(text[j:]) if d == ")" and depth == 1), None)
            if close is None:
                return None
            out.append(text[pos:name_end] + "(" + DECORATOR_ARGS + ")")
            pos = j + close + 1
            if text[pos:pos + 1] in ("(", ".", "[", "`", "<", "!", "?"):
                return None
        else:
            out.append(text[pos:name_end])
            pos = name_end
        if not AFTER_DECORATOR.match(text, pos):
            return None
    return "".join(out) + text[pos:]


def _function_body_open(text):
    """Index of the `{` that opens a function or method body: a top-level `{` that follows a finished type, not an operator
    (`): { a: 1 } {` is a return type and then the body)."""
    for i, c, paren, angle, brace in _scan(text):
        if c == "{" and paren <= 0 and angle <= 0 and brace <= 0:
            before = text[:i].rstrip()
            if before and before[-1] not in ":|&<,(=" and not before.endswith("=>") and not OPEN_WORDS.search(before):
                return i
    return -1


def _initializer_at(text):
    """Index of the `=` that starts an initializer (not `=>`, `==`, `<=`, `>=`, `!=`, and not inside (), [], <> or {})."""
    for i, c, paren, angle, brace in _scan(text):
        if c == "=" and paren <= 0 and angle <= 0 and brace <= 0:
            following, previous = text[i + 1:i + 2], text[i - 1:i] if i else ""
            if (following and following in "=>") or (previous and previous in "=!<>"):
                continue
            return i
    return -1


def _arrow_at(text):
    for i, c, paren, angle, brace in _scan(text):
        if c == "=" and text[i + 1:i + 2] == ">" and paren <= 0 and angle <= 0 and brace <= 0:
            return i
    return -1


PLAIN_DEFAULT = re.compile(r"""\s*(?:"(?:[^"\\\n]|\\.){0,80}"|'(?:[^'\\\n]|\\.){0,80}'|-?\d[\w.]{0,30}"""
                           r"""|\{\s*\}|\[\s*\]|[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*(?=[,)\]}])""")
DEFAULTS_OMITTED = shape.DEFAULTS


TYPE_OMITTED = ": " + shape.TYPE
TAIL_CHARS = re.compile(r"[\w$\s.<>\[\]|&,?:(){}-]*")
TAIL_REFUSED = re.compile(
    r"\b(?:return|await|yield|throw|function|class|var|let|const|if|else|for|while|do|switch|case|break|continue|try|catch|finally|"
    r"delete|new|import|export|async|with|debugger|instanceof|super|arguments|enum|default)\b|\|\||&&|\?\?|\?\.|\.\.\.")
MEMBER_OF_TYPE = re.compile(r"(?:readonly\s+)?[\w$]+\??\s*:(?P<type>.+)", re.S)
INDEX_OF_TYPE = re.compile(r"\[\s*[\w$]+\s*:\s*[\w$]+\s*\]\s*:(?P<type>.+)", re.S)
MAX_TYPE_NEST = 8


def _split_top(text):
    """`text` split at the commas that are not inside (), [], {} or <>; None when the brackets do not balance."""
    pieces, depth, start = [], 0, 0
    for i, c in enumerate(text):
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
            if depth < 0:
                return None
        elif c == "," and depth == 0:
            pieces.append(text[start:i])
            start = i + 1
    pieces.append(text[start:])
    return pieces if depth == 0 else None


def _safe_type(text, nest=0):
    """True when `text` is made only of what a plain type is made of. The test is an allow-list: type words, `.`, `<>`, `[]`,
    `|`, `&`, `,`, `?`, `:`, `()` for grouping, and `{ name: type, ... }` with no `;`, no `(` and no `=>` in it. A quote, a
    backtick, `=`, `/`, `+`, `;`, an operator, a statement word or a brace group that is not made of `name: type` members
    fails it. It decides what is shown, not what is valid TypeScript: a refusal costs the type, a pass shows only type-shaped text."""
    text = text.strip()
    if nest > MAX_TYPE_NEST or not text or not TAIL_CHARS.fullmatch(text) or TAIL_REFUSED.search(text):
        return False
    flat, stack, i = [], [], 0
    while i < len(text):
        c = text[i]
        if c == "{":
            depth, j = 1, i + 1
            while j < len(text) and depth:
                depth += (text[j] == "{") - (text[j] == "}")
                j += 1
            if depth:
                return False
            inner = text[i + 1:j - 1]
            if "(" in inner or ")" in inner:
                return False
            members = _split_top(inner)
            if members is None:
                return False
            for member in members:
                member = member.strip()
                if not member:
                    continue
                match = MEMBER_OF_TYPE.fullmatch(member) or INDEX_OF_TYPE.fullmatch(member)
                if not match or not _safe_type(match["type"], nest + 1):
                    return False
            flat.append("X")
            i = j
            continue
        if c == "}":
            return False
        if c in "([":
            stack.append(")" if c == "(" else "]")
        elif c in ")]" and (not stack or stack.pop() != c):
            return False
        flat.append(c)
        i += 1
    return not stack and "".join(flat).count("<") == "".join(flat).count(">")


def _safe_tail(tail):
    """What follows a parameter list the scanner cut (its return type, and the `=>` of an arrow). The tail is returned as it is
    only when it is empty or `: <plain type>` (see _safe_type); anything else, which is how a misread parameter list shows
    itself, is replaced by `: /* type omitted */`. A `=>` inside the type is refused: a function type cannot be told from code."""
    core = tail.rstrip()
    arrow = core.endswith("=>")
    if arrow:
        core = core[:-2].rstrip()
    if not core.strip() or (core.lstrip().startswith(":") and _safe_type(core.lstrip()[1:])):
        return tail
    return TYPE_OMITTED + (" =>" if arrow else "")


def _drop_defaults(header):
    """A parameter list without the code in its default values. A default that is a plain literal (number, short string,
    true/false/null/undefined, `{}`, `[]`, an identifier or a dotted name, followed at once by `,` or a closing bracket) stays.
    From the first default that is anything else (`cb = () => { ... }`, `x = f(...)`, `a = x < y`) the rest of the parameter
    list is withheld: the output has `/* defaults omitted */)` and then what follows the list's own `)`, the declared return
    type. Nothing tries to find where such a default ends, since type arguments (`<T, U>`) make a comma ambiguous. The list's
    own `)` is found by the same scanner that can misread a string or a regex, so what follows it is kept only if _safe_tail
    accepts it, and is otherwise replaced; no code in or after a default can come back. Inside a parameter list an `=` is a
    default (`==`, `<=`, `!=` can only occur inside a default)."""
    for i, c, paren, _, _ in _scan(header):
        if c != "=" or paren < 1 or header[i + 1:i + 2] in ("=", ">") or PLAIN_DEFAULT.match(header, i + 1):
            continue
        close = next((j for j, d, depth, _, _ in _scan(header) if j > i and d == ")" and depth == 1), None)
        return header[:i + 1] + " " + DEFAULTS_OMITTED + ")" + ("" if close is None else _safe_tail(header[close + 1:]))
    return header


def _is_arrow_head(head):
    """True when `head`, the text before an initializer's first top-level `=>`, is an arrow function's head: `x`, `async x`, or
    `[async] [<T>](params)` followed by nothing or `: <type>`. Anything else (`cond ? init(x) : (y)`, `a && (y)`, `x as (a: T)`) is
    code that happens to end in an arrow, and the initializer is omitted."""
    head = re.sub(r"^async\b\s*(?=[(<A-Za-z_$])", "", head.strip())
    if re.fullmatch(r"[A-Za-z_$][\w$]*", head):
        return True
    rest = head
    if rest.startswith("<"):
        close = next((i for i, c, _, angle, _ in _scan(rest) if c == ">" and angle == 1 and rest[i - 1] != "="), None)
        if close is None:
            return False
        rest = rest[close + 1:].lstrip()
    if not rest.startswith("("):
        return False
    close = next((i for i, c, depth, _, _ in _scan(rest) if c == ")" and depth == 1), None)
    if close is None:
        return True  # the list does not close for the scanner: _drop_defaults withholds it and the gate checks what is left
    tail = rest[close + 1:]
    return not tail.strip() or shape.return_type_ok(tail)


def _value_view(header, initializer):
    """`header = initializer` reduced to what a caller needs: a function's parameters and result, a short literal, else nothing."""
    initializer = initializer.strip().rstrip(";").strip()
    if LITERAL.match(initializer):
        return f"{header} = {initializer};"
    arrow = _arrow_at(initializer)
    if arrow >= 0 and _is_arrow_head(initializer[:arrow]):
        return f"{header} = {_drop_defaults(initializer[:arrow + 2])} {OMITTED};"
    if re.match(r"(?:async\s+)?function\b", initializer):
        body = _function_body_open(initializer)
        return f"{header} = {_drop_defaults(initializer[:body].rstrip() if body >= 0 else initializer)} {OMITTED};"
    return f"{header}; {INIT_OMITTED}"


# ---- the output gate -----------------------------------------------------------------------------------------------

MEMBER_CONTEXT = {"class": "class", "interface": "interface", "enum": "enum"}  # a namespace or module member is a declaration


def _leading_doc(text):
    """(doc, rest): the comment that opens `text`, kept as documentation, and what follows it."""
    stripped = text.lstrip()
    if stripped.startswith("/*"):
        end = stripped.find("*/", 2)
        if end >= 0:
            return stripped[:end + 2] + "\n", stripped[end + 2:].lstrip()
    return "", stripped


def _replacement(rest, context):
    """What is shown for a declaration the gate refused: its name and the withheld marker, or the marker alone."""
    name = None
    if context == "decl":
        decl = DECL.match(_cut_decorators(rest) or "")
        candidate = decl.group(0) + " " + WITHHELD if decl else None
    else:
        member = MEMBER.match(rest)
        after = rest[member.end("name"):].lstrip(" \t?!") if member else ""
        name = member["name"] if member and not after.startswith(("(", "<")) else None  # a method-shaped refusal keeps no name
        candidate = name + " " + WITHHELD if name else None
    end = ";" if context == "class" else ""
    if candidate and shape.accepts(candidate + end, context):
        return candidate + end
    return WITHHELD + end


def _gate(text, context):
    """The single gate every text taken from a TypeScript source file passes before it leaves this module. `text` comes back
    as it is when it is only declaration shapes (shape.accepts); otherwise its leading doc comment is kept and the rest is
    replaced by the declaration's name and `/* declaration omitted */`, or by the marker alone. The per-site cuts above keep the
    output useful; this is what guarantees no code is returned when one of them, or the scanner, gets a shape wrong."""
    if shape.accepts(text, context):
        return text
    doc, rest = _leading_doc(text)
    return doc + _replacement(rest, context)


def _view(statement):
    """declaration_view() before the gate."""
    statement = _cut_decorators(statement.strip().rstrip(";").rstrip())
    if statement is None:
        return WITHHELD
    decl = DECL.match(statement)
    kind = decl["kind"] if decl else None
    if kind == "enum":
        return _enum_view(statement)
    if kind in KEEP_WHOLE:
        return statement
    if kind in BLOCKS:
        return _block_view(statement, kind)
    equals = _initializer_at(statement)
    if equals >= 0:
        return _value_view(statement[:equals].rstrip(), statement[equals + 1:])
    body = _function_body_open(statement)
    return _drop_defaults(statement[:body].rstrip() if body >= 0 else statement) + ";"


def declaration_view(statement):
    """A statement of a TypeScript *source* file as its declaration: function and method bodies, variable and property
    initializers (short literals aside), code in heritage clauses and enum initializers, and private members are left out.
    Interfaces and type aliases are kept as written. What is returned has passed _gate()."""
    return _gate(_view(statement), "decl")


ENUM_MEMBER = re.compile(r"""^(?P<name>[A-Za-z_$][\w$]*|'[^'\\\n]*'|"[^"\\\n]*")\s*(?:=\s*(?P<init>.*))?$""", re.S)
ENUM_LITERAL = re.compile(r"""(?:"[^"\\\n]{0,80}"|'[^'\\\n]{0,80}'|-?(?:0[xXoObB][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?))""")


def _enum_view(statement):
    """An enum with each member's initializer kept only when it is a string or number literal."""
    span = _body_span(statement)
    if span is None:
        return _gate(statement, "decl")
    open_at, close_at = span
    body = statement[open_at + 1:close_at]
    lines = []
    for start, end, doc in split_statements(body, members=True):
        member = ENUM_MEMBER.match(body[start:end].strip().rstrip(",;").strip())
        if not member:
            line = WITHHELD
        elif member["init"] is None:
            line = member["name"]
        elif ENUM_LITERAL.fullmatch(member["init"].strip()):
            line = f"{member['name']} = {member['init'].strip()}"
        else:
            line = f"{member['name']} {INIT_OMITTED}"
        lines.append("  " + _with_doc(body, doc, line + ",").replace("\n", "\n  "))
    return statement[:open_at].rstrip() + (" {\n" + "\n".join(lines) + "\n}" if lines else " {}")


HERITAGE_OPENS_BLOCK = re.compile(r"(?<![\w$.])(?:class|function)(?![\w$])")
HERITAGE_OPEN_END = re.compile(r"(?:(?<![\w$.])(?:extends|implements|new|typeof|void|await|yield|in|instanceof)|[^\w$)\]>\s])\s*$")


def _top_level(text):
    """`text` without what is inside (), [], {} and <> (the brackets kept), strings and comments."""
    out = []
    for _, c, paren, angle, brace in _scan(text):
        depth = max(paren, 0) + max(angle, 0) + max(brace, 0)
        if depth == 0 or (depth == 1 and c in ")]}>"):
            out.append(c)
    return "".join(out)


def _heritage_cut(head):
    """A class or namespace header with what follows its name and type parameters replaced by `/* heritage omitted */`, when
    the header as written is not type-shaped (`extends mix(() => { ... })`). None when even the cut header is not."""
    if shape.accepts(head + " {}", "decl"):
        return head
    decl = DECL.match(head)
    if not decl:
        return None
    rest, cut = head[decl.end():], head[:decl.end()]
    flat = _top_level(rest)
    if HERITAGE_OPENS_BLOCK.search(flat) or HERITAGE_OPEN_END.search(flat):
        return None  # `extends class {`, `extends function () {`: the `{` taken for the body may be the expression's own
    if rest.lstrip().startswith("<"):
        offset = len(rest) - len(rest.lstrip())
        rest = rest.lstrip()
        close = next((i for i, c, _, angle, _ in _scan(rest) if c == ">" and angle == 1 and rest[i - 1] != "="), None)
        if close is None:
            return None
        cut = head[:decl.end() + offset + close + 1]
    cut = f"{cut} {HERITAGE_OMITTED}"
    return cut if shape.accepts(cut + " {}", "decl") else None


def _block_view(statement, kind):
    span = _body_span(statement)
    if span is None:
        return _gate(statement + ";", "decl")
    open_at, close_at = span
    head = _heritage_cut(statement[:open_at].rstrip())
    if head is None:
        return _gate(statement, "decl")
    body = statement[open_at + 1:close_at]
    context = "class" if kind == "class" else "decl"
    lines = []
    for start, end, doc in split_statements(body, members=True):
        member = _cut_decorators(body[start:end].strip().rstrip(",;"))
        if member is None or PRIVATE_MEMBER.match(member) or (kind != "class" and not member.startswith("export")):
            continue  # a member whose decorators cannot be read is left out, as a private one is
        shown = _gate(_with_doc(body, doc, _view(member)), context)
        lines.append("  " + shown.replace("\n", "\n  "))
    return head + (" {\n" + "\n".join(lines) + "\n}" if lines else " {}")


def find_declarations(text, symbol, _nest=0, source=False, exported_only=True):
    """Declaration texts (doc comment included) for `symbol` in one declaration file's text. With `source`, `text` is TypeScript
    source: only exported declarations count and each is returned as declaration_view() shows it, through _gate()."""
    head, _, rest = symbol.partition(".")
    out = []
    for start, end, doc in split_statements(text):
        statement = text[start:end].strip()
        if source and exported_only and not statement.startswith("export"):
            continue
        match = DECL.match(statement)
        if match and match["name"] == head:
            if not rest:
                out.append(_gate(_with_doc(text, doc, _view(statement)), "decl") if source else _with_doc(text, doc, statement))
            elif _nest < MAX_NEST and match["kind"] in ("namespace", "module", "class", "interface", "enum"):
                out.extend(find_members(_body(statement), rest, _nest + 1, source, match["kind"]))
        elif _nest < MAX_NEST and re.match(r"^(?:export\s+)?declare\s+(?:module\s+['\"]|global\b)", statement):
            out.extend(find_declarations(_body(statement), symbol, _nest + 1, source=source, exported_only=False))
    return out


def find_members(body, path, _nest=0, source=False, parent="interface"):
    """Members of a container body named by the dotted `path`. With `source`, each passes _gate() as a member of `parent`."""
    head, _, rest = path.partition(".")
    out = []
    for start, end, doc in split_statements(body, members=True):
        statement = body[start:end].strip().rstrip(",;")
        if source:
            statement = _cut_decorators(statement)
            if statement is None:
                continue
        if source and (PRIVATE_MEMBER.match(statement) or (parent in ("namespace", "module") and not statement.startswith("export"))):
            continue
        decl, member = DECL.match(statement), MEMBER.match(statement)
        name = decl["name"] if decl else member["name"].strip("'\"") if member else None
        if name != head:
            continue
        if not rest:
            if source:
                out.append(_gate(_with_doc(body, doc, _view(statement)), MEMBER_CONTEXT.get(parent, "decl")))
            else:
                out.append(_with_doc(body, doc, statement))
        elif _nest < MAX_NEST and decl and decl["kind"] in ("namespace", "module", "class", "interface", "enum"):
            out.extend(find_members(_body(statement), rest, _nest + 1, source, decl["kind"]))
    return out


def _export_items(clause):
    """[(original, exported)] from the inside of `{ A, B as C, type D }`."""
    items = []
    for item in clause.split(","):
        words = [w for w in item.split() if w != "type"]
        if len(words) == 1:
            items.append((words[0], words[0]))
        elif len(words) == 3 and words[1] == "as":
            items.append((words[0], words[2]))
    return items


def local_aliases(text, head, include_same=False):
    """Originals that a local `export { Original as head }` (no `from`) exports as `head`. With `include_same`, also `head`
    itself when it is exported that way (a source file declares it without `export`)."""
    found = []
    for match in re.finditer(r"export\s+(?:type\s+)?\{([^}]*)\}(?!\s*from\b)", text):
        found.extend(orig for orig, exported in _export_items(match.group(1)) if exported == head and (include_same or orig != head))
    return found


def reexports(text, head):
    """[(specifier, name to look for there)] for relative re-exports that can supply `head`:
    `export * from './x'` (same name) and `export { A as head } from './x'` (name A)."""
    found = []
    pattern = r"""export\s+(?:type\s+)?(\*|\{[^}]*\})\s*from\s*['"](\.{1,2}(?:/[^'"]*)?)['"]"""
    for match in re.finditer(pattern, text):
        clause, spec = match.groups()
        if clause == "*":
            found.append((spec, head))
        else:
            found.extend((spec, orig) for orig, exported in _export_items(clause[1:-1]) if exported == head)
    return found


# ---- package + entry resolution ------------------------------------------------------------------------------------

def deno_dir(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("DENO_DIR"):
        return Path(os.environ["DENO_DIR"])
    home = Path.home()
    if os.uname().sysname == "Darwin":
        return home / "Library" / "Caches" / "deno"
    return Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / "deno"


def parse_package(spec):
    """(name, wanted_version, kind) from `name`, `@scope/name`, `npm:name[@version]`, `jsr:@scope/name[@version]` or an
    `https://host/path` URL. kind is "npm", "deno-npm", "jsr" or "https" (for https, name is the URL)."""
    spec = str(spec or "").strip()
    if not spec or len(spec) > 500 or (len(spec) > 214 and not spec.startswith("https://")):
        raise guard.Rejected("refused: package name is empty or too long")
    if spec.startswith("https://"):
        remote.parse_https(spec)
        return spec, None, "https"
    if re.match(r"^(http|file|node|data):", spec) or spec.startswith(("/", ".", "~", "\\")):
        raise guard.Rejected(
            f"refused: {spec.split(':', 1)[0]!r} specifiers and paths are not supported; npm, jsr: and https: modules are read, "
            "from node_modules or Deno's cache")
    kind = "npm"
    for prefix, found in (("npm:", "deno-npm"), ("jsr:", "jsr")):
        if spec.startswith(prefix):
            spec, kind = spec[len(prefix):], found
    wanted = None
    at = spec.rfind("@")
    if at > 0:
        spec, wanted = spec[:at], spec[at + 1:] or None
    if kind == "jsr":
        if not remote.JSR_RE.match(spec):
            raise guard.Rejected(f"refused: {spec!r} is not a jsr package name (@scope/name)")
    elif not NAME_RE.match(spec):
        raise guard.Rejected(f"refused: {spec!r} is not an npm package name")
    return spec, wanted, kind


def types_package(name):
    return "@types/" + (name[1:].replace("/", "__") if name.startswith("@") else name)


def installed_version(directory):
    try:
        text = guard.read_text(Path(directory) / "package.json", root=directory, limit=1_000_000)
        version = json.loads(text).get("version") if text else None
    except (ValueError, guard.Rejected, OSError):
        return None
    return version if isinstance(version, str) else None


def node_package_dir(project, name):
    modules = Path(project) / "node_modules"
    if modules.is_symlink():
        raise guard.Rejected("refused: node_modules is a symlink")
    if not modules.is_dir() or not (modules / name).exists():
        return None
    return guard.contained_dir(modules, modules / name)


def deno_package_dir(deno, name, version):
    cache = deno_dir(deno) / "npm"
    if not cache.is_dir() or cache.is_symlink():
        return None
    for host in sorted(p for p in cache.iterdir() if p.is_dir() and not p.is_symlink()):
        candidate = host / name / version
        if candidate.is_dir():
            return guard.contained_dir(cache, candidate)
    return None


def _types_target(value):
    """A declaration path from package.json: the path itself, or the .d.ts beside a .js/.mjs/.cjs one."""
    if not isinstance(value, str) or not value:
        return None
    value = value[2:] if value.startswith("./") else value
    for js, dts in ((".mjs", ".d.mts"), (".cjs", ".d.cts"), (".js", ".d.ts")):
        if value.endswith(js):
            return value[: -len(js)] + dts
    return value


def _from_exports(exports):
    if isinstance(exports, dict) and "." in exports:
        exports = exports["."]
    if isinstance(exports, str):
        return _types_target(exports)
    if isinstance(exports, list):
        return next(filter(None, (_from_exports(item) for item in exports)), None)
    if isinstance(exports, dict):
        for key in ("types", "import", "require", "default", "node"):
            if key in exports:
                got = _from_exports(exports[key])
                if got:
                    return got
    return None


def entry_candidates(package_dir):
    """Declaration entry paths (relative to the package directory), in the order package.json names them."""
    try:
        text = guard.read_text(Path(package_dir) / "package.json", root=package_dir, limit=1_000_000)
        meta = json.loads(text) if text else {}
    except (ValueError, guard.Rejected):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    out = []
    for value in (meta.get("types"), meta.get("typings"), _from_exports(meta.get("exports")), _types_target(meta.get("main"))):
        if isinstance(value, str) and value and value not in out:
            out.append(value)
    out.extend(f for f in ("index.d.ts", "index.d.mts", "index.d.cts") if f not in out)
    return out


def _existing(package_dir, relative, refusals):
    """The package-relative path of an existing allowlisted file, or None (a refusal is noted in `refusals`)."""
    try:
        path = guard.resolve_inside(package_dir, relative)
    except FileNotFoundError:
        return None  # the package directory is gone
    except guard.Rejected as exc:
        note = f"{relative}: {exc}"
        if note not in refusals:
            refusals.append(note)
        return None
    return os.path.relpath(path, guard.real_root(package_dir)) if path.is_file() else None


def _module_file(package_dir, base, specifier, refusals):
    joined = os.path.normpath(os.path.join(os.path.dirname(base), specifier))
    stem = re.sub(r"\.(?:mjs|cjs|js)$", "", joined)
    for option in (f"{stem}.d.ts", f"{stem}.d.mts", f"{stem}.d.cts", f"{joined}.d.ts", f"{joined}/index.d.ts", f"{joined}/index.d.mts"):
        found = _existing(package_dir, option, refusals)
        if found:
            return found
    return None


class DirSource:
    """A package directory (node_modules or Deno's npm cache): the strict allowlist, declaration files only."""

    def __init__(self, package_dir):
        self.package_dir = package_dir

    def entry_candidates(self):
        return entry_candidates(self.package_dir)

    def existing(self, key, refusals):
        return _existing(self.package_dir, key, refusals)

    def read(self, key):
        try:
            return guard.read_text(guard.resolve_inside(self.package_dir, key), root=self.package_dir)
        except FileNotFoundError:
            return None  # the package directory is gone

    def module(self, base, specifier, refusals):
        return _module_file(self.package_dir, base, specifier, refusals)

    def is_source(self, key):
        return False


def search_package(source, symbol):
    """(hits, files_read, entry, refusals): hits are (file, declaration text); relative re-exports are followed.
    `source` is a DirSource or one of remote.py's sources."""
    refusals = []
    entry = next(filter(None, (source.existing(c, refusals) for c in source.entry_candidates())), None)
    if entry is None:
        if refusals:
            raise guard.Rejected("refused: " + "; ".join(refusals))
        raise Miss("the package ships no declaration entry (no types/typings/exports/main .d.ts, no index.d.ts)", no_types=True)
    queue, seen, hits = [(entry, symbol, 0)], set(), []
    while queue and len(seen) < MAX_FILES:
        current, wanted, depth = queue.pop(0)
        if (current, wanted) in seen:
            continue
        seen.add((current, wanted))
        text = source.read(current)
        if text is None:
            continue
        code = source.is_source(current)
        head, _, rest = wanted.partition(".")
        found = find_declarations(text, wanted, source=code)
        for original in local_aliases(text, head, include_same=code):
            found += find_declarations(text, original + ("." + rest if rest else ""), source=code, exported_only=False)
        equals = re.search(r"^\s*export\s*=\s*([A-Za-z_$][\w$]*)\s*;?", text, re.M)
        if not found and equals and not rest:
            found = find_declarations(text, f"{equals.group(1)}.{wanted}", source=code)
        if found:
            hits.extend((current, hit) for hit in found)
            continue
        if depth < MAX_NEST:
            for specifier, name in reexports(text, head):
                module = source.module(current, specifier, refusals)
                if module:
                    queue.append((module, name + ("." + rest if rest else ""), depth + 1))
    files = sorted({f for f, _ in seen})
    return hits, files, entry, refusals


def _answer(name, pinned, source, symbol, where, exact):
    """The Found for `symbol` read from `source`, or Miss."""
    try:
        hits, files, entry, refusals = search_package(source, symbol)
    except remote.Unavailable as unavailable:
        raise Miss(str(unavailable), pinned.version) from None
    except Miss as miss:
        miss.version = pinned.version
        raise
    if not hits:
        note = f"; refused: {'; '.join(refusals)}" if refusals else ""
        raise Miss(f"{symbol} not found in the {name}@{pinned.version} declarations (entry {entry}, {len(files)} file(s) read){note}",
                   pinned.version)
    member = f" (member {symbol})" if "." in symbol else ""
    text = "\n\n".join(f"// {name}@{pinned.version} — {file}{member}\n{hit}" for file, hit in hits)
    provenance = {"source": where, "version": pinned.version, "exact": exact, "retrieved": guard.now()}
    return Found(text, pinned.version, name, files, provenance)


def _lookup_jsr(project, name, wanted, symbol, deno):
    pinned = lock.pin(project, name, scheme="jsr")
    if not pinned:
        raise Miss(pinned.reason)
    if wanted and wanted != pinned.version:
        raise Miss(f"jsr:{name}@{wanted} was asked for but {pinned.lockfile} pins {pinned.version}; no result for other versions", pinned.version)
    source = remote.JsrSource(deno_dir(deno), name, pinned.version, pinned.integrity)
    try:
        verified = source.vouch()
    except remote.Unavailable as unavailable:
        raise Miss(str(unavailable), pinned.version) from None
    if not source.cached() or not any(source.existing(c, []) for c in source.entry_candidates()):
        raise Miss(f"jsr:{name}@{pinned.version} is pinned but not in Deno's cache; run `deno cache jsr:{name}@{pinned.version}` first "
                   "(this tool never fetches)", pinned.version)
    return _answer(f"jsr:{name}", pinned, source, symbol, f"deno-cache:{source.directory}", verified)


def _lookup_https(project, url, symbol, deno):
    host, path = remote.parse_https(url)
    source = remote.HttpsSource(deno_dir(deno), project, host, path)
    if not source.locked(path):
        raise Miss(f"no pinned version: {url} has no entry in deno.lock (its \"remote\" section)")
    pinned = lock.Pin(source.version(), "deno.lock")
    if not source.existing(path, []):
        raise Miss(f"{url} is pinned but not in Deno's cache; run `deno cache {url}` first (this tool never fetches)", pinned.version)
    return _answer(url, pinned, source, symbol, f"deno-cache:{source.directory}", True)


def _lookup(project, name, wanted, via_deno, symbol, deno):
    node_dir = None if via_deno else node_package_dir(project, name)
    pinned = lock.pin(project, name)
    if not pinned and pinned.candidates and node_dir:  # several versions pinned: only then does the installed one matter
        pinned = lock.pin(project, name, installed_version(node_dir))
    if not pinned:
        raise Miss(pinned.reason)
    if wanted and wanted != pinned.version:
        raise Miss(f"{name}@{wanted} was asked for but {pinned.lockfile} pins {pinned.version}; no result for other versions",
                   pinned.version)
    package_dir = node_dir or deno_package_dir(deno, name, pinned.version)
    if package_dir is None:
        raise Miss(f"{name}@{pinned.version} is pinned but not installed (node_modules"
                   f"{'' if via_deno else ' and'} Deno's npm cache were searched); install dependencies first", pinned.version)
    installed = installed_version(package_dir)
    if installed != pinned.version:
        raise Miss(f"{name} on disk is {installed or 'of unknown version'} but {pinned.lockfile} pins {pinned.version}; "
                   "reinstall to match the lockfile", pinned.version)
    where = f"{'node_modules' if package_dir is node_dir else 'deno-cache'}:{package_dir}"
    return _answer(name, pinned, DirSource(package_dir), symbol, where, installed == pinned.version)


def api_signature(project, package, symbol, *, deno=None):
    """A Found for the symbol. Raises Miss (a valid request without an answer) or guard.Rejected (refused)."""
    symbol = str(symbol or "").strip()
    if not SYMBOL_RE.match(symbol) or len(symbol) > 200:
        raise guard.Rejected(f"refused: {symbol!r} is not an identifier (use Name or Namespace.member)")
    name, wanted, kind = parse_package(package)
    if kind == "jsr":
        return _lookup_jsr(project, name, wanted, symbol, deno)
    if kind == "https":
        return _lookup_https(project, name, symbol, deno)
    via_deno = kind == "deno-npm"
    try:
        return _lookup(project, name, wanted, via_deno, symbol, deno)
    except Miss as first:
        # A package with no declarations of its own may have them in @types/<name>, pinned on its own.
        if not first.no_types or name.startswith("@types/"):
            raise
        try:
            return _lookup(project, types_package(name), None, via_deno, symbol, deno)
        except Miss:
            raise first from None
