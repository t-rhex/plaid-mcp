"""Teller provider tests — mocked HTTP, no network required.

For real sandbox integration, see ``test_teller_sandbox.py`` (gated on
TELLER_SANDBOX_ACCESS_TOKEN in the environment).
"""

from __future__ import annotations

import httpx
import pytest

from plaid_mcp.providers import Capability, Enrollment
from plaid_mcp.providers.teller import TellerError, TellerProvider


def _mock_provider(transport: httpx.MockTransport) -> TellerProvider:
    p = TellerProvider(application_id="app_test", environment="sandbox")
    p._client = httpx.Client(
        transport=transport,
        base_url="https://api.teller.io",
        headers={"Accept": "application/json"},
    )
    return p


def _enrollment() -> Enrollment:
    return Enrollment(
        id="enr_1",
        institution_id="chase",
        institution_name="Chase",
        access_token="test_token",
        provider="teller",
    )


def test_capabilities_excludes_investments_and_liabilities():
    p = TellerProvider(application_id="app_x", environment="sandbox")
    caps = p.capabilities()
    assert Capability.TRANSACTIONS in caps
    assert Capability.IDENTITY in caps
    assert Capability.INVESTMENTS not in caps
    assert Capability.LIABILITIES not in caps
    assert Capability.INCOME not in caps


def test_production_env_requires_cert():
    with pytest.raises(ValueError, match="client certificate"):
        TellerProvider(application_id="x", environment="production")


def test_list_accounts_normalizes_teller_shape():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/accounts"
        return httpx.Response(
            200,
            json=[
                {
                    "id": "acc_1",
                    "name": "Checking",
                    "type": "depository",
                    "subtype": "checking",
                    "last_four": "1234",
                    "currency": "USD",
                },
                {
                    "id": "acc_2",
                    "name": "Sapphire",
                    "type": "credit",
                    "subtype": "credit_card",
                    "last_four": "5678",
                    "currency": "USD",
                },
            ],
        )

    p = _mock_provider(httpx.MockTransport(handler))
    accounts = p.list_accounts(_enrollment())
    assert [a.id for a in accounts] == ["acc_1", "acc_2"]
    assert accounts[0].mask == "1234"
    assert accounts[1].type == "credit"


def test_get_balances_fans_out_per_account():
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/accounts":
            return httpx.Response(
                200,
                json=[
                    {"id": "acc_1", "name": "Checking", "type": "depository",
                     "subtype": "checking", "last_four": "1234", "currency": "USD"},
                ],
            )
        if req.url.path == "/accounts/acc_1/balances":
            return httpx.Response(
                200,
                json={"ledger": "1234.56", "available": "1200.00"},
            )
        return httpx.Response(404)

    p = _mock_provider(httpx.MockTransport(handler))
    balances = p.get_balances(_enrollment())
    assert len(balances) == 1
    assert balances[0].current == 1234.56
    assert balances[0].available == 1200.00
    assert "/accounts/acc_1/balances" in calls


def test_get_transactions_walks_pages_until_short_page():
    pages = [
        [{"id": f"t{i}", "amount": "10.00", "date": "2026-03-01",
          "description": f"tx {i}", "status": "posted", "details": {}}
         for i in range(500)],
        [{"id": "t500", "amount": "99.99", "date": "2026-03-02",
          "description": "last", "status": "posted", "details": {}}],
    ]
    call_i = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/accounts":
            return httpx.Response(
                200,
                json=[{"id": "acc_1", "name": "Checking", "type": "depository",
                       "subtype": "checking", "last_four": "1234", "currency": "USD"}],
            )
        if req.url.path == "/accounts/acc_1/transactions":
            # First call has no from_id; second does.
            from_id = req.url.params.get("from_id")
            if from_id is None:
                call_i["n"] += 1
                return httpx.Response(200, json=pages[0])
            assert from_id == "t499"
            call_i["n"] += 1
            return httpx.Response(200, json=pages[1])
        return httpx.Response(404)

    p = _mock_provider(httpx.MockTransport(handler))
    txs = p.get_transactions(_enrollment(), "2026-03-01", "2026-03-31")
    assert len(txs) == 501
    assert call_i["n"] == 2
    assert txs[-1].id == "t500"
    assert txs[-1].amount == 99.99


def test_401_raises_teller_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    p = _mock_provider(httpx.MockTransport(handler))
    with pytest.raises(TellerError, match="invalid"):
        p.list_accounts(_enrollment())


def test_complete_enrollment_extracts_fields_from_connect_payload():
    p = TellerProvider(application_id="app_x", environment="sandbox")
    e = p.complete_enrollment(
        {
            "accessToken": "tok_abc",
            "enrollment": {
                "id": "enr_xyz",
                "institution": {"id": "chase", "name": "Chase"},
            },
            "user": {"id": "usr_1"},
        }
    )
    assert e.access_token == "tok_abc"
    assert e.institution_name == "Chase"
    assert e.institution_id == "chase"
    assert e.provider == "teller"


def test_complete_enrollment_missing_token_raises():
    p = TellerProvider(application_id="app_x", environment="sandbox")
    with pytest.raises(TellerError, match="accessToken"):
        p.complete_enrollment({"enrollment": {}})


def test_begin_enrollment_requires_application_id():
    p = TellerProvider(application_id=None, environment="sandbox")
    with pytest.raises(TellerError, match="APPLICATION_ID"):
        p.begin_enrollment()
