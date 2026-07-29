# Live driver plan

You are the driver. Work through every case below, in order, against the `binja` MCP
server. Do not skip one, and do not fix, retry differently, or work around a failure
before recording it.

This covers what the pytest suite cannot: the plugin never touches Binary Ninja in the
automated tests, so every claim about transactions, tab handling, and the UI is unverified
until something runs here.

The human is needed twice — **Setup** and **Checkpoint** — and not in between. Ask for
each of those as a single message and wait; do not scatter other questions through the run.

**Never write GUI code.** Scripts run on a worker thread; calling into Qt or
`binaryninjaui` crashes Binary Ninja and loses unsaved analysis. Nothing prevents it. If a
case seems to need the interface, record it as a gap.

Record any traceback, unexpected tool error, or disagreement between a tool and the UI as
a failure rather than working around it. The workaround is the finding.

Pass `topic` to `binja_guide` except where told otherwise — the whole guide is ~27 KB,
two thirds of a result, and one section is usually what you want.

Cases leave this plan once the suite covers them and several runs have passed them, so
what is here is what pytest cannot reach: real transactions, real undo, real analysis, tab
handling, the client's own behaviour, and whether the guide is followable. Deliberately
absent, and not to be added back — Python scoping and builtins, reading the file on disk,
the rollback note's wording, the status bar and menu entries, and `h.lib`'s ordinary
refusals. All are unit-tested and all have passed every run.

---

## Setup — everything the human does, first

`make driver` has already cleared `scratch/driver/` and put fresh copies of `/bin/ls`
there as `ls-a` and `ls-b`. Do not recreate them, and do not clean up at the end: the
*next* run clears them, so a run that stops partway leaves its databases intact to
inspect. Reports at the `scratch/` root are left alone either way.

Ask the human for all of this in one message:

1. Open `scratch/driver/ls-a` **and** `scratch/driver/ls-b`, let both finish analysing.
2. Start the server.

Report both, then leave it alone until the Checkpoint.

---

## A. Orientation

**A1 — the guide describes the live session.** Call `binja_guide` with no `topic` (the one
case that wants the whole document). Expect a Mach-O view, an architecture, a non-zero
function count, "Analysis: complete", a version, and two open tabs with one marked
named. Any `?`, `0`, or `unknown` is a failure. Nothing is marked selected — every call
names its own `target`, and the header is where you learn which names are valid.

**A2 — the globals are real.** `print(type(bv).__module__, type(bv).__name__)`,
`bn.core_version()`, `h.binaries()`. Expect `binaryninja.binaryview BinaryView`, a version,
and two entries. `bn` as `None` means the module never reached the script.

## B. Transactions — the reason this rewrite exists

**B1 — a successful batch lands.** Rename five `sub_*` functions to `driver_test_0..4`,
**all in one `execute` call** — the grouping in B3 depends on it. Read them back in a
*second* call.

**B2 — a failing batch leaves nothing behind.** *The most important case here.* In one
call, rename three more functions and then `raise ValueError("boom")`. In a second call,
read those three addresses back: expect their **original** names.

This regressed silently once — an optimisation gated the revert on `bv.file.modified`,
which does not move when a script mutates — and was found by hand, not by the suite. Any
renamed function here is a partial-state failure and the most serious possible result.

**B3 — one undo step.** `bv.undo()` from a script is what ⌘Z does. First confirm you are
about to undo the right thing — `[len(e.actions) for e in bv.file.undo_entries][-3:]`
should show B1's call as a single entry of **five** actions. Then undo once and read the
five names back: expect all five reverted **together**, and `bv.redo()` to restore them.

A previous run recorded a FAIL here that did not reproduce: five renames spread over more
than one call give one entry each, which looks identical to broken grouping from the
undo side. Check the actions count before concluding anything.

## C. Multiple binaries

**C1 — no target is refused, not guessed.** With both open, send any script *without* a
`target`. Expect an error naming both candidates and showing the parameter. A call that
picks one for you is the wrong-database write this design exists to prevent.

