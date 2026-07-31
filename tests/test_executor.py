"""Execution contract: scoping, transactions, globals, output, errors."""

import textwrap
import time
from pathlib import Path

import pytest

from binja_codemode_mcp.plugin.artifact import ArtifactSpec
from binja_codemode_mcp.plugin.executor import (
    KEEP_SOURCES,
    SCRIPT_PREFIX,
    TIMEOUT_CHECK_GLOBAL,
    Batch,
    CodeExecutor,
    ExecutionResult,
    _Budget,
    compile_script,
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

    def test_no_statement_after_a_late_native_call_runs(self, bv):
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute(
            "gate.wait(5)\nbv.rename('must_not_run')",
            target=bv,
            extra={"gate": gate},
        )
        assert result.timed_out
        gate.set()
        assert executor.wait_for_idle(timeout=3)
        assert bv.renames == []

    def test_a_second_call_is_refused_while_one_is_still_running(self, bv):
        """Two open undo transactions on one database interleave
        unpredictably, so overlap is refused rather than risked."""
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05, queue_wait=0.05)
        executor.execute("gate.wait(5)", target=bv, extra={"gate": gate})

        second = executor.execute("bv.rename('other')", target=bv)
        assert not second.success
        assert "timed out" in (second.error or "")
        assert bv.renames == []

        gate.set()
        executor.wait_for_idle(timeout=5)

    def test_status_distinguishes_a_timed_out_native_call(self, bv):
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute("gate.wait(5)", target=bv, extra={"gate": gate})
        assert result.timed_out
        live = executor.running_script()
        assert live is not None and live[2] is True
        gate.set()
        assert executor.wait_for_idle(timeout=3)

    def test_a_very_late_call_is_marked_stuck_and_analysis_is_aborted(self, bv, caplog):
        import logging
        import threading
        import time

        gate = threading.Event()
        aborted = threading.Event()
        bv.abort_analysis = aborted.set
        executor = CodeExecutor(timeout=0.02, queue_wait=0.01, stuck_after=0.08)
        result = executor.execute(
            "gate.wait(5)",
            target=bv,
            target_name="firmware",
            description="analyze vectors",
            extra={"gate": gate},
        )
        assert result.timed_out
        assert aborted.wait(1), "the second-stage watchdog did not request abort"

        live = executor.running_script()
        assert live is not None and live[2:] == (True, True)
        refused = executor.execute("pass", target=bv)
        assert "analyze vectors" in (refused.error or "")
        assert "may be stuck" in (refused.error or "")
        assert "Restarting Binary Ninja may be required" in (refused.error or "")
        assert any(
            "call — analyze vectors — ran for over" in record.getMessage()
            for record in caplog.get_records("call")
            if record.levelno >= logging.ERROR
        )

        gate.set()
        assert executor.wait_for_idle(timeout=3)
        time.sleep(0.01)
        assert executor.running_script() is None


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
        assert other.reverted == 1


class TestLoadedViewOwnership:
    class File:
        def __init__(self, owner):
            self.owner = owner

        def close(self):
            self.owner.closed = True

    class View:
        def __init__(self):
            import threading

            self._view_type = "Mapped"
            self.closed = False
            self.aborted = False
            self.release = threading.Event()
            self.file = TestLoadedViewOwnership.File(self)

        @property
        def view_type(self):
            if self.closed:
                raise RuntimeError("closed")
            return self._view_type

        def abort_analysis(self):
            self.aborted = True
            self.release.set()

        def update_analysis_and_wait(self):
            self.release.wait(5)

    class BN:
        def __init__(self):
            self.loaded = []

        def load(self, *args, **kwargs):
            view = TestLoadedViewOwnership.View()
            self.loaded.append(view)
            return view

    def test_bn_load_requires_analysis_to_be_disabled(self, bv):
        module = self.BN()
        result = CodeExecutor().execute("bn.load('x')", target=bv, bn=module)
        assert not result.success
        assert "update_analysis=False" in (result.error or "")
        assert module.loaded == []

    def test_loaded_view_is_closed_at_call_end(self, bv):
        module = self.BN()
        result = CodeExecutor().execute(
            "v = bn.load('x', update_analysis=False)\nprint(v.view_type)",
            target=bv,
            bn=module,
        )
        assert result.success
        assert result.output.strip() == "Mapped"
        assert len(module.loaded) == 1 and module.loaded[0].closed

    def test_loaded_view_is_closed_after_an_exception(self, bv):
        module = self.BN()
        result = CodeExecutor().execute(
            "bn.load('x', update_analysis=False)\nraise ValueError('boom')",
            target=bv,
            bn=module,
        )
        assert not result.success
        assert len(module.loaded) == 1 and module.loaded[0].closed

    def test_timeout_aborts_and_closes_loaded_view(self, bv):
        module = self.BN()
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute(
            "v = bn.load('x', update_analysis=False)\n"
            "wait = getattr(v, 'update_analysis_and_wait')\n"
            "wait()\n"
            "bv.rename('must_not_run')",
            target=bv,
            bn=module,
        )
        assert result.timed_out
        assert executor.wait_for_idle(timeout=3)
        assert len(module.loaded) == 1
        assert module.loaded[0].aborted and module.loaded[0].closed
        assert bv.renames == []


