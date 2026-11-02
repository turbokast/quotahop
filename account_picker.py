"""Select a subscription by email; ask with an arrow-key picker when it is ambiguous.

Every account command takes the email that identifies the subscription. When
one email has several subscriptions (for example a personal Max plan and a
Team seat), the command raises Ambiguous with the candidates; the CLI shows a
picker on the terminal and retries with the chosen row. Without a terminal the
command refuses rather than guessing.
"""
import os
import sys

import account_identity


from claude_usage import UsageError


class Ambiguous(UsageError):
    """Several saved subscriptions match; carries them for a picker."""

    def __init__(self, candidates, message=None):
        self.candidates = list(candidates)
        super().__init__(message or f"This email matches {len(self.candidates)} subscriptions.")


class PlanNeeded(UsageError):
    """A new subscription row needs its plan; carries the allowed plans."""

    def __init__(self, plans, message=None):
        self.plans = list(plans)
        super().__init__(message or "Choose the subscription type for this new account.")


def emails(account, entries):
    """Lower-cased email/name labels that identify a saved row."""
    values = {account["name"].casefold()}
    values.update(v.casefold() for v in account_identity.saved(entries.get(account["id"], {})).values()
                  if isinstance(v, str))
    return values


def match(accounts, entries, selector, plan=None):
    """The one row for this email (or exact internal ID); Ambiguous for several."""
    candidates = [a for a in accounts if plan is None or a["plan"] == plan]
    found = ([a for a in candidates if a["id"] == selector]
             or [a for a in candidates if selector.casefold() in emails(a, entries)])
    if not found:
        raise UsageError("No saved subscription uses that email. Check the email in the report, or add it with --add EMAIL.")
    if len(found) > 1:
        raise Ambiguous(found)
    return found[0]


def describe(account, others=()):
    from claude_usage import PLANS
    label = f"{account['name']} · {PLANS[account['plan']]}"
    if sum(o["plan"] == account["plan"] for o in others) > 1:
        label += f" ({account['id']})"
    return label


def interactive():
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def _keys(fd):
    """Yield 'up', 'down', 'enter' or 'cancel' from a raw terminal."""
    while True:
        data = os.read(fd, 8)
        if not data:
            yield "cancel"
            return
        if data in (b"\r", b"\n"):
            yield "enter"
        elif data in (b"\x1b[A", b"\x1bOA", b"k"):
            yield "up"
        elif data in (b"\x1b[B", b"\x1bOB", b"j"):
            yield "down"
        elif data in (b"\x1b", b"q", b"\x03", b"\x04"):
            yield "cancel"


def choose_index(title, labels, keys, write):
    """Pure picker loop: returns the chosen index, or None when cancelled."""
    index = 0
    lines = 0

    def draw():
        nonlocal lines
        out = "\x1b[F\x1b[2K" * lines if lines else ""
        rows = [title] + [("\x1b[7m❯ " if i == index else "  ") + label + ("\x1b[0m" if i == index else "")
                          for i, label in enumerate(labels)] + ["\x1b[2m↑/↓ move · Enter choose · Esc cancel\x1b[0m"]
        out += "".join("\x1b[2K" + row + "\r\n" for row in rows)
        lines = len(rows)
        write(out)
    draw()
    for key in keys:
        if key == "enter":
            return index
        if key == "cancel":
            return None
        index = (index - 1) % len(labels) if key == "up" else (index + 1) % len(labels)
        draw()
    return None


def pick(title, labels):
    """Ask on the controlling terminal; raises UsageError when cancelled."""
    import termios
    import tty
    from claude_usage import UsageError
    fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        os.write(fd, b"\x1b[?25l")  # hide cursor while choosing
        choice = choose_index(title, labels, _keys(fd), lambda text: os.write(fd, text.encode()))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        os.write(fd, b"\x1b[?25h")
        os.close(fd)
    if choice is None:
        raise UsageError("Cancelled; nothing changed.")
    return choice


def run(action, selector, *, picker=None, is_interactive=None):
    """Call action(selector, plan) and resolve ambiguity by asking.

    action raises Ambiguous or PlanNeeded when it cannot decide; the answer is
    fed back as an exact account ID or a plan. At most a few rounds.
    """
    from claude_usage import PLANS, UsageError
    picker = picker or pick
    is_interactive = interactive if is_interactive is None else is_interactive
    plan = None
    for _ in range(3):
        try:
            return action(selector, plan)
        except Ambiguous as exc:
            options = exc.candidates
            if not is_interactive():
                raise UsageError(f"{exc} Run this in a terminal to choose: "
                                 + "; ".join(describe(a, options) for a in options) + ".") from None
            chosen = options[picker(f"{exc} Which one?", [describe(a, options) for a in options])]
            selector, plan = chosen["id"], chosen["plan"]
        except PlanNeeded as exc:
            if not is_interactive():
                raise UsageError(f"{exc} Run this in a terminal to choose: "
                                 + ", ".join(PLANS[p] for p in exc.plans) + ".") from None
            plan = exc.plans[picker(str(exc), [PLANS[p] for p in exc.plans])]
    raise UsageError("Could not resolve the subscription; nothing changed.")