**C2 — the target decides where writes land.** With `target="ls-b"`:

```python
print(bv.file.filename)
bv.get_function_at(bv.entry_point).name = "driver_target_here"
```

Expect `ls-b`, and verify in a second call (`target="ls-a"`) that `ls-a`'s entry function
is untouched. A rename landing in `ls-a` while the tool reports success is critical.

**C3 — the source is readable and not writable.** With `target="ls-a"`:

```python
src = h.read_only_view("ls-b")
print(src.file.filename, len(src.functions))
print(src.get_function_at(src.entry_point).name)     # reading is fine
```

Expect `ls-b` and its function count. Then, in a separate call, try to write through it —
`h.read_only_view("ls-b").get_function_at(...).name = "nope"`. Expect the call to **fail**,
the message to name `ls-b`, and a third call to confirm the name did not change. Also
confirm `h.read_only_view("ls-a")` while targeting `ls-a` is refused with a message
pointing at `bv`.

**C4 — a Type object crosses views.** A freshly-analysed Mach-O has no user types, so
first define one in `ls-b` with `target="ls-b"`. Then target `ls-a`, read that type from
`ls-b` through `h.read_only_view`, and apply it with `bv.define_user_type`. This is the
reason both views are live in one call rather than one per call; if it fails, say how.

## D. Limits and failure modes

**D1 — output is capped, and the cap is usable.** `print("x" * 500000)`. Expect truncated
output with a note. Then the part that matters: did it arrive **inline**, or did the client
spill it to a file? The cap dropped from 100 KB to 32 KB because 100 KB was being spilled
and never read. If 32 KB still spills, say so.

**D2 — the timeout discards its work, and overlap is handled.** First check the ordinary
collision: two quick calls issued together should **both succeed**, because a second call
waits a few seconds for the first rather than being refused outright. Then the real case —
one call that renames the entry function to `driver_timeout_probe` then `time.sleep(45)`.
While it sleeps, send a second call: expect it to wait, then report that a script is still
running *and name the binary it is running on*, not to hang. Expect the first to
time out at ~30s saying the batch was discarded. Wait ~20s, then read the name back: expect
the **original**. `driver_timeout_probe` surviving means an abandoned script committed
after its call had already reported failure.

**D3 — failures are clean and readable.** Three calls: a syntax error, an `AttributeError`,
and `raise ValueError("x" * 500_000)`. Expect useful messages, a bounded result naming
`ValueError` that arrives inline rather than spilled, the timing footer intact, and the
endpoint still serving. The large error is the gap that let an unbounded traceback ship.

**D4 — a removed function stays removed.** `remove_user_function`, not `remove_function`:
the latter is an auto-level action analysis undoes. Remove a real function, check
`get_function_at` is `None`, `update_analysis_and_wait()`, check again. Expect `True` both
times, then `bv.undo()` and confirm it returns.

Then check what the undo cost: count `is_var_user_defined` across the restored function.
Undoing a removal is known to leave two `void*` arguments falsely flagged, permanently.
Confirm the guide's warning still matches what you see, and report if the count is anything
other than two.

## E. Guidance quality

**E1 — the guide's advice is followable.** Pick a workflow from `binja_guide` you have not
used and follow it literally. Untested since they were written: *Working across two
databases* (the most valuable — needs both tabs, so do it before the Checkpoint), *User
annotations vs auto-analysis*, and *Diffing two builds*. Also check the four claims added
after the last run, each measured rather than guessed:

- the HLIL **window** rather than `il.address == addr` (61% of disassembly addresses have
  no HLIL instruction of their own)
- `bv.strings` floors at `analysis.limits.minStringLength`, default 4
- `seg.data_offset` is slice-relative — reading `/bin/ls` at it lands in the other slice
- chained fixups are resolved by `bv.read` and encoded in the file on disk

**E2 — what was missing.** What did you discover by trial and error that the guide should
have told you? What did you assume was unavailable and work around? These become guide
edits. A run needing none of the previously surfaced idioms is the signal it has caught up.

