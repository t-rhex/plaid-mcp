"""Offline tests for the x402 payment gate.

No live facilitator, no network. We hand-roll a minimal Starlette app
that echoes JSON-RPC-shaped responses, wrap it with ``X402Gate``, and
drive it through Starlette's ``TestClient``. The facilitator is stubbed
in-process via ``_FakeFacilitator`` so verify/settle flow can be
exercised without leaving the test runner.

The one live-facilitator test (sign + pay a real Base Sepolia payment)
lives under the ``x402_testnet`` marker and is skipped without a funded
``X402_TESTNET_PRIVATE_KEY`` in env.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from x402.schemas import SettleResponse, VerifyResponse

from plaid_mcp.config import Config
from plaid_mcp.payments import (
    DEFAULT_PRICES,
    MppGate,
    NoopGate,
    PaymentGate,
    PriceTable,
    X402Gate,
    build_gate,
)
from plaid_mcp.payments.base import DEFAULT_TOOL_PRICE_CENTS
from plaid_mcp.payments.mpp import _cents_to_amount
from plaid_mcp.payments.mpp import is_mainnet as _mpp_is_mainnet

# ----------------------------------------------------------------------
# Test double — stands in for x402's HTTPFacilitatorClient so we can
# exercise verify + settle without leaving the process.
# ----------------------------------------------------------------------


@dataclass
class _FakeFacilitator:
    """In-process facilitator stub honoring the async verify/settle shape."""

    is_valid: bool = True
    invalid_reason: str | None = None
    settle_success: bool = True
    settle_raises: Exception | None = None
    verify_calls: list = field(default_factory=list)
    settle_calls: list = field(default_factory=list)

    async def verify(self, payload, requirements):
        self.verify_calls.append((payload, requirements))
        return VerifyResponse(
            is_valid=self.is_valid,
            invalid_reason=self.invalid_reason,
            invalid_message=None,
            payer="0xPAYER",
        )

    async def settle(self, payload, requirements):
        self.settle_calls.append((payload, requirements))
        if self.settle_raises is not None:
            raise self.settle_raises
        if self.settle_success:
            return SettleResponse(
                success=True,
                transaction="0xDEADBEEF",
                network=requirements.network,
                amount=requirements.amount,
                payer="0xPAYER",
            )
        return SettleResponse(
            success=False,
            error_reason="settle_failed",
            error_message="facilitator says no",
            transaction="",
            network=requirements.network,
            amount=requirements.amount,
            payer="0xPAYER",
        )

# ----------------------------------------------------------------------
# Shared fixtures — a stand-in for FastMCP's Starlette app.
# ----------------------------------------------------------------------


async def _fake_mcp_endpoint(request: Request) -> JSONResponse:
    """Echoes back 'ok' with the RPC id. Stands in for FastMCP's
    StreamableHTTPASGIApp so we can assert pass-through without wiring
    up a real MCP session."""
    body = await request.json()
    return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": "ok"})


@pytest.fixture
def fake_mcp_app() -> Starlette:
    return Starlette(routes=[Route("/mcp", _fake_mcp_endpoint, methods=["POST"])])


@pytest.fixture
def x402_gate() -> X402Gate:
    return X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
    )


# ----------------------------------------------------------------------
# PriceTable
# ----------------------------------------------------------------------


def test_price_table_known_tool_returns_configured_cents() -> None:
    table = PriceTable(prices={"foo_tool": 42}, default_cents=7)
    assert table.for_tool("foo_tool") == 42


def test_price_table_unknown_tool_falls_back_to_default() -> None:
    table = PriceTable(prices={"foo_tool": 42}, default_cents=7)
    assert table.for_tool("unknown") == 7


def test_price_table_default_prices_match_spec() -> None:
    assert DEFAULT_PRICES.for_tool("summarize_debt_tool") == 50
    assert DEFAULT_PRICES.for_tool("get_balances_tool") == 10
    assert DEFAULT_PRICES.for_tool("sync_transactions_tool") == 5
    assert DEFAULT_PRICES.for_tool("get_holdings_tool") == 20
    # Default fall-through.
    assert DEFAULT_PRICES.for_tool("nonexistent_tool") == DEFAULT_TOOL_PRICE_CENTS


def test_price_table_non_string_name_returns_default() -> None:
    table = PriceTable(prices={"foo_tool": 42}, default_cents=3)
    assert table.for_tool(None) == 3  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# build_gate factory
# ----------------------------------------------------------------------


def _base_config(**overrides) -> Config:
    defaults = dict(
        client_id="x",
        secret="y",
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_build_gate_none_returns_noop() -> None:
    gate = build_gate(_base_config(paywall="none"))
    assert isinstance(gate, NoopGate)
    assert gate.name == "noop"


def test_build_gate_x402_returns_x402_gate() -> None:
    gate = build_gate(
        _base_config(
            paywall="x402",
            x402_receiving_address="0xabc0000000000000000000000000000000000001",
            x402_network="base-sepolia",
        )
    )
    assert isinstance(gate, X402Gate)
    assert gate.name == "x402"


def test_build_gate_unknown_mode_raises() -> None:
    with pytest.raises(ValueError):
        build_gate(_base_config(paywall="bitcoin-lightning"))


def test_paymentgate_protocol_is_satisfied_by_adapters(x402_gate: X402Gate) -> None:
    # Structural typing check — mypy won't catch this, but a runtime
    # isinstance against Protocol with runtime_checkable would be
    # overkill. We just assert both adapters expose .asgi_middleware.
    assert callable(NoopGate().asgi_middleware)
    assert callable(x402_gate.asgi_middleware)
    # Trick mypy into thinking the variable is PaymentGate-typed.
    g: PaymentGate = x402_gate  # noqa: F841


# ----------------------------------------------------------------------
# Config wiring
# ----------------------------------------------------------------------


def test_config_paywall_x402_requires_receiving_address(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "x402")
    monkeypatch.delenv("X402_RECEIVING_ADDRESS", raising=False)
    with pytest.raises(RuntimeError, match="X402_RECEIVING_ADDRESS"):
        Config.from_env()


def test_config_paywall_x402_with_address_loads(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "x402")
    monkeypatch.setenv("X402_RECEIVING_ADDRESS", "0xabc0000000000000000000000000000000000001")
    monkeypatch.setenv("X402_NETWORK", "base")
    cfg = Config.from_env()
    assert cfg.paywall == "x402"
    assert cfg.x402_receiving_address == "0xabc0000000000000000000000000000000000001"
    assert cfg.x402_network == "base"


def test_config_default_paywall_is_none(monkeypatch) -> None:
    monkeypatch.delenv("PAYWALL", raising=False)
    cfg = Config.from_env()
    assert cfg.paywall == "none"
    assert cfg.x402_receiving_address is None
    assert cfg.x402_network == "base-sepolia"


def test_config_rejects_invalid_paywall_mode(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "stripe")
    with pytest.raises(RuntimeError, match="PAYWALL"):
        Config.from_env()


# ----------------------------------------------------------------------
# NoopGate
# ----------------------------------------------------------------------


def test_noop_gate_returns_app_unchanged(fake_mcp_app: Starlette) -> None:
    wrapped = NoopGate().asgi_middleware(fake_mcp_app)
    assert wrapped is fake_mcp_app  # literal same object; no wrapping.


def test_noop_gate_lets_tool_call_through(fake_mcp_app: Starlette) -> None:
    client = TestClient(NoopGate().asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_balances_tool", "arguments": {}},
    }
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200
    assert response.json()["result"] == "ok"


# ----------------------------------------------------------------------
# X402Gate — the interesting part
# ----------------------------------------------------------------------


def test_x402_rejects_unsupported_network() -> None:
    with pytest.raises(ValueError):
        X402Gate(
            receiving_address="0x0000000000000000000000000000000000000001",
            network="solana-devnet",
            prices=DEFAULT_PRICES,
        )


def test_x402_returns_402_on_tool_call_without_payment(
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 402
    assert response.headers.get("x-payment-required") == "1"

    body = response.json()
    # x402 spec shape.
    assert body["x402Version"] == 2
    assert body["error"] == "Payment Required"
    assert body["resource"]["url"] == "mcp://tool/summarize_debt_tool"
    assert len(body["accepts"]) == 1

    requirement = body["accepts"][0]
    assert requirement["scheme"] == "exact"
    # CAIP-2 chain ID for Base Sepolia. The gate accepts the friendly
    # alias at construction but emits the spec-canonical form.
    assert requirement["network"] == "eip155:84532"
    # summarize_debt_tool = 50 cents = 500_000 atomic USDC (6 decimals).
    assert requirement["amount"] == "500000"
    assert requirement["payTo"] == "0x0000000000000000000000000000000000000001"
    # USDC on base-sepolia.
    assert requirement["asset"].startswith("0x036CbD")
    # Price metadata plumbed through for client introspection.
    assert requirement["extra"]["tool"] == "summarize_debt_tool"
    assert requirement["extra"]["priceCents"] == 50


def test_x402_prices_unknown_tool_at_default_rate(
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "some_future_tool", "arguments": {}},
    }
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 402
    amount = response.json()["accepts"][0]["amount"]
    # default 10 cents = 100_000 atomic.
    assert amount == "100000"


def test_x402_lets_tools_list_through(fake_mcp_app: Starlette, x402_gate: X402Gate) -> None:
    """Discovery (tools/list) must stay free — LLMs can't know what they'd
    be paying for if they can't enumerate the tools first."""
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200
    assert response.json()["result"] == "ok"


