"""MPP (Machine Payments Protocol) payment gate — multi-method rail.

A parallel payment rail to ``X402Gate`` that speaks MPP's HTTP scheme
(``Authorization: Payment <credential>`` + ``WWW-Authenticate: Payment
<challenge>`` + ``Payment-Receipt: <receipt>``) instead of x402's
``X-PAYMENT`` / ``X-Payment-Response`` pair. Operators pick any
combination of two rails:

  - ``tempo``  — wallet-to-wallet USDC on the Tempo L2 (no Stripe,
                 no Coinbase CDP auth; crypto-native only).
  - ``stripe`` — Stripe PaymentIntents via pympp's Stripe method
                 (any payment_method_types the operator's Stripe
                 account supports: card, apple_pay, etc.).

When more than one method is configured, the 402 challenge advertises
*all* of them (each method emits its own ``WWW-Authenticate: Payment
...`` header) and the client picks one when signing. The incoming
``Authorization: Payment <credential>`` is dispatched back to the right
``Mpp`` instance using ``credential.challenge.method``.

Structural mirror of ``X402Gate``: single ASGI middleware that peeks
JSON-RPC on POST ``/mcp``, gates ``tools/call``, passes through
``tools/list``. The gate keeps the same "discovery free, invocation
paid" posture.

Why this can't be pympp's ``@pay`` decorator: FastMCP is a single POST
endpoint and payment metadata is tool-specific, so we need to peek the
JSON-RPC body to know which tool is being called and compute the price
before issuing a challenge. The ``@pay`` decorator wires to a specific
endpoint with a static request dict. We call ``Mpp.charge()`` directly
per request instead.

Multi-method workaround: pympp 0.6's ``mpp.server.Mpp`` constructor
accepts a single ``method=`` argument — there is no native multi-method
Mpp today. We therefore build one ``Mpp`` instance per configured rail
(sharing a single ``secret_key`` so HMAC-bound challenges remain valid
regardless of which handler issued them) and route each request by:

  - unpaid / malformed Authorization header → call every ``Mpp.charge``
    with ``authorization=None`` and emit one ``WWW-Authenticate`` header
    per method (HTTP allows multiple values);
  - valid-shaped Authorization → parse ``credential.challenge.method``
    and dispatch to the matching handler only.

When pympp ships a native multi-method ``Mpp`` we'll collapse this back
down to a single instance; the shared ``secret_key`` is the invariant
that keeps that upgrade straightforward.

Settlement lifecycle::

    1. Peek body → decide if this is a paid tools/call
    2. No Authorization header        → 402 + WWW-Authenticate (one
                                         per configured method) + a
                                         JSON-RPC error (-32042)
    3. Malformed credential           → 402, fresh challenges
    4. verify_or_challenge rejects    → 402, fresh challenges
    5. Credential OK (returns (cred, receipt)) → forward request,
       capture upstream response, attach Payment-Receipt header.

Note on the response receipt header name: pympp's ``Receipt`` object
serializes via ``to_payment_receipt()`` to a base64url string intended
for a ``Payment-Receipt`` response header (see ``mpp/_parsing.py``).
The IETF httpauth draft also defines ``Authentication-Info`` as a
carrier for auth-scheme metadata; we emit both headers so clients
written against either version of the spec can find the receipt. The
primary, pympp-native header is ``Payment-Receipt``.
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from .base import PriceTable

logger = logging.getLogger(__name__)

# Friendly aliases → pympp chain IDs. Kept small on purpose — only
# Tempo mainnet (4217) and the Moderato testnet (42431) are meaningful
# today; an operator who wants something exotic can pass MPP_RPC_URL.
_NETWORK_ALIASES = {
    "tempo-testnet": 42431,
    "tempo-mainnet": 4217,
    "moderato":      42431,
    "tempo":         4217,
}

# Chain IDs considered "mainnet" — require explicit opt-in. Mirrors the
# x402 ``_MAINNET_NETWORKS`` guard so a typo in ``MPP_NETWORK`` can't
# silently start accepting real USDC.
_MAINNET_CHAIN_IDS = {4217}

# MCP's standard HTTP path. FastMCP's ``http_app()`` mounts the
# StreamableHTTP app at ``/mcp`` by default; if an operator reroutes it
# the env var ``MPP_MCP_PATH`` (read in __main__) can adjust this.
DEFAULT_MCP_PATH = "/mcp"

# Soft cap on body size we'll buffer for JSON-RPC peeking — same value
# the x402 gate uses. MCP payloads are well under a kilobyte; 1 MiB is
# paranoid-safe without being a DoS surface.
_MAX_PEEK_BYTES = 1 * 1024 * 1024

# JSON-RPC error code reserved for payment gating per
# draft-payment-transport-mcp-00 (pympp's MCP extension constant).
# We emit HTTP 402 *and* a JSON-RPC error body carrying this code so
# clients can parse the failure either way.
ERR_CODE_PAYMENT_REQUIRED = -32042

# HTTP header names the gate reads/writes. Kept as lowercase bytes so
# they compare cleanly against the ASGI scope headers list.
_AUTH_HEADER = b"authorization"
_WWW_AUTH_HEADER = b"www-authenticate"
_PAYMENT_RECEIPT_HEADER = b"payment-receipt"
_AUTHENTICATION_INFO_HEADER = b"authentication-info"


def _normalize_network(network: str) -> int:
    """Map friendly name → Tempo chain ID; raise on unknown aliases.

    The factory layer is responsible for mainnet opt-in enforcement;
    this helper only handles the alias → int translation.
    """
    key = (network or "").strip().lower()
    if not key:
        raise ValueError("MppGate: empty network name")
    if key in _NETWORK_ALIASES:
        return _NETWORK_ALIASES[key]
    # Accept raw chain IDs ("42431", "4217") so operators who prefer
    # explicit values aren't forced through the alias map.
    try:
        return int(key)
    except ValueError:
        raise ValueError(
            f"MppGate: unknown network {network!r}; expected one of "
            f"{sorted(_NETWORK_ALIASES)} or a numeric Tempo chain ID."
        ) from None


def is_mainnet(network: str) -> bool:
    """True if ``network`` resolves to a Tempo mainnet chain ID.

    Exposed at module scope so the factory can perform its opt-in check
    before constructing the gate — same pattern as ``x402._MAINNET_NETWORKS``.
    """
    try:
        return _normalize_network(network) in _MAINNET_CHAIN_IDS
    except ValueError:
        return False


@dataclass(frozen=True)
class _PaymentDecision:
    """Result of peeking a request. ``tool_name`` is None for discovery."""

    gate: bool          # True → must pay; False → free
    tool_name: str | None
    rpc_id: Any         # echo back in JSON-RPC error body


class MppGate:
    """Gate that returns HTTP 402 for unpaid JSON-RPC ``tools/call`` requests.

    Structurally identical to :class:`~plaid_mcp.payments.x402.X402Gate`
    but speaks the MPP wire format (``Authorization: Payment`` +
    ``WWW-Authenticate: Payment`` + ``Payment-Receipt``) and can settle
    across multiple rails (Tempo stablecoin, Stripe cards, or both).
    """

    name = "mpp"

    def __init__(
        self,
        *,
        destination_address: str | None = None,
        network: str = "tempo-testnet",
        prices: PriceTable,
        mcp_path: str = DEFAULT_MCP_PATH,
        secret_key: str | None = None,
        rpc_url: str | None = None,
        realm: str = "plaid-mcp",
        currency: str | None = None,
        methods: list[str] | None = None,
        stripe_secret_key: str | None = None,
        stripe_payment_method_types: list[str] | None = None,
        stripe_currency: str | None = None,
        mpp_handler: Any | None = None,
        mpp_handlers: dict[str, Any] | None = None,
    ) -> None:
        """Build a gate.

        Args:
            destination_address: Tempo wallet that receives USDC. Required
                when ``"tempo"`` appears in ``methods``.
            network: Friendly alias (``tempo-testnet`` /
                ``tempo-mainnet``) or raw chain ID. Ignored when ``tempo``
                isn't in ``methods``.
            prices: Per-tool ``PriceTable`` keyed by MCP tool name.
            mcp_path: HTTP path where the MCP transport is mounted.
            secret_key: HMAC secret for stateless challenge verification.
                Auto-generated if unset; override via ``MPP_SECRET_KEY``
                when you need restarts to leave outstanding challenges
                valid. Shared across all ``Mpp`` instances.
            rpc_url: Override Tempo RPC endpoint. Defaults to pympp's
                chain-ID-derived URL.
            realm: Realm string sent in ``WWW-Authenticate`` challenges.
            currency: Tempo TIP-20 token contract. Defaults to pympp's
                chain-appropriate value (USDC on mainnet, pathUSD on
                testnet).
            methods: List of MPP method names to advertise. Defaults to
                ``["tempo"]`` for backward compatibility with the
                single-rail constructor. Accepted values: ``"tempo"``,
                ``"stripe"``.
            stripe_secret_key: Stripe API secret (``sk_live_...`` or
                ``sk_test_...``). Required when ``"stripe"`` in
                ``methods``.
            stripe_payment_method_types: Stripe PaymentIntent
                ``payment_method_types``. Defaults to ``["card"]``.
            stripe_currency: Stripe ISO 4217 currency code. Defaults to
                ``"usd"``.
            mpp_handler: Backward-compat single-handler DI seam. Treated
                as the Tempo handler when supplied — kept to avoid
                breaking the pre-multi-method test fixtures.
            mpp_handlers: Per-method DI seam: ``{"tempo": ..., "stripe":
                ...}``. When None, real ``Mpp`` instances are built for
                each entry in ``methods``.
        """
        # Normalize + validate the method list up front so misconfigured
        # callers see a clear error before we try to import pympp.
        # ``None`` → default to ``["tempo"]`` for backward-compat with the
        # single-rail constructor; an explicit empty list is a bug.
        if methods is None:
            raw_methods: list[str] = ["tempo"]
        else:
            raw_methods = list(methods)
        if not raw_methods:
            raise ValueError("MppGate: 'methods' must be a non-empty list.")
        requested = [m.strip().lower() for m in raw_methods if m and m.strip()]
        if not requested:
            raise ValueError("MppGate: 'methods' must be a non-empty list.")
        unknown = [m for m in requested if m not in {"tempo", "stripe"}]
        if unknown:
            raise ValueError(
                f"MppGate: unknown method(s) {unknown!r}; expected 'tempo' or 'stripe'."
            )

        # Tempo bookkeeping — only materialized when tempo is requested.
        if "tempo" in requested:
            if not destination_address:
                raise ValueError(
                    "MppGate: destination_address is required when 'tempo' "
                    "is in methods."
                )
            chain_id = _normalize_network(network)
        else:
            chain_id = 0  # unused; keeps the attribute typed as int.

        if "stripe" in requested and not stripe_secret_key:
            raise ValueError(
                "MppGate: stripe_secret_key is required when 'stripe' is in methods."
            )

        self.destination_address = destination_address
        self.chain_id = chain_id
        self.network = network
        self.prices = prices
        self.mcp_path = mcp_path
        self.realm = realm
        self.rpc_url = rpc_url
        self.currency = currency
        self.methods = requested
        self.stripe_secret_key = stripe_secret_key
        self.stripe_payment_method_types = list(stripe_payment_method_types or ["card"])
        self.stripe_currency = stripe_currency or "usd"
        # When an operator doesn't supply a secret, synthesize one so
        # challenges are HMAC-bound but volatile across restarts. Matches
        # pympp's own ``detect_secret_key`` default behavior from an
        # in-memory seed rather than env. Callers that want stable
        # restarts pass a key via the constructor (factory wires env).
        self.secret_key = secret_key or secrets.token_urlsafe(32)

        # Resolve the per-method handler dict. Priority:
        #   1. explicit ``mpp_handlers`` dict (tests)
        #   2. legacy ``mpp_handler=`` wired in as the tempo handler
        #      (tests that predate multi-method)
        #   3. build real pympp ``Mpp`` instances per requested method.
        if mpp_handlers is not None:
            self._mpps: dict[str, Any] = dict(mpp_handlers)
        elif mpp_handler is not None:
            # Backward compat: a single injected handler is treated as
            # the tempo handler. This keeps pre-multi-method fixtures
            # working without churn.
            self._mpps = {requested[0]: mpp_handler}
        else:
            self._mpps = self._build_mpp_handlers()

    # ------------------------------------------------------------------
    # Backward-compat alias — some tests and external code still read
    # ``gate._mpp``. We expose the first configured handler under that
    # attribute so the single-method accessor keeps working.
    # ------------------------------------------------------------------

    @property
    def _mpp(self) -> Any:
        """First configured handler — kept for single-method compat callers."""
        # iterate ``self.methods`` so the order matches the requested
        # configuration (operator-controlled priority).
        for name in self.methods:
            if name in self._mpps:
                return self._mpps[name]
        # Fallback: any handler we have. Shouldn't normally fire.
        return next(iter(self._mpps.values()))

    # ------------------------------------------------------------------
    # pympp handler wiring
    # ------------------------------------------------------------------

    def _build_mpp_handlers(self) -> dict[str, Any]:
        """Construct one pympp ``Mpp`` per requested method.

        Imported lazily so the module is importable on a machine without
        the ``[mpp]`` extra installed. Operators who set ``PAYWALL=mpp``
        without installing the extra see a clear ImportError at gate
        construction time instead of a silent misconfiguration.
        """
        try:
            from mpp.server import Mpp
        except ImportError as exc:  # pragma: no cover — structural import guard
            raise RuntimeError(
                "PAYWALL=mpp requires the 'mpp' extra. "
                "Install with: uv sync --extra mpp "
                "(or pip install 'plaid-mcp[mpp]')."
            ) from exc

        handlers: dict[str, Any] = {}
        if "tempo" in self.methods:
            try:
                from mpp.methods.tempo import ChargeIntent as TempoChargeIntent
                from mpp.methods.tempo import tempo as tempo_method
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "PAYWALL=mpp with tempo method requires the 'tempo' extra "
                    "on pympp. Install: uv sync --extra mpp."
                ) from exc

            tempo = tempo_method(
                chain_id=self.chain_id,
                rpc_url=self.rpc_url,
                recipient=self.destination_address,
                currency=self.currency,
                intents={
                    "charge": TempoChargeIntent(
                        chain_id=self.chain_id, rpc_url=self.rpc_url
                    )
                },
            )
            handlers["tempo"] = Mpp(
                method=tempo, realm=self.realm, secret_key=self.secret_key
            )

        if "stripe" in self.methods:
            try:
                from mpp.methods.stripe import ChargeIntent as StripeChargeIntent
                from mpp.methods.stripe import stripe as stripe_method
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError(
                    "PAYWALL=mpp with stripe method requires the 'stripe' extra "
                    "on pympp. Install: uv sync --extra mpp."
                ) from exc

            stripe = stripe_method(
                intents={
                    "charge": StripeChargeIntent(secret_key=self.stripe_secret_key)
                },
                currency=self.stripe_currency,
                payment_method_types=self.stripe_payment_method_types,
                # pympp requires a ``recipient`` string even though Stripe
                # settles into the merchant's own account (identified by
                # the secret key). We echo a marker so the challenge
                # serializes; it isn't used by Stripe itself.
                recipient=self.destination_address or "stripe-account",
            )
            handlers["stripe"] = Mpp(
                method=stripe, realm=self.realm, secret_key=self.secret_key
            )

        return handlers

    # ------------------------------------------------------------------
    # ASGI entrypoint
    # ------------------------------------------------------------------

    def asgi_middleware(self, app: Any) -> Any:
        """Return an ASGI callable wrapping ``app``."""

        async def _wrapped(scope: dict, receive, send) -> None:
            if scope.get("type") != "http":
                await app(scope, receive, send)
                return

            # Only meter POSTs on the MCP path. Everything else (GETs
            # for SSE, health checks, static routes) passes through
            # untouched — same policy as X402Gate.
            if scope.get("method") != "POST" or scope.get("path") != self.mcp_path:
                await app(scope, receive, send)
                return

            body, receive_replay = await _buffer_body(receive)
            decision = _decide(body, self.prices)

            if not decision.gate:
                await app(scope, receive_replay, send)
                return

            headers = _headers_dict(scope.get("headers") or [])
            auth_header = headers.get(_AUTH_HEADER)
            authorization = auth_header.decode("ascii", errors="replace") if auth_header else None

            tool = decision.tool_name or "unknown"
            amount_str = _cents_to_amount(self.prices.for_tool(tool))

            target_handler = self._route_authorization(authorization)
            if target_handler is None:
                # No credential (or unparseable): collect a fresh challenge
                # from every configured method and return them together.
                challenges = await self._gather_challenges(amount_str, tool)
                await self._send_402(send, decision, challenges)
                return

            # Credential parsed and routed to a specific handler. Let
            # that handler verify.
            try:
                result = await target_handler.charge(
                    authorization=authorization,
                    amount=amount_str,
                    extra={"tool": tool},
                )
            except Exception as exc:  # noqa: BLE001 — any pympp error = treat as challenge
                # pympp raises on malformed credentials / verification
                # failures; the cleanest recovery is to re-issue fresh
                # challenges (across all methods) rather than surfacing a
                # 5xx. Log for operator visibility.
                logger.warning("mpp charge() errored, re-challenging: %s", exc)
                challenges = await self._gather_challenges(amount_str, tool)
                await self._send_402(send, decision, challenges)
                return

            # pympp returns a Challenge instance when payment is missing
            # or invalid, and a (Credential, Receipt) tuple when verified.
            if _is_challenge(result):
                # Even when a specific handler rejects, it's cheapest to
                # also offer the other configured methods as alternatives.
                # Start from this rejection and top up with siblings.
                challenges = [result]
                challenges.extend(
                    await self._gather_challenges(
                        amount_str, tool, skip={result.method}
                    )
                )
                await self._send_402(send, decision, challenges)
                return

            credential, receipt = result
            await self._forward_and_attach_receipt(
                app, scope, receive_replay, send, receipt
            )

        return _wrapped

    # ------------------------------------------------------------------
    # Multi-method dispatch helpers
    # ------------------------------------------------------------------

    def _route_authorization(self, authorization: str | None) -> Any | None:
        """Pick the ``Mpp`` instance responsible for ``authorization``.

        Returns ``None`` when the header is missing or un-parseable — the
        caller then emits a fresh challenge instead of trying to verify.
        When exactly one method is configured we skip parsing and return
        that handler directly, so a malformed-but-present Authorization
        header still gets a chance at pympp's own re-challenge path
        (preserving backward-compatible behavior).
        """
        if not authorization:
            return None
        if len(self._mpps) == 1:
            # Single-method deployments: delegate to the one handler we
            # have; pympp re-challenges on parse failure.
            return next(iter(self._mpps.values()))

        try:
            from mpp import Credential
        except ImportError:  # pragma: no cover — shouldn't hit here
            return None
        try:
            cred = Credential.from_authorization(authorization)
            method = cred.challenge.method
        except Exception:  # noqa: BLE001 — malformed header = no handler
            return None
        return self._mpps.get(method)

    async def _gather_challenges(
        self, amount_str: str, tool: str, *, skip: set[str] | None = None
    ) -> list[Any]:
        """Request a fresh challenge from each configured handler.

        Keeps the ordering stable (iterates ``self.methods``) so the 402
        always lists methods in the operator-declared priority. ``skip``
        lets the caller drop methods that already produced a challenge
        this request — avoids sending duplicate ``WWW-Authenticate``
        headers for the same method.
        """
        skip = skip or set()
        out: list[Any] = []
        for name in self.methods:
            if name in skip:
                continue
            handler = self._mpps.get(name)
            if handler is None:
                continue
            try:
                result = await handler.charge(
                    authorization=None, amount=amount_str, extra={"tool": tool}
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("mpp %s challenge mint errored: %s", name, exc)
                continue
            if _is_challenge(result):
                out.append(result)
        return out

    # ------------------------------------------------------------------
    # 402 response
    # ------------------------------------------------------------------

    async def _send_402(
        self, send, decision: _PaymentDecision, challenges: Any
    ) -> None:
        """Emit a 402 with one ``WWW-Authenticate`` per challenge.

        Body shape: ``{"jsonrpc": "2.0", "id": <rpc_id>, "error":
        {"code": -32042, "message": "Payment Required", "data":
        {"httpStatus": 402, "methods": [...]}}}``. Matches
        draft-payment-transport-mcp-00 so MCP-aware clients can dispatch
        on the ``code`` field; HTTP-aware clients can dispatch on the
        status + ``WWW-Authenticate`` headers.

        Accepts either a single challenge (backward compat) or a list —
        this keeps the pre-multi-method test fixtures working while
        letting the new path emit multiple challenges cleanly.
        """
        if isinstance(challenges, list):
            chs = challenges
        else:
            chs = [challenges]

        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", b"application/json"),
            (b"cache-control", b"no-store"),
            (b"x-payment-required", b"1"),
        ]
        methods_meta: list[dict[str, Any]] = []
        for challenge in chs:
            www_auth = challenge.to_www_authenticate(self.realm)
            # HTTP allows repeating ``WWW-Authenticate`` to advertise
            # multiple auth schemes (RFC 9110 §11.6.1). Emit one header
            # per method rather than coalescing; keeps pympp's per-method
            # challenge strings intact and lets the client pick.
            headers.append((_WWW_AUTH_HEADER, www_auth.encode("utf-8")))
            methods_meta.append(
                {
                    "method": challenge.method,
                    "intent": challenge.intent,
                    "challengeId": challenge.id,
                }
            )

        body_obj = {
            "jsonrpc": "2.0",
            "id": decision.rpc_id,
            "error": {
                "code": ERR_CODE_PAYMENT_REQUIRED,
                "message": "Payment Required",
                "data": {
                    "httpStatus": 402,
                    "realm": self.realm,
                    "methods": methods_meta,
                    # Back-compat single-method fields: downstream clients
                    # that only read the first method still work.
                    **(
                        {
                            "method": methods_meta[0]["method"],
                            "intent": methods_meta[0]["intent"],
                            "challengeId": methods_meta[0]["challengeId"],
                        }
                        if methods_meta
                        else {}
                    ),
                },
            },
        }
        body = json.dumps(body_obj, separators=(",", ":")).encode("utf-8")
        headers.append((b"content-length", str(len(body)).encode()))

        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})

    # ------------------------------------------------------------------
    # Successful-payment forwarding
    # ------------------------------------------------------------------

    async def _forward_and_attach_receipt(
        self,
        app,
        scope: dict,
        receive,
        send,
        receipt: Any,
    ) -> None:
        """Run the wrapped app and inject the receipt header on the way out.

        Receipt attachment failures do NOT block the response — the user
        already paid and received their tool output; a bad header is our
        operational problem, not theirs. We log and fall through to send
        the response as-is. Mirrors ``X402Gate._forward_and_settle``.
        """
        forwarded_start = False
        receipt_header: bytes | None = None
        try:
            receipt_header = receipt.to_payment_receipt().encode("ascii")
        except Exception as exc:  # noqa: BLE001
            logger.error("mpp receipt serialize failed: %s", exc, exc_info=True)

        async def _send(message: dict) -> None:
            nonlocal forwarded_start
            if message["type"] == "http.response.start" and not forwarded_start:
                start_msg = dict(message)
                if receipt_header is not None:
                    headers = list(start_msg.get("headers") or [])
                    # Emit both the pympp-native `Payment-Receipt` and
                    # the RFC-style `Authentication-Info`. See the
                    # module docstring for why we send both.
                    headers.append((_PAYMENT_RECEIPT_HEADER, receipt_header))
                    headers.append((_AUTHENTICATION_INFO_HEADER, receipt_header))
                    start_msg["headers"] = headers
                forwarded_start = True
                await send(start_msg)
            else:
                await send(message)

        await app(scope, receive, _send)


# ----------------------------------------------------------------------
# Helpers — body buffering + JSON-RPC decision (shape-aligned with x402).
# ----------------------------------------------------------------------


def _cents_to_amount(cents: int) -> str:
    """Convert integer USD cents to the ``"D.DD"`` string pympp expects.

    ``Mpp.charge(amount=...)`` accepts a human-readable string and
    internally converts it to base units via the method's decimals. We
    quantize to two decimal places so 5 cents → "0.05", 50 cents →
    "0.50", etc.
    """
    c = max(int(cents), 0)
    return f"{c // 100}.{c % 100:02d}"


def _is_challenge(obj: Any) -> bool:
    """True if ``obj`` is a pympp Challenge (vs. a (Credential, Receipt) tuple).

    Uses duck-typing on the ``.to_www_authenticate`` method so tests can
    substitute a lightweight stub without importing the real Challenge
    class. Real pympp Challenges are frozen dataclasses so isinstance
    would also work, but keeping this structural avoids a hard import
    in the happy path.
    """
    return hasattr(obj, "to_www_authenticate") and hasattr(obj, "id") and hasattr(obj, "intent")


def _headers_dict(raw: list) -> dict[bytes, bytes]:
    """Flatten ASGI headers into a lowercase-key dict.

    ASGI gives us ``list[tuple[bytes, bytes]]`` with potentially
    duplicate keys; for the gate we only care about first-occurrence
    values of Authorization, which is single-valued per RFC 9110.
    """
    out: dict[bytes, bytes] = {}
    for k, v in raw:
        key = k.lower() if isinstance(k, bytes) else k
        if key not in out:
            out[key] = v
    return out


async def _buffer_body(receive):
    """Drain the ASGI receive channel, returning (body_bytes, replay_receive).

    Same shape as ``x402._buffer_body``; kept local so each gate module
    is self-contained and changes to one don't accidentally regress the
    other.
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
        return await receive()

    return body, replay


def _decide(body: bytes, prices: PriceTable) -> _PaymentDecision:
    """Classify a JSON-RPC request as free or gated.

    Identical semantics to ``x402._decide`` — kept local so the two
    gates can evolve independently without tight coupling.
    """
    if not body:
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _PaymentDecision(gate=False, tool_name=None, rpc_id=None)

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

    # Silence unused-var warning — prices is part of the signature for
    # symmetry with x402 even though MppGate uses self.prices.for_tool
    # at the call site.
    _ = prices
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
