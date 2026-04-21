"""CLI commands for the Teller provider — ``plaid-mcp teller <connect|probe>``.

``connect`` spins up a tiny localhost server that hosts the Teller Connect
widget (runs in the browser, not Python — that's how Teller works) and
captures the access_token from the ``onSuccess`` callback. The enrollment is
persisted at ``~/.plaid-mcp/teller/enrollment.json`` (chmod 600) so ``probe``
and future commands can reuse it without re-linking.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import socketserver
import threading
import webbrowser
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import click

from .config import Config
from .providers import Capability, Enrollment
from .providers.teller import TellerProvider

_ENROLL_PATH = Path("~/.plaid-mcp/teller/enrollment.json").expanduser()

_CONNECT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>plaid-mcp — Teller Connect</title>
<script src="https://cdn.teller.io/connect/connect.js"></script>
<style>
  body { font-family: ui-sans-serif, system-ui, sans-serif;
         max-width: 480px; margin: 8vh auto; padding: 0 1rem; color: #111; }
  h1 { font-size: 1.25rem; }
  button { font-size: 1rem; padding: .6rem 1.1rem;
           background: #111; color: #fff; border: 0; border-radius: 6px;
           cursor: pointer; }
  button:hover { background: #333; }
  code { background: #f5f5f5; padding: .1rem .3rem; border-radius: 3px; }
  .ok { color: #0a7; }
  .err { color: #b22; }
</style>
</head>
<body>
<h1>Link a bank through Teller</h1>
<p>Environment: <code>__ENV__</code>. Sandbox creds:
<code>username</code> / <code>password</code>.</p>
<button id="go">Open Teller Connect</button>
<p id="status"></p>
<script>
  const setup = TellerConnect.setup({
    applicationId: "__APPID__",
    environment: "__ENV__",
    onSuccess: async (enrollment) => {
      const el = document.getElementById("status");
      el.className = "ok";
      el.textContent = "Success — saving…";
      try {
        const resp = await fetch("/callback", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(enrollment),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        el.textContent = "Saved. You can close this tab.";
      } catch (err) {
        el.className = "err";
        el.textContent =
          "Failed to save (" + err.message + "). " +
          "Paste the JSON below into the terminal manually:";
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(enrollment, null, 2);
        pre.style.cssText =
          "background:#f5f5f5;padding:.6rem;border-radius:4px;" +
          "overflow:auto;font-size:.85rem;";
        document.body.appendChild(pre);
      }
    },
    onExit: () => {
      document.getElementById("status").className = "err";
      document.getElementById("status").textContent = "Cancelled.";
      fetch("/cancel", { method: "POST" });
    },
  });
  document.getElementById("go").addEventListener("click", () => setup.open());
</script>
</body>
</html>
"""


def _write_enrollment(enrollment: Enrollment) -> None:
    _ENROLL_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = _ENROLL_PATH.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(enrollment), f, indent=2)
    os.replace(tmp, _ENROLL_PATH)
    os.chmod(_ENROLL_PATH, 0o600)


def _read_enrollment() -> Enrollment | None:
    if not _ENROLL_PATH.exists():
        return None
    data = json.loads(_ENROLL_PATH.read_text())
    return Enrollment(**data)


