# Working in Binary Ninja

Techniques that hold up in practice. The Binary Ninja API itself is documented at
api.binary.ninja — match the version in the header above. This file covers what that
documentation does not: which calls behave surprisingly, and how to make changes that
are actually correct.

## Ground rules

**Only make changes you are confident in.** A wrong name or type is worse than no name,
because everything downstream inherits it and the next reader trusts it. If the evidence
is ambiguous, record the ambiguity in a comment instead of guessing at a precise type.

**Each `execute` call runs in one undo transaction.** If your script raises, every change
it made is reverted — a failed batch leaves no partial state. On success the whole batch
becomes a single undo step. You do not need checkpoints, and you should not build your own
rollback.

**Each `execute` call is independent.** Nothing carries over: no variables, no imports, no
open handles. Re-derive what you need, or keep intermediate results in your own notes.

**Print what you want back.** `print()` output is the return channel, verbatim. It is
capped at 100 KB, so filter before printing rather than dumping and hoping.

**Print addresses as hex.** The API returns ints and the disassembly shows hex; mixing the
two is an easy way to lose your place. `print(hex(addr))`, always.

## Environment

Three globals:

- `bv` — the selected `BinaryView`. The real thing, not a wrapper.
- `bn` — the `binaryninja` module.
- `h` — this plugin's helpers, for the few things not in the Binary Ninja API.

Everything on `bv` works: `bv.functions`, `bv.get_code_refs()`, `bv.read()`,
`bv.define_user_type()`, `func.hlil`, `block.outgoing_edges`. Ordinary Python works too —
`import struct`, `import re`, comprehensions, nested functions, `collections`.

`h` is small:

- `h.binaries()` — list open tabs.
- `h.select(index_or_name)` — pick which one to work on. The session stays on your choice
  even if the user switches tabs in the UI.

**Do not iterate every function and decompile it.** On a few thousand functions that is
minutes of analysis and far more output than fits. Filter to a handful first, then look
closely:

```python
candidates = [f for f in bv.functions if "alloc" in f.name]
print(len(candidates))
for f in candidates[:5]:
    print(hex(f.start), f.name)
```

## Reading the binary

Raw bytes alone are not enough to correct analysis. Look at the bytes, the symbol, the
data variable, and the references together:

```python
addr = 0x47514
print(bytes(bv.read(addr, 16)))

sym = bv.get_symbol_at(addr)
var = bv.get_data_var_at(addr)
print(sym.name if sym else None)
print(str(var.type) if var else None)

for ref in bv.get_code_refs(addr):
    print(hex(ref.address), ref.function.name if ref.function else None)
```

The four address lookups differ in ways that matter:

- `get_symbol_at(addr)` — a symbol at *exactly* that address.
- `get_data_var_at(addr)` — may return the variable *containing* an interior address.
- `get_function_at(addr)` — requires a function start.
- `get_functions_containing(addr)` — finds the function when the address is in its body.

A compact hexdump:

```python
base, size = 0x3AA00, 0x50
data = bytes(bv.read(base, size))
for off in range(0, len(data), 8):
    chunk = data[off : off + 8]
    print(hex(base + off), " ".join(f"{b:02x}" for b in chunk), repr(chunk))
```

## Recovering data formats

When a blob looks like strings, a table, or an image, read the code that *consumes* it
before assigning a type. The consumer reveals record stride, index arithmetic, field
offsets, explicit lengths versus null termination, and signedness — none of which are
visible in the bytes alone.

```python
func = bv.get_function_at(0x34DCC)
print(func.name)
print(str(func.hlil))
```

After applying a type, print the HLIL again. Named fields and array indexing appearing in
the output is good evidence that the type matches the access pattern; if the decompiler
still shows raw offset arithmetic, the type is probably wrong.

## Types

For a single type, `parse_type_string` is the simplest path:

```python
struct_type, _ = bv.parse_type_string(
    "struct { char label[5]; uint8_t label_len; uint16_t value; }"
)
bv.define_user_type("record_t", struct_type)

defined = bv.get_type_by_name("record_t")
print(defined.width)
```

**Always verify the width.** A correct field list with wrong packing or alignment still
produces misleading analysis.

`parse_types_from_string` returns a `BasicTypeParserResult`, not a tuple. This does not
work:

```python
types, variables = bv.parse_types_from_string(source)  # wrong
```

Use the result object:

```python
result = bv.parse_types_from_string(source)
for name, type_object in result.types.items():
    bv.define_user_type(name, type_object)
```

