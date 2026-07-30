"""MCP protocol layer: initialize, tools, resources, and error mapping."""

from typing import Any

import pytest

from binja_codemode_mcp.plugin.executor import ExecutionResult
from binja_codemode_mcp.plugin.mcp import (
    INSTRUCTIONS,
    MAX_ERROR_BYTES,
    MAX_MESSAGE_BYTES,
    MAX_RESULT_BYTES,
    MCPHandler,
    _clip_error,
    _clip_head,
    _clip_tail,
)


class FakeBackend:
    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result or ExecutionResult(success=True, output="ok\n")
        self.executed: list[str] = []
        self.targets: list[Any] = []
        self.descriptions: list[Any] = []
        self.guide_topics: list[str | None] = []
        self.defined: list[str] = []
        self.removed: list[str] = []

    def execute(
        self, code: str, target: Any = None, description: Any = None
    ) -> ExecutionResult:
        self.executed.append(code)
        self.targets.append(target)
        self.descriptions.append(description)
        return self.result

    def define_lib_function(self, source: str) -> str:
        self.defined.append(source)
        return "defined"

    def list_lib_functions(self) -> str:
        return "library listing"

    def remove_lib_function(self, name: str) -> str:
        self.removed.append(name)
        return "removed"

    def guide(self, topic: str | None) -> str:
        self.guide_topics.append(topic)
        return f"GUIDE({topic})"

    def status(self) -> dict[str, Any]:
        return {"binary": None, "tabs": []}


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def handler(backend: FakeBackend) -> MCPHandler:
    return MCPHandler(backend)


