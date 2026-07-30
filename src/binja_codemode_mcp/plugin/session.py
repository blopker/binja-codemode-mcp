"""Which binary a call writes to.

The tab list is resolved per request rather than captured once, so a binary
opened later is reachable and a file that was closed and reopened simply works.

There is deliberately **no pinned target**. The write target arrives as a
parameter on every `execute` call, so nothing that decides where a write lands
survives between calls: no state to go stale, no state for a failed or abandoned
script to move, and no disagreement between the view the transaction was opened
on and the view the script can see.

Pure module: the tab list arrives through an injected provider, so this is
testable without Binary Ninja.
"""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any

RAW_VIEW = "Raw"


def same_view(a: Any, b: Any) -> bool:
    """Compare BinaryViews by value, never by `is`.

    Binary Ninja hands back a fresh Python wrapper around the same core handle
    on each call, which is why BinaryView defines __eq__ as a comparison of
    ctypes.addressof(self.handle.contents). Identity would make two references
    to one open binary look like two different binaries.
    """
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception:
        return False


@dataclass(frozen=True)
class BinaryTab:
    index: int
    name: str
    path: str
    bv: Any


def _native_id(bv: Any) -> int | None:
    """Binary Ninja's stable identifier for one open file session."""
    try:
        return int(bv.file.session_id)
    except Exception:
        return None


class BinaryNotFoundError(LookupError):
    """A target did not resolve to exactly one open binary."""


def analysed_view(bv: Any) -> Any:
    """Prefer an analysed view over Raw.

    A tab shows one view at a time and the user can switch it from the GUI.
    Handing back Raw because that is what they happen to be looking at gives the
    model a database with no functions, which reads as an empty binary rather
    than as the wrong view — so a stray click would silently make every lookup
    return None. More than one non-Raw view is a corner case; first wins.
    """
    try:
        if getattr(bv, "view_type", None) != RAW_VIEW:
            return bv
        meta = bv.file
        for name in meta.existing_views:
            if name != RAW_VIEW:
                found = meta.get_view_of_type(name)
                if found is not None:
                    return found
    except Exception:
        pass
    return bv


def _is_alive(bv: Any) -> bool:
    """Whether a view still has a live handle.

    Closing the underlying file disposes the view but leaves its tab open, so
    Binary Ninja keeps listing a binary whose every attribute raises. Reads that
    go through `.handle` are what notice — `bv.file.filename` still answers,
    which is why the tab looks fine.
    """
    try:
        _ = bv.view_type  # any read through `.handle` is enough
    except Exception:
        return False
    return True


class BinarySession:
    """Resolves a target name to one open binary."""

    def __init__(self, tabs_provider: Callable[[], list[BinaryTab]]) -> None:
        self._tabs_provider = tabs_provider
        self._ids: dict[int, str] = {}
        self._next_id = 1
        self._id_lock = Lock()

    def tabs(self) -> list[BinaryTab]:
        return self._tabs_provider()

    def identifier(self, tab: BinaryTab) -> str | None:
        """Short handle stable for this server process and open file session."""
        native = _native_id(tab.bv)
        if native is None:
            return None
        with self._id_lock:
            identifier = self._ids.get(native)
            if identifier is None:
                identifier = f"binary-{self._next_id}"
                self._next_id += 1
                self._ids[native] = identifier
            return identifier

    def describe(self) -> list[dict[str, Any]]:
        """Open binaries, for the guide header and the `h.binaries()` helper."""
        return [
            {
                "id": self.identifier(tab),
                "index": tab.index,
                "name": tab.name,
                "path": tab.path,
            }
            for tab in self.tabs()
        ]

    def resolve(self, key: Any = None) -> BinaryTab:
        """The tab a target names, or the only one open when target is omitted."""
        tabs = self.tabs()
        if not tabs:
            raise BinaryNotFoundError(
                "No binaries are open in Binary Ninja. Open a file and try again."
            )

        if key is None:
            if len(tabs) == 1:
                return self._analysed(tabs[0])
            raise BinaryNotFoundError(
                f"{len(tabs)} binaries are open, so `target` is required — this "
                f"call would otherwise have to guess where its writes land. "
                f"Open: {self._names(tabs)}. Pass one as the `target` parameter, "
                f'e.g. target="{tabs[0].name}".'
            )

        if isinstance(key, (bool, int)):
            # Indices are assigned by tab order, so dragging a tab silently
            # renames every target. Refuse rather than resolve something that
            # will mean a different binary tomorrow.
            raise BinaryNotFoundError(
                f"target must be a name or path, not the index {key!r} — indices "
                f"follow tab order and change when tabs are moved. "
                f"Open: {self._names(tabs)}."
            )

        needle = str(key)
        exact_ids = [t for t in tabs if self.identifier(t) == needle]
        if exact_ids:
            return self._analysed(exact_ids[0])

        matches = [t for t in tabs if needle in t.name or needle in t.path]
        if len(matches) > 1:
            raise BinaryNotFoundError(
                f"target {needle!r} matches several open binaries: "
                f"{self._names(matches)}. Use a longer name or a full path."
            )
        if not matches:
            raise BinaryNotFoundError(
                f"No open binary matches target {needle!r}. Open: {self._names(tabs)}."
            )
        return self._analysed(matches[0])

    def _analysed(self, tab: BinaryTab) -> BinaryTab:
        view = analysed_view(tab.bv)
        if not _is_alive(view):
            raise BinaryNotFoundError(
                f'The view for "{tab.name}" has been disposed: something closed '
                "the underlying file while its tab stayed open, most likely a "
                "script using the view as a context manager — `with bv:` calls "
                "BinaryView.__exit__, which closes the file. Nothing can reach "
                "it now. Close that tab in Binary Ninja and reopen it."
            )
        if view is tab.bv:
            return tab
        return BinaryTab(index=tab.index, name=tab.name, path=tab.path, bv=view)

    def _names(self, tabs: list[BinaryTab]) -> str:
        def label(tab: BinaryTab) -> str:
            identifier = self.identifier(tab)
            return f'"{tab.name}" ({identifier})' if identifier else f'"{tab.name}"'

        return ", ".join(label(t) for t in tabs)
