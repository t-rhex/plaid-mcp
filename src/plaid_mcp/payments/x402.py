"""x402 payment gate — custom ASGI middleware with real facilitator verify + settle.

Why a custom middleware instead of the ``x402[fastapi]`` middleware:
FastMCP runs on Starlette directly (no FastAPI), and MCP traffic is
JSON-RPC over a single POST path, so we need to peek the request body
to know which JSON-RPC method (``tools/call`` vs ``tools/list``) is
being invoked and what the tool name is (for per-tool pricing). The
off-the-shelf FastAPI middleware only scopes by URL path.

The middleware delegates actual payment verification to the x402
HTTP facilitator (``HTTPFacilitatorClient`` hitting a hosted
facilitator URL — ``https://x402.org/facilitator`` by default) and
reuses the x402 schemas (``PaymentRequirements``, ``PaymentRequired``,
``PaymentPayload``) so the 402 body shape is exactly what x402 clients
expect.

We use the *remote* HTTP facilitator (``x402.http.HTTPFacilitatorClient``)
rather than a local ``x402Facilitator`` with schemes registered in-
process. That means the EVM exact scheme mechanics (EIP-3009 signature
recovery, USDC settlement) live at the hosted facilitator — we only
forward the signed payload. The ``x402.mechanisms.evm.exact``
``ExactEvmFacilitatorScheme`` is what the *remote* service registers;
our role is to format the verify/settle HTTP calls correctly, which
``HTTPFacilitatorClient`` handles.

Verify/settle lifecycle per request::

    1. Peek body → decide if this is a paid tools/call
    2. No X-PAYMENT header     → 402, no `error` (standard initial quote)
    3. Malformed X-PAYMENT     → 402, error="invalid_payment_header"
    4. Facilitator.verify fails → 402, error=<facilitator invalid_reason>
    5. verify OK → forward request, capture upstream response,
       then facilitator.settle(); attach X-Payment-Response header.
       Settle failures are LOGGED but do NOT block the upstream
       response — the user already got what they paid for; settle
       failure is our operational problem, not theirs.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from x402.http import (
    DEFAULT_FACILITATOR_URL,
    FacilitatorConfig,
    HTTPFacilitatorClient,
    encode_payment_response_header,
)
from x402.schemas import (
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    VerifyResponse,
)

from .base import PriceTable

logger = logging.getLogger(__name__)

# USDC contract addresses keyed by CAIP-2 network ID. x402 v2 schemas
# (ExactEvmClientScheme etc.) identify networks via CAIP-2, so we emit
# the same identifiers in our PaymentRequirements. Friendly aliases
# ("base-sepolia" / "base") are accepted at construction time and
# normalized here so operators don't have to remember chain IDs.
_USDC_ASSETS = {
    "eip155:84532": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Base Sepolia
    "eip155:8453":  "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # Base mainnet
}

# EIP-712 domain info for the exact-EVM scheme. The facilitator uses
# ``extra.name`` + ``extra.version`` to build the TransferWithAuthorization
# typed-data hash; omitting them (or getting them wrong) causes rejection
# as ``invalid_exact_evm_missing_eip712_domain`` or a silent
# ``invalid_payload`` on signature recovery.
#
# The values here are what Circle's USDC deployment reports on-chain via
# ``name()`` and ``version()``. Base mainnet and Base Sepolia disagree on
# the name string — mainnet is "USD Coin" (with space), Sepolia ships as
# "USDC". Getting this wrong at the client side produces a signed payload
# whose recovered address doesn't match authorization.from.
_USDC_DOMAIN = {
    "eip155:84532": {"name": "USDC",     "version": "2"},  # Base Sepolia
    "eip155:8453":  {"name": "USD Coin", "version": "2"},  # Base mainnet
}

# Friendly aliases → CAIP-2. The list is small on purpose — only
# networks we actually test.
_NETWORK_ALIASES = {
    "base-sepolia": "eip155:84532",
    "base": "eip155:8453",
}

# Networks considered "mainnet" — require explicit opt-in so a typo in
# config can't silently start accepting real USDC.
_MAINNET_NETWORKS = {"eip155:8453"}


def _normalize_network(network: str) -> str:
    """Map friendly names → CAIP-2; pass CAIP-2 through unchanged."""
    return _NETWORK_ALIASES.get(network, network)

# 6 decimals for USDC → 1 cent = 10,000 atomic units.
_ATOMIC_PER_CENT = 10_000

# Request header clients use to attach the signed payment payload.
# x402 v2 also accepts ``Payment-Signature`` but ``X-PAYMENT`` is the
# legacy-compatible name most clients send.
_PAYMENT_HEADER = b"x-payment"

# Response header carrying the base64-encoded SettleResponse per x402 spec.
_PAYMENT_RESPONSE_HEADER = b"x-payment-response"

# MCP's standard HTTP path. FastMCP's ``http_app()`` mounts the
# StreamableHTTP app at ``/mcp`` by default; if an operator reroutes
# it, the env var ``X402_MCP_PATH`` (read in __main__) can adjust this.
DEFAULT_MCP_PATH = "/mcp"

# Soft cap on body size we'll buffer for JSON-RPC peeking. Anything
# beyond this is passed through uninspected — MCP payloads should be
# well under a kilobyte, so 1 MiB is paranoid-safe without being a DoS
# surface.
_MAX_PEEK_BYTES = 1 * 1024 * 1024

# Machine-readable error strings set on the 402 `error` field. These
# map one-to-one with the VerifyResponse.invalid_reason values the
# facilitator returns, plus two local ones the gate sets itself.
ERR_INVALID_PAYMENT_HEADER = "invalid_payment_header"
ERR_VERIFY_FAILED = "invalid_payment"


class FacilitatorLike(Protocol):
    """Subset of the x402 FacilitatorClient protocol we use.

    Declared locally so tests can supply a fake without importing
    the full HTTPFacilitatorClient surface (e.g. a dataclass stub).
    """

    async def verify(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> VerifyResponse: ...

    async def settle(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> SettleResponse: ...


@dataclass(frozen=True)
class _PaymentDecision:
    """Result of peeking a request. ``tool_name`` is None for discovery."""

    gate: bool          # True → must pay; False → free (discovery or not a tool call)
    tool_name: str | None
    rpc_id: Any         # echo back in 402 response


def _validate_network_facilitator(network: str, facilitator_url: str | None) -> None:
    """Fail fast if a mainnet facilitator URL is paired with testnet network
    (or the reverse). Heuristic — we can't know every facilitator's chain
    set, but we can catch the obvious typos.
    """
    if not facilitator_url:
        return
    url = facilitator_url.lower()
    url_is_testnet = ("sepolia" in url) or ("testnet" in url)
    url_is_mainnet = "mainnet" in url
    net_is_mainnet = network in _MAINNET_NETWORKS

    if url_is_testnet and net_is_mainnet:
        raise RuntimeError(
            f"X402 facilitator URL {facilitator_url!r} looks like a testnet "
            f"facilitator but X402_NETWORK={network!r} is mainnet. Refusing "
            "to start with mismatched network/facilitator."
        )
    if url_is_mainnet and not net_is_mainnet:
        raise RuntimeError(
            f"X402 facilitator URL {facilitator_url!r} looks like a mainnet "
            f"facilitator but X402_NETWORK={network!r} is testnet. Refusing "
            "to start with mismatched network/facilitator."
        )


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
        facilitator: FacilitatorLike | None = None,
    ) -> None:
        network = _normalize_network(network)
        if network not in _USDC_ASSETS:
            raise ValueError(
                f"X402Gate: unsupported network {network!r}; "
                f"expected one of {sorted(_USDC_ASSETS)} "
                f"(aliases: {sorted(_NETWORK_ALIASES)})."
            )

        _validate_network_facilitator(network, facilitator_url)

        self.receiving_address = receiving_address
        self.network = network
        # Resolve the effective URL up front so the tests + the smoke
        # log line can see which facilitator we're about to hit.
        self.facilitator_url = facilitator_url or DEFAULT_FACILITATOR_URL
        self.prices = prices
        self.mcp_path = mcp_path
        self._asset = _USDC_ASSETS[network]

        # Allow tests to inject a stub via ``facilitator=``; the real
        # HTTPFacilitatorClient performs actual HTTP to the facilitator.
        self._facilitator: FacilitatorLike = facilitator or HTTPFacilitatorClient(
            FacilitatorConfig(url=self.facilitator_url)
        )

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

            headers = dict(scope.get("headers") or [])
            payment_header = headers.get(_PAYMENT_HEADER)

            # No header → standard initial 402 quote (no `error` field).
            if not payment_header:
                await self._send_402(send, decision, error=None)
                return

            # Header present but not parseable per the x402 schema.
            try:
                payment_payload = PaymentPayload.model_validate_json(payment_header)
            except Exception:  # noqa: BLE001 — any parse error = payment invalid
                await self._send_402(send, decision, error=ERR_INVALID_PAYMENT_HEADER)
                return

            # Build the requirements this caller *should* have paid
            # against, and verify the signed payload with the facilitator.
            requirements = self._payment_requirements(decision.tool_name or "unknown")

            try:
                verify_response = await self._facilitator.verify(
                    payment_payload, requirements
                )
            except Exception as exc:  # noqa: BLE001
                # Facilitator unreachable / bad response. Treat as a
                # verification failure so the client gets a clean 402
                # and can retry; don't expose internals via 5xx.
                logger.warning("x402 facilitator verify errored: %s", exc)
                await self._send_402(send, decision, error=ERR_VERIFY_FAILED)
                return

            if not verify_response.is_valid:
                reason = verify_response.invalid_reason or ERR_VERIFY_FAILED
                await self._send_402(send, decision, error=reason)
                return

            # Verify OK → forward the request, buffering the upstream
            # response so we can settle *after* FastMCP returns and
            # attach the X-Payment-Response header.
            await self._forward_and_settle(
                app, scope, receive_replay, send,
                payment_payload, requirements,
            )

        return _wrapped

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    async def _forward_and_settle(
        self,
        app,
        scope: dict,
        receive,
        send,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> None:
        """Run the wrapped app, then call facilitator.settle() once it
        returns ``http.response.start``. Attaches the settle response as
        ``X-Payment-Response``.

        Settle failures do NOT block the response — the user's tool call
        already ran. We just log the failure.
        """
        start_msg: dict | None = None
        forwarded_start = False

        # Run settle concurrently: the moment we see the upstream start
        # message we kick off settle, then we inject the response
        # header into the start message before sending it downstream.
        async def _send(message: dict) -> None:
            nonlocal start_msg, forwarded_start
            if message["type"] == "http.response.start" and not forwarded_start:
                start_msg = dict(message)
                # Attach settle response header (or an error marker).
                settle_header = await self._run_settle(payload, requirements)
                if settle_header is not None:
                    headers = list(start_msg.get("headers") or [])
                    headers.append((_PAYMENT_RESPONSE_HEADER, settle_header))
                    start_msg["headers"] = headers
                forwarded_start = True
                await send(start_msg)
            else:
                await send(message)

        await app(scope, receive, _send)

    async def _run_settle(
        self,
        payload: PaymentPayload,
        requirements: PaymentRequirements,
    ) -> bytes | None:
        """Call facilitator.settle(), returning the base64 header bytes or
        None if settlement failed/errored. We never raise from here — the
        caller must return the upstream response regardless.
        """
        try:
            settle_response = await self._facilitator.settle(payload, requirements)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "x402 settle raised for pay_to=%s amount=%s: %s",
                requirements.pay_to, requirements.amount, exc,
                exc_info=True,
            )
            return None

        if not settle_response.success:
            logger.error(
                "x402 settle reported failure pay_to=%s amount=%s reason=%s message=%s",
                requirements.pay_to, requirements.amount,
                settle_response.error_reason, settle_response.error_message,
            )
            # Still propagate the structured response so clients can see
            # why — they already have their tool output, but knowing the
            # operator failed to settle may matter for retries.
            try:
                return encode_payment_response_header(settle_response).encode("ascii")
            except Exception:  # noqa: BLE001
                return None

        try:
            return encode_payment_response_header(settle_response).encode("ascii")
        except Exception as exc:  # noqa: BLE001
            logger.error("x402 encode settle header failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # 402 response
    # ------------------------------------------------------------------

    def _payment_requirements(self, tool_name: str) -> PaymentRequirements:
        cents = self.prices.for_tool(tool_name)
        atomic = str(max(cents, 0) * _ATOMIC_PER_CENT)
        domain = _USDC_DOMAIN.get(self.network, {})
        extra = {
            **domain,  # name, version — required by exact-EVM facilitator
            "tool": tool_name,
            "priceCents": cents,
        }
        return PaymentRequirements(
            scheme="exact",
            network=self.network,
            asset=self._asset,
            amount=atomic,
            pay_to=self.receiving_address,
            max_timeout_seconds=60,
            extra=extra,
        )

    def _build_402_body(
        self,
        decision: _PaymentDecision,
        error: str | None,
    ) -> bytes:
        tool = decision.tool_name or "unknown"
        requirements = self._payment_requirements(tool)
        resource = ResourceInfo(
            url=f"mcp://tool/{tool}",
            description=f"MCP tool: {tool}",
            mime_type="application/json",
        )
        # The x402 PaymentRequired schema marks ``error`` as required;
        # on the initial quote (no header sent yet) we emit the spec-
        # standard generic string so clients know "pay and retry".
        body = PaymentRequired(
            x402_version=X402_VERSION,
            error=error or "Payment Required",
            resource=resource,
            accepts=[requirements],
        )
        return body.model_dump_json(by_alias=True).encode("utf-8")

    async def _send_402(
        self,
        send,
        decision: _PaymentDecision,
        *,
        error: str | None,
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


