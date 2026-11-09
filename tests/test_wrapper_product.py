"""FG-N1 rounds 4 and 5: wrappers, option forms and payloads, judged by both detectors. Every read assertion is enabled.

The cases are built from the rows of lib/command_wrappers.SPECS, so a wrapper added to the table is covered without an edit.

  depth 1   every wrapper x every option the table lists (each flag, each valued option in each spelling, `--`) and one option no
            wrapper has x every way the wrapper is given a command (words, a remote line, each string option in each spelling) x
            every payload                                                             (full product)
  depth 2   outer wrapper x option form x way x inner wrapper x option form x way x payload. The full product is ~76 million cases,
            far over the 2,000,000 the brief allows, so (round 5, rule 5, second branch):
              - the full product over (outer wrapper x inner wrapper x payload), and
              - an all-pairs design over the seven dimensions (both wrappers, both option-form kinds, both ways, the payload):
                every pair of values that can occur together appears in at least one case, asserted by the test itself.
  depth 3   every wrapper triple x no options x each wrapper's first way x two payloads (full product)

A payload that is a search or a read must be both a search (redact_hook.bash_searches) and sensitive (sensitive_read.classify). A
command line a wrapper hands on (a string, a remote command) that cannot be read for certain is both, too. A command that is
harmless and is not handed on as text is neither. An option the table does not list is both, at any depth and with any payload.

The cases are judged by four forked workers; a failure lists the first commands that were judged wrong. Nothing here opens a real
credential file: the one credential name is created empty in a temp tree.
"""
import session_env  # noqa: F401  first: removes the caller's session, however the suite is started
import itertools
import sys
from collections import Counter
import multiprocessing
import re
import shlex
import time
import unittest

import test_wrapper_lists as base

table, redact_hook, sr = base.table, base.redact_hook, base.sr

WORKERS = 4
UNKNOWN_OPTION = "-@"  # an option no wrapper has
OPERAND = "OPERAND"
FULL_PRODUCT_LIMIT = 2_000_000  # the brief: run the full depth-two product only up to this many cases

# payload name -> (text, only a quoted string can carry it).
# Searches and reads: judged as such wherever they are.
SEARCHES = {
    "direct": ("rg --hidden TOKEN .", False),
    "nested sh -c": ("sh -c 'rg --hidden TOKEN .'", False),
    "nested sh -cx": ("sh -cx 'rg --hidden TOKEN .'", False),
    "nested bash -lc": ('bash -lc "rg --hidden TOKEN ."', False),
    "nested twice": ("sh -c \"bash -c 'rg --hidden TOKEN .'\"", False),
    "nested eval": ("eval 'rg --hidden TOKEN .'", False),
    "docker exec": ("docker exec c rg --hidden TOKEN .", False),
    "kubectl exec": ("kubectl exec p -- rg --hidden TOKEN .", False),
    "find -exec": ("find . -exec rg --hidden TOKEN . {} +", False),
    "substitution": ("$(echo rg) --hidden TOKEN .", False),  # `echo rg` is read, so this runs `rg --hidden TOKEN .`
    "tabs": ("rg\t--hidden\tTOKEN\t.", True),
    "second line": ("ls\nrg --hidden TOKEN .", True),
}
# The shell that runs a handed-on line picks the command: a search always; a read when a wrapper handed the line on as text.
UNRESOLVED = {
    "variable": ("$REMOTE_COMMAND --hidden TOKEN .", False),
    "bare variable": ("$REMOTE_COMMAND", False),
    "nested sh -c": ('sh -c "$REMOTE_COMMAND"', False),
    "nested sh -cx": ('sh -cx "$REMOTE_COMMAND"', False),
    "nested twice": ("sh -c 'sh -c \"$REMOTE_COMMAND\"'", False),
    "nested eval": ("eval $REMOTE_COMMAND", False),
    "positional": ("$1", False),
    "all arguments": ("$@", False),
    "find -exec sh -c": ('find . -exec sh -c "$REMOTE_COMMAND" \\;', False),
}
# No search, no credential: neither detector fires when the words are the wrapper's own...
HARMLESS = {
    "echo": ("echo hi", False),
    "nested sh -c": ("sh -c 'echo hi'", False),
    "two commands": ("echo hi; echo there", False),
}
# ...and both do when a wrapper handed them on as text that cannot be read for certain: an expansion, a shell with anything but
# `-c` before its string, a program that runs other words.
HARMLESS_UNCERTAIN = {
    "variable argument": ("echo $HOME", False),
    "status argument": ("echo $?", False),
    "nested sh -cx": ("sh -cx 'echo hi'", False),
    "docker exec": ("docker exec c echo hi", False),
    "docker exec sh -c": ('docker exec c sh -c "$REMOTE_COMMAND"', False),
    "find -exec": ("find . -exec echo hi {} +", False),
}
ALL = (("search", SEARCHES), ("unresolved", UNRESOLVED), ("harmless", HARMLESS), ("uncertain", HARMLESS_UNCERTAIN))


