"""claude_mate_ui -- the arrow-key chooser, in one place.

WHY THIS IS A MODULE AND NOT THREE COPIES. The top level of `claude-mate` got
arrow keys and the levels underneath it did not, so choosing an account still
meant reading numbers off a list and typing one. That is the complaint this file
answers, and the only way to answer it once is to have one chooser that every
level calls -- the app's submenus, `claude-mate accounts rm`, and the picker the
wrapper shows every time you start a session.

IT IS ALSO A BUG FIX, not only a nicer surface. The wrapper's picker asked you
to TYPE a profile name, and an arrow key types an escape sequence -- so pressing
Up at that prompt created a profile literally called "\\033", which renders as a
blank row with somebody's email beside it and cannot be typed back to delete it.
A real accounts directory on the maintainer's machine had one, and a "в" from a
keyboard left in the wrong layout. A chooser you cannot typo into cannot do that.

Imported by path-adjacent scripts (bin/claude-mate, bin/claude-mate-wrap), so it
is deliberately dependency-free and safe to import when there is no terminal at
all: everything here degrades to None/False rather than raising, and callers are
expected to have a typed fallback for that case.
"""
import os
import re
import select
import sys
import termios
import tty
import unicodedata

ESC = "\x1b"

B, D, OFF = "\033[1m", "\033[2m", "\033[0m"
C_SEL = "\033[36m"          # the cursor row
C_WARN = "\033[31m"
C_OK = "\033[32m"
# Blanked off a terminal, like every other file here does. The chooser will not
# run in that case anyway -- supports_ui() says no -- but a caller is free to
# use these constants for its own printing, and none of this project's output
# puts escape codes down a pipe.
if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
    B = D = OFF = C_SEL = C_WARN = C_OK = ""


def supports_ui():
    """Can we drive a full-screen chooser here?

    BOTH ENDS, for the same reason the app checks both: a test harness run from
    a developer's terminal inherits a TTY stdin while its stdout is a pipe, and
    CI redirects `< /dev/null` while stdout is a pipe too. Either check alone
    picks the wrong answer on a machine that matters.
    """
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
CSI_KEYS = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end", "5~": "pgup", "6~": "pgdn", "3~": "delete",
}

# READ THE DESCRIPTOR, NOT sys.stdin, and keep our own buffer. select() answers
# about the KERNEL's buffer while sys.stdin.read() fills PYTHON's, so an arrow
# key -- three bytes in one burst -- leaves two of them where select cannot see
# them, the follow-up poll says "nothing more", and every arrow decodes as a
# bare ESC. os.read() has no layer to hide bytes in.
_KEYBUF = bytearray()


def _fill(fd, timeout):
    if not select.select([fd], [], [], timeout)[0]:
        return False
    try:
        chunk = os.read(fd, 64)
    except OSError:
        return False
    _KEYBUF.extend(chunk if chunk else b"\x00")     # b"" is EOF
    return True


def read_key(fd, timeout=None):
    """One keypress as a name, or None on timeout.

    ESC IS AMBIGUOUS: it is both a key someone pressed and the first byte of
    every arrow. Only time separates them -- a terminal sends a sequence in one
    burst, and nobody types ESC then [ within milliseconds -- so a lone ESC is
    declared only after a short grace with nothing behind it.
    """
    if not _KEYBUF and not _fill(fd, timeout):
        return None
    b = _KEYBUF.pop(0)
    if b == 0x00:
        return "eof"
    if b != 0x1B:
        if b in (0x0D, 0x0A):
            return "enter"
        if b == 0x09:
            return "tab"
        if b == 0x7F:
            return "backspace"
        if b == 0x03:
            return "ctrl-c"
        if b < 0x20:
            return "ctrl"
        return chr(b)
    if not _KEYBUF and not _fill(fd, 0.05):
        return "esc"
    second = chr(_KEYBUF.pop(0))
    if second == "O":                       # SS3: tmux and app-cursor mode
        if not _KEYBUF and not _fill(fd, 0.05):
            return "esc"
        return CSI_KEYS.get(chr(_KEYBUF.pop(0)), "esc")
    if second != "[":
        return "esc"
    body = ""
    for _ in range(8):
        if not _KEYBUF and not _fill(fd, 0.05):
            break
        c = chr(_KEYBUF.pop(0))
        body += c
        if c.isalpha() or c == "~":
            break
    return CSI_KEYS.get(body, "esc")


