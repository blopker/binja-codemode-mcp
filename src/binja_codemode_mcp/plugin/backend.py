"""Wires the session, executor, and guide into the MCP Backend protocol.

`bv` is duck-typed throughout, so this is testable without Binary Ninja; only
`_binja_version` and the module handed to scripts touch the real thing.
"""

import inspect
import keyword
import linecache
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from .executor import SCRIPT_PREFIX, CodeExecutor, ExecutionResult
from .guide import render
from .session import BinarySession, BinaryTab

# Supplied fresh on every call, so a saved function must never carry the
# defining call's copies of these.
LIVE_GLOBALS = ("bv", "bn", "h", "print")


@dataclass
class _Saved:
    """A saved function taken apart.

    The function object itself is deliberately not kept: `fn.__globals__` is
    the whole defining script's scope, so holding one would pin that call's
    BinaryView — and every large intermediate the script left behind — for the
    life of the session, including after the user closes that binary.
    """

    code: types.CodeType
    def_name: str
    source: str
    origin: str  # the defining script's pseudo-filename
    cached: tuple[Any, ...] | None  # and its linecache entry
    signature: str
    doc: str | None
    captured: dict[str, Any]  # top-level names it uses, minus the live globals
    defaults: tuple[Any, ...] | None = None
    kwdefaults: dict[str, Any] | None = None
    annotations: dict[str, Any] = field(default_factory=dict)


def _referenced_names(code: types.CodeType) -> set[str]:
    """Every global name the function body could read, nested scopes included.

    Over-inclusive — `co_names` also holds attribute names — which only means
    the odd extra entry is carried along, never a missing one.
    """
    names = set(code.co_names)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _referenced_names(const)
    return names


