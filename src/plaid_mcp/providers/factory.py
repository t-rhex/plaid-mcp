"""Provider factory — picks the concrete provider based on Config.provider.

Usage::

    provider = build_provider(config, storage)
    enrollments = provider.list_accounts(...)

Plaid needs ``storage`` because it persists items + cached transactions
in SQLite; Teller is stateless from our side (access_tokens come from the
Connect widget and live in the caller's layer), so ``storage`` is optional
there.
"""

from __future__ import annotations

from ..config import Config
from ..storage import Storage
from .base import Provider


def build_provider(config: Config, storage: Storage | None = None) -> Provider:
    provider_name = (config.provider or "plaid").strip().lower()

    if provider_name == "teller":
        from .teller import TellerProvider

        return TellerProvider(
            application_id=config.teller_application_id,
            environment=config.teller_env,
            cert_path=str(config.teller_cert_path) if config.teller_cert_path else None,
            key_path=str(config.teller_key_path) if config.teller_key_path else None,
        )

    if provider_name == "plaid":
        from .plaid import PlaidProvider

        if storage is None:
            raise ValueError(
                "PlaidProvider requires a Storage instance — "
                "pass storage=Storage(config.db_path) to build_provider()."
            )
        return PlaidProvider(storage, config)

    raise ValueError(
        f"Unknown provider {provider_name!r}; set PROVIDER=plaid or PROVIDER=teller."
    )
