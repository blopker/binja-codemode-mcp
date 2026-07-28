# Live driver plan

A script for an LLM connected to this server to run against a real Binary Ninja
session. It covers what the pytest suite cannot: the plugin never touches Binary Ninja
in the automated tests, so every claim about transactions, tab handling, and the UI is
unverified until something runs here.

Give this file to the driver, ask it to work through the cases in order, and have it
report using the format at the end. Cases marked **[human]** need someone looking at the
window; the driver should say what to check and stop for an answer.

## Setup

Do not run this against work you care about. Make throwaway copies, from the repo root:

```sh
rm -rf scratch/driver
mkdir -p scratch/driver
cp /bin/ls scratch/driver/ls-a
cp /bin/ls scratch/driver/ls-b
cp /bin/cat scratch/driver/other
```

`scratch/` is gitignored, so the copies and the `.bndb` files Binary Ninja writes next to
them stay out of the repo.

Open `scratch/driver/ls-a` in Binary Ninja, let analysis finish, and start the server
(`Plugins > Code Mode MCP > Start Server`). Leave `ls-b` and `other` closed for now.

**Never write GUI code.** Scripts run on a worker thread, and calling into Qt or
`binaryninjaui` from off the main thread crashes Binary Ninja and loses unsaved analysis.
Nothing in the plugin prevents it. If a case seems to need the interface, record it as a
gap instead of reaching for `UIContext`.

**Watch for the window jumping.** Binary Ninja coming to the foreground mid-run is under
investigation (section F). Whoever is at the keyboard should keep another window focused
between cases and note *which* calls pull Binary Ninja forward. This is the one thing
easier to see than to test.

The driver should treat any Python traceback, any tool error it did not expect, and any
disagreement between what a tool reports and what the UI shows as a failure worth
recording rather than working around.

---

## A. Orientation

**A0 [human] — the server starts with no file open.** Before opening anything, ask the
user to click the status-bar indicator, confirm it reads Running, click it again to stop,
and confirm the Plugins menu lists all three Code Mode MCP entries with no binary loaded.
Then start the server and open `ls-a`. Skip if the session is already running.

**A1 — the guide describes the live session.** Call `binja_guide`.
Expect: the header names `ls-a`, a Mach-O view, an architecture, a non-zero function
count, "Analysis: complete", a Binary Ninja version, and one open tab marked selected.
Fail if any field reads `?`, `0`, `unknown`, or names a different binary.

**A2 — the globals are real.** One `execute`:
```python
print(type(bv).__module__, type(bv).__name__)
print(bn.core_version())
print(h.binaries())
```
Expect `binaryninja.binaryview BinaryView`, a version string, and a one-entry list with
`selected: True`. Fail if `bn` is `None` — that would mean the module never reached the
script.

**A3 — ordinary Python works.** One `execute` that imports `struct` and `re`, uses a
comprehension, and defines a nested function that reads a name bound at the top level.
Expect no `NameError`. This is the scoping bug that used to force `global` workarounds.

**A4 — the filesystem is reachable.** Read the raw file and compare against the view:
```python
raw = bv.file.original_filename
with open(raw, "rb") as f:
    head = f.read(16)
print(raw, head.hex())
print(bv.read(bv.start, 16).hex())
```
Expect both to print. They need not match — the view is mapped, the file is not.

## B. Transactions — the reason this rewrite exists

**B1 — a successful batch lands.** Rename five `sub_*` functions to `driver_test_0..4`.
Read them back in a *second* `execute`. Expect the new names.

**B2 — a failing batch leaves nothing behind.** *The single most important case here.*
In one `execute`, rename three more functions and then `raise ValueError("boom")`. Expect
the tool to report the error. Then, in a second `execute`, read those three addresses
back: expect their **original** names.

This silently regressed once: an optimisation gated the revert on `bv.file.modified`,
which does not move when a script mutates, so failed scripts kept their changes. It was
found by hand, not by the suite. Any renamed function here is a partial-state failure and
the most serious possible result.

