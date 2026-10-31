"""The allow-list of declaration shapes that text taken from a TypeScript source file must match to leave signature.py.

`accepts(text, context)` is a recogniser, not a TypeScript parser. It answers one question: is `text` made only of a
declaration, meaning declaration words, names, types, parameter lists whose defaults are plain literals, and the omission markers
signature.py writes? Anything it does not know is refused, including:
- a call outside a type,
- a `=>` followed by anything but a type or the body marker,
- braces holding anything but type members, class members or enum members,
- a quote or backtick outside a literal type, a plain literal value or a member name,
- a regex or `/`, an operator such as `||`, `&&`, `??`, `?.`, `+`,
- a statement word, or two declarations run together.

A refusal costs the declaration; it never shows code. Comments are documentation and are skipped (an unclosed one is refused).

Contexts: "decl" (a top-level or namespace declaration), "class" (one class member), "interface" (one interface or type
literal member), "enum" (one enum member).
"""
import re

BODY = "/* body omitted */"
DEFAULTS = "/* defaults omitted */"
TYPE = "/* type omitted */"
INIT = "/* initializer omitted */"
HERITAGE = "/* heritage omitted */"
ARGS = "/* args omitted */"
WITHHELD = "/* declaration omitted */"
MARKERS = {BODY: "BODY", DEFAULTS: "DEFAULTS", TYPE: "TYPE", INIT: "INIT", HERITAGE: "HERITAGE", ARGS: "ARGS", WITHHELD: "WITHHELD"}

STEP_LIMIT = 200_000
NUMBER = re.compile(r"(?:0[xXoObB][0-9a-fA-F_]+|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?)n?(?![\w$.])")
IDENT = re.compile(r"[A-Za-z_$][\w$]*")
REFUSED_PUNCT = ("?.", "??", "||", "&&", "==", "!=", "++", "--", "**", "=<", "<=", ">=")
PUNCT = ("...", "=>", "?", ":", ";", ",", ".", "(", ")", "[", "]", "{", "}", "<", ">", "=", "|", "&", "*", "!", "-", "+", "@")
STATEMENT_WORDS = frozenset((
    "return", "await", "yield", "throw", "if", "else", "for", "while", "do", "switch", "case", "break", "continue", "try", "catch",
    "finally", "delete", "new", "function", "class", "var", "let", "const", "import", "export", "with", "debugger", "super", "in",
    "instanceof", "enum", "default", "extends", "implements", "typeof", "keyof", "infer", "interface", "package", "static"))
DECL_MODS = frozenset(("export", "default", "declare", "abstract", "async"))
CLASS_MODS = frozenset(("public", "protected", "private", "static", "readonly", "abstract", "declare", "async", "override", "accessor",
                        "export", "get", "set"))
PARAM_MODS = frozenset(("public", "protected", "private", "readonly", "override"))
END = ("end", None, True)


class Refused(Exception):
    pass


class _Cut(Exception):
    """`= /* defaults omitted */`: signature.py withheld the rest of the parameter list; the list's own `)` comes next."""


def tokens(text):
    """[(kind, value, newline_before)] for `text`; kinds: id, num, str, tpl (value: the `${}` holes), p (punctuation), m (marker)."""
    out, i, n, newline = [], 0, len(text), False

    def add(kind, value):
        nonlocal newline
        out.append((kind, value, newline))
        newline = False

    while i < n:
        c = text[i]
        if c in " \t\r\f\v":
            i += 1
        elif c == "\n":
            newline, i = True, i + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise Refused("unclosed comment")
            comment = text[i:end + 2]
            if comment in MARKERS:
                add("m", MARKERS[comment])
            i = end + 2
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                if text[j] == "\n":
                    raise Refused("string not closed on its line")
                j += 2 if text[j] == "\\" else 1
            if j >= n:
                raise Refused("string not closed")
            add("str", text[i:j + 1])
            i = j + 1
        elif c == "`":
            j, holes = i + 1, []
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 2
                elif text.startswith("${", j):
                    end = text.find("}", j + 2)
                    hole = text[j + 2:end] if end >= 0 else ""
                    if end < 0 or re.search(r"[`'\"{/\\]", hole):
                        raise Refused("template hole that is not a plain type")
                    holes.append(hole)
                    j = end + 1
                else:
                    j += 1
            if j >= n:
                raise Refused("template not closed")
            add("tpl", holes)
            i = j + 1
        elif c.isdigit():
            match = NUMBER.match(text, i)
            if not match:
                raise Refused("number")
            add("num", match.group())
            i = match.end()
        elif c.isascii() and (c.isalpha() or c in "_$"):
            match = IDENT.match(text, i)
            add("id", match.group())
            i = match.end()
        else:
            if any(text.startswith(p, i) for p in REFUSED_PUNCT):
                raise Refused("operator")
            punct = next((p for p in PUNCT if text.startswith(p, i)), None)
            if punct is None:
                raise Refused(f"character {c!r}")
            add("p", punct)
            i += len(punct)
    return out


