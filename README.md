# Binary Ninja Code Mode MCP

An MCP server that lets an LLM drive a live Binary Ninja session by writing Python
against the **real** Binary Ninja API.

There is no wrapper API to learn. Inside a tool call the model gets `bv` — the actual
`BinaryView` — plus the `binaryninja` module, real builtins, and real imports. Models
already know this API from api.binary.ninja, so they use it correctly instead of guessing
at a bespoke dialect.

Each call runs in one undo transaction: if the script raises, every change it made is
reverted, and a successful batch collapses to a single ⌘Z.

macOS, GUI, personal use.

## Install

```sh
make install
```

That symlinks `src/binja_codemode_mcp` into your Binary Ninja plugins folder, so editing
the repo edits the plugin directly. Restart Binary Ninja afterwards. Override the location
with `make install BN_USER_DIR=...` if yours differs.

Nothing to pip install — Binary Ninja's bundled Python 3.10 has everything the plugin
needs.

## Use

1. Open a binary.
2. `Plugins → Code Mode MCP → Start Server`, or click the status-bar indicator.
3. The log prints the endpoint, the API key, and a ready-to-paste command:

```sh
claude mcp add --transport http binja http://127.0.0.1:42069/mcp \
  --header "Authorization: Bearer binja-codemode-local"
```

Then just ask:

- "Decompile main and tell me what the argument parsing does."
- "Find every caller of memcpy and check whether the size is bounded."
- "Name and type the functions in the 0x3a000 range from how their callers use them."

## What the model gets

**Tools**

- `execute` — run Python against the selected binary.
- `binja_guide` — live session state (which binary, architecture, analysis status, open
  tabs, Binary Ninja version) plus practical guidance on types, data variables, function
  prototypes, and the API calls that behave surprisingly.

**Globals inside `execute`**

| Name | What |
|---|---|
| `bv` | The real `BinaryView` |
| `bn` | The `binaryninja` module |
| `h` | `h.binaries()`, `h.select(index_or_name)` |

Guidance is delivered three ways, because clients surface each differently: the MCP
`instructions` field (always in context), the tool descriptions, and the `binja_guide`
tool. Resources exist too, but nothing depends on them — in Claude Code a resource is
`@`-mention only and the model cannot read one on its own.

## Multiple binaries

Open as many as you like. The session pins one on first use and stays on it even when you
click another tab, so a long analysis cannot retarget under the model's feet.
`h.binaries()` lists them; `h.select(1)` or `h.select("libfoo")` switches. The guide
header lists open tabs on every call.

## Configuration

Optional, at `<user dir>/codemode_mcp/config.json`:

```json
{ "api_key": "your-key" }
```

The server binds `127.0.0.1`, validates `Origin`, and requires the bearer token.

## Safety

This runs arbitrary Python inside your Binary Ninja process, with your permissions, and
there is deliberately no sandbox. An AST filter over submitted code cannot contain it —
CPython injects the real `builtins` module whenever the globals dict has no
`__builtins__` key, so `open` and `__import__` stay reachable at runtime no matter what
the filter rejects by name — while it does reliably block legitimate stdlib use like
`struct` and `pathlib`. The undo transaction is the real protection: a failed script
reverts, and any batch can be undone in one step. Point it at trusted clients only.

## Design notes

**The real API, not a wrapper.** A curated `binja.*` facade looks like it saves tokens and
does the opposite: models already know `BinaryView` from pretraining, so a smaller dialect
costs far more in failed calls and hallucination recovery than the curation saves. It also
goes stale against the API it wraps. Exposing `bv` deletes that whole class of problem.

**One transaction per call.** Mutations applied outside `bv.undoable_transaction()` do not
register as undo actions, which means Binary Ninja's modified-tracking may not fire and
the edits can be lost on save. Wrapping the whole script also makes a tool call atomic and
collapses a batch to one ⌘Z, which is what a checkpoint/rollback feature would otherwise
try — and fail — to provide.

