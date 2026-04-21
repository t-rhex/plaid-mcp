"""Shared helpers for the MCP server layer.

Centralizes the provider-agnostic plumbing used by multiple tools:

- ``list_enrollments`` — resolve the set of ``Enrollment`` objects to query,
  based on ``Config.provider``. For Plaid we walk every row in the local
  SQLite ``items`` cache; for Teller we read the single enrollment JSON
  persisted by ``plaid-mcp teller connect``.

- ``require_capability`` — raise a clean ``RuntimeError`` when a tool needs
  a capability the active provider doesn't offer. Used by the Plaid-only
  tools to 4xx cleanly under ``PROVIDER=teller`` instead of crashing deep
  in the Plaid SDK.

Keeping this logic out of ``server.py`` lets the tool bodies read as plain
"build provider, resolve enrollments, fan out" without each one duplicating
the Plaid-vs-Teller discovery dance.
"""

from __future__ import annotations

from .config import Config
from .providers import Capability, Enrollment, Provider
from .storage import Storage


def list_enrollments(config: Config, storage: Storage) -> list[Enrollment]:
    """Return every active ``Enrollment`` for the configured provider.

    Plaid: one enrollment per ``items`` row (access_token pulled from storage).
    Teller: zero or one enrollment loaded from ``~/.plaid-mcp/teller/enrollment.json``.

    Raises ``RuntimeError`` with an actionable message when Teller is
    configured but no enrollment has been saved yet.
    """
    provider_name = (config.provider or "plaid").strip().lower()

    if provider_name == "teller":
        # Import lazily so Plaid-only users don't pay the import cost and
        # don't hit the ``click`` dependency at module load time.
        from .teller_cli import _read_enrollment

        enrollment = _read_enrollment()
        if enrollment is None:
            raise RuntimeError(
                "No Teller enrollment found. Run `plaid-mcp teller connect` "
                "to link a bank before calling Teller-backed tools."
            )
        return [enrollment]

    if provider_name == "plaid":
        out: list[Enrollment] = []
        items = {i["item_id"]: i for i in storage.list_items()}
        for item_id, item in items.items():
            access_token = storage.get_access_token(item_id)
            if not access_token:
                continue
            out.append(
                Enrollment(
                    id=item_id,
                    institution_id=item.get("institution_id"),
                    institution_name=item.get("institution_name"),
                    access_token=access_token,
                    provider="plaid",
                )
            )
        return out

    raise RuntimeError(
        f"Unknown provider {provider_name!r}; set PROVIDER=plaid or PROVIDER=teller."
    )


def require_capability(provider: Provider, capability: Capability) -> None:
    """Raise a clean error when a tool needs a capability this provider lacks.

    The message names both the missing capability and the active provider so
    the caller knows exactly why the call was refused and how to switch.
    """
    caps = provider.capabilities()
    if capability in caps:
        return
    raise RuntimeError(
        f"Tool requires the {capability.value!r} capability; the active provider "
        f"({provider.name!r}) does not support it. "
        "Switch providers (e.g. set PROVIDER=plaid in your .env) and retry."
    )