**B3 [human] — the UI reflects it, with nothing driving it.** Ask: does the function list
show `driver_test_0` without clicking away and back? This is now the *only* check on view
updates — the plugin used to force a refresh after every call and no longer does, on the
theory that undo-registered changes propagate on their own. A stale list here means that
theory is wrong and the refresh has to come back.

**B4 [human] — one undo step.** Ask the user to press ⌘Z once and say what happened.
Expect all five B1 renames to revert together. Five presses to undo five renames means
the batch is not one transaction. Ask them to ⌘⇧Z (redo) back before continuing.

**B5 [human] — it survives a save.** Ask the user to save (⌘S), close the tab, and
reopen the `.bndb`. Then call `binja_guide` and read the five names back. Expect them to
persist. This is the exact failure the rewrite targets: edits that looked applied and
vanished on save.

## C. Multiple binaries

**C1 — the target is stable across calls.** Run three `execute` calls in a row that each
print `bv.file.filename`. Expect the same path all three times. A "no longer open" error
on call two would mean the session cannot hold a target at all.

**C2 [human] — open a second binary.** Ask the user to open `scratch/driver/ls-b` in a
new tab and let it analyse. Then call `h.binaries()`: expect two entries, with `ls-a`
still `selected: True`.

**C3 — the pin survives the user switching tabs.** Ask the user to click the `ls-b` tab.
Without any `h.select`, run `print(bv.file.filename)`. Expect `ls-a` — the model's target
must not follow the user's focus.

**C4 — select and edit in one script.** This is the case that failed in the first live
run. One `execute`:
```python
print(h.select("ls-b"))
print(bv.file.filename)
bv.get_function_at(bv.entry_point).name = "driver_selected_here"
```
Expect the printed filename to be `ls-b` and the rename to land in `ls-b`. Verify in a
second call that `ls-a`'s entry function is untouched. A rename landing in `ls-a` while
the tool reports success is a wrong-database write — record it as critical.

**C5 [human] — a closed target recovers.** With `ls-b` selected, ask the user to close
the `ls-b` tab. Then `execute` anything. Expect one error saying the selected binary is
no longer open and naming the binary now selected, and expect the *next* `execute` to
succeed. A session that stays dead after this is a failure.

## D. Limits and failure modes

**D1 — output is capped, and the cap is usable.** `print("x" * 500000)`. Expect truncated
output with a note, no hang, no dropped response. Then answer the part that matters: did
the truncated result appear **inline**, or did the client spill it to a file? The cap
dropped from 100 KB to 32 KB precisely because 100 KB was being spilled and never read.
If 32 KB still spills, say so — the number needs to come down again.

**D2 — the timeout discards its work.** One `execute`:
```python
import time
bv.get_function_at(bv.entry_point).name = "driver_timeout_probe"
time.sleep(45)
```
Expect a timeout error after ~30s saying the batch was discarded. Then wait ~20s and, in
a new `execute`, read that function's name: expect the **original** name. If
`driver_timeout_probe` is there, an abandoned script committed after its call had already
reported failure.

**D3 — overlap is refused, not interleaved.** Immediately after starting D2 (while it is
still sleeping), send a second `execute`. Expect a clear "a previous script is still
running" error rather than a hang or a second transaction.

**D4 — a bad script fails cleanly.** Send a syntax error and an
`AttributeError`-producing line in separate calls. Expect useful messages, no server
crash, and the endpoint still responding afterwards.

**D5 — removing a function makes it stay removed.** The guide was corrected to say
`remove_user_function`, not `remove_function`, because the latter is an auto-level action
that analysis undoes. Verify on a real function:

```python
f = bv.get_functions_containing(bv.entry_point)[0]
addr, name = f.start, f.name
bv.remove_user_function(f)
print("gone?", bv.get_function_at(addr) is None)
bv.update_analysis_and_wait()
print("still gone after reanalysis?", bv.get_function_at(addr) is None)
print(addr, name)
```

