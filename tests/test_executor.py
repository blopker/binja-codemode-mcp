"""Execution contract: scoping, transactions, globals, output, errors."""

import textwrap

import pytest

from binja_codemode_mcp.plugin.executor import CodeExecutor


@pytest.fixture
def run(bv):
    """Execute a dedented snippet against the fake BinaryView."""
    executor = CodeExecutor()
    return lambda src, **kw: executor.execute(textwrap.dedent(src).strip(), bv=bv, **kw)


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
            "bv.rename('late')\ngate.wait(5)", bv=bv, helpers=None, extra={"gate": gate}
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
        executor = CodeExecutor(timeout=0.05)
        executor.execute("gate.wait(5)", bv=bv, extra={"gate": gate})

        second = executor.execute("bv.rename('other')", bv=bv)
        assert not second.success
        assert "still running" in (second.error or "")
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
        result = CodeExecutor().execute("print(1)", bv=None)
        assert not result.success
        assert "No binary selected" in (result.error or "")
        assert "h.select" in (result.error or "")


class TestOutput:
    """print() is the result channel."""

    def test_print_output_is_verbatim(self, run):
        """No timestamp prefix. The model parses this output."""
        assert run("print('hello')").output == "hello\n"

    def test_output_is_truncated_at_the_cap(self, bv):
        executor = CodeExecutor(max_output_bytes=100)
        result = executor.execute("print('x' * 500)", bv=bv)
        assert len(result.output) < 500
        assert "truncated" in result.output

    def test_the_cap_counts_bytes_not_characters(self, bv):
        """A binary full of CJK strings would otherwise return ~4x the cap."""
        executor = CodeExecutor(max_output_bytes=400)
        result = executor.execute("print('\u6f22' * 1000)", bv=bv)
        assert len(result.output.encode()) < 1200

    def test_a_runaway_printer_stops_accumulating(self, bv):
        """The buffer must be bounded at write time, not just at read time, or
        an abandoned thread grows it without limit."""
        import threading

        stop = threading.Event()
        executor = CodeExecutor(max_output_bytes=1000, timeout=0.2)
        result = executor.execute(
            "while not stop.is_set():\n    print('x' * 256)",
            bv=bv,
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
            "print('started')\ngate.wait(5)", bv=bv, extra={"gate": gate}
        )
        assert result.timed_out
        assert not result.success
        assert "started" in result.output
        gate.set()
        executor.wait_for_idle(timeout=5)
