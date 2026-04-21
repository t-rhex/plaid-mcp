"""Provider Protocol — the normalized shape every bank-data source must speak.

Each concrete provider (Teller, Plaid, SimpleFin, ...) is responsible for
translating its native API into these dataclasses so MCP tools and the TUI
can stay source-agnostic. Capabilities gate tools at runtime: a tool that
needs ``Capability.LIABILITIES`` raises a clean error on a Teller-backed
setup instead of crashing deep in HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class Capability(str, Enum):
    ACCOUNTS = "accounts"
    BALANCES = "balances"
    TRANSACTIONS = "transactions"
    IDENTITY = "identity"
    INVESTMENTS = "investments"
    LIABILITIES = "liabilities"
    INCOME = "income"


@dataclass(frozen=True)
class Enrollment:
    """A user's link at one institution. 1 enrollment → N accounts."""

    id: str
    institution_id: str | None
    institution_name: str | None
    access_token: str
    provider: str  # "teller" | "plaid"


@dataclass(frozen=True)
class Account:
    id: str
    enrollment_id: str
    name: str | None
    official_name: str | None
    type: str | None       # depository | credit | investment | loan
    subtype: str | None    # checking | savings | credit card | ...
    mask: str | None       # last 4
    iso_currency: str | None


@dataclass(frozen=True)
class Balance:
    account_id: str
    current: float | None
    available: float | None
    limit: float | None
    iso_currency: str | None


@dataclass(frozen=True)
class Transaction:
    id: str
    account_id: str
    amount: float              # positive = outflow (spend), per Plaid convention
    iso_currency: str | None
    date: str                  # YYYY-MM-DD
    authorized_date: str | None
    name: str | None
    merchant_name: str | None
    category: str | None
    subcategory: str | None
    pending: bool
    payment_channel: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Identity:
    account_id: str
    names: list[str]
    emails: list[str]
    phones: list[str]
    addresses: list[dict[str, Any]]


class Provider(Protocol):
    """All sources of bank data implement this.

    Implementations live under ``plaid_mcp.providers.<name>`` and are
    selected at runtime via the ``PROVIDER`` env var.
    """

    name: str

    def capabilities(self) -> set[Capability]:
        """Which products this provider can serve. Tools that need a missing
        capability should 4xx clean rather than hit the wire."""
        ...

    # Linking ---------------------------------------------------------------

    def begin_enrollment(self) -> dict[str, Any]:
        """Return whatever the UI needs to start the link flow. Shape varies:
        Plaid returns ``{link_token, hosted_url}``; Teller returns
        ``{application_id, environment}`` because Connect runs client-side."""
        ...

    def complete_enrollment(self, payload: dict[str, Any]) -> Enrollment:
        """Finish a link. ``payload`` is provider-specific (e.g. Plaid's
        link_token to poll, Teller's access_token from the Connect callback).
        Returns the persisted Enrollment."""
        ...

    def remove_enrollment(self, enrollment: Enrollment) -> None:
        """Best-effort revoke upstream. Local cleanup is the caller's job."""
        ...

    # Reads -----------------------------------------------------------------

    def list_accounts(self, enrollment: Enrollment) -> list[Account]:
        ...

    def get_balances(self, enrollment: Enrollment) -> list[Balance]:
        ...

    def get_transactions(
        self,
        enrollment: Enrollment,
        start_date: str,
        end_date: str,
        account_id: str | None = None,
    ) -> list[Transaction]:
        ...

    def get_identity(self, enrollment: Enrollment) -> list[Identity]:
        ...
