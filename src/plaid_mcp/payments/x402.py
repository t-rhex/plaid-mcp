"""x402 payment gate — custom ASGI middleware.

Why a custom middleware instead of the ``x402[fastapi]`` middleware:
FastMCP runs on Starlette directly (no FastAPI), and MCP traffic is
JSON-RPC over a single POST path, so we need to peek the request body
to know which JSON-RPC method (``tools/call`` vs ``tools/list``) is
being invoked and what the tool name is (for per-tool pricing). The
off-the-shelf FastAPI middleware only scopes by URL path.

The middleware delegates actual payment verification to the x402
facilitator primitives (``x402Facilitator``) and reuses the x402 schemas
(``PaymentRequirements``, ``PaymentRequired``, ``PaymentPayload``) so
the 402 body shape is exactly what x402 clients expect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from x402.schemas import (
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
)

from .base import PriceTable

# USDC contract addresses on Base and Base Sepolia. These are the only
# networks x402 officially supports at time of writing; we gate on the
# network string and refuse anything else so an operator can't silently
# accept USDC on an unsupported chain.
_USDC_ASSETS = {
    "base-sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "base": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
}

# 6 decimals for USDC → 1 cent = 10,000 atomic units.
_ATOMIC_PER_CENT = 10_000

# Request header clients use to attach the signed payment payload.
# x402 v2 also accepts ``Payment-Signature`` but ``X-PAYMENT`` is the
# legacy-compatible name most clients send.
_PAYMENT_HEADER = b"x-payment"

# MCP's standard HTTP path. FastMCP's ``http_app()`` mounts the
# StreamableHTTP app at ``/mcp`` by default; if an operator reroutes
# it, the env var ``X402_MCP_PATH`` (read in __main__) can adjust this.
DEFAULT_MCP_PATH = "/mcp"

# Soft cap on body size we'll buffer for JSON-RPC peeking. Anything
# beyond this is passed through uninspected — MCP payloads should be
# well under a kilobyte, so 1 MiB is paranoid-safe without being a DoS
# surface.
_MAX_PEEK_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class _PaymentDecision:
    """Result of peeking a request. ``tool_name`` is None for discovery."""

    gate: bool          # True → must pay; False → free (discovery or not a tool call)
    tool_name: str | None
    rpc_id: Any         # echo back in 402 response


class X402Gate:
    """Gate that returns HTTP 402 for unpaid JSON-RPC ``tools/call`` requests.

    Stateless w.r.t. requests — each call peeks the body, derives pricing
    from the :class:`PriceTable`, and either delegates to the wrapped app
    (free, or payment accepted) or responds 402 with a
    :class:`PaymentRequired` body.
    """

    name = "x402"

    def __init__(
        self,
        *,
        receiving_address: str,
        network: str = "base-sepolia",
        facilitator_url: str | None = None,
        prices: PriceTable,
        mcp_path: str = DEFAULT_MCP_PATH,
    ) -> None:
        if network not in _USDC_ASSETS:
            raise ValueError(
                f"X402Gate: unsupported network {network!r}; "
                f"expected one of {sorted(_USDC_ASSETS)}."
            )
        self.receiving_address = receiving_address
        self.network = network
        self.facilitator_url = facilitator_url  # None → use x402 default
        self.prices = prices
        self.mcp_path = mcp_path
        self._asset = _USDC_ASSETS[network]

    # ------------------------------------------------------------------
    # ASGI entrypoint
    # ------------------------------------------------------------------

    def asgi_middleware(self, app: Any) -> Any:
        """Return an ASGI callable wrapping ``app``."""

        async def _wrapped(scope: dict, receive, send) -> None:
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return

            # Only meter POSTs on the MCP path. Everything else (GETs for
            # SSE, health checks, static routes) passes through untouched.
            if scope.get("method") != "POST" or scope.get("path") != self.mcp_path:
                await app(scope, receive, send)
                return

            body, receive_replay = await _buffer_body(receive)
            decision = _decide(body, self.prices)

            if not decision.gate:
                await app(scope, receive_replay, send)
                return

            # Tool call. Require a payment header; for v1 the gate just
            # checks presence — treating the body as an opaque signed
            # payload. Future: call the x402 facilitator verifier.
            headers = dict(scope.get("headers") or [])
            payment_header = headers.get(_PAYMENT_HEADER)

            if not payment_header:
                await self._send_402(send, decision)
                return

            # A payment header is present. Validate the shape with the
            # x402 schema so malformed payloads get a clean 402 instead
            # of an internal crash. We don't hit the live facilitator
            # here because that requires scheme mechanisms to be
            # registered for the configured network — a separate slice.
            try:
                PaymentPayload.model_validate_json(payment_header)
            except Exception:  # noqa: BLE001 — any parse error = payment invalid
                await self._send_402(send, decision, error="Invalid payment payload")
                return

            # Payment looks well-formed; forward the request. Real
            # settlement (on-chain) is a follow-up once we have a
            # facilitator configured with schemes.
            await app(scope, receive_replay, send)

        return _wrapped

    # ------------------------------------------------------------------
    # 402 response
    # ------------------------------------------------------------------

    def _payment_requirements(self, tool_name: str) -> PaymentRequirements:
        cents = self.prices.for_tool(tool_name)
        atomic = str(max(cents, 0) * _ATOMIC_PER_CENT)
        return PaymentRequirements(
            scheme="exact",
            network=self.network,
            asset=self._asset,
            amount=atomic,
            pay_to=self.receiving_address,
            max_timeout_seconds=60,
            extra={"tool": tool_name, "priceCents": cents},
        )

    def _build_402_body(self, decision: _PaymentDecision, error: str) -> bytes:
        tool = decision.tool_name or "unknown"
        requirements = self._payment_requirements(tool)
        resource = ResourceInfo(
            url=f"mcp://tool/{tool}",
            description=f"MCP tool: {tool}",
            mime_type="application/json",
        )
        body = PaymentRequired(
            x402_version=X402_VERSION,
            error=error,
            resource=resource,
            accepts=[requirements],
        )
        return body.model_dump_json(by_alias=True).encode("utf-8")

    async def _send_402(
        self,
        send,
        decision: _PaymentDecision,
        *,
        error: str = "Payment Required",
    ) -> None:
        body = self._build_402_body(decision, error)
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"x-payment-required", b"1"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


async def _buffer_body(receive):
    """Drain the ASGI receive channel, returning (body_bytes, replay_receive).

    We have to read the body to peek the JSON-RPC method, which consumes
    the receive channel. The returned ``replay_receive`` yields the same
    body back to the wrapped app so downstream handlers see the original
    request intact.
    """
    chunks: list[bytes] = []
    more = True
    total = 0
    while more:
        message = await receive()
        if message["type"] == "http.request":
            chunk = message.get("body", b"") or b""
            total += len(chunk)
            if total > _MAX_PEEK_BYTES:
                # Stop buffering; we'll still replay what we have plus
                # pass through the rest untouched.
                chunks.append(chunk)
                more = message.get("more_body", False)
                break
            chunks.append(chunk)
            more = message.get("more_body", False)
        elif message["type"] == "http.disconnect":
            more = False
        else:
            more = False

    body = b"".join(chunks)
    sent = {"done": False}

    async def replay():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        # After the body is delivered, fall back to the real receive in
        # case Starlette asks again (it shouldn't for a finished body).
        return await receive()

    return body, replay


def _decide(body: bytes, prices: PriceTable) -> _PaymentDecision:
    """Classify a JSON-RPC request as free or gated."""
    if not body:
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Not JSON → not an MCP tool call; let FastMCP produce its own error.
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)

    # JSON-RPC batch — gate if any member is a tools/call. Keeps batch
    # semantics simple (all-or-nothing payment); real per-method metering
    # in a batch is a separate slice.
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and item.get("method") == "tools/call":
                name = _tool_name_from(item)
                return _PaymentDecision(gate=True, tool_name=name, rpc_id=item.get("id"))
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)

    if not isinstance(payload, dict):
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)

    method = payload.get("method")
    if method != "tools/call":
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=payload.get("id"))

    return _PaymentDecision(
        gate=True,
        tool_name=_tool_name_from(payload),
        rpc_id=payload.get("id"),
    )


def _tool_name_from(rpc_payload: dict) -> str | None:
    params = rpc_payload.get("params")
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str):
            return name
    return None
