"""Runs LLM-submitted Python against live BinaryViews.

Pure module: views are duck-typed, so this is testable without Binary Ninja.
"""

import contextlib
import itertools
import linecache
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# Scripts are compiled under a unique pseudo-filename per call, and their text
# is registered where linecache can find it. That buys two things: tracebacks
# quote the line that raised instead of just numbering it, and inspect.getsource
# works on functions the script saved into h.lib.
SCRIPT_PREFIX = "<mcp:"
KEEP_SOURCES = 8  # recent scripts whose text stays available
_call_seq = itertools.count(1)


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
    linecache.cache[name] = (len(code), None, code.splitlines(True), name)
    held = [k for k in linecache.cache if k.startswith(SCRIPT_PREFIX)]
    for stale in held[:-KEEP_SOURCES]:  # insertion order is age order
        linecache.cache.pop(stale, None)


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


@dataclass
class _Outcome:
    """Scratch shared with the worker thread.

    Deliberately not an ExecutionResult. On timeout the caller is handed a
    result while this thread is still running, and the thread keeps writing
    here afterwards — copying the fields out at return time is what stops a
    script that finished late from mutating a value the caller already has.
    """

    error: str | None = None
    reverted: bool = False  # set by the worker, read by the caller
    abandoned: bool = False  # set by the caller, read by the worker
    settling: bool = False  # the script finished; transactions are closing
    settled: bool = False  # the worker closed its transactions and is done


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
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            if self._truncated:
                return
            self._size += len(text.encode("utf-8", "replace"))
            self._chunks.append(text)
            if self._size > self.max_bytes:
                self._truncated = True

    def value(self) -> str:
        with self._lock:
            out = "".join(self._chunks)
            truncated = self._truncated
        if not truncated:
            return out
        # Trim on the encoded form so the advertised cap really is in bytes.
        clipped = out.encode("utf-8", "replace")[: self.max_bytes]
        return clipped.decode("utf-8", "ignore") + (
            f"\n... (truncated at {self.max_bytes} bytes; "
            "filter or paginate before printing)"
        )


@dataclass
class _Held:
    """One open undo state."""

    view: Any
    state: Any
    name: str
    written: Callable[[], bool] | None = None  # None for the write target
    release: Callable[[], None] | None = None