## G. The per-session library

New and unproven. The question is not "does it work" — the suite covers that — but **does
a model reach for it unprompted**.

**G1 — save, reuse, inspect.** Define a function, save it with `h.lib["name"] = fn`, call
it in a later call, and confirm the footer lists it. Then `print(h.lib)`,
`h.lib.<name>.source`, `h.lib_sources()`, `del h.lib.<name>`.

**G2 — saved functions follow the call's target.** Save a function returning
`bv.file.filename`. Call it with `target="ls-a"`, then with `target="ls-b"`: the answers
must differ, without the function being redefined. Then save one that takes a view as a
parameter and pass `h.read_only_view(...)` to it. Also confirm a saved function's
`print()` output comes back in the calling script's result.

**G3 — what it carries, and the refusal that matters.** A script that does `import json`
at the top and saves a function using it must still work several calls later, and so must
one reading a top-level constant — those are carried deliberately.

Then the refusal worth checking by hand, because it is the shape the closure message
recommends: `def where(src=bv): ...` saved into `h.lib`. It must be refused, naming the
default argument — a view held that way would still point at this call's binary when the
function is later run against another target. Confirm the message says to take the view as
a parameter, and that an ordinary default (`def top(limit=5)`) is still accepted.

**G4 — the dump is self-sufficient.** Save a function that uses a top-level `import` and a
constant, then read `h.lib_sources()`. It is advertised as what you paste into a new
session, so the import and the constant must appear alongside the `def` — a previous run
found only the bodies, which raise `NameError` on first use.

**G5 — error quality.** Failures now report the *line* that raised, not just its number.
Confirm on an ordinary failure, then on a raise inside a function saved 15+ calls earlier —
the second is where the source has to be republished from the library.

**G6 — was it worth it?** Did you save anything without being told to? Call a saved
function more than once? If you re-emitted the same code across calls instead of saving it,
say so — that is the outcome that would retire the feature.

---

## Checkpoint — the only interruption

Ask for all of this in one message, then stop.

1. Does the function list show `driver_test_0` **without** clicking away and back? This is
   the only remaining check on view updates: the plugin used to force a refresh and no
   longer does, on the theory that undo-registered changes propagate on their own.
2. Close the **`ls-b`** tab.
3. Save `ls-a` (⌘S), close its tab, reopen the `.bndb`.
4. Glance at the log pane: every call should have logged what it was doing and which
   binary, then its verdict and elapsed time. Ask whether the failures said "rolled back".
5. While a script is running, do **not** edit the database — a failed script reverts to
   where its transaction opened and would take your edit with it. The status bar says
   `⚠️ MCP: running script Ns. Do not edit` while one holds it; D2 runs for 30 seconds, so
   confirm the warning appears and then leave it alone.

## F. After the Checkpoint

**F1 — a closed binary is simply gone.** `ls-b` is closed. A call with `target="ls-b"`
must fail saying no open binary matches, and list what is open. A call targeting `ls-a`
must work immediately — there is no pin to recover, so nothing should need a retry.

**F2 — edits survive a save.** Read the five `driver_test_*` names back from the reopened
`ls-a`. This is the exact failure the rewrite targets: edits that looked applied and
vanished on save.

---

## Report format

Write the report to `scratch/driver-run-<timestamp>.md` — `date +%Y-%m-%d-%H%M` for the
timestamp — and summarise it in chat. At the scratch root, not `scratch/driver/`, which
the next `make driver` wipes. `scratch/` is gitignored; the findings worth keeping get
copied out and folded into `guide.md`.

`PASS`, `FAIL`, or `SKIPPED (reason)` per case. For a failure give the exact code sent,
the exact response, and what was expected.

Finish with:

- Anything that behaved differently from what the guide or tool descriptions promised.
- Anything that cost more round trips than it should have.
- Whether you would trust this to make changes to a database you cared about, and why.

Leave the tabs and the databases as they are. `make driver` clears them at the start of
the next run, which is what makes a half-finished run inspectable.
