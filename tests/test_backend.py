"""Wiring of session, executor, helpers and guide behind one backend."""

import pytest

from binja_codemode_mcp.config import Config
from binja_codemode_mcp.plugin.backend import PluginBackend, render_status_report
from binja_codemode_mcp.plugin.session import BinaryTab


@pytest.fixture
def config(tmp_path):
    return Config(api_key="k", data_dir=tmp_path)


@pytest.fixture
def backend(config, tabs):
    return PluginBackend(config, tabs_provider=lambda: tabs)


class TestExecute:
    def test_runs_against_the_selected_binary(self, backend, bv):
        result = backend.execute("bv.rename('parse')")
        assert result.success
        assert bv.renames == ["parse"]

    def test_helpers_can_list_binaries(self, backend):
        result = backend.execute("print([b['name'] for b in h.binaries()])")
        assert result.output.strip() == "['target']"

    def test_helpers_can_switch_binary(self, config, bv):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)

        backend.execute("h.select('other')")
        backend.execute("bv.rename('from_other')")
        assert other.renames == ["from_other"]
        assert bv.renames == []

    def test_select_rebinds_bv_within_the_same_script(self, config, bv):
        """Selecting and then editing in one script is the obvious thing to
        write; if `bv` still pointed at the old binary the edit would land in
        the wrong database and still report success."""
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)

        result = backend.execute("h.select('other')\nbv.rename('landed')")
        assert result.success
        assert other.renames == ["landed"]
        assert bv.renames == []

    def test_closing_the_pinned_binary_is_recoverable(self, config, bv):
        """The session must not be permanently dead: the script does not run
        when the pin is stale, so advice to call h.select() is unactionable."""
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        open_tabs = [list(tabs)]
        backend = PluginBackend(config, tabs_provider=lambda: open_tabs[0])
        backend.execute("pass")  # pins "target"

        open_tabs[0] = [BinaryTab(0, "other", "/bin/other", other)]
        first = backend.execute("bv.rename('a')")
        assert not first.success
        assert "no longer open" in (first.error or "")
        assert "other" in (first.error or "")

        second = backend.execute("bv.rename('b')")
        assert second.success, "the session must be usable again"
        assert other.renames == ["b"]

    def test_no_binary_open_is_a_clean_error(self, config):
        backend = PluginBackend(config, tabs_provider=list)
        result = backend.execute("print(1)")
        assert not result.success
        assert "No binary selected" in (result.error or "")

    def test_refresh_hook_fires_only_on_success(self, config, tabs):
        calls: list[int] = []
        backend = PluginBackend(
            config, tabs_provider=lambda: tabs, on_mutation=lambda: calls.append(1)
        )
        backend.execute("pass")
        assert calls == [1]
        backend.execute("raise ValueError('x')")
        assert calls == [1]


class TestModuleGlobal:
    def test_bn_reaches_the_script(self, config, tabs):
        """`bn` is one of three advertised globals; nothing else in the suite
        passes bn_module, so dropping the wiring would go unnoticed."""
        marker = type("FakeBN", (), {"core_version": staticmethod(lambda: "5.3.9757")})
        backend = PluginBackend(config, tabs_provider=lambda: tabs, bn_module=marker)
        result = backend.execute("print(bn.core_version())")
        assert result.output.strip() == "5.3.9757"

    def test_binja_version_reaches_the_guide_header(self, config, tabs):
        marker = type("FakeBN", (), {"core_version": staticmethod(lambda: "5.3.9757")})
        backend = PluginBackend(config, tabs_provider=lambda: tabs, bn_module=marker)
        assert "5.3.9757" in backend.guide(None)


class TestStatus:
    def test_describes_the_binary(self, backend):
        binary = backend.status()["binary"]
        assert binary["name"] == "target"
        assert binary["arch"] == "aarch64"
        assert binary["functions"] == 3
        assert binary["start"] == "0x100000000"
        assert binary["analysis"] == "complete"

    def test_survives_a_binary_view_that_raises(self, config):
        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError("nope")

        tabs = [BinaryTab(0, "broken", "", Hostile())]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        assert backend.status()["binary"]["name"] == "broken"

    def test_no_binary_open(self, config):
        backend = PluginBackend(config, tabs_provider=list)
        status = backend.status()
        assert status["binary"] is None
        assert status["tabs"] == []


class TestGuide:
    def test_includes_the_live_header(self, backend):
        assert "target" in backend.guide(None)

    def test_topic_narrows_the_output(self, backend):
        assert len(backend.guide("Types")) < len(backend.guide(None))


class TestStatusReport:
    """Show Status previously wrote only to the Log pane, so with that pane
    closed the menu item looked broken."""

    def test_not_running_says_how_to_start(self):
        out = render_status_report(None, None, None)
        assert "NOT RUNNING" in out
        assert "Start Server" in out

    def test_running_shows_a_pasteable_connect_command(self):
        out = render_status_report("http://127.0.0.1:9/mcp", "k3y", [])
        assert "http://127.0.0.1:9/mcp" in out
        assert "k3y" in out
        assert "claude mcp add --transport http" in out

    def test_running_with_no_binary_open_says_so(self):
        assert "No binaries are open." in render_status_report("e", "k", [])

    def test_marks_the_selected_binary(self):
        out = render_status_report(
            "e",
            "k",
            [
                {"index": 0, "name": "ls", "selected": False},
                {"index": 1, "name": "libfoo", "selected": True},
            ],
        )
        assert "  * [1] libfoo" in out
        assert "    [0] ls" in out
