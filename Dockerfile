# syntax=docker/dockerfile:1.6

# ---------- Stage 1: builder ----------
# Installs uv, resolves deps from the lockfile, and builds a self-contained
# virtualenv at /app/.venv that the runtime stage copies verbatim. Keeping
# uv out of the runtime image trims ~80MB.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv is a single static binary; no system deps needed beyond ca-certificates.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /usr/local/bin/uv

WORKDIR /app

# Copy the minimum needed to resolve the lockfile. Project sources come next
# so dependency layers stay cached when only src/ changes.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --frozen: never re-resolve, fail if lockfile drift. --no-dev: skip pytest/ruff.
RUN uv sync --frozen --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH \
    # plaid-mcp defaults the SQLite DB to ~/.plaid-mcp/plaid.db; the non-root
    # user's HOME resolves that to /home/plaidmcp/.plaid-mcp inside the image.
    HOME=/home/plaidmcp

# Create unprivileged user + home, pre-create the data dir so the VOLUME
# mount inherits correct ownership on first run.
RUN groupadd --system --gid 1000 plaidmcp \
    && useradd --system --uid 1000 --gid plaidmcp \
        --home-dir /home/plaidmcp --create-home \
        --shell /usr/sbin/nologin plaidmcp \
    && mkdir -p /home/plaidmcp/.plaid-mcp/teller \
    && chown -R plaidmcp:plaidmcp /home/plaidmcp

WORKDIR /app

# Pull in only the prebuilt venv and source tree — no uv, no build toolchain.
COPY --from=builder --chown=plaidmcp:plaidmcp /app/.venv /app/.venv
COPY --from=builder --chown=plaidmcp:plaidmcp /app/src /app/src
COPY --from=builder --chown=plaidmcp:plaidmcp /app/pyproject.toml /app/README.md ./

USER plaidmcp

EXPOSE 8080

# Persist SQLite tokens + Teller enrollment/mTLS across container restarts.
# Anything written under ~/.plaid-mcp survives; everything else is ephemeral.
VOLUME ["/home/plaidmcp/.plaid-mcp"]

# Healthcheck hits the MCP endpoint. A 402 Payment Required response (emitted
# by the x402 gate before any tool runs) still counts as "process is up" — we
# only need to confirm the server accepts connections.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/mcp/', timeout=3); sys.exit(0)" || exit 1

CMD ["plaid-mcp", "serve", "--host", "0.0.0.0", "--port", "8080"]
