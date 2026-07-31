"""Runs LLM-submitted Python against live BinaryViews.

Pure module: views are duck-typed, so this is testable without Binary Ninja.
"""

import __future__

import ast
import contextlib
import itertools
import linecache
import logging
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

from .artifact import WRITE_CHARS, ArtifactSink, ArtifactSpec
from .session import same_view

# Scripts are compiled under a unique pseudo-filename per call, and their text
# is registered where linecache can find it. That buys two things: tracebacks
# quote the line that raised instead of just numbering it, including inside
# functions retained by h.lib.
SCRIPT_PREFIX = "<mcp:"
TIMEOUT_CHECK_GLOBAL = "__binja_mcp_check_timeout__"
# How long an interrupted script is given to unwind and revert before the caller
# stops waiting. Guarded Python raises at the next statement boundary and settles
# immediately, so this only has to cover the unwind. A thread
# still blocked in a C call cannot see the check yet.
INTERRUPT_GRACE_S = 0.1
KEEP_SOURCES = 8  # recent scripts whose text stays available
_call_seq = itertools.count(1)
_source_lock = threading.Lock()
logger = logging.getLogger(__name__)


def next_script_name() -> str:
    """A fresh pseudo-filename to compile a script under."""
    return f"{SCRIPT_PREFIX}{next(_call_seq)}>"


def publish_source(name: str, code: str) -> None:
    """Make a script's text available to linecache, evicting older ones.

    The mtime slot holds None deliberately: it is the sentinel
    `linecache.checkcache()` skips, and traceback rendering calls checkcache on
    every frame — with a real mtime the entry is purged before it is ever read.

    Call this only for a script that is about to run. Publishing on the way in
    would let scripts that never ran — refused because another was still
    running, or rejected for a syntax error — evict the text of the one that
    did, permanently costing it source lines and h.lib's `.source`.
    """
    with _source_lock:
        linecache.cache[name] = (len(code), None, code.splitlines(True), name)
        held = [k for k in linecache.cache if k.startswith(SCRIPT_PREFIX)]
        for stale in held[:-KEEP_SOURCES]:  # insertion order is age order
            linecache.cache.pop(stale, None)


class _Abandoned(BaseException):
    """Raised from a cooperative checkpoint after a call outruns its deadline.

    BaseException, not Exception: a script wrapping its loop in
    `except Exception` would otherwise swallow its own eviction.
    """


class ScriptRejected(ValueError):
    """A known-unsafe operation must use a dedicated MCP tool."""


class ExecutorBusyError(RuntimeError):
    """Another operation still owns the executor's serialization lock."""


class _UnsafeCallCheck(ast.NodeVisitor):
    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "update_analysis_and_wait"
        ):
            raise ScriptRejected(
                "Do not call update_analysis_and_wait() from execute: Binary "
                "Ninja blocks in native code, where the 30-second timeout cannot "
                "interrupt it. Call update_analysis() and inspect results in a "
                "later MCP call."
            )
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "rebase"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "bv"
        ):
            raise ScriptRejected(
                "Do not call bv.rebase() from execute: it replaces the live view "
                "outside this call's undo transaction. Use the rebase_view tool."
            )
        self.generic_visit(node)


class _GuardCheckpoints(ast.NodeTransformer):
    """Add a cooperative timeout check before every Python statement."""

    @staticmethod
    def _check(node: ast.AST) -> ast.Expr:
        check = ast.Expr(
            value=ast.Call(
                func=ast.Name(id=TIMEOUT_CHECK_GLOBAL, ctx=ast.Load()),
                args=[],
                keywords=[],
            )
        )
        return ast.copy_location(check, node)

    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        for field_name, value in ast.iter_fields(node):
            if not value or not isinstance(value, list):
                continue
            if not all(isinstance(item, ast.stmt) for item in value):
                continue
            statements = cast(list[ast.stmt], value)
            guarded: list[ast.stmt] = []
            for index, statement in enumerate(statements):
                if index == 0 and _starts_with_docstring(statements):
                    guarded.append(statement)
                else:
                    guarded.extend((self._check(statement), statement))
            setattr(node, field_name, guarded)
        return node


def _starts_with_docstring(body: list[ast.stmt]) -> bool:
    if not body or not isinstance(body[0], ast.Expr):
        return False
    value = body[0].value
    return isinstance(value, ast.Constant) and isinstance(value.value, str)


