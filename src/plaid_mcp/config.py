"""Configuration — loads env vars and produces Plaid SDK enum lists."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


# Plaid retired the Development environment in late 2024; new accounts get a
# Sandbox + a limited Production trial. We keep `development` as an alias that
# points at Production so older configs still work, but treat Production as the
# primary "real data" environment.
_PLAID_ENVS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
    "development": "https://production.plaid.com",  # legacy alias
}


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


@dataclass
class Config:
    client_id: str
    secret: str
    env: str = "production"
    # Products Plaid MUST satisfy at link time. Every institution linked has to
    # support all of these, otherwise the Link flow rejects the bank.
    products: list[str] = field(default_factory=lambda: ["transactions"])
    # "Nice-to-have" products: requested if the institution supports them, and
    # the link still succeeds if not. Keeps a single server config usable
    # across banks (Citi = no investments), brokers (Fidelity = no liabilities),
    # etc. without requiring separate .env files per bank.
    optional_products: list[str] = field(
        default_factory=lambda: ["investments", "liabilities", "identity"]
    )
    country_codes: list[str] = field(default_factory=lambda: ["US"])
    client_name: str = "plaid-mcp"
    db_path: Path = field(default_factory=lambda: _expand("~/.plaid-mcp/plaid.db"))
    auth_token: str | None = None
    webhook_url: str | None = None

    # Provider selection + Teller config. Plaid remains the default so the
    # existing tools keep working; set PROVIDER=teller to switch.
    provider: str = "plaid"
    teller_application_id: str | None = None
    teller_env: str = "sandbox"
    teller_cert_path: Path | None = None
    teller_key_path: Path | None = None

    # Payment gate (opt-in, HTTP transport only). Default "none" means
    # the stdio + HTTP paths both stay free; set PAYWALL=x402 when
    # hosting plaid-mcp commercially over HTTP.
    paywall: str = "none"
    x402_receiving_address: str | None = None
    x402_network: str = "base-sepolia"
    x402_facilitator_url: str | None = None
    # Explicit opt-in required before the gate will accept Base mainnet
    # (real USDC). Without this, build_gate refuses to construct an
    # X402Gate bound to a mainnet network — guardrail against typos.
    x402_allow_mainnet: bool = False

    # MPP (Machine Payments Protocol). Multi-method rail: operators can
    # advertise any combination of Tempo stablecoin + Stripe cards; when
    # both are configured, the 402 challenge carries both and the client
    # picks one when signing. Same mainnet opt-in as the x402 block above.
    mpp_methods: list[str] = field(default_factory=lambda: ["tempo"])
    mpp_destination_address: str | None = None
    mpp_network: str = "tempo-testnet"
    mpp_allow_mainnet: bool = False
    mpp_secret_key: str | None = None
    mpp_rpc_url: str | None = None

    # Stripe card-rail config — only consulted when "stripe" is in
    # mpp_methods. STRIPE_SECRET_KEY is the merchant's Stripe API secret;
    # pympp's ChargeIntent uses it to create PaymentIntents server-side.
    stripe_secret_key: str | None = None
    stripe_payment_method_types: list[str] = field(default_factory=lambda: ["card"])
    stripe_currency: str | None = "usd"

    @property
    def host(self) -> str:
        try:
            return _PLAID_ENVS[self.env]
        except KeyError as e:
            raise ValueError(
                f"PLAID_ENV must be one of {list(_PLAID_ENVS)} (got {self.env!r})"
            ) from e

    @classmethod
    def from_env(cls) -> Config:
        provider = os.getenv("PROVIDER", "plaid").strip().lower()

        # Plaid creds are required only when PROVIDER=plaid. A Teller-only
        # user shouldn't have to set them.
        client_id = os.getenv("PLAID_CLIENT_ID", "").strip()
        secret = os.getenv("PLAID_SECRET", "").strip()
        if provider == "plaid" and (not client_id or not secret):
            raise RuntimeError(
                "PLAID_CLIENT_ID and PLAID_SECRET must be set. "
                "Copy .env.example to .env and fill in your credentials."
            )

        env = os.getenv("PLAID_ENV", "production").strip().lower()
        products = [
            p.strip().lower()
            for p in os.getenv("PLAID_PRODUCTS", "transactions").split(",")
            if p.strip()
        ]
        optional_products = [
            p.strip().lower()
            for p in os.getenv(
                "PLAID_OPTIONAL_PRODUCTS", "investments,liabilities,identity"
            ).split(",")
            if p.strip()
        ]
        # Anything already in the required list shouldn't appear in optional.
        optional_products = [p for p in optional_products if p not in products]
        country_codes = [
            c.strip().upper()
            for c in os.getenv("PLAID_COUNTRY_CODES", "US").split(",")
            if c.strip()
        ]

        teller_cert = os.getenv("TELLER_CERT_PATH", "").strip()
        teller_key = os.getenv("TELLER_KEY_PATH", "").strip()

        paywall = os.getenv("PAYWALL", "none").strip().lower() or "none"
        x402_receiving_address = os.getenv("X402_RECEIVING_ADDRESS", "").strip() or None
        x402_network = os.getenv("X402_NETWORK", "base-sepolia").strip().lower() or "base-sepolia"
        x402_facilitator_url = os.getenv("X402_FACILITATOR_URL", "").strip() or None
        x402_allow_mainnet = os.getenv("X402_ALLOW_MAINNET", "").strip().lower() in {
            "1", "true", "yes", "on",
        }

        mpp_destination_address = os.getenv("MPP_DESTINATION_ADDRESS", "").strip() or None
        mpp_network = (
            os.getenv("MPP_NETWORK", "tempo-testnet").strip().lower() or "tempo-testnet"
        )
        mpp_allow_mainnet = os.getenv("MPP_ALLOW_MAINNET", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        mpp_secret_key = os.getenv("MPP_SECRET_KEY", "").strip() or None
        mpp_rpc_url = os.getenv("MPP_RPC_URL", "").strip() or None

        # MPP_METHODS — which rails to advertise in the 402 challenge.
        # Default keeps backward-compat: unset means "tempo only", same
        # behavior as the single-method gate that shipped originally.
        mpp_methods_raw = os.getenv("MPP_METHODS", "").strip()
        if mpp_methods_raw:
            mpp_methods = [m.strip().lower() for m in mpp_methods_raw.split(",") if m.strip()]
        else:
            mpp_methods = ["tempo"]
        _VALID_MPP_METHODS = {"tempo", "stripe"}
        for m in mpp_methods:
            if m not in _VALID_MPP_METHODS:
                raise RuntimeError(
                    f"MPP_METHODS contains unknown method {m!r}; expected a comma-"
                    f"separated subset of {sorted(_VALID_MPP_METHODS)}."
                )
        if not mpp_methods:
            raise RuntimeError(
                "MPP_METHODS must be non-empty when PAYWALL=mpp. "
                "Use MPP_METHODS=tempo, MPP_METHODS=stripe, or MPP_METHODS=tempo,stripe."
            )

        stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip() or None
        stripe_pmt_raw = os.getenv("STRIPE_PAYMENT_METHOD_TYPES", "").strip()
        if stripe_pmt_raw:
            stripe_payment_method_types = [
                p.strip() for p in stripe_pmt_raw.split(",") if p.strip()
            ]
        else:
            stripe_payment_method_types = ["card"]
        stripe_currency = (
            os.getenv("STRIPE_CURRENCY", "usd").strip().lower() or "usd"
        )

        if paywall == "x402" and not x402_receiving_address:
            raise RuntimeError(
                "PAYWALL=x402 requires X402_RECEIVING_ADDRESS to be set to the "
                "Base wallet address that should receive USDC payments."
            )
        if paywall == "mpp":
            # Tempo is only required when it's an advertised method —
            # a Stripe-only operator shouldn't need a Tempo wallet.
            if "tempo" in mpp_methods and not mpp_destination_address:
                raise RuntimeError(
                    "PAYWALL=mpp with 'tempo' in MPP_METHODS requires "
                    "MPP_DESTINATION_ADDRESS to be set to the Tempo wallet "
                    "address that should receive USDC payments."
                )
            if "stripe" in mpp_methods and not stripe_secret_key:
                raise RuntimeError(
                    "PAYWALL=mpp with 'stripe' in MPP_METHODS requires "
                    "STRIPE_SECRET_KEY to be set to your Stripe API secret "
                    "(sk_live_... or sk_test_...). Get one at "
                    "https://dashboard.stripe.com/apikeys."
                )
            # Defer the "what counts as mainnet" decision to the gate
            # module so both places agree on the alias table. Only
            # applies when tempo is actually advertised.
            if "tempo" in mpp_methods:
                from .payments.mpp import is_mainnet as _mpp_is_mainnet

                if _mpp_is_mainnet(mpp_network) and not mpp_allow_mainnet:
                    raise RuntimeError(
                        f"MPP_NETWORK={mpp_network!r} resolves to a mainnet "
                        "chain but MPP_ALLOW_MAINNET is not set. Set "
                        "MPP_ALLOW_MAINNET=1 to confirm you want to accept "
                        "real USDC on Tempo."
                    )
        if paywall not in {"none", "x402", "mpp"}:
            raise RuntimeError(
                f"PAYWALL must be 'none', 'x402', or 'mpp' (got {paywall!r})."
            )

        return cls(
            client_id=client_id,
            secret=secret,
            env=env,
            products=products,
            optional_products=optional_products,
            country_codes=country_codes,
            client_name=os.getenv("PLAID_CLIENT_NAME", "plaid-mcp"),
            db_path=_expand(os.getenv("PLAID_MCP_DB", "~/.plaid-mcp/plaid.db")),
            auth_token=os.getenv("MCP_AUTH_TOKEN") or None,
            webhook_url=os.getenv("PLAID_WEBHOOK_URL") or None,
            provider=provider,
            teller_application_id=os.getenv("TELLER_APPLICATION_ID") or None,
            teller_env=os.getenv("TELLER_ENV", "sandbox").strip().lower(),
            teller_cert_path=_expand(teller_cert) if teller_cert else None,
            teller_key_path=_expand(teller_key) if teller_key else None,
            paywall=paywall,
            x402_receiving_address=x402_receiving_address,
            x402_network=x402_network,
            x402_facilitator_url=x402_facilitator_url,
            x402_allow_mainnet=x402_allow_mainnet,
            mpp_methods=mpp_methods,
            mpp_destination_address=mpp_destination_address,
            mpp_network=mpp_network,
            mpp_allow_mainnet=mpp_allow_mainnet,
            mpp_secret_key=mpp_secret_key,
            mpp_rpc_url=mpp_rpc_url,
            stripe_secret_key=stripe_secret_key,
            stripe_payment_method_types=stripe_payment_method_types,
            stripe_currency=stripe_currency,
        )

    def as_products(self):  # -> list[plaid.model.products.Products]
        from plaid.model.products import Products

        return [Products(p) for p in self.products]

    def as_optional_products(self):  # -> list[plaid.model.products.Products]
        from plaid.model.products import Products

        return [Products(p) for p in self.optional_products]

    def as_country_codes(self):  # -> list[plaid.model.country_code.CountryCode]
        from plaid.model.country_code import CountryCode

        return [CountryCode(c) for c in self.country_codes]