class _Library:
    """`h.lib`: functions kept for the rest of the server session.

    A mapping and a namespace at once — `h.lib["x"] = f`, `h.lib.x()`,
    `del h.lib.x` — which is why every method here is a dunder or private.
    `__getattr__` fires only when normal lookup fails, so a public method
    named `keys` or `items` would permanently shadow an entry of that name.

    Entries are functions, never values, so nothing stored can go stale: each
    is rebound to the running script's scope on the way out, and re-derives
    against whatever `bv` is live now.
    """

    _INTERNAL = ("_scope", "_entries")

    def __init__(self, scope: Callable[[], dict[str, Any] | None]) -> None:
        self._scope = scope
        self._entries: dict[str, _Saved] = {}

    def __setitem__(self, key: str, fn: Any) -> None:
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or key.startswith("_")
            or keyword.iskeyword(key)
        ):
            raise ValueError(
                f"{key!r} is not usable as a library name: use a plain identifier "
                "that is not a keyword and does not start with an underscore."
            )
        if not isinstance(fn, types.FunctionType):
            raise TypeError(
                f"h.lib holds functions, not {type(fn).__name__}. Define one with "
                "`def` (or a lambda) in this script and assign that."
            )
        if fn.__closure__ is not None:
            raise ValueError(
                f"{key!r} captured a value from this call, which would freeze it "
                "inside the saved function and go stale. Define it at the top level "
                "and take what it needs as a parameter."
            )
        origin = fn.__code__.co_filename
        if not origin.startswith(SCRIPT_PREFIX):
            raise ValueError(
                f"{key!r} must be defined in your script. A function from elsewhere "
                f"({origin}) needs its own module's globals, which rebinding removes."
            )
        try:
            source = inspect.getsource(fn)
        except OSError:  # its script aged out of the source cache
            source = f"# source unavailable for {key}"
        try:
            signature = str(inspect.signature(fn))
        except (TypeError, ValueError):
            signature = "(...)"

        # Carry the top-level names the body reads — imports, constants — so a
        # saved function is not stripped of everything its defining script set
        # up. The live globals are excluded: those are supplied fresh per call.
        defining = self._scope() or fn.__globals__
        wanted = _referenced_names(fn.__code__)
        captured = {
            name: value
            for name, value in defining.items()
            if name in wanted and name not in LIVE_GLOBALS and name != "__builtins__"
        }

        self._entries[key] = _Saved(
            code=fn.__code__,
            def_name=fn.__name__,
            source=source,
            origin=origin,
            cached=linecache.cache.get(origin),
            signature=signature,
            doc=fn.__doc__,
            captured=captured,
            defaults=fn.__defaults__,
            kwdefaults=fn.__kwdefaults__,
            annotations=dict(getattr(fn, "__annotations__", {})),
        )

    def __getitem__(self, key: str) -> Any:
        rec = self._entries.get(key)
        if rec is None:
            raise KeyError(key)
        # Republish the defining script so a traceback from inside this function
        # still quotes source, however many calls ago it was saved.
        if rec.cached is not None and rec.origin not in linecache.cache:
            linecache.cache[rec.origin] = rec.cached
        return self._rebind(key, rec)

    def _rebind(self, key: str, rec: _Saved) -> Any:
        """Rebuild the function against the running script's live globals.

        A function resolves globals from the call that defined it, so without
        this a saved function sees the first call's `bv` forever and its
        `print` writes into an output budget that closed long ago. Only the
        live globals come from the calling script — the rest of what the
        function sees is what it was defined with, so a name the caller happens
        to have bound cannot quietly change what a saved function means.
        """
        scope = self._scope() or {}
        merged = dict(rec.captured)
        merged.update({k: scope[k] for k in LIVE_GLOBALS if k in scope})
        if "__builtins__" in scope:
            merged["__builtins__"] = scope["__builtins__"]

        fn = types.FunctionType(rec.code, merged, key, rec.defaults, None)
        fn.__kwdefaults__ = rec.kwdefaults
        fn.__doc__ = rec.doc
        fn.__annotations__ = dict(rec.annotations)
        # Through __dict__ because `source` is not an attribute functions
        # declare; reading it back as `h.lib.name.source` works either way.
        fn.__dict__["source"] = rec.source
        return fn

    def __delitem__(self, key: str) -> None:
        if key not in self._entries:
            raise KeyError(f"no saved function {key!r}. Saved: {self._names()}.")
        del self._entries[key]

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self[key]
        except KeyError:
            # AttributeError, not the KeyError __getitem__ raises: hasattr()
            # swallows the former and propagates the latter.
            raise AttributeError(
                f"no saved function {key!r}. Saved: {self._names()}. "
                f'Save one with h.lib["{key}"] = your_function.'
            ) from None

    def __setattr__(self, key: str, value: Any) -> None:
        """`h.lib.name = fn` is the obvious spelling, so it must be the real one.

        Left to the default, it would write into the instance __dict__ instead:
        no validation, and the entry permanently shadowed for attribute reads
        while the subscript form and the footer still saw the saved function.
        """
        if key in self._INTERNAL and key not in self.__dict__:
            object.__setattr__(self, key, value)
            return
        if key.startswith("_"):
            raise AttributeError(f"{key} belongs to h.lib itself and cannot be set.")
        self[key] = value

    def __delattr__(self, key: str) -> None:
        if key.startswith("_"):
            # Never object.__delattr__: dropping _entries wedges every later
            # call, including ones that never touch the library.
            raise AttributeError(
                f"{key} belongs to h.lib itself and cannot be deleted."
            )
        try:
            del self[key]
        except KeyError as e:
            raise AttributeError(str(e)) from None

    def __contains__(self, key: object) -> bool:
        return key in self._entries

    def __iter__(self) -> Any:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        if not self._entries:
            return (
                'h.lib is empty. Save a function with h.lib["name"] = your_function, '
                "then call it as h.lib.name()."
            )
        lines = [f"h.lib — {len(self._entries)} saved:"]
        for key, rec in self._entries.items():
            line = f"  h.lib.{key}{rec.signature}"
            if rec.def_name != key:
                line += f"  [def {rec.def_name}]"
            doc = (rec.doc or "").strip().splitlines()
            if doc:
                line += f" — {doc[0]}"
            lines.append(line)
        return "\n".join(lines)

    def _names(self) -> str:
        return ", ".join(self._entries) or "nothing"

    def _sources(self) -> str:
        if not self._entries:
            return "h.lib is empty."
        return "\n\n".join(
            f'# h.lib["{key}"]\n{rec.source.rstrip()}'
            for key, rec in self._entries.items()
        )


class Helpers:
    """The `h` global: the few things not in the Binary Ninja API."""

    def __init__(self, session: BinarySession) -> None:
        self._session = session
        self._scope: dict[str, Any] | None = None
        self.lib = _Library(lambda: self._scope)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "lib" and "lib" in self.__dict__:
            raise AttributeError(
                "h.lib cannot be replaced. Save an entry with "
                'h.lib["name"] = your_function.'
            )
        object.__setattr__(self, name, value)

    def bind_scope(self, scope: dict[str, Any]) -> None:
        """Called by the executor before a script runs, so select() can rebind
        `bv` for the remainder of that script."""
        self._scope = scope

    def lib_sources(self) -> str:
        """Every saved definition as text, to carry a library to a new session.

        On `h` rather than `h.lib` for the same reason the library's own methods
        are private: a public name there would shadow an entry.
        """
        return self.lib._sources()

    def binaries(self) -> list[dict[str, Any]]:
        """Open binaries, as dicts with index, name, path and selected flag."""
        return self._session.describe()

    def select(self, key: int | str) -> dict[str, Any]:
        """Choose which open binary to work on, by tab index or name.

        Takes effect immediately: `bv` is rebound for the rest of this script.
        """
        tab = self._session.select(key)
        if self._scope is not None:
            self._scope["bv"] = tab.bv
        return {"index": tab.index, "name": tab.name, "path": tab.path}

    def __repr__(self) -> str:
        return (
            "<binja mcp helpers: h.binaries(), h.select(index_or_name), "
            "h.lib, h.lib_sources()>"
        )