class TestInterruption:
    """A script that outruns the deadline used to hold the lock for the life of
    the process, so one `while True:` disabled the plugin until Binary Ninja was
    restarted.

    Best-effort: a C call cannot be stopped while it is running, dynamically
    compiled code is not transformed, and arbitrary Python can deliberately
    evade a cooperative check. Such a script still holds the lock until it
    finishes."""

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

    def test_an_infinite_loop_inside_a_function_is_evicted(self, bv):
        executor = CodeExecutor(timeout=0.2)
        result = executor.execute(
            "def spin():\n    while True:\n        pass\nspin()", target=bv
        )
        assert result.timed_out
        assert executor.wait_for_idle(timeout=3)
        assert bv.reverted == 1

    def test_an_infinite_for_iterator_is_evicted(self, bv):
        executor = CodeExecutor(timeout=0.2)
        result = executor.execute(
            "import itertools\nfor _ in itertools.count():\n    pass", target=bv
        )
        assert result.timed_out
        assert executor.wait_for_idle(timeout=3)
        assert bv.reverted == 1

    def test_timeout_during_transaction_setup_cannot_strand_the_undo_state(self, bv):
        """The old asynchronous exception could land after begin_undo_actions()
        opened a state but before Batch recorded it. The worker then exited with
        an undo transaction that nothing could commit or revert."""
        import threading

        entered = threading.Event()
        release = threading.Event()

        class SlowBegin(type(bv)):
            def begin_undo_actions(self, anonymous_allowed=True):
                state = super().begin_undo_actions(anonymous_allowed)
                entered.set()
                release.wait(5)
                return state

        view = SlowBegin("slow")
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute("bv.rename('never_lands')", target=view)

        assert entered.is_set()
        assert result.timed_out
        assert view.transactions == 1
        assert view.committed == 0 and view.reverted == 0

        release.set()
        assert executor.wait_for_idle(timeout=3)
        assert view.renames == []
        assert view.reverted == 1
        assert view._snapshots == {}