def test_x402_lets_initialize_through(fake_mcp_app: Starlette, x402_gate: X402Gate) -> None:
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200


def test_x402_ignores_non_mcp_paths(fake_mcp_app: Starlette, x402_gate: X402Gate) -> None:
    # A GET to /mcp or POST to some other path is passed through
    # untouched — we don't want to 402 liveness probes.
    app = Starlette(
        routes=[
            Route("/mcp", _fake_mcp_endpoint, methods=["POST"]),
            Route("/healthz", lambda r: JSONResponse({"ok": True}), methods=["GET"]),
        ]
    )
    client = TestClient(x402_gate.asgi_middleware(app))
    assert client.get("/healthz").status_code == 200


def test_x402_passes_through_well_formed_payment_header(
    fake_mcp_app: Starlette,
) -> None:
    """When the client attaches a shape-valid ``X-PAYMENT`` payload AND the
    facilitator confirms it, we forward to the upstream app. The
    facilitator is stubbed so this stays offline; the subsequent settle
    call also succeeds (see the fake)."""
    from x402.schemas import PaymentPayload

    payload = PaymentPayload(
        x402_version=2,
        payload={"signature": "0xabc", "authorization": {}},
        accepted={
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "500000",
            "payTo": "0x0000000000000000000000000000000000000001",
            "maxTimeoutSeconds": 60,
        },
        resource={
            "url": "mcp://tool/summarize_debt_tool",
            "description": "Tool: summarize_debt_tool",
            "mimeType": "application/json",
        },
    )
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=_FakeFacilitator(is_valid=True, settle_success=True),
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post(
        "/mcp",
        json=rpc,
        headers={"X-PAYMENT": payload.model_dump_json(by_alias=True)},
    )
    assert response.status_code == 200
    # Settlement succeeded → X-Payment-Response header is present.
    assert "x-payment-response" in {k.lower() for k in response.headers.keys()}


