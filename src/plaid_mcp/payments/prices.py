"""Default per-tool price table.

Prices are in integer USD cents. Kept in a dedicated module so operators
hosting plaid-mcp can import and override the table cleanly without
forking the x402 adapter.
"""

from __future__ import annotations

from .base import DEFAULT_TOOL_PRICE_CENTS, PriceTable

# Tool name → USD cents. Names match the decorator ``name=`` kwargs in
# ``server.py`` exactly. Tools not listed fall back to
# :data:`DEFAULT_TOOL_PRICE_CENTS`.
_DEFAULT_PRICE_MAP: dict[str, int] = {
    # Read-only Plaid tools.
    "get_balances_tool": 10,
    "get_transactions_tool": 10,
    "sync_transactions_tool": 5,
    "spending_summary_tool": 15,
    "get_holdings_tool": 20,
    "get_liabilities_tool": 20,
    # Analysis tools — more compute, more value.
    "summarize_debt_tool": 50,
}


DEFAULT_PRICES = PriceTable(
    prices=_DEFAULT_PRICE_MAP,
    default_cents=DEFAULT_TOOL_PRICE_CENTS,
)
