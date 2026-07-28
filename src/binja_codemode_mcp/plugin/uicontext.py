"""The Binary Ninja / Qt seam.

Everything that touches `binaryninjaui` lives here so the rest of the plugin
stays importable and testable without the host application.

Qt objects may only be touched on the main thread, so tab enumeration is
marshalled with execute_on_main_thread_and_wait. Do not call these while
holding a BinaryView lock.
"""

import contextlib
import os
from typing import Any

from binaryninja import execute_on_main_thread_and_wait

# binaryninjaui is a compiled extension with no type stubs, so pyright cannot
# see its members. Only imported from GUI-only call paths.
from binaryninjaui import UIContext  # type: ignore

from .session import BinaryTab, _same_view


def _tab_name(ctx: Any, tab: Any, bv: Any) -> str:
    # UIContext exposes getNameForTab(); getTabName() lives on ViewFrame.
    for getter in ("getNameForTab", "getTabName"):
        try:
            name = getattr(ctx, getter)(tab)
            if name:
                return str(name)
        except Exception:
            continue
    try:
        return os.path.basename(bv.file.filename)
    except Exception:
        return "<unknown>"


def _collect() -> list[BinaryTab]:
    """Walk every UI context and collect one entry per open BinaryView."""
    tabs: list[BinaryTab] = []
    seen: list[Any] = []

    for ctx in UIContext.allContexts():
        found_before = len(tabs)
        try:
            widgets = ctx.getTabs()
        except Exception:
            widgets = []

        for widget in widgets:
            try:
                frame = ctx.getViewFrameForTab(widget)
                bv = frame.getCurrentBinaryView() if frame else None
            except Exception:
                bv = None
            if bv is None or any(_same_view(bv, s) for s in seen):
                continue
            seen.append(bv)
            path = ""
            with contextlib.suppress(Exception):
                path = bv.file.filename or ""
            tabs.append(
                BinaryTab(
                    index=len(tabs),
                    name=_tab_name(ctx, widget, bv),
                    path=path,
                    bv=bv,
                )
            )

        # Fall back to the active view if this context yielded nothing usable —
        # no tabs, or getViewFrameForTab did not behave as expected. Degrading
        # to single-binary mode beats reporting "no binary open".
        if len(tabs) == found_before:
            try:
                frame = ctx.getCurrentViewFrame()
                bv = frame.getCurrentBinaryView() if frame else None
            except Exception:
                bv = None
            if bv is not None and not any(_same_view(bv, s) for s in seen):
                seen.append(bv)
                path = getattr(getattr(bv, "file", None), "filename", "") or ""
                tabs.append(
                    BinaryTab(
                        index=len(tabs),
                        name=os.path.basename(path) or "<unknown>",
                        path=path,
                        bv=bv,
                    )
                )

    return tabs


def list_tabs() -> list[BinaryTab]:
    """Open binaries, newest context last. Safe to call from any thread."""
    result: list[list[BinaryTab]] = []

    def gather() -> None:
        try:
            result.append(_collect())
        except Exception:
            result.append([])

    execute_on_main_thread_and_wait(gather)
    return result[0] if result else []