def test_x402_rejects_malformed_payment_header(
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_balances_tool"},
    }
    response = client.post("/mcp", json=rpc, headers={"X-PAYMENT": "not-json"})
    assert response.status_code == 402
    # Machine-readable error field per the x402 spec — clients dispatch
    # on this string rather than parsing prose.
    assert response.json()["error"] == "invalid_payment_header"


def test_x402_body_is_replayed_to_downstream(
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    """When we pass through (tools/list), the downstream handler must
    still see the original body — we consume the receive channel to peek
    the JSON-RPC method, so we have to replay it intact."""
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 123, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200
    # The echo endpoint returns the id from the parsed body — if replay
    # were broken it would raise or return null.
    assert response.json()["id"] == 123


def test_x402_handles_non_json_body_gracefully(x402_gate: X402Gate) -> None:
    # Garbage bodies pass through to FastMCP, which emits its own error.
    # The gate must not 402 or crash. We use an echo-anything app so the
    # downstream doesn't itself blow up on invalid JSON.
    async def echo_anything(request: Request) -> JSONResponse:
        raw = await request.body()
        return JSONResponse({"bytes": len(raw)})

    app = Starlette(routes=[Route("/mcp", echo_anything, methods=["POST"])])
    client = TestClient(x402_gate.asgi_middleware(app))
    response = client.post(
        "/mcp",
        content=b"this is not json",
        headers={"content-type": "application/octet-stream"},
    )
    # The gate doesn't turn garbage into a 402 — it lets the downstream
    # handle (and, in real FastMCP, produce a JSON-RPC parse error).
    assert response.status_code != 402


def test_x402_base_mainnet_price_matches_usdc(fake_mcp_app: Starlette) -> None:
    gate = X402Gate(
        receiving_address="0xabc0000000000000000000000000000000000001",
        network="base",
        prices=DEFAULT_PRICES,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_balances_tool"},
    }
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 402
    accepts = response.json()["accepts"][0]
    # Base mainnet USDC.
    assert accepts["asset"] == "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    assert accepts["network"] == "eip155:8453"


def test_x402_batch_with_tool_call_gates_entire_batch(
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    # A JSON-RPC batch containing a tools/call → 402. Per-method
    # metering inside a batch is a follow-up; for now we gate the
    # whole batch so callers can't smuggle a paid call behind a free
    # tools/list.
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_balances_tool"},
        },
    ]
    # httpx needs the body as raw bytes because TestClient defaults to a
    # single-object JSON body.
    response = client.post(
        "/mcp",
        content=json.dumps(batch).encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 402


# ----------------------------------------------------------------------
# Facilitator verify + settle (offline, with _FakeFacilitator)
# ----------------------------------------------------------------------


def _shape_valid_payload_json() -> str:
    """Build a schema-valid PaymentPayload that the facilitator stub will
    judge — the signature bytes are fake but the shape parses."""
    from x402.schemas import PaymentPayload

    return PaymentPayload(
        x402_version=2,
        payload={"signature": "0xabc", "authorization": {}},
        accepted={
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "amount": "500000",
            "payTo": "0x0000000000000000000000000000000000000001",
            "maxTimeoutSeconds": 60,
        },
        resource={
            "url": "mcp://tool/summarize_debt_tool",
            "description": "Tool: summarize_debt_tool",
            "mimeType": "application/json",
        },
    ).model_dump_json(by_alias=True)


def test_x402_spoofed_payment_is_rejected_by_facilitator(
    fake_mcp_app: Starlette,
) -> None:
    """A schema-valid X-PAYMENT that the facilitator flags invalid →
    402 with ``error`` field populated from VerifyResponse.invalid_reason."""
    fake = _FakeFacilitator(is_valid=False, invalid_reason="insufficient_funds")
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post(
        "/mcp", json=rpc, headers={"X-PAYMENT": _shape_valid_payload_json()}
    )
    assert response.status_code == 402
    body = response.json()
    assert body["error"] == "insufficient_funds"
    # Verify was called; settle was NOT (we don't settle a failed verify).
    assert len(fake.verify_calls) == 1
    assert fake.settle_calls == []


def test_x402_no_header_has_no_error_field_in_402(
    fake_mcp_app: Starlette,
) -> None:
    """The initial 402 quote (no X-PAYMENT header yet) is generic per the
    spec — clients shouldn't see a specific ``error`` reason because no
    payment has been attempted."""
    fake = _FakeFacilitator(is_valid=True)
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_balances_tool"},
    }
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 402
    # Generic string; no machine-readable error slug because nothing
    # was actually *verified* as failing.
    body = response.json()
    assert body["error"] == "Payment Required"
    # Facilitator was never called.
    assert fake.verify_calls == []


