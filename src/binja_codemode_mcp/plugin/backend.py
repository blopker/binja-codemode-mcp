"""Wires the session, executor, and guide into the MCP Backend protocol.

Views are duck-typed throughout, so this is testable without Binary Ninja; only
`_binja_version`, the module handed to scripts, and the read-only watcher touch
the real thing, and all three degrade to None without it.
"""

import ast
import dis
import inspect
import linecache
import logging
import threading
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Config
from .artifact import ArtifactSpec
from .executor import (
    TIMEOUT_CHECK_GLOBAL,
    Batch,
    CodeExecutor,
    ExecutionResult,
    compile_script,
    next_script_name,
    publish_source,
)
from .guide import render
from .logging import LOG_PREFIX
from .rebase import (
    capture_rebase_state,
    format_rebase_result,
    rebase_backup_path,
    validate_rebase_request,
    verify_rebase,
)
from .session import BinarySession, BinaryTab, same_view

# Supplied fresh on every call, so a saved function must never carry the
# defining call's copies of these.
LIVE_GLOBALS = ("__name__", "bv", "bn", "h", "print", TIMEOUT_CHECK_GLOBAL)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Saved:
    """A self-contained function without its defining globals."""

    code: types.CodeType
    source: str
    origin: str
    signature: str
    doc: str | None
    defaults: tuple[Any, ...] | None = None
    kwdefaults: tuple[tuple[str, Any], ...] = ()


_IMMUTABLE_DEFAULT_LEAVES = (
    type(None),
    bool,
    int,
    float,
    complex,
    str,
    bytes,
    type(Ellipsis),
    type(NotImplemented),
)


def _default_problem(value: Any, path: str) -> str | None:
    """Why a default is not deeply immutable."""
    if type(value) in _IMMUTABLE_DEFAULT_LEAVES:
        return None
    if type(value) is tuple:
        for index, item in enumerate(value):
            problem = _default_problem(item, f"{path}[{index}]")
            if problem:
                return problem
        return None
    return (
        f"{path} is {type(value).__name__}, not a deeply immutable value; "
        "pass it when calling the saved function"
    )


def _default_syntax_problem(node: ast.expr, path: str) -> str | None:
    """Reject default expressions that would run code while defining a helper."""
    if isinstance(node, ast.Constant) and type(node.value) in _IMMUTABLE_DEFAULT_LEAVES:
        return None
    if isinstance(node, ast.Tuple):
        for index, item in enumerate(node.elts):
            problem = _default_syntax_problem(item, f"{path}[{index}]")
            if problem:
                return problem
        return None
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in (int, float, complex)
    ):
        return None
    return f"{path} must be an immutable literal"


def _definition(source: str) -> ast.FunctionDef:
    try:
        tree = ast.parse(source, "<lib-function>", "exec")
    except SyntaxError as e:
        raise ValueError(f"Syntax error: {e.msg} (line {e.lineno}).") from None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError("Source must contain exactly one top-level `def`.")
    fn = tree.body[0]
    if fn.name.startswith("_"):
        raise ValueError("Library function names cannot start with an underscore.")
    if fn.decorator_list:
        raise ValueError("Library functions cannot have decorators.")
    for index, default in enumerate(fn.args.defaults):
        problem = _default_syntax_problem(default, f"default argument {index + 1}")
        if problem:
            raise ValueError(problem)
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True):
        if default is None:
            continue
        problem = _default_syntax_problem(default, f"the default for {arg.arg}")
        if problem:
            raise ValueError(problem)
    return fn


