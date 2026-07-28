# Field notes: porting analysis between two databases

Friction found while driving the server through a real task — diffing two builds of the same
STM32 firmware (S310US and S410EU, 26200 bytes each, 219 functions) and porting a complete
annotation set from the analyzed database into the bare one: 121 types, 219 function
prototypes, 235 named globals, 285 user variables, and 186 comments.

The port succeeded and verified at full parity. Everything below is about the cost of getting
there, ordered by how much it slowed the work down.

---

## 1. Nothing in the guide covers working across two databases

This was the whole task, and it is the one thing the guide has no section for. `h.select()`
and `h.binaries()` are documented as a way to *switch* which binary you are on — a
navigation aid. They are actually the primitive for cross-database work, because `h.select()`
rebinds `bv` mid-script. That makes this the core pattern:

```python
h.select(1)                       # source
src = {f.start: (f.name, f.type) for f in bv.functions}
h.select(0)                       # destination
for addr, (name, tobj) in src.items():
    f = bv.get_function_at(addr)
    f.type = tobj
    f.name = name
```

The guide states the rebinding fact in one clause under `Environment` and never draws the
conclusion. Everything in this port — types, prototypes, variables, data variables, comments
— is that shape.

**Suggestion:** a `## Working across two databases` section in `guide.md` with the read-into-
locals-then-select-and-write pattern, and the two facts below (§2, §3) that make it work.

## 2. Core objects transfer between BinaryViews directly — undocumented, and a large win

`Type` and `Variable` objects from one view can be applied to another with no serialization:

```python
h.select(1); tobj = bv.get_type_by_name("ControllerFlashConfigShadow")
h.select(0); bv.define_user_type("ControllerFlashConfigShadow", tobj)   # works
```

All 121 types transferred this way with byte-exact widths, and `func.type = tobj` carried
return type, parameter names, and struct-pointer parameters intact.

I did not know this would work and nearly built a C-header round-trip through
`parse_types_from_string` instead. That path would have been slower, would have needed
dependency ordering, and would have silently dropped calling conventions and confidence
levels.

**Suggestion:** state it in the new cross-database section. It is the difference between a
five-call port and a fifty-call one.

## 3. `func.type = <string>` is the documented path and is the wrong one here

`guide.md` (`## Functions`) is emphatic:

> Assign the signature STRING, not a parsed Type: the setter only applies the name when it is
> given a string. Passing a parsed Type sets the prototype and silently leaves the function
> called sub_123456.

That is correct and worth keeping, but it steers you wrong for a port, where you have a real
`Type` object and no named prototype string. The working idiom is both, in order:

```python
f.type = tobj      # prototype, parameter names, calling convention
f.name = name      # name, which the type assignment does not set
```

**Suggestion:** keep the existing warning, and add the two-line form immediately after it as
the answer for when you already hold a `Type`.

## 4. No way to tell a real annotation from auto-analysis without three different predicates

The central question in any port, diff, or export is *which of this is human work?* The
answer uses a different API per object kind, and only one of the three is in the guide:

| Object | Predicate | In guide? |
|---|---|---|
| Symbol | `sym.auto` | yes, under Data variables |
| Data variable | `var.auto_discovered` | yes, same section |
| Function variable | `func.is_var_user_defined(var)` | **no** |
| Type | `bv.user_type_container.types` vs `bv.types` | **no** |

Without `is_var_user_defined` I had to guess. My first attempt filtered variable names against
a regex of Binary Ninja's auto-naming conventions and reported 766 candidates; my second
compared names against the bare database and reported 596. The real answer was 285. Both
heuristics were badly wrong, and had I trusted either I would have written several hundred
auto-generated names into the destination as user annotations — exactly the "a wrong name is
worse than no name" failure the guide warns about.

`bv.user_type_container.types` is also undocumented and non-obvious: it is keyed by UUID
string with `(name, type)` tuples as values, not by name.

**Suggestion:** a short `## User annotations vs auto-analysis` section with that table. This
is load-bearing for more than porting — any "summarize what's been done to this database"
question needs it.

## 5. The 30-second limit makes batch sizing a guess with a real cost

A timeout abandons the script and reverts it, so an over-large batch costs the full 30
seconds *and* produces nothing. There is no signal about remaining budget or typical
throughput, so I hedged: 75 functions per call, 120 data variables per call, three calls where
one might have done. Every one of those calls also re-ran the source-side collection (§6).

