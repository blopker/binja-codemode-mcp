# Working in Binary Ninja

What api.binary.ninja does not tell you: which calls behave surprisingly, and how to
make changes that are actually correct.

## Ground rules

**Only make changes you are confident in.** A wrong name or type is worse than no name,
because everything downstream inherits it and the next reader trusts it. If the evidence
is ambiguous, record the ambiguity in a comment instead of guessing at a precise type.

**There is a 30-second limit.** A script that exceeds it cannot be interrupted: it is
abandoned, its changes are reverted when it eventually finishes, and nothing else can run
until then. Prefer several focused calls to one sweeping one, sized from the
`[1.4s of 30s]` footer on each result — your only signal about throughput on this binary.

**Each call runs in one undo transaction.** If your script raises, every change it made is
reverted, so you need no checkpoints and should not build your own rollback.

**Do not touch the GUI.** Your script runs on a worker thread. Qt may only be used from
Binary Ninja's main thread, so importing `binaryninjaui` or `PySide6` and calling into a
widget — `findChildren`, `windowTitle`, anything on `UIContext` — crashes Binary Ninja
outright, losing unsaved analysis. Nothing stops you. Everything you need is on `bv`.

**Nothing carries over between calls**: no variables, no imports, no open handles.
`print()` is the return channel, verbatim and capped at 32 KB, so filter rather than dump;
a failing script returns the tail of its traceback, trimmed to 4 KB.
Print addresses as hex — the API returns ints while the disassembly shows hex, and
mixing them loses your place.

## Environment

`bv` is the selected `BinaryView` — the real thing, not a wrapper — and `bn` is the
`binaryninja` module. Everything on `bv` works, and so does ordinary Python. Two helpers
cover what the Binary Ninja API does not:

- `h.binaries()` — list open tabs.
- `h.select(index_or_name)` — pick which one to work on. It rebinds `bv` immediately, so
  you can select and then edit in the same script.

**You have the filesystem.** There is no sandbox: `open()`, `pathlib`, `struct`,
`hashlib`, `subprocess` all work. Reading the file on disk alongside the analysis is often
the shortest path to an answer — diffing two builds byte for byte, checking a region the
analysis has not typed, or loading a symbol list or SDK header from the project directory.

```python
raw = bv.file.original_filename   # the binary; bv.file.filename is the .bndb
with open(raw, "rb") as f:
    image = f.read()
print(len(image), image[:16].hex())
```

Prefer `bv.read()` for anything mapped, since it resolves virtual addresses and reflects
the analysis; use the file directly when you need bytes the view does not cover, or when
you need a second binary that is not open in Binary Ninja.

**Do not iterate every function and decompile it.** On a few thousand functions that is
minutes of analysis and far more output than fits. Filter to a handful first, then look
closely:

```python
candidates = [f for f in bv.functions if "alloc" in f.name]
print(len(candidates))
for f in candidates[:5]:
    print(hex(f.start), f.name)
```

## Working across two databases

Porting annotations, diffing builds, or carrying a type library forward is one pattern:
read into locals from one view, `h.select()` the other, write.

```python
h.select(1)                                   # source
src = {f.start: (f.name, f.type) for f in bv.functions}

h.select(0)                                   # destination
for addr, (name, tobj) in src.items():
    f = bv.get_function_at(addr)
    if f:
        f.type = tobj                         # prototype, parameters, convention
        f.name = name                         # the type assignment does not set this
```

**Core objects move between views directly.** A `Type` or `Variable` read from one
BinaryView can be applied to another with no serialisation:

```python
h.select(1); tobj = bv.get_type_by_name("config_t")
h.select(0); bv.define_user_type("config_t", tobj)
```

Widths, parameter names, struct-pointer parameters, and calling conventions all survive.
Do **not** round-trip through C source with `parse_types_from_string` — it is far slower,
needs dependency ordering, and drops calling conventions and confidence levels.

**Stage intermediate results in a file.** Calls do not share state, so a multi-batch port
otherwise re-reads the source side once per batch. Collect once, write JSON next to the
binary, and read it back per batch:

```python
import json, pathlib
scratch = pathlib.Path(bv.file.filename).with_suffix(".port.json")
scratch.write_text(json.dumps(collected))     # first call
collected = json.loads(scratch.read_text())   # every call after
```

Type and Variable objects cannot be serialised this way — keep those in the
select-read-select-write shape above, and use the file for plain data like names,
addresses, and comment text.

## User annotations vs auto-analysis

"Which of this is human work?" is the central question in any port, diff, or summary, and
the answer uses a different predicate per object kind. Guessing from naming conventions
does not work — it over-reports badly, and writing auto-generated names into a database as
if they were annotations is exactly the wrong-name-is-worse-than-no-name failure.

| Object | Is it user work? |
|---|---|
| Symbol | `sym.auto` is `False` |
| Data variable | `var.auto_discovered` is `False` |
| Function variable | `func.is_var_user_defined(var)` |
| Type | present in `bv.user_type_container.types` |

`user_type_container.types` is a mapping of *type id* to `(QualifiedName, Type)` — keyed
by an opaque id, not by name, so match on the name inside the tuple.

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

