"""Route standard Python logging to Binary Ninja's Log pane."""

import logging
from collections.abc import Callable

LOGGER_NAME = "binja_codemode_mcp"
LOG_PREFIX = "Code Mode MCP: "
_HANDLER_MARKER = "_binja_codemode_mcp_handler"


class _BinaryNinjaHandler(logging.Handler):
    """A logging handler whose destination is Binary Ninja."""

    def __init__(
        self,
        debug: Callable[[str], None],
        info: Callable[[str], None],
        warning: Callable[[str], None],
        error: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._sinks = (debug, info, warning, error)
        setattr(self, _HANDLER_MARKER, True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            if record.levelno >= logging.ERROR:
                sink = self._sinks[3]
            elif record.levelno >= logging.WARNING:
                sink = self._sinks[2]
            elif record.levelno >= logging.INFO:
                sink = self._sinks[1]
            else:
                sink = self._sinks[0]
            sink(message)
        except Exception:
            # A broken logging destination must never break a plugin action or
            # an MCP request.
            pass


def configure_binary_ninja_logging(
    *,
    debug: Callable[[str], None],
    info: Callable[[str], None],
    warning: Callable[[str], None],
    error: Callable[[str], None],
) -> None:
    """Install the package's one Binary Ninja handler, including after reload."""
    package_logger = logging.getLogger(LOGGER_NAME)
    for handler in tuple(package_logger.handlers):
        # The attribute survives a module reload even though isinstance()
        # against the newly created handler class would not.
        if getattr(handler, _HANDLER_MARKER, False):
            package_logger.removeHandler(handler)
            handler.close()

    handler = _BinaryNinjaHandler(debug, info, warning, error)
    handler.setFormatter(logging.Formatter(f"{LOG_PREFIX}%(message)s"))
    package_logger.addHandler(handler)
    package_logger.setLevel(logging.DEBUG)
    package_logger.propagate = False
