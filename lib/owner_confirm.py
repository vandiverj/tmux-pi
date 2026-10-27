"""The owner at a terminal: the same three checks tp-trust makes before a grant, for other commands only a person may run.

An agent runs its commands without a terminal and with TP_TASK_ID, TP_TASK_DIR or TP_MODE set, so it is refused before it is
asked; a person types back a random code read from /dev/tty. Every check is one `if` carrying a `check:<name>` comment marker."""
import os
import secrets
import sys

WORDS = ("amber", "birch", "cedar", "delta", "ember", "fjord", "grove", "harbor", "ivory", "jasper",
         "kelp", "lunar", "maple", "nectar", "onyx", "pearl", "quartz", "raven", "sable", "tundra",
         "umber", "velvet", "willow", "yarrow", "zephyr", "basalt", "coral", "dune", "flint", "glacier",
         "heron", "indigo")


class Refused(ValueError):
    pass


def _clean(text, limit=300):
    out = "".join(c if " " <= c <= "~" else "?" for c in str(text))
    return out if len(out) <= limit else out[:limit] + "..."


def confirm(what):
    """Return when the owner typed the code back; raise Refused otherwise. `what` is shown above the code."""
    for name in ("TP_TASK_ID", "TP_TASK_DIR", "TP_MODE"):
        if name in os.environ:                                                         # check:task_env
            raise Refused(f"{name} is set: this looks like an agent session; the owner runs this command in their own terminal")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):                               # check:stdin_tty
        raise Refused("stdin and stdout must be a terminal; the owner runs this command in their own terminal")
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        raise Refused("no controlling terminal (/dev/tty); the owner runs this command in their own terminal")
    try:
        code = "-".join(secrets.choice(WORDS) for _ in range(4))
        os.write(fd, (f"\n{_clean(what, 2000)}\nTerminal: {_clean(os.ttyname(fd))}   Directory: {_clean(os.getcwd())}\n"
                      f"Type this code to confirm, anything else cancels:\n  {code}\n> ").encode())
        buf = b""
        while not buf.endswith(b"\n") and len(buf) <= 512:
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
        if not secrets.compare_digest(buf.decode("utf-8", "replace").strip().encode(), code.encode()):  # check:code
            raise Refused("the code typed back did not match; nothing was changed")
    finally:
        os.close(fd)
