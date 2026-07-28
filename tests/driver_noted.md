# Live driver run — notes

Results of working through `driver_plan.md` against a real session.

- Date: 2026-07-27
- Binary Ninja 5.3.9757 Personal, macOS (aarch64)
- Targets: `scratch/driver/ls-a` (copy of `/bin/ls`, Mach-O fat, arm64e slice,
  133 functions), `scratch/driver/ls-b` (second copy)

## Results

| Case | Result | Case | Result | Case | Result |
| --- | --- | --- | --- | --- | --- |
| A1 | PASS | B1 | PASS | C1 | PASS |
| A2 | PASS | B2 | PASS | C2 | PASS |
| A3 | PASS | B3 | PASS | C3 | PASS |
| A4 | PASS | B4 | PASS | C4 | PASS |
| | | B5 | PASS | C5 | PASS |
| D1 | PASS | D3 | PASS | E1 | PASS |
| D2 | PASS | D4 | PASS | | |

Every atomicity claim held under the conditions it was written for:

- **B2** — three renames followed by `raise ValueError("boom")`. The tool reported the
  error with a traceback; a second call read all three addresses back as `sub_*`.
- **D2** — a rename followed by `time.sleep(45)`. The call failed at 30s, and after the
  abandoned script finished the entry function was still `_start`. Nothing committed
  after its call had already reported failure.
- **B4** — one ⌘Z reverted all five renames from a single batch; one ⌘⇧Z restored them.
- **B5** — the five names survived save, tab close, and reopening the `.bndb`. This is
  the failure the rewrite targeted.
- **C4** — `h.select("ls-b")` followed by a rename in the same script landed in `ls-b`
  (`driver_selected_here` at its entry point) and left `ls-a`'s `_start` untouched. This
  is the case that produced a wrong-database write in the previous live run.

No partial state was observed at any point.

## Findings

### 1. `binja_guide` does not recover from a closed target; `execute` does

Severity: moderate.

After B5 — closing the `ls-a` tab and reopening the `.bndb` — `binja_guide` returned a
header that contradicts itself:

```
No binary is open in Binary Ninja.
Binary Ninja 5.3.9757 Personal — API docs: api.binary.ninja (5.3)
Open tabs: [0] ls-a.arm64e.bndb
```

It reports nothing open while listing an open tab, and the tab carries no `(selected)`
marker.

`execute` against the identical state behaved correctly:

```
Error: The selected binary (ls-a.arm64e) is no longer open. Call h.binaries() and
h.select(<index>) to pick another. Selected [0] ls-a.arm64e.bndb instead — re-run your
script, or call h.select() to choose another.
```

The next `execute` succeeded. `binja_guide` only began reporting the binary *after* that
`execute` had done the re-selection.

This matters more than the severity suggests. `binja_guide` is documented as the first
call of a session, and reopening a file is exactly when a model reaches for it — so the
first thing it reads is a false statement that no binary is loaded, at the moment it is
least sure what it is pointing at. A model that believes it may take a recovery action
nobody wants. The re-selection logic in the execute path should also run for the guide.

Reproduced twice: once after the B5 reopen, once as the C5 closed-tab case.

### 2. The server logs a `socketserver` traceback on every client disconnect

Severity: low functionally, but it reads as a crash.

Observed at least twice during the run, both from the same client socket, on normal
connection teardown:

```
[ScriptingProvider] Exception occurred during processing of request from ('127.0.0.1', 56250)
[ScriptingProvider] Traceback (most recent call last):
...
[ScriptingProvider]   File ".../http/server.py", line 401, in handle_one_request
[ScriptingProvider]     self.raw_requestline = self.rfile.readline(65537)
[ScriptingProvider] ConnectionResetError: [Errno 54] Connection reset by peer
```

