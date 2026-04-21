"""Transactions screen — last 30 days by default."""

from __future__ import annotations

from datetime import date, timedelta

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ...providers import Capability, Enrollment, Provider, Transaction

_COLUMNS = ("Date", "Amount", "Merchant", "Category")
_DAYS = 30


def _fmt_amount(value: float) -> str:
    return f"{value:,.2f}"


def _merchant(tx: Transaction) -> str:
    return tx.merchant_name or tx.name or "—"


class TransactionsScreen(Screen[None]):
    """Scrollable table of the last 30 days of transactions."""

    DEFAULT_CSS = """
    TransactionsScreen {
        layout: vertical;
    }
    TransactionsScreen #tx-title {
        padding: 0 1;
        color: $text-muted;
    }
    TransactionsScreen #tx-error {
        padding: 0 1;
        color: $error;
        background: $error 15%;
        display: none;
    }
    TransactionsScreen #tx-error.-visible {
        display: block;
    }
    TransactionsScreen DataTable {
        height: 1fr;
    }
    TransactionsScreen #tx-footer {
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        provider: Provider,
        enrollment: Enrollment,
        days: int = _DAYS,
    ) -> None:
        super().__init__()
        self._provider = provider
        self._enrollment = enrollment
        self._days = days

    def compose(self) -> ComposeResult:
        end = date.today()
        start = end - timedelta(days=self._days)
        yield Header(show_clock=False)
        yield Static(
            f"Transactions — {start.isoformat()} → {end.isoformat()}",
            id="tx-title",
        )
        yield Static("", id="tx-error")
        table: DataTable[str] = DataTable(zebra_stripes=True, cursor_type="row")
        table.id = "tx-table"
        yield table
        yield Static("", id="tx-footer")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def _show_error(self, message: str) -> None:
        banner = self.query_one("#tx-error", Static)
        banner.update(f"⚠ {message}")
        banner.add_class("-visible")

    def _clear_error(self) -> None:
        banner = self.query_one("#tx-error", Static)
        banner.update("")
        banner.remove_class("-visible")

    def _refresh(self) -> None:
        self._clear_error()
        table = self.query_one("#tx-table", DataTable)
        footer = self.query_one("#tx-footer", Static)
        table.clear(columns=True)
        for column in _COLUMNS:
            table.add_column(column, key=column)

        txs = self._load_transactions()
        # Most recent first — providers don't guarantee sort order.
        txs.sort(key=lambda t: t.date, reverse=True)

        for tx in txs:
            table.add_row(
                tx.date or "—",
                _fmt_amount(tx.amount),
                _merchant(tx)[:60],
                tx.category or "—",
                key=tx.id,
            )

        footer.update(f"{len(txs)} transactions")

    def _load_transactions(self) -> list[Transaction]:
        caps = self._provider.capabilities()
        if Capability.TRANSACTIONS not in caps:
            self._show_error("Provider does not support transactions.")
            return []
        end = date.today()
        start = end - timedelta(days=self._days)
        try:
            return list(
                self._provider.get_transactions(
                    self._enrollment,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
            )
        except Exception as e:  # noqa: BLE001 - surface upstream error, don't crash
            self._show_error(f"Could not load transactions: {e}")
            return []
