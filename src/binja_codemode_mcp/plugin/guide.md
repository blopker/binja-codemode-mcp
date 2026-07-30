# Binary Ninja guide

## Safety

- Change only what the evidence supports. Put uncertainty in a comment.
- Each call is one undo transaction on `target`; an exception rolls it back.
- Calls time out after 30 seconds. Filter before iterating and split large jobs; use
  the `[1.4s of 30s]` result footer to size the next batch.
- `print()` returns up to 32 KB; errors keep their last 4 KB. Print addresses as hex.
- Do not call Qt, `binaryninjaui`, or `PySide6`: scripts run off the GUI thread.
- Do not use `bv` or `h.read_only_view()` as a context manager; exiting closes the
  user's view. A view opened by `bn.load(path)` is yours and may use `with`.
- Keep calls short if the user is active: rolling back a failed call also rewinds GUI
  edits made after its transaction began.

## Environment

`bv` is the real `BinaryView` named by `target`; `bn` is `binaryninja`. `bv` is the
only writable view. Omit `target` only when one binary is open.

- `h.binaries()` lists open binaries and target names.
- `h.read_only_view(name)` opens another tab for reading. Its transaction always
  rolls back; a detected write also fails the call after the script finishes.
- Normal Python, imports, and filesystem access work. Use `bv.read(address, length)`
  for mapped bytes; `bv.file.original_filename` names the original file.

Targets match any part of a name or path; ambiguous matches are rejected.

Never decompile every function. Select a few first:

```python
hits = [f for f in bv.functions if "parse" in f.name]
for f in hits[:10]:
    print(hex(f.start), f.name)
```

## Querying

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

Calls share no variables or imports. Save reusable functions in `h.lib`:

```python
def named(view):
    return [(f.start, f.name) for f in view.functions if not f.symbol.auto]

h.lib["named"] = named
print(h.lib.named(bv)[:10])
```

Only functions can be saved. They run with the current call's `bv`, `bn`, `h`, and
`print`; pass live database values as arguments instead of capturing them. Use
`print(h.lib)` to list entries and `h.lib_sources()` to export them.

Referenced imports, plain data, and top-level helpers travel with a saved function;
stateful objects must be constructed inside it or passed as arguments. Names defined
by a later caller do not. Inspect `h.lib.name.source`, and call saved functions from
each other through `h.lib.name()`.

## Verification

Read every edit back. For tables, decode sample records and check width, count, stride,
end address, padding, and boundary entries. For functions, print the final name,
prototype, and focused IL. Verify comments with `bv.get_comment_at(addr)`.

A productive edit batch is:

1. Inspect bytes, existing analysis, references, and consumers.
2. Remove only conflicting objects.
3. Define types and verify widths.
4. Define data variables and function prototypes.
5. Add comments for constraints or uncertainty the types cannot express.
6. Read everything back and inspect the resulting IL.
