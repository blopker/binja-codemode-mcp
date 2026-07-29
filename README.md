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

Claude
```sh
claude mcp add --transport http binja http://127.0.0.1:42069/mcp \
  --header "Authorization: Bearer binja-codemode-local"
```

Opencode
```sh
opencode mcp add binja --url http://127.0.0.1:42069/mcp \
  --header "Authorization=Bearer binja-codemode-local"
```

If `claude mcp list` shows the server connected but `execute` and `binja_guide` do not
appear in the tool list, reconnect the client. A reachable endpoint is not the same as
registered tools, and the symptom looks like a broken server.

Then just ask:

- "Decompile main and tell me what the argument parsing does."
- "Find every caller of memcpy and check whether the size is bounded."
- "Name and type the functions in the 0x3a000 range from how their callers use them."

## What the model gets

**Tools**

- `execute` — run Python against a named binary, which is the only one it can write to.
- `binja_guide` — live session state (which binary, architecture, analysis status, open
  tabs, Binary Ninja version) plus practical guidance on types, data variables, function
  prototypes, and the API calls that behave surprisingly.

**Globals inside `execute`**

| Name | What |
|---|---|
| `bv` | The real `BinaryView` |
| `bn` | The `binaryninja` module |
| `h` | `h.binaries()`, `h.read_only_view(name)`, `h.lib`, `h.lib_sources()` |

Guidance is delivered three ways, because clients surface each differently: the MCP
`instructions` field (always in context), the tool descriptions, and the `binja_guide`
tool. Resources exist too, but nothing depends on them — in Claude Code a resource is
`@`-mention only and the model cannot read one on its own.

## Multiple binaries

Open as many as you like. Every `execute` call names its `target` — the one binary it may
write to — so a write can never land somewhere nobody chose. With a single binary open the
parameter is optional; with more than one it is required, and omitting it is an error that
lists the candidates.

A second binary is read through `h.read_only_view("name")`, which returns the real
`BinaryView`. Both are live in the same call, which is what a cross-database port needs:
`Type` objects move between views directly and cannot survive to the next call. Writing
through a read-only view is detected, rolled back, and fails the call.

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

**One transaction per call.** Mutations applied outside an undo transaction do not
register as undo actions, which means Binary Ninja's modified-tracking may not fire and
the edits can be lost on save. Wrapping the whole script also makes a tool call atomic and
collapses a batch to one ⌘Z, which is what a checkpoint/rollback feature would otherwise
try — and fail — to provide. The executor drives `begin_undo_actions` /
`commit_undo_actions` by hand rather than using the `undoable_transaction` context
manager, because a batch that outran the timeout has to revert on its way out and the
context manager would commit it. Scripts are serialised for the same reason: two open undo
states on one database interleave, so reverting one can rewind the other.

**Functions persist, values do not.** No variables or imports survive an `execute` call;
functions assigned into `h.lib` do. Storing values would mean answering "is this still
true?" on every read, and a namespace neither side can see is worse than re-deriving.
A stored function never raises that question — it re-runs against the live database — so
the library is closer to a per-session set of tools than to a cache. Each entry is rebound
to the calling script's scope on the way out, because a function otherwise resolves
globals from the call that defined it: it would see that call's `bv` forever, and its
`print` would write into an output buffer that closed long ago. The footer lists what is
saved on every result, so the library is the most visible thing in the session rather
than the least.

**The target is a call parameter, not session state.** An earlier design pinned a target
and let scripts rebind it mid-run, which put three sources of truth in play: the view the
transaction was opened on, the view the script could see, and the pin deciding where the
next call would start. They could disagree, and when they did a write landed outside any
transaction. Naming the target in the call collapses all three — the transaction is opened
on the view the script writes to, by construction — and leaves nothing to go stale, so a
reopened file simply works. Names, never indices: indices follow tab order and change when
tabs are dragged.

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
make driver     # drive a live session through tests/driver_plan.md
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

`pyproject.toml` also carries a `[tool.basedpyright]` block so an editor's language server
resolves the same imports — the package lives under `src/` and the Binary Ninja API lives
in the app bundle, so without it both look missing. It is editor-only: not a gate, not a
dependency. A local `pyrightconfig.json` would override it, and is gitignored.

### Iterating inside Binary Ninja

`make install` symlinks the package, so edits land immediately — but Python still has the
old modules loaded. In Binary Ninja's Python console:

```python
import binja_codemode_mcp, importlib
importlib.reload(binja_codemode_mcp)
```

Then `[UP] [ENTER]` to re-run after each edit. One caveat: a running server keeps serving
the modules it was started with — stop it, reload, start it again. When anything looks
stale, restart Binary Ninja. Re-registering a command of the same name replaces the
existing one, and the status widget tears down its previous notification on reload, so
neither duplicates.

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

### Checks that only work against a live session

Whether a transaction reaches the `.bndb` cannot be tested here, and neither can undo
grouping, tab handling, or the guide's own advice. `tests/driver_plan.md` covers that:
`make driver` lays out fresh copies of `/bin/ls` under `scratch/driver/` and hands the
plan to a model connected to a running server, which works through it and writes a report
to `scratch/`. A human is needed twice — once to set up, once at a
checkpoint that needs a save, a tab close, and a look at the window.

Read the report's E2 section first. Asking what the driver had to discover by trial and
error is what produced most of `guide.md`, and it has repeatedly found things the suite
could not: a failed script keeping its changes, a guide call swallowing a target switch,
and a write landing outside its transaction after a mid-script retarget.

Treat a reported failure as a hypothesis. Two have turned out to be artifacts of how the
run was conducted rather than defects, so measure before changing anything — both times
the disproof took two calls.

### Known limitations

- The execution timeout interrupts a running script only sometimes. A script that outran
  it is marked abandoned, so it reverts rather than commits whenever it finishes, and no
  other script can run until it does. On top of that the executor raises an asynchronous
  exception into it, which evicts a plain runaway loop immediately — but measurably does
  **not** evict a loop whose body contains a `try`/`except`, and does nothing at all while
  the script sits inside a long Binary Ninja core call. A script of either shape still
  holds the executor until it returns on its own.
- `tools/list_changed` is advertised but never emitted: the tool surface does not actually
  change when tabs open and close, and the guide header is regenerated per call.
- `uicontext.list_tabs()` depends on `binaryninjaui` methods that have no type stubs. If
  they misbehave it degrades to single-binary mode rather than reporting no binary open.
- **A failed script pulls the Binary Ninja window to the foreground.** Every failure
  reverts its undo transaction, and `revert_undo_actions` raises the window even when the
  transaction recorded nothing — so a typo costs a window raise. Confirmed by isolating
  the cases against a live instance: an empty *commit* is silent, an empty *revert* pops.
  Skipping the revert when nothing changed would fix it, but nothing available answers
  "did this script change anything" reliably (`file.modified` does not move on a rename;
  `file.undo_entries` only grows at commit), and an earlier attempt to gate on
  `file.modified` silently stopped failed scripts reverting at all. The redraw is
  cosmetic; the revert is the guarantee the plugin exists for. See `settle()` in
  `plugin/executor.py`.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). Copyright (C) 2026 BO LLC.

## Acknowledgements

Started as a fork of
[binja-codemode-mcp](https://github.com/akrutsinger/binja-codemode-mcp) by Austyn
Krutsinger. Almost none of that code survives the rewrite, but the idea — let the model
write Python against Binary Ninja instead of calling a wrapper — came from there.
