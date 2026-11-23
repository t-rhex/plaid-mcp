"""Bearer-token gate for the HTTP transport.

``MCP_AUTH_TOKEN`` was documented as required for HTTP mode but never checked, so every
caller who could reach the port could read the linked accounts. This is a plain ASGI
middleware: when a token is configured, every HTTP request must carry
``Authorization: Bearer <token>``; anything else gets a 401 before it reaches the MCP app
(or the payment gate).
"""

from __future__ import annotations

import hmac
from typing import Any

_UNAUTHORIZED = b'{"error":"unauthorized"}'


class BearerAuth:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self._token = token.encode()

    def _allowed(self, scope: dict) -> bool:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, presented = value.partition(b" ")
                return scheme.lower() == b"bearer" and hmac.compare_digest(
                    presented.strip(), self._token
                )
        return False

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        # Lifespan events carry no request; every other connection type needs the token.
        if scope["type"] == "lifespan" or self._allowed(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(_UNAUTHORIZED)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _UNAUTHORIZED})