def call(handler: MCPHandler, method: str, **params: Any) -> dict[str, Any]:
    response = handler.handle(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    assert response is not None
    return response


class TestInitialize:
    def test_advertises_tools_and_resources(self, handler):
        result = call(handler, "initialize")["result"]
        assert result["capabilities"]["tools"]["listChanged"] is True
        assert "resources" in result["capabilities"]

    def test_guidance_uses_the_real_instructions_field(self, handler):
        """Guidance must ride the spec field clients actually read; anywhere
        else it never reaches the model."""
        result = call(handler, "initialize")["result"]
        assert result["instructions"] == INSTRUCTIONS
        assert "binja_guide" in result["instructions"]

    def test_instructions_fit_the_client_truncation_limit(self):
        """Claude Code truncates server instructions at 2 KB."""
        assert len(INSTRUCTIONS.encode()) < 2048


class TestNotifications:
    def test_notification_gets_no_response(self, handler):
        assert (
            handler.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
            is None
        )


class TestTools:
    def test_lists_execution_library_and_guide_tools(self, handler):
        names = [t["name"] for t in call(handler, "tools/list")["result"]["tools"]]
        assert names == [
            "execute",
            "define_lib_function",
            "list_lib_functions",
            "remove_lib_function",
            "binja_guide",
        ]

    def test_tool_descriptions_fit_the_truncation_limit(self, handler):
        for tool in call(handler, "tools/list")["result"]["tools"]:
            assert len(tool["description"].encode()) < 2048, tool["name"]

    def test_execute_passes_code_through_and_returns_output(self, handler, backend):
        result = call(
            handler, "tools/call", name="execute", arguments={"code": "print(1)"}
        )["result"]
        assert backend.executed == ["print(1)"]
        assert result["content"][0]["text"].startswith("ok\n")
        assert result["isError"] is False

    def test_a_timing_footer_follows_the_output(self, handler):
        """Sizing a batch against the timeout is guesswork without a
        throughput signal — but it must not contaminate what the script
        printed, which the model parses."""
        handler.backend.result = ExecutionResult(
            success=True, output="line one\n", elapsed_s=2.5, timeout_s=30.0
        )
        text = call(handler, "tools/call", name="execute", arguments={"code": "x"})[
            "result"
        ]["content"][0]["text"]
        assert text.startswith("line one\n")
        assert text.rstrip().endswith("[2.5s of 30s]")

    def test_the_footer_lists_saved_library_functions(self, handler):
        """`h.lib` is invisible otherwise: the model cannot see what it saved
        two calls ago, and neither can anyone reading the transcript."""
        text = _text(
            handler,
            success=True,
            output="ok\n",
            elapsed_s=1.0,
            timeout_s=30.0,
            lib=("unported", "port_types"),
        )
        assert text.rstrip().endswith("[1.0s of 30s | lib: unported, port_types]")

    def test_no_library_section_when_nothing_is_saved(self, handler):
        text = _text(
            handler, success=True, output="ok\n", elapsed_s=1.0, timeout_s=30.0
        )
        assert text.rstrip().endswith("[1.0s of 30s]")

    def test_a_large_library_cannot_crowd_out_the_result(self, handler):
        text = _text(
            handler,
            success=True,
            output="ok\n",
            elapsed_s=1.0,
            timeout_s=30.0,
            lib=tuple(f"name{i}" for i in range(500)),
        )
        assert len(text.encode()) <= MAX_RESULT_BYTES
        assert "more]" in text
        assert "lib: name0, name1" in text

    def test_one_absurd_name_does_not_empty_the_listing(self, handler):
        """Counting the name before deciding whether it fits returned a bare
        `lib: , +1 more` — a library the model is told about but cannot see."""
        text = _text(
            handler,
            success=True,
            output="ok\n",
            elapsed_s=1.0,
            timeout_s=30.0,
            lib=("z" * 500,),
        )
        assert "lib: zzz" in text

    def test_the_target_reaches_the_backend(self, handler, backend):
        """It decides which database is written to, so it must not be dropped
        silently on the way through the protocol layer."""
        call(
            handler,
            "tools/call",
            name="execute",
            arguments={"code": "pass", "target": "firmware-1.3"},
        )
        assert backend.targets == ["firmware-1.3"]

    def test_the_description_reaches_the_backend(self, handler, backend):
        """It is what the log line says the call is doing, so dropping it
        silently would leave the user watching unlabelled scripts."""
        call(
            handler,
            "tools/call",
            name="execute",
            arguments={"code": "pass", "description": "rename five functions"},
        )
        assert backend.descriptions == ["rename five functions"]

    def test_a_non_string_target_is_rejected_before_running(self, handler, backend):
        result = call(
            handler,
            "tools/call",
            name="execute",
            arguments={"code": "pass", "target": 1},
        )["result"]
        assert result["isError"] is True
        assert backend.executed == []

    def test_execute_reports_failure_as_a_tool_error(self, handler):
        handler.backend.result = ExecutionResult(
            success=False, output="partial\n", error="ValueError: boom"
        )
        result = call(handler, "tools/call", name="execute", arguments={"code": "x"})[
            "result"
        ]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "partial" in text and "ValueError: boom" in text

    def test_silent_script_says_so_rather_than_returning_empty(self, handler):
        handler.backend.result = ExecutionResult(success=True, output="")
        result = call(
            handler, "tools/call", name="execute", arguments={"code": "pass"}
        )["result"]
        assert "no output" in result["content"][0]["text"]

    def test_missing_code_is_rejected(self, handler, backend):
        result = call(handler, "tools/call", name="execute", arguments={})["result"]
        assert result["isError"] is True
        assert backend.executed == []

    def test_guide_forwards_the_topic(self, handler, backend):
        call(handler, "tools/call", name="binja_guide", arguments={"topic": "Types"})
        assert backend.guide_topics == ["Types"]

    def test_define_lib_function_forwards_source(self, handler, backend):
        result = call(
            handler,
            "tools/call",
            name="define_lib_function",
            arguments={"source": "def answer():\n    return 42"},
        )["result"]
        assert backend.defined == ["def answer():\n    return 42"]
        assert result["content"][0]["text"] == "defined"
        assert result["isError"] is False

    def test_define_lib_function_requires_source(self, handler, backend):
        result = call(handler, "tools/call", name="define_lib_function", arguments={})[
            "result"
        ]
        assert result["isError"] is True
        assert backend.defined == []

    def test_list_lib_functions_returns_the_listing(self, handler):
        result = call(handler, "tools/call", name="list_lib_functions", arguments={})[
            "result"
        ]
        assert result["content"][0]["text"] == "library listing"

    def test_remove_lib_function_forwards_name(self, handler, backend):
        result = call(
            handler,
            "tools/call",
            name="remove_lib_function",
            arguments={"name": "answer"},
        )["result"]
        assert backend.removed == ["answer"]
        assert result["content"][0]["text"] == "removed"

    def test_remove_lib_function_requires_name(self, handler, backend):
        result = call(handler, "tools/call", name="remove_lib_function", arguments={})[
            "result"
        ]
        assert result["isError"] is True
        assert backend.removed == []

    def test_library_validation_errors_are_tool_errors(self, handler):
        def reject(_source):
            raise ValueError("not self-contained")

        handler.backend.define_lib_function = reject  # type: ignore[method-assign]
        result = call(
            handler,
            "tools/call",
            name="define_lib_function",
            arguments={"source": "def bad(): pass"},
        )["result"]
        assert result["isError"] is True
        assert "not self-contained" in result["content"][0]["text"]

    def test_unknown_tool_is_a_tool_error_not_a_crash(self, handler):
        result = call(handler, "tools/call", name="nope", arguments={})["result"]
        assert result["isError"] is True


class TestResources:
    def test_lists_guide_and_status(self, handler):
        uris = [
            r["uri"] for r in call(handler, "resources/list")["result"]["resources"]
        ]
        assert uris == ["binja://guide", "binja://status"]

    def test_reads_the_guide(self, handler):
        result = call(handler, "resources/read", uri="binja://guide")["result"]
        assert result["contents"][0]["text"] == "GUIDE(None)"

    def test_unknown_resource_is_an_error(self, handler):
        response = call(handler, "resources/read", uri="binja://nope")
        assert response["error"]["code"] == -32602


class TestMalformedRequests:
    """Shape errors are protocol errors, not internal ones — the old paths
    leaked a Python exception where the spec defines a code."""

    def test_positional_params_are_invalid_params(self, handler):
        response = handler.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x", {}]}
        )
        assert response["error"]["code"] == -32602
        assert "AttributeError" not in response["error"]["message"]

    def test_a_structured_method_is_an_invalid_request(self, handler):
        response = handler.handle({"jsonrpc": "2.0", "id": 1, "method": {"a": 1}})
        assert response["error"]["code"] == -32600

    def test_a_missing_method_is_an_invalid_request(self, handler):
        response = handler.handle({"jsonrpc": "2.0", "id": 1, "params": {}})
        assert response["error"]["code"] == -32600


