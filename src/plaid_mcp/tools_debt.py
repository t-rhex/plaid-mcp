"""Debt overrides + payoff analysis.

Plaid's /liabilities/get reliably returns purchase and cash APRs for credit
cards, but frequently misses intro / balance-transfer / special APRs — Citi
in particular. That leaves a gap when an LLM reasons about "which debt to
pay first": it might see a 26% APR on a card that's actually at 0% promo.

This module lets the user annotate what Plaid missed (per-account APR
overrides + promo expiration) and add debts that don't live behind a linked
Plaid account at all (BNPL, medical, personal loans at small lenders). Those
annotations are merged on top of Plaid data when summarize_debt runs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from .storage import Storage
from .tools_wealth import get_liabilities

# ---- overrides --------------------------------------------------------------


def set_account_override(
    storage: Storage,
    account_id: str,
    effective_apr: float | None = None,
    promo_expires: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Record an APR correction / promo note for a Plaid-linked account.

    ``effective_apr`` is the user-confirmed rate (e.g., 0.0 for a 0% promo).
    ``promo_expires`` is an ISO date (YYYY-MM-DD) indicating when the promo
    ends — after that date summarize_debt falls back to Plaid's reported APR.
    """
    if effective_apr is None and promo_expires is None and note is None:
        raise ValueError(
            "Provide at least one of effective_apr, promo_expires, or note."
        )
    storage.save_account_override(
        account_id=account_id,
        effective_apr=effective_apr,
        promo_expires=promo_expires,
        note=note,
    )
    return {"status": "saved", "override": storage.get_account_override(account_id)}


def clear_account_override(storage: Storage, account_id: str) -> dict[str, Any]:
    """Remove any override for an account so summarize_debt reverts to Plaid-reported APR."""
    removed = storage.clear_account_override(account_id)
    return {"status": "removed" if removed else "not_found", "account_id": account_id}


def list_overrides(storage: Storage) -> list[dict[str, Any]]:
    """Return every override the user has recorded."""
    return storage.list_account_overrides()


# ---- external debts ---------------------------------------------------------