# Pass the length: without it only references to that exact byte are found,
# and consumers of a table almost always index into its interior.
for ref in bv.get_code_refs(addr, 16):
    print(hex(ref.address), ref.function.name if ref.function else None)
```

The four address lookups differ in ways that matter:

- `get_symbol_at(addr)` — a symbol at *exactly* that address.
- `get_data_var_at(addr)` — may return the variable *containing* an interior address.
- `get_function_at(addr)` — requires a function start.
- `get_functions_containing(addr)` — finds the function when the address is in its body.

Three that reliably cost a round trip:

- `len(bv)` raises. Use `bv.start` and `bv.end` for the address range.
- `Type.enumeration` is a static constructor, not a property. To read an enum's members
  it is `t.members`; `t.enumeration.members` fails with `'function' object has no
  attribute 'members'`.
- `Segment` has no `.flags`. What you want after a raw-file diff is `seg.data_offset` and
  `seg.data_length`, which map file offsets to virtual addresses.

## Strings, sections, and references

Reading a C string at a pointer — the second argument is a *minimum* length, and the
default of 4 silently skips short strings:

```python
s = bv.get_ascii_string_at(ptr, 1)
print(s.value if s else None)
```

`bv.sections` is a mapping, not a list: iterate `bv.sections.values()` for `.name`,
`.start`, `.end`.

Follow **data** references as well as code ones. A pointer table is referenced by data
refs, so looking only at `get_code_refs` can miss the consumer entirely:

```python
print([hex(a) for a in bv.get_data_refs(addr, 8)])   # who points at this
print([hex(a) for a in bv.get_data_refs_from(addr, 8)])  # what this points at
```

Read comments back with `bv.get_comment_at(addr)`, the same way you read back every other
write.

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

`str(func.hlil)` on a real function is often ten thousand characters, which blows the
output cap and buries the line you care about. To look at one address, iterate the
instructions instead:

```python
func = bv.get_functions_containing(addr)[0]
for il in func.hlil.instructions:
    if il.address == addr:
        print(hex(il.address), il)
```

Widen it to a window (`abs(il.address - addr) < 0x40`) when you need surrounding context.

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
print(defined.width if defined else "NOT DEFINED")
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
sym = bv.get_symbol_at(0x3AA08)
var = bv.get_data_var_at(0x3AA08)
print(sym.name if sym else "NO SYMBOL")
print(str(var.type) if var else "NO DATA VAR")
```

Guard these lookups. They all return `None` on failure, and an `AttributeError` here
propagates out of the transaction and reverts the very definition you were checking.

### Replacing conflicting analysis

Binary Ninja may have already inferred a pointer, string, or smaller variable inside the
range you want to cover. Remove the conflicting definitions first:

Auto-discovered objects and ones you created need different calls, and the user-level
variants silently do nothing to auto-discovered ones — which are usually exactly what is
in the way. Check `sym.auto` and undefine accordingly:

```python
for addr in (0x3AA08, 0x3AA20, 0x3AA38):
    var = bv.get_data_var_at(addr)
    if var and var.address == addr:
        if var.auto_discovered:
            # blacklist stops analysis recreating it on the next pass
            bv.undefine_data_var(addr, blacklist=True)
        else:
            bv.undefine_user_data_var(addr)

    sym = bv.get_symbol_at(addr)
    if sym:
        if sym.auto:
            bv.undefine_auto_symbol(sym)
        else:
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
func = bv.get_function_at(addr)
# Assign the signature STRING, not a parsed Type: the setter only applies the
# name when it is given a string. Passing a parsed Type sets the prototype and
# silently leaves the function called sub_123456.
func.type = signature
bv.update_analysis_and_wait()

print(func.name, func.type)
print(str(func.hlil)[:2000])
```

When you hold a `Type` object rather than a string there is no name in it to apply, so do
both: `f.type = tobj` then `f.name = name`.

Without `update_analysis_and_wait()` a query right after a type change shows stale
analysis. Verify the prototype and the decompilation after
every signature change.

## Diffing two builds

Two analysis behaviours manufacture differences that are not in the bytes.

**Rendered IL is not a semantic diff.** Binary Ninja folds runs of consecutive constant
stores into `__builtin_memcpy`, and which run it folds depends on register allocation —
so two builds of the same source can fold at different addresses and read as a dozen
differing stores at MLIL and HLIL while the underlying bytes are identical. Compare
operations, not text:

```python
for il in f.mlil.instructions:
    if il.operation == bn.MediumLevelILOperation.MLIL_STORE:
        print(hex(il.address), il.dest.value, il.src.value)
```

**`bv.address_comments` includes platform-imported comments.** A freshly loaded firmware
database can report thousands before anyone has written one — SVD peripheral register
descriptions and similar. Counting them to judge how annotated a database is overstates it
by an order of magnitude; filter by address range first.

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
and rescan. Use `remove_user_function`, not `remove_function`: the latter is an auto-level
action, so the next analysis pass recreates every function you deleted.

```python
for func in false_functions:
    bv.remove_user_function(func)

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
