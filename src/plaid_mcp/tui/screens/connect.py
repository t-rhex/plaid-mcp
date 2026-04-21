"""Screen for in-TUI Teller Connect flow.

Pressing ``c`` anywhere in the TUI pushes this screen. The user presses a
button, which kicks ``run_connect_flow`` off the UI thread — the same
localhost-server + browser flow used by ``plaid-mcp teller connect``. Status
updates stream back through a thread-safe ``app.call_from_thread`` callback
so the user sees live progress ("Waiting for browser…", "Saving…").

On success we dismiss with the new ``Enrollment``; the app then re-mounts
the accounts screen against it. On cancel/timeout we leave the screen open
with an error message so the user can retry or press ``q``/``esc`` to bail.

We import ``run_connect_flow`` lazily inside the worker — keeps this module
cheap to import and makes the monkeypatch path (``plaid_mcp.teller_cli
.run_connect_flow``) obvious in tests.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Middle, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static
from textual.worker import Worker, WorkerState

from ...config import Config
from ...providers import Enrollment


class ConnectScreen(Screen[Enrollment | None]):
    """In-app Teller Connect: button + live status + background worker."""

    DEFAULT_CSS = """
    ConnectScreen #connect-body {
        align: center middle;
        height: 1fr;
    }
    ConnectScreen #connect-box {
        padding: 1 2;
        border: round $accent;
        width: 70;
    }
    ConnectScreen #connect-title {
        text-style: bold;
        padding-bottom: 1;
    }
    ConnectScreen #connect-help {
        color: $text-muted;
        padding-bottom: 1;
    }
    ConnectScreen #connect-button {
        margin: 1 0;
    }
    ConnectScreen #connect-status {
        padding-top: 1;
        color: $text-muted;
    }
    ConnectScreen #connect-status.-error {
        color: $error;
    }
    ConnectScreen #connect-status.-success {
        color: $success;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Back", show=True),
        Binding("q", "cancel", "Back"),
    ]

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self._cfg = cfg
        self._flow_running = False

    def compose(self) -> ComposeResult:
        env = self._cfg.teller_env or "sandbox"
        sandbox_hint = (
            "Sandbox creds: username / password."
            if env == "sandbox"
            else f"Environment: {env}."
        )
        yield Header(show_clock=False)
        with Middle(id="connect-body"):
            with Center():
                with Vertical(id="connect-box"):
                    yield Static(
                        "Link a bank through Teller",
                        id="connect-title",
                    )
                    yield Static(
                        f"Environment: {env}. {sandbox_hint}\n"
                        "Press the button below — your browser will open.",
                        id="connect-help",
                    )
                    yield Button(
                        "Open Teller Connect",
                        id="connect-button",
                        variant="primary",
                    )
                    yield Static("", id="connect-status")
        yield Footer()

    # ---- actions --------------------------------------------------------

    def action_cancel(self) -> None:
        # Only let the user leave when no worker is in flight; otherwise the
        # localhost server keeps running in a background thread.
        if self._flow_running:
            self._set_status(
                "Still waiting for the browser — finish or close the tab.",
                variant="error",
            )
            return
        self.dismiss(None)

    # ---- button handler -------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "connect-button":
            return
        if self._flow_running:
            return
        self._start_flow()

    # ---- flow orchestration --------------------------------------------

    def _start_flow(self) -> None:
        self._flow_running = True
        self.query_one("#connect-button", Button).disabled = True
        self._set_status("Starting Teller Connect…")
        self.run_worker(
            self._run_flow,
            name="teller-connect",
            thread=True,
            exclusive=True,
        )

    def _run_flow(self) -> Enrollment | None:
        """Worker body — runs on a background thread.

        Imports ``run_connect_flow`` lazily so tests can monkeypatch it on
        the module (``plaid_mcp.teller_cli.run_connect_flow``) before the
        import resolves here.
        """
        from ...teller_cli import run_connect_flow

        def _status(msg: str) -> None:
            # UI widget updates must happen on the Textual event loop.
            self.app.call_from_thread(self._set_status, msg)

        try:
            return run_connect_flow(self._cfg, on_status=_status)
        except ValueError as e:
            self.app.call_from_thread(self._set_status, str(e), "error")
            return None
        except Exception as e:  # noqa: BLE001 - surface to UI, don't crash
            self.app.call_from_thread(
                self._set_status, f"Error: {e}", "error"
            )
            return None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "teller-connect":
            return
        if event.state == WorkerState.SUCCESS:
            self._flow_running = False
            result = event.worker.result
            if isinstance(result, Enrollment):
                self._set_status(
                    f"Linked {result.institution_name or 'institution'}.",
                    variant="success",
                )
                self.dismiss(result)
            else:
                # None → timeout or cancel. Leave the button re-enabled so
                # the user can retry without leaving the screen.
                self.query_one("#connect-button", Button).disabled = False
        elif event.state == WorkerState.ERROR:
            self._flow_running = False
            self.query_one("#connect-button", Button).disabled = False
            self._set_status("Connect flow failed — see logs.", variant="error")

    # ---- status helper --------------------------------------------------

    def _set_status(self, message: str, variant: str | None = None) -> None:
        status = self.query_one("#connect-status", Static)
        status.update(message)
        status.remove_class("-error")
        status.remove_class("-success")
        if variant == "error":
            status.add_class("-error")
        elif variant == "success":
            status.add_class("-success")