def form_kind(form):
    label, words, _, _ = form
    if label in ("none", "--", "unknown"):
        return label
    if label.startswith("flag"):
        return "flag"
    return "valued apart" if len(words) == 2 else "valued =" if "=" in words[0] else "valued attached"


def way_kind(way):
    kind, flag = way
    if not kind.startswith("string"):
        return kind
    if flag.startswith("--"):
        return "string long =" if kind.endswith("=STR") else "string long apart"
    return "string short apart" if kind.endswith(" STR") else "string short attached"


def option_forms(name):
    """(label, words, operands the option stands in for, ends-with-dashes) for every way to give `name` options: none, `--`, each
    flag, each valued option (apart, attached, `=`)."""
    spec = table.SPECS[name]
    forms = [("none", [], 0, False), ("--", ["--"], 0, True)]
    forms += [("flag " + flag, [flag], 0, False) for flag in sorted(spec.flags)]
    for flag in sorted(spec.valued - spec.string):
        filled = int(flag in spec.fills)
        if flag.startswith("--"):
            forms += [("valued " + flag + "=v", [flag + "=v"], filled, False), ("valued " + flag + " v", [flag, "v"], filled, False)]
        else:
            forms.append(("valued " + flag + " v", [flag, "v"], filled, False))
            if len(flag) == 2:  # a one-letter option takes its value attached; `-arch` is a word of its own
                forms.append(("valued " + flag + "v", [flag + "v"], filled, False))
    return forms


UNKNOWN_FORM = ("unknown", [UNKNOWN_OPTION], 0, False)


def ways(name):
    """How `name` is given a command: words, a remote line (as words and as one string), or a string option in each spelling."""
    spec = table.SPECS[name]
    found = []
    if spec.rest:
        found += [("rest words", None), ("rest string", None)]
    elif spec.words:
        found.append(("words", None))
    for flag in sorted(spec.string):
        if flag.startswith("--"):
            found += [("string " + flag + "=STR", flag), ("string " + flag + " STR", flag)]
        else:
            found += [("string " + flag + " STR", flag), ("string " + flag + "STR", flag)]
    return found


def invoke(name, form, way, command):
    """The text of `name` with these options, given `command` this way."""
    _, options, filled, dashes = form
    kind, flag = way
    if kind == "rest words" and re.search("['\"]", command):
        kind = "rest string"  # a remote or `watch` line given as words is the words joined: quotes inside it would be gone
    spec = table.SPECS[name]
    operands = [OPERAND] * max(spec.operands - filled, 0)
    quoted = shlex.quote(command)
    lead = [name] + [word for word in options if word != "--"]
    if kind.startswith("string"):
        if flag.startswith("--"):
            lead += [flag + "=" + quoted] if kind.endswith("=STR") else [flag, quoted]
        elif kind.endswith(" STR"):
            lead += [flag, quoted]
        else:
            lead += [shlex.quote(flag + command)]
        return " ".join(lead + (["--"] if dashes else []) + operands)
    lead += ["--"] if dashes else []
    return " ".join(lead + operands + [quoted if kind == "rest string" else command])


