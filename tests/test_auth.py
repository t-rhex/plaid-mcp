"""MCP_AUTH_TOKEN is enforced on the HTTP transport."""

from __future__ import annotations

from click.testing import CliRunner
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from plaid_mcp.__main__ import main
from plaid_mcp.auth import BearerAuth


def _client(token: str = "s3cret") -> TestClient:
    app = Starlette(routes=[Route("/mcp", lambda r: PlainTextResponse("ok"))])
    return TestClient(BearerAuth(app, token))


def test_missing_header_is_rejected():
    r = _client().get("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_rejected():
    assert _client().get("/mcp", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_other_scheme_is_rejected():
    assert _client().get("/mcp", headers={"Authorization": "Basic s3cret"}).status_code == 401


def test_correct_token_passes():
    r = _client().get("/mcp", headers={"Authorization": "Bearer s3cret"})
    assert (r.status_code, r.text) == (200, "ok")


def test_serve_refuses_public_bind_without_token(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "x")
    monkeypatch.setenv("PLAID_SECRET", "x")
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("MCP_ALLOW_NO_AUTH", raising=False)
    result = CliRunner().invoke(main, ["serve", "--host", "0.0.0.0"])
    assert result.exit_code != 0
    assert "MCP_AUTH_TOKEN" in result.output