def compile_script(code: str, name: str, *, defer_annotations: bool = False) -> Any:
    """Compile MCP source with safe points in loops and functions.

    The check is part of submitted code, so it cannot fire during transaction
    setup or settlement.
    """
    tree = ast.parse(code, name, "exec")
    _UnsafeCallCheck().visit(tree)
    tree = _GuardCheckpoints().visit(tree)
    ast.fix_missing_locations(tree)
    flags = __future__.annotations.compiler_flag if defer_annotations else 0
    return compile(tree, name, "exec", flags=flags)


@dataclass
class ExecutionResult:
    success: bool
    output: str
    error: str | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0
    timeout_s: float = 0.0
    reverted: bool = False
    lib: tuple[str, ...] = field(default_factory=tuple)
    artifact_path: str | None = None
    artifact_status: str | None = None
    artifact_bytes: int = 0
    artifact_error: str | None = None


@dataclass
class _Outcome:
    """Scratch shared with the worker thread.

    Deliberately not an ExecutionResult. On timeout the caller is handed a
    result while this thread is still running, and the thread keeps writing
    here afterwards — copying the fields out at return time is what stops a
    script that finished late from mutating a value the caller already has.
    """

    error: str | None = None
    artifact_error: str | None = None
    reverted: bool = False  # set by the worker, read by the caller
    abandoned: bool = False  # set by the caller, read by the worker
    settling: bool = False  # the script finished; transactions are closing
    settled: bool = False  # the worker closed its transactions and is done
    finalize_lock: threading.Lock = field(default_factory=threading.Lock)


class _LoadedViews:
    """Views created through this call's injected ``bn.load``."""

    def __init__(self) -> None:
        self._views: list[Any] = []
        self._lock = threading.Lock()

    def add(self, view: Any) -> Any:
        with self._lock:
            self._views.append(view)
        return view

    def abort_analysis(self) -> None:
        with self._lock:
            views = list(self._views)
        for view in views:
            with contextlib.suppress(Exception):
                view.abort_analysis()

    def close(self) -> list[str]:
        with self._lock:
            views, self._views = self._views, []
        failures: list[str] = []
        for view in reversed(views):
            try:
                _ = view.view_type
            except Exception:
                continue  # already closed by a `with bn.load(...)` block
            try:
                view.file.close()
            except Exception as e:
                failures.append(f"{type(e).__name__}: {e}")
        return failures


class _BinaryNinjaFacade:
    """The real module, with call-owned and non-analyzing ``load``."""

    def __init__(self, module: Any, loaded: _LoadedViews) -> None:
        self._module = module
        self._loaded = loaded

    def __getattr__(self, name: str) -> Any:
        return getattr(self._module, name)

    def __str__(self) -> str:
        return str(self._module)

    def __repr__(self) -> str:
        return repr(self._module)

    def load(self, *args: Any, **kwargs: Any) -> Any:
        update_analysis = args[1] if len(args) > 1 else kwargs.get("update_analysis")
        if update_analysis is not False:
            raise ValueError(
                "bn.load() inside execute requires update_analysis=False; its "
                "default waits in native code without an enforceable timeout. "
                "The loaded view is closed automatically when the call finishes."
            )
        return self._loaded.add(self._module.load(*args, **kwargs))


class _Budget:
    """Collects output and stops accumulating once the cap is reached.

    Capping at read time is not enough: a script that outran the timeout keeps
    printing into a buffer nobody will ever read, so the bound has to apply on
    the way in.
    """

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._chunks: list[str] = []
        self._size = 0
        self._truncated = False

    def write(self, text: str) -> None:
        """Deliberately lock-free.

        Exactly one script runs at a time, so there is one writer and one
        reader. The reader may see a slightly shorter prefix while the writer
        finishes, which is harmless, and a lock here would tax every print.
        """
        if self._truncated:
            return
        remaining = max(0, self.max_bytes - self._size)

        # UTF-8 uses at least one byte per character. Looking at at most
        # remaining + 1 characters is therefore enough to know whether the
        # whole string fits, without encoding or retaining a huge print.
        encoded = text[: remaining + 1].encode("utf-8", "replace")
        if len(encoded) <= remaining:
            self._chunks.append(text)
            self._size += len(encoded)
            return

        # Decode after the byte slice so a multibyte character crossing the
        # boundary is omitted rather than returning invalid UTF-8.
        clipped = encoded[:remaining].decode("utf-8", "ignore")
        self._chunks.append(clipped)
        self._size += len(clipped.encode("utf-8"))
        self._truncated = True

    def value(self, full_output: bool = False) -> str:
        out = "".join(self._chunks[:])  # snapshot first; the writer may still run
        truncated = self._truncated
        if not truncated:
            return out
        remedy = (
            "full output is in the artifact below"
            if full_output
            else (
                "rerun with output_directory and output_extension for complete "
                "output, or print a smaller slice"
            )
        )
        return out + (f"\n... (truncated at {self.max_bytes} bytes; {remedy})")


