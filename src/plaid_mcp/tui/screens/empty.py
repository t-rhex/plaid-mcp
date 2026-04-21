"""Screen shown when there's no enrollment saved yet."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Middle
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

_MESSAGE = (
    "No enrollment saved.\n\n"
    "Run this to link a bank through Teller Connect:\n\n"
    "    plaid-mcp teller connect\n\n"
    "Then relaunch the TUI with:\n\n"
    "    plaid-mcp tui\n"
)


class EmptyScreen(Screen[None]):
    """Instructional placeholder for the 'no enrollment' state."""

    DEFAULT_CSS = """
    EmptyScreen #empty-body {
        align: center middle;
        height: 1fr;
    }
    EmptyScreen Static#empty-msg {
        padding: 1 2;
        border: round $accent;
        width: auto;
        text-align: center;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Middle(id="empty-body"):
            with Center():
                yield Static(_MESSAGE, id="empty-msg")
        yield Footer()
