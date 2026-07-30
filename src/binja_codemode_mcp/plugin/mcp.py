"""MCP JSON-RPC dispatch.

Pure module: the backend is injected, so the whole protocol layer is testable
without Binary Ninja or a socket.
"""

import json
from typing import Any, Protocol

from ..version import VERSION

# Every text field that leaves this process converges here, which is the last
# layer that knows what each string means: this one is print output and should
# keep its head, that one a traceback and should keep its tail. server.py cannot
# do this job — it sees a finished JSON-RPC dict, and clipping serialized JSON
# produces bytes no client can parse.
MAX_RESULT_BYTES = 40_000  # assembled text of one tool result
MAX_ERROR_BYTES = 4_000  # the Error: section reserved inside it
MAX_MESSAGE_BYTES = 2_000  # a JSON-RPC error message or tool-error string
MAX_FOOTER_LIB_BYTES = 200  # the h.lib name list inside the footer

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "binja-codemode-mcp"
SERVER_VERSION = VERSION
_MISSING = object()

# Loaded at session start. Keep only orientation and safety rules that must be
# visible before the first tool call.
INSTRUCTIONS = """\
Run Python against the real Binary Ninja API with `execute`. Globals are `bv` (the
`target` BinaryView), `bn` (binaryninja), and `h` (helpers). Only `bv` is writable;
set `read_only` for queries. Omit `target` only when one binary is open.

Read `binja_guide` before non-trivial work. Change only what evidence supports;
record uncertainty in comments. Reusable functions are managed with
`define_lib_function`, `list_lib_functions`, and `remove_lib_function`. For complete
large output, pass `output_directory` and `output_extension`."""

EXECUTE_DESCRIPTION = """\
Run Python with `bv` (the selected `target` BinaryView), `bn` (binaryninja), and
`h` (`binaries()`, `read_only_view(name)`, read-only `lib`). Builtins and imports work.

An exception rolls back the call; `read_only=true` always rolls it back. The limit
is 30 seconds. Return data with `print()` (32 KB preview; errors keep 4 KB);
`output_directory` plus `output_extension` streams up to 100 MiB to a generated
file. Filter before decompiling and print addresses as hex. Calls are stateless
except functions managed by the lib tools and called as `h.lib.name()`. Read
`binja_guide` before non-trivial work."""

GUIDE_DESCRIPTION = """\
Return live session details and concise guidance for safe queries and edits."""

DEFINE_LIB_DESCRIPTION = """\
Define or replace one reusable function. `source` must contain one self-contained,
undecorated `def`; put imports and helpers inside it. Call it later from `execute`
as `h.lib.name(...)`."""

LIST_LIB_DESCRIPTION = """\
List every reusable function with its signature, docstring, and source."""

REMOVE_LIB_DESCRIPTION = """\
Remove one reusable function by name."""

REBASE_DESCRIPTION = """\
Rebase an open BinaryView through Binary Ninja's UI and verify the replacement.
Requires a saved BNDB with no unsaved changes and creates a timestamped sibling
backup without overwriting. Optionally add an analysis entry point. It may take as
long as a full analysis. Non-relocatable images require explicit opt-in; prefer
`bv.memory_map` for offset-aware raw-image layouts."""