# --------------------------------------------------------------------------- #
# The screen
# --------------------------------------------------------------------------- #
class Term:
    """cbreak + the alternate screen, with a guaranteed way back.

    cbreak rather than raw: it leaves ISIG alone so ctrl-C still interrupts, and
    OPOST alone so ordinary print() from a program we hand the terminal to does
    not stair-step. The alternate screen rather than a clear, so the scrollback
    someone was reading is still there afterwards.
    """

    def __init__(self):
        self.fd = sys.stdin.fileno()
        self.saved = None

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        sys.stdout.write("\033[?1049h\033[?25l")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        self.restore()
        return False

    def restore(self):
        """Idempotent: called from the exit path AND before handing over."""
        if self.saved is None:
            return
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        self.saved = None
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()

    def size(self):
        """Columns and rows, with a floor under both.

        A terminal can honestly report 0x0 -- that is what a pty allocated
        without a winsize does, which is what pty.openpty() and every
        script-style harness hands you. With rows=0 a paint that slices
        lines[:rows-1] silently drops the last line of every frame.
        """
        try:
            sz = os.get_terminal_size()
            cols, rows = sz.columns, sz.lines
        except OSError:
            cols = rows = 0
        return max(cols, 20), max(rows, 6)

    def paint(self, lines):
        """Home, then clear-to-end-of-line per row. Never a full ESC[2J.

        Erasing the whole screen and redrawing it is what makes a repainting
        view flicker.
        """
        cols, rows = self.size()
        out = ["\033[H"]
        for line in lines[:rows - 1]:
            # EVERY ROW ERASED, A NOTE'S OWN ROWS TOO. A line may carry its own
            # breaks (a two-row note), and an erase only at its end left the
            # previous screen's text on the rows before it: a chat's cwd ran on
            # into the title of the row drawn there a moment ago.
            out.append(line[:cols + 200].replace("\n", "\033[K\r\n")
                       + "\033[K\r\n")
        out.append("\033[J")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# The chooser
# --------------------------------------------------------------------------- #
class Item:
    """One row. `value` is what pick() gives back when it is chosen."""

    def __init__(self, label, value=None, detail="", danger=False, gap=False):
        self.label = label
        self.value = label if value is None else value
        self.detail = detail
        self.danger = danger
        self.gap = gap          # draw a blank line above this row


def pick(title, items, term=None, hint=None, note=None, initial=0,
         digits=True):
    """Choose one item with the arrow keys. Returns its value, or None.

    None means backed out -- ESC, q, or ctrl-C. Every level treats that the
    same way, which is the point: one chooser, one set of keys, so "how do I
    get out of here" has one answer everywhere in this program.

    `term` lets a caller that is already holding the screen (the app) paint
    into it; without one, this manages its own and hands the terminal back
    exactly as it found it.

    `digits=False` turns the number shortcuts off -- for a menu where one
    keystroke must not be able to do something that cannot be taken back.
    """
    if not items:
        return None
    own = term is None
    if own:
        if not supports_ui():
            return None
        term = Term()
        term.__enter__()
    try:
        sel = max(0, min(initial, len(items) - 1))
        while True:
            cols, rows = term.size()
            lines, whole = _layout(title, items, sel, hint, note, cols, rows)
            term.paint(lines)
            key = read_key(term.fd)
            if key in ("q", "esc", "ctrl-c", "eof", None):
                return None
            if key == "up":
                sel = (sel - 1) % len(items)
            elif key == "down":
                sel = (sel + 1) % len(items)
            elif key == "home":
                sel = 0
            elif key == "end":
                sel = len(items) - 1
            elif key == "enter":
                return items[sel].value
            elif (digits and whole and key and len(key) == 1
                  and key in "0123456789"):
                # NUMBERS STILL WORK. Muscle memory from the old typed picker
                # is real, and a chooser that punished it would be a downgrade
                # for the people who used the thing most. But ONLY WHILE EVERY
                # ROW IS ON THE SCREEN: once the list scrolls, `3` would choose
                # a row nobody can see -- the very thing the scrolling is there
                # to stop -- and never where the caller said no (confirm()).
                # ASCII DIGITS, NOT str.isdigit(): read_key hands a UTF-8 key
                # over a byte at a time, and the second byte of a Cyrillic в, г
                # or й is ², ³ or ¹ -- digits to isdigit(), a ValueError to
                # int(), and a traceback for a keyboard left in that layout.
                n = int(key)
                if 0 <= n < len(items):
                    return items[n].value
    finally:
        if own:
            term.restore()


