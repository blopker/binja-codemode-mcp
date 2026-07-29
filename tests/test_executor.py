"""Execution contract: scoping, transactions, globals, output, errors."""

import textwrap
import time

import pytest

from binja_codemode_mcp.plugin.executor import (
    KEEP_SOURCES,
    SCRIPT_PREFIX,
    Batch,
    CodeExecutor,
    ExecutionResult,
)


@pytest.fixture
def run(bv):
    """Execute a dedented snippet against the fake BinaryView."""
    executor = CodeExecutor()

    def go(src, **kw):
        return executor.execute(textwrap.dedent(src).strip(), target=bv, **kw)

    return go


class TestScoping:
    """Scripts run with one dict for both globals and locals.

    With separate dicts, names bound at the top level land in `locals` while
    nested scopes resolve against `globals`, so any function or comprehension
    that reads a top-level name raises NameError.
    """

    def test_nested_function_sees_top_level_name(self, run):
        result = run("""
            THRESHOLD = 10

            def keep(value):
                return value > THRESHOLD

            print([v for v in (5, 15) if keep(v)])
        """)
        assert result.error is None
        assert "[15]" in result.output

    def test_comprehension_sees_earlier_assignment(self, run):
        result = run("""
            start = 10
            print([v for v in (5, 15) if v >= start])
        """)
        assert result.error is None
        assert "[15]" in result.output


class TestTransactions:
    """One undo transaction per call, so a tool call is atomic."""

    def test_successful_script_commits_once(self, run, bv):
        result = run("bv.rename('parse_header')")
        assert result.success
        assert bv.transactions == 1
        assert bv.committed == 1
        assert bv.renames == ["parse_header"]

    def test_a_failure_reverts_even_when_the_file_looks_unmodified(self, run, bv):
        """bv.file.modified stays False through a rename, so anything that
        gates the revert on it silently stops reverting. Found live: a script
        raised and its rename persisted."""
        assert bv.file.modified is False
        result = run("bv.rename('must_not_persist')\nraise ValueError('boom')")
        assert not result.success
        assert bv.renames == []
        assert bv.reverted == 1
        assert result.reverted is True

    def test_failing_script_reverts_every_change(self, run, bv):
        result = run("""
            bv.rename('first')
            bv.rename('second')
            raise ValueError('boom')
        """)
        assert not result.success
        assert bv.reverted == 1
        assert bv.renames == [], "a failed batch must leave no partial state"

    def test_read_only_script_still_runs_in_a_transaction(self, run, bv):
        run("print(len(bv.functions))")
        assert bv.transactions == 1

    def test_a_failure_after_a_change_still_reverts(self, run, bv):
        result = run("bv.rename('partial')\nraise ValueError('boom')")
        assert not result.success
        assert bv.reverted == 1
        assert bv.renames == []


class TestTimeout:
    """A script that outruns the timeout cannot be killed, so the damage it can
    still do after the call returns has to be bounded."""

    def test_a_batch_that_finishes_late_reverts_instead_of_committing(self, bv):
        """Otherwise the call reports failure and the edits land 30s later,
        on top of whatever ran in between."""
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute(
            "bv.rename('late')\ngate.wait(5)",
            target=bv,
            helpers=None,
            extra={"gate": gate},
        )
        assert result.timed_out
        gate.set()
        executor.wait_for_idle(timeout=5)
        assert bv.renames == [], "the late batch must not land"
        assert bv.reverted == 1
        assert bv.committed == 0

    def test_a_second_call_is_refused_while_one_is_still_running(self, bv):
        """Two open undo transactions on one database interleave
        unpredictably, so overlap is refused rather than risked."""
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05, queue_wait=0.05)
        executor.execute("gate.wait(5)", target=bv, extra={"gate": gate})

        second = executor.execute("bv.rename('other')", target=bv)
        assert not second.success
        assert "still running" in (second.error or "")
        assert bv.renames == []

        gate.set()
        executor.wait_for_idle(timeout=5)


