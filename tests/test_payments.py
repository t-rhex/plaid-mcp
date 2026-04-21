"""Offline tests for the x402 payment gate.

No live facilitator, no network. We hand-roll a minimal Starlette app
that echoes JSON-RPC-shaped responses, wrap it with ``X402Gate``, and
drive it through Starlette's ``TestClient``. The x402 facilitator isn't
hit here — the gate just emits 402 when no ``X-PAYMENT`` header is
attached. On-chain settlement is a follow-up slice.
"""

from __future__ import annotations

import json

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

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
    assert requirement["network"] == "base-sepolia"
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
    fake_mcp_app: Starlette, x402_gate: X402Gate
) -> None:
    """When the client attaches a shape-valid ``X-PAYMENT`` payload we
    forward to the upstream app. On-chain settlement is a later slice;
    this test pins the current "header present + parseable → forward"
    behavior so clients can dev-loop without waiting for facilitator
    wiring."""
    # Minimal PaymentPayload — the x402 schema will validate it.
    from x402.schemas import PaymentPayload

    payload = PaymentPayload(
        x402_version=2,
        payload={"signature": "0xabc", "authorization": {}},
        accepted={
            "scheme": "exact",
            "network": "base-sepolia",
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
    client = TestClient(x402_gate.asgi_middleware(fake_mcp_app))
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
    assert response.json()["error"].startswith("Invalid payment payload")


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
    assert accepts["network"] == "base"


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