def printable(s):
    """Anything that came from a profile name, made safe to print.

    THIS IS NOT COSMETIC. Profile names are directory names, and one of them on
    a real machine is literally "\\033" -- so its *path* carries a raw ESC too.
    Printing that into a terminal does not show a funny character, it starts an
    escape sequence: the bytes after it are eaten as a control code and the
    screen is left in whatever state they asked for. Every string this module
    renders from data goes through here.
    """
    if not s:
        return ""
    return "".join(c if (c.isprintable() or c == " ") else
                   f"\\x{ord(c):02x}" for c in str(s))


# --------------------------------------------------------------------------- #
# Fitting a line to the terminal's width
# --------------------------------------------------------------------------- #
# EVERY SCREEN HERE PAINTS ONE TERMINAL ROW PER LINE (Term.paint: home, then
# each line and an erase), so a line wider than the terminal is not just cut
# off -- it wraps, every row below it moves down one, and once they pass the
# bottom the terminal scrolls: the header and the cursor row go off the top,
# and Enter acts on a row nobody can see. So the lines that carry data are
# fitted here, in COLUMNS: colour codes take none, an East Asian wide character
# two. They live beside the chooser because pick() fits its lines with them as
# much as the app's top level does; bin/claude-mate imports them -- the
# machinery, never the colours.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _cells(ch):
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0                              # combining marks, joiners
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def vis_width(text):
    """The columns `text` takes on a terminal."""
    return sum(_cells(ch) for ch in _ANSI.sub("", text))


def clip(text, room):
    """`text` in at most `room` columns. A cut ends in '…' -- so '3/12' cut
    short cannot pass for a whole '3/1' -- and in a colour reset, so the
    colour of the half that went cannot run on into the next line. Colour
    codes are kept, and cost nothing."""
    if vis_width(text) <= room:
        return text
    out, used, i = [], 0, 0
    while i < len(text):
        m = _ANSI.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = _cells(text[i])
        if used + w > room - 1:
            break
        out.append(text[i])
        used += w
        i += 1
    kept = "".join(out)
    mark = "" if (room <= 0 or _ANSI.sub("", kept).endswith("…")) else "…"
    return kept + mark + OFF


def _height(lines):
    """Terminal rows `lines` take. A note may carry its own line breaks
    (menu_one_account's does), and each one is a row like any other."""
    return sum(ln.count("\n") + 1 for ln in lines)


def _fit(line, room):
    """clip(), a row at a time when the line carries its own breaks -- and the
    same string back, byte for byte, when every row already fits."""
    return "\n".join(clip(part, room) for part in line.split("\n"))


def _span(heights, gaps, lo, hi):
    """Rows the items [lo, hi) take drawn as a window: a '↑ N more' or
    '↓ N more' line while something is hidden on that side, and no gap line at
    the top -- the '↑' line already sets the window apart."""
    return (sum(heights[lo:hi]) - (1 if lo > 0 and gaps[lo] else 0)
            + (1 if lo > 0 else 0) + (1 if hi < len(heights) else 0))


def _window(heights, gaps, sel, room):
    """Which items to draw, [lo, hi), in `room` rows -- `sel` always among them.

    Grown one item below, then one above, in turn, so the cursor sits in the
    middle of what is drawn and a step moves the view by a row rather than
    jumping a page."""
    n = len(heights)
    lo, hi = sel, sel + 1
    grew = True
    while grew:
        grew = False
        if hi < n and _span(heights, gaps, lo, hi + 1) <= room:
            hi, grew = hi + 1, True
        if lo > 0 and _span(heights, gaps, lo - 1, hi) <= room:
            lo, grew = lo - 1, True
    return lo, hi


