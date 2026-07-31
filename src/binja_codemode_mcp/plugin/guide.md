# Binary Ninja guide

## Safety

- Change only what evidence supports; put uncertainty in a comment.
- Each call is one undo transaction on `target`; an exception rolls it back.
- Calls return after 30 seconds. Native code cannot be killed; while it unwinds the
  UI says timed out and other calls wait. Never use `update_analysis_and_wait()`;
  call `update_analysis()` and inspect results later.
- `print()` returns 32 KB; errors keep their last 4 KB. Complete output needs an
  absolute `output_directory` and alphanumeric `output_extension`; it streams at
  most 100 MiB to a generated `.partial`, renamed `.failed` on error. Print
  addresses as hex.
- Do not call Qt, `binaryninjaui`, or `PySide6`: scripts run off the GUI thread.
- Do not use handed views as a context manager; exiting closes the user's view.
  `bn.load()` requires `update_analysis=False`; its view closes at call end.
- Undo does not cover files created by a script.
- A rollback also rewinds GUI edits made after the call began.

## Environment

`bv` is the real `target` `BinaryView` and the only writable view; `bn` is
`binaryninja`. Omit `target` only with one binary open. Use `read_only=true` for
queries; all views roll back and analysis/cache notifications are ignored.

- `h.binaries()` lists open binaries and stable IDs such as `binary-42`.
- `target` and `h.read_only_view()` accept an ID, unique name, or path.
- `h.read_only_view(id)` reads another tab; its transaction always rolls back and
  a detected write fails the call after the script finishes.
- Normal Python, imports, and filesystem access work. Use `bv.read(address, length)`
  for mapped bytes; `bv.file.original_filename` names the original file.

Filter `bv.functions` by name, address, or references before decompiling a few.

## Address layout

`rebase_view` relocates an established database. Direct `bv.rebase()` is blocked
because it replaces the transactional view. The tool requires a clean
BNDB, writes a timestamped sibling backup, may reanalyze, and verifies the result.

Non-relocatable images need `allow_non_relocatable=true`; prefer `bv.memory_map`
for raw firmware. A region maps data; a section only labels mapped addresses. For
a header, read payload bytes from `bv.file.raw` at their file offset and add a
region at the virtual base. Changes support undo and persist; legacy removal may not.

`bv.entry_point` is the loader entry; user entries are in `bv.entry_functions` and
added with `bv.add_entry_point(addr)`. `bv.modified` reports unsaved changes,
`bv.has_database` a BNDB, and `bv.save_auto_snapshot()` persists its current state.

## Querying

Use `bv.get_disassembly(addr)` for one instruction. Bound output:

```python
for addr in addresses[start:start + 50]:
    print(hex(addr), bv.get_disassembly(addr))
```

Address lookups differ:

- `get_symbol_at(addr)` requires an exact address.
- `get_data_var_at(addr)` may return an object containing `addr`.
- `get_function_at(addr)` requires the function start.
- `get_functions_containing(addr)` accepts an address in the body.

Pass a length when searching a range:

```python
for ref in bv.get_code_refs(addr, length):
    print(hex(ref.address), ref.function)
print([hex(a) for a in bv.get_data_refs(addr, length)])
```

`bv.sections` and `bv.data_vars` are mappings; `bv.segments` is a list. Read short
C strings with `bv.get_ascii_string_at(addr, 1)`.

Before typing a blob, inspect bytes, references, and consumers for stride, offsets,
signedness, and lengths.

Inspect a bounded IL window; IL order is not guaranteed and many addresses have no
exact HLIL item:

```python
f = bv.get_functions_containing(addr)[0]
items = [i for i in f.hlil.instructions if abs(i.address - addr) < 0x40]
for i in sorted(items, key=lambda x: x.address):
    print(hex(i.address), i)
```

IL operands are recursive; use `instruction.traverse(...)` rather than assuming a
convenience property such as `.constants` exists.

## Types and data

`parse_type_string` returns `(Type, QualifiedName)`:

```python
t, _ = bv.parse_type_string(
    "struct { uint32_t kind; const char *name; }"
)
bv.define_user_type("record_t", t)
print(bv.get_type_by_name("record_t").width)
```

Verify type width; wrong packing or alignment produces bad decompilation.

`parse_types_from_string` returns a `BasicTypeParserResult`:

```python
result = bv.parse_types_from_string(source)
for name, t in result.types.items():
    bv.define_user_type(name, t)
```

Give data variables a string name and verify the result; guard lookups because an
exception rolls the definition back.

```python
t, _ = bv.parse_type_string("record_t[8]")
bv.define_user_data_var(addr, t, "records")
var = bv.get_data_var_at(addr)
print(str(var.type) if var else "NOT DEFINED")
```

Remove a conflicting object with the API matching its origin:

```python
var = bv.get_data_var_at(addr)
if var and var.address == addr:
    if var.auto_discovered:
        bv.undefine_data_var(addr, blacklist=True)
    else:
        bv.undefine_user_data_var(addr)

sym = bv.get_symbol_at(addr)
if sym:
    bv.undefine_auto_symbol(sym) if sym.auto else bv.undefine_user_symbol(sym)
```

Only undefine an exact object start: an interior `get_data_var_at()` returns the
enclosing variable. After redefining a range, check for stale interior symbols.

## Functions

Assign a declaration string to set both name and prototype, then request analysis:

```python
f = bv.get_function_at(addr)
f.type = "uint32_t parse_record(const uint8_t *data, uint16_t length);"
bv.update_analysis()
# Verify f.name and f.type in a later call after analysis completes.
```

Assigning a `Type` object changes only the prototype; set `f.name` separately.
Use `bv.remove_user_function(f)` to remove a function persistently. Do not use
`bv.undo()` inside a call; raise an exception to roll back the transaction.

Recover return and parameter types, widths, signedness, pointers, `const`, and
calling convention. Re-read HLIL to confirm them.

## Multiple binaries

Make the destination the call's `target`, read the source through
`h.read_only_view()`, and write only to `bv`:

```python
src = h.read_only_view("old-build")
old = src.get_function_at(addr)
new = bv.get_function_at(addr)
if old and new and not old.symbol.auto:
    new.type = old.type
    new.name = old.name
```

Addresses match only for equivalent layouts; otherwise match bytes, symbols,
strings, or call relationships.

`Type` objects can move directly between live views:

```python
t = src.get_type_by_name("config_t")
if t:
    bv.define_user_type("config_t", t)
```

To identify user annotations:

- symbol: `not sym.auto`
- data variable: `not var.auto_discovered`
- function variable: `f.is_var_user_defined(var)`
- type: present in `bv.user_type_container.types`

Check `f.vars`; `f.parameter_vars` contains only arguments.

## Saved functions

Calls share no variables or imports. Pass one complete `def` to
`define_lib_function`, then call it as `h.lib.name(bv)` from `execute`; the namespace
is read-only. Definitions use
that call's globals. Put imports and helpers inside, pass other values, and use
immutable literal defaults. Annotations are not retained. Inspect with
`list_lib_functions`, delete with `remove_lib_function`. Calls to `h.lib.other()`
resolve dynamically; removing `other` makes its caller fail normally.

## Verification

Read edits and IL back. Check table width, count, stride, and boundaries; comments
use `bv.get_comment_at(addr)`.
