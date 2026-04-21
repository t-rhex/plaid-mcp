"""Plaid provider — wraps the existing Plaid SDK helpers behind the Provider Protocol.

Delegates heavy lifting to the pre-existing modules:
  - ``link.create_hosted_link`` / ``link.complete_link`` for enrollment
  - ``tools_transactions.remove_institution`` for /item/remove + local purge
  - ``storage.query_transactions`` for cached reads

The Plaid SDK enforces a persistent access_token per item, and items are already
the natural enrollment boundary in our SQLite schema — so ``Enrollment.id`` is
simply the ``item_id``. Accounts, balances, and transactions are derived off
that binding.

Sign convention:
  Plaid reports ``amount`` as positive when money leaves a depository account
  (i.e. a charge or spend). Our normalized ``Transaction.amount`` follows the
  same convention, so we pass the value through unchanged.
"""

from __future__ import annotations

from typing import Any

from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.identity_get_request import IdentityGetRequest

from .. import client as client_mod
from .. import link as link_mod
from .. import tools_transactions as tools_tx
from ..config import Config
from ..storage import Storage
from .base import (
    Account,
    Balance,
    Capability,
    Enrollment,
    Identity,
    Transaction,
)


class PlaidError(RuntimeError):
    """Raised when a Plaid operation can't be completed (e.g. link not ready)."""


class PlaidProvider:
    """Provider impl against Plaid. Thin wrapper over existing helpers."""

    name = "plaid"

    def __init__(self, storage: Storage, config: Config):
        self.storage = storage
        self.config = config

    # ---- capabilities ---------------------------------------------------

    def capabilities(self) -> set[Capability]:
        return {
            Capability.ACCOUNTS,
            Capability.BALANCES,
            Capability.TRANSACTIONS,
            Capability.IDENTITY,
            Capability.INVESTMENTS,
            Capability.LIABILITIES,
            Capability.INCOME,
        }

    # ---- enrollment -----------------------------------------------------

    def begin_enrollment(self) -> dict[str, Any]:
        """Returns ``{link_token, hosted_url, expiration}``. Caller opens the
        hosted_url in a browser, then passes ``link_token`` back in
        ``complete_enrollment``'s payload."""
        return link_mod.create_hosted_link(self.storage, self.config)

    def complete_enrollment(self, payload: dict[str, Any]) -> Enrollment:
        """``payload`` must contain ``link_token``. Polls Plaid until the user
        finishes the hosted link flow, exchanges the public_token, and
        persists the item. Raises PlaidError if the link isn't complete yet
        (so the caller can re-try after the user finishes in the browser).
        """
        link_token = payload.get("link_token")
        if not link_token:
            raise PlaidError("complete_enrollment payload missing link_token")

        timeout_s = int(payload.get("timeout_s", 300))
        result = link_mod.complete_link(
            self.storage, link_token, timeout_s=timeout_s
        )
        if result.get("status") != "completed":
            raise PlaidError(
                result.get("message")
                or "Plaid link has not completed yet — finish the hosted flow and retry."
            )

        item_id = result["item_id"]
        access_token = self.storage.get_access_token(item_id)
        if not access_token:
            raise PlaidError(f"Plaid link completed but no access_token stored for {item_id}")

        items = {i["item_id"]: i for i in self.storage.list_items()}
        item_row = items.get(item_id, {})

        return Enrollment(
            id=item_id,
            institution_id=item_row.get("institution_id"),
            institution_name=item_row.get("institution_name")
            or result.get("institution_name"),
            access_token=access_token,
            provider=self.name,
        )

    def remove_enrollment(self, enrollment: Enrollment) -> None:
        """Delegates to the existing remove_institution helper, which does
        /item/remove upstream and purges locally. Best-effort — if Plaid
        errors, the local rows still get deleted."""
        tools_tx.remove_institution(self.storage, enrollment.id)

    # ---- reads ----------------------------------------------------------

    def list_accounts(self, enrollment: Enrollment) -> list[Account]:
        """Read from the local account cache (populated at link time)."""
        rows = self.storage.list_accounts()
        return [
            _to_account(r)
            for r in rows
            if r.get("item_id") == enrollment.id
        ]

    def get_balances(self, enrollment: Enrollment) -> list[Balance]:
        """Live Plaid /accounts/balance/get for just this enrollment."""
        client = client_mod.get_client()
        resp = client.accounts_balance_get(
            AccountsBalanceGetRequest(access_token=enrollment.access_token)
        )
        return [_to_balance(a) for a in resp.get("accounts", [])]

    def get_transactions(
        self,
        enrollment: Enrollment,
        start_date: str,
        end_date: str,
        account_id: str | None = None,
    ) -> list[Transaction]:
        """Reads from the locally-cached synced transactions.

        Callers are expected to run ``sync_transactions`` ahead of time. Rows
        are filtered to this enrollment by walking accounts that belong to the
        item, since ``query_transactions`` doesn't filter by item_id directly.
        """
        own_account_ids = {
            r["account_id"]
            for r in self.storage.list_accounts()
            if r.get("item_id") == enrollment.id
        }
        if account_id is not None:
            if account_id not in own_account_ids:
                return []
            rows = self.storage.query_transactions(
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
            )
        else:
            rows = self.storage.query_transactions(
                start_date=start_date,
                end_date=end_date,
            )
            rows = [r for r in rows if r.get("account_id") in own_account_ids]
        return [_to_transaction(r) for r in rows]

    def get_identity(self, enrollment: Enrollment) -> list[Identity]:
        """Live Plaid /identity/get for this enrollment."""
        client = client_mod.get_client()
        resp = client.identity_get(
            IdentityGetRequest(access_token=enrollment.access_token)
        )
        out: list[Identity] = []
        for acct in resp.get("accounts", []):
            acct = acct.to_dict() if hasattr(acct, "to_dict") else dict(acct)
            out.append(_to_identity(acct))
        return out


