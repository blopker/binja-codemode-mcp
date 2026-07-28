"""MCP JSON-RPC dispatch.

Pure module: the backend is injected, so the whole protocol layer is testable
without Binary Ninja or a socket.
"""

import json
from typing import Any, Protocol

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "binja-codemode-mcp"
SERVER_VERSION = "0.2.0"

# Loaded at session start and always in context. Orientation only; the depth
# lives behind binja_guide. Truncated at 2 KB by some clients, so keep it short
# and put what matters first — a test enforces the budget.
INSTRUCTIONS = """\
Drive a live Binary Ninja session by writing Python.

You get the REAL Binary Ninja API, not a wrapper — use what you already know from
api.binary.ninja. Globals inside `execute`: `bv` (the selected BinaryView), `bn` (the
binaryninja module), `h` (this plugin's few helpers). Ordinary Python works: imports,
comprehensions, nested functions.

Call `binja_guide` before your first non-trivial script. It reports the loaded binary,
its architecture and analysis state, the Binary Ninja version to match docs against, the
open tabs, and the API calls that behave surprisingly.

Each `execute` runs in one undo transaction: if the script raises, every change it made
is reverted, so a failed batch leaves no partial state. Each call is independent — no
variables persist between calls. `print()` is the return channel and is capped at
32 KB, so filter before printing. Print addresses as hex.

Only make database changes you are confident in; record ambiguity in a comment instead
of guessing."""

EXECUTE_DESCRIPTION = """\
Run Python against the selected binary in Binary Ninja. Use this for every query and
every edit — reading bytes, decompiling, renaming, applying types, adding comments.

Globals: `bv` (real BinaryView), `bn` (binaryninja module), `h` (helpers:
`h.binaries()`, `h.select(index_or_name)`). Real builtins and imports work.

Return values via `print()`; output is verbatim and capped at 32 KB. Print
addresses as hex. Do NOT iterate every function and decompile — filter to a
handful first, or you will blow the cap and the time limit.

The whole script runs in one undo transaction: an exception reverts every change
it made. Call `bv.update_analysis_and_wait()` after changing a function type or
signature, or subsequent reads see stale analysis.

Nothing persists between calls. Read `binja_guide` first if you have not yet."""

GUIDE_DESCRIPTION = """\
Read this before your first non-trivial script in a session. Returns the live session
state — which binary is loaded, its architecture, analysis status, the Binary Ninja
version, and the open tabs — followed by practical guidance on recovering data formats,
defining types and data variables, applying function prototypes, and the Binary Ninja
calls that behave surprisingly. Pass `topic` to read a single section."""


class Backend(Protocol):
    """What the protocol layer needs from the plugin."""

    def execute(self, code: str) -> Any: ...
    def guide(self, topic: str | None) -> str: ...
    def status(self) -> dict[str, Any]: ...


class MCPError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class MCPHandler:
    """Turns a JSON-RPC message into a response, or None for a notification."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        # Notifications carry no id and take no response.
        if msg_id is None:
            return None

        try:
            result = self._dispatch(method, params)
        except MCPError as e:
            return _error(msg_id, e.code, e.message)
        except Exception as e:  # never take the transport down
            return _error(msg_id, -32603, f"Internal error: {type(e).__name__}: {e}")

        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str | None, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize()
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self._tools()}
        if method == "tools/call":
            return self._call_tool(params)
        if method == "resources/list":
            return {"resources": self._resources()}
        if method == "resources/read":
            return self._read_resource(params)
        if method in ("prompts/list", "resources/templates/list"):
            key = "prompts" if method.startswith("prompts") else "resourceTemplates"
            return {key: []}
        raise MCPError(-32601, f"Method not found: {method}")

    def _initialize(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": True},
                "resources": {},
            },
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # The spec field clients actually read. Guidance placed anywhere
            # else — `_meta`, or a resource the model is told to fetch — never
            # reaches the model: in Claude Code a resource is @-mention only.
            "instructions": INSTRUCTIONS,
        }

    def _tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "execute",
                "description": EXECUTE_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Python to run against `bv`.",
                        },
                        "description": {
                            "type": "string",
                            "description": "One line on what this script does.",
                        },
                    },
                    "required": ["code"],
                },
            },
            {
                "name": "binja_guide",
                "description": GUIDE_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": (
                                "Optional section name, e.g. 'Types', 'Functions', "
                                "'Data variables'. Omit for the whole guide."
                            ),
                        }
                    },
                },
            },
        ]

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments") or {}

        if name == "execute":
            code = args.get("code")
            if not isinstance(code, str) or not code.strip():
                return _tool_error("`code` is required and must be a non-empty string.")
            result = self.backend.execute(code)
            parts: list[str] = []
            if result.output:
                parts.append(result.output)
            if result.error:
                parts.append(f"\nError: {result.error}")
            if not parts:
                parts.append("(no output — the script printed nothing)")
            # A footer, never mixed into the script's own output: batch sizing
            # against the timeout is guesswork without a throughput signal.
            budget = getattr(result, "timeout_s", None)
            elapsed = getattr(result, "elapsed_s", 0.0) or 0.0
            parts.append(
                f"\n\n[{elapsed:.1f}s of {budget:.0f}s]"
                if budget
                else f"\n\n[{elapsed:.1f}s]"
            )
            return {
                "content": [{"type": "text", "text": "".join(parts)}],
                "isError": not result.success,
            }

        if name == "binja_guide":
            topic = args.get("topic")
            text = self.backend.guide(topic if isinstance(topic, str) else None)
            return {"content": [{"type": "text", "text": text}], "isError": False}

        return _tool_error(f"Unknown tool: {name}")

    def _resources(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": "binja://guide",
                "name": "Binary Ninja usage guide",
                "description": "Same content as the binja_guide tool.",
                "mimeType": "text/markdown",
            },
            {
                "uri": "binja://status",
                "name": "Session status",
                "description": "Loaded binary, analysis state, and open tabs.",
                "mimeType": "application/json",
            },
        ]

    def _read_resource(self, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri")
        if uri == "binja://guide":
            return _contents(uri, self.backend.guide(None), "text/markdown")
        if uri == "binja://status":
            body = json.dumps(self.backend.status(), indent=2, default=str)
            return _contents(uri, body, "application/json")
        raise MCPError(-32602, f"Unknown resource: {uri}")


def _contents(uri: str, text: str, mime: str) -> dict[str, Any]:
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