def test_x402_non_mcp_path_bypasses_gate_entirely(
    x402_gate: X402Gate,
) -> None:
    """Only /mcp is gated. Tool-call JSON on some other path must pass
    through untouched, even if the JSON body looks like a tools/call."""
    async def echo(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"saw": body.get("method")})

    app = Starlette(routes=[Route("/other", echo, methods=["POST"])])
    client = TestClient(x402_gate.asgi_middleware(app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool"},
    }
    response = client.post("/other", json=rpc)
    assert response.status_code == 200
    assert response.json()["saw"] == "tools/call"


def test_x402_settle_failure_does_not_block_upstream_response(
    fake_mcp_app: Starlette, caplog,
) -> None:
    """If verify succeeds but settle raises, the tool response still
    reaches the client — the user already got what they paid for.
    Failure is logged for operator visibility."""
    fake = _FakeFacilitator(
        is_valid=True,
        settle_raises=RuntimeError("facilitator down"),
    )
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    import logging as _logging
    with caplog.at_level(_logging.ERROR, logger="plaid_mcp.payments.x402"):
        response = client.post(
            "/mcp",
            json=rpc,
            headers={"X-PAYMENT": _shape_valid_payload_json()},
        )
    # Upstream tool ran and returned 200.
    assert response.status_code == 200
    assert response.json()["result"] == "ok"
    # Settle was attempted.
    assert len(fake.settle_calls) == 1
    # And a structured error was logged.
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("x402 settle raised" in r.message for r in error_records), (
        f"Expected an x402 settle-raised log; saw: {[r.message for r in error_records]}"
    )


def test_x402_settle_reported_failure_does_not_block(
    fake_mcp_app: Starlette, caplog,
) -> None:
    """Same as above but the facilitator returned success=False instead of
    raising. Still must not 5xx — upstream response flows out."""
    fake = _FakeFacilitator(is_valid=True, settle_success=False)
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    import logging as _logging
    with caplog.at_level(_logging.ERROR, logger="plaid_mcp.payments.x402"):
        response = client.post(
            "/mcp",
            json=rpc,
            headers={"X-PAYMENT": _shape_valid_payload_json()},
        )
    assert response.status_code == 200
    assert any(
        "x402 settle reported failure" in r.message for r in caplog.records
    )


def test_x402_successful_verify_and_settle_emits_payment_response_header(
    fake_mcp_app: Starlette,
) -> None:
    """Happy path end-to-end: verify OK → forward → settle OK → header
    attached to the outgoing 200."""
    fake = _FakeFacilitator(is_valid=True, settle_success=True)
    gate = X402Gate(
        receiving_address="0x0000000000000000000000000000000000000001",
        network="base-sepolia",
        prices=DEFAULT_PRICES,
        facilitator=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post(
        "/mcp", json=rpc, headers={"X-PAYMENT": _shape_valid_payload_json()}
    )
    assert response.status_code == 200
    header_names = {k.lower() for k in response.headers.keys()}
    assert "x-payment-response" in header_names


# ----------------------------------------------------------------------
# build_gate — network/facilitator consistency + mainnet opt-in
# ----------------------------------------------------------------------


def test_build_gate_refuses_mainnet_without_explicit_opt_in() -> None:
    with pytest.raises(RuntimeError, match="X402_ALLOW_MAINNET"):
        build_gate(
            _base_config(
                paywall="x402",
                x402_receiving_address="0xabc0000000000000000000000000000000000001",
                x402_network="base",
                x402_allow_mainnet=False,
            )
        )


def test_build_gate_allows_mainnet_with_opt_in() -> None:
    gate = build_gate(
        _base_config(
            paywall="x402",
            x402_receiving_address="0xabc0000000000000000000000000000000000001",
            x402_network="base",
            x402_allow_mainnet=True,
        )
    )
    assert isinstance(gate, X402Gate)
    assert gate.network == "eip155:8453"


def test_build_gate_refuses_mismatched_testnet_facilitator_for_mainnet() -> None:
    with pytest.raises(RuntimeError, match="testnet"):
        build_gate(
            _base_config(
                paywall="x402",
                x402_receiving_address="0xabc0000000000000000000000000000000000001",
                x402_network="base",
                x402_allow_mainnet=True,
                x402_facilitator_url="https://facilitator-sepolia.example.org",
            )
        )


def test_build_gate_refuses_mismatched_mainnet_facilitator_for_testnet() -> None:
    with pytest.raises(RuntimeError, match="mainnet"):
        build_gate(
            _base_config(
                paywall="x402",
                x402_receiving_address="0xabc0000000000000000000000000000000000001",
                x402_network="base-sepolia",
                x402_facilitator_url="https://facilitator-mainnet.example.org",
            )
        )


def test_build_gate_defaults_facilitator_url_to_x402_hosted() -> None:
    from x402.http import DEFAULT_FACILITATOR_URL

    gate = build_gate(
        _base_config(
            paywall="x402",
            x402_receiving_address="0xabc0000000000000000000000000000000000001",
            x402_network="base-sepolia",
        )
    )
    assert isinstance(gate, X402Gate)
    assert gate.facilitator_url == DEFAULT_FACILITATOR_URL


def test_build_gate_respects_facilitator_url_override() -> None:
    gate = build_gate(
        _base_config(
            paywall="x402",
            x402_receiving_address="0xabc0000000000000000000000000000000000001",
            x402_network="base-sepolia",
            x402_facilitator_url="https://facilitator.example.org/x402",
        )
    )
    assert gate.facilitator_url == "https://facilitator.example.org/x402"


# ----------------------------------------------------------------------
# Online-optional: live Base Sepolia facilitator (skipped without key)
# ----------------------------------------------------------------------


@pytest.mark.x402_testnet
def test_x402_live_base_sepolia_payment_round_trip(fake_mcp_app: Starlette) -> None:
    """End-to-end against a hosted facilitator on Base Sepolia.

    Requires a wallet key in X402_TESTNET_PRIVATE_KEY that already has a
    tiny amount of Base Sepolia USDC (the user funds it out of band — this
    test does NOT auto-fund). Skipped otherwise.
    """
    private_key = os.getenv("X402_TESTNET_PRIVATE_KEY", "").strip()
    if not private_key:
        pytest.skip(
            "X402_TESTNET_PRIVATE_KEY not set — skipping live Base Sepolia test."
        )

    # x402[evm] primitives — only imported inside the test so the offline
    # test run doesn't pull web3 unnecessarily if someone strips the extra.
    from eth_account import Account
    from x402 import x402Client
    from x402.mechanisms.evm.exact import ExactEvmClientScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentRequired

    signer = EthAccountSigner(Account.from_key(private_key))
    receiver = os.getenv(
        "X402_TESTNET_RECEIVING_ADDRESS", signer.address
    )

    # Construct a real gate pointing at the default (hosted) facilitator;
    # one cent costs 10_000 atomic USDC so the test barely moves funds.
    gate = X402Gate(
        receiving_address=receiver,
        network="base-sepolia",
        prices=PriceTable(prices={}, default_cents=1),
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "live_test_tool", "arguments": {}},
    }
    # Step 1: hit the gate unpaid → 402 with requirements.
    probe = client.post("/mcp", json=rpc)
    assert probe.status_code == 402
    required = PaymentRequired.model_validate(probe.json())

    # Step 2: sign a PaymentPayload with x402Client.
    # Register under the CAIP-2 network identifier that the gate emits
    # in its 402 requirements (eip155:84532 = Base Sepolia).
    x402_client = x402Client()
    x402_client.register("eip155:84532", ExactEvmClientScheme(signer=signer))
    import asyncio
    payload = asyncio.run(x402_client.create_payment_payload(required))

    # Step 3: replay with the signed header → expect 200 + response header.
    paid = client.post(
        "/mcp",
        json=rpc,
        headers={"X-PAYMENT": payload.model_dump_json(by_alias=True)},
    )
    assert paid.status_code == 200, paid.text
    assert "x-payment-response" in {k.lower() for k in paid.headers.keys()}


@pytest.mark.x402_mainnet
def test_x402_live_base_mainnet_payment_round_trip(fake_mcp_app: Starlette) -> None:
    """End-to-end against the hosted facilitator on Base **mainnet** — spends
    REAL USDC.

    Costs 1 cent of real USDC per run when the sender equals the receiver,
    because the facilitator pays gas and the principal loops back to you.
    Set a different ``X402_MAINNET_RECEIVING_ADDRESS`` to send the cent to
    someone else (or to a throwaway you want to burn).

    Guardrails:
      - Skipped unless ``X402_MAINNET_PRIVATE_KEY`` is set. Deliberate: the
        testnet variant uses ``X402_TESTNET_PRIVATE_KEY`` so that an operator
        with only a testnet key configured can't accidentally hit mainnet.
      - The mainnet opt-in lives in ``build_gate(config)``; constructing
        ``X402Gate`` directly (as we do here) skips that policy guard, so
        this test is the only code path that can construct a mainnet gate
        without setting ``X402_ALLOW_MAINNET=true``.
    """
    private_key = os.getenv("X402_MAINNET_PRIVATE_KEY", "").strip()
    if not private_key:
        pytest.skip(
            "X402_MAINNET_PRIVATE_KEY not set — skipping live Base mainnet test."
        )

    from eth_account import Account
    from x402 import x402Client
    from x402.mechanisms.evm.exact import ExactEvmClientScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentRequired

    signer = EthAccountSigner(Account.from_key(private_key))
    receiver = os.getenv(
        "X402_MAINNET_RECEIVING_ADDRESS", signer.address
    )

    # CDP facilitator: x402.org/facilitator is testnet-only. Mainnet
    # settlement requires an authenticated facilitator; Coinbase's CDP
    # runs one at api.cdp.coinbase.com and ``cdp.x402`` bundles a
    # JWT-signing FacilitatorConfig helper. CDP API creds go in
    # CDP_API_KEY_ID + CDP_API_KEY_SECRET env vars.
    cdp_key_id = os.getenv("CDP_API_KEY_ID", "").strip()
    cdp_key_secret = os.getenv("CDP_API_KEY_SECRET", "").strip()
    if not (cdp_key_id and cdp_key_secret):
        pytest.skip(
            "CDP_API_KEY_ID / CDP_API_KEY_SECRET not set — x402.org's "
            "default facilitator is testnet-only; mainnet needs CDP auth."
        )

    from cdp.x402 import create_facilitator_config
    from x402.http import HTTPFacilitatorClient

    cdp_cfg = create_facilitator_config(cdp_key_id, cdp_key_secret)
    cdp_facilitator = HTTPFacilitatorClient(cdp_cfg)

    gate = X402Gate(
        receiving_address=receiver,
        network="base",
        prices=PriceTable(prices={}, default_cents=1),
        facilitator=cdp_facilitator,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "live_mainnet_test_tool", "arguments": {}},
    }
    probe = client.post("/mcp", json=rpc)
    assert probe.status_code == 402
    required = PaymentRequired.model_validate(probe.json())

    x402_client = x402Client()
    # Base mainnet = eip155:8453.
    x402_client.register("eip155:8453", ExactEvmClientScheme(signer=signer))
    import asyncio
    payload = asyncio.run(x402_client.create_payment_payload(required))

    paid = client.post(
        "/mcp",
        json=rpc,
        headers={"X-PAYMENT": payload.model_dump_json(by_alias=True)},
    )
    assert paid.status_code == 200, paid.text
    assert "x-payment-response" in {k.lower() for k in paid.headers.keys()}