# ---- normalizers -------------------------------------------------------


def _to_account(row: dict[str, Any]) -> Account:
    return Account(
        id=row["account_id"],
        enrollment_id=row["item_id"],
        name=row.get("name"),
        official_name=row.get("official_name"),
        type=row.get("type"),
        subtype=row.get("subtype"),
        mask=row.get("mask"),
        iso_currency=row.get("iso_currency"),
    )


def _to_balance(acct: dict[str, Any]) -> Balance:
    balances = acct.get("balances") or {}
    return Balance(
        account_id=acct["account_id"],
        current=balances.get("current"),
        available=balances.get("available"),
        limit=balances.get("limit"),
        iso_currency=balances.get("iso_currency_code"),
    )


def _to_transaction(row: dict[str, Any]) -> Transaction:
    return Transaction(
        id=row["transaction_id"],
        account_id=row["account_id"],
        amount=float(row.get("amount") or 0.0),
        iso_currency=row.get("iso_currency"),
        date=row.get("date") or "",
        authorized_date=row.get("authorized_date"),
        name=row.get("name"),
        merchant_name=row.get("merchant_name"),
        category=row.get("category"),
        subcategory=row.get("subcategory"),
        pending=bool(row.get("pending")),
        payment_channel=row.get("payment_channel"),
        raw={},
    )


def _to_identity(acct: dict[str, Any]) -> Identity:
    owners = acct.get("owners") or []
    names: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    addresses: list[dict[str, Any]] = []
    for o in owners:
        for n in o.get("names") or []:
            names.append(n)
        for e in o.get("emails") or []:
            if e.get("data"):
                emails.append(e["data"])
        for p in o.get("phone_numbers") or []:
            if p.get("data"):
                phones.append(p["data"])
        for a in o.get("addresses") or []:
            addresses.append(a.get("data") or a)
    return Identity(
        account_id=acct.get("account_id") or "",
        names=names,
        emails=emails,
        phones=phones,
        addresses=addresses,
    )
