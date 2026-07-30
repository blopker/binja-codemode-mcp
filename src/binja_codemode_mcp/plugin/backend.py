"""Wires the session, executor, and guide into the MCP Backend protocol.

Views are duck-typed throughout, so this is testable without Binary Ninja; only
`_binja_version`, the module handed to scripts, and the read-only watcher touch
the real thing, and all three degrade to None without it.
"""

import ast
import contextlib
import inspect
import keyword
import linecache
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_args

from ..config import Config
from .executor import (
    SCRIPT_PREFIX,
    TIMEOUT_CHECK_GLOBAL,
    Batch,
    CodeExecutor,
    ExecutionResult,
)
from .guide import render
from .session import BinarySession, BinaryTab, _same_view

# Supplied fresh on every call, so a saved function must never carry the
# defining call's copies of these.
LIVE_GLOBALS = ("bv", "bn", "h", "print", TIMEOUT_CHECK_GLOBAL)


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


def _holds_a_view(value: Any) -> bool:
    """Whether a value is a BinaryView, without importing Binary Ninja to ask.

    Duck-typed on three attributes that only a view carries together, so this
    stays true in tests and in the host alike.
    """
    try:
        for name in ("file", "functions", "start"):
            inspect.getattr_static(value, name)
    except AttributeError:
        return False
    return True


_SAFE_CAPTURE_LEAVES = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    range,
    type(Ellipsis),
    type(NotImplemented),
)


def _capture_problem(
    value: Any,
    path: str,
    seen: set[int] | None = None,
    *,
    allow_helper: bool = False,
) -> str | None:
    """Why retaining value in h.lib is unsafe, or None for bounded state.

    Traverse only containers whose contents are well-defined without running
    user code. Arbitrary objects can hide a BinaryView in attributes or slots,
    so accepting one would recreate the same stale-view hole under a wrapper.
    """
    if _holds_a_view(value):
        return f"{path} contains a BinaryView"
    if isinstance(value, _SAFE_CAPTURE_LEAVES):
        return None
    if isinstance(value, types.ModuleType):
        return None  # imports are rebuilt as globals and are expected captures

    if seen is None:
        seen = set()
    identity = id(value)
    if identity in seen:
        return None
    seen.add(identity)

    if isinstance(value, (types.GenericAlias, types.UnionType)) or (
        type(value).__module__ == "typing"
    ):
        for index, item in enumerate(get_args(value)):
            problem = _capture_problem(item, f"{path} argument {index + 1}", seen)
            if problem:
                return problem
        return None

    if isinstance(value, type):
        if value.__module__ == "__binja_mcp__":
            return f"{path} is a stateful script-defined class"
        return None

    if isinstance(value, types.BuiltinFunctionType):
        receiver = value.__self__
        if receiver is None or isinstance(receiver, types.ModuleType):
            return None
        return _capture_problem(receiver, f"{path} receiver", seen)

    if isinstance(value, slice):
        for name in ("start", "stop", "step"):
            problem = _capture_problem(getattr(value, name), f"{path}.{name}", seen)
            if problem:
                return problem
        return None

    if type(value) in (list, tuple):
        for index, item in enumerate(value):
            problem = _capture_problem(item, f"{path}[{index}]", seen)
            if problem:
                return problem
        return None

    if type(value) in (set, frozenset):
        for index, item in enumerate(value):
            problem = _capture_problem(item, f"{path} item {index}", seen)
            if problem:
                return problem
        return None

    if type(value) is dict:
        for index, (key, item) in enumerate(value.items()):
            problem = _capture_problem(key, f"{path} key {index}", seen)
            if problem:
                return problem
            problem = _capture_problem(item, f"{path}[{key!r}]", seen)
            if problem:
                return problem
        return None

    if isinstance(value, types.FunctionType):
        if not allow_helper:
            return (
                f"{path} is a function nested in retained state; "
                "keep helpers as direct top-level names"
            )
        if value.__closure__ is not None:
            return f"{path} is a helper function with a closure"
        for index, item in enumerate(value.__defaults__ or ()):
            problem = _capture_problem(
                item, f"{path} default argument {index + 1}", seen
            )
            if problem:
                return problem
        for name, item in (value.__kwdefaults__ or {}).items():
            problem = _capture_problem(item, f"{path} default for {name}", seen)
            if problem:
                return problem
        for name, item in getattr(value, "__annotations__", {}).items():
            problem = _capture_problem(item, f"{path} annotation on {name}", seen)
            if problem:
                return problem
        for name, item in vars(value).items():
            problem = _capture_problem(item, f"{path} attribute {name!r}", seen)
            if problem:
                return problem
        return None

    return (
        f"{path} is an opaque stateful {type(value).__name__}; "
        "construct it inside the function or pass it as an argument"
    )


