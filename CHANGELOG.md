# Changelog

All notable, user-facing changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - unreleased

A rewrite.

### Fixed

- **Database changes could be lost on save.** Mutations were applied outside any undo
  transaction, so Binary Ninja's modified-tracking did not fire, the UI did not refresh,
  and edits could die with the process. Every `execute` now runs in one undo transaction:
  a script that raises reverts everything it did, and a batch that succeeds collapses to a
  single ⌘Z.
- **Only the first opened binary was reachable.** The server captured a `BinaryView` at
  start and never updated it, so a second tab was invisible and edits could land on a
  stale view. The target is now resolved per request and pinned per session.
- **Scripts could not use their own top-level names.** `exec(code, globals, {})` meant
  nested functions and comprehensions raised `NameError`.
- **`print()` output was prefixed with a timestamp**, corrupting output the model parses.
- Checkpoint/rollback could revert the *user's* manual edits: it counted undo actions and
  called `bv.undo()` that many times. Removed — transactions replace it.
- The server no longer stops itself when the last file closes, which used to yank the
  endpoint out from under a connected client.

### Changed

- **The `binja` wrapper API is gone.** Scripts get `bv` (the real `BinaryView`), `bn`
  (the `binaryninja` module), and `h` (binary selection). This removes almost every
  friction point models reported: missing methods that exist on `BinaryView`, wrong
  `instruction_count`, inconsistent naming and return shapes.
- **Transport is MCP Streamable HTTP served by the plugin.** The separate `mcp_bridge.py`
  stdio process and the bespoke REST API are deleted; there is no longer a `python3` on
  `PATH` requirement or a bridge path to configure. The server is also multi-threaded, so
  a long `execute` no longer blocks every other request.
- **Guidance now actually reaches the model.** It is delivered via the MCP `instructions`
  field and the tool descriptions, plus a `binja_guide` *tool*. The old design put a note
  in `_meta` pointing at a resource, which no client acts on and which the model cannot
  read on its own.
- `binja_guide` generates a live header — loaded binary, architecture, analysis state,
  Binary Ninja version, open tabs — so the guidance describes the actual session.
- The code sandbox is removed. It blocked legitimate stdlib use while providing no real
  containment: CPython injects the real builtins when `globals` has no `__builtins__`.
- Config asks `binaryninja.user_directory()` instead of guessing paths per platform.
- Package moved to a `src/` layout; install is now a symlink.

### Removed

- Workspace files and saved skills. MCP clients have their own filesystem.
- `checkpoint` and `rollback` tools.
- **Headless support, and every platform but macOS.** This targets a running Binary Ninja
  GUI; a Personal licence forbids headless anyway, so the code paths pretending otherwise
  were untestable and misleading.

### Added

- **`h.lib`, a per-session library.** `h.lib["name"] = fn` keeps a function for the rest of
  the server session; `h.lib.name()` re-runs it against whatever is selected now. It stores
  functions rather than values, so nothing held can go stale. `print(h.lib)` lists what is
  saved, `h.lib.name.source` returns its text, `h.lib_sources()` returns all of them, and
  `del h.lib.name` removes one. Calls remain stateless in every other respect.
- **Every response is bounded.** One tool result is capped at 40 KB, with the error section
  reserved inside it, so a script that raises with an enormous message comes back readable
  instead of unbounded. Output keeps its head, a traceback keeps its tail *and* its first
  line, so the exception type survives however large its message.
- **Failures say whether the database was rolled back**, and a timing footer (`[1.4s of
  30s]`) gives the throughput signal needed to size a batch against the 30-second limit.
- **Tracebacks quote the line that raised**, rather than giving a line number into code the
  model has to remember.
- **A closed target is announced rather than silently replaced.** When the pinned binary is
  closed, the guide header says so and the next `execute` is refused once, so a script
  cannot write to a database nobody chose.
- **Status is reported in a dialog**, not only the log pane, and the server can be started
  from the status bar with no file open.
- pytest, ruff, and [ty](https://github.com/astral-sh/ty), with 222 tests covering the
  protocol, transport, execution contract, binary selection, library, and guide assembly,
  plus end-to-end tests over a real socket. Python pinned to 3.10 to match Binary Ninja's
  bundled interpreter. `tests/driver_plan.md` covers what the suite cannot reach — undo
  behaviour, tab handling, and persistence — by having a model drive a live session.

## [0.1.3] - 2026-01-08

### Added

- Status indicator for MCP server running state

### Fixed

- Plugin not running properly in headless mode

## [0.1.2] - 2025-12-18

### Added

- This changelog

### Changed

- Cleaned up the README and included community plugin installation information

### Fixed

- Fix `mcp_bridge.py` variables to initialize before use

## [0.1.1] - 2025-12-09

### Fixed

- Update `plugin.json` with correct key name so Vector35's `generate_plugininfo.py -v plugin.json` succeeds

## [0.1.0] - 2025-12-02

### Added

- Initial release