def _global_reads(code: types.CodeType) -> set[str]:
    """Names loaded from function globals, including nested bodies.

    LOAD_NAME alongside LOAD_GLOBAL because nested class bodies read
    unqualified names with it; missing those passes a definition that
    NameErrors at call time. A name the same code object also stores is a
    class-body local, not a global read.
    """
    instructions = list(dis.get_instructions(code))
    stored = {
        str(instruction.argval)
        for instruction in instructions
        if instruction.opname == "STORE_NAME"
    }
    names = {
        str(instruction.argval)
        for instruction in instructions
        if instruction.opname == "LOAD_GLOBAL"
        or (instruction.opname == "LOAD_NAME" and str(instruction.argval) not in stored)
    }
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            names |= _global_reads(const)
    return names


def _unsupported_globals(fn: types.FunctionType) -> list[str]:
    """Global reads that a fresh call cannot supply safely."""
    defining = fn.__globals__
    builtins_value = defining.get("__builtins__", {})
    builtin_names = (
        set(builtins_value)
        if isinstance(builtins_value, dict)
        else set(vars(builtins_value))
    )
    unsupported = []
    for name in _global_reads(fn.__code__):
        if name in LIVE_GLOBALS:
            continue
        if name not in defining and name in builtin_names:
            continue
        unsupported.append(name)
    return sorted(unsupported)


def _default_values_problem(fn: types.FunctionType) -> str | None:
    for index, value in enumerate(fn.__defaults__ or ()):
        problem = _default_problem(value, f"default argument {index + 1}")
        if problem:
            return problem
    for name, value in (fn.__kwdefaults__ or {}).items():
        problem = _default_problem(value, f"the default for {name}")
        if problem:
            return problem
    return None


