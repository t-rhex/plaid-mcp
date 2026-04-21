"""No-op gate — the default when ``PAYWALL`` is unset.

Returns the Starlette app unchanged; the HTTP server behaves exactly as
it would without the payments package installed. This is also what the
stdio path gets implicitly — no HTTP, so the gate is never consulted.
"""

from __future__ import annotations

from typing import Any


class NoopGate:
    """Pass-through gate. All requests are free."""

    name = "noop"

    def asgi_middleware(self, app: Any) -> Any:
        return app
