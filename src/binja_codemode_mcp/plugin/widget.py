"""MCP status indicator for the Binary Ninja status bar.

A clickable button showing whether the server is running.

This module is only reachable in GUI mode — the package entry point guards on
`core_ui_enabled()` — so it imports Qt unconditionally rather than hiding the
dependency behind a try/except that leaves every name possibly-unbound.
"""

import contextlib

from binaryninja import execute_on_main_thread
from binaryninja.log import log_debug, log_error

# binaryninjaui MUST be imported before PySide6: it selects the PySide6 build
# that matches the host, and importing PySide6 first can load the wrong one and
# crash. A test guards this ordering. It is also a compiled extension with no
# type stubs, hence the ignore.
from binaryninjaui import UIContext, UIContextNotification  # type: ignore
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

# Module-level state
_status_button = None
_status_container = None
_indicator_timer = None
_ui_notification = None
_plugin_instance = None


def _get_status_text(running: bool) -> str:
    """Get the status text for the button."""
    if running:
        return "🟢 MCP: Running"
    return "🔴 MCP: Stopped"


def _create_status_button():
    """Create and configure the status button widget."""
    global _status_button, _status_container

    if _status_button is not None:
        return _status_container

    _status_button = QPushButton()
    _status_button.setObjectName("mcpStatusButton")
    _status_button.setFlat(True)
    _status_button.setCursor(Qt.CursorShape.PointingHandCursor)
    _status_button.setToolTip("Click to start/stop MCP server")
    _status_button.setContentsMargins(0, 0, 0, 0)
    _status_button.setStyleSheet(
        "margin:0; padding:0 6px; border:0; border-radius:1px;"
    )
    _status_button.setText(_get_status_text(False))
    _status_button.clicked.connect(_on_button_click)

    # Wrap in container with margins
    _status_container = QWidget()
    _status_container.setObjectName("mcpStatusContainer")
    layout = QHBoxLayout(_status_container)
    layout.setContentsMargins(8, 0, 3, 0)
    layout.setSpacing(0)
    layout.addWidget(_status_button)

    return _status_container


def _on_button_click():
    """Handle status button click to toggle server state."""
    global _plugin_instance

    if _plugin_instance is None:
        log_error("MCP Status: Plugin instance not set")
        return

    try:
        # No BinaryView needed either way: the server resolves its target per
        # request and is meant to be startable with nothing open.
        if _plugin_instance.is_running:
            _plugin_instance.stop_server()
        else:
            _plugin_instance.start_server()
    except Exception as e:
        log_error(f"MCP Status: Error toggling server: {e}")


def _update_status_indicator():
    """Update the status button text based on server state."""
    global _status_button, _plugin_instance

    if _status_button is None or _plugin_instance is None:
        return

    running = _plugin_instance.is_running
    _status_button.setText(_get_status_text(running))


def _ensure_indicator_in_status_bar():
    """Ensure the status indicator is present in the status bar."""
    global _status_container

    ctx = UIContext.activeContext()
    if ctx is None:
        return

    # Get the main window, which has the status bar
    main_window = ctx.mainWindow()
    if main_window is None:
        return

    # Create button if needed
    container = _create_status_button()
    if container is None:
        return

    # Get status bar from main window
    status_bar = main_window.statusBar()
    if status_bar is None:
        return

    # Check if container is already in the status bar
    if container.parent() == status_bar:
        return

    # Insert at position 1 (after the first default widget)
    status_bar.insertWidget(1, container, 0)
    log_debug("MCP Status: Added status indicator to status bar")


def _timer_tick():
    """QTimer already fires on the main thread, so no hop is needed.

    Guarded because an exception here would otherwise print a traceback twice a
    second forever: Qt can destroy the underlying C++ widget (closing a window)
    while the Python wrapper survives, and touching it then raises.
    """
    global _status_button, _status_container
    try:
        _ensure_indicator_in_status_bar()
        _update_status_indicator()
    except RuntimeError:
        # Wrapped C++ object went away; rebuild on the next tick.
        _status_button = None
        _status_container = None


class MCPUINotification(UIContextNotification):
    """UI notification handler for MCP status updates."""

    def OnContextOpen(self, context):
        """Called when a UI context is opened."""
        execute_on_main_thread(lambda: _ensure_indicator_in_status_bar())

    def OnViewChange(self, context, frame, type_name):
        """Called when the view changes."""
        execute_on_main_thread(lambda: _update_status_indicator())

    def OnAfterCloseFile(self, context, file, frame):
        """Called after a file is closed.

        The server deliberately keeps running with no binary open. Stopping it
        would yank the endpoint out from under any connected MCP client, which
        would mark the server failed and require a reconnect; reporting "no
        binary open" per request is better.
        """
        log_debug("MCP Status: file closed; server left running")
        execute_on_main_thread(lambda: _update_status_indicator())


def init_status_indicator(plugin_instance):
    """Initialize the status indicator system.

    Args:
        plugin_instance: The BinjaCodeModeMCP plugin instance
    """
    global _indicator_timer, _ui_notification, _plugin_instance

    # Reloading the module re-runs this. Tear the previous registration down
    # first: rebinding the global would drop the last reference to a
    # notification the C++ side still holds a pointer to, and the next tab
    # switch would call into freed memory.
    cleanup_status_indicator()

    _plugin_instance = plugin_instance

    # Held in a module global for the lifetime of the session — the core keeps
    # a raw pointer and will not keep this object alive.
    _ui_notification = MCPUINotification()
    UIContext.registerNotification(_ui_notification)

    # Start periodic timer for UI updates
    _indicator_timer = QTimer()
    _indicator_timer.setInterval(500)
    _indicator_timer.timeout.connect(_timer_tick)
    _indicator_timer.start()

    log_debug("MCP Status: Status indicator initialized")


def update_status(running: bool):
    """Update the status indicator.

    Args:
        running: Whether the server is running
    """
    button = _status_button
    if button is None:
        return

    execute_on_main_thread(lambda: button.setText(_get_status_text(running)))


def cleanup_status_indicator():
    """Clean up the status indicator resources."""
    global _indicator_timer, _ui_notification, _status_button, _status_container

    if _indicator_timer is not None:
        _indicator_timer.stop()
        _indicator_timer = None

    if _ui_notification is not None:
        with contextlib.suppress(Exception):
            UIContext.unregisterNotification(_ui_notification)
        _ui_notification = None

    _status_button = None
    _status_container = None
