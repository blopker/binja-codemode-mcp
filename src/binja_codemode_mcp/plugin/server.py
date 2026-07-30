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

from .mcp import PROTOCOL_VERSION

MAX_BODY_BYTES = 8 * 1024 * 1024

# The MCP layer budgets every text field it assembles; this is the backstop for
# anything that gets past it. A response cannot be clipped — trimming serialized
# JSON yields bytes no client can parse — so an oversized one is refused whole.
# If this fires it is a bug in the output budget, not a routine outcome.
MAX_RESPONSE_BYTES = 1024 * 1024

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
    # Without this the socket blocks forever, so a client that opens a
    # connection, promises a body and never sends it pins a handler thread for
    # good — unauthenticated, and with nothing bounding how many. Applies per
    # socket operation, not to the request as a whole, so a script running for
    # the full execution timeout is unaffected: nothing is read or written
    # while it runs.
    timeout = 30

    # Injected by MCPHTTPServer before serving.
    mcp: Any
    api_key: str

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Binary Ninja's log is the place for this, not stderr

    def do_POST(self) -> None:
        server: Any = self.server
        if not server.begin_request():
            self.close_connection = True
            self._send(503, {"error": "MCP server is shutting down."})
            return
        try:
            self._do_POST()
        finally:
            server.end_request()

    def _do_POST(self) -> None:
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
        requested_version = self.headers.get("MCP-Protocol-Version")
        if requested_version is not None and requested_version != PROTOCOL_VERSION:
            self._reject(
                400,
                _invalid_request(
                    f"Unsupported MCP-Protocol-Version: {requested_version!r}."
                ),
            )
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
        # No server-initiated stream to open. Refused like any other request so
        # the body is dealt with: a GET carrying one would otherwise leave it in
        # the stream to be read as the next request.
        self._reject(405, {"error": "Method not allowed. Use POST."}, allow="POST")

    def do_DELETE(self) -> None:
        # Stateless: no session to terminate.
        self._reject(405, {"error": "Method not allowed. Use POST."}, allow="POST")

    def _reject(
        self, status: int, payload: dict[str, Any], allow: str | None = None
    ) -> None:
        """Reply to a request we are refusing, and deal with its body.

        The answer goes out first. Draining first meant an unauthenticated
        client could hold a handler thread for as long as it liked simply by
        promising 8 MB and sending one byte, with the 401 never reaching it.
        """
        self._send(status, payload, allow=allow)
        self._drain()

    def _drain(self) -> None:
        """Consume the request body, or close if it cannot be consumed.

        An undrained body on a keep-alive connection is read as the start of the
        next request, so a body can smuggle a whole pipelined request past the
        check that just rejected this one.
        """
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            # We never parse chunk framing, so there is no safe way to find the
            # end of this body. Closing is the only way to keep the stream sane.
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length > MAX_BODY_BYTES:
            # We refused to read this much; we are not going to discard it
            # either, so the connection cannot continue.
            self.close_connection = True
            return
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
        # Compared as bytes: http.server decodes headers as latin-1, and
        # hmac.compare_digest refuses str operands holding non-ASCII — so a
        # header with a high byte in it raised TypeError, dropped the connection
        # with no reply, and wrote a traceback into Binary Ninja's log, all
        # before authenticating.
        header = self.headers.get("Authorization", "").encode("latin-1", "replace")
        expected = f"Bearer {self.api_key}".encode()
        return hmac.compare_digest(header, expected)

    def _send(
        self, status: int, payload: dict[str, Any], allow: str | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        if len(body) > MAX_RESPONSE_BYTES:
            # Not payload["id"]: it is client-controlled and unbounded, so
            # echoing it into the replacement made the reply that announces the
            # limit larger than the limit. A 3 MB id produced a 3 MB refusal.
            body = json.dumps(
                _response_too_large(_safe_id(payload.get("id")), len(body))
            ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if allow:
            self.send_header("Allow", allow)
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


# An id long enough to matter is not a real correlation id. Anything past this
# is dropped rather than echoed, so the reply announcing the size limit cannot
# itself exceed it.
MAX_ECHOED_ID_CHARS = 128


def _safe_id(msg_id: Any) -> Any:
    """The request id, if it is small enough to echo back safely."""
    if isinstance(msg_id, str) and len(msg_id) > MAX_ECHOED_ID_CHARS:
        return None
    if isinstance(msg_id, (str, int, float)) or msg_id is None:
        return msg_id
    return None  # the spec allows only a string, number or null


def _response_too_large(msg_id: Any, size: int) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {
            "code": -32603,
            "message": (
                f"Response of {size} bytes exceeds the {MAX_RESPONSE_BYTES}-byte "
                "limit and was withheld. This is a bug in the server's output "
                "budget; please report it."
            ),
        },
    }


def _invalid_request(message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": message}}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._requests_idle = threading.Event()
        self._requests_idle.set()
        self._requests_lock = threading.Lock()
        self._active_requests = 0
        self._stopping = False

    def begin_request(self) -> bool:
        """Reserve the backend before shutdown can observe it as idle."""
        with self._requests_lock:
            if self._stopping:
                return False
            self._active_requests += 1
            self._requests_idle.clear()
            return True

    def end_request(self) -> None:
        with self._requests_lock:
            self._active_requests -= 1
            if self._active_requests == 0:
                self._requests_idle.set()

    def begin_shutdown(self) -> None:
        with self._requests_lock:
            self._stopping = True

    def wait_for_requests(self) -> None:
        self._requests_idle.wait()

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
        self._stop_lock = threading.Lock()

    def start(self) -> str:
        if self._server is not None:
            raise RuntimeError("MCP server is already running")
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
        """Stop accepting requests, then wait for every accepted request."""
        with self._stop_lock:
            server = self._server
            thread = self._thread
            if server is None:
                return
            # Existing keep-alive sockets can try another request after the
            # listener closes. Mark first so they are refused rather than
            # reaching a backend the plugin is about to discard.
            server.begin_shutdown()
            server.shutdown()
            server.server_close()
            server.wait_for_requests()
            if thread is not None and thread is not threading.current_thread():
                thread.join()
            self._server = None
            self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None