class Backend(Protocol):
    """What the protocol layer needs from the plugin."""

    def execute(
        self,
        code: str,
        target: Any = None,
        description: Any = None,
        read_only: bool = False,
        output_directory: str | None = None,
        output_extension: str | None = None,
    ) -> Any: ...
    def define_lib_function(self, source: str) -> str: ...
    def list_lib_functions(self) -> str: ...
    def remove_lib_function(self, name: str) -> str: ...
    def rebase_view(
        self,
        target: Any,
        new_base: int,
        entry_point: int | None = None,
        allow_non_relocatable: bool = False,
    ) -> str: ...
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
        raw_id = message.get("id", _MISSING)
        msg_id = raw_id if _valid_id(raw_id) else None
        raw_params = message.get("params", _MISSING)
        params = {} if raw_params is _MISSING else raw_params

        if message.get("jsonrpc") != "2.0":
            return _error(msg_id, -32600, "Invalid Request: jsonrpc must be '2.0'.")
        if not isinstance(method, str):
            return _error(
                msg_id,
                -32600,
                f"Invalid Request: method must be a string, "
                f"got {type(method).__name__}.",
            )
        if not isinstance(params, dict):
            return _error(
                msg_id,
                -32602,
                "Invalid params: expected an object, got "
                f"{type(params).__name__}. This server takes named parameters only.",
            )
        if raw_id is not _MISSING and not _valid_id(raw_id):
            return _error(
                None,
                -32600,
                "Invalid Request: id must be a string or integer and cannot be null.",
            )

        # Valid notifications carry no id and never receive a response. MCP
        # defines specific notification methods; none invoke this server's
        # tools, so there is no work to dispatch here.
        if raw_id is _MISSING:
            return None

        try:
            result = self._dispatch(method, params)
        except MCPError as e:
            return _error(msg_id, e.code, e.message)
        except Exception as e:  # never take the transport down
            return _error(msg_id, -32603, f"Internal error: {type(e).__name__}: {e}")

        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
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

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client = params.get("clientInfo")
        if not isinstance(version, str) or not version:
            raise MCPError(-32602, "Invalid params: protocolVersion is required.")
        if not isinstance(capabilities, dict):
            raise MCPError(-32602, "Invalid params: capabilities must be an object.")
        if not isinstance(client, dict):
            raise MCPError(-32602, "Invalid params: clientInfo must be an object.")
        if not isinstance(client.get("name"), str) or not isinstance(
            client.get("version"), str
        ):
            raise MCPError(
                -32602,
                "Invalid params: clientInfo requires string name and version fields.",
            )
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
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
                        "target": {
                            "type": "string",
                            "description": (
                                "Binary ID, name, or path. Optional when only "
                                "one binary is open."
                            ),
                        },
                        "read_only": {
                            "type": "boolean",
                            "description": (
                                "Always roll back every view; use for queries."
                            ),
                        },
                        "output_directory": {
                            "type": "string",
                            "description": (
                                "Existing absolute directory for complete output."
                            ),
                        },
                        "output_extension": {
                            "type": "string",
                            "description": (
                                "Artifact extension: 1–16 letters or digits."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": "Short log label.",
                        },
                    },
                    "required": ["code"],
                },
            },
            {
                "name": "define_lib_function",
                "description": DEFINE_LIB_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "One complete Python `def` statement.",
                        }
                    },
                    "required": ["source"],
                },
            },
            {
                "name": "list_lib_functions",
                "description": LIST_LIB_DESCRIPTION,
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "remove_lib_function",
                "description": REMOVE_LIB_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Function name to remove.",
                        }
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "rebase_view",
                "description": REBASE_DESCRIPTION,
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": (
                                "Binary ID, name, or path. Optional when only "
                                "one binary is open."
                            ),
                        },
                        "new_base": {
                            "oneOf": [
                                {"type": "integer", "minimum": 0},
                                {"type": "string", "pattern": "^0[xX][0-9a-fA-F]+$"},
                            ],
                            "description": "New virtual base; integer or hex string.",
                        },
                        "entry_point": {
                            "oneOf": [
                                {"type": "integer", "minimum": 0},
                                {"type": "string", "pattern": "^0[xX][0-9a-fA-F]+$"},
                            ],
                            "description": (
                                "Optional analysis entry point after rebase."
                            ),
                        },
                        "allow_non_relocatable": {
                            "type": "boolean",
                            "description": (
                                "Acknowledge that embedded absolute values may "
                                "not be adjusted."
                            ),
                        },
                    },
                    "required": ["new_base"],
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
                            "description": "Section name; omit for the full guide.",
                        }
                    },
                },
            },
        ]

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        raw_args = params.get("arguments", _MISSING)
        args = {} if raw_args is _MISSING else raw_args
        if not isinstance(name, str) or not name:
            raise MCPError(-32602, "Invalid params: tool name is required.")
        if not isinstance(args, dict):
            raise MCPError(-32602, "Invalid params: arguments must be an object.")

        if name == "execute":
            code = args.get("code")
            if not isinstance(code, str) or not code.strip():
                return _tool_error("`code` is required and must be a non-empty string.")
            target = args.get("target")
            if target is not None and not isinstance(target, str):
                return _tool_error("`target` must be the name of an open binary.")
            note = args.get("description")
            if note is not None and not isinstance(note, str):
                return _tool_error("`description` must be a string.")
            read_only = args.get("read_only", False)
            if not isinstance(read_only, bool):
                return _tool_error("`read_only` must be a boolean.")
            output_directory = args.get("output_directory")
            if output_directory is not None and not isinstance(output_directory, str):
                return _tool_error("`output_directory` must be a string.")
            output_extension = args.get("output_extension")
            if output_extension is not None and not isinstance(output_extension, str):
                return _tool_error("`output_extension` must be a string.")
            if (output_directory is None) != (output_extension is None):
                return _tool_error(
                    "`output_directory` and `output_extension` must be provided "
                    "together."
                )
            result = self.backend.execute(
                code,
                target,
                note,
                read_only,
                output_directory,
                output_extension,
            )

            # Reserve the footer and the error out of the budget first, then
            # give the rest to output. The footer is concatenated after the
            # clipping so no truncation can reach it.
            footer = _footer(
                getattr(result, "elapsed_s", 0.0) or 0.0,
                getattr(result, "timeout_s", None),
                tuple(getattr(result, "lib", ()) or ()),
            )
            artifact = _artifact_note(result)
            tail = ""
            if result.error:
                note = _ROLLBACK_NOTE if getattr(result, "reverted", False) else ""
                room = MAX_ERROR_BYTES - _size(_ERROR_PREFIX) - _size(note)
                tail = _ERROR_PREFIX + _clip_error(result.error, room) + note

            allowance = MAX_RESULT_BYTES - _size(footer) - _size(artifact)
            if tail:
                allowance -= MAX_ERROR_BYTES
            body = _clip_head(result.output, allowance) if result.output else ""
            if not body and not tail:
                body = "(no output — the script printed nothing)"

            return {
                "content": [{"type": "text", "text": body + tail + artifact + footer}],
                "isError": not result.success,
            }

        if name == "binja_guide":
            topic = args.get("topic")
            if topic is not None and not isinstance(topic, str):
                return _tool_error("`topic` must be a string.")
            text = self.backend.guide(topic)
            return _tool_text(text, MAX_RESULT_BYTES, is_error=False)

        if name == "rebase_view":
            target = args.get("target")
            if target is not None and not isinstance(target, str):
                return _tool_error("`target` must be the name of an open binary.")
            try:
                new_base = _address(args.get("new_base"), "new_base", required=True)
                entry_point = _address(
                    args.get("entry_point"), "entry_point", required=False
                )
                allow_non_relocatable = args.get("allow_non_relocatable", False)
                if not isinstance(allow_non_relocatable, bool):
                    raise ValueError("`allow_non_relocatable` must be a boolean.")
                assert new_base is not None
                text = self.backend.rebase_view(
                    target,
                    new_base,
                    entry_point,
                    allow_non_relocatable,
                )
            except (RuntimeError, ValueError) as e:
                return _tool_error(str(e))
            return _tool_text(text, MAX_RESULT_BYTES, is_error=False)

        if name == "define_lib_function":
            source = args.get("source")
            if not isinstance(source, str) or not source.strip():
                return _tool_error(
                    "`source` is required and must be one complete `def`."
                )
            try:
                text = self.backend.define_lib_function(source)
            except ValueError as e:
                return _tool_error(str(e))
            return _tool_text(text, MAX_RESULT_BYTES, is_error=False)

        if name == "list_lib_functions":
            return _tool_text(
                self.backend.list_lib_functions(),
                MAX_RESULT_BYTES,
                is_error=False,
            )

        if name == "remove_lib_function":
            function_name = args.get("name")
            if not isinstance(function_name, str) or not function_name:
                return _tool_error("`name` is required.")
            try:
                text = self.backend.remove_lib_function(function_name)
            except ValueError as e:
                return _tool_error(str(e))
            return _tool_text(text, MAX_RESULT_BYTES, is_error=False)

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
        if not isinstance(uri, str):
            raise MCPError(-32602, "Invalid params: uri is required.")
        if uri == "binja://guide":
            return _contents(uri, self.backend.guide(None), "text/markdown")
        if uri == "binja://status":
            body = json.dumps(self.backend.status(), indent=2, default=str)
            return _contents(uri, body, "application/json")
        raise MCPError(-32602, f"Unknown resource: {uri}")