# ======================================================================
# MPP (Machine Payments Protocol via Tempo) — parallel rail to x402.
#
# Tests below exercise MppGate using stubbed-out pympp handlers so the
# offline suite never touches the Tempo network. The one live-facilitator
# test lives under the ``mpp_testnet`` marker at the bottom and is
# skipped without MPP_TESTNET_PRIVATE_KEY.
# ======================================================================


@dataclass
class _FakeChallenge:
    """Minimal stand-in for ``mpp.Challenge`` for middleware tests.

    MppGate duck-types against ``to_www_authenticate`` and a few
    attributes (see ``_is_challenge``) — we don't need the real
    HMAC-bound Challenge to exercise the 402 code path.
    """

    id: str = "ch_fake"
    realm: str = "plaid-mcp"
    method: str = "tempo"
    intent: str = "charge"
    www_auth: str = 'Payment realm="plaid-mcp", method="tempo"'

    def to_www_authenticate(self, realm: str) -> str:  # noqa: ARG002 — realm echoed in.
        return self.www_auth


@dataclass
class _FakeReceipt:
    """Minimal stand-in for ``mpp.Receipt``."""

    header: str = "receipt-b64"

    def to_payment_receipt(self) -> str:
        return self.header


@dataclass
class _FakeMppHandler:
    """Stub pympp Mpp handler. Returns whatever ``result`` is set to.

    ``result`` can be either a ``_FakeChallenge`` (simulating an unpaid
    or invalid request) or a ``(credential, receipt)`` tuple (simulating
    a valid payment). Tests toggle this to exercise both branches.
    """

    result: object = field(default_factory=_FakeChallenge)
    charge_calls: list = field(default_factory=list)

    async def charge(self, *, authorization, amount, extra=None, **kwargs):
        self.charge_calls.append(
            {"authorization": authorization, "amount": amount, "extra": extra, **kwargs}
        )
        return self.result


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def test_mpp_cents_to_amount_formats_as_two_decimals() -> None:
    """pympp's charge() expects a human amount string; the gate builds
    one by quantizing integer cents. Zero-pad the cents half so "5" →
    "0.05", not "0.5" (which would be 50 cents)."""
    assert _cents_to_amount(0) == "0.00"
    assert _cents_to_amount(5) == "0.05"
    assert _cents_to_amount(10) == "0.10"
    assert _cents_to_amount(50) == "0.50"
    assert _cents_to_amount(123) == "1.23"
    # Negative cents clamp to zero — shouldn't happen in practice but
    # the conversion must not emit a string pympp will reject.
    assert _cents_to_amount(-5) == "0.00"


