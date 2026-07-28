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


class TestStaleTargetRecovery:
    """Reopening a file is exactly when a model calls binja_guide, so the
    orientation path must recover a closed target, not just execute()."""

    def _reopened(self, config, bv):
        other = type(bv)("reopened")
        open_tabs = [[BinaryTab(0, "target", "/bin/target", bv)]]
        backend = PluginBackend(config, tabs_provider=lambda: open_tabs[0])
        backend.execute("pass")  # pins "target"
        open_tabs[0] = [BinaryTab(0, "reopened", "/bin/reopened", other)]
        return backend

    def test_status_does_not_claim_nothing_is_open(self, config, bv):
        status = self._reopened(config, bv).status()
        assert status["binary"] is not None
        assert status["binary"]["name"] == "reopened"

    def test_the_guide_header_agrees_with_its_own_tab_list(self, config, bv):
        header = self._reopened(config, bv).guide(None)
        assert "No binary is open" not in header
        assert "(selected)" in header


class TestGuide:
    def test_includes_the_live_header(self, backend):
        assert "target" in backend.guide(None)

    def test_topic_narrows_the_output(self, backend):
        assert len(backend.guide("Types")) < len(backend.guide(None))


class TestTargetSwitchNotice:
    """Closing the pinned tab re-pins, and whichever call arrives first
    consumes that. When it is `binja_guide` — which is exactly what the guide
    tells a model to do after a tab closes — the notice was thrown away and the
    next script wrote to a database nobody chose, silently.
    """

    def _pin_then_close(self, config, bv):
        other = type(bv)("other")
        open_tabs = [
            [
                BinaryTab(0, "target", "/bin/target", bv),
                BinaryTab(1, "other", "/bin/other", other),
            ]
        ]
        backend = PluginBackend(config, tabs_provider=lambda: open_tabs[0])
        backend.execute('h.select("other")')
        open_tabs[0] = [BinaryTab(0, "target", "/bin/target", bv)]
        return backend

    def test_the_guide_header_reports_the_switch(self, config, bv):
        header = self._pin_then_close(config, bv).guide(None)
        assert "no longer open" in header
        assert "other" in header

    def test_a_script_after_a_guide_call_is_still_refused_once(self, config, bv):
        backend = self._pin_then_close(config, bv)
        backend.guide(None)
        result = backend.execute("bv.rename('should_not_land')")
        assert not result.success
        assert "no longer open" in (result.error or "")
        assert bv.renames == []

    def test_the_notice_is_delivered_once_not_forever(self, config, bv):
        backend = self._pin_then_close(config, bv)
        backend.guide(None)
        backend.execute("pass")
        result = backend.execute("bv.rename('lands')")
        assert result.success
        assert bv.renames == ["lands"]

    def test_the_direct_path_does_not_report_it_twice(self, config, bv):
        backend = self._pin_then_close(config, bv)
        assert not backend.execute("bv.rename('a')").success
        assert backend.execute("bv.rename('b')").success
        assert bv.renames == ["b"]

    def test_an_untouched_session_reports_no_switch(self, config, bv):
        backend = PluginBackend(
            config, tabs_provider=lambda: [BinaryTab(0, "target", "/bin/target", bv)]
        )
        backend.execute("pass")
        assert "no longer open" not in backend.guide(None)
        assert backend.execute("bv.rename('fine')").success