_HEAD_NOTE = (
    "\n... (truncated here; rerun with output_directory and output_extension "
    "for complete output, or print a smaller slice)"
)
_TAIL_NOTE = "... (truncated; earlier lines were dropped)\n"
_ERROR_PREFIX = "\nError: "
_ROLLBACK_NOTE = "\n(Rolled back: any changes this script made are gone.)"


def _address(value: Any, name: str, *, required: bool) -> int | None:
    if value is None:
        if required:
            raise ValueError(f"`{name}` is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"`{name}` must be a non-negative integer or hex string.")
    try:
        parsed = int(value, 0) if isinstance(value, str) else value
    except ValueError:
        raise ValueError(
            f"`{name}` must be a non-negative integer or hex string."
        ) from None
    if parsed < 0 or parsed > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"`{name}` must fit in an unsigned 64-bit address.")
    return parsed


def _size(text: str) -> int:
    """Bytes, matching _Budget's encoding so both caps mean the same thing."""
    return len(text.encode("utf-8", "replace"))


def _clip_head(text: str, limit: int) -> str:
    """Keep the beginning. For print() output, which is read top-down."""
    if _size(text) <= limit:
        return text
    room = limit - _size(_HEAD_NOTE)
    if room <= 0:
        return _HEAD_NOTE[:limit] if limit > 0 else ""
    return text.encode("utf-8", "replace")[:room].decode("utf-8", "ignore") + _HEAD_NOTE


