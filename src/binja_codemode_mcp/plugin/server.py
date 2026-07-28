"""MCP Streamable HTTP transport.

Single endpoint, JSON in and JSON out. No SSE: there are no server-initiated
streams, and the MCP spec allows a plain JSON response to a POST.

Pure module: stdlib only, the handler is injected.
"""

import hmac
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

MAX_BODY_BYTES = 8 * 1024 * 1024

# How long shutdown() can block: serve_forever() only notices the stop flag
# between polls. The stdlib default of 0.5s stalls the Qt main thread when the
# user clicks the status-bar button to stop the server.
SHUTDOWN_POLL_S = 0.02


def origin_allowed(origin: str | None) -> bool:
    """Reject cross-origin requests.

    The spec's DNS-rebinding guard for local servers: without it any web page
    the user visits could POST to 127.0.0.1 and drive their RE session.
    """
    if not origin:
        return True  # non-browser client
    try:
        host = urlparse(origin).hostname
    except ValueError:
        return False  # malformed Origin, e.g. a bad IPv6 literal
    return host in ("127.0.0.1", "::1", "localhost")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Injected by MCPHTTPServer before serving.
    mcp: Any
    api_key: str

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Binary Ninja's log is the place for this, not stderr

    def do_POST(self) -> None:
        # Every rejection below has to consume the request body first. On a
        # keep-alive connection an undrained body is read as the start of the
        # next request, so the client's following call gets a nonsense reply —
        # and a body could smuggle a whole pipelined request past the check
        # that just rejected it.
        if urlparse(self.path).path != "/mcp":
            self._reject(404, {"error": "Not found. The MCP endpoint is /mcp."})
            return
        if not origin_allowed(self.headers.get("Origin")):
            self._reject(403, {"error": "Cross-origin requests are not allowed."})
            return
        if not self._authorized():
            self._reject(401, {"error": "Unauthorized."})
            return

        # No chunked support: the body length has to be known up front for the
        # cap to mean anything, and treating a chunked body as empty would turn
        # a real request into a silent no-op.
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._reject(411, {"error": "Chunked encoding is not supported."})
            return

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            self._reject(400, {"error": "Bad Content-Length."})
            return
        # A negative length would make rfile.read() read to EOF and hang the
        # thread until the client disconnects.
        if length < 0:
            self._reject(400, {"error": "Bad Content-Length."})
            return
        if length > MAX_BODY_BYTES:
            self._reject(413, {"error": "Request too large."})
            return

        body = self.rfile.read(length)
        try:
            message = json.loads(body or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, _parse_error())
            return

        if not isinstance(message, dict):
            # Batching was removed from the spec in 2025-06-18. The JSON parsed
            # fine, so this is an invalid request rather than a parse error.
            self._send(400, _invalid_request("Expected a single JSON-RPC object."))
            return

        response = self.mcp.handle(message)
        if response is None:
            self._send_status(202)  # notification: accepted, no body
            return
        self._send(200, response)

    def do_GET(self) -> None:
        # No server-initiated stream to open.
        self._send_status(405, allow="POST")

    def do_DELETE(self) -> None:
        # Stateless: no session to terminate.
        self._send_status(405, allow="POST")

    def _reject(self, status: int, payload: dict[str, Any]) -> None:
        """Reply to a request we are refusing, draining its body first."""
        self._drain()
        self._send(status, payload)

    def _drain(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        remaining = min(max(length, 0), MAX_BODY_BYTES)
        while remaining:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)
        if length > MAX_BODY_BYTES:
            # Too big to drain safely; the connection cannot be reused.
            self.close_connection = True

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return hmac.compare_digest(header, f"Bearer {self.api_key}")

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_status(self, status: int, allow: str | None = None) -> None:
        self.send_response(status)
        if allow:
            self.send_header("Allow", allow)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _parse_error(message: str = "Parse error") -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": message}}


def _invalid_request(message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": message}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client hanging up is normal; do not print a traceback for it.

        socketserver's default prints the stack to stderr, which Binary Ninja
        surfaces in the Log pane where it reads as a plugin crash and buries
        real messages. Anything unexpected still gets logged, once, as a line.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, ConnectionError | BrokenPipeError | TimeoutError):
            return
        print(f"binja-mcp: error handling {client_address}: {exc!r}", file=sys.stderr)


class MCPHTTPServer:
    """Runs the MCP endpoint on a background thread."""

    def __init__(self, mcp: Any, host: str, port: int, api_key: str) -> None:
        self.mcp = mcp
        self.host = host
        self.port = port
        self.api_key = api_key
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        handler = type(
            "BoundHandler", (_Handler,), {"mcp": self.mcp, "api_key": self.api_key}
        )
        # Threading matters: an execute call can run for the full timeout and
        # must not block status or guide requests behind it.
        self._server = _Server((self.host, self.port), handler)
        # Port 0 asks the OS to pick one, and it is only known after binding.
        # Record it so `port` and the returned URL are the real ones.
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            args=(SHUTDOWN_POLL_S,),
            daemon=True,
            name="binja-mcp-http",
        )
        self._thread.start()
        return f"http://{self.host}:{self.port}/mcp"

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        self._server = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None
