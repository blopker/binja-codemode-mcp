"""Runs LLM-submitted Python against a live BinaryView.

Pure module: `bv` is duck-typed, so this is testable without Binary Ninja.
"""

import threading
import time
import traceback
from dataclasses import dataclass
from io import StringIO
from typing import Any


@dataclass
class ExecutionResult:
    success: bool
    output: str
    error: str | None = None
    timed_out: bool = False


class NoBinaryError(RuntimeError):
    """Raised when no BinaryView is selected."""


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
        max_output_bytes: int = 100_000,
        timeout: float = 30.0,
    ) -> None:
        self.max_output_bytes = max_output_bytes
        self.timeout = timeout

    def execute(
        self,
        code: str,
        *,
        bv: Any,
        bn: Any = None,
        helpers: Any = None,
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

        stdout = StringIO()

        def captured_print(*args: Any, **kwargs: Any) -> None:
            """print() is the result channel; output must be verbatim so the
            model can parse it."""
            kwargs.setdefault("file", stdout)
            print(*args, **kwargs)

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

        state: dict[str, str | None] = {"error": None}
        start = time.time()

        def run() -> None:
            try:
                # One transaction per call: an exception inside reverts every
                # change made in the script, so a tool call is atomic and the
                # user can undo an LLM batch as a single step. It also keeps
                # Binary Ninja's modified-tracking correct, so edits survive a
                # save.
                with bv.undoable_transaction():
                    exec(compiled, scope, scope)
            except Exception as e:
                state["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

        thread = threading.Thread(target=run, daemon=True, name="binja-mcp-exec")
        thread.start()
        thread.join(timeout=self.timeout)

        if thread.is_alive():
            # The thread cannot be killed; it is abandoned with its transaction
            # still open. Known limitation; see the README.
            elapsed = time.time() - start
            return ExecutionResult(
                success=False,
                output=self._cap(stdout.getvalue()),
                error=(
                    f"Execution timed out after {elapsed:.1f}s "
                    f"(limit {self.timeout}s). Partial output above. "
                    f"Narrow the work: filter before iterating, or process in batches."
                ),
                timed_out=True,
            )

        output = self._cap(stdout.getvalue())
        if state["error"]:
            return ExecutionResult(success=False, output=output, error=state["error"])
        return ExecutionResult(success=True, output=output)

    def _cap(self, output: str) -> str:
        if len(output) <= self.max_output_bytes:
            return output
        return (
            output[: self.max_output_bytes]
            + f"\n... (truncated at {self.max_output_bytes} bytes; "
            "filter or paginate before printing)"
        )
