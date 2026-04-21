"""CLI entry point.

Usage:
    python -m plaid_mcp                     # Run stdio MCP server (default)
    python -m plaid_mcp serve               # Run HTTP MCP server
    python -m plaid_mcp link                # One-shot CLI linking flow
    python -m plaid_mcp list                # Show linked institutions
"""

from __future__ import annotations

import sys
import webbrowser

import click

from .config import Config
from .link import complete_link, create_hosted_link
from .providers import Enrollment, build_provider
from .server import build_server
from .storage import Storage
from .teller_cli import _read_enrollment as _read_teller_enrollment
from .teller_cli import teller_group


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """plaid-mcp — read-only MCP server for Plaid-connected accounts."""
    if ctx.invoked_subcommand is None:
        # Default: run stdio MCP server (what Claude Desktop expects)
        build_server().run()


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8080, type=int, help="Port")
@click.option("--transport", default="http", help="http | sse | streamable-http")
def serve(host: str, port: int, transport: str) -> None:
    """Run the MCP server over HTTP for remote clients (Claude web, ChatGPT)."""
    server = build_server()
    click.echo(f"plaid-mcp listening on {host}:{port} ({transport})")
    server.run(transport=transport, host=host, port=port)


@main.command("link")
@click.option("--no-open", is_flag=True, help="Don't auto-open the browser")
def link_cmd(no_open: bool) -> None:
    """Link a new bank account via Plaid Hosted Link."""
    config = Config.from_env()
    storage = Storage(config.db_path)

    session = create_hosted_link(storage, config)
    url = session["hosted_url"]
    click.echo(f"\nOpen this URL in your browser to connect an account:\n\n  {url}\n")
    if not no_open and url:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    click.echo("Waiting for you to finish (press Ctrl-C to abort)...")

    try:
        result = complete_link(storage, session["link_token"], timeout_s=600)
    except KeyboardInterrupt:
        click.echo("\nAborted.")
        sys.exit(1)

    if result["status"] == "completed":
        click.echo(
            f"\n✓ Linked {result.get('institution_name') or 'institution'} "
            f"({result['accounts']} accounts). item_id={result['item_id']}"
        )
    else:
        click.echo(f"\n⚠ {result['message']}")
        sys.exit(2)


@main.command("list")
def list_cmd() -> None:
    """List linked institutions."""
    storage = Storage(Config.from_env().db_path)
    items = storage.list_items()
    if not items:
        click.echo("No institutions linked yet. Run:  python -m plaid_mcp link")
        return
    for i in items:
        click.echo(
            f"- {i.get('institution_name') or '(unknown)'}  "
            f"item_id={i['item_id']}  products={i['products']}"
        )


main.add_command(teller_group)


def _pick_tui_enrollment(cfg: Config, storage: Storage | None) -> Enrollment | None:
    """Pick which enrollment the TUI should open with.

    Teller: the single saved enrollment file (connect writes one).
    Plaid: the first linked item in storage — a multi-item picker is a
    later slice. Returns None when nothing is linked yet; the TUI then
    renders the empty-state screen with onboarding instructions.
    """
    if cfg.provider == "teller":
        return _read_teller_enrollment()
    if cfg.provider == "plaid" and storage is not None:
        items = storage.list_items()
        if not items:
            return None
        first = items[0]
        access_token = storage.get_access_token(first["item_id"])
        if not access_token:
            return None
        return Enrollment(
            id=first["item_id"],
            institution_id=first.get("institution_id"),
            institution_name=first.get("institution_name"),
            access_token=access_token,
            provider="plaid",
        )
    return None


@main.command("tui")
def tui_cmd() -> None:
    """Launch the Textual TUI to browse accounts and transactions.

    Works with whichever provider is selected in ``PROVIDER``. If no
    enrollment exists for that provider the TUI opens on an instructional
    empty screen.
    """
    from .tui import PlaidMcpTUI

    cfg = Config.from_env()
    storage = Storage(cfg.db_path) if cfg.provider == "plaid" else None
    enrollment = _pick_tui_enrollment(cfg, storage)

    provider = build_provider(cfg, storage) if enrollment is not None else None
    try:
        app = PlaidMcpTUI(provider=provider, enrollment=enrollment)
        app.run()
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        if storage is not None:
            storage.close()


if __name__ == "__main__":
    main()