class TestSettleContract:
    """The parts of settling that decide whether a call told the truth."""

    def test_a_late_commit_is_not_reported_as_discarded(self, bv):
        """join() returns only at full thread exit, which is after the commit.
        Reading thread liveness instead of an explicit settled flag reported a
        script whose commit ran past the deadline as discarded — so the model
        re-ran it and applied everything twice."""
        import threading

        slow = threading.Event()

        class SlowCommit(type(bv)):
            def commit_undo_actions(self, state):
                slow.wait(5)
                super().commit_undo_actions(state)

        view = SlowCommit("slow")
        executor = CodeExecutor(timeout=0.2)
        result = executor.execute("bv.rename('landed')", target=view)
        assert result.timed_out, "the commit outran the deadline"
        # A commit in flight cannot be called back, so the report must not
        # promise a rollback that is not going to happen.
        assert "still closing its transaction" in (result.error or "")
        assert "reverted" not in (result.error or "")
        slow.set()
        executor.wait_for_idle(timeout=5)
        assert view.committed == 1, (
            "a batch already committing must not be interrupted mid-close"
        )

    def test_the_timeout_result_carries_the_budget(self, bv):
        """The footer's whole job is sizing the next batch; the timeout is the
        one result where that matters most."""
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.1)
        result = executor.execute("gate.wait(5)", target=bv, extra={"gate": gate})
        assert result.timed_out
        assert result.timeout_s == 0.1
        assert result.elapsed_s > 0
        gate.set()
        executor.wait_for_idle(timeout=5)

    def test_a_failure_while_preparing_hands_the_lock_back(self, bv):
        """Anything between acquiring the lock and the worker's finally is
        unprotected; a leak there refuses every later call for the process."""

        def explode(scope, batch):
            raise RuntimeError("wiring failed")

        executor = CodeExecutor()
        result = executor.execute("pass", target=bv, on_call=explode)
        assert not result.success
        assert "prepare" in (result.error or "")
        assert executor.wait_for_idle(timeout=2)
        assert executor.execute("print('after')", target=bv).output.strip() == "after"

    def test_a_write_detector_that_raises_is_assumed_to_have_written(self, bv):
        """Fail safe: an unreadable detector must undo the view rather than
        wave it through, since the alternative leaves a write unprotected."""

        def broken(view):
            def boom():
                raise RuntimeError("detector died")

            return (boom, lambda: None)

        other = type(bv)("other")
        executor = CodeExecutor()

        def wire(scope, batch):
            batch.open_read_only(other, "other")

        result = executor.execute(
            "pass", target=bv, on_call=wire, watcher_factory=broken
        )
        assert not result.success
        assert "read-only" in (result.error or "")
        assert other.reverted == 1

    def test_a_watcher_release_that_raises_does_not_break_the_verdict(self, bv):
        def leaky(view):
            def release():
                raise RuntimeError("unregister failed")

            return (lambda: False, release)

        other = type(bv)("other")
        executor = CodeExecutor()

        def wire(scope, batch):
            batch.open_read_only(other, "other")

        result = executor.execute(
            "pass", target=bv, on_call=wire, watcher_factory=leaky
        )
        assert result.success
        assert other.committed == 1


class TestInterruption:
    """A script that outruns the deadline used to hold the lock for the life of
    the process, so one `while True:` disabled the plugin until Binary Ninja was
    restarted.

    Best-effort, and deliberately not tested beyond what it delivers: CPython's
    asynchronous exception does not evict a loop whose body contains a
    `try`/`except`, and no amount of re-arming changes that. Such a script still
    holds the lock until it finishes, exactly as before. There is no test for it
    here because the only way to write one is to leave a thread spinning for the
    rest of the session."""

    def test_a_runaway_loop_is_evicted_and_rolled_back(self, bv):
        executor = CodeExecutor(timeout=0.2)
        result = executor.execute(
            "bv.rename('doomed')\nwhile True:\n    pass", target=bv
        )
        assert result.timed_out
        assert "interrupted" in (result.error or "")
        assert executor.wait_for_idle(timeout=3), "the lock was never handed back"
        assert bv.renames == [], "its changes must be rolled back like any failure"
        assert bv.reverted == 1

    def test_the_executor_is_usable_again_afterwards(self, bv):
        executor = CodeExecutor(timeout=0.2)
        executor.execute("while True:\n    pass", target=bv)
        executor.wait_for_idle(timeout=3)
        assert executor.execute("print('recovered')", target=bv).output.strip() == (
            "recovered"
        )