class _CapturedOutput:
    """A bounded preview optionally teeing complete output to disk."""

    def __init__(self, preview_bytes: int, artifact: ArtifactSink | None) -> None:
        self.budget = _Budget(preview_bytes)
        self.artifact = artifact
        self._lock = threading.Lock()
        self._active = True

    def write(self, text: str) -> None:
        # A chunk-sized lock lets a timeout stop a huge print promptly.
        for start in range(0, len(text), WRITE_CHARS):
            chunk = text[start : start + WRITE_CHARS]
            with self._lock:
                if not self._active:
                    return
                if self.artifact is not None:
                    self.artifact.write(chunk)
                self.budget.write(chunk)

    def finish(self, success: bool) -> None:
        with self._lock:
            if not self._active:
                return
            try:
                if self.artifact is not None:
                    self.artifact.finish(success)
            finally:
                self._active = False

    def discard(self) -> None:
        with self._lock:
            if not self._active:
                return
            try:
                if self.artifact is not None:
                    self.artifact.discard()
            finally:
                self._active = False

    def value(self) -> str:
        with self._lock:
            return self.budget.value(full_output=self.artifact is not None)

    def artifact_fields(self) -> dict[str, Any]:
        with self._lock:
            if self.artifact is None or self.artifact.status == "discarded":
                return {}
            return {
                "artifact_path": str(self.artifact.path),
                "artifact_status": self.artifact.status,
                "artifact_bytes": self.artifact.bytes_written,
            }


@dataclass
class _Held:
    """One open undo state."""

    view: Any
    state: Any
    name: str
    read_only: bool = False
    written: Callable[[], bool] | None = None
    release: Callable[[], None] | None = None


class Batch:
    """The undo states one call holds open.

    The write target's state opens before the script runs, so a write to it is
    inside a transaction by construction rather than by luck. A read-only view's
    state opens when the script asks for it and always reverts. The write watcher
    improves the error message; correctness does not depend on its incomplete
    notification coverage.
    """

    def __init__(
        self,
        watcher_factory: Callable[[Any], Any] | None = None,
        report_read_only_writes: bool = True,
    ) -> None:
        self._watcher_factory = watcher_factory
        self._report_read_only_writes = report_read_only_writes
        self._held: list[_Held] = []
        self._lock = threading.Lock()
        self.violations: list[str] = []

    def open_target(self, view: Any, name: str, read_only: bool = False) -> None:
        state = view.begin_undo_actions()
        with self._lock:
            self._held.append(
                _Held(view=view, state=state, name=name, read_only=read_only)
            )

    def open_read_only(self, view: Any, name: str) -> None:
        """Open a state that will revert if the script writes through it."""
        written: Callable[[], bool] | None = None
        release: Callable[[], None] | None = None
        if self._report_read_only_writes and self._watcher_factory is not None:
            watcher = self._watcher_factory(view)
            if watcher is not None:
                written, release = watcher
        state = view.begin_undo_actions()
        with self._lock:
            self._held.append(
                _Held(
                    view=view,
                    state=state,
                    name=name,
                    read_only=True,
                    written=written,
                    release=release,
                )
            )

    def holds(self, view: Any) -> bool:
        """By value, never identity.

        Binary Ninja returns a fresh Python wrapper around the same core handle
        on every call, so identity would read a binary this call already holds
        as a new one — and open a second undo state on it, which is the
        interleaving the executor lock exists to prevent.
        """
        with self._lock:
            return any(same_view(h.view, view) for h in self._held)

    def settle(self, revert: bool) -> tuple[bool, str | None]:
        """Close every open state. Returns (anything reverted, first failure).

        Never raises: a settle that throws would escape the worker and skip the
        lock release, wedging every later call, and an exception here has to be
        reported rather than mistaken for success.
        """
        with self._lock:
            held, self._held = self._held, []
        reverted = False
        failure: str | None = None

        for entry in reversed(held):
            if entry.release is not None:
                # A watcher must never decide the outcome.
                with contextlib.suppress(Exception):
                    entry.release()

            wrote = False
            if entry.written is not None:
                try:
                    wrote = bool(entry.written())
                except Exception:
                    wrote = True  # assume the worst and undo it
                if wrote:
                    self.violations.append(entry.name)

            # The target follows the caller's verdict. A read-only view always
            # reverts, so writes outside the watcher's notification coverage
            # cannot leak into another database.
            undo_this = True if entry.read_only else revert
            try:
                if undo_this:
                    entry.view.revert_undo_actions(entry.state)
                    reverted = True
                else:
                    entry.view.commit_undo_actions(entry.state)
            except BaseException as e:
                verb = "revert" if undo_this else "commit"
                if failure is None:
                    failure = (
                        f"Failed to {verb} the undo transaction on {entry.name}: "
                        f"{type(e).__name__}: {e}. The database may be in an "
                        "inconsistent state; check it before continuing."
                    )
        return reverted, failure


