"""Plugin start/stop coordination without importing Binary Ninja or Qt."""

import importlib
import sys
import threading
import types
from collections.abc import Callable
from typing import Any

import pytest


class _StubModule(types.ModuleType):
    __path__: list[str]
    PluginCommand: type
    log: types.ModuleType
    log_debug: Callable[[str], None]
    log_error: Callable[[str], None]
    log_info: Callable[[str], None]
    log_warn: Callable[[str], None]
    list_tabs: Callable[[], list[Any]]
    update_status: Callable[..., None]


@pytest.fixture
def commands_module(monkeypatch):
    errors: list[str] = []
    infos: list[str] = []
    statuses: list[tuple[bool, bool]] = []

    binaryninja = _StubModule("binaryninja")
    binaryninja.__path__ = []  # make the stub a package
    binaryninja.PluginCommand = type(
        "PluginCommand",
        (),
        {"register_global": staticmethod(lambda *args, **kwargs: None)},
    )
    log = _StubModule("binaryninja.log")
    log.log_debug = infos.append
    log.log_error = errors.append
    log.log_info = infos.append
    log.log_warn = errors.append
    binaryninja.log = log

    uicontext = _StubModule("binja_codemode_mcp.plugin.uicontext")
    uicontext.list_tabs = lambda: []
    widget = _StubModule("binja_codemode_mcp.plugin.widget")
    widget.update_status = lambda running, *, shutting_down=False: statuses.append(
        (running, shutting_down)
    )

    name = "binja_codemode_mcp.plugin.commands"
    monkeypatch.setitem(sys.modules, "binaryninja", binaryninja)
    monkeypatch.setitem(sys.modules, "binaryninja.log", log)
    monkeypatch.setitem(sys.modules, "binja_codemode_mcp.plugin.uicontext", uicontext)
    monkeypatch.setitem(sys.modules, "binja_codemode_mcp.plugin.widget", widget)
    sys.modules.pop(name, None)
    module = importlib.import_module(name)
    yield module, errors, infos, statuses
    sys.modules.pop(name, None)


class _BlockingServer:
    def __init__(self) -> None:
        self.running = True
        self.entered = threading.Event()
        self.release = threading.Event()

    def stop(self) -> None:
        self.entered.set()
        self.release.wait()
        self.running = False


class _BlockingBackend:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def wait_for_idle(self) -> None:
        self.entered.set()
        self.release.wait()


def test_shutdown_retains_the_backend_and_refuses_restart(commands_module):
    module, errors, infos, statuses = commands_module
    plugin = module.BinjaCodeModeMCP()
    server = _BlockingServer()
    backend = _BlockingBackend()
    plugin._server = server
    plugin._backend = backend
    plugin._config = object()

    plugin.stop_server()
    shutdown_thread = plugin._shutdown_thread
    assert shutdown_thread is not None
    assert server.entered.wait(1)
    assert plugin.is_shutting_down
    assert plugin._server is server
    assert plugin._backend is backend
    assert statuses == [(True, True)]

    plugin.start_server()
    assert errors[-1] == "Code Mode MCP: still shutting down."

    # HTTP handlers are gone, but a timed-out execution thread can still be
    # reverting its transaction. The old backend must remain authoritative.
    server.release.set()
    assert backend.entered.wait(1)
    assert plugin.is_shutting_down
    assert plugin._backend is backend

    backend.release.set()
    shutdown_thread.join(1)
    assert not shutdown_thread.is_alive()
    assert not plugin.is_shutting_down
    assert plugin._server is None
    assert plugin._backend is None
    assert plugin._config is None
    assert statuses[-1] == (False, False)
    assert infos[-1] == "Code Mode MCP: server stopped."
