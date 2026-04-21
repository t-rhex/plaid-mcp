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
from .mpp import MppGate
from .mpp import is_mainnet as _mpp_is_mainnet
from .noop import NoopGate
from .prices import DEFAULT_PRICES
from .x402 import X402Gate

__all__ = [
    "DEFAULT_PRICES",
    "MppGate",
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
      with an HTTP 402 micropayment challenge per tool call on Base.
    - ``PAYWALL=mpp``             → :class:`MppGate` wrapping the ASGI app
      with an HTTP 402 + ``WWW-Authenticate: Payment`` challenge per
      tool call on Tempo.
    """
    mode = (config.paywall or "none").strip().lower()
    if mode == "none":
        return NoopGate()
    if mode == "x402":
        # Config.from_env() already validates that the receiving address is
        # set when paywall="x402", so asserting here is just belt + braces.
        assert config.x402_receiving_address, "X402_RECEIVING_ADDRESS required"

        # Mainnet guard — refuse to bind the gate to real USDC unless the
        # operator explicitly opted in. Typos in config shouldn't silently
        # flip us from Sepolia (play money) to Base (real money).
        if config.x402_network in {"base"} and not config.x402_allow_mainnet:
            raise RuntimeError(
                f"X402_NETWORK={config.x402_network!r} is a mainnet network but "
                "X402_ALLOW_MAINNET is not set. Set X402_ALLOW_MAINNET=1 to "
                "confirm you want to accept real USDC on Base."
            )

        return X402Gate(
            receiving_address=config.x402_receiving_address,
            network=config.x402_network,
            facilitator_url=config.x402_facilitator_url,
            prices=DEFAULT_PRICES,
        )
    if mode == "mpp":
        # Same belt-and-braces assertion as x402 above — Config.from_env
        # already enforces this, but direct Config(...) construction in
        # tests can skip it.
        if not config.mpp_destination_address:
            raise RuntimeError(
                "PAYWALL=mpp requires mpp_destination_address to be set (Tempo "
                "wallet that receives USDC payments)."
            )

        # Mainnet opt-in guard — mirrors the x402 policy exactly.
        if _mpp_is_mainnet(config.mpp_network) and not config.mpp_allow_mainnet:
            raise RuntimeError(
                f"MPP_NETWORK={config.mpp_network!r} resolves to a mainnet "
                "chain but MPP_ALLOW_MAINNET is not set. Set "
                "MPP_ALLOW_MAINNET=1 to confirm you want to accept real USDC "
                "on Tempo."
            )

        return MppGate(
            destination_address=config.mpp_destination_address,
            network=config.mpp_network,
            secret_key=config.mpp_secret_key,
            rpc_url=config.mpp_rpc_url,
            prices=DEFAULT_PRICES,
        )
    raise ValueError(
        f"Unknown PAYWALL mode {mode!r}; expected 'none', 'x402', or 'mpp'."
    )