class TestBatchInvariants:
    """Survivors from the mutation review: true of the code, pinned by nothing."""

    def test_a_view_is_matched_by_value_not_identity(self, bv):
        """Binary Ninja hands back a fresh Python wrapper around the same core
        handle on every call, so identity would open a second transaction on a
        binary this call already holds."""
        batch = Batch()
        batch.open_target(bv, "target")
        twin = type(bv)(bv.name)  # a different object for the same binary
        assert batch.holds(twin), "a fresh wrapper must not read as a new binary"

    def test_settling_twice_closes_nothing_twice(self, bv):
        """settle() hands its list off under the lock; without that a second
        call would commit or revert states that are already closed."""
        batch = Batch()
        batch.open_target(bv, "target")
        batch.settle(revert=False)
        batch.settle(revert=False)
        assert bv.committed == 1

    def test_the_write_watcher_ignores_analysis_churn(self, bv):
        """FunctionUpdated is excluded on purpose: analysis fires it unprompted,
        so watching it would fail a call that only read."""
        from binja_codemode_mcp.plugin.backend import _WRITE_NOTIFICATIONS

        assert "FunctionUpdated" not in _WRITE_NOTIFICATIONS
        assert "SymbolUpdated" in _WRITE_NOTIFICATIONS


class TestQueueing:
    """Clients issue tool calls in parallel; a collision that resolves itself
    should not become a failure the model has to reason about."""

    def test_a_brief_collision_waits_and_then_runs(self, bv):
        import threading

        executor = CodeExecutor(queue_wait=5.0)
        results: dict[str, ExecutionResult] = {}

        def first():
            results["first"] = executor.execute(
                "import time\ntime.sleep(0.15)\nbv.rename('first')", target=bv
            )

        def second():
            time.sleep(0.05)  # arrives while the first still holds the lock
            results["second"] = executor.execute("bv.rename('second')", target=bv)

        threads = [threading.Thread(target=f) for f in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        assert results["first"].success
        assert results["second"].success, (
            "the second call should have queued, not been refused"
        )
        assert bv.renames == ["first", "second"]

    def test_a_script_that_outlasts_the_queue_is_refused_with_its_target(self, bv):
        """Past the wait it is genuinely long-running, and naming the target
        lets the model tell whether the conflict even concerned its work."""
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=5.0, queue_wait=0.05)
        threading.Thread(
            target=lambda: executor.execute(
                "gate.wait(3)", target=bv, target_name="ls-a", extra={"gate": gate}
            ),
            daemon=True,
        ).start()
        time.sleep(0.1)

        refused = executor.execute("bv.rename('nope')", target=bv, target_name="ls-b")
        assert not refused.success
        assert "still running on ls-a" in (refused.error or "")
        assert bv.renames == []

        gate.set()
        executor.wait_for_idle(timeout=5)


class TestGlobals:
    """Scripts get the real API and a real Python environment."""

    def test_bv_bn_and_h_are_available(self, run):
        result = run("print(bv is not None, bn, h)", bn="BN", helpers="H")
        assert result.success
        assert result.output == "True BN H\n"

    def test_real_builtins_and_imports_work(self, run):
        result = run("""
            import struct
            print(struct.pack('<I', 1))
            print(sorted({3, 1, 2}))
        """)
        assert result.error is None
        assert "b'\\x01\\x00\\x00\\x00'" in result.output
        assert "[1, 2, 3]" in result.output

    def test_exit_in_a_script_is_reported_as_a_failure(self, run, bv):
        """SystemExit and KeyboardInterrupt are not Exception; the transaction
        reverts either way, so reporting success would be a lie."""
        result = run("bv.rename('gone')\nimport sys\nsys.exit(0)")
        assert not result.success
        assert bv.renames == []
        assert bv.reverted == 1

    def test_no_binary_selected_is_a_clear_error(self):
        result = CodeExecutor().execute("print(1)", target=None)
        assert not result.success
        assert "No binary to work on" in (result.error or "")
        assert "h.binaries()" in (result.error or "")