def is_string(way):
    """Whether a command line is handed on this way (a `-c` string or a remote/`watch` line) rather than the command as words."""
    return way[0] != "words"


# Programs that run the words after them (the brief's list): inside text a wrapper hands on, each is uncertain.
DISPATCHERS = {"xargs", "nsenter", "chroot", "parallel", "watch", "eval", "exec", "command", "builtin"}
OWN = set(table.OWN)  # ssh and xargs: judged in a branch of their own, not replaced by the command they run


def xargs_runs_a_bulk_program(levels, text):
    """Whether some xargs of the stack runs a reader or a shell, which with no file list before it is a bulk read by design
    (sensitive_read._BULK): the levels after it are looked through up to a string (a shell reads it), an ssh or another xargs
    (judged on their own), and the payload's first word is what runs at the end."""
    for j, (name, _, _) in enumerate(levels):
        if name != "xargs":
            continue
        for later, _, way in levels[j + 1:]:
            if later == "ssh":
                return True  # ssh is among the readers: run by xargs it is a bulk read (sensitive_read._READERS)
            if later in OWN:
                break  # another xargs: judged on its own
            if is_string(way):
                return True
        else:
            if text.split()[0] in sr._BULK:
                return True
    return False


def expected(levels, kind, text):
    """(search, read) wanted for `text` of payload `kind` under these levels [(name, form, way)].

    A line a wrapper hands on is uncertain when it holds an exec-style dispatcher: a wrapper of the stack inside a level that hands
    its command on as text. xargs also reads in bulk whatever reader or shell it runs when no file list feeds it."""
    if any(form[0] == "unknown" for _, form, _ in levels):
        want = (True, True)  # where the command starts cannot be told: a search and a read
    else:
        handed = any(is_string(way) for _, _, way in levels)
        inside = any(is_string(levels[j][2]) and levels[k][0] in DISPATCHERS for j in range(len(levels)) for k in range(j + 1, len(levels)))
        want = {"search": (True, True), "unresolved": (True, handed), "harmless": (inside, inside),
                "uncertain": (handed, handed)}[kind]
    if want[1] is not True and any(name == "xargs" for name, _, _ in levels):
        want = (want[0], xargs_runs_a_bulk_program(levels, text))
    return want


def takes_string_by_option(name, words):
    """Whether one of `words` (the option words of a wrapper that stands after `name`) is read as `name`'s own command-string
    option: the floor finds a wrapper's string option among all the words after it, by its letter (`-c`, `-qc`, `-cv`) or its long
    name. That is the over-approximation the floor is built on (a stray `-c` of `exec -c` after `flock L` is read as flock's)."""
    spec = table.SPECS[name]
    letters = {flag[1] for flag in spec.string if not flag.startswith("--")}
    longs = {flag for flag in spec.string if flag.startswith("--")}
    return any((word.partition("=")[0] in longs) if word.startswith("--") else
               (len(word) > 1 and word.startswith("-") and bool(letters & set(word[1:]))) for word in words)


def stray(levels):
    """Whether an option of a later level looks like the command-string option of an earlier one (reported to Main: a false
    positive of the table-free floor). Such stacks are left out of the product; their number is counted and printed."""
    return any(takes_string_by_option(levels[j][0], levels[m][1][1]) for j in range(len(levels)) for m in range(j + 1, len(levels)))


