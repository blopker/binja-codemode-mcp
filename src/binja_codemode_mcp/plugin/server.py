"""MCP Streamable HTTP transport.

Single endpoint, JSON in and JSON out. No SSE: there are no server-initiated
streams, and the MCP spec allows a plain JSON response to a POST.

Pure module: stdlib only, the handler is injected.
"""

import json
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
    host = urlparse(origin).hostname
    return host in ("127.0.0.1", "::1", "localhost")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Injected by MCPHTTPServer before serving.
    mcp: Any
    api_key: str

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Binary Ninja's log is the place for this, not stderr

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/mcp":
            self._send(404, {"error": "Not found. The MCP endpoint is /mcp."})
            return
        if not origin_allowed(self.headers.get("Origin")):
            self._send(403, {"error": "Cross-origin requests are not allowed."})
            return
        if not self._authorized():
            self._send(401, {"error": "Unauthorized."})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, {"error": "Bad Content-Length."})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"error": "Request too large."})
            return

        try:
            message = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, _parse_error())
            return

        if not isinstance(message, dict):
            # Batching was removed from the spec in 2025-06-18.
            self._send(400, _parse_error("Expected a single JSON-RPC object."))
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

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.api_key}"

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


class MCPHTTPServer:
    """Runs the MCP endpoint on a background thread."""

    def __init__(self, mcp: Any, host: str, port: int, api_key: str) -> None:
        self.mcp = mcp
        self.host = host
        self.port = port
        self.api_key = api_key
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        handler = type(
            "BoundHandler", (_Handler,), {"mcp": self.mcp, "api_key": self.api_key}
        )
        # Threading matters: an execute call can run for the full timeout and
        # must not block status or guide requests behind it.
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
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