class _Library:
    """Read-only runtime namespace for tool-managed functions."""

    def __init__(self, scope: Callable[[], dict[str, Any] | None]) -> None:
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_entries", {})
        object.__setattr__(self, "_lock", threading.RLock())

    def _define(self, source: str) -> str:
        node = _definition(source)
        origin = next_script_name()
        try:
            compiled = compile_script(source, origin, defer_annotations=True)
        except SyntaxError as e:
            raise ValueError(f"Syntax error: {e.msg} (line {e.lineno}).") from None
        defining: dict[str, Any] = {
            "__name__": "__binja_mcp__",
            TIMEOUT_CHECK_GLOBAL: lambda: None,
        }
        exec(compiled, defining, defining)
        fn = defining[node.name]
        fn.__annotations__ = {}

        unsupported = _unsupported_globals(fn)
        if unsupported:
            names = ", ".join(repr(name) for name in unsupported)
            raise ValueError(
                f"{node.name!r} is not self-contained: it reads {names}. Move "
                "imports and helpers inside the function, and pass other values "
                "as arguments."
            )
        defaults_problem = _default_values_problem(fn)
        if defaults_problem:
            raise ValueError(
                f"{node.name!r} cannot be saved because {defaults_problem}."
            )

        try:
            signature = str(inspect.signature(fn))
        except (TypeError, ValueError):
            signature = "(...)"
        rec = _Saved(
            code=fn.__code__,
            source=source,
            origin=origin,
            signature=signature,
            doc=fn.__doc__,
            defaults=fn.__defaults__,
            kwdefaults=tuple((fn.__kwdefaults__ or {}).items()),
        )
        publish_source(origin, source)
        with self._lock:
            replaced = node.name in self._entries
            self._entries[node.name] = rec
        verb = "Replaced" if replaced else "Defined"
        return (
            f"{verb} h.lib.{node.name}{signature}. "
            f"Call it from execute as h.lib.{node.name}(...)."
        )

    def _remove(self, name: str) -> str:
        with self._lock:
            if name not in self._entries:
                raise ValueError(
                    f"No library function {name!r}. Saved: {self._names_locked()}."
                )
            del self._entries[name]
        return f"Removed h.lib.{name}."

    def _listing(self) -> str:
        with self._lock:
            entries = tuple(self._entries.items())
        if not entries:
            return "No library functions are defined."
        blocks = []
        for name, rec in entries:
            heading = f"h.lib.{name}{rec.signature}"
            doc = (rec.doc or "").strip().splitlines()
            if doc:
                heading += f" — {doc[0]}"
            blocks.append(f"{heading}\n\n{rec.source.rstrip()}")
        return "\n\n".join(blocks)

    def _names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._entries)

    def _names_locked(self) -> str:
        return ", ".join(self._entries) or "nothing"

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            rec = self._entries.get(key)
        if rec is None:
            raise KeyError(key)
        if rec.origin not in linecache.cache:
            publish_source(rec.origin, rec.source)
        return self._rebind(key, rec)

    def _rebind(self, key: str, rec: _Saved) -> Any:
        """Rebuild the function against the running call's live globals.

        Saved functions are self-contained, so only the current call's live
        globals and builtins are present. Nothing from the defining call is
        retained.
        """
        scope = self._scope() or {}
        merged = {k: scope[k] for k in LIVE_GLOBALS if k in scope}
        if "__builtins__" in scope:
            merged["__builtins__"] = scope["__builtins__"]

        fn = types.FunctionType(rec.code, merged, key, rec.defaults, None)
        fn.__kwdefaults__ = dict(rec.kwdefaults) or None
        fn.__doc__ = rec.doc
        # Through __dict__ because `source` is not an attribute functions
        # declare; reading it back as `h.lib.name.source` works either way.
        fn.__dict__["source"] = rec.source
        return fn

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        try:
            return self[key]
        except KeyError:
            # AttributeError, not the KeyError __getitem__ raises: hasattr()
            # swallows the former and propagates the latter.
            raise AttributeError(
                f"No library function {key!r}. Saved: "
                f"{', '.join(self._names()) or 'nothing'}. "
                "Use the define_lib_function tool to add one."
            ) from None

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("h.lib is read-only in execute. Use define_lib_function.")

    def __delattr__(self, key: str) -> None:
        raise AttributeError("h.lib is read-only in execute. Use remove_lib_function.")

    def __setitem__(self, key: str, value: Any) -> None:
        raise TypeError("h.lib is read-only in execute. Use define_lib_function.")

    def __delitem__(self, key: str) -> None:
        raise TypeError("h.lib is read-only in execute. Use remove_lib_function.")

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    def __iter__(self) -> Any:
        return iter(self._names())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def __repr__(self) -> str:
        with self._lock:
            entries = tuple(self._entries.items())
        if not entries:
            return "h.lib is empty. Use define_lib_function to add a function."
        lines = [f"h.lib — {len(entries)} saved:"]
        for key, rec in entries:
            line = f"  h.lib.{key}{rec.signature}"
            doc = (rec.doc or "").strip().splitlines()
            if doc:
                line += f" — {doc[0]}"
            lines.append(line)
        return "\n".join(lines)


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
                "h.lib cannot be replaced. Use define_lib_function and "
                "remove_lib_function."
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        # Helpers outlives the call: an unguarded `del h.lib` would break every
        # later call until the server restarts, and make `lib` assignable again.
        if name == "lib":
            raise AttributeError(
                "h.lib cannot be deleted. Use remove_lib_function to remove "
                "one function."
            )
        object.__delattr__(self, name)

    def bind_call(self, scope: dict[str, Any], batch: Batch, target: BinaryTab) -> None:
        """Called by the executor before a script runs."""
        self._scope = scope
        self._batch = batch
        self._target = target

    def binaries(self) -> list[dict[str, Any]]:
        """Open binaries, as dicts with stable id, index, name and path."""
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
        if self._target is not None and same_view(tab.bv, self._target.bv):
            raise ValueError(
                f"{tab.name!r} is this call's target — use `bv` to write to it. "
                "h.read_only_view is for the other binary."
            )
        if self._batch is not None and not self._batch.holds(tab.bv):
            self._batch.open_read_only(tab.bv, tab.name)
        return tab.bv

    def __repr__(self) -> str:
        return "<binja mcp helpers: h.binaries(), h.read_only_view(name), h.lib>"


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
        rebase_provider: Callable[[Any, int], Any] | None = None,
    ) -> None:
        self.config = config
        self.session = BinarySession(tabs_provider)
        self.helpers = Helpers(self.session)
        self.executor = CodeExecutor(
            max_output_bytes=config.max_output_bytes,
            timeout=config.execution_timeout_s,
            queue_wait=config.queue_wait_s,
        )
        self._bn = bn_module
        self._rebase_provider = rebase_provider
        self._watcher_factory = (
            watcher_factory
            if watcher_factory is not None
            else make_watcher_factory(bn_module)
        )

    def define_lib_function(self, source: str) -> str:
        result = self.helpers.lib._define(source)
        logger.info(result)
        return result

    def list_lib_functions(self) -> str:
        return self.helpers.lib._listing()

    def remove_lib_function(self, name: str) -> str:
        result = self.helpers.lib._remove(name)
        logger.info(result)
        return result

    def rebase_view(
        self,
        target: Any,
        new_base: int,
        entry_point: int | None = None,
        allow_non_relocatable: bool = False,
    ) -> str:
        """Run Binary Ninja's UI-aware rebase and verify the replacement view."""
        try:
            tab = self.session.resolve(target)
        except LookupError as e:
            logger.warning("refused — %s", e)
            raise ValueError(str(e)) from None
        if self._rebase_provider is None:
            raise RuntimeError(
                "UI rebasing is unavailable in this Binary Ninja session."
            )

        with self.executor.exclusive_operation(tab.name):
            before = capture_rebase_state(tab.bv)
            validate_rebase_request(
                before,
                new_base,
                entry_point=entry_point,
                allow_non_relocatable=allow_non_relocatable,
            )
            if not before.relocatable:
                logger.warning(
                    "%s is non-relocatable with %d relocation ranges; "
                    "embedded absolute values will not be adjusted",
                    tab.name,
                    before.relocation_count,
                )
            backup_path = rebase_backup_path(str(tab.bv.file.filename))
            logger.info("backing up %s to %s", tab.name, backup_path)
            if not tab.bv.create_database(str(backup_path)):
                raise RuntimeError(
                    f"Binary Ninja failed to create pre-rebase backup {backup_path}."
                )
            logger.info("rebasing %s to %#x", tab.name, new_base)
            replacement = self._rebase_provider(tab.bv, new_base)

            if entry_point is not None:
                replacement.add_entry_point(entry_point)
                replacement.update_analysis()

            after = capture_rebase_state(replacement)
            problems, notes = verify_rebase(
                before,
                after,
                new_base,
                requested_entry_point=entry_point,
            )
            if problems:
                detail = "\n- ".join(problems)
                logger.error("rebase verification failed — %s", detail)
                raise RuntimeError(
                    "Rebase verification failed:\n- "
                    f"{detail}\nThe replacement view remains open and may already "
                    f"be saved; verification failure does not roll back a UI "
                    f"rebase. Recover from {backup_path}."
                )

            result = format_rebase_result(
                tab.name,
                before,
                after,
                requested_entry_point=entry_point,
                backup_path=backup_path,
                notes=notes,
            )
            logger.info("rebase verified: %#x -> %#x", before.start, after.start)
            return result

    def execute(
        self,
        code: str,
        target: Any = None,
        description: str | None = None,
        read_only: bool = False,
        output_directory: str | None = None,
        output_extension: str | None = None,
    ) -> ExecutionResult:
        result = self._execute(
            code,
            target,
            description,
            read_only,
            output_directory,
            output_extension,
        )
        # Every result carries the library, including the failures: a script
        # that saved a function and then raised keeps the definition, and the
        # footer is the only place either side can see that. Never at the cost
        # of the result itself — a footer that raised here would turn every
        # later call into an internal error, whether or not it used h.lib.
        try:
            result.lib = self.helpers.lib._names()
        except Exception:
            result.lib = ()
        return result

    def _execute(
        self,
        code: str,
        target: Any,
        description: str | None = None,
        read_only: bool = False,
        output_directory: str | None = None,
        output_extension: str | None = None,
    ) -> ExecutionResult:
        try:
            tab = self.session.resolve(target)
        except LookupError as e:
            logger.warning("refused — %s", e)
            return ExecutionResult(success=False, output="", error=str(e))

        artifact_spec: ArtifactSpec | None = None
        if (output_directory is None) != (output_extension is None):
            error = (
                "`output_directory` and `output_extension` must be provided together."
            )
            logger.warning("refused — %s", error)
            return ExecutionResult(success=False, output="", error=error)
        if output_directory is not None and output_extension is not None:
            try:
                artifact_spec = ArtifactSpec.build(
                    output_directory,
                    output_extension,
                    target_name=tab.name,
                    target_path=tab.path,
                    target_id=self.session.identifier(tab) or "binary",
                )
            except ValueError as e:
                logger.warning("refused — %s", e)
                return ExecutionResult(success=False, output="", error=str(e))

        # The log is where the user watches what is being done to their
        # database, so say what and to which one before it happens rather than
        # only afterwards — a script that never returns would otherwise leave
        # no trace at all.
        said = f" — {description}" if description else ""
        mode = "querying" if read_only else "running"
        logger.info("%s on %s%s", mode, tab.name, said)

        def on_call(scope: dict[str, Any], batch: Batch) -> None:
            self.helpers.bind_call(scope, batch, tab)

        # Nothing here touches the UI. Changes recorded in the undo state
        # propagate to the view on their own, and driving a refresh from this
        # thread pulled Binary Ninja to the foreground mid-session.
        result = self.executor.execute(
            code,
            target=tab.bv,
            target_name=tab.name,
            description=description,
            bn=self._bn,
            helpers=self.helpers,
            on_call=on_call,
            watcher_factory=self._watcher_factory,
            read_only=read_only,
            artifact_spec=artifact_spec,
        )
        if result.artifact_error:
            logger.error("%s", result.artifact_error)
        if result.timed_out:
            verdict = "timed out"
        elif result.success:
            verdict = "ok, rolled back" if read_only else "ok"
        else:
            verdict = "failed" + (", rolled back" if result.reverted else "")
        logger.info("%s in %.1fs", verdict, result.elapsed_s)
        return result

    def running_script(self) -> tuple[str | None, float, bool, bool] | None:
        """A script in flight, for the status indicator to warn about."""
        return self.executor.running_script()

    def wait_for_idle(self) -> None:
        """Wait for execution that may have outlived its timed-out request."""
        self.executor.wait_for_idle()

    def guide(self, topic: str | None) -> str:
        return render(self.status(), topic)

    def status(self) -> dict[str, Any]:
        try:
            tabs = self.session.tabs()
        except LookupError:
            tabs = []

        return {
            "binja_version": self._binja_version(),
            "binaries": [
                _describe_binary(t.bv, t.name, t.path, self.session.identifier(t))
                for t in tabs
            ],
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
            f"{LOG_PREFIX}NOT RUNNING\n\n"
            "Start it from Plugins > Code Mode MCP > Start Server, "
            "or click the indicator in the status bar."
        )

    lines = [
        f"{LOG_PREFIX}RUNNING",
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
        lines.append("Open binaries:")
        lines += [f"  {b.get('id') or b['name']}: {b['name']}" for b in binaries]
    return "\n".join(lines)


def _describe_binary(
    bv: Any, name: str, path: str = "", identifier: str | None = None
) -> dict[str, Any]:
    """Facts the model would otherwise waste a round trip discovering."""

    def safe(fn: Callable[[], Any], default: Any = None) -> Any:
        try:
            return fn()
        except Exception:
            return default

    entry = safe(lambda: bv.entry_point)
    return {
        "id": identifier,
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
