"""The one JSON writer of the scripts in this directory (called by tp_json_write in _common.sh; never run by hand).

usage: python3 -I json_record.py OUTPUT   (the document comes on stdin)

No script builds JSON by hand. A script passes its values here as NUL-separated tokens, which no file name or argument
can contain, so no value is ever interpolated into code or into JSON text. Every token is decoded with os.fsdecode
(UTF-8 with surrogateescape): a byte that is not UTF-8 becomes the lone surrogate \\udcXX, json.dumps writes it as an
escape, and lib/gate_record.py:as_bytes (os.fsencode) turns it back into the same byte.

The tokens are a list of fields, in the order they are written to the document:
  s KEY VALUE            a string
  r KEY JSON             a JSON literal the script computed itself (a number, true, false, null)
  a KEY N ITEM...        a list of N strings
  k KEY N (NAME STATUS EXIT SECONDS)...   a list of N gate checks; EXIT is empty for null, SECONDS is a JSON number
A KEY with dots (base.ref) is a field of an object.
"""
import json
import os
import sys


def parse(tokens):
    doc = {}
    pos = 0

    def take(count):
        nonlocal pos
        if pos + count > len(tokens):
            raise ValueError("document ends inside a field")
        got = tokens[pos:pos + count]
        pos += count
        return got

    while pos < len(tokens):
        kind, key = take(2)
        target = doc
        *parents, leaf = key.split(".")
        for part in parents:
            target = target.setdefault(part, {})
        if kind == "s":
            target[leaf] = take(1)[0]
        elif kind == "r":
            target[leaf] = json.loads(take(1)[0])
        elif kind == "a":
            target[leaf] = take(int(take(1)[0]))
        elif kind == "k":
            checks = []
            for _ in range(int(take(1)[0])):
                name, status, code, seconds = take(4)
                checks.append({"name": name, "status": status, "exit": int(code) if code != "" else None,
                               "seconds": json.loads(seconds)})
            target[leaf] = checks
        else:
            raise ValueError("unknown field kind: %r" % kind)
    return doc


def main(argv):
    if len(argv) != 2:
        sys.stderr.write("usage: json_record.py OUTPUT\n")
        return 2
    out = argv[1]
    tokens = [os.fsdecode(t) for t in sys.stdin.buffer.read().split(b"\0")]
    if tokens:
        tokens.pop()  # every token ends with a NUL, so the last piece is empty
    tmp = "%s.tmp.%d" % (out, os.getpid())
    try:
        text = json.dumps(parse(tokens)) + "\n"
        with open(tmp, "w", encoding="ascii") as handle:
            handle.write(text)
        os.replace(tmp, out)  # a reader never sees half of a record
    except (OSError, ValueError) as exc:
        sys.stderr.write("json_record.py: %s\n" % exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
