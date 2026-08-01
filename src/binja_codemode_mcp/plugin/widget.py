"""MCP status indicator for the Binary Ninja status bar.

A clickable button showing whether the server is running.

This module is only reachable in GUI mode — the package entry point guards on
`core_ui_enabled()` — so it imports Qt unconditionally rather than hiding the
dependency behind a try/except that leaves every name possibly-unbound.
"""

import contextlib
import logging

from binaryninja import execute_on_main_thread, execute_on_main_thread_and_wait

# binaryninjaui MUST be imported before PySide6: it selects the PySide6 build
# that matches the host, and importing PySide6 first can load the wrong one and
# crash. A test guards this ordering. It is also a compiled extension with no
# type stubs, hence the ignore.
from binaryninjaui import UIContext, UIContextNotification  # type: ignore
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

logger = logging.getLogger(__name__)

# Module-level state
_status_button = None
_status_container = None
_indicator_timer = None
_ui_notification = None
_plugin_instance = None
_tick_failure_logged = False


def _get_status_text(running: bool, script=None, *, shutting_down: bool = False) -> str:
    """The label, with a warning while a script holds the database.

    A failed script reverts the database to where its transaction opened, which
    takes any edit the user made in the meantime with it. Nothing can prevent
    that — Binary Ninja has no per-thread undo scoping — so the one mitigation
    is telling them, at the moment it matters, that now is not the time to type.
    """
    if shutting_down:
        return "🟡 MCP: Shutting down..."
    if script is not None:
        _target, elapsed, timed_out, stuck = script
        if stuck:
            return f"⚠️ MCP: call may be stuck {elapsed:.0f}s"
        if timed_out:
            return f"⚠️ MCP: timed out; native call active {elapsed:.0f}s"
        return f"⚠️ MCP: running script {elapsed:.0f}s. Do not edit"
    if running:
        return "🟢 MCP: Running"
    return "🔴 MCP: Stopped"


def _get_status_tooltip(script=None, *, shutting_down: bool = False) -> str:
    if shutting_down:
        return "Waiting for active MCP work to finish before stopping"
    if script is None:
        return "Click to start/stop MCP server"
    target, _elapsed, timed_out, stuck = script
    where = f" against {target}" if target else ""
    if stuck:
        return (
            f"A call may be stuck inside native code{where}.\n"
            "Analysis cancellation was requested; restarting Binary Ninja may "
            "be required."
        )
    if timed_out:
        return (
            f"A timed-out script is still inside native code{where}.\n"
            "The server is waiting to close its transactions and loaded views."
        )
    return (
        f"A script is running{where}.\n"
        "Edits you make now will be rolled back with it if it fails."
    )


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
        logger.error("status widget has no plugin instance")
        return

    try:
        if _plugin_instance.is_shutting_down:
            return
        # No BinaryView needed either way: the server resolves its target per
        # request and is meant to be startable with nothing open.
        if _plugin_instance.is_running:
            _plugin_instance.stop_server()
        else:
            _plugin_instance.start_server()
    except Exception:
        logger.exception("error toggling server from status widget")


def _update_status_indicator():
    """Update the status button text based on server state."""
    global _status_button, _plugin_instance

    if _status_button is None or _plugin_instance is None:
        return

    shutting_down = _plugin_instance.is_shutting_down
    running = _plugin_instance.is_running
    script = _plugin_instance.running_script if running else None
    _status_button.setText(
        _get_status_text(running, script, shutting_down=shutting_down)
    )
    _status_button.setToolTip(_get_status_tooltip(script, shutting_down=shutting_down))
    _status_button.setEnabled(not shutting_down)


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
    logger.debug("added status indicator to status bar")


def _timer_tick():
    """QTimer already fires on the main thread, so no hop is needed.

    Guarded because an exception here would otherwise print a traceback twice a
    second forever: Qt can destroy the underlying C++ widget (closing a window)
    while the Python wrapper survives, and touching it then raises.
    """
    global _status_button, _status_container, _tick_failure_logged
    try:
        _ensure_indicator_in_status_bar()
        _update_status_indicator()
    except RuntimeError:
        # Wrapped C++ object went away; rebuild on the next tick.
        _status_button = None
        _status_container = None
    except Exception:
        # Anything else keeps recurring at every tick, so report it once
        # rather than letting the timer flood the log with tracebacks.
        if not _tick_failure_logged:
            _tick_failure_logged = True
            logger.exception("status indicator update failed; suppressing repeats")


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
        logger.debug("file closed; server left running")
        execute_on_main_thread(lambda: _update_status_indicator())


def init_status_indicator(plugin_instance):
    """Initialize or replace the indicator on Qt's main thread."""
    execute_on_main_thread_and_wait(lambda: _init_status_indicator(plugin_instance))


def _init_status_indicator(plugin_instance):
    """Initialize the status indicator system.

    Args:
        plugin_instance: The BinjaCodeModeMCP plugin instance
    """
    global _indicator_timer, _ui_notification, _plugin_instance

    # Reloading the module re-runs this. Tear the previous registration down
    # first: rebinding the global would drop the last reference to a
    # notification the C++ side still holds a pointer to, and the next tab
    # switch would call into freed memory.
    _cleanup_status_indicator()

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

    logger.debug("status indicator initialized")


def update_status(running: bool, *, shutting_down: bool = False):
    """Update the status indicator.

    Args:
        running: Whether the server is running
    """
    button = _status_button
    if button is None:
        return

    def apply() -> None:
        button.setText(_get_status_text(running, shutting_down=shutting_down))
        button.setToolTip(_get_status_tooltip(shutting_down=shutting_down))
        button.setEnabled(not shutting_down)

    execute_on_main_thread(apply)


def cleanup_status_indicator():
    """Remove the indicator on Qt's main thread."""
    execute_on_main_thread_and_wait(_cleanup_status_indicator)


def _cleanup_status_indicator():
    """Clean up the status indicator resources."""
    global _indicator_timer, _ui_notification, _status_button, _status_container
    global _plugin_instance, _tick_failure_logged
    _tick_failure_logged = False

    if _indicator_timer is not None:
        _indicator_timer.stop()
        _indicator_timer.deleteLater()
        _indicator_timer = None

    if _ui_notification is not None:
        with contextlib.suppress(Exception):
            UIContext.unregisterNotification(_ui_notification)
        _ui_notification = None

    if _status_container is not None:
        with contextlib.suppress(RuntimeError):
            parent = _status_container.parent()
            remove = getattr(parent, "removeWidget", None)
            if callable(remove):
                remove(_status_container)
            _status_container.deleteLater()

    _status_button = None
    _status_container = None
    _plugin_instance = None