**Suggestion:** either surface elapsed time in the `execute` result so throughput can be
learned across calls, or put concrete numbers in `guide.md` — "applying ~75 function
prototypes with one `update_analysis_and_wait` fits comfortably" is the kind of figure that
turns guesswork into planning.

## 6. No state between calls means re-deriving the source data every batch

Each of the three prototype batches re-selected the source database and re-walked all 219
functions to rebuild the same dictionary, then threw it away. Same for the two data-variable
batches. For a port, the source-side read is pure overhead repeated N times.

The guide notes the filesystem is available, but frames it as a way to read the binary or SDK
headers, not as a workaround for the stateless execution model. Writing the collected
annotations to JSON in a scratch file and reading it back per batch would have been strictly
better, and it did not occur to me until afterwards.

**Suggestion:** either a session-scoped scratch dict in the `execute` globals (`h.state`, a
plain dict that survives between calls), or an explicit note in `guide.md` that for multi-call
work you should stage intermediate results in a file. The first is a small change to
`executor.py` and removes a whole class of redundant work.

## 7. API surprises the guide's "calls that behave surprisingly" section should absorb

Each of these cost a round trip:

- **`len(bv)`** → `TypeError: object of type 'BinaryView' has no len()`. Natural thing to
  write when printing image size. Use `bv.start` / `bv.end`.
- **`Type.enumeration`** is a method here, not a property. `t.enumeration.members` fails with
  `'function' object has no attribute 'members'`; `t.members` is the working path. This bit me
  while reading an enum to name a value, which is a common operation.
- **`Segment.flags`** does not exist. Segments are otherwise undocumented in the guide, and
  `seg.data_offset` / `seg.data_length` are what you need to map file offsets to virtual
  addresses — which is the first thing you do after any raw-file diff.

**Suggestion:** all three are one-liners in the existing style of `guide.md`.

## 8. Two analysis behaviours that produce false differences when diffing builds

Not server bugs, but they cost real time and a guide note would pay for itself, since
"compare these two binaries" is an obvious use of a tool that can hold two open at once.

- **`__builtin_memcpy` folding is register-allocation-dependent.** Binary Ninja collapses a
  run of five consecutive constant byte stores into a `memcpy` call. Because the two builds
  allocated registers differently, it folded a *different* run in each — `0x20000448` in one
  and `0x20000477` in the other. At MLIL and HLIL this reads as ten differing stores. The
  underlying bytes were identical. I only resolved it by dumping raw `MLIL_STORE` operations
  and comparing `(dest.value, src.value)` pairs, which is the technique that actually works
  for semantic diffing:

  ```python
  for il in f.mlil.instructions:
      if il.operation == bn.MediumLevelILOperation.MLIL_STORE:
          ...  # compare il.dest.value / il.src.value, not rendered text
  ```

- **`bv.address_comments` includes platform-imported comments.** The bare database reported
  1894 comments before I had written a single one — all SVD peripheral register bit-field
  descriptions in the `0x40000000` range. Counting comments to estimate how annotated a
  database is overstates it by an order of magnitude. Filter by address range, or the number
  is meaningless.

## 9. MCP tools were not registered at session start

`claude mcp list` reported the server connected, but `execute` and `binja_guide` were absent
from the client's tool list. They appeared only after I had already worked around it by
POSTing to `/mcp` directly with `httpx`. Likely a client-side registration race rather than a
server bug, but it is the first thing a user hits and it looks like the server is broken.

**Suggestion:** one line in `README.md` under **Use** — if the tools do not appear after
adding the server, reconnect the client; the endpoint being reachable is not sufficient.

---

## Not issues

Recorded so they are not re-investigated:

- The undo transaction behaved exactly as documented. Two scripts raised mid-run and both
  reverted cleanly, including the partial output printed before the exception, which was still
  returned and made the failure easy to read.
- Error reporting is good: real traceback, real line number in `<mcp>`, and the `print()`
  output that preceded the exception.
- `h.select()` holding its selection across calls meant no batch ever retargeted unexpectedly
  over a long multi-call sequence. This is the right default.
- 32 KB output cap was never hit, including a 141-line disassembly diff and a full HLIL dump.