class TestOutput:
    """print() is the result channel."""

    def test_print_output_is_verbatim(self, run):
        """No timestamp prefix. The model parses this output."""
        assert run("print('hello')").output == "hello\n"

    def test_output_is_truncated_at_the_cap(self, bv):
        executor = CodeExecutor(max_output_bytes=100)
        result = executor.execute("print('x' * 500)", target=bv)
        assert len(result.output) < 500
        assert "truncated" in result.output

    def test_the_cap_counts_bytes_not_characters(self, bv):
        """A binary full of CJK strings would otherwise return ~4x the cap."""
        executor = CodeExecutor(max_output_bytes=400)
        result = executor.execute("print('\u6f22' * 1000)", target=bv)
        assert len(result.output.encode()) < 1200

    def test_a_runaway_printer_stops_accumulating(self, bv):
        """The buffer must be bounded at write time, not just at read time, or
        an abandoned thread grows it without limit."""
        import threading

        stop = threading.Event()
        executor = CodeExecutor(max_output_bytes=1000, timeout=0.2)
        result = executor.execute(
            "while not stop.is_set():\n    print('x' * 256)",
            target=bv,
            extra={"stop": stop},
        )
        assert result.timed_out
        assert len(result.output.encode()) < 20_000
        # Stop the abandoned thread: left spinning it burns a core for the rest
        # of the test session.
        stop.set()
        executor.wait_for_idle(timeout=5)


class TestErrors:
    def test_exception_is_reported_with_partial_output(self, run):
        result = run("""
            print('before')
            raise ValueError('boom')
        """)
        assert not result.success
        assert "before" in result.output
        assert "ValueError: boom" in (result.error or "")

    def test_the_error_field_keeps_the_whole_traceback(self, run, bv):
        """Bounding happens at the transport boundary, not here — this field
        never crosses a wire, and clipping it at production would scatter the
        limit back across the codebase."""
        result = run("raise ValueError('q' * 50_000)")
        assert result.error is not None
        assert len(result.error) > 50_000

    def test_a_reverted_failure_reports_that_it_reverted(self, run, bv):
        result = run("bv.rename('gone')\nraise ValueError('boom')")
        assert result.reverted is True

    def test_every_failure_reports_a_rollback(self, run, bv):
        """There is no reliable way to tell whether a script recorded
        anything, so the transaction is always rolled back and always said
        to be. Vacuous when the script changed nothing; never wrong."""
        assert run("raise ValueError('boom')").reverted is True
        assert run("bv.rename('x')\nraise ValueError('boom')").reverted is True

    def test_a_traceback_shows_the_line_that_raised(self, run):
        """Without the script text registered where linecache can find it, a
        frame reads `File "<mcp:1>", line 2` with no source — a line number
        into code the model has to remember."""
        result = run("x = 1\nraise ValueError('boom')")
        assert "raise ValueError('boom')" in (result.error or "")

    def test_a_refused_script_does_not_evict_the_running_ones_source(self, bv):
        """Overlapping requests are real — the server is threaded. Publishing
        before the busy check let a handful of refusals push the winner's own
        text out of a cache that only holds a few, silently and permanently
        costing it source lines and its h.lib entries' `.source`."""
        import linecache
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05, queue_wait=0.0)
        running = executor.execute("gate.wait(5)", target=bv, extra={"gate": gate})
        assert running.timed_out
        mine = sorted(k for k in linecache.cache if k.startswith(SCRIPT_PREFIX))[-1]

        for i in range(KEEP_SOURCES * 3):
            executor.execute(f"x = {i}", target=bv)

        assert mine in linecache.cache
        gate.set()
        executor.wait_for_idle(timeout=5)

    def test_script_sources_do_not_accumulate(self, bv):
        """They are held for `inspect.getsource` on saved functions; retained
        per call forever they would grow for the life of the process."""
        import linecache

        executor = CodeExecutor()
        for i in range(30):
            executor.execute(f"x = {i}", target=bv)
        held = [k for k in linecache.cache if k.startswith(SCRIPT_PREFIX)]
        assert len(held) <= 10

    def test_syntax_error_is_reported_without_running_anything(self, run, bv):
        result = run("def broken(")
        assert not result.success
        assert "Syntax error" in (result.error or "")
        assert bv.transactions == 0

    def test_timeout_reports_partial_output(self, bv):
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.1)
        result = executor.execute(
            "print('started')\ngate.wait(5)", target=bv, extra={"gate": gate}
        )
        assert result.timed_out
        assert not result.success
        assert "started" in result.output
        gate.set()
        executor.wait_for_idle(timeout=5)
