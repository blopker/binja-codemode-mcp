"""Runs LLM-submitted Python against a live BinaryView.

Pure module: `bv` is duck-typed, so this is testable without Binary Ninja.
"""

import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class ExecutionResult:
    success: bool
    output: str
    error: str | None = None
    timed_out: bool = False
    elapsed_s: float = 0.0
    timeout_s: float = 0.0


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


def _unmodified(bv: Any) -> bool:
    """True only when the file is known-clean; False if unreadable."""
    try:
        return not bv.file.modified
    except Exception:
        return False


class CodeExecutor:
    """Executes a script in one undo transaction and captures its output.

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
    ) -> None:
        self.max_output_bytes = max_output_bytes
        self.timeout = timeout
        # One script at a time. Two open undo states on one database interleave:
        # reverting the inner one rewinds whatever the outer one recorded after
        # it, so a failing script can silently roll back a successful one.
        self._busy = threading.Lock()
        self._started_at: float | None = None
        self._idle = threading.Event()
        self._idle.set()

    def execute(
        self,
        code: str,
        *,
        bv: Any,
        bn: Any = None,
        helpers: Any = None,
        extra: dict[str, Any] | None = None,
        on_scope: Callable[[dict[str, Any]], None] | None = None,
    ) -> ExecutionResult:
        if bv is None:
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    "No binary selected. Open a file in Binary Ninja, or call "
                    "h.binaries() to list open tabs and h.select(<index>) to pick one."
                ),
            )

        try:
            compiled = compile(code, "<mcp>", "exec")
        except SyntaxError as e:
            return ExecutionResult(success=False, output="", error=f"Syntax error: {e}")

        if not self._busy.acquire(blocking=False):
            running_for = time.time() - (self._started_at or time.time())
            return ExecutionResult(
                success=False,
                output="",
                error=(
                    f"A previous script is still running ({running_for:.0f}s so far) "
                    "and cannot be interrupted. Its changes will be discarded when it "
                    "finishes. Wait for it, or restart Binary Ninja if it is wedged."
                ),
            )

        started = time.time()
        self._started_at = started
        self._idle.clear()
        budget = _Budget(self.max_output_bytes)

        def captured_print(*args: Any, **kwargs: Any) -> None:
            """print() is the result channel; output must be verbatim so the
            model can parse it."""
            sep = kwargs.get("sep", " ")
            end = kwargs.get("end", "\n")
            budget.write(sep.join(str(a) for a in args) + end)

        # Same dict for globals and locals: with separate dicts, names bound at
        # the top level land in `locals` while nested scopes resolve against
        # `globals`, so functions and comprehensions raise NameError.
        scope: dict[str, Any] = {
            "__name__": "__binja_mcp__",
            "bv": bv,
            "bn": bn,
            "h": helpers,
            "print": captured_print,
        }
        if extra:
            scope.update(extra)
        if on_scope is not None:
            # Lets h.select() rebind `bv` for the rest of the running script,
            # instead of the switch only taking effect on the next call.
            on_scope(scope)

        state: dict[str, Any] = {"error": None, "abandoned": False}
        clean_before = _unmodified(bv)

        def settle(undo: Any, revert: bool) -> None:
            """Close the undo state, skipping calls that provably do nothing.

            Reverting or committing makes the core redraw the view, which is
            disruptive if the script never touched the database — and most
            failures are a typo or an AttributeError on the first line.
            """
            if clean_before and _unmodified(bv):
                # The file was clean going in and is clean now, so this
                # transaction is empty either way.
                return
            if revert:
                bv.revert_undo_actions(undo)
            else:
                bv.commit_undo_actions(undo)

        def run() -> None:
            # The manual undo API rather than `with bv.undoable_transaction()`:
            # a script that outran the timeout has to revert on its way out, and
            # the context manager would commit.
            undo = bv.begin_undo_actions()
            try:
                exec(compiled, scope, scope)
            except BaseException as e:
                # BaseException, not Exception: sys.exit() inside a script
                # reverts the batch just the same, so reporting success for it
                # would be a lie.
                state["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                settle(undo, revert=True)
            else:
                settle(undo, revert=bool(state["abandoned"]))
            finally:
                self._started_at = None
                self._idle.set()
                self._busy.release()

        thread = threading.Thread(target=run, daemon=True, name="binja-mcp-exec")
        thread.start()
        thread.join(timeout=self.timeout)

        if thread.is_alive():
            # The thread cannot be killed. Mark the batch abandoned so it
            # reverts instead of committing after this call has already reported
            # failure, and leave the lock held so nothing overlaps it.
            state["abandoned"] = True
            elapsed = time.time() - (self._started_at or time.time())
            return ExecutionResult(
                success=False,
                output=budget.value(),
                elapsed_s=elapsed,
                error=(
                    f"Execution timed out after {elapsed:.1f}s (limit "
                    f"{self.timeout}s) and was discarded: the script cannot be "
                    "interrupted, so its changes are reverted when it finishes. "
                    "Partial output above. Narrow the work: filter before "
                    "iterating, or process in batches."
                ),
                timed_out=True,
            )

        output = budget.value()
        elapsed = time.time() - started
        if state["error"]:
            return ExecutionResult(
                success=False,
                output=output,
                error=state["error"],
                elapsed_s=elapsed,
                timeout_s=self.timeout,
            )
        return ExecutionResult(
            success=True, output=output, elapsed_s=elapsed, timeout_s=self.timeout
        )

    def wait_for_idle(self, timeout: float | None = None) -> bool:
        """Block until no script is running. For tests and orderly shutdown."""
        return self._idle.wait(timeout)
