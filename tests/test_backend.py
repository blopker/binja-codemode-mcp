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

    def test_no_binary_open_is_a_clean_error(self, config):
        backend = PluginBackend(config, tabs_provider=list)
        result = backend.execute("print(1)")
        assert not result.success
        assert "No binaries are open" in (result.error or "")


class TestTargeting:
    """The write target arrives with the call, so a write can never land in a
    database the caller did not name."""

    def _two(self, config, bv):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        return PluginBackend(config, tabs_provider=lambda: tabs), other

    def test_the_target_names_where_writes_land(self, config, bv):
        backend, other = self._two(config, bv)
        assert backend.execute("bv.rename('landed')", "other").success
        assert other.renames == ["landed"]
        assert bv.renames == []

    def test_two_binaries_without_a_target_is_refused(self, config, bv):
        backend, other = self._two(config, bv)
        result = backend.execute("bv.rename('nope')")
        assert not result.success
        assert "`target` is required" in (result.error or "")
        assert bv.renames == [] and other.renames == []

    def test_the_write_target_is_the_one_in_a_transaction(self, config, bv):
        """The bug this replaced: the transaction was opened on whichever view
        happened to be pinned, so a write elsewhere had no transaction at all
        and a raise reverted the untouched database."""
        backend, other = self._two(config, bv)
        result = backend.execute("bv.rename('gone')\nraise ValueError('boom')", "other")
        assert not result.success
        assert other.transactions == 1 and other.reverted == 1
        assert other.renames == []
        assert bv.transactions == 0

    def test_a_reopened_binary_needs_no_recovery(self, config, bv):
        open_tabs = [[BinaryTab(0, "target", "/bin/target", bv)]]
        backend = PluginBackend(config, tabs_provider=lambda: open_tabs[0])
        assert backend.execute("pass").success
        open_tabs[0] = [BinaryTab(0, "target", "/bin/target", type(bv)("target"))]
        assert backend.execute("bv.rename('after')").success


class TestLogging:
    """The log is where the user watches what is being done to their database."""

    def _backend(self, config, bv):
        lines: list[str] = []
        tabs = [BinaryTab(0, "ls-a", "/bin/ls-a", bv)]
        backend = PluginBackend(config, tabs_provider=lambda: tabs, log=lines.append)
        return backend, lines

    def test_a_call_announces_itself_before_running(self, config, bv):
        """Said up front, not only on the way out: a script that never returns
        would otherwise leave no trace of what it was doing."""
        backend, lines = self._backend(config, bv)
        backend.execute("bv.rename('x')", None, "rename five functions")
        assert "running on ls-a — rename five functions" in lines[0]

    def test_the_end_reports_the_verdict_and_elapsed(self, config, bv):
        backend, lines = self._backend(config, bv)
        backend.execute("pass")
        assert "ok in " in lines[-1]

    def test_a_failure_says_it_rolled_back(self, config, bv):
        backend, lines = self._backend(config, bv)
        backend.execute("bv.rename('x')\nraise ValueError('boom')")
        assert "failed, rolled back" in lines[-1]

    def test_a_refused_target_is_logged_too(self, config, bv):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "ls-a", "/bin/ls-a", bv),
            BinaryTab(1, "ls-b", "/bin/ls-b", other),
        ]
        lines: list[str] = []
        backend = PluginBackend(config, tabs_provider=lambda: tabs, log=lines.append)
        backend.execute("pass")
        assert any("refused" in line for line in lines)

    def test_a_logger_that_raises_cannot_take_a_call_down(self, config, bv):
        def boom(_message):
            raise RuntimeError("log died")

        tabs = [BinaryTab(0, "ls-a", "/bin/ls-a", bv)]
        backend = PluginBackend(config, tabs_provider=lambda: tabs, log=boom)
        assert backend.execute("print('fine')").output.strip() == "fine"


class TestRunningScript:
    """The status indicator's only source of truth. A failed script reverts the
    database to where its transaction opened, taking any edit the user made in
    the meantime with it; nothing can prevent that, so telling them while it
    matters is the whole mitigation."""

    def test_idle_reports_nothing(self, backend):
        assert backend.running_script() is None

    def test_a_running_script_reports_its_target_and_age(self, config, bv):
        import threading
        import time

        tabs = [BinaryTab(0, "ls-a", "/bin/ls-a", bv)]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)

        worker = threading.Thread(
            target=lambda: backend.execute("import time\ntime.sleep(0.25)"),
            daemon=True,
        )
        worker.start()
        time.sleep(0.1)
        live = backend.running_script()
        worker.join(5)

        assert live is not None, "the indicator had nothing to warn about"
        target, elapsed = live
        assert target == "ls-a"
        assert 0.0 < elapsed < 5.0
        assert backend.running_script() is None, "cleared once the script ends"


