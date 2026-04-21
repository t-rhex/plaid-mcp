"""PaymentGate Protocol + PriceTable — the shapes every gate adapter speaks.

Keeping the gate surface to a single method (``asgi_middleware``) lets us
bolt it in at one place in ``__main__.serve`` without threading gate
state through FastMCP's tool machinery. Concrete gates are responsible
for peeking the JSON-RPC payload and deciding whether a request gets a
free pass (discovery) or a 402 challenge (tool invocation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

# Default per-tool price when the caller asks for an unlisted tool. Kept low
# — the goal is "pay something, even for tools we forgot about" rather than
# blocking the call outright.
DEFAULT_TOOL_PRICE_CENTS = 10


@dataclass(frozen=True)
class PriceTable:
    """Map of MCP tool name → USD cents (int).

    Integer cents, not floats — payment amounts must be exact and the
    x402 payload serializes to USDC atomic units (cents × 10_000).
    """

    prices: dict[str, int] = field(default_factory=dict)
    default_cents: int = DEFAULT_TOOL_PRICE_CENTS

    def for_tool(self, name: str) -> int:
        """Return the price for ``name`` in USD cents.

        Unknown tools fall back to :attr:`default_cents` so adding a new
        MCP tool doesn't accidentally ship a free endpoint to paying users.
        """
        if not isinstance(name, str):
            return self.default_cents
        return self.prices.get(name, self.default_cents)


class PaymentGate(Protocol):
    """A middleware wrapper around FastMCP's Starlette ASGI app.

    Implementations return either the same app unchanged (``NoopGate``)
    or a wrapped ASGI callable that intercepts JSON-RPC ``tools/call``
    requests and answers with HTTP 402 when payment is missing.
    """

    name: str

    def asgi_middleware(self, app: Any) -> Any:
        """Return an ASGI app wrapping ``app``."""
        ...