class CodeExecutor:
    """Executes a script against one write target and captures its output.

    There is deliberately no sandbox: the submitted code gets real builtins and
    real imports. An AST filter cannot contain it anyway — CPython injects the
    real builtins module whenever `globals` has no `__builtins__` key, so `open`
    and `__import__` stay reachable however the filter is written — while it does
    reliably block legitimate stdlib use. The undo transaction is the safety net.
    """

    def __init__(
        self,
        max_output_bytes: int = 32_000,
        timeout: float = 30.0,
        queue_wait: float = 5.0,
    ) -> None:
        self.max_output_bytes = max_output_bytes
        self.timeout = timeout
        self.queue_wait = queue_wait
        # One script at a time. Two open undo states on one database interleave:
        # reverting the inner one rewinds whatever the outer one recorded after
        # it, so a failing script can silently roll back a successful one.
        self._busy = threading.Lock()
        self._started_at: float | None = None
        self._running_target: str | None = None
        self._timed_out = False
        self._idle = threading.Event()
        self._idle.set()

    @contextlib.contextmanager
    def exclusive_operation(self, target_name: str) -> Any:
        """Serialize a non-script operation against scripts and settlement."""
        if not self._busy.acquire(timeout=self.queue_wait):
            raise ExecutorBusyError(self._busy_message())
        self._started_at = time.time()
        self._running_target = target_name
        self._timed_out = False
        self._idle.clear()
        try:
            yield
        finally:
            self._started_at = None
            self._running_target = None
            self._timed_out = False
            self._idle.set()
            self._busy.release()

    def _busy_message(self) -> str:
        started_at = self._started_at
        running_for = time.time() - started_at if started_at else 0.0
        on = self._running_target
        whose = f" on {on}" if on else ""
        if self._timed_out:
            state = "timed out and is still inside a native call"
        else:
            state = "is still running"
        return (
            f"Waited {self.queue_wait:.0f}s, but a previous operation {state}"
            f"{whose} ({running_for:.0f}s so far). All calls serialize until its "
            "resources and transactions close; wait, or restart Binary Ninja if "
            "it is wedged."
        )

    def execute(
        self,
        code: str,
        *,
        target: Any,
        target_name: str = "the target",
        bn: Any = None,
        helpers: Any = None,
        extra: dict[str, Any] | None = None,
        on_call: Callable[[dict[str, Any], Batch], None] | None = None,
        watcher_factory: Callable[[Any], Any] | None = None,
        read_only: bool = False,
        artifact_spec: ArtifactSpec | None = None,
    ) -> ExecutionResult:
        if target is None:
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    "No binary to work on. Open a file in Binary Ninja, or call "
                    "h.binaries() to list what is open and pass one as `target`."
                ),
            )

        script_name = next_script_name()
        try:
            compiled = compile_script(code, script_name)
        except SyntaxError as e:
            return ExecutionResult(success=False, output="", error=f"Syntax error: {e}")
        except ScriptRejected as e:
            return ExecutionResult(success=False, output="", error=str(e))

        # Queue rather than refuse. Clients issue tool calls in parallel and the
        # ordinary script finishes in well under a second, so an instant refusal
        # turned a collision that would have resolved itself into a failure the
        # model had to understand and retry.
        if not self._busy.acquire(timeout=self.queue_wait):
            return ExecutionResult(
                success=False,
                output="",
                error=self._busy_message(),
            )

        # Everything from here to the worker's `finally` must be exception-safe:
        # anything that escapes leaves the lock held and every later call
        # refused for the life of the process.
        started = time.time()
        outcome = _Outcome()
        loaded_views = _LoadedViews()
        artifact: ArtifactSink | None = None
        output: _CapturedOutput | None = None
        batch = Batch(
            watcher_factory,
            report_read_only_writes=not read_only,
        )
        try:
            if artifact_spec is not None:
                artifact = ArtifactSink(artifact_spec)
            output = _CapturedOutput(self.max_output_bytes, artifact)
            publish_source(script_name, code)
            self._started_at = started
            self._running_target = target_name
            self._timed_out = False
            self._idle.clear()

            def captured_print(*args: Any, **kwargs: Any) -> None:
                """print() is the result channel; output must be verbatim so the
                model can parse it."""
                sep = kwargs.get("sep", " ")
                end = kwargs.get("end", "\n")
                # Stream pieces into the bounded collector. Joining first
                # briefly duplicated one enormous argument before it could be
                # clipped.
                for index, arg in enumerate(args):
                    if index:
                        output.write(sep)
                    output.write(str(arg))
                output.write(end)

            def check_timeout() -> None:
                if outcome.abandoned:
                    raise _Abandoned

            # Same dict for globals and locals: with separate dicts, names bound
            # at the top level land in `locals` while nested scopes resolve
            # against `globals`, so functions and comprehensions raise NameError.
            scope: dict[str, Any] = {
                "__name__": "__binja_mcp__",
                "bv": target,
                "bn": _BinaryNinjaFacade(bn, loaded_views) if bn is not None else None,
                "h": helpers,
                "print": captured_print,
                TIMEOUT_CHECK_GLOBAL: check_timeout,
            }
            if extra:
                scope.update(extra)
            if on_call is not None:
                on_call(scope, batch)
        except BaseException as e:  # nothing ran; hand the lock back
            if output is not None:
                with contextlib.suppress(Exception):
                    output.discard()
            self._started_at = None
            self._running_target = None
            self._timed_out = False
            self._idle.set()
            self._busy.release()
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to prepare the call: {type(e).__name__}: {e}",
            )

        def run() -> None:
            executed = False
            try:
                try:
                    batch.open_target(target, target_name, read_only=read_only)
                except BaseException as e:
                    outcome.error = (
                        f"Could not open an undo transaction on {target_name}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return
                try:
                    executed = True
                    exec(compiled, scope, scope)
                except _Abandoned:
                    # A checkpoint raised after the caller marked the script
                    # abandoned. A traceback would only be noise.
                    outcome.error = "Interrupted after exceeding the time limit."
                    revert = True
                except BaseException as e:
                    # BaseException, not Exception: sys.exit() inside a script
                    # reverts the batch just the same, so reporting success for
                    # it would be a lie.
                    outcome.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    revert = True
                else:
                    revert = outcome.abandoned
                # Set before settling, not after: a commit that outruns the
                # deadline cannot be called back, so the caller has to be able
                # to tell "still running, will revert" from "already landing".
                outcome.settling = True
                reverted, failure = batch.settle(revert=revert)
                outcome.reverted = reverted
                if batch.violations:
                    views = ", ".join(sorted(set(batch.violations)))
                    note = (
                        f"Wrote to {views}, which this call opened read-only. "
                        "Those changes were rolled back — make it the `target` "
                        "of its own call to write to it."
                    )
                    outcome.error = (
                        f"{outcome.error}\n\n{note}" if outcome.error else note
                    )
                if failure is not None:
                    outcome.error = (
                        f"{outcome.error}\n\n{failure}" if outcome.error else failure
                    )
            finally:
                cleanup_failures = loaded_views.close()
                if cleanup_failures:
                    note = "Failed to close view(s) created by bn.load(): " + "; ".join(
                        cleanup_failures
                    )
                    logger.error("%s", note)
                    outcome.error = (
                        f"{outcome.error}\n\n{note}" if outcome.error else note
                    )
                # Artifact publication and the settled flag share a lock with
                # timeout finalization, so a response cannot race a successful
                # rename and misreport it as timed out.
                with outcome.finalize_lock:
                    try:
                        if executed:
                            output.finish(
                                success=not outcome.error and not outcome.abandoned
                            )
                        else:
                            output.discard()
                    except BaseException as e:
                        cause = (
                            f"Failed to finalize artifact output: "
                            f"{type(e).__name__}: {e}"
                        )
                        if outcome.error:
                            note = cause
                        elif outcome.reverted:
                            note = (
                                "The script finished and its transaction was rolled "
                                f"back, but {cause[0].lower()}{cause[1:]}"
                            )
                        else:
                            note = (
                                "The script finished and its transaction committed, "
                                f"but {cause[0].lower()}{cause[1:]} Check the database "
                                "before rerunning the script."
                            )
                        outcome.artifact_error = note
                        outcome.error = (
                            f"{outcome.error}\n\n{note}" if outcome.error else note
                        )
                    # Set before releasing the lock: the caller reads `settled`
                    # to decide whether a script that outran the deadline landed.
                    outcome.settled = True
                if outcome.abandoned:
                    logger.info(
                        "timed-out script on %s released its resources after %.1fs",
                        target_name,
                        time.time() - started,
                    )
                self._started_at = None
                self._running_target = None
                self._timed_out = False
                self._idle.set()
                self._busy.release()

        thread = threading.Thread(target=run, daemon=True, name="binja-mcp-exec")
        try:
            thread.start()
        except BaseException as e:
            with contextlib.suppress(Exception):
                output.discard()
            self._started_at = None
            self._running_target = None
            self._timed_out = False
            self._idle.set()
            self._busy.release()
            return ExecutionResult(
                success=False,
                output="",
                error=f"Could not start the execution thread: {type(e).__name__}: {e}",
            )
        thread.join(timeout=self.timeout)

        # `settled`, not `thread.is_alive()`. join() waits for full thread exit,
        # which happens after the transaction closes — so a script that finished
        # just under the deadline but whose commit ran past it would otherwise be
        # reported as discarded while its changes had in fact landed, and the
        # model would re-run it and apply everything twice.
        if not outcome.settled:
            artifact_failure: str | None = None
            with outcome.finalize_lock:
                if not outcome.settled:
                    outcome.abandoned = True
                    self._timed_out = True
                    # Best effort for script-owned views blocked in native
                    # analysis. The next statement checkpoint prevents any
                    # later operation from running after the call returns.
                    loaded_views.abort_analysis()
                    try:
                        output.finish(success=False)
                    except BaseException as e:
                        artifact_failure = (
                            f"Failed to finalize artifact output: "
                            f"{type(e).__name__}: {e}"
                        )
            # A statement checkpoint sees `abandoned` at its next safe point.
            # Give ordinary Python a moment to unwind and revert.
            if not outcome.settling:
                thread.join(timeout=INTERRUPT_GRACE_S)
            elapsed = time.time() - started
            if outcome.settled:
                detail = (
                    "the script was interrupted and everything it changed has "
                    "been rolled back."
                )
            elif outcome.settling:
                # The script itself finished; the database is mid-commit and
                # cannot be called back. Claiming a rollback here would send the
                # model to re-run work that in fact landed.
                detail = (
                    "the script finished but is still closing its transaction, so "
                    "its changes are most likely being applied. Read the database "
                    "back before re-running any of it."
                )
            else:
                detail = (
                    "the script has not reached a cooperative timeout checkpoint, "
                    "so its changes are reverted when it finishes."
                )
            return ExecutionResult(
                success=False,
                output=output.value(),
                elapsed_s=elapsed,
                timeout_s=self.timeout,
                error=(
                    f"Execution timed out after {elapsed:.1f}s (limit "
                    f"{self.timeout}s): {detail} Partial output above. Narrow the "
                    "work: filter before iterating, or process in batches."
                    + (f"\n\n{artifact_failure}" if artifact_failure else "")
                ),
                timed_out=True,
                artifact_error=artifact_failure,
                **output.artifact_fields(),
            )

        captured = output.value()
        elapsed = time.time() - started
        if outcome.error:
            return ExecutionResult(
                success=False,
                output=captured,
                error=outcome.error,
                elapsed_s=elapsed,
                timeout_s=self.timeout,
                reverted=outcome.reverted,
                artifact_error=outcome.artifact_error,
                **output.artifact_fields(),
            )
        return ExecutionResult(
            success=True,
            output=captured,
            elapsed_s=elapsed,
            timeout_s=self.timeout,
            reverted=outcome.reverted,
            **output.artifact_fields(),
        )

    def running_script(self) -> tuple[str | None, float, bool] | None:
        """The script in flight and how long it has run, or None when idle.

        Polled from the Qt main thread by the status indicator, so it must not
        block or take the lock: these are plain attribute reads of immutable
        values, and the worst a torn read costs is a stale label for one tick.
        """
        started = self._started_at
        if started is None:
            return None
        return (
            self._running_target,
            max(0.0, time.time() - started),
            self._timed_out,
        )

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until no script is running. For tests and orderly shutdown."""
        return self._idle.wait(timeout)