class TestLibrary:
    """`h.lib`: functions saved for the rest of the server session.

    Functions rather than values, so there is never an "is this still true?"
    question — a saved function re-derives against whatever is live now.
    """

    def _two_binaries(self, config, bv):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        return PluginBackend(config, tabs_provider=lambda: tabs)

    def test_a_saved_function_sees_the_binary_selected_now(self, config, bv):
        """The whole design. A stored function resolves globals from the call
        that defined it, so without rebinding on retrieval this runs against
        the first call's `bv` forever — the staleness it exists to remove."""
        backend = self._two_binaries(config, bv)
        backend.execute(
            'def where():\n    return bv.file.filename\nh.lib["where"] = where'
        )

        first = backend.execute("print(h.lib.where())")
        backend.execute('h.select("other")')
        second = backend.execute("print(h.lib.where())")

        assert first.output.strip() == "/bin/target"
        assert second.output.strip() == "/bin/other"

    def test_a_saved_function_prints_into_the_running_script(self, backend):
        """Its `print` must be this call's, not the closed budget of the call
        that defined it — where the output would vanish silently."""
        backend.execute(
            'def shout():\n    print("from the library")\nh.lib["shout"] = shout'
        )
        assert "from the library" in backend.execute("h.lib.shout()").output

    def test_select_inside_a_saved_function_rebinds_its_own_bv(self, config, bv):
        """The guide's own cross-database example calls h.select() *inside* the
        saved function. Each retrieval gets its own globals dict, so a select
        that only wrote to the calling script's scope left the function reading
        the binary it started on — and the read half of a port then returns an
        empty result the caller has no reason to distrust."""
        backend = self._two_binaries(config, bv)
        backend.execute(
            "def where():\n"
            '    h.select("other")\n'
            "    return bv.file.filename\n"
            'h.lib["where"] = where'
        )
        assert backend.execute("print(h.lib.where())").output.strip() == "/bin/other"

    def test_select_inside_a_saved_function_also_moves_the_caller(self, config, bv):
        """One selection per session, not one per scope: the script must not
        carry on writing to the old binary after its helper switched."""
        backend = self._two_binaries(config, bv)
        backend.execute('def go():\n    h.select("other")\nh.lib["go"] = go')
        result = backend.execute("h.lib.go()\nprint(bv.file.filename)")
        assert result.output.strip() == "/bin/other"

    def test_repeated_retrieval_does_not_accumulate_scopes(self, backend):
        """A loop calling a saved function is ordinary; one globals dict per
        call would grow without bound inside a single script."""
        backend.execute('def noop():\n    return 1\nh.lib["noop"] = noop')
        backend.execute("for _ in range(50):\n    h.lib.noop()")
        assert len(backend.helpers.lib._bound) <= 1

    def test_saved_functions_call_each_other_through_lib(self, backend):
        backend.execute('def base():\n    return 6\nh.lib["base"] = base')
        backend.execute(
            'def doubled():\n    return h.lib.base() * 2\nh.lib["doubled"] = doubled'
        )
        assert backend.execute("print(h.lib.doubled())").output.strip() == "12"

    def test_a_saved_function_keeps_the_imports_it_was_defined_with(self, backend):
        """Rebinding replaces the function's globals wholesale, so without
        carrying these the most ordinary script there is — import at the top,
        use it in the function — raises NameError on the next call."""
        backend.execute(
            "import json\n"
            'def dump():\n    return json.dumps({"a": 1})\nh.lib["dump"] = dump'
        )
        result = backend.execute("print(h.lib.dump())")
        assert result.success, result.error
        assert result.output.strip() == '{"a": 1}'

    def test_a_saved_function_keeps_the_constants_it_was_defined_with(self, backend):
        backend.execute(
            "THRESHOLD = 10\n"
            "def over():\n    return [v for v in (5, 15) if v > THRESHOLD]\n"
            'h.lib["over"] = over'
        )
        assert backend.execute("print(h.lib.over())").output.strip() == "[15]"

    def test_a_saved_function_does_not_hold_the_defining_calls_binary(self, backend):
        """Keeping the function object would pin its __globals__ — that call's
        BinaryView, live long after the user closes the binary, plus every
        large intermediate the script happened to leave at the top level."""
        backend.execute(
            'big = list(range(50000))\ndef tiny():\n    return 1\nh.lib["tiny"] = tiny'
        )
        captured = backend.helpers.lib._entries["tiny"].captured
        assert "bv" not in captured
        assert "big" not in captured

    def test_the_calling_script_cannot_redefine_what_a_saved_function_means(
        self, backend
    ):
        """Only the live globals come from the caller. Otherwise a name the
        caller happened to bind would silently change the saved function."""
        backend.execute(
            'LIMIT = 1\ndef limit():\n    return LIMIT\nh.lib["limit"] = limit'
        )
        assert backend.execute("LIMIT = 99\nprint(h.lib.limit())").output.strip() == "1"

    def test_a_name_the_saved_function_never_had_stays_missing(self, backend):
        """It has to fail the same way whatever the caller has bound. If the
        calling scope showed through, a saved function would quietly mean
        something different depending on which script called it."""
        backend.execute(
            "def uses_missing():\n    return MISSING\n"
            'h.lib["uses_missing"] = uses_missing'
        )
        result = backend.execute("MISSING = 5\nprint(h.lib.uses_missing())")
        assert not result.success
        assert "MISSING" in (result.error or "")

    def test_reassigning_a_name_replaces_it(self, backend):
        backend.execute('def f():\n    return 1\nh.lib["f"] = f')
        backend.execute('def f():\n    return 2\nh.lib["f"] = f')
        assert backend.execute("print(h.lib.f())").output.strip() == "2"

    def test_the_key_names_the_entry_not_the_def(self, backend):
        result = backend.execute(
            'def collect():\n    return 7\nh.lib["port"] = collect\nprint(h.lib.port())'
        )
        assert result.output.strip() == "7"

    def test_a_lambda_can_be_saved(self, backend):
        """The key supplies the name, so `<lambda>` is no obstacle."""
        result = backend.execute(
            'h.lib["double"] = lambda n: n * 2\nprint(h.lib.double(21))'
        )
        assert result.output.strip() == "42"

    def test_an_entry_may_be_named_like_a_mapping_method(self, backend):
        """Any named method on the namespace would permanently shadow an entry
        of that name, because __getattr__ only fires when lookup fails."""
        result = backend.execute('h.lib["keys"] = lambda: "mine"\nprint(h.lib.keys())')
        assert result.output.strip() == "mine"

    def test_source_comes_back_verbatim(self, backend):
        backend.execute(
            'def probe():\n    """Doc."""\n    return 1\nh.lib["probe"] = probe'
        )
        out = backend.execute("print(h.lib.probe.source)").output
        assert "def probe():" in out
        assert '"""Doc."""' in out

    def test_source_is_snapshotted_at_save_time(self, backend):
        """Read lazily instead, it would be lost whenever the defining script's
        text is gone — which is every path where linecache cannot be restored."""
        backend.execute('def keeper():\n    return 1\nh.lib["keeper"] = keeper')
        saved = backend.helpers.lib._entries["keeper"]
        assert "def keeper():" in saved.source

    def test_attribute_assignment_saves_a_real_entry(self, backend):
        """The obvious spelling. Falling through to the instance __dict__ would
        skip every check and shadow the entry for attribute reads while the
        subscript form and the footer still saw the saved function."""
        result = backend.execute(
            'def probe():\n    return "saved"\n'
            "h.lib.probe = probe\nprint(h.lib.probe())"
        )
        assert result.output.strip() == "saved"
        assert result.lib == ("probe",)

    def test_attribute_assignment_is_validated_like_a_key(self, backend):
        assert not backend.execute("h.lib.bad = 5").success

    def test_the_librarys_own_state_cannot_be_reassigned_or_deleted(self, backend):
        """Losing `_entries` would fail every later call, including calls that
        never touch the library."""
        assert not backend.execute("del h.lib._entries").success
        assert not backend.execute("h.lib._entries = None").success
        assert not backend.execute("h.lib = {}").success
        assert backend.execute('print("still alive")').output.strip() == "still alive"

    def test_a_broken_library_cannot_take_the_result_down(self, backend):
        """Belt and braces for the footer: whatever state h.lib is left in, the
        script's own output still comes back."""
        object.__setattr__(backend.helpers.lib, "_entries", None)
        result = backend.execute('print("output survives")')
        assert result.output.strip() == "output survives"
        assert result.lib == ()

    def test_a_keyword_is_refused_as_a_name(self, backend):
        """`h.lib["class"]` would be reachable by subscript only."""
        assert not backend.execute('h.lib["class"] = lambda: 1').success

    def test_a_traceback_inside_an_old_saved_function_quotes_its_source(self, backend):
        """The defining script has long since aged out of the source cache, so
        the entry has to be republished on retrieval or the frame that raised
        comes back as a bare line number."""
        backend.execute(
            'def boom():\n    raise ValueError("inside")\nh.lib["boom"] = boom'
        )
        for i in range(20):
            backend.execute(f"x = {i}")
        result = backend.execute("h.lib.boom()")
        assert not result.success
        assert 'raise ValueError("inside")' in (result.error or "")

    def test_lib_sources_returns_every_definition(self, backend):
        backend.execute('def a():\n    return 1\nh.lib["a"] = a')
        backend.execute('def b():\n    return 2\nh.lib["b"] = b')
        out = backend.execute("print(h.lib_sources())").output
        assert "def a():" in out
        assert "def b():" in out

    def test_the_listing_shows_signature_and_docstring(self, backend):
        backend.execute(
            'def unported(src=1):\n    """Names that differ."""\n'
            '    return src\nh.lib["unported"] = unported'
        )
        out = backend.execute("print(h.lib)").output
        assert "h.lib.unported(src=1)" in out
        assert "Names that differ." in out

    def test_an_empty_listing_says_how_to_save(self, backend):
        assert 'h.lib["' in backend.execute("print(h.lib)").output

    def test_del_removes_by_attribute_and_by_key(self, backend):
        result = backend.execute(
            'h.lib["a"] = lambda: 1\nh.lib["b"] = lambda: 2\n'
            'del h.lib.a\ndel h.lib["b"]\nprint(len(h.lib))'
        )
        assert result.output.strip() == "0"

    def test_deleting_an_unknown_name_is_an_error(self, backend):
        assert not backend.execute("del h.lib.nope").success

    def test_an_unknown_name_lists_what_is_saved(self, backend):
        backend.execute('h.lib["real"] = lambda: 1')
        result = backend.execute("h.lib.nope()")
        assert not result.success
        assert "AttributeError" in (result.error or "")
        assert "real" in (result.error or "")

    def test_an_unknown_name_leaves_hasattr_usable(self, backend):
        """Attribute access must raise AttributeError, not the KeyError a bare
        delegation to __getitem__ would leak — hasattr() propagates the latter."""
        result = backend.execute('print(hasattr(h.lib, "nope"))')
        assert result.output.strip() == "False"

    def test_a_captured_value_is_refused(self, backend):
        """A closure freezes the defining call's `bv` inside the function —
        precisely the staleness storing functions is meant to avoid."""
        result = backend.execute(
            "def outer():\n    captured = bv\n    def inner():\n"
            "        return captured\n    return inner\n"
            'h.lib["inner"] = outer()'
        )
        assert not result.success
        assert "captur" in (result.error or "").lower()

    def test_a_function_from_another_module_is_refused(self, backend):
        """Rebinding it would strip the globals its own module needs."""
        result = backend.execute('import json\nh.lib["dumps"] = json.dumps')
        assert not result.success
        assert "defined in your script" in (result.error or "")

    def test_a_non_function_is_refused(self, backend):
        assert not backend.execute('h.lib["c"] = 5').success
        assert not backend.execute('h.lib["c"] = len').success
        assert not backend.execute('h.lib["c"] = str').success

    def test_a_private_name_is_refused(self, backend):
        """Underscore names are where the namespace keeps its own state."""
        assert not backend.execute('h.lib["_entries"] = lambda: 1').success

    def test_a_failed_script_keeps_its_definitions(self, backend):
        """A definition is not a database change: the undo transaction reverts
        the binary, and leaving the definition is what lets the next call fix
        the caller without re-emitting the function."""
        result = backend.execute('h.lib["kept"] = lambda: 1\nraise ValueError("boom")')
        assert not result.success
        assert backend.execute("print(len(h.lib))").output.strip() == "1"

    def test_saved_names_ride_back_on_the_result(self, backend):
        """The footer is the only thing that keeps the library visible; without
        it the model forgets what it is holding."""
        backend.execute('h.lib["one"] = lambda: 1')
        assert backend.execute("pass").lib == ("one",)


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