def test_mpp_is_mainnet_accepts_aliases_and_raw_ids() -> None:
    assert _mpp_is_mainnet("tempo-mainnet") is True
    assert _mpp_is_mainnet("tempo") is True
    assert _mpp_is_mainnet("4217") is True
    assert _mpp_is_mainnet("tempo-testnet") is False
    assert _mpp_is_mainnet("moderato") is False
    assert _mpp_is_mainnet("42431") is False
    # Garbage falls through to "not mainnet" — the gate validates the
    # alias explicitly at construction, so this helper only gates the
    # opt-in check.
    assert _mpp_is_mainnet("gibberish") is False


def test_mpp_gate_rejects_unknown_network() -> None:
    with pytest.raises(ValueError):
        MppGate(
            destination_address="0xabc0000000000000000000000000000000000001",
            network="solana",
            prices=DEFAULT_PRICES,
            mpp_handler=_FakeMppHandler(),
        )


# ----------------------------------------------------------------------
# build_gate + Config wiring for PAYWALL=mpp
# ----------------------------------------------------------------------


def test_build_gate_mpp_returns_mpp_gate() -> None:
    gate = build_gate(
        _base_config(
            paywall="mpp",
            mpp_destination_address="0xabc0000000000000000000000000000000000001",
            mpp_network="tempo-testnet",
        )
    )
    assert isinstance(gate, MppGate)
    assert gate.name == "mpp"
    assert gate.chain_id == 42431


def test_build_gate_mpp_requires_destination_address() -> None:
    with pytest.raises(RuntimeError, match="mpp_destination_address"):
        build_gate(
            _base_config(paywall="mpp", mpp_destination_address=None)
        )


def test_build_gate_mpp_refuses_mainnet_without_opt_in() -> None:
    with pytest.raises(RuntimeError, match="MPP_ALLOW_MAINNET"):
        build_gate(
            _base_config(
                paywall="mpp",
                mpp_destination_address="0xabc0000000000000000000000000000000000001",
                mpp_network="tempo-mainnet",
                mpp_allow_mainnet=False,
            )
        )


def test_config_paywall_mpp_requires_destination(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "mpp")
    monkeypatch.delenv("MPP_DESTINATION_ADDRESS", raising=False)
    with pytest.raises(RuntimeError, match="MPP_DESTINATION_ADDRESS"):
        Config.from_env()


def test_config_paywall_mpp_with_address_loads(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "mpp")
    monkeypatch.setenv(
        "MPP_DESTINATION_ADDRESS", "0xabc0000000000000000000000000000000000001"
    )
    monkeypatch.setenv("MPP_NETWORK", "tempo-testnet")
    cfg = Config.from_env()
    assert cfg.paywall == "mpp"
    assert cfg.mpp_destination_address == "0xabc0000000000000000000000000000000000001"
    assert cfg.mpp_network == "tempo-testnet"


def test_config_paywall_mpp_mainnet_requires_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("PAYWALL", "mpp")
    monkeypatch.setenv(
        "MPP_DESTINATION_ADDRESS", "0xabc0000000000000000000000000000000000001"
    )
    monkeypatch.setenv("MPP_NETWORK", "tempo-mainnet")
    monkeypatch.delenv("MPP_ALLOW_MAINNET", raising=False)
    with pytest.raises(RuntimeError, match="MPP_ALLOW_MAINNET"):
        Config.from_env()


def test_config_rejects_invalid_paywall_includes_mpp(monkeypatch) -> None:
    """The 'unknown paywall' error message should mention both rails so
    the operator knows mpp is a legitimate option."""
    monkeypatch.setenv("PAYWALL", "stripe")
    with pytest.raises(RuntimeError, match="mpp"):
        Config.from_env()


