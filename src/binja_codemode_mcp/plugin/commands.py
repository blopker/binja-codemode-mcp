"""Plugin lifecycle and Binary Ninja command registration."""

import logging
import threading

import binaryninja
from binaryninja import PluginCommand
from binaryninja.log import log_debug, log_error, log_info, log_warn

from ..config import Config
from .backend import PluginBackend, render_status_report
from .logging import LOG_PREFIX, configure_binary_ninja_logging
from .mcp import MCPHandler
from .server import MCPHTTPServer
from .uicontext import list_tabs, rebase_current_view
from .widget import update_status

configure_binary_ninja_logging(
    debug=log_debug,
    info=log_info,
    warning=log_warn,
    error=log_error,
)
logger = logging.getLogger(__name__)


class BinjaCodeModeMCP:
    """Serves the MCP endpoint for the running Binary Ninja instance.

    Deliberately holds no BinaryView. The target binary is resolved per request
    so that opening a second tab works and edits cannot land on a stale view.
    """

    def __init__(self) -> None:
        self._config: Config | None = None
        self._server: MCPHTTPServer | None = None
        self._backend: PluginBackend | None = None
        self._shutdown_thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.running

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_thread is not None

    @property
    def running_script(self) -> tuple[str | None, float] | None:
        if self._backend is None:
            return None
        try:
            return self._backend.running_script()
        except Exception:  # the indicator must never take the plugin down
            return None

    def start_server(self, bv: object = None) -> None:
        if self.is_shutting_down:
            logger.error("still shutting down.")
            return
        if self.is_running:
            logger.error("already running.")
            return

        try:
            config = Config()
            config.ensure_dirs()
            backend = PluginBackend(
                config,
                tabs_provider=list_tabs,
                bn_module=binaryninja,
                rebase_provider=rebase_current_view,
            )
            server = MCPHTTPServer(
                MCPHandler(backend),
                host=config.host,
                port=config.port,
                api_key=config.api_key,
            )
            endpoint = server.start()
        except Exception:
            logger.exception("failed to start")
            update_status(False)
            return

        self._config = config
        self._server = server
        self._backend = backend

        logger.info(
            "%s\nserver started\n  Endpoint: %s\n  API key:  %s\n\n"
            "  claude mcp add --transport http binja \\\n"
            "    %s \\\n"
            '    --header "Authorization: Bearer %s"\n%s',
            "=" * 66,
            endpoint,
            config.api_key,
            endpoint,
            config.api_key,
            "=" * 66,
        )
        update_status(True)

    def stop_server(self, bv: object = None) -> None:
        if self.is_shutting_down:
            logger.error("already shutting down.")
            return
        if self._server is None:
            logger.error("not running.")
            return

        server = self._server
        backend = self._backend

        def finish() -> None:
            try:
                server.stop()
                if backend is not None:
                    backend.wait_for_idle()
            except Exception:
                logger.exception("error while stopping")
            finally:
                # Keep both objects reachable until their work is gone. A
                # timed-out execute request can finish before its script does.
                if self._server is server:
                    self._server = None
                    self._backend = None
                    self._config = None
                self._shutdown_thread = None
                logger.info("server stopped.")
                update_status(False)

        self._shutdown_thread = threading.Thread(
            target=finish,
            daemon=True,
            name="binja-mcp-shutdown",
        )
        logger.info("server shutting down.")
        update_status(True, shutting_down=True)
        self._shutdown_thread.start()

    def show_status(self, bv: object = None) -> None:
        if self._config is None or self._backend is None:
            report = render_status_report(None, None, None)
        else:
            report = render_status_report(
                self._config.endpoint,
                self._config.api_key,
                self._backend.status().get("binaries", []),
            )
        # The report is also returned as standalone display text, where it
        # carries its own label. The logging handler supplies that label here.
        logger.info("%s", report.removeprefix(LOG_PREFIX))

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