`_Handler.log_message` is overridden in `src/binja_codemode_mcp/plugin/server.py:46`, but
nothing overrides `ThreadingHTTPServer.handle_error`, which is what prints this. A client
hanging up a keep-alive socket is not an error condition; the traceback looks like a
plugin crash to the user and buries real messages in the Log pane.

### 3. The 100 KB output cap is measured in bytes, not tokens

Severity: usability.

`print("x" * 500000)` truncated cleanly at exactly 100,000 bytes with a clear note:

```
... (truncated at 100000 bytes; filter or paginate before printing)
```

The case passes — no hang, no dropped response. But 100 KB is roughly 25k tokens, past a
typical client's per-result budget. The driving client spilled the result to a file
rather than showing it, so the output was unavailable inline. The cap prevents the hang
it was designed to prevent; it does not guarantee a usable response.

## Guide gaps

Things that had to be found by trial and error, in rough order of how much they cost:

- **Searching HLIL by address.** The guide shows only `str(func.hlil)`. For the consumer
  function in E1 that was 12,245 characters with no way to locate the instruction at a
  known address. What works is iterating `func.hlil.instructions` and filtering on
  `il.address`. This is the single most useful missing idiom — it cost three of E1's
  eight round trips.
- **Reading a C string at a pointer.** `bv.get_ascii_string_at(ptr, 1)`, where the second
  argument is a minimum length that has to be lowered to catch short strings. The guide
  has no string section at all, yet string tables are among the most common things worth
  typing.
- **`bv.sections` is a dict**, so listing them needs `.values()`. Enumerating sections is
  the natural first move on an unfamiliar binary.
- **`bv.get_comment_at(addr)`** for verification. Only `set_comment_at` is documented, in
  a guide that otherwise insists every write be read back.
- **`bv.get_data_refs`** alongside the documented `get_code_refs`. A pointer table's
  references are often data refs, so following only code refs can miss the consumer.

Nothing was assumed unavailable and worked around this run; the explicit "you have the
filesystem" note covered A4 directly.

## Guide claims verified

Both documented gotchas behave exactly as written:

- `bv.parse_types_from_string(...)` returns a `BasicTypeParserResult`. Tuple-unpacking it
  raises `TypeError: cannot unpack non-iterable BasicTypeParserResult object`.
- `func.type = <parsed Type>` sets the prototype and leaves the name alone;
  `func.type = "<signature string>"` sets both. Confirmed on the same function in
  sequence: the name stayed `driver_test_0` after the parsed `Type`, and became
  `guide_claim_check` after the string.

`bv.parse_type_string` returns a `QualifiedName` as its second value, as documented.
`get_data_var_at` at an interior address returned the enclosing array, as documented.

## E1 workflow

Followed the Types → Data variables → Comments sequence literally on `__const#7` in
`ls-a`, which turned out to hold the macOS ACL name table: 24-byte records of a name
pointer, an applies-to code, and a single-bit mask, with an all-zero record separating
the `acl_perm` names from the two inheritance flags.

Every documented call worked first time and returned the documented shape:
`parse_type_string` → `define_user_type` → width check (24, as expected) →
`undefine_data_var(blacklist=True)` over the 19 conflicting auto-discovered string
pointers → `parse_type_string("acl_name_entry_t const[20]")` →
`define_user_data_var(base, type, "acl_name_table")` → verify → `set_comment_at`. Keeping
the destructive call separate from the definitions, as the guide advises, made the
sequence easy to follow.

The one honest ambiguity — whether the applies-to field is a `uint64_t` or a `uint32_t`
with four bytes of padding — was recorded in the comment rather than guessed at, since
the consumer at `0x100002560` only takes the record address.

## Verdict

Worth trusting with a database that matters. The three hardest cases — a mid-batch
exception, a timed-out script still running after its call reported failure, and select
plus edit in one script — all behaved correctly, and no partial write was seen anywhere.

The reservation is finding 1. Correct behaviour in the execute path is not much use if
the orientation call a model makes right after a reopen tells it no binary is loaded.