If the project ships SDK or vendor headers, search them before inventing an approximate
type. Copy the exact definition and define its dependencies in order. Preprocessor-heavy
headers work better reduced to the minimal declarations you need than imported wholesale.
Applying an accurate type usually improves every caller too, so re-read their HLIL
afterwards.

## Data variables

Parse the type, then pass `define_user_data_var` a plain Python string for the name:

```python
array_type, _ = bv.parse_type_string("record_t const[5]")
bv.define_user_data_var(0x3AA08, array_type, "unit_records")
```

The second value from `parse_type_string` is a `QualifiedName`, not a `str`. Passing that
object as the `name` argument fails — pass a string.

Verify immediately:

```python
print(bv.get_symbol_at(0x3AA08).name)
print(str(bv.get_data_var_at(0x3AA08).type))
```

### Replacing conflicting analysis

Binary Ninja may have already inferred a pointer, string, or smaller variable inside the
range you want to cover. Remove the conflicting definitions first:

```python
for addr in (0x3AA08, 0x3AA20, 0x3AA38):
    try:
        bv.undefine_user_data_var(addr)
    except Exception:
        pass
    sym = bv.get_symbol_at(addr)
    if sym:
        bv.undefine_user_symbol(sym)
```

Be precise about addresses. `get_data_var_at` queried at an interior address reports the
*enclosing* variable, so do not undefine an address merely because that call returned
something. After defining the enclosing object, confirm the old interior symbols are gone
and interior queries resolve to the new array.

## Functions

**Naming a function and typing it are one operation.** A rename without a recovered
prototype is half-finished. Include the return type, a meaningful name and type for every
argument, integer width and signedness, pointer versus value, and `const` where it
applies. Do not leave `arg1` or a guessed `int32_t` when callers, callees, or an SDK
prototype provide better evidence.

```python
addr = 0x123456
signature = (
    "uint32_t parse_record(const uint8_t *data, uint16_t length, record_t *out);"
)
parsed, _ = bv.parse_type_string(signature)
func = bv.get_function_at(addr)
func.type = parsed
bv.update_analysis_and_wait()

print(func.type)
print(str(func.hlil)[:2000])
```

`update_analysis_and_wait()` matters: querying a function immediately after changing its
type otherwise shows stale analysis. Verify the prototype and the decompilation after
every signature change.

## Cleaning up false functions

Data — bitmap fonts especially — can contain byte sequences that decode as plausible
instructions, and Binary Ninja may create hundreds of false functions in such a region.
Establish that the region is data from references and consumers *first*; never delete
functions based on appearance alone.

```python
lo, hi = 0x397E4, 0x479F0
false_functions = [f for f in bv.functions if lo <= f.start < hi]
print(len(false_functions), [hex(f.start) for f in false_functions[:20]])
```

Then remove them, define the real data objects so the intended interpretation is explicit,
and rescan:

```python
for func in false_functions:
    bv.remove_function(func)

remaining = [hex(f.start) for f in bv.functions if lo <= f.start < hi]
print("remaining", len(remaining), remaining)
```

## Comments

Comments are most useful at the base of a table, or on the function that implements its
format:

```python
bv.set_comment_at(
    0x3AA08,
    "Unit label pairs. Each 12-byte record is 5 bytes of metric text, its "
    "length, 5 bytes of imperial text, and its length.",
)
```

Record what the type cannot express — character ranges, special-case conventions, why two
visually similar tables differ, and any uncertainty you did not resolve.

## Validating a table

A type that parses is not a type that is correct. Decode representative records from raw
bytes and compare against the fields you proposed:

```python
for addr in range(0x3AA08, 0x3AA08 + 5 * 12, 12):
    raw = bytes(bv.read(addr, 12))
    print(hex(addr), repr(raw[0:5]), raw[5], repr(raw[6:11]), raw[11])
```

For an array of structures also check the structure width, the element count, the
calculated end address, padding against the next object, the first and last records, and
the stride the indexing code actually uses.

## A batch pattern

1. Read raw bytes; inspect existing symbols, variables, functions, and references.
2. Read one or more consumers to establish the format.
3. Remove only the conflicting analysis objects.
4. Define named types; check their widths.
5. Define data variables with explicit string names.
6. Apply function names and prototypes together.
7. Add comments recording what the types cannot.
8. Verify: decode records, re-read HLIL, rescan for stale interior symbols.

Keep destructive work (deleting functions, undefining variables) in a separate `execute`
call from the definitions that follow it. Both calls are individually atomic, and the
split makes a failure much easier to understand.
