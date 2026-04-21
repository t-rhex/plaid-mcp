"""Top-level Textual app: header + footer + pushable screens.

The provider and enrollment are passed in at construction; the app itself
doesn't know how to build them — that's the CLI's job (see ``__main__.tui``)
so the TUI stays testable with mocks.

The Connect screen is a special case: it's the one entry point that *does*
mutate state (a new enrollment → a new provider). When it dismisses with an
``Enrollment``, we rebuild the provider via the injectable ``provider_factory``
hook and re-mount the accounts screen, so the user never has to restart.
"""

from __future__ import annotations

from collections.abc import Callable

from textual.app import App
from textual.binding import Binding

from ..config import Config
from ..providers import Enrollment, Provider
from .screens.accounts import AccountsScreen
from .screens.connect import ConnectScreen
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
        Binding("c", "connect", "Connect"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(
        self,
        *,
        provider: Provider | None,
        enrollment: Enrollment | None,
        config: Config | None = None,
        provider_factory: Callable[[Enrollment], Provider] | None = None,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._enrollment = enrollment
        self._config = config
        # Factory used to rebuild the provider after a new enrollment lands.
        # Injected by ``__main__.tui_cmd``; tests pass a lambda that returns
        # a FakeProvider. If not provided, the existing provider is reused.
        self._provider_factory = provider_factory

        provider_name = provider.name if provider is not None else "none"
        institution = (
            enrollment.institution_name if enrollment is not None else None
        ) or "(no enrollment)"
        self.title = "plaid-mcp"
        self.sub_title = f"{provider_name} · {institution}"

    @property
    def _has_data(self) -> bool:
        return self._provider is not None and self._enrollment is not None

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

    def action_connect(self) -> None:
        # If we have no Config we can't run the flow — happens only when a
        # test constructs the app without passing ``config``. No-op quietly.
        if self._config is None:
            return
        # Don't stack multiple Connect screens.
        if isinstance(self.screen, ConnectScreen):
            return
        self.push_screen(ConnectScreen(self._config), self._on_connect_result)

    def action_refresh(self) -> None:
        # Each screen owns its own `action_refresh`; forward if present.
        screen = self.screen
        refresh = getattr(screen, "action_refresh", None)
        if callable(refresh):
            refresh()

    # ---- connect callback ----------------------------------------------

    def _on_connect_result(self, enrollment: Enrollment | None) -> None:
        """Callback from ``ConnectScreen.dismiss(...)``.

        On success, swap to the accounts screen with the new enrollment.
        On cancel/None, leave the user wherever they were.
        """
        if enrollment is None:
            return
        self._enrollment = enrollment
        if self._provider_factory is not None:
            # Close the previous provider (if any) before replacing it —
            # Teller has an httpx client; leaking sockets on repeat link
            # would be noisy.
            close = getattr(self._provider, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
            self._provider = self._provider_factory(enrollment)

        # Update the header sub-title so the newly-linked institution is
        # visible immediately.
        provider_name = self._provider.name if self._provider else "none"
        self.sub_title = (
            f"{provider_name} · "
            f"{enrollment.institution_name or '(unknown institution)'}"
        )

        if self._has_data:
            self._swap_to(self._make_accounts())

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