class TestErrors:
    def test_unknown_method(self, handler):
        assert call(handler, "wat/list")["error"]["code"] == -32601

    def test_backend_exception_becomes_an_internal_error(self, handler):
        def boom(
            code: str, target: Any = None, description: Any = None
        ) -> ExecutionResult:
            raise RuntimeError("backend died")

        handler.backend.execute = boom  # type: ignore[method-assign]
        response = call(handler, "tools/call", name="execute", arguments={"code": "x"})
        assert response["error"]["code"] == -32603
        assert "backend died" in response["error"]["message"]


def _text(handler, **result_kwargs) -> str:
    handler.backend.result = ExecutionResult(**result_kwargs)
    return call(handler, "tools/call", name="execute", arguments={"code": "x"})[
        "result"
    ]["content"][0]["text"]


class TestClipping:
    """The primitives, at limits the call sites do not currently produce —
    a clipper that fails open at a small limit is one constant edit from
    restoring the unbounded responses this exists to prevent."""

    @pytest.mark.parametrize("limit", [0, 1, 20, 44, 88, 200, 4000])
    def test_no_clipper_returns_more_than_its_limit(self, limit):
        big = "x" * 50_000
        err = "ValueError: " + "z" * 20_000 + "\n" + "frame\n" * 2000
        for clipped in (
            _clip_head(big, limit),
            _clip_tail(big, limit),
            _clip_error(err, limit),
        ):
            assert len(clipped.encode()) <= limit

    def test_clippers_are_byte_exact_on_multibyte_text(self):
        assert len(_clip_head("漢" * 5_000, 300).encode()) <= 300
        assert len(_clip_tail("漢" * 5_000, 300).encode()) <= 300


