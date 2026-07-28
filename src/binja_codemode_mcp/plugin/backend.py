"""Wires the session, executor, and guide into the MCP Backend protocol.

`bv` is duck-typed throughout, so this is testable without Binary Ninja; only
`_binja_version` and the module handed to scripts touch the real thing.
"""

from collections.abc import Callable
from typing import Any

from ..config import Config
from .executor import CodeExecutor, ExecutionResult
from .guide import render
from .session import BinarySession, BinaryTab


class Helpers:
    """The `h` global: the few things not in the Binary Ninja API."""

    def __init__(self, session: BinarySession) -> None:
        self._session = session

    def binaries(self) -> list[dict[str, Any]]:
        """Open binaries, as dicts with index, name, path and selected flag."""
        return self._session.describe()

    def select(self, key: int | str) -> dict[str, Any]:
        """Choose which open binary to work on, by tab index or name."""
        tab = self._session.select(key)
        return {"index": tab.index, "name": tab.name, "path": tab.path}

    def __repr__(self) -> str:
        return "<binja mcp helpers: h.binaries(), h.select(index_or_name)>"


class PluginBackend:
    """Implements mcp.Backend."""

    def __init__(
        self,
        config: Config,
        tabs_provider: Callable[[], list[BinaryTab]],
        bn_module: Any = None,
        on_mutation: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.session = BinarySession(tabs_provider)
        self.helpers = Helpers(self.session)
        self.executor = CodeExecutor(
            max_output_bytes=config.max_output_bytes,
            timeout=config.execution_timeout_s,
        )
        self._bn = bn_module
        self._on_mutation = on_mutation

    def execute(self, code: str) -> ExecutionResult:
        try:
            tab = self.session.current()
        except LookupError as e:
            return ExecutionResult(success=False, output="", error=str(e))

        result = self.executor.execute(
            code,
            bv=tab.bv if tab else None,
            bn=self._bn,
            helpers=self.helpers,
        )

        if result.success and self._on_mutation is not None:
            self._on_mutation()
        return result

    def guide(self, topic: str | None) -> str:
        return render(self.status(), topic)

    def status(self) -> dict[str, Any]:
        # current() before describe(): the first call is what pins a binary, and
        # describing first would report nothing selected on a fresh session.
        try:
            tab = self.session.current()
        except LookupError:
            tab = None

        try:
            tabs = self.session.describe()
        except LookupError:
            tabs = []

        return {
            "binja_version": self._binja_version(),
            "tabs": tabs,
            "binary": _describe_binary(tab.bv, tab.name) if tab else None,
            "endpoint": self.config.endpoint,
        }

    def _binja_version(self) -> str | None:
        if self._bn is None:
            return None
        try:
            return str(self._bn.core_version())
        except Exception:
            return None


def _describe_binary(bv: Any, name: str) -> dict[str, Any]:
    """Facts the model would otherwise waste a round trip discovering."""

    def safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    entry = safe(lambda: bv.entry_point)
    return {
        "name": name,
        "path": safe(lambda: bv.file.filename, ""),
        "view_type": safe(lambda: bv.view_type, "?"),
        "arch": safe(lambda: bv.arch.name if bv.arch else None, "?"),
        "platform": safe(lambda: bv.platform.name if bv.platform else None, "?"),
        "functions": safe(lambda: len(bv.functions), 0),
        "start": safe(lambda: hex(bv.start)),
        "end": safe(lambda: hex(bv.end)),
        "entry": hex(entry) if isinstance(entry, int) else "none",
        "analysis": safe(
            lambda: (
                "complete"
                if bv.analysis_progress.state == 2
                else str(bv.analysis_progress.state)
            ),
            "unknown",
        ),
    }
