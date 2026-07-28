"""Binary selection: which open binary a session targets, and when that changes."""

import pytest

from binja_codemode_mcp.plugin.session import (
    BinaryNotFoundError,
    BinarySession,
    BinaryTab,
)


def make_tabs(*names: str) -> list[BinaryTab]:
    return [
        BinaryTab(index=i, name=n, path=f"/bin/{n}", bv=object())
        for i, n in enumerate(names)
    ]


class TestPinning:
    def test_first_use_pins_the_first_open_binary(self):
        tabs = make_tabs("ls", "cat")
        session = BinarySession(lambda: tabs)
        current = session.current()
        assert current is not None and current.name == "ls"

    def test_selection_survives_the_user_switching_tabs(self):
        """A long analysis must not retarget under the model's feet."""
        tabs = make_tabs("ls", "cat")
        order = [tabs]
        session = BinarySession(lambda: order[0])
        session.select("cat")

        # User reorders tabs in the UI; the pinned view is still `cat`.
        order[0] = [
            BinaryTab(index=0, name="cat", path="/bin/cat", bv=tabs[1].bv),
            BinaryTab(index=1, name="ls", path="/bin/ls", bv=tabs[0].bv),
        ]
        current = session.current()
        assert current is not None and current.name == "cat"

    def test_closing_the_pinned_binary_is_reported_not_papered_over(self):
        tabs = make_tabs("ls", "cat")
        order = [tabs]
        session = BinarySession(lambda: order[0])
        session.select("cat")

        order[0] = [tabs[0]]
        with pytest.raises(BinaryNotFoundError, match="no longer open"):
            session.current()

    def test_no_open_binaries_returns_none(self):
        assert BinarySession(list).current() is None


class TestSelect:
    def test_select_by_index(self):
        session = BinarySession(lambda: make_tabs("ls", "cat"))
        assert session.select(1).name == "cat"

    def test_select_by_partial_name(self):
        session = BinarySession(lambda: make_tabs("ls", "libfoo.dylib"))
        assert session.select("libfoo").name == "libfoo.dylib"

    def test_ambiguous_name_lists_the_candidates(self):
        session = BinarySession(lambda: make_tabs("libfoo.dylib", "libfoobar.dylib"))
        with pytest.raises(BinaryNotFoundError, match="matches several"):
            session.select("libfoo")

    def test_unknown_name_lists_what_is_open(self):
        session = BinarySession(lambda: make_tabs("ls"))
        with pytest.raises(BinaryNotFoundError, match=r"\[0\] ls"):
            session.select("nope")

    def test_select_with_nothing_open(self):
        with pytest.raises(BinaryNotFoundError, match="No binaries are open"):
            BinarySession(list).select(0)


class TestDescribe:
    def test_marks_the_selected_binary(self):
        # One stable list: a real provider hands back the same live BinaryView
        # objects each call, and pinning is by object identity.
        tabs = make_tabs("ls", "cat")
        session = BinarySession(lambda: tabs)
        session.select("cat")
        described = session.describe()
        assert [d["selected"] for d in described] == [False, True]
        assert described[0]["name"] == "ls"
