"""Tests for the Textual TUI.

Uses Textual's ``App.run_test`` pilot to drive the app headlessly. A tiny
in-test provider stands in for Teller/Plaid so no HTTP calls happen.
"""

from __future__ import annotations

import pytest

from plaid_mcp.providers import (
    Account,
    Balance,
    Capability,
    Enrollment,
    Identity,
    Transaction,
)
from plaid_mcp.tui import PlaidMcpTUI
from plaid_mcp.tui.screens.accounts import AccountsScreen
from plaid_mcp.tui.screens.empty import EmptyScreen
from plaid_mcp.tui.screens.transactions import TransactionsScreen

# ---- fake provider --------------------------------------------------------


class FakeProvider:
    """Minimal Provider Protocol impl with canned return values."""

    name = "teller"

    def __init__(
        self,
        accounts: list[Account],
        balances: list[Balance] | None = None,
        transactions: list[Transaction] | None = None,
    ) -> None:
        self._accounts = accounts
        self._balances = balances or []
        self._transactions = transactions or []

    def capabilities(self) -> set[Capability]:
        return {
            Capability.ACCOUNTS,
            Capability.BALANCES,
            Capability.TRANSACTIONS,
            Capability.IDENTITY,
        }

    def begin_enrollment(self) -> dict:
        return {}

    def complete_enrollment(self, payload: dict) -> Enrollment:  # pragma: no cover
        raise NotImplementedError

    def remove_enrollment(self, enrollment: Enrollment) -> None:  # pragma: no cover
        return None

    def list_accounts(self, enrollment: Enrollment) -> list[Account]:
        return list(self._accounts)

    def get_balances(self, enrollment: Enrollment) -> list[Balance]:
        return list(self._balances)

    def get_transactions(
        self,
        enrollment: Enrollment,
        start_date: str,
        end_date: str,
        account_id: str | None = None,
    ) -> list[Transaction]:
        if account_id is None:
            return list(self._transactions)
        return [t for t in self._transactions if t.account_id == account_id]

    def get_identity(self, enrollment: Enrollment) -> list[Identity]:  # pragma: no cover
        return []

    def close(self) -> None:
        return None


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def enrollment() -> Enrollment:
    return Enrollment(
        id="enr_test",
        institution_id="ins_1",
        institution_name="Test Bank",
        access_token="test_token",
        provider="teller",
    )


@pytest.fixture
def accounts() -> list[Account]:
    return [
        Account(
            id="acc_1",
            enrollment_id="enr_test",
            name="Everyday Checking",
            official_name="Everyday Checking",
            type="depository",
            subtype="checking",
            mask="1234",
            iso_currency="USD",
        ),
        Account(
            id="acc_2",
            enrollment_id="enr_test",
            name="Rewards Card",
            official_name="Rewards Card",
            type="credit",
            subtype="credit card",
            mask="9876",
            iso_currency="USD",
        ),
    ]


@pytest.fixture
def balances() -> list[Balance]:
    return [
        Balance(
            account_id="acc_1",
            current=1234.56,
            available=1000.0,
            limit=None,
            iso_currency="USD",
        ),
        Balance(
            account_id="acc_2",
            current=-250.0,
            available=9750.0,
            limit=10_000.0,
            iso_currency="USD",
        ),
    ]


@pytest.fixture
def transactions() -> list[Transaction]:
    return [
        Transaction(
            id="tx_1",
            account_id="acc_1",
            amount=12.34,
            iso_currency="USD",
            date="2026-04-20",
            authorized_date=None,
            name="BLUE BOTTLE",
            merchant_name="Blue Bottle Coffee",
            category="food_and_drink",
            subcategory=None,
            pending=False,
            payment_channel="in_store",
        ),
        Transaction(
            id="tx_2",
            account_id="acc_2",
            amount=99.00,
            iso_currency="USD",
            date="2026-04-18",
            authorized_date=None,
            name="SOME STORE",
            merchant_name="Some Store",
            category="shopping",
            subcategory=None,
            pending=False,
            payment_channel="in_store",
        ),
    ]


# ---- tests ----------------------------------------------------------------


async def test_launches_empty_when_no_enrollment() -> None:
    app = PlaidMcpTUI(provider=None, enrollment=None)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, EmptyScreen)


async def test_renders_accounts_with_two_rows(
    enrollment: Enrollment,
    accounts: list[Account],
    balances: list[Balance],
) -> None:
    provider = FakeProvider(accounts=accounts, balances=balances)
    app = PlaidMcpTUI(provider=provider, enrollment=enrollment)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)
        from textual.widgets import DataTable

        table = app.screen.query_one("#accounts-table", DataTable)
        assert table.row_count == 2


async def test_pressing_t_switches_to_transactions(
    enrollment: Enrollment,
    accounts: list[Account],
    balances: list[Balance],
    transactions: list[Transaction],
) -> None:
    provider = FakeProvider(
        accounts=accounts, balances=balances, transactions=transactions
    )
    app = PlaidMcpTUI(provider=provider, enrollment=enrollment)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, AccountsScreen)
        await pilot.press("t")
        await pilot.pause()
        assert isinstance(app.screen, TransactionsScreen)

        from textual.widgets import DataTable

        table = app.screen.query_one("#tx-table", DataTable)
        assert table.row_count == 2


async def test_pressing_q_exits(
    enrollment: Enrollment,
    accounts: list[Account],
) -> None:
    provider = FakeProvider(accounts=accounts)
    app = PlaidMcpTUI(provider=provider, enrollment=enrollment)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        assert app._exit is True
