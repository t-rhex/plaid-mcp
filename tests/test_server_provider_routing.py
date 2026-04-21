"""Provider-routing tests for the MCP tool layer.

Exercises the FastMCP server through its in-memory Client so the tests run
over the same request/response path a real MCP client would use. We swap
``build_provider`` (for Teller) and ``get_client`` (for Plaid) with fakes
so nothing touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from plaid_mcp.providers import (
    Account,
    Balance,
    Capability,
    Enrollment,
    Identity,
    Transaction,
)

# ---------- Fakes ----------------------------------------------------------


class FakeTellerProvider:
    """Minimal Provider impl for Teller-side routing tests.

    Matches ``providers.base.Provider`` at the call sites the server uses,
    returning canned data sliced from the dicts passed to the constructor.
    """

    name = "teller"

    def __init__(
        self,
        *,
        accounts: list[Account] | None = None,
        balances: list[Balance] | None = None,
        transactions: list[Transaction] | None = None,
        identities: list[Identity] | None = None,
    ) -> None:
        self._accounts = accounts or []
        self._balances = balances or []
        self._transactions = transactions or []
        self._identities = identities or []
        self.closed = False

    def capabilities(self) -> set[Capability]:
        return {
            Capability.ACCOUNTS,
            Capability.BALANCES,
            Capability.TRANSACTIONS,
            Capability.IDENTITY,
        }

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
        txs = list(self._transactions)
        if account_id:
            txs = [t for t in txs if t.account_id == account_id]
        return [t for t in txs if start_date <= t.date <= end_date]

    def get_identity(self, enrollment: Enrollment) -> list[Identity]:
        return list(self._identities)

    def close(self) -> None:
        self.closed = True


# ---------- Env fixtures ---------------------------------------------------


@pytest.fixture
def teller_env(monkeypatch, tmp_path):
    """Flip the autouse _env into Teller mode with a fake saved enrollment.

    The ``plaid-mcp`` Teller CLI reads ``~/.plaid-mcp/teller/enrollment.json``;
    we redirect ``_ENROLL_PATH`` at that location under ``tmp_path`` so the
    test doesn't touch the developer's real home directory.
    """
    monkeypatch.setenv("PROVIDER", "teller")
    monkeypatch.setenv("TELLER_APPLICATION_ID", "app_test_123")
    monkeypatch.setenv("TELLER_ENV", "sandbox")

    enroll_path = tmp_path / "enrollment.json"
    from plaid_mcp import teller_cli

    monkeypatch.setattr(teller_cli, "_ENROLL_PATH", enroll_path)
    return enroll_path


def _write_enrollment(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "id": "enr_1",
                "institution_id": "ins_teller_test",
                "institution_name": "Teller Test Bank",
                "access_token": "teller_access_token",
                "provider": "teller",
            }
        )
    )


def _install_fake_teller(monkeypatch, fake: FakeTellerProvider) -> None:
    """Replace ``build_provider`` so the server picks up our fake instead of
    constructing a real ``TellerProvider`` (which would try to speak HTTP)."""
    from plaid_mcp import server as server_mod

    def _factory(config, storage=None):
        return fake

    monkeypatch.setattr(server_mod, "build_provider", _factory)


# ---------- Helpers --------------------------------------------------------


def _data(result):
    """FastMCP structured-content unwrap: tools that return lists end up in
    result.data, but some transports place the payload under ``structured_content``."""
    if hasattr(result, "data") and result.data is not None:
        return result.data
    return getattr(result, "structured_content", None)


# ---------- Tests: Teller-backed, provider-agnostic tools ------------------


async def test_list_accounts_tool_routes_to_teller(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    fake = FakeTellerProvider(
        accounts=[
            Account(
                id="teller_acct_1",
                enrollment_id="enr_1",
                name="Checking",
                official_name="Primary Checking",
                type="depository",
                subtype="checking",
                mask="0001",
                iso_currency="USD",
            )
        ]
    )
    _install_fake_teller(monkeypatch, fake)

    from plaid_mcp.server import build_server

    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool("list_accounts_tool", {})

    data = _data(result)
    assert isinstance(data, list)
    assert len(data) == 1
    acct = data[0]
    assert acct["account_id"] == "teller_acct_1"
    assert acct["institution_name"] == "Teller Test Bank"
    assert acct["name"] == "Checking"
    assert acct["mask"] == "0001"
    # Legacy "item_id" key is populated from the enrollment id for
    # backward compat with MCP clients that expect it.
    assert acct["item_id"] == "enr_1"


async def test_get_balances_tool_routes_to_teller(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    fake = FakeTellerProvider(
        accounts=[
            Account(
                id="teller_acct_1",
                enrollment_id="enr_1",
                name="Checking",
                official_name="Checking",
                type="depository",
                subtype="checking",
                mask="0001",
                iso_currency="USD",
            )
        ],
        balances=[
            Balance(
                account_id="teller_acct_1",
                current=123.45,
                available=100.00,
                limit=None,
                iso_currency="USD",
            )
        ],
    )
    _install_fake_teller(monkeypatch, fake)

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool("get_balances_tool", {})

    data = _data(result)
    assert len(data) == 1
    bal = data[0]
    assert bal["current"] == 123.45
    assert bal["available"] == 100.00
    assert bal["institution_name"] == "Teller Test Bank"
    assert bal["name"] == "Checking"


async def test_get_transactions_tool_fetches_live_for_teller(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    fake = FakeTellerProvider(
        accounts=[
            Account(
                id="teller_acct_1",
                enrollment_id="enr_1",
                name="Checking",
                official_name=None,
                type="depository",
                subtype="checking",
                mask="0001",
                iso_currency="USD",
            )
        ],
        transactions=[
            Transaction(
                id="tx_a",
                account_id="teller_acct_1",
                amount=5.25,
                iso_currency="USD",
                date="2026-04-10",
                authorized_date=None,
                name="COFFEE",
                merchant_name="Blue Bottle",
                category="food_and_drink",
                subcategory=None,
                pending=False,
                payment_channel=None,
            ),
            Transaction(
                id="tx_b",
                account_id="teller_acct_1",
                amount=50.00,
                iso_currency="USD",
                date="2026-04-15",
                authorized_date=None,
                name="GROCERIES",
                merchant_name="Whole Foods",
                category="food_and_drink",
                subcategory=None,
                pending=False,
                payment_channel=None,
            ),
        ],
    )
    _install_fake_teller(monkeypatch, fake)

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool(
            "get_transactions_tool",
            {"start_date": "2026-04-01", "end_date": "2026-04-30"},
        )

    data = _data(result)
    assert {r["transaction_id"] for r in data} == {"tx_a", "tx_b"}
    # Merchant filter should apply in-Python for Teller results.
    async with Client(build_server()) as client:
        filtered = await client.call_tool(
            "get_transactions_tool",
            {
                "start_date": "2026-04-01",
                "end_date": "2026-04-30",
                "merchant": "Blue Bottle",
            },
        )
    filtered_data = _data(filtered)
    assert [r["transaction_id"] for r in filtered_data] == ["tx_a"]


async def test_search_transactions_tool_teller(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    fake = FakeTellerProvider(
        accounts=[
            Account(
                id="teller_acct_1",
                enrollment_id="enr_1",
                name=None,
                official_name=None,
                type=None,
                subtype=None,
                mask=None,
                iso_currency=None,
            )
        ],
        transactions=[
            Transaction(
                id="tx_a",
                account_id="teller_acct_1",
                amount=5.25,
                iso_currency=None,
                date="2026-04-10",
                authorized_date=None,
                name="COFFEE",
                merchant_name="Blue Bottle",
                category=None,
                subcategory=None,
                pending=False,
                payment_channel=None,
            ),
        ],
    )
    _install_fake_teller(monkeypatch, fake)

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool(
            "search_transactions_tool",
            {
                "query": "blue bottle",
                "start_date": "2026-04-01",
                "end_date": "2026-04-30",
            },
        )
    data = _data(result)
    assert len(data) == 1
    assert data[0]["transaction_id"] == "tx_a"


async def test_get_identity_tool_teller(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    fake = FakeTellerProvider(
        accounts=[
            Account(
                id="teller_acct_1",
                enrollment_id="enr_1",
                name="Checking",
                official_name="Primary Checking",
                type="depository",
                subtype="checking",
                mask="0001",
                iso_currency="USD",
            )
        ],
        identities=[
            Identity(
                account_id="teller_acct_1",
                names=["Alex Example"],
                emails=["alex@example.com"],
                phones=["+15555551234"],
                addresses=[{"city": "SF", "region": "CA"}],
            )
        ],
    )
    _install_fake_teller(monkeypatch, fake)

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool("get_identity_tool", {})
    data = _data(result)
    assert "identities" in data
    ident = data["identities"][0]
    assert ident["account_id"] == "teller_acct_1"
    assert ident["owners"][0]["names"] == ["Alex Example"]
    assert ident["owners"][0]["emails"] == ["alex@example.com"]


# ---------- Tests: Plaid-only tool gates -----------------------------------


async def _call_tool_expect_error(server, name: str, args: dict) -> str:
    """Call a tool through an MCP client and return the text of the raised
    ``ToolError``. FastMCP's default client surfaces server-side exceptions
    as ``ToolError`` with the original message preserved."""
    async with Client(server) as client:
        with pytest.raises(ToolError) as excinfo:
            await client.call_tool(name, args)
    return str(excinfo.value)


async def test_get_holdings_under_teller_raises_capability_error(
    teller_env, monkeypatch
):
    _write_enrollment(teller_env)
    _install_fake_teller(monkeypatch, FakeTellerProvider())

    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "get_holdings_tool", {})
    assert "investments" in msg.lower()


async def test_get_liabilities_under_teller_raises_capability_error(
    teller_env, monkeypatch
):
    _write_enrollment(teller_env)
    _install_fake_teller(monkeypatch, FakeTellerProvider())

    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "get_liabilities_tool", {})
    assert "liabilities" in msg.lower()


async def test_sync_transactions_under_teller_raises(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    _install_fake_teller(monkeypatch, FakeTellerProvider())

    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "sync_transactions_tool", {})
    assert "plaid" in msg.lower()


async def test_link_account_under_teller_raises(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    _install_fake_teller(monkeypatch, FakeTellerProvider())

    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "link_account", {})
    assert "plaid" in msg.lower()


async def test_summarize_debt_under_teller_raises(teller_env, monkeypatch):
    _write_enrollment(teller_env)
    _install_fake_teller(monkeypatch, FakeTellerProvider())

    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "summarize_debt_tool", {})
    assert "liabilities" in msg.lower()


# ---------- Tests: missing Teller enrollment --------------------------------


async def test_list_accounts_tool_errors_without_enrollment(teller_env, monkeypatch):
    """No saved enrollment file → actionable error pointing at the CLI."""
    _install_fake_teller(monkeypatch, FakeTellerProvider())
    # Intentionally do NOT write the enrollment file.
    from plaid_mcp.server import build_server

    msg = await _call_tool_expect_error(build_server(), "list_accounts_tool", {})
    assert "plaid-mcp teller connect" in msg


# ---------- Tests: Plaid side still works -----------------------------------


async def test_plaid_list_accounts_reads_cache(tmp_db, monkeypatch):
    """Sanity check: under PROVIDER=plaid, list_accounts_tool pulls from the
    local cache just like before — routed via ``PlaidProvider`` now, but
    shape-identical."""
    # Autouse fixture already sets PROVIDER=plaid + points PLAID_MCP_DB at
    # tmp_path. Seed an item + account there, then call through MCP.
    import os

    from plaid_mcp.storage import Storage

    db_path = Path(os.environ["PLAID_MCP_DB"])
    store = Storage(db_path)
    store.save_item("item_x", "access_x", "ins_x", "Plaid Test Bank", ["transactions"])
    store.upsert_account(
        "item_x",
        {
            "account_id": "plaid_acct_1",
            "name": "Checking",
            "type": "depository",
            "subtype": "checking",
            "mask": "1234",
            "iso_currency": "USD",
        },
    )
    store.close()

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool("list_accounts_tool", {})
    data = _data(result)
    assert len(data) == 1
    assert data[0]["account_id"] == "plaid_acct_1"
    assert data[0]["institution_name"] == "Plaid Test Bank"


async def test_plaid_summarize_debt_still_works(tmp_db, monkeypatch):
    """Plaid-only debt tools remain callable under PROVIDER=plaid."""
    # Patch get_liabilities to skip any Plaid SDK call.
    from plaid_mcp import tools_debt as td

    monkeypatch.setattr(
        td,
        "get_liabilities",
        lambda storage: {"credit_cards": [], "student_loans": [], "mortgages": []},
    )

    from plaid_mcp.server import build_server

    async with Client(build_server()) as client:
        result = await client.call_tool("summarize_debt_tool", {})
    data = _data(result)
    assert data is not None
    assert "debts" in data
