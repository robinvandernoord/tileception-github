"""Curses checklist for composing a whitelist and blacklist."""

from __future__ import annotations

import curses
import os
from dataclasses import dataclass

AUTO, INCLUDE, EXCLUDE = 0, 1, 2
MARKS = {AUTO: "[ ]", INCLUDE: "[x]", EXCLUDE: "[-]"}
HELP = "Space/Tab: [ ] auto, [x] only, [-] never  ^A only shown  ^X reset all  Enter save  Esc cancel"


@dataclass
class Entry:
    key: str
    label: str
    note: str = ""

    def matches(self, query: str) -> bool:
        label = self.label.casefold()
        return not query or query in label or query in label.replace(" ", "")


def pick(title: str, entries: list[Entry], states: dict[str, int]) -> dict[str, int] | None:
    """Return each key's state (AUTO, INCLUDE or EXCLUDE), or None when cancelled."""
    if not entries:
        raise ValueError("Nothing to choose from")
    # Make Esc respond immediately instead of waiting for a possible escape sequence.
    os.environ.setdefault("ESCDELAY", "25")
    return curses.wrapper(Picker(title, entries, states).run)


class Picker:
    def __init__(self, title: str, entries: list[Entry], states: dict[str, int]):
        self.title = title
        self.entries = entries
        self.states = {entry.key: states.get(entry.key, AUTO) for entry in entries}
        self.query = ""
        self.cursor = 0
        self.top = 0

    def visible(self) -> list[Entry]:
        query = self.query.casefold()
        return [entry for entry in self.entries if entry.matches(query)]

    def run(self, screen) -> dict[str, int] | None:
        curses.curs_set(0)
        curses.use_default_colors()
        screen.keypad(True)
        while True:
            shown = self.visible()
            self.cursor = max(0, min(self.cursor, len(shown) - 1))
            self.draw(screen, shown)
            key = screen.get_wch()
            rows = max(1, screen.getmaxyx()[0] - 4)
            if key in ("\n", "\r", curses.KEY_ENTER):
                return self.states
            if key == "\x1b":
                return None
            if key in (" ", "\t") and shown:
                entry = shown[self.cursor].key
                self.states[entry] = (self.states[entry] + 1) % 3
            elif key == "\x01" and shown:
                everything = all(self.states[entry.key] == INCLUDE for entry in shown)
                for entry in shown:
                    self.states[entry.key] = AUTO if everything else INCLUDE
            elif key == "\x18":
                self.states = dict.fromkeys(self.states, AUTO)
            elif key == curses.KEY_UP:
                self.cursor -= 1
            elif key == curses.KEY_DOWN:
                self.cursor += 1
            elif key == curses.KEY_PPAGE:
                self.cursor -= rows
            elif key == curses.KEY_NPAGE:
                self.cursor += rows
            elif key == curses.KEY_HOME:
                self.cursor = 0
            elif key == curses.KEY_END:
                self.cursor = len(shown) - 1
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                self.query = self.query[:-1]
                self.cursor = 0
            elif key == "\x15":  # Ctrl-U clears the filter, as in a shell prompt.
                self.query = ""
                self.cursor = 0
            elif isinstance(key, str) and key.isprintable():
                self.query += key
                self.cursor = 0

    def draw(self, screen, shown: list[Entry]):
        height, width = screen.getmaxyx()
        screen.erase()
        rows = max(1, height - 4)
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + rows:
            self.top = self.cursor - rows + 1
        counts = [sum(state == wanted for state in self.states.values()) for wanted in (INCLUDE, EXCLUDE)]
        summary = f"{counts[0]} only, {counts[1]} never" if counts[0] else f"automatic, {counts[1]} never"
        self.line(screen, 0, f"{self.title} ({summary})", width, curses.A_BOLD)
        self.line(screen, 1, f"Filter: {self.query}", width)
        for row, entry in enumerate(shown[self.top : self.top + rows]):
            index = self.top + row
            mark = MARKS[self.states[entry.key]]
            suffix = f"  ({entry.note})" if entry.note else ""
            attributes = curses.A_REVERSE if index == self.cursor else 0
            self.line(screen, row + 2, f"{mark} {entry.label}{suffix}", width, attributes)
        if not shown:
            self.line(screen, 2, "Nothing matches the filter", width, curses.A_DIM)
        self.line(screen, height - 1, HELP, width, curses.A_DIM)
        screen.refresh()

    @staticmethod
    def line(screen, y: int, text: str, width: int, attributes: int = 0):
        try:
            # Writing the bottom-right cell raises after the text is drawn; nothing is lost.
            screen.addnstr(y, 0, text, max(0, width - 1), attributes)
        except curses.error:
            pass