class PluginBackend:
    """Implements mcp.Backend."""

    def __init__(
        self,
        config: Config,
        tabs_provider: Callable[[], list[BinaryTab]],
        bn_module: Any = None,
    ) -> None:
        self.config = config
        self.session = BinarySession(tabs_provider)
        self.helpers = Helpers(self.session)
        self.executor = CodeExecutor(
            max_output_bytes=config.max_output_bytes,
            timeout=config.execution_timeout_s,
        )
        self._bn = bn_module

    def execute(self, code: str) -> ExecutionResult:
        result = self._execute(code)
        # Every result carries the library, including the failures: a script
        # that saved a function and then raised keeps the definition, and the
        # footer is the only place either side can see that. Never at the cost
        # of the result itself — a footer that raised here would turn every
        # later call into an internal error, whether or not it used h.lib.
        try:
            result.lib = tuple(self.helpers.lib)
        except Exception:
            result.lib = ()
        return result

    def _execute(self, code: str) -> ExecutionResult:
        try:
            tab = self.session.current()
        except LookupError as e:
            # The pinned binary was closed. Re-pin so the session is usable
            # again rather than dead: the script never runs when current()
            # raises, so telling the model to call h.select() would be advice
            # it cannot act on.
            tab = self.session.repin()
            if tab is None:
                return ExecutionResult(success=False, output="", error=str(e))
            self.session.take_switch()  # this message is the notice
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    f"{e} Selected [{tab.index}] {tab.name} instead — "
                    "re-run your script, or call h.select() to choose another."
                ),
            )

        # A guide call re-pins too, and used to consume the switch on the way
        # past — leaving the next script to run against a database nobody chose,
        # with nothing in the response to say so. Refuse once, exactly as the
        # direct path does, so the caller that is about to write is the one told.
        switch = self.session.take_switch()
        if switch is not None:
            dropped, now = switch
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    f"The selected binary ({dropped}) is no longer open. "
                    f"Selected [{now.index}] {now.name} instead — re-run your "
                    "script, or call h.select() to choose another."
                ),
            )

        # Nothing here touches the UI. Changes recorded in the undo state
        # propagate to the view on their own, and driving a refresh from this
        # thread pulled Binary Ninja to the foreground mid-session.
        return self.executor.execute(
            code,
            bv=tab.bv if tab else None,
            bn=self._bn,
            helpers=self.helpers,
            on_scope=self.helpers.bind_scope,
        )

    def guide(self, topic: str | None) -> str:
        return render(self.status(), topic)

    def status(self) -> dict[str, Any]:
        # current() before describe(): the first call is what pins a binary, and
        # describing first would report nothing selected on a fresh session.
        try:
            tab = self.session.current()
        except LookupError:
            # Recover a stale pin here too, not just in execute(). This is the
            # orientation call a model makes right after reopening a file, and
            # reporting "no binary is open" while listing an open tab is both
            # self-contradictory and the worst possible moment to be wrong.
            tab = self.session.repin()

        try:
            tabs = self.session.describe()
        except LookupError:
            tabs = []

        # Peeked, never taken: the header saying so does not excuse the next
        # execute from saying so too.
        switch = self.session.pending_switch()

        return {
            "binja_version": self._binja_version(),
            "tabs": tabs,
            "binary": _describe_binary(tab.bv, tab.name) if tab else None,
            "endpoint": self.config.endpoint,
            "switched": {"from": switch[0], "to": switch[1].name} if switch else None,
        }

    def _binja_version(self) -> str | None:
        if self._bn is None:
            return None
        try:
            return str(self._bn.core_version())
        except Exception:
            return None


def render_status_report(
    endpoint: str | None,
    api_key: str | None,
    tabs: list[dict[str, Any]] | None,
) -> str:
    """The text behind Plugins > Code Mode MCP > Show Status.

    `endpoint` is None when the server is not running.
    """
    if endpoint is None:
        return (
            "Code Mode MCP: NOT RUNNING\n\n"
            "Start it from Plugins > Code Mode MCP > Start Server, "
            "or click the indicator in the status bar."
        )

    lines = [
        "Code Mode MCP: RUNNING",
        "",
        f"  Endpoint: {endpoint}",
        f"  API key:  {api_key}",
        "",
        "Connect a client with:",
        "",
        f"  claude mcp add --transport http binja {endpoint} \\",
        f'    --header "Authorization: Bearer {api_key}"',
        "",
    ]
    if not tabs:
        lines.append("No binaries are open.")
    else:
        lines.append("Open binaries (* = selected):")
        lines += [
            f"  {'*' if tab['selected'] else ' '} [{tab['index']}] {tab['name']}"
            for tab in tabs
        ]
    return "\n".join(lines)


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