class TestResponseBudget:
    """Everything is serialized to cross HTTP, so the size limit belongs where
    every outbound field converges — not scattered where output happens to be
    produced. Only print() output was ever bounded; a traceback was not."""

    def test_a_huge_traceback_is_trimmed(self, handler):
        text = _text(handler, success=False, output="", error="E" * 200_000)
        assert len(text.encode()) <= MAX_RESULT_BYTES

    def test_the_end_of_a_traceback_survives(self, handler):
        """The last line is the exception and the frame that raised it."""
        error = "ValueError: bad\n" + ("  frame\n" * 5000) + "FINAL LINE"
        text = _text(handler, success=False, output="", error=error)
        assert "FINAL LINE" in text

    def test_the_exception_type_survives_an_enormous_message(self, handler):
        """One line, no frames — pure tail-clipping would return filler and
        lose the type name entirely."""
        text = _text(
            handler, success=False, output="", error="ValueError: " + "z" * 100_000
        )
        assert "ValueError" in text

    def test_output_is_trimmed_from_the_end_not_the_start(self, handler):
        text = _text(handler, success=True, output="FIRST\n" + "x" * 200_000 + "\nLAST")
        assert text.startswith("FIRST")
        assert "LAST" not in text

    def test_the_timing_footer_survives_truncation(self, handler):
        text = _text(
            handler,
            success=False,
            output="o" * 200_000,
            error="e" * 200_000,
            elapsed_s=1.4,
            timeout_s=30.0,
        )
        assert text.rstrip().endswith("[1.4s of 30s]")

    def test_a_script_that_filled_its_output_then_raised_shows_both(self, handler):
        text = _text(
            handler,
            success=False,
            output="PRINTED OUTPUT\n" + "x" * 32_000,
            error="RuntimeError: boom\n" + "f" * 50_000,
        )
        assert "PRINTED OUTPUT" in text
        assert "RuntimeError" in text
        assert len(text.encode()) <= MAX_RESULT_BYTES

    def test_output_gives_up_room_so_the_error_still_fits(self, handler):
        """Without reserving MAX_ERROR_BYTES out of the allowance, an output
        that nearly fills the budget plus an error overruns it."""
        text = _text(
            handler,
            success=False,
            output="o" * 39_000,
            error="RuntimeError: boom\n" + "E" * 100_000,
        )
        assert len(text.encode()) <= MAX_RESULT_BYTES
        assert "RuntimeError" in text

    def test_the_budget_counts_bytes_not_characters(self, handler):
        """30_000 CJK characters is 90_000 bytes; a character-based check
        would wave it through against a 40_000-byte cap."""
        text = _text(handler, success=True, output="漢" * 30_000)
        assert len(text.encode()) <= MAX_RESULT_BYTES

    def test_a_result_that_fits_is_not_annotated(self, handler):
        """Stops a helper appending a notice unconditionally."""
        text = _text(handler, success=True, output="small\n")
        assert "truncated" not in text

    def test_a_failure_says_the_batch_was_rolled_back(self, handler):
        text = _text(handler, success=False, output="", error="boom", reverted=True)
        assert "Rolled back" in text

    def test_an_oversized_guide_result_is_bounded(self, handler):
        handler.backend.guide = lambda topic: "G" * 200_000  # type: ignore[method-assign]
        result = call(handler, "tools/call", name="binja_guide", arguments={})["result"]
        assert len(result["content"][0]["text"].encode()) <= MAX_RESULT_BYTES

    def test_an_internal_error_message_is_bounded(self, handler):
        def boom(code: str, target: Any = None, description: Any = None):
            raise RuntimeError("x" * 200_000)

        handler.backend.execute = boom  # type: ignore[method-assign]
        response = call(handler, "tools/call", name="execute", arguments={"code": "x"})
        assert len(response["error"]["message"].encode()) <= MAX_MESSAGE_BYTES

    def test_an_unknown_tool_name_is_not_echoed_whole(self, handler):
        result = call(handler, "tools/call", name="n" * 200_000, arguments={})["result"]
        assert len(result["content"][0]["text"].encode()) <= MAX_MESSAGE_BYTES

    def test_an_unknown_resource_uri_is_not_echoed_whole(self, handler):
        response = call(handler, "resources/read", uri="u" * 200_000)
        assert len(response["error"]["message"].encode()) <= MAX_MESSAGE_BYTES

    def test_the_reserved_error_room_leaves_output_its_promised_cap(self):
        """The invariant recorded in config.py: if these overlap, a normal
        result gets truncated twice with two notices."""
        from binja_codemode_mcp.config import Config

        assert Config(api_key="k").max_output_bytes + MAX_ERROR_BYTES < MAX_RESULT_BYTES
