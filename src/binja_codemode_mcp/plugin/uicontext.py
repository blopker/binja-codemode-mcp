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

from .session import BinaryTab, same_view


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
            if bv is None or any(same_view(bv, s) for s in seen):
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
            if bv is not None and not any(same_view(bv, s) for s in seen):
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


def rebase_current_view(view: Any, address: int) -> Any:
    """Rebase the tab containing ``view`` and return its replacement view."""
    result: list[Any] = []
    error: list[str] = []

    def rebase() -> None:
        try:
            session_id = int(view.file.session_id)
            for ctx in UIContext.allContexts():
                tab = ctx.getTabForSessionId(session_id)
                if tab is None:
                    continue
                frame = ctx.getViewFrameForTab(tab)
                current = frame.getCurrentBinaryView() if frame else None
                if current is None or not same_view(current, view):
                    continue

                # rebaseCurrentView only operates on the active tab. It replaces
                # that tab's BinaryView and updates every associated UI object.
                ctx.activateTab(tab)
                if not ctx.rebaseCurrentView(address):
                    error.append("Binary Ninja refused the UI rebase.")
                    return

                new_tab = ctx.getTabForSessionId(session_id)
                new_frame = ctx.getViewFrameForTab(new_tab) if new_tab else None
                replacement = (
                    new_frame.getCurrentBinaryView() if new_frame is not None else None
                )
                if replacement is None:
                    error.append(
                        "Binary Ninja reported success but no replacement "
                        "view appeared."
                    )
                else:
                    result.append(replacement)
                return
            error.append("The selected BinaryView is not attached to an open UI tab.")
        except Exception as e:
            error.append(f"{type(e).__name__}: {e}")

    execute_on_main_thread_and_wait(rebase)
    if error:
        raise RuntimeError(error[0])
    if not result:
        raise RuntimeError("The UI rebase did not return a replacement BinaryView.")
    return result[0]
