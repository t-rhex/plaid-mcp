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
    NoopGate,
    PaymentGate,
    PriceTable,
    X402Gate,
    build_gate,
)
from plaid_mcp.payments.base import DEFAULT_TOOL_PRICE_CENTS

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

    gate = X402Gate(
        receiving_address=receiver,
        network="base",
        prices=PriceTable(prices={}, default_cents=1),
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