# ----------------------------------------------------------------------
# MppGate ASGI middleware behavior
# ----------------------------------------------------------------------


@pytest.fixture
def mpp_gate_with_challenge(fake_mcp_app: Starlette) -> tuple[MppGate, _FakeMppHandler]:
    """An MppGate whose stubbed handler always returns a challenge.

    Used to assert the 402 path — the response must include
    ``WWW-Authenticate: Payment ...`` and a JSON-RPC error body with
    code -32042 (payment-required per draft-payment-transport-mcp-00).
    """
    fake = _FakeMppHandler(result=_FakeChallenge())
    gate = MppGate(
        destination_address="0xabc0000000000000000000000000000000000001",
        network="tempo-testnet",
        prices=DEFAULT_PRICES,
        mpp_handler=fake,
    )
    return gate, fake


def test_mpp_returns_402_with_www_authenticate_on_unpaid_tool_call(
    fake_mcp_app: Starlette,
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    gate, fake = mpp_gate_with_challenge
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post("/mcp", json=rpc)

    assert response.status_code == 402
    # The WWW-Authenticate header is the defining feature of the MPP
    # wire protocol — clients discover the payment challenge there.
    www_auth = response.headers.get("www-authenticate", "")
    assert www_auth.lower().startswith("payment ")
    # MCP-aware clients dispatch on the JSON-RPC error code instead.
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32042
    assert body["error"]["data"]["httpStatus"] == 402

    # Handler was called with the computed amount.
    assert len(fake.charge_calls) == 1
    # summarize_debt_tool = 50 cents = "0.50"
    assert fake.charge_calls[0]["amount"] == "0.50"
    # Tool name is plumbed through so pympp can include it in the
    # challenge metadata for client-side introspection.
    assert fake.charge_calls[0]["extra"] == {"tool": "summarize_debt_tool"}


def test_mpp_tools_list_is_free(
    fake_mcp_app: Starlette,
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    """Discovery must not be charged — the LLM can't know what it's
    paying for until it's enumerated the tools."""
    gate, fake = mpp_gate_with_challenge
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200
    assert response.json()["result"] == "ok"
    # Handler is never consulted for free endpoints.
    assert fake.charge_calls == []


def test_mpp_initialize_is_free(
    fake_mcp_app: Starlette,
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    gate, _ = mpp_gate_with_challenge
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200


def test_mpp_ignores_non_mcp_paths(
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    """Gate only wraps POSTs to /mcp. Health checks, SSE GETs, and
    anything else on the host must pass through untouched."""
    gate, fake = mpp_gate_with_challenge

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/mcp", _fake_mcp_endpoint, methods=["POST"]),
            Route("/healthz", health, methods=["GET"]),
        ]
    )
    client = TestClient(gate.asgi_middleware(app))
    assert client.get("/healthz").status_code == 200
    assert fake.charge_calls == []


def test_mpp_non_mcp_post_path_bypasses_gate(
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    """Tool-call JSON on some other path must not be gated — avoids
    surprising operators who mount additional JSON-RPC apps alongside
    the MCP one."""
    gate, fake = mpp_gate_with_challenge

    async def echo(request: Request) -> JSONResponse:
        body = await request.json()
        return JSONResponse({"saw": body.get("method")})

    app = Starlette(routes=[Route("/other", echo, methods=["POST"])])
    client = TestClient(gate.asgi_middleware(app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool"},
    }
    response = client.post("/other", json=rpc)
    assert response.status_code == 200
    assert response.json()["saw"] == "tools/call"
    assert fake.charge_calls == []


def test_mpp_valid_credential_forwards_and_attaches_receipt(
    fake_mcp_app: Starlette,
) -> None:
    """Happy path: handler returns (credential, receipt) → gate lets the
    request through and injects the receipt header on the outgoing 200."""
    receipt = _FakeReceipt(header="ok-receipt-b64")
    # Any sentinel value stands in for the credential; the gate doesn't
    # introspect it today.
    fake = _FakeMppHandler(result=(object(), receipt))
    gate = MppGate(
        destination_address="0xabc0000000000000000000000000000000000001",
        network="tempo-testnet",
        prices=DEFAULT_PRICES,
        mpp_handler=fake,
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 9,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool", "arguments": {}},
    }
    response = client.post(
        "/mcp",
        json=rpc,
        headers={"Authorization": "Payment eyJtb2NrIjogInRva2VuIn0"},
    )
    assert response.status_code == 200
    assert response.json()["result"] == "ok"
    # pympp-native receipt header.
    assert response.headers.get("payment-receipt") == "ok-receipt-b64"
    # Also emitted as Authentication-Info for IETF-draft-aligned clients.
    assert response.headers.get("authentication-info") == "ok-receipt-b64"
    # Authorization header reached the pympp handler intact.
    assert fake.charge_calls[0]["authorization"] == "Payment eyJtb2NrIjogInRva2VuIn0"


def test_mpp_handler_exception_falls_back_to_challenge(
    fake_mcp_app: Starlette,
) -> None:
    """If pympp's charge() raises on a malformed credential, the gate
    must not 5xx — it re-calls with ``authorization=None`` to mint a
    fresh challenge and returns 402."""

    class Explodes:
        calls: list[dict] = []

        async def charge(self, *, authorization, amount, extra=None, **kwargs):
            Explodes.calls.append(
                {
                    "authorization": authorization,
                    "amount": amount,
                    "extra": extra,
                }
            )
            # First call with malformed auth → raise. Second call with
            # authorization=None → return challenge.
            if authorization is not None:
                raise ValueError("malformed payload")
            return _FakeChallenge()

    gate = MppGate(
        destination_address="0xabc0000000000000000000000000000000000001",
        network="tempo-testnet",
        prices=DEFAULT_PRICES,
        mpp_handler=Explodes(),
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "summarize_debt_tool"},
    }
    response = client.post(
        "/mcp",
        json=rpc,
        headers={"Authorization": "Payment not-really-valid"},
    )
    assert response.status_code == 402
    assert response.headers.get("www-authenticate", "").lower().startswith("payment ")
    # Two handler invocations: the failing one, then the recovery.
    assert len(Explodes.calls) == 2
    assert Explodes.calls[0]["authorization"] == "Payment not-really-valid"
    assert Explodes.calls[1]["authorization"] is None


def test_mpp_body_is_replayed_to_downstream(
    fake_mcp_app: Starlette,
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    """When the gate passes through (tools/list), the wrapped handler
    must still see the original body — we consume the receive channel
    to peek the JSON-RPC method, so we have to replay it intact."""
    gate, _ = mpp_gate_with_challenge
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {"jsonrpc": "2.0", "id": 456, "method": "tools/list", "params": {}}
    response = client.post("/mcp", json=rpc)
    assert response.status_code == 200
    # The echo endpoint returns the id from the parsed body — if replay
    # were broken it would raise or return null.
    assert response.json()["id"] == 456


def test_mpp_batch_with_tool_call_gates_entire_batch(
    fake_mcp_app: Starlette,
    mpp_gate_with_challenge: tuple[MppGate, _FakeMppHandler],
) -> None:
    """A JSON-RPC batch containing a tools/call → 402. Matches the
    x402 policy: per-method metering in a batch is a follow-up."""
    gate, _ = mpp_gate_with_challenge
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_balances_tool"},
        },
    ]
    response = client.post(
        "/mcp",
        content=json.dumps(batch).encode(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 402


def test_mpp_noop_factory_still_returns_noop() -> None:
    """Adding the MPP branch must not regress the noop default."""
    gate = build_gate(_base_config(paywall="none"))
    assert isinstance(gate, NoopGate)


def test_mpp_paymentgate_protocol_is_satisfied(mpp_gate_with_challenge) -> None:
    gate, _ = mpp_gate_with_challenge
    assert callable(gate.asgi_middleware)
    g: PaymentGate = gate  # noqa: F841 — mypy shape check.


# ----------------------------------------------------------------------
# Online-optional: live Tempo testnet (skipped without key)
# ----------------------------------------------------------------------


@pytest.mark.mpp_testnet
@pytest.mark.xfail(
    reason=(
        "pympp <=0.6 rejects auto-attributed transfers in _validate_calls "
        "(mpp/methods/tempo/intents.py:_match_single_transfer_calldata). "
        "Client auto-generates an attribution memo when challenge.methodDetails.memo "
        "is None, signs with transferWithMemo selector 0x95777d59. The pre-broadcast "
        "validator then rejects because it only matches transfer() selector 0xa9059cbb "
        "when the challenge memo is None. The async post-broadcast validator "
        "(_assert_challenge_bound_memo) would have handled this correctly. "
        "Offline tests prove our gate's wire format is correct. Re-enable when "
        "pympp fixes https://github.com/tempoxyz/pympp (likely in a 0.7 release)."
    ),
    strict=False,
)
def test_mpp_live_tempo_testnet_payment_round_trip(fake_mcp_app: Starlette) -> None:
    """End-to-end against a real Tempo testnet (Moderato) RPC.

    Requires a funded wallet key in ``MPP_TESTNET_PRIVATE_KEY``. The
    test spins up a real MppGate, hits it unpaid to collect the
    challenge, signs a credential with pympp's Tempo client, and
    replays. Skipped without the key.
    """
    private_key = os.getenv("MPP_TESTNET_PRIVATE_KEY", "").strip()
    if not private_key:
        pytest.skip(
            "MPP_TESTNET_PRIVATE_KEY not set — skipping live Tempo testnet test."
        )

    # Imports inside the test so the offline suite doesn't pull pytempo
    # (native extension) if someone strips the extra.
    from mpp import Challenge
    from mpp.methods.tempo import ChargeIntent, TempoAccount
    from mpp.methods.tempo import tempo as tempo_method

    account = TempoAccount.from_key(private_key)
    destination = os.getenv(
        "MPP_TESTNET_DESTINATION_ADDRESS", account.address
    )

    gate = MppGate(
        destination_address=destination,
        network="tempo-testnet",
        # Default-rate 1 cent so the test spends the minimum meaningful
        # amount of testnet tokens.
        prices=PriceTable(prices={}, default_cents=1),
    )
    client = TestClient(gate.asgi_middleware(fake_mcp_app))
    rpc = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "live_mpp_test_tool", "arguments": {}},
    }

    # Step 1: unpaid probe → 402 + WWW-Authenticate.
    probe = client.post("/mcp", json=rpc)
    assert probe.status_code == 402
    www_auth = probe.headers.get("www-authenticate", "")
    challenge = Challenge.from_www_authenticate(www_auth)

    # Step 2: sign a credential via pympp's Tempo client.
    client_method = tempo_method(
        account=account,
        chain_id=42431,
        intents={"charge": ChargeIntent(chain_id=42431)},
    )
    import asyncio

    credential = asyncio.run(client_method.create_credential(challenge))

    # Step 3: replay with the Authorization: Payment header.
    paid = client.post(
        "/mcp",
        json=rpc,
        headers={"Authorization": credential.to_authorization()},
    )
    assert paid.status_code == 200, paid.text
    # Receipt header surfaced on the successful response.
    assert "payment-receipt" in {k.lower() for k in paid.headers.keys()}
