"""HTTP transport, exercised over a real socket.

The details that matter here — status codes, headers, framing — are exactly what
a mocked handler would hide.
"""

import json
import urllib.error
import urllib.request

import pytest

from binja_codemode_mcp.plugin.server import MCPHTTPServer, origin_allowed

API_KEY = "test-key"


class EchoHandler:
    """Stands in for MCPHandler."""

    def handle(self, message):
        if message.get("id") is None:
            return None
        return {"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message}}


@pytest.fixture
def endpoint():
    server = MCPHTTPServer(EchoHandler(), host="127.0.0.1", port=0, api_key=API_KEY)
    server.start()
    # port=0 asked the OS for a free port; read back what it actually bound.
    assert server._server is not None
    port = server._server.server_address[1]
    yield f"http://127.0.0.1:{port}/mcp"
    server.stop()


def post(
    url: str,
    body: dict | None = None,
    *,
    key: str | None = API_KEY,
    origin: str | None = None,
    method: str = "POST",
    raw: bytes | None = None,
) -> tuple[int, bytes]:
    data = raw if raw is not None else json.dumps(body or {}).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if key is not None:
        request.add_header("Authorization", f"Bearer {key}")
    if origin:
        request.add_header("Origin", origin)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestOriginCheck:
    """The spec's DNS-rebinding guard: without it any web page the user visits
    could drive their Binary Ninja session."""

    @pytest.mark.parametrize(
        "origin",
        [None, "http://127.0.0.1:3000", "http://localhost:8080", "http://[::1]:9"],
    )
    def test_local_and_absent_origins_allowed(self, origin):
        assert origin_allowed(origin)

    @pytest.mark.parametrize(
        "origin", ["https://evil.example", "http://127.0.0.1.evil.example"]
    )
    def test_remote_origins_rejected(self, origin):
        assert not origin_allowed(origin)

    def test_server_rejects_a_cross_origin_post(self, endpoint):
        status, _ = post(endpoint, {"id": 1}, origin="https://evil.example")
        assert status == 403


class TestAuth:
    def test_missing_key_rejected(self, endpoint):
        assert post(endpoint, {"id": 1}, key=None)[0] == 401

    def test_wrong_key_rejected(self, endpoint):
        assert post(endpoint, {"id": 1}, key="nope")[0] == 401


class TestRequests:
    def test_request_gets_a_json_rpc_response(self, endpoint):
        status, body = post(endpoint, {"jsonrpc": "2.0", "id": 7, "method": "ping"})
        assert status == 200
        assert json.loads(body)["id"] == 7

    def test_notification_gets_202_and_no_body(self, endpoint):
        status, body = post(endpoint, {"jsonrpc": "2.0", "method": "notifications/x"})
        assert status == 202
        assert body == b""

    def test_malformed_json_gets_a_parse_error(self, endpoint):
        status, body = post(endpoint, raw=b"{not json")
        assert status == 400
        assert json.loads(body)["error"]["code"] == -32700

    def test_batches_are_rejected(self, endpoint):
        """Removed from the spec in 2025-06-18."""
        status, _ = post(endpoint, raw=b'[{"id": 1}]')
        assert status == 400

    def test_wrong_path_is_404(self, endpoint):
        status, _ = post(endpoint.replace("/mcp", "/execute"), {"id": 1})
        assert status == 404


class TestUnsupportedMethods:
    def test_get_is_405(self, endpoint):
        assert post(endpoint, raw=b"", method="GET")[0] == 405

    def test_delete_is_405(self, endpoint):
        assert post(endpoint, raw=b"", method="DELETE")[0] == 405


def test_concurrent_requests_are_not_serialised(endpoint):
    """An execute call can run for the full timeout; it must not block the
    status and guide requests a client makes alongside it."""
    import threading

    results: list[int] = []

    def hit() -> None:
        results.append(post(endpoint, {"jsonrpc": "2.0", "id": 1})[0])

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert results == [200] * 8
