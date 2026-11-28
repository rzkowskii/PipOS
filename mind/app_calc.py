"""The calculator's transient input and ANSI picture for being-console."""

from dataclasses import dataclass

from .calc import KEYBOARD_CHARS, PANEL_TEXT, keyboard

_ENTRY_KEYS = b"0123456789.+-*/^() "
_GRID = 107
_DISPLAY = 84
_INNER = _DISPLAY - 4
_RESET = "\x1b[0m"
_BORDER = "\x1b[2;94m"
_KEYS = (
    ("Ctrl+L", "(", ")", "/"),
    ("7", "8", "9", "*"),
    ("4", "5", "6", "-"),
    ("1", "2", "3", "+"),
    ("0", ".", "Backspace", "Enter"),
)


@dataclass
class Calculator:
    entry: str = ""
    last: tuple[str, str, bool] | None = None

    def key(self, data):
        """Handle one existing hands key datagram. True if anything changed."""
        if data in (b"\r", b"\n", b"=") and self.entry.strip():
            value, error = keyboard(self.entry)
            self.last = (self.entry.strip(), value, error)
            self.entry = ""
        elif data in (b"\x08", b"\x7f") and self.entry:
            self.entry = self.entry[:-1]
        elif data in (b"\x0c", b"\x15") and (self.entry or self.last is not None):
            self.entry, self.last = "", None
        elif len(data) == 1 and data in _ENTRY_KEYS and len(self.entry) < KEYBOARD_CHARS:
            self.entry += data.decode("ascii")
        else:
            return False
        return True

    def show_spoken(self, expression, result, error):
        # Only bounded printable data enters a terminal's input stream.
        if type(error) is not bool or not isinstance(result, str) or not result.strip():
            return False
        if not all(isinstance(value, str) and len(value) <= PANEL_TEXT and
                   all(" " <= char <= "~" for char in value) for value in (expression, result)):
            return False
        updated = (expression, result, error)
        if updated == self.last:
            return False
        self.last = updated
        return True

    def render(self):
        """Draw the display and the keyboard guide without scrolling the panel."""
        out = ["\x1b[?25l"]  # the entry carries its own caret
        column = (_GRID - _DISPLAY) // 2 + 1

        def row(number, text):
            out.append(f"\x1b[{number};{column}H{text}{_RESET}")

        def display(number, text, ink, *, align=">"):
            field = f"{text:{align}{_INNER}}"
            row(number, f"{_BORDER}│{_RESET}\x1b[40;{ink}m {field} {_RESET}{_BORDER}│")

        row(2, f"{_BORDER}╭{'─' * (_DISPLAY - 2)}╮")
        display(3, "Enter a calculation", "90", align="<")
        display(4, f"› {self.entry}_", "1;97", align="<")
        expression, result, error = self.last or ("", "0", False)
        # The current edit, previous expression and result each own their
        # rows, so shorter values and Clear overwrite the previous pixels.
        for index in range(2):
            start, end = index * _INNER, (index + 1) * _INNER
            display(5 + index, expression[start:end], "90")
            display(7 + index, result[start:end], "1;93" if error else "1;96")
        row(9, f"{_BORDER}╰{'─' * (_DISPLAY - 2)}╯")
        row(11, "\x1b[90mKeyboard")
        width = (_DISPLAY - 3 * 4) // 4
        for number, keys in enumerate(_KEYS, 12):
            caps = []
            for key in keys:
                ink = "46;1;96" if key in {"+", "-", "*", "/", "Enter"} else "40;97"
                caps.append(f"\x1b[{ink}m{key:^{width}}{_RESET}")
            row(number, "    ".join(caps))
        row(18, "\x1b[90mEnter  Calculate    Ctrl+L  Clear    ^  Power    Esc  Close")
        row(19, "\x1b[90mYou can also say a sum.")
        return "".join(out).encode("utf-8")