def stray_payload(levels, kind, text):
    """Whether, with no wrapper handing on its command as text, an option of the payload looks like the command-string option of a
    wrapper of the stack (`flock L sh -c "$X"`: the floor reads the payload's `-c` as flock's own and judges the next word as text
    that cannot be checked). Only payloads that hold something uncertain can show it."""
    if kind not in ("unresolved", "uncertain") or any(is_string(way) for _, _, way in levels):
        return False
    return any(takes_string_by_option(name, text.split()) for name, _, _ in levels)


def build(levels, text):
    command = text
    for name, form, way in reversed(levels):
        command = invoke(name, form, way, command)
    return command


def payloads(levels, groups, strays=False):
    """(label, kind, text) for each payload of `groups` that the innermost way can carry; with `strays` the ones that are left out
    as stray (see stray_payload), which are reported and counted instead."""
    last_string = is_string(levels[-1][2])
    for kind, table_ in groups:
        for label, (text, string_only) in table_.items():
            if string_only and not last_string:
                continue  # a newline starts another command: only a quoted string can carry one
            if stray_payload(levels, kind, text) == strays:
                yield label, kind, text


def cases(stacks, groups):
    """(command, wanted search, wanted read, label) for every stack x every payload."""
    for levels in stacks:
        for label, kind, text in payloads(levels, groups):
            search, read = expected(levels, kind, text)
            yield build(levels, text), search, read, "%s | %s %s" % (" > ".join(n + ":" + f[0] + ":" + w[0] for n, f, w in levels), kind, label)


def judge(case):
    command, search, read, label = case
    got_search, got_read = redact_hook.bash_searches(command), sr.classify("Bash", {"command": command}, cwd=TREE)["sensitive"]
    if got_search != search or got_read != read:
        return "%s: search=%s read=%s, wanted %s %s: %r" % (label, got_search, got_read, search, read, command)
    return None


def judge_all(items):
    """Judge every case; the number judged and the first wrong ones."""
    count, wrong = 0, []
    pool = multiprocessing.get_context("fork").Pool(WORKERS)
    try:
        for result in pool.imap_unordered(judge, items, chunksize=400):
            count += 1
            if result and len(wrong) < 15:
                wrong.append(result)
    finally:
        pool.terminate()
        pool.join()
    return count, wrong


# ---------------------------------------------------------------------------------------------------------------------------
# Depth two: the size of the full product, and the all-pairs design used when it is too big.
# ---------------------------------------------------------------------------------------------------------------------------
NAMES = sorted(table.SPECS)
FORMS = {name: option_forms(name) + [UNKNOWN_FORM] for name in NAMES}
WAYS = {name: ways(name) for name in NAMES}
FORM_KINDS = {name: sorted({form_kind(form) for form in FORMS[name]}) for name in NAMES}
WAY_KINDS = {name: sorted({way_kind(way) for way in WAYS[name]}) for name in NAMES}
PAYLOADS = {"%s: %s" % (kind, label): (kind, text, string_only)
            for kind, group in ALL for label, (text, string_only) in group.items()}
DIMENSIONS = ("outer", "outer form", "outer way", "inner", "inner form", "inner way", "payload")


def full_product_size():
    """The number of depth-two cases of the full product: outer x option form x way, inner x option form x way, x payload."""
    level = sum(len(FORMS[name]) * len(WAYS[name]) for name in NAMES)
    string_ways = sum(len(FORMS[name]) * sum(is_string(way) for way in WAYS[name]) for name in NAMES)
    string_only = sum(1 for _, _, only in PAYLOADS.values() if only)
    return level * (string_ways * len(PAYLOADS) + (level - string_ways) * (len(PAYLOADS) - string_only))


class Rotation:
    """The concrete option form / way of a wrapper for a kind: the next one in turn, so over the design every listed option and
    spelling is used at depth two as well as at depth one."""

    def __init__(self):
        self.next = {}

    def pick(self, key, choices):
        i = self.next.get(key, 0)
        self.next[key] = i + 1
        return choices[i % len(choices)]