class TestCheckpointCompilation:
    """Every intended safe point receives the live timeout checker."""

    @staticmethod
    def _all_code_names(code):
        import types

        names = set(code.co_names)
        for value in code.co_consts:
            if isinstance(value, types.CodeType):
                names.update(TestCheckpointCompilation._all_code_names(value))
        return names

    @pytest.mark.parametrize(
        "source",
        [
            "while ready:\n    work()",
            "for item in items:\n    work(item)",
            "async def consume():\n    async for item in items:\n        work(item)",
            "def work():\n    return 1",
            "async def work():\n    return 1",
        ],
    )
    def test_safe_point_references_the_timeout_checker(self, source):
        compiled = compile_script(source, f"{SCRIPT_PREFIX}checkpoint>")
        assert TIMEOUT_CHECK_GLOBAL in self._all_code_names(compiled)

    def test_function_checkpoint_preserves_its_docstring(self):
        compiled = compile_script(
            'def documented():\n    """Kept."""\n    return 1',
            f"{SCRIPT_PREFIX}docstring>",
        )
        scope = {TIMEOUT_CHECK_GLOBAL: lambda: None}
        exec(compiled, scope, scope)
        assert scope["documented"].__doc__ == "Kept."

    def test_direct_blocking_analysis_wait_is_rejected(self):
        with pytest.raises(ValueError, match="update_analysis"):
            compile_script(
                "other.update_analysis_and_wait()",
                f"{SCRIPT_PREFIX}analysis-wait>",
            )

    def test_direct_rebase_is_rejected(self):
        with pytest.raises(ValueError, match="rebase_view"):
            compile_script("bv.rebase(0x400000)", f"{SCRIPT_PREFIX}rebase>")


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
        assert "output_directory" in result.output
        assert "output_extension" in result.output

    def test_one_huge_write_retains_only_the_cap(self):
        """The old collector appended the complete string before checking its
        size, so a nominal 32 KB cap could retain and re-encode 500 MB."""
        budget = _Budget(100)
        budget.write("x" * 1_000_000)
        assert sum(len(chunk.encode()) for chunk in budget._chunks) == 100
        assert budget._size == 100
        assert "truncated" in budget.value()

    def test_the_cap_counts_bytes_not_characters(self, bv):
        """A binary full of CJK strings would otherwise return ~4x the cap."""
        executor = CodeExecutor(max_output_bytes=400)
        result = executor.execute("print('\u6f22' * 1000)", target=bv)
        assert len(result.output.encode()) < 1200

    def test_clipping_does_not_split_a_multibyte_character(self):
        budget = _Budget(5)
        budget.write("\u6f22\u6f22")
        content, _, notice = budget.value().partition("\n")
        assert content == "\u6f22"
        assert len(content.encode()) <= 5
        assert "truncated" in notice

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


class TestArtifactOutput:
    def _spec(self, tmp_path):
        return ArtifactSpec.build(
            str(tmp_path),
            "txt",
            target_name="Firmware",
            target_path="/tmp/Firmware.bndb",
            target_id="binary-1",
        )

    def test_complete_output_is_streamed_while_the_preview_stays_bounded(
        self, bv, tmp_path
    ):
        result = CodeExecutor(max_output_bytes=100).execute(
            "print('x' * 10_000)",
            target=bv,
            artifact_spec=self._spec(tmp_path),
        )
        assert result.success
        assert "truncated" in result.output
        assert "full output is in the artifact" in result.output
        assert result.artifact_status == "success"
        path = Path(result.artifact_path or "")
        assert path.read_text() == "x" * 10_000 + "\n"
        assert result.artifact_bytes == 10_001
        assert not list(tmp_path.glob("*.partial"))

    def test_an_exception_publishes_failed_output(self, bv, tmp_path):
        result = CodeExecutor().execute(
            "print('before')\nraise ValueError('boom')",
            target=bv,
            artifact_spec=self._spec(tmp_path),
        )
        assert not result.success
        assert result.artifact_status == "failed"
        assert result.artifact_path is not None
        assert result.artifact_path.endswith(".txt.failed")
        assert (tmp_path / Path(result.artifact_path).name).read_text() == "before\n"

    def test_timeout_closes_the_failed_file_and_discards_later_output(
        self, bv, tmp_path
    ):
        import threading

        gate = threading.Event()
        executor = CodeExecutor(timeout=0.05)
        result = executor.execute(
            "print('before')\ngate.wait(5)\nprint('after')",
            target=bv,
            extra={"gate": gate},
            artifact_spec=self._spec(tmp_path),
        )
        assert result.timed_out
        assert result.artifact_status == "failed"
        path = Path(result.artifact_path or "")
        before = path.read_bytes()
        assert before == b"before\n"
        assert not list(tmp_path.glob("*.partial"))

        gate.set()
        executor.wait_for_idle(timeout=5)
        assert path.read_bytes() == before

    def test_syntax_error_creates_no_artifact(self, bv, tmp_path):
        result = CodeExecutor().execute(
            "def broken(",
            target=bv,
            artifact_spec=self._spec(tmp_path),
        )
        assert not result.success
        assert list(tmp_path.iterdir()) == []


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
