# Binary Ninja Code Mode MCP

An MCP server that lets an LLM drive a live Binary Ninja session with Python and the
real `BinaryView` API—no wrapper dialect.

Each call runs in one undo transaction. An exception reverts its changes; a successful
batch is one undo step.

Currently tested with the Binary Ninja Personal GUI.

## Install

```sh
make install
```

This symlinks `src/binja_codemode_mcp` into Binary Ninja's plugin directory. Restart
Binary Ninja after installing. If your user directory differs:

```sh
make install BN_USER_DIR=/path/to/binary-ninja/user
```

There are no runtime dependencies or separate `pip install` step.

## Connect

Start the server from `Plugins → Code Mode MCP → Start Server` or the status-bar
indicator. It can start without a binary open; open one before calling `execute`.
The Binary Ninja log shows the endpoint and token. The commands below use their
defaults.

Claude Code:

```sh
claude mcp add --transport http binja http://127.0.0.1:42069/mcp \
  --header "Authorization: Bearer binja-codemode-local"
```

OpenCode:

```sh
opencode mcp add binja --url http://127.0.0.1:42069/mcp \
  --header "Authorization=Bearer binja-codemode-local"
```

Codex reads the token from an environment variable:

```sh
export BINJA_MCP_TOKEN=binja-codemode-local
codex mcp add binja --url http://127.0.0.1:42069/mcp \
  --bearer-token-env-var BINJA_MCP_TOKEN
```

This adds:

```toml
[mcp_servers.binja]
url = "http://127.0.0.1:42069/mcp"
bearer_token_env_var = "BINJA_MCP_TOKEN"
```

Keep `BINJA_MCP_TOKEN` exported in sessions that use the server. If a client reaches
the endpoint but does not expose `execute` and `binja_guide`, reconnect it.

Then ask for work directly:

- “Decompile main and explain the argument parsing.”
- “Find every caller of memcpy and check whether the size is bounded.”
- “Name and type the functions in the 0x3a000 range from their callers.”

## Tool surface

- `execute` runs Python against one open binary.
- `rebase_view` backs up and relocates a clean saved database through Binary Ninja's
  UI.
- `binja_guide` returns live session details and concise guidance for safe queries and
  edits.

Rebase backups are siblings named
`<database-stem>.pre-rebase-YYYYMMDDTHHMMSS±HHMM.bndb`; a collision is an error.
Images Binary Ninja marks non-relocatable require `allow_non_relocatable=true`.

`execute` provides:

| Name | Value |
|---|---|
| `bv` | The `BinaryView` selected by `target` |
| `bn` | The `binaryninja` module |
| `h` | `binaries()`, `read_only_view(name)`, and read-only `lib` |

Builtins, imports, and filesystem access work. Return data with `print()`. Calls time
out after 30 seconds; results include a 32 KB preview and retain 4 KB of errors.

For complete output, pass an existing absolute `output_directory` and an
`output_extension` of 1–16 letters or digits. The server streams up to 100 MiB into
an exclusively created name containing the target, stable ID, UTC start time, and
random suffix. It ends in `.partial` while running, loses that suffix on success,
and becomes `.failed` after any execution failure:

```text
binja-{target}-{binary-id}-{YYYYMMDDTHHMMSSZ}-{8-hex}.{extension}.partial
```

### Multiple binaries

`h.binaries()` returns a stable ID for each open file session, such as `binary-42`.
Use that ID as `target` or pass a unique name or path. With one binary open, `target`
is optional.

Read another open binary with `h.read_only_view("binary-42")`. It returns a live
`BinaryView`, so types and annotations can move directly between databases. Its
transaction always rolls back; a detected write also fails the call.

Set `read_only=true` on `execute` for queries. `target` still selects `bv`, but every
view rolls back and lazy analysis/cache updates do not count as write violations.

### Saved functions

Calls do not share variables or imports. Use `define_lib_function` to store one
self-contained function:

```python
def named(view):
    return [f.name for f in view.functions if not f.symbol.auto]
```

Call it later from `execute` as `h.lib.named(bv)`. It runs against that call's
database rather than caching a result. Put imports and helpers inside, pass other
values as arguments, and use only immutable literal defaults. Use
`list_lib_functions` to inspect or export definitions and `remove_lib_function` to
delete one. Functions may call other saved functions through `h.lib`; a missing
dependency raises normally.

## Configuration and safety

The optional configuration file is `<Binary Ninja user directory>/codemode_mcp/config.json`:

```json
{ "api_key": "your-key" }
```

The server binds to `127.0.0.1`, checks `Origin`, and requires the bearer token. The
default token is intended to prevent accidental access, not secure the endpoint from
other local software.

Submitted code runs inside Binary Ninja with your user permissions. There is
deliberately no sandbox: Python can read files, import modules, and start processes.
Undo transactions protect Binary Ninja database edits, not the filesystem or other
side effects. Connect only trusted clients.

## Development

```sh
make setup      # install development dependencies
make test       # lint, type-check, and run tests
make fmt        # apply formatting and lint fixes
make install    # symlink the plugin into Binary Ninja
make status     # inspect the link and endpoint
make driver     # run the live-session test plan
```

`make help` lists all targets. The project targets Python 3.10 to match Binary Ninja
and has no runtime dependencies. Type checking uses the Binary Ninja API from the app
bundle.

### Reloading

Because installation uses a symlink, edits are immediately visible on disk, but Binary
Ninja keeps plugin modules in memory. Stop the server and restart Binary Ninja to load
changes; reloading only the package leaves its imported submodules stale.

### Testing

`pytest` cannot create a real `BinaryView` with a Personal license, so unit and socket
integration tests use a small fake. `make driver` exercises transactions, undo grouping,
tabs, and the guide against a live Binary Ninja session using `tests/driver_plan.md`.

## Known limitations

- Submitted loops and function entries contain cooperative timeout checks, so
  accidental infinite loops and recursion are interrupted and rolled back. A core
  API call or code without a checkpoint may continue; it is marked abandoned and
  reverts when it finishes. Later calls wait meanwhile.
- A failed call or any use of `h.read_only_view()` may bring Binary Ninja forward
  because reverting even an empty undo transaction raises the window. The redraw is
  cosmetic.
- Multi-tab discovery relies on untyped `binaryninjaui` APIs. If it fails, the plugin
  falls back to the current binary.

## License

GPL-3.0-or-later—see [LICENSE](LICENSE). Copyright (C) 2026 BO LLC.

## Acknowledgements

Started as a fork of
[binja-codemode-mcp](https://github.com/akrutsinger/binja-codemode-mcp) by Austyn
Krutsinger. The core idea—let the model use Binary Ninja's Python API directly—came
from that project.
