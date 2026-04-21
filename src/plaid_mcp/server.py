"""FastMCP server — wires every tool into a single MCP instance.

Tool surface split:

- Provider-agnostic tools (accounts, balances, transactions, search,
  identity) route through ``build_provider`` and work on either Plaid or
  Teller. Enrollments are resolved from SQLite (Plaid) or the on-disk
  Teller enrollment file (Teller) via ``server_helpers.list_enrollments``.

- Plaid-only tools (investments, liabilities, income, cursor-based sync /
  refresh, debt analysis, Plaid Hosted Link) check the active provider's
  capabilities and raise a clean ``RuntimeError`` when they can't be
  served — instead of crashing deep in the Plaid SDK under
  ``PROVIDER=teller``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from .config import Config
from .link import complete_link, create_hosted_link
from .providers import Capability, build_provider
from .server_helpers import list_enrollments
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
    list_linked_institutions,
    refresh_transactions,
    remove_institution,
    spending_summary,
    sync_transactions,
)
from .tools_wealth import (
    get_holdings,
    get_income,
    get_investment_transactions,
    get_liabilities,
)


def _require_plaid(config: Config, capability: Capability | None = None) -> None:
    """Gate Plaid-only tools.

    Fast-path when ``PROVIDER=plaid`` — we don't touch the factory at all.
    Under any other provider we raise a clean ``RuntimeError`` that names
    both the required capability (when given) and the active provider.
    """
    provider_name = (config.provider or "plaid").strip().lower()
    if provider_name == "plaid":
        return
    if capability is not None:
        raise RuntimeError(
            f"Tool requires the {capability.value!r} capability; the active "
            f"provider ({provider_name!r}) does not support it. "
            "Set PROVIDER=plaid in your .env to use this tool."
        )
    raise RuntimeError(
        f"Tool requires PROVIDER=plaid; active provider is {provider_name!r}. "
        "Set PROVIDER=plaid in your .env and retry."
    )


def build_server() -> FastMCP:
    config = Config.from_env()
    storage = Storage(config.db_path)

    mcp = FastMCP(
        "plaid-mcp",
        instructions=(
            "Read-only access to the user's real financial accounts via Plaid or Teller. "
            "For Plaid, call sync_transactions before querying cached transactions. "
            "For Teller, transactions are fetched live by date range. "
            "Dates are ISO strings (YYYY-MM-DD). Amounts are positive for outflows (spend) "
            "and negative for inflows (deposits)."
        ),
    )

    # ---- Linking (Plaid-only in the MCP surface) -----------------------------
    # Teller linking is a CLI concern — see `plaid-mcp teller connect`.

    @mcp.tool
    def link_account():
        """Start a new Plaid Link session. Returns a URL the user opens in their browser
        to authenticate with their bank. After they finish, call complete_linking with
        the returned link_token.

        Plaid-only: under PROVIDER=teller, run `plaid-mcp teller connect` instead."""
        _require_plaid(config)
        return create_hosted_link(storage, config)

    @mcp.tool
    def complete_linking(link_token: str, timeout_seconds: int = 180):
        """Finalize a Link session once the user has completed it in their browser.
        Exchanges the public_token for a permanent access_token and caches accounts.

        Plaid-only."""
        _require_plaid(config)
        return complete_link(storage, link_token, timeout_s=timeout_seconds)

    @mcp.tool
    def list_linked_institutions_tool() -> list[dict[str, Any]]:
        """List every institution currently linked, with account counts and any errors.

        Plaid-only — reads the local SQLite items table, which Teller doesn't use."""
        _require_plaid(config)
        return list_linked_institutions(storage)

    @mcp.tool
    def remove_institution_tool(item_id: str):
        """Unlink an institution (Plaid item) and delete its local data.

        Plaid-only."""
        _require_plaid(config)
        return remove_institution(storage, item_id)

    # ---- Accounts + balances (provider-agnostic) ------------------------------

    @mcp.tool
    def list_accounts_tool() -> list[dict[str, Any]]:
        """List every account across every linked institution."""
        return _list_accounts(config, storage)

    @mcp.tool
    def get_balances_tool(account_id: str | None = None) -> list[dict[str, Any]]:
        """Live balance lookup. Filter by account_id if given."""
        return _get_balances(config, storage, account_id=account_id)

    # ---- Transactions --------------------------------------------------------

    @mcp.tool
    def sync_transactions_tool(
        wait_for_ready: bool = True,
        wait_timeout_seconds: int = 60,
    ):
        """Pull the latest transactions from Plaid into the local cache.
        Idempotent and incremental — uses cursors from the last sync.

        Plaid-only: Teller has no cursor-based sync; `get_transactions_tool`
        fetches Teller transactions live by date range."""
        _require_plaid(config)
        return sync_transactions(
            storage,
            wait_for_ready=wait_for_ready,
            wait_timeout_s=wait_timeout_seconds,
        )

    @mcp.tool
    def refresh_transactions_tool(item_id: str | None = None):
        """Nudge Plaid to pull fresh transactions from the bank right now.

        Plaid-only."""
        _require_plaid(config)
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
        """Query transactions between two dates. Dates are YYYY-MM-DD.

        Plaid: reads from the local cache — run sync_transactions first.
        Teller: fetches live from Teller's API for the date range.
        Positive amounts = spend."""
        return _get_transactions(
            config,
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
        return _search_transactions(
            config,
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
        """Aggregate spending by category | subcategory | merchant | account.

        Plaid-only: reads the Plaid-shaped cache populated by sync_transactions."""
        _require_plaid(config)
        return spending_summary(
            storage, start_date=start_date, end_date=end_date, group_by=group_by
        )

    # ---- Investments (Plaid-only) --------------------------------------------

    @mcp.tool
    def get_holdings_tool(account_id: str | None = None):
        """Current investment positions (tickers, quantities, market value, cost basis).

        Plaid-only — Teller has no INVESTMENTS capability."""
        _require_plaid(config, Capability.INVESTMENTS)
        return get_holdings(storage, account_id=account_id)

    @mcp.tool
    def get_investment_transactions_tool(
        start_date: str,
        end_date: str,
        account_id: str | None = None,
        limit: int = 250,
    ):
        """Brokerage transactions: buys, sells, dividends, fees.

        Plaid-only — Teller has no INVESTMENTS capability."""
        _require_plaid(config, Capability.INVESTMENTS)
        return get_investment_transactions(
            storage,
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            limit=limit,
        )

    # ---- Liabilities (Plaid-only) --------------------------------------------

    @mcp.tool
    def get_liabilities_tool():
        """Credit cards, student loans, mortgages with APRs, balances, due dates.

        Plaid-only — Teller has no LIABILITIES capability."""
        _require_plaid(config, Capability.LIABILITIES)
        return get_liabilities(storage)

    # ---- Identity (provider-agnostic) ----------------------------------------

    @mcp.tool
    def get_identity_tool(account_id: str | None = None):
        """Account holder names, emails, phones, addresses as reported by the institution."""
        return _get_identity(config, storage, account_id=account_id)

    # ---- Income (Plaid-only) -------------------------------------------------

    @mcp.tool
    def get_income_tool():
        """Bank-detected income streams. Requires Income product enabled in your Plaid dashboard.

        Plaid-only — Teller has no INCOME capability."""
        _require_plaid(config, Capability.INCOME)
        return get_income(storage)

    # ---- Debt overrides + payoff analysis (Plaid-only) -----------------------
    # These merge Plaid-reported liabilities data with local overrides; Teller
    # has no liabilities capability, so the whole surface is gated.

    @mcp.tool
    def set_account_override_tool(
        account_id: str,
        effective_apr: float | None = None,
        promo_expires: str | None = None,
        note: str | None = None,
    ):
        """Annotate a linked card with the real APR when Plaid misses it.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
        return set_account_override(
            storage,
            account_id=account_id,
            effective_apr=effective_apr,
            promo_expires=promo_expires,
            note=note,
        )

    @mcp.tool
    def clear_account_override_tool(account_id: str):
        """Remove any APR override for an account.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
        return clear_account_override(storage, account_id=account_id)

    @mcp.tool
    def list_overrides_tool():
        """List every account APR override the user has recorded.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
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

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
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
        """Update any subset of fields on an existing external debt.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
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
        """Delete an external debt entry.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
        return remove_external_debt(storage, debt_id=debt_id)

    @mcp.tool
    def list_external_debts_tool():
        """List every external (non-Plaid-linked) debt the user has recorded.

        Plaid-only."""
        _require_plaid(config, Capability.LIABILITIES)
        return list_external_debts(storage)

    @mcp.tool
    def summarize_debt_tool(
        strategy: str = "avalanche",
        extra_monthly_payment: float = 0.0,
        today: str | None = None,
    ):
        """Rank every debt and project payoff timelines.

        Plaid-only — merges Plaid liabilities with local overrides + external debts."""
        _require_plaid(config, Capability.LIABILITIES)
        return summarize_debt(
            storage,
            strategy=strategy,
            extra_monthly_payment=extra_monthly_payment,
            today=today,
        )

    return mcp


# ============================================================================
# Provider-agnostic tool bodies.
#
# These translate between the Provider Protocol's normalized dataclasses and
# the JSON shape MCP clients have been consuming since day one. When the
# active provider can't serve a requested field (e.g. Teller doesn't expose
# a separate ``updated_at`` per account), we emit ``None`` rather than drop
# the key — shape stability matters more than Teller-side richness.
# ============================================================================


def _with_provider(config: Config, storage: Storage):
    """Context-manager-ish helper: yields (provider, enrollments) and closes
    the provider on exit. Centralizes the close() call because Teller's
    provider holds an httpx client."""
    provider = build_provider(config, storage)
    try:
        enrollments = list_enrollments(config, storage)
        return provider, enrollments
    except Exception:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        raise


def _close(provider) -> None:
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def _list_accounts(config: Config, storage: Storage) -> list[dict[str, Any]]:
    """Flatten every enrollment's accounts into the legacy tool shape.

    For Plaid we still read from the local account cache (populated at link
    time); for Teller we hit the live ``/accounts`` endpoint. Both paths
    return dicts with the same keys so downstream clients don't care.
    """
    provider, enrollments = _with_provider(config, storage)
    try:
        out: list[dict[str, Any]] = []
        for enrollment in enrollments:
            for acct in provider.list_accounts(enrollment):
                out.append(
                    {
                        "account_id": acct.id,
                        "item_id": enrollment.id,  # legacy key name
                        "institution_name": enrollment.institution_name,
                        "name": acct.name,
                        "official_name": acct.official_name,
                        "type": acct.type,
                        "subtype": acct.subtype,
                        "mask": acct.mask,
                        "iso_currency": acct.iso_currency,
                    }
                )
        return out
    finally:
        _close(provider)


def _get_balances(
    config: Config,
    storage: Storage,
    account_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten balance results across enrollments, adding the institution name
    and account-level metadata the legacy shape carries.

    Plaid errors per-enrollment are recorded on the item and skipped so a
    single bad link doesn't nuke the whole call; Teller errors bubble up
    because there's exactly one enrollment.
    """
    provider, enrollments = _with_provider(config, storage)
    try:
        # Cache accounts per enrollment so we can fill in the legacy
        # name/mask/type/subtype fields without re-querying each balance.
        out: list[dict[str, Any]] = []
        for enrollment in enrollments:
            try:
                balances = provider.get_balances(enrollment)
            except Exception as e:  # noqa: BLE001
                # Record on the item for Plaid so list_linked_institutions
                # can surface the failure; Teller has no such table.
                if enrollment.provider == "plaid":
                    storage.set_item_error(enrollment.id, str(e))
                    continue
                raise

            accounts_by_id = {
                a.id: a for a in provider.list_accounts(enrollment)
            }

            for balance in balances:
                if account_id and balance.account_id != account_id:
                    continue
                acct = accounts_by_id.get(balance.account_id)
                out.append(
                    {
                        "account_id": balance.account_id,
                        "institution_name": enrollment.institution_name,
                        "name": acct.name if acct else None,
                        "mask": acct.mask if acct else None,
                        "type": acct.type if acct else None,
                        "subtype": acct.subtype if acct else None,
                        "current": balance.current,
                        "available": balance.available,
                        "limit": balance.limit,
                        "iso_currency": balance.iso_currency,
                    }
                )
        return out
    finally:
        _close(provider)


def _get_transactions(
    config: Config,
    storage: Storage,
    *,
    start_date: str,
    end_date: str,
    account_id: str | None,
    category: str | None,
    merchant: str | None,
    min_amount: float | None,
    max_amount: float | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Return transactions in the legacy dict shape.

    Plaid: reads from the SQLite cache via ``storage.query_transactions`` so
    filter semantics (category, merchant LIKE, amount bounds, limit) match
    exactly what callers have been getting. We keep that path because the
    provider's ``get_transactions`` for Plaid doesn't expose those filters.

    Teller: fetches live from the provider for the date range, then applies
    the same filters in Python before returning. Teller has no local cache.
    """
    provider_name = (config.provider or "plaid").strip().lower()

    if provider_name == "plaid":
        # Preserve the exact legacy shape by reading from storage directly
        # — ``query_transactions`` emits the dict the old tool returned.
        return storage.query_transactions(
            start_date=start_date,
            end_date=end_date,
            account_id=account_id,
            category=category,
            merchant=merchant,
            min_amount=min_amount,
            max_amount=max_amount,
            limit=limit,
        )

    # Teller (or any other non-Plaid provider): fetch live, filter in Python.
    provider, enrollments = _with_provider(config, storage)
    try:
        rows: list[dict[str, Any]] = []
        for enrollment in enrollments:
            txs = provider.get_transactions(
                enrollment,
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
            )
            for tx in txs:
                if category and tx.category != category:
                    continue
                if merchant and (tx.merchant_name or "").lower().find(merchant.lower()) < 0:
                    continue
                if min_amount is not None and (tx.amount or 0.0) < min_amount:
                    continue
                if max_amount is not None and (tx.amount or 0.0) > max_amount:
                    continue
                rows.append(_transaction_to_dict(tx))
                if len(rows) >= limit:
                    return rows
        return rows
    finally:
        _close(provider)


def _search_transactions(
    config: Config,
    storage: Storage,
    *,
    query: str,
    start_date: str | None,
    end_date: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Fuzzy text search across name + merchant_name.

    Plaid: delegates to ``storage.query_transactions(text=...)`` so the
    existing ranking semantics are preserved.

    Teller: there's no local text index; fetch the date range live and
    substring-match in Python. If no date range is given, default to the
    last 90 days so we don't inadvertently walk every page of history.
    """
    provider_name = (config.provider or "plaid").strip().lower()

    if provider_name == "plaid":
        return storage.query_transactions(
            start_date=start_date,
            end_date=end_date,
            text=query,
            limit=limit,
        )

    from datetime import date, timedelta

    if not end_date:
        end_date = date.today().isoformat()
    if not start_date:
        start_date = (date.today() - timedelta(days=90)).isoformat()

    provider, enrollments = _with_provider(config, storage)
    try:
        needle = query.lower()
        rows: list[dict[str, Any]] = []
        for enrollment in enrollments:
            txs = provider.get_transactions(
                enrollment, start_date=start_date, end_date=end_date
            )
            for tx in txs:
                haystack = (
                    (tx.name or "") + " " + (tx.merchant_name or "")
                ).lower()
                if needle in haystack:
                    rows.append(_transaction_to_dict(tx))
                    if len(rows) >= limit:
                        return rows
        return rows
    finally:
        _close(provider)


def _get_identity(
    config: Config,
    storage: Storage,
    account_id: str | None,
) -> dict[str, Any]:
    """Normalize identity results into the legacy ``{"identities": [...]}`` shape."""
    provider, enrollments = _with_provider(config, storage)
    try:
        # Preload accounts once per enrollment so we can attach account_name.
        out: list[dict[str, Any]] = []
        for enrollment in enrollments:
            try:
                identities = provider.get_identity(enrollment)
            except Exception as e:  # noqa: BLE001
                if enrollment.provider == "plaid":
                    storage.set_item_error(enrollment.id, str(e))
                    continue
                raise

            accounts_by_id = {
                a.id: a for a in provider.list_accounts(enrollment)
            }

            for ident in identities:
                if account_id and ident.account_id != account_id:
                    continue
                acct = accounts_by_id.get(ident.account_id)
                out.append(
                    {
                        "account_id": ident.account_id,
                        "institution_name": enrollment.institution_name,
                        "account_name": acct.name if acct else None,
                        # Legacy shape used a nested "owners" list with per-owner
                        # name/email/phone/address arrays. Our normalized
                        # ``Identity`` already flattened across owners, so we
                        # re-wrap into a single synthetic owner to keep the
                        # contract stable for existing callers.
                        "owners": [
                            {
                                "names": ident.names,
                                "emails": ident.emails,
                                "phone_numbers": ident.phones,
                                "addresses": [
                                    {
                                        "city": a.get("city"),
                                        "region": a.get("region"),
                                        "postal_code": a.get("postal_code"),
                                        "country": a.get("country"),
                                    }
                                    for a in ident.addresses
                                ],
                            }
                        ],
                    }
                )
        return {"identities": out}
    finally:
        _close(provider)


def _transaction_to_dict(tx) -> dict[str, Any]:
    """Map a ``Transaction`` dataclass to the dict shape ``query_transactions``
    emits, so callers can't tell whether the rows came from Plaid's cache or
    Teller's live API."""
    return {
        "transaction_id": tx.id,
        "account_id": tx.account_id,
        "amount": tx.amount,
        "iso_currency": tx.iso_currency,
        "date": tx.date,
        "authorized_date": tx.authorized_date,
        "name": tx.name,
        "merchant_name": tx.merchant_name,
        "category": tx.category,
        "subcategory": tx.subcategory,
        "pending": tx.pending,
        "payment_channel": tx.payment_channel,
    }