def _clip_tail(text: str, limit: int) -> str:
    """Keep the end. For a traceback, whose last line is the exception itself."""
    if _size(text) <= limit:
        return text
    room = limit - _size(_TAIL_NOTE)
    if room <= 0:
        # Guard the slice: bytes[-0:] is the WHOLE buffer, not an empty one, so
        # a small limit would return more than it was given.
        return _TAIL_NOTE[:limit] if limit > 0 else ""
    kept = text.encode("utf-8", "replace")[-room:].decode("utf-8", "ignore")
    return _TAIL_NOTE + kept


def _clip_error(error: str, limit: int) -> str:
    """Trim an error to its most useful parts: the first line and the last frames.

    Tail-first, because the bottom of a traceback is the exception and the frame
    that raised it. The first line is kept as well: the executor puts
    `Type: message` there, so an exception whose own message is enormous still
    reports what kind it was — which pure tail-clipping would lose.
    """
    if limit <= 0:
        return ""
    if _size(error) <= limit:
        return error
    first, _, rest = error.partition("\n")
    head = _clip_head(first, max(limit // 4, 1))
    out = head + "\n" + _clip_tail(rest, max(limit - _size(head) - 1, 0))
    # Composing two bounded pieces can still overshoot by the joiner at tiny
    # limits; clamp so the bound holds for every input.
    return out if _size(out) <= limit else _clip_head(out, limit)


def _footer(elapsed: float, budget: float | None, lib: tuple[str, ...] = ()) -> str:
    """Throughput signal, plus whatever is saved in h.lib.

    Batch sizing against the timeout is otherwise guesswork, and a library
    nobody can see is one the model re-derives or wrongly trusts.
    """
    timing = f"{elapsed:.1f}s of {budget:.0f}s" if budget else f"{elapsed:.1f}s"
    if not lib:
        return f"\n\n[{timing}]"
    return f"\n\n[{timing} | lib: {_lib_names(lib)}]"


def _artifact_note(result: Any) -> str:
    path = getattr(result, "artifact_path", None)
    if not path:
        return ""
    status = getattr(result, "artifact_status", "unknown")
    size = int(getattr(result, "artifact_bytes", 0) or 0)
    return _clip_head(
        f"\n\nOutput artifact ({status}, {size} bytes): {path}",
        MAX_MESSAGE_BYTES,
    )


def _lib_names(lib: tuple[str, ...]) -> str:
    """Bounded: a large library must not crowd out the result it annotates."""
    shown: list[str] = []
    used = 0
    for name in lib:
        used += _size(name) + 2
        # Always name at least one, clipped if it has to be: a bare "+N more"
        # tells the model it has a library but not what is in it.
        if shown and used > MAX_FOOTER_LIB_BYTES:
            return ", ".join(shown) + f", +{len(lib) - len(shown)} more"
        shown.append(_clip_head(name, MAX_FOOTER_LIB_BYTES))
    return ", ".join(shown)


def _tool_text(text: str, limit: int, *, is_error: bool) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _clip_head(text, limit)}],
        "isError": is_error,
    }


def _contents(uri: str, text: str, mime: str) -> dict[str, Any]:
    """Bound a resource body.

    JSON is replaced rather than clipped: trimming it mid-token produces
    exactly the unparseable bytes server.py refuses to send. Prose is clipped.
    """
    if _size(text) > MAX_RESULT_BYTES:
        text = (
            json.dumps({"error": "Resource too large to return."})
            if mime == "application/json"
            else _clip_head(text, MAX_RESULT_BYTES)
        )
    return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}


def _tool_error(message: str) -> dict[str, Any]:
    return _tool_text(message, MAX_MESSAGE_BYTES, is_error=True)


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": _clip_head(message, MAX_MESSAGE_BYTES)},
    }


def _valid_id(value: Any) -> bool:
    """MCP request IDs are strings or integers, never null or booleans."""
    return isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool)
    )
