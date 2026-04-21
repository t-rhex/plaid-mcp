"""Teller provider — https://teller.io

Auth model:
  - Per-API-call: HTTP Basic with ``(access_token, "")`` where the access_token
    comes from an enrollment completed in Teller Connect.
  - mTLS: required for ``development`` and ``production`` environments.
    Not required in ``sandbox`` — which is what we default to here so the
    quickstart doesn't need cert wrangling. When cert_path/key_path are
    provided, they're passed to httpx as the client cert.

Pagination:
  Transactions are date-range queries with ``from_id`` for pages past the
  first. We don't expose that to callers — ``get_transactions`` walks every
  page and returns the flattened list.

What Teller exposes vs Plaid:
  ✓ accounts, balances, transactions, identity
  ✗ investments, liabilities, income  (see Capability set below)
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import (
    Account,
    Balance,
    Capability,
    Enrollment,
    Identity,
    Transaction,
)

_API_BASE = "https://api.teller.io"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class TellerError(RuntimeError):
    """Raised when Teller returns a non-2xx we can't interpret."""


class TellerProvider:
    """Provider impl against Teller's REST API."""

    name = "teller"

    def __init__(
        self,
        *,
        application_id: str | None = None,
        environment: str = "sandbox",
        cert_path: str | None = None,
        key_path: str | None = None,
        base_url: str = _API_BASE,
    ):
        self.application_id = application_id
        self.environment = environment
        self.base_url = base_url
        cert = (cert_path, key_path) if cert_path and key_path else None
        if environment != "sandbox" and cert is None:
            raise ValueError(
                f"Teller {environment!r} requires a client certificate; "
                "set TELLER_CERT_PATH and TELLER_KEY_PATH."
            )
        self._client = httpx.Client(
            base_url=base_url,
            timeout=_TIMEOUT,
            cert=cert,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    # ---- capabilities ---------------------------------------------------

    def capabilities(self) -> set[Capability]:
        return {
            Capability.ACCOUNTS,
            Capability.BALANCES,
            Capability.TRANSACTIONS,
            Capability.IDENTITY,
        }

    # ---- enrollment -----------------------------------------------------

    def begin_enrollment(self) -> dict[str, Any]:
        """Teller Connect runs in the client (browser widget), so backends
        can't hand out a hosted URL the way Plaid can. Return the config the
        widget needs; the TUI spins up a local page that loads Connect.js
        with this payload and POSTs the resulting access_token back."""
        if not self.application_id:
            raise TellerError(
                "TELLER_APPLICATION_ID is required to start Connect."
            )
        return {
            "application_id": self.application_id,
            "environment": self.environment,
        }

    def complete_enrollment(self, payload: dict[str, Any]) -> Enrollment:
        """``payload`` is what Connect's onSuccess hands back:
            {
              "accessToken": "...",
              "enrollment": {"id": "...", "institution": {"id": "...", "name": "..."}},
              "user": {"id": "..."}
            }
        We normalize it and persist via the caller."""
        access_token = payload.get("accessToken") or payload.get("access_token")
        if not access_token:
            raise TellerError("complete_enrollment payload missing accessToken")

        enrollment_blob = payload.get("enrollment") or {}
        institution = enrollment_blob.get("institution") or {}
        enrollment_id = enrollment_blob.get("id") or access_token[:16]

        return Enrollment(
            id=enrollment_id,
            institution_id=institution.get("id"),
            institution_name=institution.get("name"),
            access_token=access_token,
            provider=self.name,
        )

    def remove_enrollment(self, enrollment: Enrollment) -> None:
        # Teller: DELETE /accounts/:id on each account would revoke per-account;
        # there's no single enrollment-level revoke. Caller usually just drops
        # the access_token locally.
        pass

    # ---- reads ----------------------------------------------------------

    def list_accounts(self, enrollment: Enrollment) -> list[Account]:
        data = self._get("/accounts", enrollment.access_token)
        return [_to_account(a, enrollment.id) for a in data]

    def get_balances(self, enrollment: Enrollment) -> list[Balance]:
        """Teller exposes balances per-account at /accounts/:id/balances.
        We fan out across every account in the enrollment."""
        out: list[Balance] = []
        for acct in self.list_accounts(enrollment):
            blob = self._get(
                f"/accounts/{acct.id}/balances", enrollment.access_token
            )
            out.append(_to_balance(blob, acct))
        return out

    def get_transactions(
        self,
        enrollment: Enrollment,
        start_date: str,
        end_date: str,
        account_id: str | None = None,
    ) -> list[Transaction]:
        accounts = (
            [a for a in self.list_accounts(enrollment) if a.id == account_id]
            if account_id
            else self.list_accounts(enrollment)
        )

        out: list[Transaction] = []
        for acct in accounts:
            from_id: str | None = None
            while True:
                params: dict[str, Any] = {
                    "from_date": start_date,
                    "to_date": end_date,
                    "count": 500,
                }
                if from_id:
                    params["from_id"] = from_id
                page = self._get(
                    f"/accounts/{acct.id}/transactions",
                    enrollment.access_token,
                    params=params,
                )
                if not page:
                    break
                for tx in page:
                    out.append(_to_transaction(tx, acct.id))
                # Teller returns at most `count` items; if less, we're done.
                if len(page) < params["count"]:
                    break
                from_id = page[-1]["id"]
        return out

    def get_identity(self, enrollment: Enrollment) -> list[Identity]:
        data = self._get("/identity", enrollment.access_token)
        return [_to_identity(i) for i in data]

    # ---- http -----------------------------------------------------------

    def _get(
        self,
        path: str,
        access_token: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        resp = self._client.get(
            path, params=params, auth=(access_token, "")
        )
        if resp.status_code == 401:
            raise TellerError(
                "Teller 401 — access_token invalid or enrollment disconnected"
            )
        if resp.status_code >= 400:
            raise TellerError(
                f"Teller {resp.status_code} on {path}: {resp.text[:200]}"
            )
        return resp.json()


# ---- normalizers -------------------------------------------------------


def _to_account(blob: dict[str, Any], enrollment_id: str) -> Account:
    # Teller uses `type` ∈ {depository, credit} and `subtype` like `checking`.
    # `last_four` is the mask.
    return Account(
        id=blob["id"],
        enrollment_id=enrollment_id,
        name=blob.get("name"),
        official_name=blob.get("name"),   # Teller has no separate official_name
        type=blob.get("type"),
        subtype=blob.get("subtype"),
        mask=blob.get("last_four"),
        iso_currency=blob.get("currency"),
    )


def _to_balance(blob: dict[str, Any], acct: Account) -> Balance:
    # Teller returns `ledger` (settled) and `available`. `ledger` is what
    # Plaid calls `current`; `limit` isn't exposed (credit lines live
    # elsewhere in Teller's shape — not universally available).
    def _f(v: Any) -> float | None:
        if v is None or v == "":
            return None
        return float(v)

    return Balance(
        account_id=acct.id,
        current=_f(blob.get("ledger")),
        available=_f(blob.get("available")),
        limit=None,
        iso_currency=acct.iso_currency,
    )


def _to_transaction(blob: dict[str, Any], account_id: str) -> Transaction:
    # Teller amount is a string, sign-convention MATCHES Plaid (positive = debit
    # out of depository, i.e. spend). Categories live under `details.category`.
    details = blob.get("details") or {}
    counterparty = details.get("counterparty") or {}

    amt_raw = blob.get("amount")
    amount = float(amt_raw) if amt_raw is not None else 0.0

    return Transaction(
        id=blob["id"],
        account_id=account_id,
        amount=amount,
        iso_currency=None,  # not returned per-tx; inherit from account at caller
        date=blob.get("date") or "",
        authorized_date=None,
        name=blob.get("description"),
        merchant_name=counterparty.get("name"),
        category=details.get("category"),
        subcategory=None,
        pending=(blob.get("status") == "pending"),
        payment_channel=details.get("processing_status"),
        raw=blob,
    )


def _to_identity(blob: dict[str, Any]) -> Identity:
    owners = blob.get("owners") or []
    names: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    addresses: list[dict[str, Any]] = []
    for o in owners:
        if o.get("name"):
            names.append(o["name"])
        for e in o.get("emails") or []:
            if e.get("data"):
                emails.append(e["data"])
        for p in o.get("phone_numbers") or []:
            if p.get("data"):
                phones.append(p["data"])
        for a in o.get("addresses") or []:
            addresses.append(a.get("data") or a)
    return Identity(
        account_id=blob.get("account_id") or "",
        names=names,
        emails=emails,
        phones=phones,
        addresses=addresses,
    )
