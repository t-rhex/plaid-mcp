"""Accounts screen — one row per account with balances joined in."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ...providers import Account, Balance, Capability, Enrollment, Provider

_COLUMNS = ("Institution", "Name", "Type", "Mask", "Current", "Available")


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}"


def _fmt_type(acct: Account) -> str:
    if acct.type and acct.subtype:
        return f"{acct.type}/{acct.subtype}"
    return acct.type or acct.subtype or "—"


class AccountsScreen(Screen[None]):
    """Table of accounts for the current enrollment."""

    DEFAULT_CSS = """
    AccountsScreen {
        layout: vertical;
    }
    AccountsScreen #accounts-title {
        padding: 0 1;
        color: $text-muted;
    }
    AccountsScreen #accounts-error {
        padding: 0 1;
        color: $error;
        background: $error 15%;
        display: none;
    }
    AccountsScreen #accounts-error.-visible {
        display: block;
    }
    AccountsScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(self, provider: Provider, enrollment: Enrollment) -> None:
        super().__init__()
        self._provider = provider
        self._enrollment = enrollment

    def compose(self) -> ComposeResult:
        institution = self._enrollment.institution_name or "(unknown institution)"
        yield Header(show_clock=False)
        yield Static(f"Accounts — {institution}", id="accounts-title")
        yield Static("", id="accounts-error")
        table: DataTable[str] = DataTable(zebra_stripes=True, cursor_type="row")
        table.id = "accounts-table"
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#accounts-error", Static)
        banner.update(f"⚠ {message}")
        banner.add_class("-visible")

    def _clear_error(self) -> None:
        banner = self.query_one("#accounts-error", Static)
        banner.update("")
        banner.remove_class("-visible")

    def _refresh(self) -> None:
        self._clear_error()
        table = self.query_one("#accounts-table", DataTable)
        table.clear(columns=True)
        for column in _COLUMNS:
            table.add_column(column, key=column)

        try:
            accounts = list(self._provider.list_accounts(self._enrollment))
        except Exception as e:  # noqa: BLE001 - surface upstream error, don't crash
            self._show_error(f"Could not load accounts: {e}")
            return

        balances_by_id = self._load_balances(accounts)
        institution = self._enrollment.institution_name or "—"

        for acct in accounts:
            bal = balances_by_id.get(acct.id)
            table.add_row(
                institution,
                acct.name or acct.official_name or "—",
                _fmt_type(acct),
                f"••{acct.mask}" if acct.mask else "—",
                _fmt_money(bal.current if bal else None),
                _fmt_money(bal.available if bal else None),
                key=acct.id,
            )

    def _load_balances(self, accounts: list[Account]) -> dict[str, Balance]:
        caps = self._provider.capabilities()
        if Capability.BALANCES not in caps:
            return {}
        try:
            balances = self._provider.get_balances(self._enrollment)
        except Exception as e:  # noqa: BLE001 - network can fail; keep table usable
            self._show_error(f"Balances unavailable: {e}")
            return {}
        return {b.account_id: b for b in balances}
