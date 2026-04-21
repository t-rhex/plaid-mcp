"""FastMCP server — wires every tool into a single MCP instance."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Config
from .link import complete_link, create_hosted_link
from .storage import Storage
from .tools_debt import (
    add_external_debt,
    clear_account_override,
    list_external_debts,
    list_overrides,
    remove_external_debt,
    set_account_override,
    summarize_debt,
    update_external_debt,
)
from .tools_transactions import (
    get_balances,
    get_transactions,
    list_accounts,
    list_linked_institutions,
    refresh_transactions,
    remove_institution,
    search_transactions,
    spending_summary,
    sync_transactions,
)
from .tools_wealth import (
    get_holdings,
    get_identity,
    get_income,
    get_investment_transactions,
    get_liabilities,
)


def build_server() -> FastMCP:
    config = Config.from_env()
    storage = Storage(config.db_path)

    mcp = FastMCP(
        "plaid-mcp",
        instructions=(
            "Read-only access to the user's real financial accounts via Plaid. "
            "Before querying transactions, call sync_transactions so the local cache is fresh. "
            "Dates are ISO strings (YYYY-MM-DD). Amounts are positive for outflows (spend) "
            "and negative for inflows (deposits), per Plaid's convention."
        ),
    )

    # ---- Account linking ------------------------------------------------------

    @mcp.tool
    def link_account():
        """Start a new Plaid Link session. Returns a URL the user opens in their browser
        to authenticate with their bank. After they finish, call complete_linking with
        the returned link_token."""
        return create_hosted_link(storage, config)

    @mcp.tool
    def complete_linking(link_token: str, timeout_seconds: int = 180):
        """Finalize a Link session once the user has completed it in their browser.
        Exchanges the public_token for a permanent access_token and caches accounts."""
        return complete_link(storage, link_token, timeout_s=timeout_seconds)

    @mcp.tool
    def list_linked_institutions_tool() -> list[dict[str, Any]]:
        """List every institution currently linked, with account counts and any errors."""
        return list_linked_institutions(storage)

    @mcp.tool
    def remove_institution_tool(item_id: str):
        """Unlink an institution (Plaid item) and delete its local data."""
        return remove_institution(storage, item_id)

    # ---- Accounts + balances --------------------------------------------------

    @mcp.tool
    def list_accounts_tool() -> list[dict[str, Any]]:
        """List every account across every linked institution (from the local cache)."""
        return list_accounts(storage)

    @mcp.tool
    def get_balances_tool(account_id: str | None = None) -> list[dict[str, Any]]:
        """Live balance lookup (hits Plaid, not cached). Filter by account_id if given."""
        return get_balances(storage, account_id=account_id)

    # ---- Transactions ---------------------------------------------------------

    @mcp.tool
    def sync_transactions_tool(
        wait_for_ready: bool = True,
        wait_timeout_seconds: int = 60,
    ):
        """Pull the latest transactions from Plaid into the local cache.
        Idempotent and incremental — uses cursors from the last sync.

        Plaid's first sync after linking an institution runs asynchronously;
        when wait_for_ready is True (default), this tool blocks briefly until
        the historical pull reports HISTORICAL_UPDATE_COMPLETE. Returned
        ``status`` field surfaces that state per item."""
        return sync_transactions(
            storage,
            wait_for_ready=wait_for_ready,
            wait_timeout_s=wait_timeout_seconds,
        )

    @mcp.tool
    def refresh_transactions_tool(item_id: str | None = None):
        """Nudge Plaid to pull fresh transactions from the bank right now.

        Use when a user just made a purchase and wants to see it, or when
        transactions look stale. Plaid normally refreshes on its own every
        few hours; this forces an immediate pull. Asynchronous — wait
        30-60s then call sync_transactions to ingest any new data.

        Pass item_id to refresh one institution, or leave empty to refresh
        everything. Some smaller banks don't support on-demand refresh."""
        return refresh_transactions(storage, item_id=item_id)

    @mcp.tool
    def get_transactions_tool(
        start_date: str,
        end_date: str,
        account_id: str | None = None,
        category: str | None = None,
        merchant: str | None = None,
        min_amount: float | None = None,
        max_amount: float | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Query cached transactions. Dates are YYYY-MM-DD.
        Run sync_transactions first to refresh. Positive amounts = spend."""
        return get_transactions(
            storage,
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            category=category,
            merchant=merchant,
            min_amount=min_amount,
            max_amount=max_amount,
            limit=limit,
        )

    @mcp.tool
    def search_transactions_tool(
        query: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fuzzy search across transaction description and merchant name."""
        return search_transactions(
            storage,
            query=query,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
        )

    @mcp.tool
    def spending_summary_tool(
        start_date: str,
        end_date: str,
        group_by: str = "category",
    ) -> list[dict[str, Any]]:
        """Aggregate spending by category | subcategory | merchant | account."""
        return spending_summary(
            storage, start_date=start_date, end_date=end_date, group_by=group_by
        )

    # ---- Investments ----------------------------------------------------------

    @mcp.tool
    def get_holdings_tool(account_id: str | None = None):
        """Current investment positions (tickers, quantities, market value, cost basis)."""
        return get_holdings(storage, account_id=account_id)

    @mcp.tool
    def get_investment_transactions_tool(
        start_date: str,
        end_date: str,
        account_id: str | None = None,
        limit: int = 250,
    ):
        """Brokerage transactions: buys, sells, dividends, fees."""
        return get_investment_transactions(
            storage,
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            limit=limit,
        )

    # ---- Liabilities ----------------------------------------------------------

    @mcp.tool
    def get_liabilities_tool():
        """Credit cards, student loans, mortgages with APRs, balances, due dates."""
        return get_liabilities(storage)

    # ---- Identity + income ----------------------------------------------------

    @mcp.tool
    def get_identity_tool(account_id: str | None = None):
        """Account holder names, emails, phones, addresses as reported by the institution."""
        return get_identity(storage, account_id=account_id)

    @mcp.tool
    def get_income_tool():
        """Bank-detected income streams. Requires Income product enabled in your Plaid dashboard."""
        return get_income(storage)

    # ---- Debt overrides + payoff analysis -------------------------------------

    @mcp.tool
    def set_account_override_tool(
        account_id: str,
        effective_apr: float | None = None,
        promo_expires: str | None = None,
        note: str | None = None,
    ):
        """Annotate a linked card with the real APR when Plaid misses it.

        Common case: Citi doesn't consistently report 0% intro / balance-transfer
        promos through /liabilities/get. Use this to record the true effective
        APR and (optionally) a promo expiration date so summarize_debt_tool can
        reason honestly. After the promo_expires date, payoff analysis reverts
        to Plaid's reported purchase APR."""
        return set_account_override(
            storage,
            account_id=account_id,
            effective_apr=effective_apr,
            promo_expires=promo_expires,
            note=note,
        )

    @mcp.tool
    def clear_account_override_tool(account_id: str):
        """Remove any APR override for an account."""
        return clear_account_override(storage, account_id=account_id)

    @mcp.tool
    def list_overrides_tool():
        """List every account APR override the user has recorded."""
        return list_overrides(storage)

    @mcp.tool
    def add_external_debt_tool(
        name: str,
        balance: float,
        apr: float,
        minimum_payment: float = 0.0,
        next_payment_due_date: str | None = None,
        promo_expires: str | None = None,
        note: str | None = None,
    ):
        """Track a debt that isn't behind a linked Plaid account.

        Use for BNPL (Affirm, Klarna), medical bills, 401(k) loans, or debts at
        non-linkable lenders. ``apr`` is a percentage (e.g. 18.5 for 18.5%, not
        0.185). Returns the assigned debt_id."""
        return add_external_debt(
            storage,
            name=name,
            balance=balance,
            apr=apr,
            minimum_payment=minimum_payment,
            next_payment_due_date=next_payment_due_date,
            promo_expires=promo_expires,
            note=note,
        )

    @mcp.tool
    def update_external_debt_tool(
        debt_id: str,
        name: str | None = None,
        balance: float | None = None,
        apr: float | None = None,
        minimum_payment: float | None = None,
        next_payment_due_date: str | None = None,
        promo_expires: str | None = None,
        note: str | None = None,
    ):
        """Update any subset of fields on an existing external debt."""
        return update_external_debt(
            storage,
            debt_id=debt_id,
            name=name,
            balance=balance,
            apr=apr,
            minimum_payment=minimum_payment,
            next_payment_due_date=next_payment_due_date,
            promo_expires=promo_expires,
            note=note,
        )

    @mcp.tool
    def remove_external_debt_tool(debt_id: str):
        """Delete an external debt entry."""
        return remove_external_debt(storage, debt_id=debt_id)

    @mcp.tool
    def list_external_debts_tool():
        """List every external (non-Plaid-linked) debt the user has recorded."""
        return list_external_debts(storage)

    @mcp.tool
    def summarize_debt_tool(
        strategy: str = "avalanche",
        extra_monthly_payment: float = 0.0,
        today: str | None = None,
    ):
        """Rank every debt and project payoff timelines.

        Merges Plaid-reported credit cards with user APR overrides and any
        external debts, then ranks by strategy:
          - ``avalanche`` (default): highest effective APR first — minimizes interest paid.
          - ``snowball``: lowest balance first — fastest sense of progress.

        ``extra_monthly_payment`` is dollars above the priority debt's minimum
        you'd put toward it each month. Returns total balance, monthly interest
        accrual at current rates, priority debt, amortized payoff projections
        (minimum-only vs. with-extra), and warnings for promos expiring soon."""
        return summarize_debt(
            storage,
            strategy=strategy,
            extra_monthly_payment=extra_monthly_payment,
            today=today,
        )

    return mcp
