# plaid-mcp

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets Claude, ChatGPT, or any MCP-compatible client analyze your real bank, credit card, loan, and brokerage data through [Plaid](https://plaid.com).

Bring your own Plaid developer credentials, run the server locally (or on a small VPS), link your accounts through Plaid's Hosted Link flow, and then just ask:

> *"What did I spend on groceries in March?"*
> *"Show me my credit card APRs sorted by balance."*
> *"Which of my holdings are down more than 10% this year?"*
> *"I have a 0% promo on my AA card — given that, which debt should I attack first?"*

Everything runs locally. Access tokens stay in a chmod-600 SQLite file on your machine. The server never makes outbound calls except to Plaid's API.

---

## Table of contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Quick start](#quick-start)
- [Setup (detailed)](#setup-detailed)
- [Connecting it to an MCP client](#connecting-it-to-an-mcp-client)
- [Tool reference](#tool-reference)
- [Example workflows](#example-workflows)
- [How linking works under the hood](#how-linking-works-under-the-hood)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## Why this exists

Consumer financial data is locked inside whichever app happens to be connected to your bank. If you want to *ask questions* of it — "compare my March spending YoY," "what's my real effective APR across all cards," "project when I pay off this debt if I add $200/month" — you end up either exporting CSVs or trusting a SaaS aggregator with your credentials.

This project is a thin, read-only adapter: Plaid on one side, the LLM of your choice on the other. The LLM gets a small set of well-documented tools (transactions, balances, holdings, liabilities, identity, income, debt analysis). You keep your tokens.

## Features

Read-only tools grouped by Plaid product area:

- **Accounts & balances** — `list_accounts`, `get_balances`
- **Transactions** — `sync_transactions`, `refresh_transactions`, `get_transactions`, `search_transactions`, `spending_summary`
- **Investments** — `get_holdings`, `get_investment_transactions`
- **Liabilities** — `get_liabilities` (credit cards, student loans, mortgages with APRs and due dates)
- **Identity & income** — `get_identity`, `get_income`
- **Debt analysis** — `set_account_override`, `add_external_debt`, `summarize_debt` (avalanche/snowball with amortized payoff projections)
- **Account management** — `link_account`, `complete_linking`, `list_linked_institutions`, `remove_institution`

All access tokens and cached transactions live in SQLite at `~/.plaid-mcp/plaid.db` by default. Nothing leaves your machine except Plaid API calls.

## Quick start

```bash
# 1. Get the code
git clone https://github.com/YOUR_USER/plaid-mcp
cd plaid-mcp

# 2. Install (Python 3.10+)
uv sync                    # or: pip install -e .

# 3. Configure
cp .env.example .env
$EDITOR .env               # fill in PLAID_CLIENT_ID and PLAID_SECRET

# 4. Link your first bank in the browser
uv run python -m plaid_mcp link

# 5. Wire it into Claude Desktop (see below) and ask:
#    "list my accounts"
#    "sync my transactions then summarize spending last month"
```

Total setup time if you already have Plaid credentials: ~5 minutes.

## Setup (detailed)

### 1. Get Plaid credentials

You need your **own** Plaid developer account — don't share a `client_id` or reuse someone else's credentials. Plaid's terms tie each account to the person who signed up, and the Production trial tier is intended for you to link *your own* accounts.

1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com). It's free.
2. In **Developers → Keys**, note your `client_id` and one of two secrets:
   - **Sandbox secret** — fake test data. `user_good` / `pass_good` logs into `ins_109508` ("First Platypus Bank"). Great for smoke-testing without real data.
   - **Production secret** — real banks. New accounts get a Production trial (~10 linked items) without going through billing review. No time limit on the trial; it caps at 10 items.
3. Under **Team Settings → Products**, request access to **Transactions**, **Investments**, **Liabilities**, and **Identity**. Most approve instantly. **Income** requires a brief review.

> Plaid retired the separate **Development** environment in late 2024. New accounts get Sandbox + a Production trial instead. `PLAID_ENV=development` still works here for backward compatibility; it's silently routed to Production.

### 2. Install

```bash
git clone https://github.com/YOUR_USER/plaid-mcp
cd plaid-mcp
uv sync          # recommended; or: pip install -e .
```

Requires Python 3.10+. `uv` is not required but strongly recommended — it manages the virtualenv for you.

### 3. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Required fields:

```ini
PLAID_CLIENT_ID=your_client_id_here
PLAID_SECRET=your_secret_here
PLAID_ENV=production                            # or: sandbox
```

Common optional overrides:

```ini
# What Plaid products to request during linking.
# PLAID_PRODUCTS  — the bank MUST support these (link fails otherwise).
# PLAID_OPTIONAL_PRODUCTS — requested if supported; link doesn't fail if not.
PLAID_PRODUCTS=transactions
PLAID_OPTIONAL_PRODUCTS=investments,liabilities,identity

PLAID_COUNTRY_CODES=US                          # or: US,CA,GB,ES,FR,IE,NL,DE,IT
PLAID_MCP_DB=~/.plaid-mcp/plaid.db              # tilde gets expanded; file is chmod 600
PLAID_CLIENT_NAME=plaid-mcp                     # shown to the user inside Plaid Link

# For remote deployment only:
# MCP_AUTH_TOKEN=<random 32-byte token>         # required for HTTP mode
# PLAID_WEBHOOK_URL=https://yourhost/webhook    # if using webhook-driven link completion
```

**Why the two product lists?** Plaid requires every product you list under `PLAID_PRODUCTS` to be supported by the bank *at link time*. Citi doesn't have brokerage, Fidelity doesn't have liabilities, etc. `PLAID_OPTIONAL_PRODUCTS` are requested "if the bank supports them" via Plaid's `required_if_supported_products` — so one `.env` works across banks and brokers.

### 4. Link your first account

```bash
uv run python -m plaid_mcp link
# => Open this URL in your browser: https://cdn.plaid.com/link/v2/stable/link.html?...
# => After completing, press Enter.
```

Open the URL, pick your bank, complete the OAuth flow (for most banks this redirects to your bank's site and back), and return to the terminal.

You can also link new accounts directly from inside Claude/ChatGPT after the server is wired up — just say *"link a new account"* and follow the link it returns.

---

## Connecting it to an MCP client

### Claude Desktop (local, stdio)

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS or `%APPDATA%\Claude\claude_desktop_config.json` on Windows:

```json
{
  "mcpServers": {
    "plaid": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/plaid-mcp",
        "run", "python", "-m", "plaid_mcp"
      ],
      "env": {
        "PLAID_CLIENT_ID": "...",
        "PLAID_SECRET": "...",
        "PLAID_ENV": "production"
      }
    }
  }
}
```

Restart Claude Desktop. You should see "plaid" appear in the tools menu.

### Claude.ai web or ChatGPT (remote, HTTPS)

Run in HTTP mode:

```bash
MCP_AUTH_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
  uv run python -m plaid_mcp serve --host 0.0.0.0 --port 8080
```

Expose it with TLS (Caddy, Cloudflare Tunnel, ngrok). In Claude.ai or ChatGPT add it as a **Custom Connector** / **MCP Connector** and paste the bearer token. **Never expose this server without TLS and `MCP_AUTH_TOKEN` set.**

### Other clients

Anything that speaks MCP will work. The server is built on [FastMCP](https://github.com/jlowin/fastmcp), which supports both stdio and HTTP transports.

---

## Tool reference

> All dates are ISO strings (`YYYY-MM-DD`). Amounts follow Plaid's convention: **positive for outflows** (spending), **negative for inflows** (deposits).

### Account management

- **`link_account()`** — Start a new Plaid Link session. Returns a `hosted_link_url` the user opens in their browser to authenticate with their bank.
- **`complete_linking(link_token, timeout_seconds=180)`** — Finalize a Link session once the user has completed it in the browser.
- **`list_linked_institutions()`** — Every institution currently linked, with account counts and any errors.
- **`remove_institution(item_id)`** — Unlink an institution and purge its cached data.

### Accounts & balances

- **`list_accounts()`** — All accounts across all linked institutions, from the local cache. Fast.
- **`get_balances(account_id=None)`** — Live balance lookup (hits Plaid, not the cache). Optionally filter to one account.

### Transactions

- **`sync_transactions(wait_for_ready=True, wait_timeout_seconds=60)`** — Pull the latest transactions into the local cache using Plaid's cursor-based `/transactions/sync`. Incremental and idempotent. On first call after linking, Plaid runs the historical pull asynchronously; this tool blocks briefly until `HISTORICAL_UPDATE_COMPLETE`.
- **`refresh_transactions(item_id=None)`** — Ask Plaid to re-pull from the bank *right now*. Use when a user just made a purchase and wants to see it, or when data looks stale. Asynchronous — wait 30–60s then call `sync_transactions`. Some smaller banks don't support on-demand refresh.
- **`get_transactions(start_date, end_date, account_id=?, category=?, merchant=?, min_amount=?, max_amount=?, limit=500)`** — Query the cache. Filter by any combination of the above.
- **`search_transactions(query, start_date=?, end_date=?, limit=100)`** — Fuzzy search across transaction description + merchant name.
- **`spending_summary(start_date, end_date, group_by="category")`** — Aggregate spending. `group_by` can be `category`, `subcategory`, `merchant`, or `account`.

### Investments

- **`get_holdings(account_id=None)`** — Current positions: tickers, quantities, market value, cost basis.
- **`get_investment_transactions(start_date, end_date, account_id=None, limit=250)`** — Buys, sells, dividends, fees.

### Liabilities

- **`get_liabilities()`** — Credit cards (statement balance, minimum payment, due dates, APRs), student loans (balance, interest rate, payoff date, servicer), mortgages (principal, rate, maturity, next payment).

### Identity & income

- **`get_identity(account_id=None)`** — Account-holder names, emails, phones, addresses as reported by each institution.
- **`get_income()`** — Bank-detected income streams. Requires Plaid's Income product enabled in your dashboard.

### Debt analysis

Plaid's liabilities data covers the basics but misses things — notably promotional APRs on credit cards (0% intro offers, balance-transfer promos). This layer lets you annotate what Plaid missed and run honest payoff math.

> **Not financial advice.** These tools do straightforward amortization math and rank debts by APR or balance — nothing more. For decisions that meaningfully affect your finances (debt consolidation, refinancing, tax implications of early payoff, etc.), talk to a CFP, CPA, or attorney. The outputs here are a starting point for a conversation with a professional, not a substitute for one.

- **`set_account_override(account_id, effective_apr=?, promo_expires=?, note=?)`** — Record the true APR for a linked card. After `promo_expires`, analysis reverts to Plaid's reported purchase APR.
- **`clear_account_override(account_id)`** — Remove an override.
- **`list_overrides()`** — List all overrides.
- **`add_external_debt(name, balance, apr, minimum_payment=0.0, next_payment_due_date=?, promo_expires=?, note=?)`** — Track a debt that isn't behind a linked Plaid account (BNPL, medical, 401(k) loans, small lenders). APR is a percentage (e.g. `18.5`, not `0.185`).
- **`update_external_debt(debt_id, ...)`** — Partial update to any field.
- **`remove_external_debt(debt_id)`**
- **`list_external_debts()`**
- **`summarize_debt(strategy="avalanche", extra_monthly_payment=0.0, today=?)`** — Merges Plaid credit cards + overrides + external debts, ranks them, and projects payoff:
  - **`avalanche`** (default) — highest effective APR first, minimizes interest paid.
  - **`snowball`** — lowest balance first, fastest sense of progress.
  - Returns total balance, monthly interest accrual at current rates, priority debt, amortized payoff timelines (minimum-only vs. with the extra payment), and warnings for promos expiring within 60 days. Flags debts whose minimum payment can't even cover monthly interest.

---

## Example workflows

### "Summarize my spending last month"

```
You: sync my transactions then show me top 10 merchants by spend in March 2026
LLM: [calls sync_transactions_tool, then spending_summary_tool with group_by=merchant]
     Top merchants in March 2026:
       Whole Foods            $412.80   (14 transactions)
       Amazon                 $287.43   (9 transactions)
       ...
```

### "Which debt should I pay first?" — with a 0% promo

Plaid says your AA card is 26.49%, but it's actually on a 0% promo until 2027. By default, an LLM would tell you to attack the AA card first (because it sees 26.49%) — which is exactly wrong. Fix it:

```
You: my AA card is actually at 0% promo APR until 2027-01-01. set that override,
     then run an avalanche summary.

LLM: [set_account_override_tool(account_id="...", effective_apr=0.0,
                                promo_expires="2027-01-01")]
     [summarize_debt_tool(strategy="avalanche")]

     Priority: Costco Anywhere Visa, balance $1,204, effective APR 18.74%
     (was going to recommend the AA card at 26.49%, but you have a 0% override
      until 2027-01-01 — AA is now ranked last.)

     At minimum-only payments, the Costco card pays off in 78 months with
     $1,142 in interest. Add $200/mo and you're done in 7 months paying $96
     total — saves you ~$1,046 in interest.
```

### "Track a BNPL I opened outside the linked banks"

```
You: I just took out a $1,500 Affirm loan for a mattress, 0% APR for 12 months,
     $125/mo minimum. Add it.

LLM: [add_external_debt_tool(name="Affirm Mattress", balance=1500, apr=0,
                             minimum_payment=125, promo_expires=<in 12mo>)]

     Added (ext_a1b2c3d4e5f6). Included in summarize_debt going forward.
```

### "Show me stale data or link errors"

```
You: are any of my linked accounts broken?
LLM: [calls list_linked_institutions_tool]
     Chase — 5 accounts, last sync 3h ago, no errors.
     Citi — 4 accounts, last_error: "ITEM_LOGIN_REQUIRED" (your password likely
            changed; re-link via `link a new account`).
```

### "I just made a purchase and want to see it"

```
You: I just paid at Whole Foods 10 minutes ago, refresh and show it.
LLM: [refresh_transactions_tool()]
     [waits ~45 seconds]
     [sync_transactions_tool()]
     [search_transactions_tool(query="whole foods", start_date=<today>)]
     Found it: $84.12 at WHOLE FOODS MARKET today.
```

---

## How linking works under the hood

This server uses Plaid's **Hosted Link** flow so you never need to embed a web widget. The tool sequence an LLM follows to add a bank:

1. **`link_account`** — creates a `link_token` with a `hosted_link` object, returns the `hosted_link_url` and the raw `link_token`.
2. User opens that URL in a browser, completes the OAuth flow with their bank.
3. **`complete_linking(link_token)`** — polls `/link/token/get` until it sees a `public_token` in `link_sessions[].results.item_add_results[]`, exchanges it for a permanent `access_token`, and caches the account list.

Per Plaid's docs, webhooks (`SESSION_FINISHED` event) are the *recommended* production mechanism for retrieving the `public_token`. This server uses polling instead because it requires no public endpoint — fine for personal CLI / stdio use. If you deploy remotely and want webhook-driven completion, set `PLAID_WEBHOOK_URL` in `.env` and add a webhook handler (not included yet — PRs welcome).

---

## Security notes

- **Access tokens** are stored in SQLite at `PLAID_MCP_DB` (default `~/.plaid-mcp/plaid.db`). On macOS and Linux the file is chmod'd to `0600`.
- **Read-only, by design.** There are no tools that move money, create transfers, or modify anything upstream. Plaid's `/transfer/*` endpoints are not exposed. The worst a prompt-injected LLM can do is read your data — not move it.
- **If you run it remotely**, put it behind TLS and set `MCP_AUTH_TOKEN` to a random string. Never expose it over plain HTTP or without auth.
- **If you deploy this for anyone other than yourself**, you need to complete Plaid's [Production Enablement review](https://plaid.com/docs/launch-checklist/) first. The Production trial tier covers personal use; hosting a multi-user instance on your `client_id` without review violates Plaid's terms. Every user running their own copy with their own Plaid credentials is fine — that's the intended open-source usage.
- **Plaid's terms prohibit storing bank credentials** — and this server never sees them. Plaid Link handles credentials directly with the institution; this server only gets a per-user access token.
- **The LLM sees your financial data** while it's answering questions. Choose a provider you trust, and consider running against the Sandbox environment first to get a feel for what flows through context.

---

## Troubleshooting

### `INVALID_PRODUCT: Your account is not enabled for <product>`

You requested a product in `PLAID_PRODUCTS` that your Plaid dashboard isn't approved for. Go to **Team Settings → Products**, request access, and wait for approval. Or drop the product from `PLAID_PRODUCTS` (and put it in `PLAID_OPTIONAL_PRODUCTS` instead if you still want it when available).

### "No investment accounts" when linking a non-brokerage bank (e.g. Citi)

You had `investments` in `PLAID_PRODUCTS`, which makes Plaid reject banks without brokerage. Move `investments` from `PLAID_PRODUCTS` to `PLAID_OPTIONAL_PRODUCTS` and re-link.

### `sync_transactions` returns 0 transactions right after linking

Plaid's historical pull is async. The server blocks up to 60s for `HISTORICAL_UPDATE_COMPLETE` — but some banks (notoriously Citi) can take hours to backfill. Check `list_linked_institutions` for `last_error`. If there's no error, just wait and try again; the `status` field in the sync response tells you what Plaid is up to.

### `refresh_transactions` returns `PRODUCT_NOT_READY`

Some smaller institutions don't support on-demand refresh. Plaid will still refresh them on its normal schedule (every few hours). This is an institution limitation, not a bug in the server.

### Tokens got lost / I want to start over

Remove the SQLite DB: `rm ~/.plaid-mcp/plaid.db`. All your cached transactions and tokens go with it. You'll need to re-link every institution.

### The LLM hallucinates numbers

Ask it to call the tools explicitly: *"call sync_transactions_tool, then call spending_summary_tool with..."*. Also: instruct the LLM to always cite the tool output it's reasoning from. Model choice matters; the default instructions in the MCP server nudge toward tool use.

### Claude Desktop doesn't see the tools

Confirm `uv` is on your PATH (`which uv`). Claude Desktop's launchd-style environment often doesn't inherit your shell PATH. You may need the absolute path (e.g. `"command": "/Users/you/.cargo/bin/uv"`). Restart Claude Desktop after config changes.

---

## Development

```bash
uv sync --extra dev          # or: pip install -e ".[dev]"
ruff check .
pytest                       # unit + MCP smoke tests (no credentials needed)
pytest -m sandbox            # end-to-end tests against Plaid Sandbox
```

Sandbox tests read credentials from `.env.test` at the repo root (git-ignored). Create it when you want to run them:

```ini
PLAID_CLIENT_ID=your_sandbox_client_id
PLAID_SECRET=your_sandbox_secret
```

Sandbox tests use `/sandbox/public_token/create` to skip Plaid Link entirely, so no browser needed.

CI (GitHub Actions) runs on every push:
- Unit + MCP smoke tests on Python 3.10 / 3.11 / 3.12.
- Sandbox integration tests, if `PLAID_CLIENT_ID_SANDBOX` and `PLAID_SECRET_SANDBOX` are set as repository secrets.

### Contributions

Welcome — particularly:
- Additional Plaid product coverage (Assets, Statements, Signal).
- Webhook handling for real-time transaction sync.
- Export tools (write summaries to CSV / Markdown / Google Sheets).
- Better LLM prompting for the debt workflows.

Please keep all tools **read-only**. No PR that introduces write endpoints (transfers, bill pay, account modification) will be merged.

## License

MIT. See [LICENSE](./LICENSE).