def _render(title, items, sel, hint, note, cols=None, rows=None):
    """The chooser's lines. With `cols` and `rows` (pick() passes the
    terminal's) they are also FITTED to it -- see _layout(). Without them it is
    the same bytes it always was, which is what every caller drew before the
    history existed, the wrapper's account picker among them."""
    return _layout(title, items, sel, hint, note, cols, rows)[0]


def _layout(title, items, sel, hint, note, cols=None, rows=None):
    """(the lines to paint, whether every item is among them).

    A LIST LONGER THAN THE SCREEN USED TO LOSE ITS CURSOR. Term.paint keeps the
    first rows-1 lines and drops the rest, so the rows past the bottom -- the
    cursor among them, once you had arrowed far enough -- were simply not drawn,
    and Enter chose a row nobody could see. Nothing here had that many rows
    until the history, which keeps every chat until you delete it. So when the
    whole list does not fit, a WINDOW of it is drawn around the cursor, with a
    dim '↑ N more' / '↓ N more' for what is off screen; the title, the note and
    the hint stay where they are. When it all fits, these are the lines it
    always drew -- a line that fits comes back from clip() untouched -- except
    that a label of wide characters is padded by the columns it takes.
    """
    head = [f" {B}{title}{OFF}", ""]
    labels = [printable(i.label) for i in items]
    # Padded in COLUMNS once there is a width to fit, so a wide-character label
    # does not push its detail off the edge; in characters without one, as it
    # always was (the two agree for every label without wide characters).
    size = len if cols is None else vis_width
    width = max((size(x) for x in labels), default=0)
    blocks = []                   # one per item: its gap line, then its row
    for i, it in enumerate(items):
        cur = i == sel
        label = labels[i]
        mark = f"{C_SEL}{B}▸{OFF}" if cur else " "
        col = C_WARN if it.danger else (C_SEL if cur else "")
        body = f"{col}{B}{label}{OFF}" if cur else f"{col}{label}{OFF}"
        pad = " " * max(0, width - size(label))
        det = printable(it.detail)
        detail = f"  {D}{det}{OFF}" if det else ""
        row = f" {mark} {body}{pad}{detail}"
        blocks.append(["", row] if it.gap else [row])
    tail = [""]
    if note:
        tail += [f" {note}", ""]
    tail.append(f" {D}{hint or '↑↓ move   ⏎ choose   esc back'}{OFF}")

    lo, hi = 0, len(items)
    gaps = [bool(it.gap) for it in items]
    if rows is not None and items:
        heights = [len(b) for b in blocks]
        room = rows - 1 - _height(head) - _height(tail)
        if sum(heights) > room:
            if _span(heights, gaps, sel, sel + 1) > room:
                # TOO SHORT EVEN FOR THE CURSOR'S ROW: the blank lines go
                # first, before anything that says something.
                head, tail = head[:1], [ln for ln in tail if ln]
                room = rows - 1 - _height(head) - _height(tail)
            lo, hi = _window(heights, gaps, sel, room)
    shown = []
    if lo > 0:
        shown.append(f"   {D}↑ {lo} more{OFF}")
    for i in range(lo, hi):
        top_gap = i == lo and lo > 0 and gaps[i]      # the '↑' line stands in
        shown += blocks[i][1:] if top_gap else blocks[i]
    if hi < len(items):
        shown.append(f"   {D}↓ {len(items) - hi} more{OFF}")
    out = head + shown + tail
    if cols is not None:
        out = [_fit(ln, cols - 1) for ln in out]
    return out, (lo == 0 and hi == len(items))


def confirm(title, question, term=None, danger=True, note=None):
    """A yes/no, as a chooser rather than a typed y/N.

    Same keys as everything else, and No is the cursor's first position so a
    reflexive Enter cannot destroy anything. NO DIGITS: with the number
    shortcuts on, `1` answered YES to a destructive question without the cursor
    ever leaving No -- the reflex this chooser exists to stop, one key over.
    `note` is for what the question is about (which chat, which account), drawn
    above it.
    """
    items = [Item("No, leave it alone", value=False),
             Item("Yes", value=True, danger=danger)]
    got = pick(title, items, term=term, hint=question, note=note, digits=False)
    return got is True
