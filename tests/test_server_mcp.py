"""MCP protocol smoke tests using FastMCP's in-memory Client transport.

Spin up the server in-process, connect a Client to it, list tools, and call
one that doesn't require Plaid to verify the full MCP request/response path.
"""

from __future__ import annotations

from fastmcp import Client

from plaid_mcp.server import build_server

EXPECTED_TOOL_NAMES = {
    "link_account",
    "complete_linking",
    "list_linked_institutions_tool",
    "remove_institution_tool",
    "list_accounts_tool",
    "get_balances_tool",
    "sync_transactions_tool",
    "refresh_transactions_tool",
    "get_transactions_tool",
    "search_transactions_tool",
    "spending_summary_tool",
    "get_holdings_tool",
    "get_investment_transactions_tool",
    "get_liabilities_tool",
    "get_identity_tool",
    "get_income_tool",
    # Debt overrides + payoff analysis
    "set_account_override_tool",
    "clear_account_override_tool",
    "list_overrides_tool",
    "add_external_debt_tool",
    "update_external_debt_tool",
    "remove_external_debt_tool",
    "list_external_debts_tool",
    "summarize_debt_tool",
}


async def test_server_exposes_all_tools():
    server = build_server()
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools}
        missing = EXPECTED_TOOL_NAMES - names
        assert not missing, f"Missing tools in MCP surface: {missing}"


async def test_list_accounts_tool_returns_empty_on_fresh_db():
    """No Plaid call required — the tool reads from storage which is empty."""
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool("list_accounts_tool", {})
        # FastMCP returns structured content; resolve to plain Python data.
        data = result.data if hasattr(result, "data") else result.structured_content
        assert data == [] or data == {"result": []} or data is None or data == []


async def test_list_linked_institutions_empty_on_fresh_db():
    server = build_server()
    async with Client(server) as client:
        result = await client.call_tool("list_linked_institutions_tool", {})
        data = result.data if hasattr(result, "data") else result.structured_content
        # Depending on FastMCP version, an empty list may unwrap differently.
        assert data in ([], {"result": []}, None)


async def test_tool_schema_has_docstring_description():
    server = build_server()
    async with Client(server) as client:
        tools = await client.list_tools()
        by_name = {t.name: t for t in tools}
        # Each tool should have a description derived from the docstring.
        assert by_name["spending_summary_tool"].description
        assert "category" in by_name["spending_summary_tool"].description.lower()