**Stateless calls.** Nothing persists between `execute` calls: no variables, no imports.
Re-deriving state is cheaper than reasoning about a namespace neither side can see.

**Per-request binary resolution.** Capturing a `BinaryView` once means a second tab is
unreachable and edits can land on a stale view. The target is resolved per request and
pinned per session, so the model's target is stable but never wrong.

**Three guidance layers**, because clients surface each differently. The `instructions`
field is always in context; tool descriptions load when a tool is pulled in; `binja_guide`
is unbounded and on demand. Both `instructions` and tool descriptions are truncated at
2 KB by some clients, so they stay short and front-loaded — tests enforce the budget.

## Development

```sh
make setup      # uv sync
make test       # lint + typecheck + tests
make fmt        # apply autofixes and format
make install    # symlink into Binary Ninja
make status     # is it linked, is the endpoint up
```

`make help` lists everything.

`.python-version` pins 3.10 to match Binary Ninja's bundled interpreter; testing on newer
Python would pass code that fails in the host. There are no runtime dependencies, and
there must not be — the plugin has to load without pip, so there is deliberately no
`requirements.txt` for the plugin manager to act on.

Type checking runs on [ty](https://github.com/astral-sh/ty), pointed at the Binary Ninja
API inside the app bundle. It is pre-1.0, so expect its diagnostics to shift between
releases. Write suppressions as a bare `# type: ignore`; ty also accepts
`# ty: ignore[rule]`.

### Iterating inside Binary Ninja

`make install` symlinks the package, so edits land immediately — but Python still has the
old modules loaded. In Binary Ninja's Python console:

```python
import binja_codemode_mcp, importlib
importlib.reload(binja_codemode_mcp)
```

Then `[UP] [ENTER]` to re-run after each edit. Two caveats: `PluginCommand.register` calls
run again on reload, so menu entries can duplicate until restart, and a running server
keeps serving the modules it was started with — stop it, reload, start it again. When
anything looks stale, restart Binary Ninja.

For step debugging, `pip install --user debugpy`, then call
`connect_vscode_debugger(port=12345)` in the Binary Ninja console and attach with a path
mapping of `/` to `/`.

### Testing without a licence

`import binaryninja` succeeds outside the GUI, so **the type checker resolves real types**
from the app bundle. `binaryninja.load()` does not — a Personal licence forbids headless — so
**pytest can never construct a real `BinaryView`**. Three consequences:

- A module that imports `binaryninja` at module scope poisons the import path for
  everything under it. Hence the guarded import in `binja_codemode_mcp/__init__.py` and
  the empty `plugin/__init__.py`.
- Everything except `commands.py`, `widget.py`, and `uicontext.py` is pure and directly
  testable. `tests/conftest.py` supplies a small `FakeBinaryView` for the rest. It stays
  small on purpose: a large fake would mean we had rebuilt the wrapper we removed.
- `tests/test_integration.py` drives the whole stack over a real socket and asserts on
  what a client actually receives.

### Checks that only work by hand

Whether a transaction reaches the `.bndb` cannot be tested here. After changing the
executor or the session, in Binary Ninja:

- Rename ~20 functions via `execute`; confirm the view updates, ⌘Z reverts the batch as
  one step, and the names survive save → close → reopen.
- Raise an exception halfway through a mutating script; confirm nothing was applied.
- Open two binaries; confirm the session stays pinned when you switch tabs.

To find friction the tests cannot, ask a model to use the server and report where it
struggled, then fold the findings into `guide.md`. That loop is what produced most of the
guide's content.

### Known limitations

- The execution timeout abandons its worker thread rather than killing it, so a
  `while True:` leaks a thread — with its transaction still open — for the life of the
  process.
- `tools/list_changed` is advertised but never emitted: the tool surface does not actually
  change when tabs open and close, and the guide header is regenerated per call.
- `uicontext.list_tabs()` depends on `binaryninjaui` methods that have no type stubs. If
  they misbehave it degrades to single-binary mode rather than reporting no binary open.

## License

MIT — see [LICENSE](LICENSE).