def add_external_debt(
    storage: Storage,
    name: str,
    balance: float,
    apr: float,
    minimum_payment: float = 0.0,
    next_payment_due_date: str | None = None,
    promo_expires: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Add a debt that isn't behind any linked Plaid account.

    Use this for BNPL (Affirm, Klarna), medical bills, 401(k) loans, or
    debts at non-linkable lenders. ``apr`` is a percentage (e.g., 18.5 for
    18.5% — NOT 0.185). ``balance`` is the current principal owed.
    """
    if balance < 0:
        raise ValueError("balance must be >= 0")
    if apr < 0:
        raise ValueError("apr must be >= 0 (as a percentage, not a decimal)")
    debt_id = f"ext_{uuid.uuid4().hex[:12]}"
    storage.add_external_debt(
        debt_id=debt_id,
        name=name,
        balance=balance,
        apr=apr,
        minimum_payment=minimum_payment,
        next_payment_due_date=next_payment_due_date,
        promo_expires=promo_expires,
        note=note,
    )
    return {"status": "added", "debt_id": debt_id}


def update_external_debt(
    storage: Storage,
    debt_id: str,
    name: str | None = None,
    balance: float | None = None,
    apr: float | None = None,
    minimum_payment: float | None = None,
    next_payment_due_date: str | None = None,
    promo_expires: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    """Update any subset of fields on an existing external debt."""
    updated = storage.update_external_debt(
        debt_id,
        name=name,
        balance=balance,
        apr=apr,
        minimum_payment=minimum_payment,
        next_payment_due_date=next_payment_due_date,
        promo_expires=promo_expires,
        note=note,
    )
    return {"status": "updated" if updated else "no_changes", "debt_id": debt_id}


def remove_external_debt(storage: Storage, debt_id: str) -> dict[str, Any]:
    """Delete an external debt entry."""
    removed = storage.remove_external_debt(debt_id)
    return {"status": "removed" if removed else "not_found", "debt_id": debt_id}


def list_external_debts(storage: Storage) -> list[dict[str, Any]]:
    """Return every external debt the user has recorded."""
    return storage.list_external_debts()


# ---- debt summary + payoff --------------------------------------------------


def _effective_apr_for_card(
    card: dict[str, Any],
    override: dict[str, Any] | None,
    today: date,
) -> tuple[float, str]:
    """Pick the APR to reason against, and explain where it came from.

    Priority: valid (non-expired) override → Plaid's purchase_apr → None.
    """
    if override is not None and override.get("effective_apr") is not None:
        expires = override.get("promo_expires")
        if expires:
            try:
                expires_date = datetime.fromisoformat(expires).date()
            except ValueError:
                expires_date = None
            if expires_date and expires_date < today:
                return (
                    _plaid_purchase_apr(card),
                    f"override expired {expires}; using Plaid purchase APR",
                )
        return (
            float(override["effective_apr"]),
            (
                f"user override ({override['effective_apr']}%)"
                + (f" until {expires}" if expires else "")
            ),
        )
    apr = _plaid_purchase_apr(card)
    return apr, "Plaid-reported purchase APR"


def _plaid_purchase_apr(card: dict[str, Any]) -> float:
    for apr in card.get("aprs") or []:
        if apr.get("type") == "purchase_apr" and apr.get("percentage") is not None:
            return float(apr["percentage"])
    # fall back to any APR we can find
    for apr in card.get("aprs") or []:
        if apr.get("percentage") is not None:
            return float(apr["percentage"])
    return 0.0


def _project_payoff(
    balance: float,
    apr_pct: float,
    monthly_payment: float,
    max_months: int = 600,
) -> dict[str, Any]:
    """Amortize a fixed monthly payment against a simple interest balance.

    Returns months to payoff and total interest paid. If the payment is too
    small to beat interest accrual, signals `never_pays_off=True`.
    """
    if balance <= 0:
        return {"months": 0, "total_interest": 0.0, "never_pays_off": False}
    if monthly_payment <= 0:
        return {"months": None, "total_interest": None, "never_pays_off": True}
    monthly_rate = (apr_pct / 100.0) / 12.0
    remaining = balance
    total_interest = 0.0
    for m in range(1, max_months + 1):
        interest = remaining * monthly_rate
        if monthly_payment <= interest and monthly_rate > 0:
            return {"months": None, "total_interest": None, "never_pays_off": True}
        total_interest += interest
        remaining = remaining + interest - monthly_payment
        if remaining <= 0:
            total_interest += remaining  # last payment was larger than needed
            return {
                "months": m,
                "total_interest": round(max(total_interest, 0.0), 2),
                "never_pays_off": False,
            }
    return {"months": None, "total_interest": None, "never_pays_off": True}


def _normalize_card(
    card: dict[str, Any],
    override: dict[str, Any] | None,
    today: date,
) -> dict[str, Any]:
    effective_apr, source = _effective_apr_for_card(card, override, today)
    return {
        "source": "plaid",
        "account_id": card["account_id"],
        "name": card.get("institution_name"),
        "balance": float(card.get("last_statement_balance") or 0.0),
        "effective_apr": effective_apr,
        "apr_source": source,
        "plaid_purchase_apr": _plaid_purchase_apr(card),
        "minimum_payment": float(card.get("minimum_payment_amount") or 0.0),
        "next_payment_due_date": card.get("next_payment_due_date"),
        "promo_expires": (override or {}).get("promo_expires"),
        "note": (override or {}).get("note"),
    }


def _normalize_external(debt: dict[str, Any], today: date) -> dict[str, Any]:
    promo = debt.get("promo_expires")
    effective_apr = float(debt.get("apr") or 0.0)
    apr_source = "user-entered APR"
    if promo:
        try:
            expires_date = datetime.fromisoformat(promo).date()
        except ValueError:
            expires_date = None
        if expires_date and expires_date < today:
            apr_source = f"promo expired {promo}; APR as entered ({effective_apr}%)"
    return {
        "source": "external",
        "debt_id": debt["debt_id"],
        "name": debt["name"],
        "balance": float(debt.get("balance") or 0.0),
        "effective_apr": effective_apr,
        "apr_source": apr_source,
        "minimum_payment": float(debt.get("minimum_payment") or 0.0),
        "next_payment_due_date": debt.get("next_payment_due_date"),
        "promo_expires": promo,
        "note": debt.get("note"),
    }


def summarize_debt(
    storage: Storage,
    strategy: str = "avalanche",
    extra_monthly_payment: float = 0.0,
    today: str | None = None,
) -> dict[str, Any]:
    """Merge Plaid liabilities + overrides + external debts, then rank + project payoff.

    strategy:
        "avalanche" — highest effective APR first (minimizes interest paid).
        "snowball"  — lowest balance first (fastest sense of progress).

    extra_monthly_payment: dollars per month ABOVE the sum of minimums that
        you'd put toward the priority debt. Used to project payoff timelines.

    Returns a dict with:
        debts:              sorted list of every debt with balance > 0
        total_balance:      sum across all tracked debts
        total_minimums:     sum of minimum payments
        monthly_interest_at_current_rates: rough monthly interest accrual
        priority_debt:      debt to attack first per the strategy
        projections:        payoff-timeline math for the priority debt
        warnings:           notes about missing data, expiring promos, etc.
    """
    if strategy not in ("avalanche", "snowball"):
        raise ValueError("strategy must be 'avalanche' or 'snowball'")

    today_date = (
        datetime.fromisoformat(today).date() if today else date.today()
    )

    overrides_by_acct = {
        o["account_id"]: o for o in storage.list_account_overrides()
    }

    debts: list[dict[str, Any]] = []
    # Pull Plaid liabilities — non-fatal if the product isn't enabled.
    try:
        liabilities = get_liabilities(storage)
    except Exception as e:  # noqa: BLE001
        liabilities = {"credit_cards": [], "student_loans": [], "mortgages": []}
        warnings: list[str] = [f"Could not fetch Plaid liabilities: {e}"]
    else:
        warnings = []

    for card in liabilities.get("credit_cards") or []:
        if (card.get("last_statement_balance") or 0) <= 0:
            # Zero-balance cards don't need payoff planning, but note them.
            continue
        override = overrides_by_acct.get(card["account_id"])
        debts.append(_normalize_card(card, override, today_date))

    for ext in storage.list_external_debts():
        if (ext.get("balance") or 0) <= 0:
            continue
        debts.append(_normalize_external(ext, today_date))

    # Flag any promo expiring in the next 60 days.
    for d in debts:
        if d.get("promo_expires"):
            try:
                exp = datetime.fromisoformat(d["promo_expires"]).date()
            except ValueError:
                continue
            days_left = (exp - today_date).days
            label = d.get("name") or d.get("account_id")
            promo = d["promo_expires"]
            if 0 <= days_left <= 60:
                warnings.append(
                    f"Promo on '{label}' expires in {days_left} days ({promo})."
                )
            elif days_left < 0:
                warnings.append(
                    f"Promo on '{label}' already expired on {promo}; "
                    "using fallback APR."
                )

    # Rank debts.
    if strategy == "avalanche":
        debts.sort(key=lambda d: (-d["effective_apr"], d["balance"]))
    else:  # snowball
        debts.sort(key=lambda d: (d["balance"], -d["effective_apr"]))

    total_balance = round(sum(d["balance"] for d in debts), 2)
    total_minimums = round(sum(d["minimum_payment"] for d in debts), 2)
    monthly_interest = round(
        sum(d["balance"] * d["effective_apr"] / 100.0 / 12.0 for d in debts), 2
    )

    priority_debt = debts[0] if debts else None
    projections: dict[str, Any] = {}
    if priority_debt:
        # What happens if you only pay minimums on the priority debt?
        min_only = _project_payoff(
            balance=priority_debt["balance"],
            apr_pct=priority_debt["effective_apr"],
            monthly_payment=priority_debt["minimum_payment"],
        )
        # What happens if you add the extra payment on top of the minimum?
        with_extra = _project_payoff(
            balance=priority_debt["balance"],
            apr_pct=priority_debt["effective_apr"],
            monthly_payment=priority_debt["minimum_payment"] + extra_monthly_payment,
        )
        projections = {
            "priority_debt": priority_debt,
            "minimum_only": min_only,
            "with_extra_payment": with_extra,
            "extra_monthly_payment": extra_monthly_payment,
            "interest_saved_vs_minimum": (
                None
                if (min_only.get("total_interest") is None
                    or with_extra.get("total_interest") is None)
                else round(min_only["total_interest"] - with_extra["total_interest"], 2)
            ),
        }

    if not debts:
        warnings.append("No debts with positive balances found.")

    return {
        "strategy": strategy,
        "today": today_date.isoformat(),
        "debts": debts,
        "total_balance": total_balance,
        "total_minimums": total_minimums,
        "monthly_interest_at_current_rates": monthly_interest,
        "projections": projections,
        "warnings": warnings,
    }
