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
        executor = CodeExecutor(timeout=0.1)
        result = executor.execute("import time\nprint('started')\ntime.sleep(5)", bv=bv)
        assert result.timed_out
        assert not result.success
        assert "started" in result.output
