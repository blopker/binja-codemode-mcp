"""Which binary is the target of an execute call.

The tab list is resolved per request rather than captured once, so a binary
opened later is reachable and edits cannot land on a stale view. The session
pins its target, so a long analysis does not retarget when the user clicks
another tab.

Pure module: the tab list arrives through an injected provider, so this is
testable without Binary Ninja.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


def _same_view(a: Any, b: Any) -> bool:
    """Compare BinaryViews by value, never by `is`.

    Binary Ninja hands back a fresh Python wrapper around the same core handle
    on each call, which is why BinaryView defines __eq__ as a comparison of
    ctypes.addressof(self.handle.contents). Identity would make a pinned view
    look closed on the very next request.
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


class BinaryNotFoundError(LookupError):
    """Raised when a requested binary is not open."""


class BinarySession:
    """Tracks which open binary this MCP session is working on."""

    def __init__(self, tabs_provider: Callable[[], list[BinaryTab]]) -> None:
        self._tabs_provider = tabs_provider
        self._pinned: Any = None
        self._pinned_name: str | None = None
        self._switch: tuple[str, BinaryTab] | None = None

    def tabs(self) -> list[BinaryTab]:
        return self._tabs_provider()

    def current(self) -> BinaryTab | None:
        """The pinned tab, pinning the first open one on first use."""
        tabs = self.tabs()
        if not tabs:
            return None

        if self._pinned is not None:
            for tab in tabs:
                if _same_view(tab.bv, self._pinned):
                    return tab
            # Pinned view was closed. Surface that rather than silently
            # retargeting a different binary.
            raise BinaryNotFoundError(
                f"The selected binary ({self._pinned_name}) is no longer open. "
                f"Call h.binaries() and h.select(<index>) to pick another."
            )

        self._pin(tabs[0])
        return tabs[0]

    def repin(self) -> BinaryTab | None:
        """Drop the current pin and take the first open binary, if any.

        Used when the pinned binary was closed: retargeting silently would be
        wrong mid-analysis, but leaving the session permanently unusable is
        worse, so the switch is recorded for the caller to report.
        """
        dropped = self._pinned_name
        self._pinned = None
        self._pinned_name = None
        tabs = self.tabs()
        if not tabs:
            return None
        self._pin(tabs[0])
        if dropped is not None:
            self._switch = (dropped, tabs[0])
        return tabs[0]

    def pending_switch(self) -> tuple[str, BinaryTab] | None:
        """A retarget nobody has been told about yet. Does not clear it."""
        return self._switch

    def take_switch(self) -> tuple[str, BinaryTab] | None:
        """The same, claiming it — for whoever puts it in front of the model.

        Split from `pending_switch` so the guide can mention the switch without
        consuming it: the caller that matters is the one about to write, and a
        notice the guide swallowed is how a script ended up editing a database
        nobody chose.
        """
        switch, self._switch = self._switch, None
        return switch

    def select(self, key: int | str) -> BinaryTab:
        """Pin a binary by tab index or by (partial) name."""
        tabs = self.tabs()
        if not tabs:
            raise BinaryNotFoundError("No binaries are open in Binary Ninja.")

        match: BinaryTab | None = None
        if isinstance(key, int):
            for tab in tabs:
                if tab.index == key:
                    match = tab
                    break
        else:
            candidates = [t for t in tabs if key in t.name or key in t.path]
            if len(candidates) > 1:
                names = ", ".join(f"[{t.index}] {t.name}" for t in candidates)
                raise BinaryNotFoundError(f"{key!r} matches several tabs: {names}")
            if candidates:
                match = candidates[0]

        if match is None:
            names = ", ".join(f"[{t.index}] {t.name}" for t in tabs)
            raise BinaryNotFoundError(f"No open binary matches {key!r}. Open: {names}")

        self._pin(match)
        return match

    def describe(self) -> list[dict[str, Any]]:
        """Tab list for the guide header and the `h.binaries()` helper."""
        return [
            {
                "index": tab.index,
                "name": tab.name,
                "path": tab.path,
                "selected": _same_view(tab.bv, self._pinned),
            }
            for tab in self.tabs()
        ]

    def _pin(self, tab: BinaryTab) -> None:
        self._pinned = tab.bv
        self._pinned_name = tab.name