def _free_port(preferred: int = 8765) -> int:
    """Grab ``preferred`` if free, else an OS-assigned high port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def _build_provider(cfg: Config) -> TellerProvider:
    return TellerProvider(
        application_id=cfg.teller_application_id,
        environment=cfg.teller_env,
        cert_path=str(cfg.teller_cert_path) if cfg.teller_cert_path else None,
        key_path=str(cfg.teller_key_path) if cfg.teller_key_path else None,
    )


@click.group("teller")
def teller_group() -> None:
    """Teller provider commands."""


@teller_group.command("connect")
@click.option("--port", default=0, type=int,
              help="Local port (default: 8765 or next free)")
@click.option("--timeout", default=300, type=int,
              help="Seconds to wait for the user to finish linking")
@click.option("--no-open", is_flag=True,
              help="Don't auto-open the browser")
def connect_cmd(port: int, timeout: int, no_open: bool) -> None:
    """Link a bank through Teller Connect (browser flow).

    Starts a throwaway localhost server, opens the Teller Connect widget in
    your browser, waits for the onSuccess callback, and saves the resulting
    enrollment. Nothing leaves your machine except the call from the widget
    to Teller's own servers.
    """
    cfg = Config.from_env()
    if not cfg.teller_application_id:
        click.echo(
            "TELLER_APPLICATION_ID is not set. Get it from "
            "https://dashboard.teller.io and add it to .env.",
            err=True,
        )
        raise SystemExit(2)

    port = port or _free_port()
    html = (
        _CONNECT_HTML
        .replace("__APPID__", cfg.teller_application_id)
        .replace("__ENV__", cfg.teller_env)
    )

    result: dict[str, Any] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:  # silence access log
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def _finish_response(self, status: int, body: bytes) -> None:
            """Write a complete response and flush before returning.

            The fetch() on the browser side waits for the server to close the
            connection (HTTP/1.0 default). If we shut the server down before
            the write flushes, the fetch hangs — which looked like the browser
            being stuck on "saving…". Explicit headers + flush avoid that.
            """
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if body:
                self.wfile.write(body)
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length) if length else b""
            if self.path == "/callback":
                try:
                    result["payload"] = json.loads(payload.decode() or "{}")
                    body = b'{"ok":true}'
                    self._finish_response(200, body)
                except json.JSONDecodeError:
                    result["error"] = "invalid JSON from Connect onSuccess"
                    self._finish_response(400, b'{"error":"bad_json"}')
                done.set()
            elif self.path == "/cancel":
                result["error"] = "user cancelled"
                self._finish_response(200, b'{"ok":true}')
                done.set()
            else:
                self._finish_response(404, b'{"error":"not_found"}')

    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    url = f"http://127.0.0.1:{port}/"
    click.echo(f"\nOpen {url} in your browser to link a bank.")
    click.echo(f"Environment: {cfg.teller_env}. Sandbox creds: username / password.\n")
    if not no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    try:
        finished = done.wait(timeout=timeout)
        # Small settle delay so the handler's response fully flushes to the
        # browser before we close the listening socket.
        if finished:
            import time as _time
            _time.sleep(0.5)
    finally:
        httpd.shutdown()
        httpd.server_close()

    if not finished:
        click.echo("Timed out waiting for Connect.", err=True)
        raise SystemExit(1)
    if "error" in result:
        click.echo(f"Connect ended: {result['error']}", err=True)
        raise SystemExit(1)

    provider = _build_provider(cfg)
    try:
        enrollment = provider.complete_enrollment(result["payload"])
    finally:
        provider.close()
    _write_enrollment(enrollment)

    click.echo(
        f"\n✓ Linked {enrollment.institution_name or 'institution'} "
        f"({enrollment.id}). Saved to {_ENROLL_PATH}."
    )


@teller_group.command("probe")
@click.option("--days", default=30, type=int,
              help="How many days of transactions to fetch")
def probe_cmd(days: int) -> None:
    """Fetch and print accounts + balances + recent transactions.

    Uses the enrollment saved by ``teller connect``.
    """
    cfg = Config.from_env()
    enrollment = _read_enrollment()
    if not enrollment:
        click.echo(
            f"No Teller enrollment found at {_ENROLL_PATH}. "
            "Run: plaid-mcp teller connect",
            err=True,
        )
        raise SystemExit(2)

    provider = _build_provider(cfg)
    try:
        caps = provider.capabilities()
        click.echo(
            f"Provider: teller  env: {cfg.teller_env}  "
            f"capabilities: {sorted(c.value for c in caps)}"
        )
        click.echo(f"Institution: {enrollment.institution_name or '(unknown)'}")
        click.echo()

        accounts = provider.list_accounts(enrollment)
        click.echo(f"Accounts ({len(accounts)}):")
        for a in accounts:
            click.echo(
                f"  [{a.type}/{a.subtype}] {a.name}  "
                f"••{a.mask or '????'}  id={a.id}"
            )
        click.echo()

        if Capability.BALANCES in caps:
            click.echo("Balances:")
            for b in provider.get_balances(enrollment):
                parts = []
                if b.current is not None:
                    parts.append(f"current={b.current:.2f}")
                if b.available is not None:
                    parts.append(f"available={b.available:.2f}")
                click.echo(f"  {b.account_id}  " + "  ".join(parts))
            click.echo()

        if Capability.TRANSACTIONS in caps:
            end = date.today()
            start = end - timedelta(days=days)
            click.echo(f"Transactions ({start} → {end}):")
            txs = provider.get_transactions(
                enrollment,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
            click.echo(f"  {len(txs)} total")
            for t in txs[:20]:
                click.echo(
                    f"  {t.date}  {t.amount:>10.2f}  "
                    f"{(t.merchant_name or t.name or '')[:40]}"
                )
            if len(txs) > 20:
                click.echo(f"  … {len(txs) - 20} more")
    finally:
        provider.close()


@teller_group.command("whoami")
def whoami_cmd() -> None:
    """Print the saved enrollment (access token redacted)."""
    enrollment = _read_enrollment()
    if not enrollment:
        click.echo("No saved enrollment.", err=True)
        raise SystemExit(2)
    masked = enrollment.access_token[:6] + "…" + enrollment.access_token[-4:]
    click.echo(
        json.dumps(
            {
                "id": enrollment.id,
                "institution_id": enrollment.institution_id,
                "institution_name": enrollment.institution_name,
                "provider": enrollment.provider,
                "access_token": masked,
                "path": str(_ENROLL_PATH),
            },
            indent=2,
        )
    )
