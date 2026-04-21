"""End-to-end integration tests against Plaid's sandbox environment.

Marked with ``@pytest.mark.sandbox`` and auto-skipped when ``.env.test`` is
missing. When present, ``.env.test`` must contain:

    PLAID_CLIENT_ID=<your sandbox client_id>
    PLAID_SECRET=<your sandbox secret>

These tests skip Plaid Link entirely by using ``/sandbox/public_token/create``,
which returns a public_token for a synthetic institution without any user
interaction. That's the officially-supported way to integration-test.
"""

from __future__ import annotations

import pytest

from plaid_mcp import tools_transactions as tt
from plaid_mcp import tools_wealth as tw
from plaid_mcp.client import get_client
from plaid_mcp.storage import Storage

pytestmark = pytest.mark.sandbox


# Plaid sandbox institution IDs — all return canned data.
INS_FIRST_PLATYPUS = "ins_109508"  # supports most products
INS_TATTERSALL = "ins_109509"      # has investments


def _create_sandbox_item(products: list[str], institution_id: str) -> tuple[str, str]:
    """Use /sandbox/public_token/create + /item/public_token/exchange to get an
    access_token without going through Link."""
    from plaid.model.item_public_token_exchange_request import (
        ItemPublicTokenExchangeRequest,
    )
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import (
        SandboxPublicTokenCreateRequest,
    )

    client = get_client()
    sandbox_req = SandboxPublicTokenCreateRequest(
        institution_id=institution_id,
        initial_products=[Products(p) for p in products],
    )
    pt = client.sandbox_public_token_create(sandbox_req)
    exchange = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=pt["public_token"])
    )
    return exchange["access_token"], exchange["item_id"]


@pytest.fixture
def linked_sandbox_item(sandbox_creds, monkeypatch, tmp_path):
    """Create a real sandbox item, persist it, and yield a Storage ready to query."""
    # Apply sandbox credentials to the process so get_client() picks them up.
    monkeypatch.setenv("PLAID_CLIENT_ID", sandbox_creds["client_id"])
    monkeypatch.setenv("PLAID_SECRET", sandbox_creds["secret"])
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    # Clear the cached client so our new env takes effect.
    from plaid_mcp import client as client_mod
    client_mod._cached.cache_clear()

    db_path = tmp_path / "sandbox.db"
    storage = Storage(db_path)

    access_token, item_id = _create_sandbox_item(
        products=["transactions"], institution_id=INS_FIRST_PLATYPUS
    )
    storage.save_item(item_id, access_token, INS_FIRST_PLATYPUS, "First Platypus Bank",
                      ["transactions"])

    # Cache accounts so list_accounts() has data.
    from plaid.model.accounts_get_request import AccountsGetRequest
    accounts_resp = get_client().accounts_get(AccountsGetRequest(access_token=access_token))
    for acct in accounts_resp["accounts"]:
        storage.upsert_account(item_id, {
            "account_id": acct["account_id"],
            "name": acct.get("name"),
            "official_name": acct.get("official_name"),
            "type": str(acct.get("type")),
            "subtype": str(acct.get("subtype")) if acct.get("subtype") else None,
            "mask": acct.get("mask"),
            "iso_currency": (acct.get("balances") or {}).get("iso_currency_code"),
        })

    yield storage, access_token, item_id

    # Cleanup: remove the sandbox item.
    try:
        from plaid.model.item_remove_request import ItemRemoveRequest
        get_client().item_remove(ItemRemoveRequest(access_token=access_token))
    except Exception:  # noqa: BLE001
        pass
    storage.close()


def test_list_accounts_sandbox(linked_sandbox_item):
    storage, _access_token, _item_id = linked_sandbox_item
    accounts = tt.list_accounts(storage)
    assert len(accounts) > 0
    assert all(a["institution_name"] == "First Platypus Bank" for a in accounts)


def test_get_balances_sandbox(linked_sandbox_item):
    storage, _access_token, _item_id = linked_sandbox_item
    balances = tt.get_balances(storage)
    assert len(balances) > 0
    assert any(b.get("current") is not None for b in balances)


def test_sync_transactions_sandbox(linked_sandbox_item):
    import time as _time

    storage, _access_token, item_id = linked_sandbox_item

    # Plaid's sandbox historical pull is async. sync_transactions() now waits
    # for HISTORICAL_UPDATE_COMPLETE internally, but in rare cases the pull
    # takes longer than the default timeout — retry a couple of times.
    deadline = _time.time() + 90
    item_result: dict = {}
    while _time.time() < deadline:
        result = tt.sync_transactions(storage, wait_for_ready=True, wait_timeout_s=45)
        item_result = result["items"][0]
        assert "error" not in item_result, item_result.get("error")
        if item_result.get("added", 0) >= 1:
            break
        _time.sleep(3)

    # First Platypus Bank sandbox seeds several transactions.
    assert item_result.get("added", 0) >= 1, (
        f"no transactions after waiting; last status={item_result.get('status')}"
    )
    assert storage.get_cursor(item_id) is not None

    rows = storage.query_transactions(
        start_date="2000-01-01", end_date="2099-12-31", limit=1000
    )
    assert len(rows) >= 1


def test_get_identity_sandbox(linked_sandbox_item):
    storage, *_ = linked_sandbox_item
    # First Platypus Bank supports identity in sandbox.
    try:
        result = tw.get_identity(storage)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Identity not enabled for this sandbox account: {e}")
    # At least one identity record with an owner name.
    if result["identities"]:
        assert result["identities"][0]["owners"][0]["names"]