class _Parser:
    def __init__(self, toks):
        self.t, self.i, self.steps, self.depth = toks, 0, 0, 0
        self.params_typed = self.had_return = False

    # ---- token helpers
    def tok(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else END

    def is_(self, kind, value=None, k=0):
        tok = self.tok(k)
        return tok[0] == kind and (value is None or tok[1] == value)

    def p(self, value, k=0):
        return self.is_("p", value, k)

    def w(self, value, k=0):
        return self.is_("id", value, k)

    def take(self, kind, value=None):
        if self.is_(kind, value):
            self.i += 1
            return True
        return False

    def tp(self, value):
        return self.take("p", value)

    def tw(self, value):
        return self.take("id", value)

    def tm(self, value):
        return self.take("m", value)

    def tick(self):
        self.steps += 1
        if self.steps > STEP_LIMIT:
            raise Refused("too complex")

    def attempt(self, rule):
        save = self.i
        if rule():
            return True
        self.i = save
        return False

    def name(self):
        """An identifier that is not a statement word."""
        if self.is_("id") and self.tok()[1] not in STATEMENT_WORDS:
            self.i += 1
            return True
        return False

    def name_start(self, k):
        kind, value, _ = self.tok(k)
        return kind in ("id", "str", "num") or (kind == "p" and value in ("[", "*", "#"))

    # ---- types
    def type_(self):
        self.tick()
        if not self.union():
            return False
        if self.w("extends"):
            save = self.i
            self.i += 1
            if self.union() and self.tp("?") and self.type_() and self.tp(":") and self.type_():
                return True
            self.i = save
        return True

    def union(self):
        self.tp("|")
        if not self.intersection():
            return False
        while self.tp("|"):
            if not self.intersection():
                return False
        return True

    def intersection(self):
        self.tp("&")
        if not self.operator():
            return False
        while self.tp("&"):
            if not self.operator():
                return False
        return True

    def operator(self):
        self.tick()
        if (self.w("keyof") or self.w("unique") or self.w("readonly")) and not self.p("(", 1) and self.tok(1)[0] in ("id", "p", "str", "num", "tpl"):
            self.i += 1
            return self.operator()
        if self.w("infer"):
            self.i += 1
            return self.name()
        return self.postfix()

    def postfix(self):
        if not self.primary():
            return False
        while self.p("[") and not self.tok()[2]:
            save = self.i
            self.i += 1
            if self.tp("]") or (self.type_() and self.tp("]")):
                continue
            self.i = save
            break
        return True

    def primary(self):
        self.tick()
        kind, value, _ = self.tok()
        if kind in ("str", "num"):
            self.i += 1
            return True
        if kind == "tpl":
            self.i += 1
            return all(type_ok(hole) for hole in value)
        if self.p("-") and self.is_("num", k=1):
            self.i += 2
            return True
        if self.p("(") or self.p("<"):
            if self.attempt(self.function_type):
                return True
            return self.tp("(") and self.type_() and self.tp(")")
        if self.w("abstract") and self.w("new", 1):
            self.i += 1
        if self.tw("new"):
            return self.function_type()
        if self.tw("typeof"):
            if self.w("import"):
                return self.import_type()
            return self.dotted() and self.type_arguments()
        if self.w("import"):
            return self.import_type()
        if self.p("{"):
            return self.object_type()
        if self.p("["):
            return self.tuple_type()
        if kind == "id" and value not in STATEMENT_WORDS:
            return self.dotted() and self.type_arguments()
        return False

    def dotted(self):
        if not self.name():
            return False
        while self.p(".") and self.is_("id", k=1):
            self.i += 2
        return True

    def type_arguments(self):
        if not self.p("<"):
            return True
        self.i += 1
        while True:
            if not self.type_():
                return False
            if not self.tp(","):
                break
            if self.p(">"):
                break
        return self.tp(">")

    def import_type(self):
        if not (self.tw("import") and self.tp("(") and self.take("str") and self.tp(")")):
            return False
        while self.p(".") and self.is_("id", k=1):
            self.i += 2
        return self.type_arguments()

    def function_type(self):
        if self.p("<") and not self.type_parameters():
            return False
        return self.parameters() and self.tp("=>") and self.type_()

    def tuple_type(self):
        self.tp("[")
        while not self.p("]"):
            self.tick()
            self.tp("...")
            if self.is_("id") and (self.p(":", 1) or (self.p("?", 1) and self.p(":", 2))):
                self.i += 1
                self.tp("?")
                self.tp(":")
            if not self.type_():
                return False
            self.tp("?")
            if not self.tp(","):
                break
        return self.tp("]")

    def object_type(self):
        if not self.tp("{"):
            return False
        while not self.p("}"):
            self.tick()
            if not self.type_member():
                return False
            if self.tp(";") or self.tp(","):
                continue
            if not self.p("}") and not self.tok()[2]:
                return False  # two members on one line with nothing between them
        return self.tp("}")

    def type_member(self):
        if self.tm("WITHHELD"):
            return True
        if self.attempt(self.mapped_or_index):
            return True
        if self.p("(") or self.p("<"):
            return self.signature_rest()
        if self.w("new") and (self.p("(", 1) or self.p("<", 1)):
            self.i += 1
            return self.signature_rest()
        while (self.w("readonly") or self.w("get") or self.w("set")) and self.name_start(1):
            self.i += 1
        return self.named_member(in_class=False)

    def mapped_or_index(self):
        if (self.p("+") or self.p("-")) and self.w("readonly", 1):
            self.i += 1
        self.tw("readonly")
        if not (self.tp("[") and self.name()):
            return False
        if self.tp(":"):
            if not (self.type_() and self.tp("]")):
                return False
        elif self.tw("in"):
            if not self.type_():
                return False
            if self.tw("as") and not self.type_():
                return False
            if not self.tp("]"):
                return False
            if (self.p("+") or self.p("-")) and self.p("?", 1):
                self.i += 1
        else:
            return False
        self.tp("?")
        return self.tp(":") and self.type_()

    def type_parameters(self):
        if not self.tp("<"):
            return False
        while not self.p(">"):
            self.tick()
            while (self.w("const") or self.w("in") or self.w("out")) and self.is_("id", k=1):
                self.i += 1
            if not self.name():
                return False
            if self.tw("extends") and not self.type_():
                return False
            if self.tp("="):
                if self.is_("m", "DEFAULTS"):
                    raise _Cut()
                if not self.type_():
                    return False
            if not self.tp(","):
                break
        return self.tp(">")

    # ---- parameters and values
    def parameters(self):
        if not self.tp("("):
            return False
        self.depth += 1
        typed = True
        try:
            try:
                while not self.p(")"):
                    self.tick()
                    ok, has_type = self.parameter()
                    if not ok:
                        return False
                    typed = typed and has_type
                    if not self.tp(","):
                        break
            except _Cut:
                if self.depth != 1:
                    raise
                typed = False
            if not self.tp(")"):
                return False
        finally:
            self.depth -= 1
        self.params_typed = typed
        return True

    def parameter(self):
        self.decorators()
        while self.is_("id") and self.tok()[1] in PARAM_MODS and (self.is_("id", k=1) or self.p("{", 1) or self.p("[", 1)):
            self.i += 1
        self.tp("...")
        if not self.binding():
            return False, False
        self.tp("?")
        has_type = False
        if self.tp(":"):
            if not self.type_():
                return False, False
            has_type = True
        if self.tp("=") and not self.default():
            return False, False
        return True, has_type

    def binding(self):
        if self.w("this"):
            self.i += 1
            return True
        if self.p("{"):
            return self.object_pattern()
        if self.p("["):
            return self.array_pattern()
        return self.name()

    def object_pattern(self):
        self.tp("{")
        while not self.p("}"):
            self.tick()
            if self.tp("..."):
                if not self.name():
                    return False
            else:
                if not self.property_name():
                    return False
                if self.tp(":") and not self.binding():
                    return False
                if self.tp("=") and not self.default():
                    return False
            if not self.tp(","):
                break
        return self.tp("}")

    def array_pattern(self):
        self.tp("[")
        while not self.p("]"):
            self.tick()
            if self.tp(","):
                continue
            self.tp("...")
            if not self.binding():
                return False
            if self.tp("=") and not self.default():
                return False
            if not self.tp(","):
                break
        return self.tp("]")

    def default(self):
        if self.is_("m", "DEFAULTS"):
            self.i += 1
            raise _Cut()
        return self.literal()

    def literal(self):
        """A plain literal value: a string, a number, `{}`, `[]`, or a (dotted) name such as `undefined` or `Mode.Fast`."""
        if self.take("str") or self.take("num"):
            return True
        if self.p("-") and self.is_("num", k=1):
            self.i += 2
            return True
        if self.p("{") and self.p("}", 1) or self.p("[") and self.p("]", 1):
            self.i += 2
            return True
        if self.w("this"):
            self.i += 1
        elif not self.name():
            return False
        while self.p(".") and self.is_("id", k=1):
            self.i += 2
        return True

    def value(self):
        """What may follow `=` in a declaration: a plain literal, or a function head ending in the body marker."""
        return self.attempt(self.arrow_value) or self.attempt(self.function_value) or self.literal()

    def arrow_value(self):
        if self.w("async") and (self.p("(", 1) or self.p("<", 1) or self.is_("id", k=1)):
            self.i += 1
        if self.p("<") and not self.type_parameters():
            return False
        if self.p("("):
            if not self.parameters():
                return False
        elif not self.name():
            return False
        return self.return_type() and self.tp("=>") and self.tm("BODY")

    def function_value(self):
        self.tw("async")
        if not self.tw("function"):
            return False
        self.tp("*")
        self.name()
        return self.signature_rest() and self.tm("BODY")

    def return_type(self):
        self.had_return = self.p(":")
        if not self.tp(":"):
            return True
        if self.tm("TYPE"):
            return True
        if self.attempt(self.predicate):
            return True
        return self.type_()

    def predicate(self):
        if self.tw("asserts"):
            if not (self.w("this") or self.is_("id")):
                return False
            self.i += 1
            return self.type_() if self.tw("is") else True
        return (self.tw("this") or self.name()) and self.tw("is") and self.type_()

    def signature_rest(self):
        """`<T>(params): R` of a function, method or call signature."""
        if self.p("<") and not self.type_parameters():
            return False
        if not self.parameters():
            return False
        typed = self.params_typed
        ok = self.return_type()
        self.params_typed = typed
        return ok

    def decorators(self):
        while self.p("@"):
            self.i += 1
            if not self.name():
                raise Refused("decorator")
            while self.p(".") and self.is_("id", k=1):
                self.i += 2
            if self.p("("):
                if not (self.tp("(") and self.tm("ARGS") and self.tp(")")):
                    raise Refused("decorator arguments")

    def property_name(self):
        if self.take("id") or self.take("str") or self.take("num"):
            return True
        if self.p("["):
            self.i += 1
            return self.dotted() and self.tp("]")
        return False

    # ---- members
    def named_member(self, in_class):
        """A property or method. A member named by a statement word (`delete`, `return`, `catch`) must show its types in full,
        so code such as `if (x);` or `return(x);` is not taken for one."""
        statement_word = self.is_("id") and self.tok()[1] in STATEMENT_WORDS
        if not self.property_name():
            return False
        if self.tm("WITHHELD"):
            return True
        if not self.tp("?") and in_class:
            self.tp("!")
        if self.p("(") or self.p("<"):
            if not self.signature_rest():
                return False
            return not statement_word or (self.params_typed and self.had_return)
        has_type = False
        if self.tp(":"):
            if not self.type_():
                return False
            has_type = True
        if in_class and self.tp("=") and not self.value():
            return False
        return not statement_word or has_type

    def class_member(self):
        if self.tm("WITHHELD"):
            return True
        self.decorators()
        while self.is_("id") and self.tok()[1] in CLASS_MODS and self.name_start(1):
            self.i += 1
        if self.p("[") and self.attempt(self.mapped_or_index):
            return True
        self.tp("*")
        return self.named_member(in_class=True)

    def enum_member(self):
        if self.tm("WITHHELD"):
            return True
        if not (self.take("id") or self.take("str")):
            return False
        if self.tm("WITHHELD") or self.tm("INIT"):
            return True
        if self.tp("="):
            if self.take("str") or self.take("num"):
                return True
            if self.p("-") and self.is_("num", k=1):
                self.i += 2
                return True
            return False
        return True

    # ---- declarations
    def decl(self):
        self.tick()
        if self.tm("WITHHELD"):
            return True
        self.decorators()
        while self.is_("id") and self.tok()[1] in DECL_MODS and self.is_("id", k=1):
            self.i += 1
        word = self.tok()[1] if self.is_("id") else None
        self.i += 1
        if word == "function":
            self.tp("*")
            if not self.name():
                return False
            return self.tm("WITHHELD") or self.signature_rest()
        if word == "const" and self.tw("enum"):
            return self.enum_declaration()
        if word in ("const", "let", "var"):
            if not self.name():
                return False
            if self.tm("WITHHELD"):
                return True
            self.tp("!")
            if self.tp(":") and not self.type_():
                return False
            return not self.tp("=") or self.value()
        if word == "class":
            return self.class_declaration()
        if word == "interface":
            if not self.name():
                return False
            if self.tm("WITHHELD"):
                return True
            if self.p("<") and not self.type_parameters():
                return False
            if self.tw("extends") and not self.heritage_list():
                return False
            return self.object_type()
        if word == "type":
            if not self.name():
                return False
            if self.tm("WITHHELD"):
                return True
            if self.p("<") and not self.type_parameters():
                return False
            return self.tp("=") and self.type_()
        if word == "enum":
            return self.enum_declaration()
        if word in ("namespace", "module"):
            if not (self.take("str") or self.dotted()):
                return False
            return self.tm("WITHHELD") or self.namespace_body()
        if word == "global":
            return self.namespace_body()
        return False

    def heritage_list(self):
        while True:
            if not (self.dotted() and self.type_arguments()):
                return False
            if not self.tp(","):
                return True

    def class_declaration(self):
        self.name()
        if self.tm("WITHHELD"):
            return True
        if self.p("<") and not self.type_parameters():
            return False
        if not self.tm("HERITAGE"):
            if self.tw("extends") and not (self.dotted() and self.type_arguments()):
                return False
            if self.tw("implements") and not self.heritage_list():
                return False
        if not self.tp("{"):
            return False
        while not self.p("}"):
            self.tick()
            if not (self.class_member() and self.tp(";")):
                return False
            self.tm("INIT")
        return self.tp("}")

    def enum_declaration(self):
        if not self.name():
            return False
        if self.tm("WITHHELD"):
            return True
        if not self.tp("{"):
            return False
        while not self.p("}"):
            self.tick()
            if not self.enum_member():
                return False
            if not self.tp(","):
                break
        return self.tp("}")

    def namespace_body(self):
        if not self.tp("{"):
            return False
        while not self.p("}"):
            self.tick()
            if not (self.tm("WITHHELD") or (self.w("export") and self.decl())):
                return False
            self.tp(";")
            self.tm("INIT")
        return self.tp("}")


RULES = {"decl": _Parser.decl, "class": _Parser.class_member, "interface": _Parser.type_member, "enum": _Parser.enum_member}


def _whole(text, rule):
    try:
        parser = _Parser(tokens(text))
        if not rule(parser):
            return False
        parser.tp(";")
        parser.tm("INIT")
        return parser.is_("end")
    except (Refused, _Cut, RecursionError):
        return False


def accepts(text, context):
    """True when `text` is one declaration of the given context and nothing else (see the module doc)."""
    return _whole(text, RULES[context])


def type_ok(text):
    """True when `text` is one type and nothing else."""
    return _whole(text, _Parser.type_)


def return_type_ok(text):
    """True when `text` is `: <type>` (a type predicate included) and nothing else."""
    return text.lstrip().startswith(":") and _whole(text, _Parser.return_type)
