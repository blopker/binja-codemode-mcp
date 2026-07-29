"""Resolving a target name to exactly one open binary."""

import pytest

from binja_codemode_mcp.plugin.session import (
    BinaryNotFoundError,
    BinarySession,
    BinaryTab,
)
from conftest import FakeBinaryView


def make_tabs(*specs: tuple[str, str]) -> list[BinaryTab]:
    return [
        BinaryTab(index=i, name=name, path=path, bv=FakeBinaryView(name))
        for i, (name, path) in enumerate(specs)
    ]


class TestResolve:
    def test_a_single_open_binary_needs_no_target(self):
        tabs = make_tabs(("ls", "/bin/ls"))
        assert BinarySession(lambda: tabs).resolve().name == "ls"

    def test_two_open_binaries_refuse_to_guess(self):
        """Guessing would put a write in a database nobody chose, which is the
        one failure the target parameter exists to make impossible."""
        tabs = make_tabs(("ls-a", "/tmp/ls-a"), ("ls-b", "/tmp/ls-b"))
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve()
        assert "`target` is required" in str(e.value)

    def test_the_refusal_names_the_candidates_and_shows_the_fix(self):
        """It costs a round trip, so it has to be actionable in one read."""
        tabs = make_tabs(("ls-a", "/tmp/ls-a"), ("ls-b", "/tmp/ls-b"))
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve()
        message = str(e.value)
        assert '"ls-a"' in message and '"ls-b"' in message
        assert 'target="ls-a"' in message

    def test_resolve_by_name(self):
        tabs = make_tabs(("ls-a", "/tmp/ls-a"), ("ls-b", "/tmp/ls-b"))
        assert BinarySession(lambda: tabs).resolve("ls-b").name == "ls-b"

    def test_resolve_by_path_fragment(self):
        tabs = make_tabs(("firmware", "/builds/v2/firmware.bin"))
        assert BinarySession(lambda: tabs).resolve("v2").name == "firmware"

    def test_an_index_is_refused_with_the_reason(self):
        """Indices follow tab order, so dragging a tab silently retargets every
        later call. Refusing teaches; resolving would be a time bomb."""
        tabs = make_tabs(("ls-a", "/tmp/ls-a"), ("ls-b", "/tmp/ls-b"))
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve(1)
        assert "not the index" in str(e.value)
        assert "tab order" in str(e.value)

    def test_an_ambiguous_name_lists_the_candidates(self):
        tabs = make_tabs(("fw-1.2", "/b/fw-1.2"), ("fw-1.3", "/b/fw-1.3"))
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve("fw")
        assert '"fw-1.2"' in str(e.value) and '"fw-1.3"' in str(e.value)

    def test_an_unknown_name_lists_what_is_open(self):
        tabs = make_tabs(("ls", "/bin/ls"))
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve("nope")
        assert '"ls"' in str(e.value)

    def test_nothing_open_says_so(self):
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(list).resolve()
        assert "No binaries are open" in str(e.value)

    def test_a_reopened_file_just_works(self):
        """Nothing is cached between calls, so the dead-handle problem a pinned
        target used to have cannot arise."""
        open_tabs = [make_tabs(("ls", "/bin/ls"))]
        session = BinarySession(lambda: open_tabs[0])
        assert session.resolve("ls").name == "ls"
        open_tabs[0] = make_tabs(("ls", "/bin/ls"))  # closed and reopened
        assert session.resolve("ls").name == "ls"


class TestViewType:
    """A tab shows one view at a time and the user can switch it."""

    def test_a_raw_tab_resolves_to_the_analysed_view(self):
        """Otherwise a stray click in the GUI hands the model a database with no
        functions, which reads as an empty binary rather than the wrong view."""
        raw = FakeBinaryView("ls", functions=0, view_type="Raw")
        macho = raw.add_view(FakeBinaryView("ls", functions=133, view_type="Mach-O"))
        tabs = [BinaryTab(index=0, name="ls", path="/bin/ls", bv=raw)]

        resolved = BinarySession(lambda: tabs).resolve("ls")
        assert resolved.bv is macho
        assert len(resolved.bv.functions) == 133

    def test_an_analysed_tab_is_left_alone(self):
        macho = FakeBinaryView("ls", view_type="Mach-O")
        tabs = [BinaryTab(index=0, name="ls", path="/bin/ls", bv=macho)]
        assert BinarySession(lambda: tabs).resolve("ls").bv is macho

    def test_a_raw_only_file_stays_raw(self):
        raw = FakeBinaryView("blob", view_type="Raw")
        tabs = [BinaryTab(index=0, name="blob", path="/tmp/blob", bv=raw)]
        assert BinarySession(lambda: tabs).resolve("blob").bv is raw


class TestDescribe:
    def test_lists_every_open_binary(self):
        tabs = make_tabs(("ls-a", "/tmp/ls-a"), ("ls-b", "/tmp/ls-b"))
        described = BinarySession(lambda: tabs).describe()
        assert [d["name"] for d in described] == ["ls-a", "ls-b"]
        assert described[0]["path"] == "/tmp/ls-a"

    def test_nothing_open_is_an_empty_list(self):
        assert BinarySession(list).describe() == []


class TestDisposedView:
    """Closing a view's file disposes it but leaves the tab open, so Binary
    Ninja keeps listing a binary that raises on every access. Confirmed live:
    `with bv as v: pass` in the console left the tab visible and the view dead
    for the rest of the session."""

    class _Disposed:
        """Reads through `.handle` raise; `.file` still answers.

        That asymmetry is what makes the tab look healthy — `h.binaries()`
        reported the name and path of a view that raised on everything else.
        """

        def __init__(self, name: str) -> None:
            self.file = type("F", (), {"filename": f"/bin/{name}"})()

        @property
        def view_type(self):
            raise ReferenceError("BinaryView has been disposed")

    def _dead(self):
        return self._Disposed("ls-a")

    def test_a_disposed_view_is_refused_with_the_cause_and_the_cure(self):
        tabs = [BinaryTab(index=0, name="ls-a", path="/bin/ls-a", bv=self._dead())]
        with pytest.raises(BinaryNotFoundError) as e:
            BinarySession(lambda: tabs).resolve("ls-a")
        message = str(e.value)
        assert "disposed" in message
        assert "with bv" in message, "the cause is worth naming; it is not obvious"
        assert "reopen" in message, "and it is only recoverable by hand"

    def test_it_is_refused_when_it_is_the_only_binary_too(self):
        """The single-binary path skips the name match, so it needs the same
        check — otherwise the common case is the one that fails obscurely."""
        tabs = [BinaryTab(index=0, name="ls-a", path="/bin/ls-a", bv=self._dead())]
        with pytest.raises(BinaryNotFoundError):
            BinarySession(lambda: tabs).resolve()
