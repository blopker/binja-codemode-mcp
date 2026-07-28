"""Shared fakes.

Nothing here imports Binary Ninja. `bv` is duck-typed everywhere it is used, so
a small stand-in covers the whole plugin surface. Keep it small: the plugin is
supposed to hand the real BinaryView straight to `exec` rather than wrap it, and
a growing fake would be the first sign that had stopped being true.
"""

import pytest

from binja_codemode_mcp.plugin.session import BinaryTab


class _FileMetadata:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.original_filename = filename
        # Deliberately never flips: the real FileMetadata.modified does not
        # track script mutations, which a live probe established the hard way.
        self.modified: bool = False


class FakeBinaryView:
    """Just enough BinaryView to exercise the executor and backend."""

    def __init__(self, name: str = "target", functions: int = 3) -> None:
        self.file = _FileMetadata(f"/bin/{name}")
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
        self._snapshots: dict[str, list[str]] = {}

    # Mirrors binaryview.py's manual undo API, which is what the executor uses
    # so it can revert a batch that finished after its call already returned.
    def begin_undo_actions(self, anonymous_allowed: bool = True) -> str:
        self.transactions += 1
        state = f"state-{self.transactions}"
        self._snapshots[state] = list(self.renames)
        return state

    def commit_undo_actions(self, state: str) -> None:
        self._snapshots.pop(state, None)
        self.committed += 1

    def revert_undo_actions(self, state: str) -> None:
        self.renames = self._snapshots.pop(state, self.renames)
        self.reverted += 1

    def rename(self, name: str) -> None:
        self.renames.append(name)


@pytest.fixture
def bv() -> FakeBinaryView:
    return FakeBinaryView()


@pytest.fixture
def tabs(bv: FakeBinaryView) -> list[BinaryTab]:
    return [BinaryTab(index=0, name="target", path="/bin/target", bv=bv)]
