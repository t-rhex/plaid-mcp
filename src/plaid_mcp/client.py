"""Plaid SDK client wrapper.

Encapsulates construction of the Plaid ApiClient and provides a single
entry point for the rest of the package.
"""

from __future__ import annotations

from functools import lru_cache

import plaid
from plaid.api import plaid_api

from .config import Config


def build_client(config: Config) -> plaid_api.PlaidApi:
    configuration = plaid.Configuration(
        host=config.host,
        api_key={
            "clientId": config.client_id,
            "secret": config.secret,
            "plaidVersion": "2020-09-14",
        },
    )
    api_client = plaid.ApiClient(configuration)
    return plaid_api.PlaidApi(api_client)


@lru_cache(maxsize=1)
def _cached(config_key: tuple) -> plaid_api.PlaidApi:
    # config_key is only used as a memoization key; actual config rebuilt from env
    return build_client(Config.from_env())


def get_client() -> plaid_api.PlaidApi:
    cfg = Config.from_env()
    return _cached((cfg.client_id, cfg.secret, cfg.env))