def concrete(outer, outer_kind, outer_way, inner, inner_kind, inner_way, payload, rotation):
    """The stack for these values: the next concrete form of each kind, an inner form that is not read as the outer's string option
    when there is one; None when every form of that kind is."""
    outer_form = rotation.pick((outer, outer_kind), [f for f in FORMS[outer] if form_kind(f) == outer_kind])
    outer_way_ = rotation.pick((outer, outer_way), [w for w in WAYS[outer] if way_kind(w) == outer_way])
    inner_forms = [f for f in FORMS[inner] if form_kind(f) == inner_kind and not takes_string_by_option(outer, f[1])]
    inner_forms = [f for f in inner_forms if not (inner == outer and takes_string_by_option(inner, f[1]))]
    if not inner_forms:
        return None
    inner_form = rotation.pick((inner, inner_kind, outer), inner_forms)
    inner_way_ = rotation.pick((inner, inner_way), [w for w in WAYS[inner] if way_kind(w) == inner_way])
    levels = [(outer, outer_form, outer_way_), (inner, inner_form, inner_way_)]
    kind, text, _ = PAYLOADS[payload]
    return None if stray(levels) or stray_payload(levels, kind, text) else levels


def valid_value(outer, outer_kind, outer_way, inner, inner_kind, inner_way, payload):
    """Whether these seven values can be built into one case."""
    kind, text, string_only = PAYLOADS[payload]
    return (outer_kind in FORM_KINDS[outer] and outer_way in WAY_KINDS[outer] and inner_kind in FORM_KINDS[inner]
            and inner_way in WAY_KINDS[inner] and not (string_only and inner_way == "words")
            and any(not takes_string_by_option(outer, f[1]) for f in FORMS[inner] if form_kind(f) == inner_kind))


def required_pairs():
    """Every pair of values (dimension, value) x (dimension, value) that some valid case holds. Worked out per pair of wrappers,
    from what each wrapper offers, not from the cases below."""
    required = set()
    for outer, inner in itertools.product(NAMES, repeat=2):
        offers = [
            [outer], FORM_KINDS[outer], WAY_KINDS[outer], [inner],
            sorted({form_kind(f) for f in FORMS[inner] if not takes_string_by_option(outer, f[1])}), WAY_KINDS[inner],
            [name for name, (_, _, only) in PAYLOADS.items() if not only or any(way != "words" for way in WAY_KINDS[inner])]]
        for (a, left), (b, right) in itertools.combinations(enumerate(offers), 2):
            for x, y in itertools.product(left, right):
                if (a, b) == (5, 6) and PAYLOADS[y][2] and x == "words":
                    continue  # a newline starts another command: only a quoted string can carry one
                required.add(((a, x), (b, y)))
    return required


def pairs_of(values):
    return {((a, values[a]), (b, values[b])) for a, b in itertools.combinations(range(len(values)), 2)}


def candidates(fixed):
    """Seven-value vectors holding the `fixed` values {dimension: value}, the free ones filled in turn, each with the value that
    keeps the case valid and holds the most pairs not yet covered (the greedy step of the design)."""
    pools = {0: NAMES, 3: NAMES, 1: sorted({k for n in NAMES for k in FORM_KINDS[n]}), 4: sorted({k for n in NAMES for k in FORM_KINDS[n]}),
             2: sorted({k for n in NAMES for k in WAY_KINDS[n]}), 5: sorted({k for n in NAMES for k in WAY_KINDS[n]}), 6: list(PAYLOADS)}
    order = [0, 3, 6, 2, 5, 1, 4]  # wrappers first: they decide which kinds exist
    return pools, order


