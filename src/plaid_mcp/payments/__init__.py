"""Payment gate adapters — plug-in paywall layer for the HTTP MCP transport.

Mirrors the ``providers/`` package shape: a small Protocol in ``base.py``,
concrete adapters as sibling modules, and a ``build_gate`` factory that
picks one based on :class:`Config`. The stdio transport bypasses all of
this — the paywall only kicks in when the server is run over HTTP and
``PAYWALL`` is explicitly set.
"""

from __future__ import annotations

from ..config import Config
from .base import PaymentGate, PriceTable
from .noop import NoopGate
from .prices import DEFAULT_PRICES
from .x402 import X402Gate

__all__ = [
    "DEFAULT_PRICES",
    "NoopGate",
    "PaymentGate",
    "PriceTable",
    "X402Gate",
    "build_gate",
]


def build_gate(config: Config) -> PaymentGate:
    """Pick the right payment gate for ``config``.

    - ``PAYWALL=none`` (or unset) → :class:`NoopGate` (stdio-class; no 402s).
    - ``PAYWALL=x402``            → :class:`X402Gate` wrapping the ASGI app
      with an HTTP 402 micropayment challenge per tool call.
    """
    mode = (config.paywall or "none").strip().lower()
    if mode == "none":
        return NoopGate()
    if mode == "x402":
        # Config.from_env() already validates that the receiving address is
        # set when paywall="x402", so asserting here is just belt + braces.
        assert config.x402_receiving_address, "X402_RECEIVING_ADDRESS required"
        return X402Gate(
            receiving_address=config.x402_receiving_address,
            network=config.x402_network,
            facilitator_url=config.x402_facilitator_url,
            prices=DEFAULT_PRICES,
        )
    raise ValueError(
        f"Unknown PAYWALL mode {mode!r}; expected 'none' or 'x402'."
    )
