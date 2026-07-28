"""Plugin lifecycle and Binary Ninja command registration."""

import binaryninja
from binaryninja import PluginCommand
from binaryninja.log import log_error, log_info

from ..config import Config
from .backend import PluginBackend, render_status_report
from .mcp import MCPHandler
from .server import MCPHTTPServer
from .uicontext import list_tabs, refresh_views
from .widget import update_status


class BinjaCodeModeMCP:
    """Serves the MCP endpoint for the running Binary Ninja instance.

    Deliberately holds no BinaryView. The target binary is resolved per request
    so that opening a second tab works and edits cannot land on a stale view.
    """

    def __init__(self) -> None:
        self._config: Config | None = None
        self._server: MCPHTTPServer | None = None
        self._backend: PluginBackend | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.running

    def start_server(self, bv: object = None) -> None:
        if self.is_running:
            log_error("Code Mode MCP: already running.")
            return

        try:
            config = Config()
            config.ensure_dirs()
            backend = PluginBackend(
                config,
                tabs_provider=list_tabs,
                bn_module=binaryninja,
                on_mutation=refresh_views,
            )
            server = MCPHTTPServer(
                MCPHandler(backend),
                host=config.host,
                port=config.port,
                api_key=config.api_key,
            )
            endpoint = server.start()
        except Exception as e:
            log_error(f"Code Mode MCP: failed to start: {e}")
            update_status(False)
            return

        self._config = config
        self._server = server
        self._backend = backend

        log_info("=" * 66)
        log_info("Code Mode MCP server started")
        log_info(f"  Endpoint: {endpoint}")
        log_info(f"  API key:  {config.api_key}")
        log_info("")
        log_info("  claude mcp add --transport http binja \\")
        log_info(f"    {endpoint} \\")
        log_info(f'    --header "Authorization: Bearer {config.api_key}"')
        log_info("=" * 66)
        update_status(True)

    def stop_server(self, bv: object = None) -> None:
        if self._server is None:
            log_error("Code Mode MCP: not running.")
            return
        try:
            self._server.stop()
        except Exception as e:
            log_error(f"Code Mode MCP: error while stopping: {e}")
        finally:
            self._server = None
            self._backend = None
            self._config = None
            log_info("Code Mode MCP server stopped.")
            update_status(False)

    def show_status(self, bv: object = None) -> None:
        if self._config is None or self._backend is None:
            report = render_status_report(None, None, None)
        else:
            report = render_status_report(
                self._config.endpoint,
                self._config.api_key,
                self._backend.status().get("tabs", []),
            )
        for line in report.splitlines():
            log_info(line)

    def register_commands(self) -> None:
        # register_global, not register: the BinaryView-scoped variant hides
        # these commands until a file is open, and the server is meant to be
        # startable with none.
        PluginCommand.register_global(
            "Code Mode MCP\\Start Server",
            "Start the Code Mode MCP server",
            self.start_server,
        )
        PluginCommand.register_global(
            "Code Mode MCP\\Stop Server",
            "Stop the Code Mode MCP server",
            self.stop_server,
        )
        PluginCommand.register_global(
            # Named for where the output goes. Bringing the Log pane forward
            # from a plugin does not appear to be reachable, so the name is
            # what tells the user where to look.
            "Code Mode MCP\\Show Status in Log",
            "Write endpoint, API key, and open binaries to the Log pane",
            self.show_status,
        )
