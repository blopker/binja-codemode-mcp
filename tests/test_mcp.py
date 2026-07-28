"""MCP protocol layer: initialize, tools, resources, and error mapping."""

from typing import Any

import pytest

from binja_codemode_mcp.plugin.executor import ExecutionResult
from binja_codemode_mcp.plugin.mcp import INSTRUCTIONS, MCPHandler


class FakeBackend:
    def __init__(self, result: ExecutionResult | None = None) -> None:
        self.result = result or ExecutionResult(success=True, output="ok\n")
        self.executed: list[str] = []
        self.guide_topics: list[str | None] = []

    def execute(self, code: str) -> ExecutionResult:
        self.executed.append(code)
        return self.result

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
    def test_lists_execute_and_guide(self, handler):
        names = [t["name"] for t in call(handler, "tools/list")["result"]["tools"]]
        assert names == ["execute", "binja_guide"]

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


class TestErrors:
    def test_unknown_method(self, handler):
        assert call(handler, "wat/list")["error"]["code"] == -32601

    def test_backend_exception_becomes_an_internal_error(self, handler):
        def boom(code: str) -> ExecutionResult:
            raise RuntimeError("backend died")

        handler.backend.execute = boom  # type: ignore[method-assign]
        response = call(handler, "tools/call", name="execute", arguments={"code": "x"})
        assert response["error"]["code"] == -32603
        assert "backend died" in response["error"]["message"]