class Batch:
    """The undo states one call holds open.

    The write target's state opens before the script runs, so a write to it is
    inside a transaction by construction rather than by luck. A read-only view's
    state opens when the script asks for the view, and how it closes depends on
    whether anything wrote to it: reverted if something did, committed if
    nothing did. An empty commit is silent where an empty revert raises the
    Binary Ninja window, so the ordinary two-database call costs nothing and a
    stray write to the source is undone rather than left unprotected.
    """

    def __init__(self, watcher_factory: Callable[[Any], Any] | None = None) -> None:
        self._watcher_factory = watcher_factory
        self._held: list[_Held] = []
        self._lock = threading.Lock()
        self.violations: list[str] = []

    def open_target(self, view: Any, name: str) -> None:
        state = view.begin_undo_actions()
        with self._lock:
            self._held.append(_Held(view=view, state=state, name=name))

    def open_read_only(self, view: Any, name: str) -> None:
        """Open a state that will revert if the script writes through it."""
        written: Callable[[], bool] | None = None
        release: Callable[[], None] | None = None
        if self._watcher_factory is not None:
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
                    written=written,
                    release=release,
                )
            )

    def holds(self, view: Any) -> bool:
        with self._lock:
            return any(h.view is view for h in self._held)

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

            # The target follows the caller's verdict; a read-only view follows
            # whether anything wrote through it.
            undo_this = revert if entry.written is None else wrote
            try:
                if undo_this:
                    entry.view.revert_undo_actions(entry.state)
                    reverted = True
                else:
                    entry.view.commit_undo_actions(entry.state)
            except Exception as e:
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
        self._idle = threading.Event()
        self._idle.set()

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
            compiled = compile(code, script_name, "exec")
        except SyntaxError as e:
            return ExecutionResult(success=False, output="", error=f"Syntax error: {e}")

        # Queue rather than refuse. Clients issue tool calls in parallel and the
        # ordinary script finishes in well under a second, so an instant refusal
        # turned a collision that would have resolved itself into a failure the
        # model had to understand and retry.
        if not self._busy.acquire(timeout=self.queue_wait):
            started_at = self._started_at
            running_for = time.time() - started_at if started_at else 0.0
            on = self._running_target
            whose = f" on {on}" if on else ""
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    f"Waited {self.queue_wait:.0f}s, but a previous script is still "
                    f"running{whose} ({running_for:.0f}s so far) and cannot be "
                    "interrupted. Its changes will be discarded when it finishes. "
                    "Wait for it, or restart Binary Ninja if it is wedged."
                ),
            )

        # Everything from here to the worker's `finally` must be exception-safe:
        # anything that escapes leaves the lock held and every later call
        # refused for the life of the process.
        started = time.time()
        outcome = _Outcome()
        budget = _Budget(self.max_output_bytes)
        batch = Batch(watcher_factory)
        try:
            publish_source(script_name, code)
            self._started_at = started
            self._running_target = target_name
            self._idle.clear()

            def captured_print(*args: Any, **kwargs: Any) -> None:
                """print() is the result channel; output must be verbatim so the
                model can parse it."""
                sep = kwargs.get("sep", " ")
                end = kwargs.get("end", "\n")
                budget.write(sep.join(str(a) for a in args) + end)

            # Same dict for globals and locals: with separate dicts, names bound
            # at the top level land in `locals` while nested scopes resolve
            # against `globals`, so functions and comprehensions raise NameError.
            scope: dict[str, Any] = {
                "__name__": "__binja_mcp__",
                "bv": target,
                "bn": bn,
                "h": helpers,
                "print": captured_print,
            }
            if extra:
                scope.update(extra)
            if on_call is not None:
                on_call(scope, batch)
        except BaseException as e:  # nothing ran; hand the lock back
            self._started_at = None
            self._running_target = None
            self._idle.set()
            self._busy.release()
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to prepare the call: {type(e).__name__}: {e}",
            )

        def run() -> None:
            try:
                try:
                    batch.open_target(target, target_name)
                except BaseException as e:
                    outcome.error = (
                        f"Could not open an undo transaction on {target_name}: "
                        f"{type(e).__name__}: {e}"
                    )
                    return
                try:
                    exec(compiled, scope, scope)
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
                # Set before releasing the lock: the caller reads `settled` to
                # decide whether a script that outran the deadline still landed.
                outcome.settled = True
                self._started_at = None
                self._running_target = None
                self._idle.set()
                self._busy.release()

        thread = threading.Thread(target=run, daemon=True, name="binja-mcp-exec")
        try:
            thread.start()
        except BaseException as e:
            self._started_at = None
            self._running_target = None
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
            outcome.abandoned = True
            elapsed = time.time() - started
            if outcome.settling:
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
                    "the script cannot be interrupted, so its changes are reverted "
                    "when it finishes."
                )
            return ExecutionResult(
                success=False,
                output=budget.value(),
                elapsed_s=elapsed,
                timeout_s=self.timeout,
                error=(
                    f"Execution timed out after {elapsed:.1f}s (limit "
                    f"{self.timeout}s): {detail} Partial output above. Narrow the "
                    "work: filter before iterating, or process in batches."
                ),
                timed_out=True,
            )

        output = budget.value()
        elapsed = time.time() - started
        if outcome.error:
            return ExecutionResult(
                success=False,
                output=output,
                error=outcome.error,
                elapsed_s=elapsed,
                timeout_s=self.timeout,
                reverted=outcome.reverted,
            )
        return ExecutionResult(
            success=True, output=output, elapsed_s=elapsed, timeout_s=self.timeout
        )

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until no script is running. For tests and orderly shutdown."""
        return self._idle.wait(timeout)
