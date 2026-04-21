"""Top-level Textual app: header + footer + pushable screens.

The provider and enrollment are passed in at construction; the app itself
doesn't know how to build them — that's the CLI's job (see ``__main__.tui``)
so the TUI stays testable with mocks.
"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from ..providers import Enrollment, Provider
from .screens.accounts import AccountsScreen
from .screens.empty import EmptyScreen
from .screens.transactions import TransactionsScreen


class PlaidMcpTUI(App[None]):
    """Browse accounts and transactions for the active provider."""

    CSS = """
    Screen {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("a", "show_accounts", "Accounts"),
        Binding("t", "show_transactions", "Transactions"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        *,
        provider: Provider | None,
        enrollment: Enrollment | None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._enrollment = enrollment
        self._has_data = provider is not None and enrollment is not None

        provider_name = provider.name if provider is not None else "none"
        institution = (
            enrollment.institution_name if enrollment is not None else None
        ) or "(no enrollment)"
        self.title = "plaid-mcp"
        self.sub_title = f"{provider_name} · {institution}"

    def on_mount(self) -> None:
        if self._has_data:
            self.push_screen(self._make_accounts())
        else:
            self.push_screen(EmptyScreen())

    # ---- actions --------------------------------------------------------

    def action_show_accounts(self) -> None:
        if not self._has_data:
            return
        self._swap_to(self._make_accounts())

    def action_show_transactions(self) -> None:
        if not self._has_data:
            return
        self._swap_to(self._make_transactions())

    def action_refresh(self) -> None:
        # Each screen owns its own `action_refresh`; forward if present.
        screen = self.screen
        refresh = getattr(screen, "action_refresh", None)
        if callable(refresh):
            refresh()

    # ---- helpers --------------------------------------------------------

    def _swap_to(self, screen) -> None:
        # Pop back to a single-screen stack before pushing, so the Escape/back
        # chain stays sane even after many tab-switches.
        while len(self.screen_stack) > 1:
            self.pop_screen()
        self.push_screen(screen)

    def _make_accounts(self) -> AccountsScreen:
        assert self._provider is not None and self._enrollment is not None
        return AccountsScreen(self._provider, self._enrollment)

    def _make_transactions(self) -> TransactionsScreen:
        assert self._provider is not None and self._enrollment is not None
        return TransactionsScreen(self._provider, self._enrollment)