Expect `True` both times. A `False` on the second means the removal did not stick and the
guide's advice is still wrong. Ask the user to ⌘Z afterwards, and confirm the function
returns.

**D6 — a failing script returns a readable error.** `raise ValueError("x" * 500_000)`.
Expect a bounded result that names `ValueError`, arrives **inline** rather than spilled to
a file, and ends with the timing footer. Section D covered the output cap but never a
large error, which is the gap that let an unbounded traceback ship.

**D7 — a rollback is stated.** Rename a function and then raise in the same script.
Expect the error to carry a rollback note, and a second call to confirm the original
name. Every failure reverts and every failure says so, including one that changed
nothing — there is no reliable way to tell, so the note is vacuous rather than absent.


## E. Guidance quality

**E1 — the guide's advice is followable.** Pick one workflow from `binja_guide` the
driver has not used and follow it literally. Four sections are new since the last run and
none has been exercised: *Working across two databases*, *User annotations vs
auto-analysis*, *Diffing two builds*, and *Strings, sections, and references*. The
cross-database port is the most valuable to try, and needs both `ls-a` and `ls-b` open. Along the way, use the idioms added after the last run and say
whether each did what the guide claims: locating one instruction via
`func.hlil.instructions` filtered on `il.address`, `bv.get_ascii_string_at(ptr, 1)`,
`bv.sections.values()`, `bv.get_comment_at`, and `bv.get_data_refs`. Record any step where the documented call errored, returned a
different shape than described, or needed an undocumented extra step.

**E2 — what was missing.** Ask the driver: what did you have to discover by trial and
error that the guide should have told you? What did you assume was unavailable and work
around? These answers are the point of the exercise — they become guide edits. Previous
runs surfaced `open()` this way, then HLIL-by-address, `get_ascii_string_at`, `sections`
being a mapping, `get_comment_at`, and `get_data_refs`; all are documented now, so a
run that needs none of them is a signal the guide is catching up.

**E3 — orientation after a reopen.** Immediately after B5's save/close/reopen, and again
after C5's tab close, call `binja_guide` *before* any `execute`. Expect the header to
name a binary and mark a tab `(selected)`. A header that says no binary is open while
listing one is a contradiction the model reads at the moment it is least sure of its
target.

## F. Interruption (known behaviour — confirm, do not investigate)

A failed script pulls Binary Ninja to the foreground. This is understood and accepted:
every failure reverts its undo transaction, and `revert_undo_actions` raises the window
even when the transaction recorded nothing. Isolated against a live instance — an empty
*commit* is silent, an empty *revert* pops. Skipping the revert is what caused the B2
regression, so it stays.

Keep another application focused and confirm the pattern still matches. Report F1–F3 as
a three-line yes/no table.

**F1 — a read on a clean file.** `print(len(bv.functions))`. Expect **no** pop: nothing
is reverted.

**F2 — a failure that changed nothing.** `print(bv.no_such_attribute)`. Expect a pop.
Annoying, documented, not a bug.

**F3 — a successful rename.** Expect at most one pop, from the commit and redraw.

Anything that pops *outside* this pattern — a `binja_guide` call, a read that settles
nothing — is new and worth reporting.


---

## Report format

Report F1–F4 as a four-line yes/no table; everything else as `PASS`, `FAIL`, or
`SKIPPED (reason)`. For a failure, give the exact code
sent, the exact response, and what was expected. Do not fix, retry differently, or work
around a failure before recording it — the workaround is the finding.

Finish with:

- Anything that behaved differently from what the guide or tool descriptions promised.
- Anything that cost more round trips than it should have.
- Whether you would trust this to make changes to a database you cared about, and why.

## Cleanup

Close the tabs in Binary Ninja first, then:

```sh
rm -rf scratch/driver
```

That removes the copies and any `.bndb` files written beside them.
