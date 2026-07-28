"""Whole stack over a real socket: HTTP -> JSON-RPC -> backend -> executor -> bv.

Everything except Binary Ninja itself. This layer asserts on what a client
actually receives, which is the only way to catch a contract that every unit
holds up individually and still fails in composition.
"""

import json
import urllib.request

import pytest

from binja_codemode_mcp.config import Config
from binja_codemode_mcp.plugin.backend import PluginBackend
from binja_codemode_mcp.plugin.mcp import MCPHandler
from binja_codemode_mcp.plugin.server import MCPHTTPServer

KEY = "integration-key"


@pytest.fixture
def client(tmp_path, tabs):
    config = Config(api_key=KEY, data_dir=tmp_path, port=0)
    backend = PluginBackend(config, tabs_provider=lambda: tabs)
    server = MCPHTTPServer(MCPHandler(backend), host=config.host, port=0, api_key=KEY)
    url = server.start()

    counter = {"id": 0}

    def rpc(method: str, **params):
        counter["id"] += 1
        payload = {
            "jsonrpc": "2.0",
            "id": counter["id"],
            "method": method,
            "params": params,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {KEY}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    yield rpc
    server.stop()


def text_of(response):
    return response["result"]["content"][0]["text"]


def test_handshake_delivers_guidance(client):
    """A client sees the instructions at initialize, without having to ask."""
    result = client("initialize")["result"]
    assert result["serverInfo"]["name"] == "binja-codemode-mcp"
    assert "real Binary Ninja API" in result["instructions"].replace("REAL", "real")


def test_full_session(client, bv):
    client("initialize")

    tools = [t["name"] for t in client("tools/list")["result"]["tools"]]
    assert tools == ["execute", "binja_guide"]

    guide = text_of(client("tools/call", name="binja_guide", arguments={}))
    assert "target" in guide
    assert "aarch64" in guide
    assert "## Types" in guide

    read = client(
        "tools/call",
        name="execute",
        arguments={"code": "print(hex(bv.start), len(bv.functions))"},
    )
    assert text_of(read).startswith("0x100000000 3")

    write = client(
        "tools/call",
        name="execute",
        arguments={"code": "bv.rename('parse_header')"},
    )
    assert write["result"]["isError"] is False
    assert bv.renames == ["parse_header"]


def test_failed_script_reverts_and_reports(client, bv):
    client("initialize")
    response = client(
        "tools/call",
        name="execute",
        arguments={"code": "bv.rename('a')\nraise ValueError('boom')"},
    )
    assert response["result"]["isError"] is True
    assert "ValueError: boom" in text_of(response)
    assert bv.renames == [], "the transaction must have reverted the rename"


def test_status_resource_reflects_the_live_session(client):
    client("initialize")
    response = client("resources/read", uri="binja://status")
    status = json.loads(response["result"]["contents"][0]["text"])
    assert status["binary"]["name"] == "target"
    assert status["tabs"][0]["selected"] is True


def test_a_failing_script_with_an_enormous_message_stays_usable(client):
    """The reported bug, end to end: an unbounded error field produced a
    response limited only by RAM."""
    from binja_codemode_mcp.plugin.mcp import MAX_RESULT_BYTES

    client("initialize")
    response = client(
        "tools/call",
        name="execute",
        arguments={"code": "raise ValueError('x' * 500_000)"},
    )
    text = text_of(response)
    assert response["result"]["isError"] is True
    assert "ValueError" in text
    assert len(text.encode()) <= MAX_RESULT_BYTES
