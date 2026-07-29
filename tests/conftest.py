"""Shared fakes.

Nothing here imports Binary Ninja. Views are duck-typed everywhere they are
used, so a small stand-in covers the whole plugin surface. Keep it small: the
plugin is supposed to hand the real BinaryView straight to `exec` rather than
wrap it, and a growing fake would be the first sign that had stopped being true.

What the fake *can* do is deliberate. It can fail from the undo API and it can
present several view types, because both are things the real one does and
neither was reachable before — a fake more forgiving than reality is how a
data-loss bug shipped once already.
"""

import pytest

from binja_codemode_mcp.plugin.session import BinaryTab


class _FileMetadata:
    def __init__(self, filename: str, views: dict[str, "FakeBinaryView"]) -> None:
        self.filename = filename
        self.original_filename = filename
        # Deliberately never flips: the real FileMetadata.modified does not
        # track script mutations, which a live probe established the hard way.
        self.modified: bool = False
        self._views = views

    @property
    def existing_views(self) -> list[str]:
        return list(self._views)

    def get_view_of_type(self, name: str) -> "FakeBinaryView | None":
        return self._views.get(name)


class FakeBinaryView:
    """Just enough BinaryView to exercise the executor and backend."""

    def __init__(
        self,
        name: str = "target",
        functions: int = 3,
        view_type: str = "Mach-O",
        raise_on: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.view_type = view_type
        self.raise_on = set(raise_on)
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
        self.file = _FileMetadata(f"/bin/{name}", {view_type: self})

    def add_view(self, view: "FakeBinaryView") -> "FakeBinaryView":
        """Give this file a second view type, as a real Mach-O/Raw pair has."""
        view.file = self.file
        self.file._views[view.view_type] = view
        return view

    def _maybe_raise(self, hook: str) -> None:
        if hook in self.raise_on:
            raise RuntimeError(f"{hook} failed")

    # Mirrors binaryview.py's manual undo API, which is what the executor uses
    # so it can revert a batch that finished after its call already returned.
    def begin_undo_actions(self, anonymous_allowed: bool = True) -> str:
        self._maybe_raise("begin")
        self.transactions += 1
        state = f"state-{self.transactions}"
        self._snapshots[state] = list(self.renames)
        return state

    def commit_undo_actions(self, state: str) -> None:
        self._maybe_raise("commit")
        self._snapshots.pop(state, None)
        self.committed += 1

    def revert_undo_actions(self, state: str) -> None:
        self._maybe_raise("revert")
        self.renames = self._snapshots.pop(state, self.renames)
        self.reverted += 1

    def rename(self, name: str) -> None:
        self.renames.append(name)

    def __eq__(self, other: object) -> bool:
        # By value, never identity: Binary Ninja hands back a fresh wrapper
        # around the same core handle on every call.
        return (
            isinstance(other, FakeBinaryView)
            and other.name == self.name
            and (other.view_type == self.view_type)
        )

    def __hash__(self) -> int:
        return hash((self.name, self.view_type))


@pytest.fixture
def bv() -> FakeBinaryView:
    return FakeBinaryView()


@pytest.fixture
def tabs(bv: FakeBinaryView) -> list[BinaryTab]:
    return [BinaryTab(index=0, name="target", path="/bin/target", bv=bv)]


@pytest.fixture
def two_tabs(bv: FakeBinaryView) -> list[BinaryTab]:
    other = FakeBinaryView("other")
    return [
        BinaryTab(index=0, name="target", path="/bin/target", bv=bv),
        BinaryTab(index=1, name="other", path="/bin/other", bv=other),
    ]