def all_pairs_design():
    """(design, stacks left out as stray, required pairs, covered pairs). The first pass is the full product over (outer, inner,
    payload) with the option kinds and ways turning along; then, for each pair still missing, a greedy pass adds the case that holds
    it and covers the most other missing pairs."""
    required, rotation, design, state = required_pairs(), Rotation(), [], {"left_out": 0}
    covered = set()

    def add(values):
        stack = concrete(*values, rotation)
        if stack is None:
            return False
        design.append((stack, values[6]))
        covered.update(pairs_of(values))
        return True

    turn = 0
    for outer, inner in itertools.product(NAMES, repeat=2):
        for payload in PAYLOADS:
            if PAYLOADS[payload][2] and WAY_KINDS[inner] == ["words"]:
                continue  # a quoted string needs an inner that takes one
            turn += 1
            # every combination of option kinds and ways of the two wrappers, in an order that turns with the case; the first
            # that is valid and not stray is the case
            options = [(a, b, c, d) for a in FORM_KINDS[outer] for b in WAY_KINDS[outer] for c in FORM_KINDS[inner] for d in WAY_KINDS[inner]]
            start = turn * 7
            for k in range(len(options)):
                values = [outer, options[(start + k) % len(options)][0], options[(start + k) % len(options)][1], inner,
                          options[(start + k) % len(options)][2], options[(start + k) % len(options)][3], payload]
                if valid_value(*values) and add(values):
                    break
            else:
                state["left_out"] += 1  # no stack of these two wrappers can carry this payload without being stray

    pools, order = candidates({})
    for pair in sorted(required - covered, key=str):
        if pair in covered:
            continue
        (a, x), (b, y) = pair
        vector = [None] * 7
        vector[a], vector[b] = x, y
        for d in order:
            if vector[d] is not None:
                continue
            best, best_score = None, -1
            for candidate in pools[d]:
                vector[d] = candidate
                if not _partial_valid(vector):
                    continue
                score = len(pairs_of_partial(vector) - covered)
                if score > best_score:
                    best, best_score = candidate, score
            vector[d] = best
            if best is None:
                break
        if None not in vector and valid_value(*vector):
            add(vector)
    return design, state["left_out"], required, covered


def pairs_of_partial(values):
    return {((a, values[a]), (b, values[b])) for a, b in itertools.combinations(range(len(values)), 2)
            if values[a] is not None and values[b] is not None}


def _partial_valid(values):
    """The constraints that can be checked with some values still unknown (None)."""
    outer, outer_kind, outer_way, inner, inner_kind, inner_way, payload = values
    if outer and outer_kind and outer_kind not in FORM_KINDS[outer]:
        return False
    if outer and outer_way and outer_way not in WAY_KINDS[outer]:
        return False
    if inner and inner_kind and inner_kind not in FORM_KINDS[inner]:
        return False
    if inner and inner_way and inner_way not in WAY_KINDS[inner]:
        return False
    if inner_way == "words" and payload and PAYLOADS[payload][2]:
        return False
    if inner and payload and PAYLOADS[payload][2] and WAY_KINDS[inner] == ["words"]:
        return False
    if outer and inner and inner_kind and not any(not takes_string_by_option(outer, f[1]) for f in FORMS[inner] if form_kind(f) == inner_kind):
        return False
    return True


# Depth-one cases left out as stray (stray_payload), by payload: the floor reads the `-c` of the payload as the string option of
# flock / su / script and judges the next word as text that cannot be checked, so these report more than the plain command would.
STRAY_DEPTH_ONE = {"nested sh -c": 103, "nested sh -cx": 206, "nested twice": 103, "find -exec sh -c": 103,
                   "docker exec sh -c": 103, "find -exec": 103}

TREE = None  # the throwaway tree of the running class; the forked workers inherit it