class TestReadOnlyView:
    """One view is writable; the other is named for what it is."""

    def _two(self, config, bv, watcher=None):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        backend = PluginBackend(
            config, tabs_provider=lambda: tabs, watcher_factory=watcher
        )
        return backend, other

    def test_reads_from_the_other_binary(self, config, bv):
        backend, other = self._two(config, bv)
        result = backend.execute(
            'src = h.read_only_view("other")\nprint(src.file.filename)', "target"
        )
        assert result.output.strip() == "/bin/other"

    def test_both_views_are_live_in_one_call(self, config, bv):
        """Type objects cross views directly and cannot survive between calls,
        so a port needs both in scope at once."""
        backend, other = self._two(config, bv)
        result = backend.execute(
            'src = h.read_only_view("other")\n'
            "bv.rename(src.file.filename)\n"
            "print(bv.renames)",
            "target",
        )
        assert result.success
        assert bv.renames == ["/bin/other"]

    def test_asking_for_the_target_is_refused(self, config, bv):
        backend, other = self._two(config, bv)
        result = backend.execute('h.read_only_view("target")', "target")
        assert not result.success
        assert "this call's target" in (result.error or "")

    def test_the_target_guard_matches_the_view_not_just_the_name(self, config, bv):
        """Two tabs can carry the same display name — a build opened twice, or
        a file reopened alongside itself. Guarding on the name alone would
        refuse a legitimate read; guarding on the view is what the rule means."""
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "shared", "/a/shared", bv),
            BinaryTab(1, "shared", "/b/shared", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        # Ambiguous by name, so each has to be reached by its path.
        result = backend.execute(
            'src = h.read_only_view("/b/shared")\nprint(src.file.filename)',
            "/a/shared",
        )
        assert result.success, result.error
        assert result.output.strip() == "/bin/other"

    def test_a_clean_read_commits_silently(self, config, bv):
        """An empty commit is silent where an empty revert raises the Binary
        Ninja window, so the ordinary two-database call costs nothing."""
        backend, other = self._two(config, bv, watcher=_never_written)
        assert backend.execute('h.read_only_view("other")', "target").success
        assert other.committed == 1
        assert other.reverted == 0

    def test_a_write_through_it_is_rolled_back_and_fails_the_call(self, config, bv):
        backend, other = self._two(config, bv, watcher=_always_written)
        result = backend.execute(
            'src = h.read_only_view("other")\nsrc.rename("sneaky")', "target"
        )
        assert not result.success
        assert "read-only" in (result.error or "")
        assert "other" in (result.error or "")
        assert other.reverted == 1
        assert other.renames == []


def _never_written(view):
    return (lambda: False, lambda: None)


def _always_written(view):
    return (lambda: True, lambda: None)


class TestUndoApiFailures:
    """The undo API can raise. Nothing here may leave the lock held or report a
    script that did not run as a success."""

    def _backend(self, config, view):
        tabs = [BinaryTab(0, "target", "/bin/target", view)]
        return PluginBackend(config, tabs_provider=lambda: tabs)

    def test_a_failure_to_open_is_reported_not_swallowed(self, config):
        from conftest import FakeBinaryView

        backend = self._backend(config, FakeBinaryView("t", raise_on=("begin",)))
        result = backend.execute("print('never runs')")
        assert not result.success
        assert "undo transaction" in (result.error or "")

    def test_a_failure_to_open_leaves_the_executor_usable(self, config):
        """It used to hold the lock for the life of the process, so every later
        call was refused against a thread that no longer existed."""
        from conftest import FakeBinaryView

        view = FakeBinaryView("t", raise_on=("begin",))
        backend = self._backend(config, view)
        backend.execute("pass")
        assert backend.executor.wait_for_idle(timeout=2)

        view.raise_on.clear()
        assert backend.execute("print('recovered')").output.strip() == "recovered"

    def test_a_failed_commit_is_not_reported_as_success(self, config):
        from conftest import FakeBinaryView

        backend = self._backend(config, FakeBinaryView("t", raise_on=("commit",)))
        result = backend.execute("bv.rename('x')")
        assert not result.success
        assert "commit" in (result.error or "")

    def test_a_failed_revert_says_so_rather_than_claiming_a_rollback(self, config):
        from conftest import FakeBinaryView

        backend = self._backend(config, FakeBinaryView("t", raise_on=("revert",)))
        result = backend.execute("raise ValueError('boom')")
        assert not result.success
        assert "revert" in (result.error or "")
        assert "inconsistent" in (result.error or "")


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
        binary = backend.status()["binaries"][0]
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
        assert backend.status()["binaries"][0]["name"] == "broken"

    def test_no_binary_open(self, config):
        backend = PluginBackend(config, tabs_provider=list)
        assert backend.status()["binaries"] == []


class TestGuide:
    def test_includes_the_live_header(self, backend):
        assert "target" in backend.guide(None)

    def test_topic_narrows_the_output(self, backend):
        assert len(backend.guide("Types")) < len(backend.guide(None))


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

    def test_a_saved_function_prints_into_the_running_script(self, backend):
        """Its `print` must be this call's, not the closed budget of the call
        that defined it — where the output would vanish silently."""
        backend.execute(
            'def shout():\n    print("from the library")\nh.lib["shout"] = shout'
        )
        assert "from the library" in backend.execute("h.lib.shout()").output

    def test_a_captured_helper_follows_the_calling_targets_view(self, config, bv):
        """A sibling function carries its own __globals__ — the defining call's
        scope, with that call's bv. Left alone it writes to the binary the
        library was written against, outside any transaction, and reports
        success. The one remaining way a write could escape its target."""
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        backend.execute(
            "def helper():\n"
            "    bv.rename('by_helper')\n"
            "    return bv.file.filename\n"
            "def port():\n"
            "    return helper()\n"
            'h.lib["port"] = port',
            "target",
        )
        result = backend.execute("print(h.lib.port())", "other")
        assert result.output.strip() == "/bin/other"
        assert other.renames == ["by_helper"]
        assert bv.renames == [], "the write must not land in the defining binary"

    def test_captured_helpers_can_still_call_each_other(self, backend):
        """They share one globals dict, so rebinding must not strand them."""
        result = backend.execute(
            "def a():\n    return 'a' + b()\n"
            "def b():\n    return 'b'\n"
            'h.lib["a"] = a\nprint(h.lib.a())'
        )
        assert result.output.strip() == "ab"

    def test_redefining_an_entry_mid_script_takes_effect(self, backend):
        """A per-entry globals cache made the first definition win, so fixing a
        saved function and re-testing it in one call silently ran the old one."""
        result = backend.execute(
            "A = 1\ndef f():\n    return A\n"
            'h.lib["f"] = f\nprint(h.lib.f())\n'
            "A = 2\ndef f2():\n    return A\n"
            'h.lib["f"] = f2\nprint(h.lib.f())'
        )
        assert result.output.split() == ["1", "2"]

    def test_the_live_globals_are_not_captured(self, backend):
        """Capturing bv would pin the defining call's view for the session."""
        backend.execute('def where():\n    return bv\nh.lib["where"] = where')
        captured = backend.helpers.lib._entries["where"].captured
        assert "bv" not in captured and "print" not in captured

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

    def test_lib_sources_carries_what_each_function_needs(self, backend):
        """It is advertised as what you paste into a new session, so bodies
        alone are not enough — a carried import or constant that is missing
        raises NameError on the first call."""
        backend.execute(
            "import json\n"
            "LIMIT = 7\n"
            "def summarise():\n"
            "    return json.dumps({'limit': LIMIT})\n"
            'h.lib["summarise"] = summarise'
        )
        dump = backend.execute("print(h.lib_sources())").output
        assert "import json" in dump
        assert "LIMIT = 7" in dump
        assert "def summarise():" in dump

    def test_lib_sources_names_what_it_cannot_write_back(self, backend):
        """Silently omitting it would produce a dump that looks complete and
        is not."""
        backend.execute(
            'HANDLE = object()\ndef uses():\n    return HANDLE\nh.lib["uses"] = uses'
        )
        dump = backend.execute("print(h.lib_sources())").output
        assert "HANDLE" in dump
        assert "re-supply this by hand" in dump

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

    def test_a_view_held_through_a_default_is_refused(self, backend):
        """`def f(src=bv)` is exactly what the closure refusal tells you to do
        instead, and it pins the defining call's view just as hard — worse now,
        since a saved function is meant to run against whatever the call
        targets."""
        result = backend.execute(
            'def where(src=bv):\n    return src.file.filename\nh.lib["where"] = where'
        )
        assert not result.success
        assert "BinaryView" in (result.error or "")
        assert "default argument" in (result.error or "")

    def test_a_view_held_through_a_top_level_name_is_refused(self, backend):
        result = backend.execute(
            "src = bv\ndef where():\n    return src.file.filename\n"
            'h.lib["where"] = where'
        )
        assert not result.success
        assert "top-level name 'src'" in (result.error or "")

    def test_a_view_held_through_an_annotation_is_refused(self, backend):
        result = backend.execute(
            'def where(x: bv = 1):\n    return x\nh.lib["where"] = where'
        )
        assert not result.success
        assert "annotation" in (result.error or "")

    def test_the_refusal_says_to_take_the_view_as_a_parameter(self, backend):
        """The message has to name the fix, or it just moves the guessing."""
        result = backend.execute(
            'def where(src=bv):\n    return src\nh.lib["where"] = where'
        )
        assert "h.read_only_view" in (result.error or "")

    def test_ordinary_defaults_are_still_accepted(self, backend):
        """The check must not cost the normal spelling."""
        result = backend.execute(
            "def top(limit=5, *, deep=False):\n    return (limit, deep)\n"
            'h.lib["top"] = top\nprint(h.lib.top())'
        )
        assert result.success
        assert result.output.strip() == "(5, False)"

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
