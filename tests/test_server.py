"""HTTP transport, exercised over a real socket.

The details that matter here — status codes, headers, framing — are exactly what
a mocked handler would hide.
"""

import json
import socket
import struct
import threading
import time
import urllib.error
import urllib.request

import pytest

from binja_codemode_mcp.plugin.server import (
    MAX_BODY_BYTES,
    MAX_RESPONSE_BYTES,
    MCPHTTPServer,
    origin_allowed,
)

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
    url = server.start()
    yield url
    server.stop()


def post(
    url: str,
    body: dict[str, object] | None = None,
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
        "origin",
        [
            "https://evil.example",
            "http://127.0.0.1.evil.example",
            "http://localhost@evil.example",
            "http://[::1",  # malformed literal: urlparse raises
        ],
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


class TestKeepAlive:
    """urllib opens a fresh connection per call, so these need a raw socket:
    a rejection that leaves the body unread desyncs a pooled client."""

    @staticmethod
    def _sock(url: str) -> socket.socket:
        host, port = url.split("//")[1].split("/")[0].split(":")
        sk = socket.create_connection((host, int(port)), timeout=5)
        sk.settimeout(5)
        return sk

    @staticmethod
    def _read_response(sk: socket.socket) -> bytes:
        """Read exactly one HTTP response: headers, then Content-Length bytes."""
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sk.recv(4096)
            if not chunk:
                return buf
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        while len(rest) < length:
            chunk = sk.recv(4096)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest

    @staticmethod
    def _request(path: str, body: bytes, key: str | None = API_KEY) -> bytes:
        auth = f"Authorization: Bearer {key}\r\n" if key else ""
        return (
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{auth}"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        ).encode() + body

    def test_connection_is_reusable_after_a_401(self, endpoint):
        """A stale token must not poison the next request on the same socket."""
        good = json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}).encode()
        with self._sock(endpoint) as sk:
            sk.sendall(self._request("/mcp", good, key="wrong"))
            first = self._read_response(sk)
            assert b"401" in first.split(b"\r\n")[0]

            sk.sendall(self._request("/mcp", good))
            second = self._read_response(sk)
        assert b"200" in second.split(b"\r\n")[0], second[:120]
        assert b'"id": 5' in second or b'"id":5' in second

    def test_an_oversized_body_cannot_smuggle_a_pipelined_request(self, endpoint):
        # An oversized Content-Length with only a token body: the server reacts
        # to the header, so this is both the realistic shape and the one that
        # does not depend on being able to finish writing 8 MB.
        head = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {API_KEY}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {MAX_BODY_BYTES + 10}\r\n\r\n"
        ).encode()
        seen = b""
        with self._sock(endpoint) as sk:
            try:
                sk.sendall(head + b"x" * 100)
                # The body was refused unread, so the connection must close
                # rather than keep bytes that would be parsed as a new request.
                sk.sendall(b"GET /pwn HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
                while chunk := sk.recv(4096):
                    seen += chunk
            except (TimeoutError, OSError):
                pass
        assert b"413" in seen
        assert b"405" not in seen, "the smuggled GET was answered"

    def test_a_get_with_a_body_cannot_smuggle_a_request(self, endpoint):
        """do_GET answered 405 without touching the body, so a POST hidden in
        it was read as the next request and dispatched — past the method check
        that had just refused it."""
        smuggled = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {API_KEY}\r\n"
            f"Content-Type: application/json\r\nContent-Length: 46\r\n\r\n"
            '{"jsonrpc": "2.0", "id": 666, "method": "ping"}'
        ).encode()
        head = (
            f"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Length: {len(smuggled)}\r\n\r\n"
        ).encode()
        seen = b""
        with self._sock(endpoint) as sk:
            try:
                sk.sendall(head + smuggled)
                sk.settimeout(0.3)  # the connection stays alive; do not wait it out
                while chunk := sk.recv(4096):
                    seen += chunk
            except (TimeoutError, OSError):
                pass
        assert b"405" in seen
        assert b"666" not in seen, "the smuggled POST was dispatched"

    def test_a_chunked_body_cannot_smuggle_a_request(self, endpoint):
        """Chunked is refused, and the body cannot be drained without parsing
        chunk framing — so the connection has to close rather than leave it."""
        smuggled = (
            f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {API_KEY}\r\n"
            f"Content-Type: application/json\r\nContent-Length: 46\r\n\r\n"
            '{"jsonrpc": "2.0", "id": 777, "method": "ping"}'
        ).encode()
        head = (
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        seen = b""
        with self._sock(endpoint) as sk:
            try:
                sk.sendall(head + smuggled)
                while chunk := sk.recv(4096):
                    seen += chunk
            except (TimeoutError, OSError):
                pass
        assert b"777" not in seen, "the smuggled POST was dispatched"

    def test_a_non_ascii_authorization_header_is_refused_not_a_crash(self, endpoint):
        """http.server decodes headers as latin-1 and compare_digest refuses
        non-ASCII str, so this raised TypeError: no reply at all, and a
        traceback into Binary Ninja's log from an unauthenticated request."""
        head = (
            b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer \xc3\xa9vil\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        seen = b""
        with self._sock(endpoint) as sk:
            try:
                sk.sendall(head)
                sk.settimeout(0.3)
                while chunk := sk.recv(4096):
                    seen += chunk
            except (TimeoutError, OSError):
                pass
        assert b"401" in seen, seen[:120]

    def test_negative_content_length_is_rejected_not_hung(self, endpoint):
        """rfile.read(-1) reads to EOF and would hang the worker forever."""
        with self._sock(endpoint) as sk:
            sk.sendall(
                b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer " + API_KEY.encode() + b"\r\n"
                b"Content-Length: -1\r\n\r\n"
            )
            assert b"400" in self._read_response(sk).split(b"\r\n")[0]

    def test_chunked_body_is_refused_rather_than_silently_empty(self, endpoint):
        with self._sock(endpoint) as sk:
            sk.sendall(
                b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Authorization: Bearer " + API_KEY.encode() + b"\r\n"
                b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
            )
            assert b"411" in self._read_response(sk).split(b"\r\n")[0]


class TestDisconnects:
    def test_a_client_hanging_up_prints_no_traceback(self, endpoint, capfd):
        """socketserver's default dumps a stack to stderr, which Binary Ninja
        shows in the Log pane where it reads as a plugin crash."""
        host, port = endpoint.split("//")[1].split("/")[0].split(":")
        sk = socket.create_connection((host, int(port)), timeout=5)
        sk.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sk.close()  # RST mid-request
        time.sleep(0.3)
        assert "Traceback" not in capfd.readouterr().err


class TestResponseBudget:
    """The MCP layer budgets every field it assembles; this is the backstop.
    Clipping serialized JSON would hand a client bytes it cannot parse, so an
    oversized response is refused whole instead."""

    def test_an_oversized_response_is_refused_not_clipped(self):
        class BigHandler:
            def handle(self, message):
                return {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"x": "y" * (MAX_RESPONSE_BYTES + 1000)},
                }

        server = MCPHTTPServer(BigHandler(), host="127.0.0.1", port=0, api_key=API_KEY)
        url = server.start()
        try:
            status, body = post(url, {"jsonrpc": "2.0", "id": 9, "method": "ping"})
        finally:
            server.stop()

        parsed = json.loads(body)  # must be parseable, not a clipped fragment
        assert status == 200
        assert parsed["id"] == 9
        assert parsed["error"]["code"] == -32603

    def test_a_normal_response_passes_through_untouched(self, endpoint):
        status, body = post(endpoint, {"jsonrpc": "2.0", "id": 3, "method": "ping"})
        assert status == 200
        assert json.loads(body)["result"]["echo"]["id"] == 3


class TestLifecycle:
    def test_start_returns_the_url_it_actually_bound(self):
        """With port 0 the OS picks the port, so the requested value is not the
        real one. Callers get the URL from start(); nothing should have to reach
        into the server to find out where it is listening."""
        server = MCPHTTPServer(EchoHandler(), host="127.0.0.1", port=0, api_key=API_KEY)
        url = server.start()
        try:
            assert server.port != 0
            assert url == f"http://127.0.0.1:{server.port}/mcp"
            assert post(url, {"jsonrpc": "2.0", "id": 1})[0] == 200
        finally:
            server.stop()

    def test_stop_returns_promptly(self):
        """With nothing active, draining adds no noticeable delay."""
        server = MCPHTTPServer(EchoHandler(), host="127.0.0.1", port=0, api_key=API_KEY)
        server.start()
        started = time.monotonic()
        server.stop()
        assert time.monotonic() - started < 0.2
        assert not server.running

    def test_idle_keep_alive_connection_does_not_delay_stop(self):
        server = MCPHTTPServer(EchoHandler(), host="127.0.0.1", port=0, api_key=API_KEY)
        url = server.start()
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
        with TestKeepAlive._sock(url) as sk:
            sk.sendall(TestKeepAlive._request("/mcp", body))
            assert b"200" in TestKeepAlive._read_response(sk).split(b"\r\n")[0]

            started = time.monotonic()
            server.stop()
            assert time.monotonic() - started < 0.2

    def test_stop_waits_for_an_accepted_request(self):
        """Restarting before an old handler exits would give it a new executor
        lock and allow overlapping database transactions."""
        entered = threading.Event()
        release = threading.Event()

        class BlockingHandler:
            def handle(self, message):
                entered.set()
                release.wait()
                return {"jsonrpc": "2.0", "id": message["id"], "result": {}}

        server = MCPHTTPServer(
            BlockingHandler(), host="127.0.0.1", port=0, api_key=API_KEY
        )
        url = server.start()
        request = threading.Thread(
            target=lambda: post(url, {"jsonrpc": "2.0", "id": 1})
        )
        request.start()
        assert entered.wait(1)

        stopping = threading.Thread(target=server.stop)
        stopping.start()
        time.sleep(0.05)
        assert stopping.is_alive(), "stop returned while a handler was active"
        assert server.running

        release.set()
        stopping.join(1)
        request.join(1)
        assert not stopping.is_alive()
        assert not request.is_alive()
        assert not server.running


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
