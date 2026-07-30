# Binary Ninja guide

## Safety

- Change only what the evidence supports. Put uncertainty in a comment.
- Each call is one undo transaction on `target`; an exception rolls it back.
- Calls time out after 30 seconds. Filter before iterating and split large jobs; use
  the `[1.4s of 30s]` result footer to size the next batch.
- `print()` returns up to 32 KB; errors keep their last 4 KB. If truncated, rerun a
  narrower script or print a slice. Print addresses as hex.
- Do not call Qt, `binaryninjaui`, or `PySide6`: scripts run off the GUI thread.
- Do not use `bv` or `h.read_only_view()` as a context manager; exiting closes the
  user's view. A view opened by `bn.load(path)` is yours and may use `with`.
- Keep calls short if the user is active: rolling back a failed call also rewinds GUI
  edits made after its transaction began.

## Environment

`bv` is the real `BinaryView` selected by `target` and the only writable view; `bn`
is `binaryninja`. Omit `target` only when one binary is open. Set `read_only=true`
for queries: every view rolls back and analysis/cache notifications are ignored.

- `h.binaries()` lists open binaries and stable IDs such as `binary-42`.
- `target` and `h.read_only_view()` accept an ID, unique name, or path.
- `h.read_only_view(id)` opens another tab for reading. Its transaction always
  rolls back; a detected write also fails the call after the script finishes.
- Normal Python, imports, and filesystem access work. Use `bv.read(address, length)`
  for mapped bytes; `bv.file.original_filename` names the original file.

Never decompile every function. Select a few first:

```python
hits = [f for f in bv.functions if "parse" in f.name]
for f in hits[:10]:
    print(hex(f.start), f.name)
```

## Querying

For one instruction, use `bv.get_disassembly(addr)`. Keep output bounded:

```python
for addr in addresses[start:start + 50]:
    print(hex(addr), bv.get_disassembly(addr))
```

Address lookups are not interchangeable:

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

`bv.sections` and `bv.data_vars` are mappings; iterate `.values()`. `bv.segments` is
a list. Read short C strings with `bv.get_ascii_string_at(addr, 1)`.

Before typing a blob, inspect its bytes, analysis, references, and consumers.
Consumers reveal stride, field offsets, signedness, and explicit lengths.

For a large function, inspect a small IL window. IL instructions are not guaranteed
to be in address order, and many machine-code addresses have no exact HLIL item:

```python
f = bv.get_functions_containing(addr)[0]
items = [i for i in f.hlil.instructions if abs(i.address - addr) < 0x40]
for i in sorted(items, key=lambda x: x.address):
    print(hex(i.address), i)
```

## Types and data

`parse_type_string` returns `(Type, QualifiedName)`:

```python
t, _ = bv.parse_type_string(
    "struct { uint32_t kind; const char *name; }"
)
bv.define_user_type("record_t", t)
print(bv.get_type_by_name("record_t").width)
```

Always verify a defined type's width; correct-looking fields with wrong packing or
alignment still produce bad decompilation.

`parse_types_from_string` returns a `BasicTypeParserResult`:

```python
result = bv.parse_types_from_string(source)
for name, t in result.types.items():
    bv.define_user_type(name, t)
```

Give data variables an explicit string name and verify the result. Guard lookups:
an exception during verification rolls back the definition.

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

Only undefine an exact object start. An interior `get_data_var_at()` returns the
enclosing variable; deleting it by the queried interior address removes the wrong
thing. After redefining a range, check that stale interior symbols are gone.

## Functions

Assign a declaration string to set both the name and prototype, then refresh analysis:

```python
f = bv.get_function_at(addr)
f.type = "uint32_t parse_record(const uint8_t *data, uint16_t length);"
bv.update_analysis_and_wait()
print(f.name, f.type)
```

Assigning a `Type` object changes only the prototype; set `f.name` separately.
Use `bv.remove_user_function(f)` to remove a function persistently. Do not use
`bv.undo()` inside a call; raise an exception to roll back the transaction.

Recover return type, parameter names and types, widths, signedness, pointers, `const`,
and calling convention. Re-read HLIL; named fields and indexes help confirm the type.

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

Addresses match only for equivalent layouts. For changed builds, match with stable
evidence such as bytes, symbols, strings, or call relationships.

`Type` objects can move directly between live views:

```python
t = src.get_type_by_name("config_t")
if t:
    bv.define_user_type("config_t", t)
```

Direct transfer preserves widths, parameter names, and calling conventions. Avoid
round-tripping through C text unless necessary.

To identify user annotations:

- symbol: `not sym.auto`
- data variable: `not var.auto_discovered`
- function variable: `f.is_var_user_defined(var)`
- type: present in `bv.user_type_container.types`

Iterate `f.vars` when checking local annotations; `f.parameter_vars` contains only
arguments.

## Saved functions

Calls share no variables or imports. Store reusable code with the
`define_lib_function` tool, passing one complete definition:

```python
def named(view):
    return [(f.start, f.name) for f in view.functions if not f.symbol.auto]
```

Then call `h.lib.named(bv)` from `execute`. The namespace is read-only there.
Definitions run with that call's `bv`, `bn`, `h`, and `print`; put imports and helpers
inside, pass other values as arguments, and use only immutable literal defaults.
Annotations are not retained. Use `list_lib_functions` to inspect definitions and
`remove_lib_function` to delete one. Calls to `h.lib.other()` resolve dynamically, so
removing `other` makes the caller fail normally.

## Verification

Read every edit back. For tables, decode sample records and check width, count, stride,
end address, padding, and boundary entries. For functions, print the final name,
prototype, and focused IL. Verify comments with `bv.get_comment_at(addr)`.

Inspect first, remove only conflicts, apply types and annotations, then read them
back and inspect the resulting IL.
