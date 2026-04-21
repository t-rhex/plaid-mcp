# Deploying plaid-mcp to Fly.io

Operator guide for running `plaid-mcp serve` behind an x402 paywall on Fly.io
(or any container host that supports a persistent volume + env secrets).

## Prereqs

- [`flyctl`](https://fly.io/docs/hands-on/install-flyctl/) installed and `fly auth login` done
- Plaid dashboard credentials (`PLAID_CLIENT_ID`, `PLAID_SECRET`)
- A Base-compatible wallet address that will receive USDC payments (`X402_RECEIVING_ADDRESS`)
- Docker locally if you want to smoke-test before deploying

## One-time setup

```bash
# From the repo root:
fly apps create <your-app-name>                          # renames the app in fly.toml
fly volumes create plaid_mcp_data --region iad --size 1  # 1GB is plenty for SQLite + enrollment
```

## Secrets

Set every sensitive env var from `.env.example` via `fly secrets`. Never put
these in `fly.toml`:

```bash
fly secrets set \
  PLAID_CLIENT_ID=xxxxxxxxxxxx \
  PLAID_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  PLAID_ENV=production \
  PLAID_PRODUCTS=transactions \
  PLAID_OPTIONAL_PRODUCTS=investments,liabilities,identity \
  PLAID_COUNTRY_CODES=US \
  MCP_AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
  X402_RECEIVING_ADDRESS=0xYourBaseAddress \
  X402_NETWORK=base-sepolia
```

Mainnet requires an explicit opt-in:

```bash
fly secrets set X402_NETWORK=base X402_ALLOW_MAINNET=true
```

## Deploy and verify

```bash
fly deploy --config deploy/fly.toml
fly status
curl -i https://<your-app>.fly.dev/mcp/     # expect 402 Payment Required when PAYWALL=x402
```

## Environment matrix

| `X402_NETWORK` | `X402_ALLOW_MAINNET` | Default facilitator                 | Real USDC? |
| -------------- | -------------------- | ----------------------------------- | ---------- |
| `base-sepolia` | unset / false        | `https://x402.org/facilitator`      | No (testnet) |
| `base`         | **must be `true`**   | `https://x402.org/facilitator`      | **Yes**    |

Override the facilitator via `fly secrets set X402_FACILITATOR_URL=https://…`
when using a private one (Coinbase, custom).

## Ops

```bash
fly logs                                    # stream app logs
fly status                                  # machine/volume health
fly ssh console                             # shell into the running machine
fly ssh console -C "sqlite3 /home/plaidmcp/.plaid-mcp/plaid.db '.tables'"  # inspect SQLite
fly volumes list                            # confirm plaid_mcp_data is attached
```

## Rotating the receiving wallet

Tokens and linked items live in SQLite (keyed by `item_id`), independent of
the receiving wallet. Rotation is a secret update + restart:

```bash
fly secrets set X402_RECEIVING_ADDRESS=0xNewAddress
fly apps restart <your-app-name>
```

Linked items and transaction history stay intact.