def _detach_script_helper(value: Any) -> Any:
    """Drop a helper's defining globals before retaining it."""
    if not (
        isinstance(value, types.FunctionType)
        and value.__code__.co_filename.startswith(SCRIPT_PREFIX)
    ):
        return value
    detached = types.FunctionType(
        value.__code__, {}, value.__name__, value.__defaults__, None
    )
    detached.__kwdefaults__ = value.__kwdefaults__
    detached.__doc__ = value.__doc__
    detached.__annotations__ = dict(value.__annotations__)
    detached.__dict__.update(value.__dict__)
    return detached


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
    is rebuilt against the running call's live globals on the way out.
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
        # Retained values outlive this call. Keep only bounded plain data and
        # helpers that can be rebound safely to a later call's live globals.
        unsafe = _unsafe_captures(fn, self._scope())
        if unsafe:
            raise ValueError(
                f"{key!r} cannot be saved because {unsafe}. Retained state can "
                "go stale or point outside a later call's transaction. Take live "
                "values as parameters and pass bv or h.read_only_view(name) at "
                "the call."
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
            name: _detach_script_helper(value)
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
        """Rebuild the function against the running call's live globals.

        A function resolves globals from the call that defined it, so without
        this a saved function sees the first call's `bv` forever and its
        `print` writes into an output budget that closed long ago. Only the
        live globals come from the calling script — the rest of what the
        function sees is what it was defined with, so a name the caller happens
        to have bound cannot quietly change what a saved function means.

        A fresh dict per retrieval, deliberately: caching one per entry made a
        redefinition within the same script keep the previous definition's
        globals, so fixing a saved function and re-testing it in one call
        silently ran the old one.
        """
        scope = self._scope() or {}
        merged = dict(rec.captured)
        merged.update({k: scope[k] for k in LIVE_GLOBALS if k in scope})
        if "__builtins__" in scope:
            merged["__builtins__"] = scope["__builtins__"]

        # Rebind captured helper functions onto the same globals. A sibling
        # defined alongside the saved one carries its own `__globals__` — the
        # defining call's scope, including that call's `bv` — so calling it
        # would write to the binary the library was written against rather than
        # this call's target, outside any transaction and reported as success.
        # They share `merged`, so helpers that call each other still resolve.
        for name, value in list(merged.items()):
            if not isinstance(value, types.FunctionType):
                continue
            if value.__code__.co_filename.startswith(SCRIPT_PREFIX):
                helper = types.FunctionType(
                    value.__code__, merged, name, value.__defaults__, value.__closure__
                )
                helper.__kwdefaults__ = value.__kwdefaults__
                helper.__doc__ = value.__doc__
                helper.__annotations__ = dict(value.__annotations__)
                helper.__dict__.update(value.__dict__)
                merged[name] = helper

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
            # Never object.__delattr__: dropping _entries would break every
            # later use of the library.
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
        """Every definition, with what each one needs to run.

        The bodies alone are not enough to paste into a new session: a saved
        function carries the top-level imports and constants it referenced, and
        without them the text raises NameError on the first call. Anything that
        cannot be written back as source is named in a comment rather than
        silently omitted.
        """
        if not self._entries:
            return "h.lib is empty."
        blocks = []
        for key, rec in self._entries.items():
            preamble = _render_captured(rec.captured)
            blocks.append(f'# h.lib["{key}"]\n{preamble}{rec.source.rstrip()}')
        return "\n\n".join(blocks)


def _render_captured(captured: dict[str, Any]) -> str:
    """Source for the top-level names a saved function carries, where possible."""
    lines: list[str] = []

    # Imports first: a constant above the import it sits next to is valid Python
    # and reads as though it were unrelated.
    def _import_first(item: tuple[str, Any]) -> tuple[bool, str]:
        name, value = item
        return (not isinstance(value, types.ModuleType), name)

    ordered = sorted(captured.items(), key=_import_first)
    for name, value in ordered:
        if isinstance(value, types.ModuleType):
            actual = getattr(value, "__name__", name)
            lines.append(
                f"import {actual}" if actual == name else f"import {actual} as {name}"
            )
            continue
        if isinstance(value, types.FunctionType):
            try:
                lines.append(inspect.getsource(value).rstrip())
            except OSError:
                lines.append(f"# {name}() — helper source no longer available")
            continue
        try:  # only values that read back as what they are
            rendered = repr(value)
            if ast.literal_eval(rendered) == value:
                lines.append(f"{name} = {rendered}")
                continue
        except (ValueError, SyntaxError, TypeError, MemoryError):
            pass
        lines.append(f"# {name} = <{type(value).__name__}> — re-supply this by hand")
    return "\n".join(lines) + "\n\n" if lines else ""


def _unsafe_captures(fn: Any, scope: dict[str, Any] | None) -> str | None:
    """The first value a saved function cannot safely retain."""
    seen: set[int] = set()
    for i, value in enumerate(fn.__defaults__ or ()):
        problem = _capture_problem(value, f"default argument {i + 1}", seen)
        if problem:
            return problem
    for name, value in (fn.__kwdefaults__ or {}).items():
        problem = _capture_problem(value, f"the default for {name}", seen)
        if problem:
            return problem
    for name, value in getattr(fn, "__annotations__", {}).items():
        problem = _capture_problem(value, f"the annotation on {name}", seen)
        if problem:
            return problem
    if scope:
        wanted = _referenced_names(fn.__code__)
        for name, value in scope.items():
            if name in wanted and name not in LIVE_GLOBALS:
                problem = _capture_problem(
                    value,
                    f"the top-level name {name!r}",
                    seen,
                    allow_helper=True,
                )
                if problem:
                    return problem
    return None


class Helpers:
    """The `h` global: the few things not in the Binary Ninja API."""

    def __init__(self, session: BinarySession) -> None:
        self._session = session
        self._scope: dict[str, Any] | None = None
        self._batch: Batch | None = None
        self._target: BinaryTab | None = None
        self.lib = _Library(lambda: self._scope)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "lib" and "lib" in self.__dict__:
            raise AttributeError(
                "h.lib cannot be replaced. Save an entry with "
                'h.lib["name"] = your_function.'
            )
        object.__setattr__(self, name, value)

    def bind_call(self, scope: dict[str, Any], batch: Batch, target: BinaryTab) -> None:
        """Called by the executor before a script runs."""
        self._scope = scope
        self._batch = batch
        self._target = target

    def lib_sources(self) -> str:
        """Every saved definition as text, to carry a library to a new session.

        On `h` rather than `h.lib` for the same reason the library's own methods
        are private: a public name there would shadow an entry.
        """
        return self.lib._sources()

    def binaries(self) -> list[dict[str, Any]]:
        """Open binaries, as dicts with index, name and path."""
        return self._session.describe()

    def read_only_view(self, key: str) -> Any:
        """Another open binary, to read from.

        The name is the point: it is read at every call site, which is more
        often than any guide. Reads are safe because this view's transaction
        always rolls back. Detected writes also fail the call. Only the `target`
        named in the tool call is writable, and only its transaction can commit.
        """
        tab = self._session.resolve(key)
        # By view, not by display name: two tabs can share a name — the same
        # build opened twice, or a file reopened beside itself — and guarding on
        # the name would refuse a legitimate read of the other one.
        if self._target is not None and _same_view(tab.bv, self._target.bv):
            raise ValueError(
                f"{tab.name!r} is this call's target — use `bv` to write to it. "
                "h.read_only_view is for the other binary."
            )
        if self._batch is not None and not self._batch.holds(tab.bv):
            self._batch.open_read_only(tab.bv, tab.name)
        return tab.bv

    def __repr__(self) -> str:
        return (
            "<binja mcp helpers: h.binaries(), h.read_only_view(name), "
            "h.lib, h.lib_sources()>"
        )


# Mutations a script can make that mean "this view was written to". Deliberately
# excludes FunctionUpdated: analysis fires it on its own, so a read-only view
# would accuse a script that only read from it. A false negative now only misses
# the diagnostic because every read-only transaction reverts regardless; a
# false positive still fails a legitimate call.
_WRITE_NOTIFICATIONS = (
    "DataWritten",
    "DataInserted",
    "DataRemoved",
    "FunctionAdded",
    "FunctionRemoved",
    "DataVariableAdded",
    "DataVariableRemoved",
    "DataVariableUpdated",
    "SymbolAdded",
    "SymbolRemoved",
    "SymbolUpdated",
    "TypeDefined",
    "TypeUndefined",
    "TagAdded",
    "TagRemoved",
    "TagUpdated",
)


def make_watcher_factory(bn: Any) -> Callable[[Any], Any] | None:
    """Build the read-only violation detector, or None without Binary Ninja.

    Returns a callable that takes a view and returns `(was_written, release)`.
    The callback is synchronous and fires on the thread that made the change, so
    the flag is reliably set before the script's write returns.
    """
    if bn is None:
        return None
    try:
        notification_cls = bn.BinaryDataNotification
        types_enum = bn.NotificationType
    except AttributeError:
        return None

    flags = 0
    for name in _WRITE_NOTIFICATIONS:
        flag = getattr(types_enum, name, None)
        if flag is not None:
            flags |= int(flag)
    if not flags:
        return None

    def factory(view: Any) -> Any:
        seen = {"written": False}

        def mark(*_args: Any, **_kwargs: Any) -> None:
            seen["written"] = True

        members = {"__init__": lambda self: notification_cls.__init__(self, flags)}
        for hook in (
            "data_written",
            "data_inserted",
            "data_removed",
            "function_added",
            "function_removed",
            "data_var_added",
            "data_var_removed",
            "data_var_updated",
            "symbol_added",
            "symbol_removed",
            "symbol_updated",
            "type_defined",
            "type_undefined",
            "tag_added",
            "tag_removed",
            "tag_updated",
        ):
            members[hook] = mark
        watcher = type("_ReadOnlyWatch", (notification_cls,), members)()

        try:
            view.register_notification(watcher)
        except Exception:
            return None

        def release() -> None:
            view.unregister_notification(watcher)

        return (lambda: seen["written"], release)

    return factory


class PluginBackend:
    """Implements mcp.Backend."""

    def __init__(
        self,
        config: Config,
        tabs_provider: Callable[[], list[BinaryTab]],
        bn_module: Any = None,
        watcher_factory: Callable[[Any], Any] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.log = log
        self.session = BinarySession(tabs_provider)
        self.helpers = Helpers(self.session)
        self.executor = CodeExecutor(
            max_output_bytes=config.max_output_bytes,
            timeout=config.execution_timeout_s,
            queue_wait=config.queue_wait_s,
        )
        self._bn = bn_module
        self._watcher_factory = (
            watcher_factory
            if watcher_factory is not None
            else make_watcher_factory(bn_module)
        )

    def execute(
        self, code: str, target: Any = None, description: str | None = None
    ) -> ExecutionResult:
        result = self._execute(code, target, description)
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

    def _execute(
        self, code: str, target: Any, description: str | None = None
    ) -> ExecutionResult:
        try:
            tab = self.session.resolve(target)
        except LookupError as e:
            self._log(f"Code Mode MCP: refused — {e}")
            return ExecutionResult(success=False, output="", error=str(e))

        # The log is where the user watches what is being done to their
        # database, so say what and to which one before it happens rather than
        # only afterwards — a script that never returns would otherwise leave
        # no trace at all.
        said = f" — {description}" if description else ""
        self._log(f"Code Mode MCP: running on {tab.name}{said}")

        def on_call(scope: dict[str, Any], batch: Batch) -> None:
            self.helpers.bind_call(scope, batch, tab)

        # Nothing here touches the UI. Changes recorded in the undo state
        # propagate to the view on their own, and driving a refresh from this
        # thread pulled Binary Ninja to the foreground mid-session.
        result = self.executor.execute(
            code,
            target=tab.bv,
            target_name=tab.name,
            bn=self._bn,
            helpers=self.helpers,
            on_call=on_call,
            watcher_factory=self._watcher_factory,
        )
        if result.timed_out:
            verdict = "timed out"
        elif result.success:
            verdict = "ok"
        else:
            verdict = "failed" + (", rolled back" if result.reverted else "")
        self._log(f"Code Mode MCP: {verdict} in {result.elapsed_s:.1f}s")
        return result

    def _log(self, message: str) -> None:
        if self.log is None:
            return
        with contextlib.suppress(Exception):  # logging must never take a call down
            self.log(message)

    def running_script(self) -> tuple[str | None, float] | None:
        """A script in flight, for the status indicator to warn about."""
        return self.executor.running_script()

    def guide(self, topic: str | None) -> str:
        return render(self.status(), topic)

    def status(self) -> dict[str, Any]:
        try:
            tabs = self.session.tabs()
        except LookupError:
            tabs = []

        return {
            "binja_version": self._binja_version(),
            "binaries": [_describe_binary(t.bv, t.name, t.path) for t in tabs],
            "endpoint": self.config.endpoint,
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
    binaries: list[dict[str, Any]] | None,
) -> str:
    """The text behind Plugins > Code Mode MCP > Show Status in Log.

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
    if not binaries:
        lines.append("No binaries are open.")
    else:
        lines.append("Open binaries (name is what a call targets):")
        lines += [f"  {b['name']}" for b in binaries]
    return "\n".join(lines)


def _describe_binary(bv: Any, name: str, path: str = "") -> dict[str, Any]:
    """Facts the model would otherwise waste a round trip discovering."""

    def safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    entry = safe(lambda: bv.entry_point)
    return {
        "name": name,
        "path": path or safe(lambda: bv.file.filename, ""),
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
