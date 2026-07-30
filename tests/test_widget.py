"""Status-widget teardown without loading Binary Ninja or Qt."""

import importlib
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest


class _StubModule(types.ModuleType):
    __path__: list[str]
    execute_on_main_thread: Callable[[Callable[[], None]], None]
    execute_on_main_thread_and_wait: Callable[[Callable[[], None]], None]
    log_debug: Callable[[str], None]
    log_error: Callable[[str], None]
    UIContext: type
    UIContextNotification: type
    Qt: Any
    QTimer: type
    QHBoxLayout: type
    QPushButton: type
    QWidget: type


@pytest.fixture
def widget_module(monkeypatch):
    unregistered: list[object] = []
    main_thread_calls: list[Callable[[], None]] = []

    binaryninja = _StubModule("binaryninja")
    binaryninja.__path__ = []
    binaryninja.execute_on_main_thread = lambda callback: callback()

    def on_main_thread(callback):
        main_thread_calls.append(callback)
        callback()

    binaryninja.execute_on_main_thread_and_wait = on_main_thread
    log = _StubModule("binaryninja.log")
    log.log_debug = lambda _message: None
    log.log_error = lambda _message: None

    class UIContext:
        @classmethod
        def unregisterNotification(cls, notification):
            unregistered.append(notification)

    binaryninjaui = _StubModule("binaryninjaui")
    binaryninjaui.UIContext = UIContext
    binaryninjaui.UIContextNotification = object

    pyside = _StubModule("PySide6")
    pyside.__path__ = []
    qtcore = _StubModule("PySide6.QtCore")
    qtcore.Qt = object()
    qtcore.QTimer = object
    qtwidgets = _StubModule("PySide6.QtWidgets")
    qtwidgets.QHBoxLayout = object
    qtwidgets.QPushButton = object
    qtwidgets.QWidget = object

    name = "binja_codemode_mcp.plugin.widget"
    monkeypatch.setitem(sys.modules, "binaryninja", binaryninja)
    monkeypatch.setitem(sys.modules, "binaryninja.log", log)
    monkeypatch.setitem(sys.modules, "binaryninjaui", binaryninjaui)
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PySide6.QtWidgets", qtwidgets)
    sys.modules.pop(name, None)
    module = importlib.import_module(name)
    yield module, unregistered, main_thread_calls
    sys.modules.pop(name, None)


def test_cleanup_detaches_and_deletes_qt_objects(widget_module):
    module, unregistered, main_thread_calls = widget_module

    class Timer:
        stopped = False
        deleted = False

        def stop(self):
            self.stopped = True

        def deleteLater(self):
            self.deleted = True

    class Parent:
        removed = None

        def removeWidget(self, widget):
            self.removed = widget

    class Container:
        deleted = False

        def __init__(self, parent):
            self._parent = parent

        def parent(self):
            return self._parent

        def deleteLater(self):
            self.deleted = True

    timer = Timer()
    parent = Parent()
    container = Container(parent)
    notification = object()
    module._indicator_timer = timer
    module._ui_notification = notification
    module._status_button = object()
    module._status_container = container
    module._plugin_instance = object()

    module.cleanup_status_indicator()

    assert len(main_thread_calls) == 1
    assert timer.stopped and timer.deleted
    assert unregistered == [notification]
    assert parent.removed is container
    assert container.deleted
    assert module._indicator_timer is None
    assert module._ui_notification is None
    assert module._status_button is None
    assert module._status_container is None
    assert module._plugin_instance is None


def test_initialization_is_marshaled_to_the_main_thread(widget_module, monkeypatch):
    module, _unregistered, main_thread_calls = widget_module
    plugin = object()
    initialized: list[object] = []
    monkeypatch.setattr(module, "_init_status_indicator", initialized.append)

    module.init_status_indicator(plugin)

    assert initialized == [plugin]
    assert len(main_thread_calls) == 1