class ProductCase(base.WrapperTreeCase):
    @classmethod
    def setUpClass(cls):
        global TREE
        super().setUpClass()
        TREE = cls.tree

    def run_product(self, stacks, groups):
        """Judge every stack x every payload; the count judged is the size of the product, which is checked against its factors."""
        started = time.monotonic()
        items = list(cases(stacks, groups))
        count, wrong = judge_all(items)
        self.assertEqual(wrong, [], "%d judged, in %.1fs" % (count, time.monotonic() - started))
        self.assertEqual(count, sum(1 for levels in stacks for _ in payloads(levels, groups)))  # nothing sampled
        return count

    @staticmethod
    def left_out(stacks, groups):
        """The payloads left out as stray, by label (see stray_payload)."""
        return Counter(label for levels in stacks for label, _, _ in payloads(levels, groups, strays=True))


class FullProduct(ProductCase):
    def test_depth_one_every_wrapper_every_option_every_way_every_payload(self):
        stacks = [[(name, form, way)] for name in NAMES for form in FORMS[name] for way in WAYS[name]]
        self.assertEqual(len(stacks), sum(len(FORMS[name]) * len(WAYS[name]) for name in NAMES))
        count, left_out = self.run_product(stacks, ALL), self.left_out(stacks, ALL)
        self.assertGreater(len(stacks), 1500)
        print("depth one: %d judged, %d left out as stray: %s" % (count, sum(left_out.values()), dict(left_out)), file=sys.stderr)
        # the stray cases are a decision for Main (see the module docstring): their number is pinned so that a change shows
        self.assertEqual(dict(left_out), STRAY_DEPTH_ONE)

    def test_depth_two_full_product_is_too_big_so_the_design_is_used(self):
        # the brief's branch: the full product only up to 2,000,000 cases
        self.assertGreater(full_product_size(), FULL_PRODUCT_LIMIT, "the full depth-two product fits: run it all")

    def test_depth_two_every_outer_inner_payload(self):
        # the full product over (outer wrapper x inner wrapper x payload); the option kinds and ways turn with it
        design, _, _, _ = all_pairs_design()
        triples = {(stack[0][0], stack[1][0], payload) for stack, payload in design}
        every = {(o, i, p) for o, i in itertools.product(NAMES, repeat=2) for p in PAYLOADS
                 if not (PAYLOADS[p][2] and WAY_KINDS[i] == ["words"])}  # a quoted string needs an inner that takes one
        self.assertEqual(every - triples, set())

    def test_depth_two_all_pairs_design_holds_every_pair_of_the_seven_dimensions(self):
        design, left_out, required, covered = all_pairs_design()
        self.assertEqual(sorted(required - covered, key=str), [], "pairs of values no case holds")
        self.assertGreater(len(required), 5000)  # (the size of what is asserted)
        items = []
        for levels, payload in design:
            kind, text, _ = PAYLOADS[payload]
            search, read = expected(levels, kind, text)
            items.append((build(levels, text), search, read, "%s | %s" % (" > ".join(n + ":" + f[0] + ":" + w[0] for n, f, w in levels), payload)))
        started = time.monotonic()
        count, wrong = judge_all(items)
        self.assertEqual(wrong, [], "%d judged, in %.1fs; %d stacks left out as stray" % (count, time.monotonic() - started, left_out))
        self.assertEqual(count, len(design))
        self.assertGreater(count, 30000)
        print("depth two: full product %d cases (limit %d); design %d cases holding %d pairs of the seven dimensions; %d (outer, inner,"
              " payload) left out" % (full_product_size(), FULL_PRODUCT_LIMIT, count, len(required), left_out), file=sys.stderr)
        self.assertEqual(left_out, 0)

    def test_depth_three_every_wrapper_triple(self):
        groups = (("search", {k: v for k, v in SEARCHES.items() if k in ("direct", "nested sh -c")}),)
        none = {name: FORMS[name][0] for name in NAMES}
        stacks = [[(name, none[name], WAYS[name][0]) for name in triple] for triple in itertools.product(NAMES, repeat=3)]
        self.assertEqual(self.run_product(stacks, groups), 2 * len(NAMES) ** 3)


if __name__ == "__main__":
    unittest.main()
