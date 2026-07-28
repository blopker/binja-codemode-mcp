"""Shared fakes.

Nothing here imports Binary Ninja. `bv` is duck-typed everywhere it is used, so
a small stand-in covers the whole plugin surface. Keep it small: the plugin is
supposed to hand the real BinaryView straight to `exec` rather than wrap it, and
a growing fake would be the first sign that had stopped being true.
"""

import contextlib
from typing import Any

import pytest

from binja_codemode_mcp.plugin.session import BinaryTab


class FakeBinaryView:
    """Just enough BinaryView to exercise the executor and backend."""

    def __init__(self, name: str = "target", functions: int = 3) -> None:
        self.file = type("FileMetadata", (), {"filename": f"/bin/{name}"})()
        self.view_type = "Mach-O"
        self.arch = type("Arch", (), {"name": "aarch64"})()
        self.platform = type("Platform", (), {"name": "macos-aarch64"})()
        self.functions = [object()] * functions
        self.start = 0x100000000
        self.end = 0x100004000
        self.entry_point = 0x100001000
        self.analysis_progress = type("Progress", (), {"state": 2})()

        self.transactions = 0
        self.committed = 0
        self.reverted = 0
        self.renames: list[str] = []

    @contextlib.contextmanager
    def undoable_transaction(self) -> Any:
        """Mirrors the real contract: an exception reverts the whole batch."""
        self.transactions += 1
        before = list(self.renames)
        try:
            yield
        except BaseException:
            self.renames = before
            self.reverted += 1
            raise
        self.committed += 1

    def rename(self, name: str) -> None:
        self.renames.append(name)


@pytest.fixture
def bv() -> FakeBinaryView:
    return FakeBinaryView()


@pytest.fixture
def tabs(bv: FakeBinaryView) -> list[BinaryTab]:
    return [BinaryTab(index=0, name="target", path="/bin/target", bv=bv)]
