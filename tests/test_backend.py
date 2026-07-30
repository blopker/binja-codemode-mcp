"""Wiring of session, executor, helpers and guide behind one backend."""

import logging
from concurrent.futures import ThreadPoolExecutor

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

    def test_helpers_list_stable_binary_ids(self, backend):
        result = backend.execute("print(h.binaries()[0]['id'])")
        assert result.output.strip().startswith("binary-")

    def test_complete_output_can_be_written_to_a_generated_file(
        self, backend, tmp_path
    ):
        result = backend.execute(
            "print('complete')",
            output_directory=str(tmp_path),
            output_extension="txt",
        )
        assert result.success
        assert result.artifact_path is not None
        assert result.artifact_status == "success"
        assert "target-binary-1-" in result.artifact_path
        assert result.artifact_path.endswith(".txt")

    def test_artifact_arguments_must_be_paired(self, backend, tmp_path):
        result = backend.execute(
            "print('never')",
            output_directory=str(tmp_path),
        )
        assert not result.success
        assert "provided together" in (result.error or "")
        assert not list(tmp_path.glob("binja-*"))

    def test_artifact_directory_and_extension_are_validated(self, backend, tmp_path):
        relative = backend.execute(
            "pass",
            output_directory="relative",
            output_extension="txt",
        )
        unsafe = backend.execute(
            "pass",
            output_directory=str(tmp_path),
            output_extension=".txt",
        )
        assert not relative.success and "absolute" in (relative.error or "")
        assert not unsafe.success and "output_extension" in (unsafe.error or "")

    def test_artifact_publication_failure_reaches_result_and_log(
        self, backend, tmp_path, monkeypatch, caplog
    ):
        def fail_link(_source, _destination):
            raise OSError("hard links unavailable")

        logger = logging.getLogger("binja_codemode_mcp.plugin.backend")
        old_level = logger.level
        logger.addHandler(caplog.handler)
        logger.setLevel(logging.ERROR)
        monkeypatch.setattr("os.link", fail_link)
        try:
            result = backend.execute(
                "print('preserved')",
                output_directory=str(tmp_path),
                output_extension="txt",
            )
        finally:
            logger.removeHandler(caplog.handler)
            logger.setLevel(old_level)

        assert not result.success
        assert result.artifact_status == "partial"
        assert "filesystem must support hard links" in (result.error or "")
        assert "transaction committed" in (result.error or "")
        assert "before rerunning" in (result.error or "")
        assert any(
            "filesystem must support hard links" in record.getMessage()
            for record in caplog.records
        )

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

    def test_a_stable_id_selects_the_target(self, config, bv):
        backend, other = self._two(config, bv)
        identifier = backend.session.describe()[1]["id"]
        assert backend.execute("bv.rename('landed')", identifier).success
        assert other.renames == ["landed"]
        assert bv.renames == []

    def test_read_only_execution_rolls_back_a_successful_script(self, config, bv):
        backend, other = self._two(config, bv)
        result = backend.execute("bv.rename('discarded')", "other", read_only=True)
        assert result.success
        assert result.reverted
        assert other.renames == []
        assert other.reverted == 1

    def test_read_only_execution_ignores_write_notifications(self, config, bv):
        backend, other = self._two(config, bv)
        backend._watcher_factory = _always_written
        result = backend.execute("bv.rename('discarded')", "other", read_only=True)
        assert result.success
        assert other.renames == []

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

    @pytest.fixture
    def records(self, caplog):
        import logging

        target = logging.getLogger("binja_codemode_mcp.plugin.backend")
        old_level = target.level
        target.addHandler(caplog.handler)
        target.setLevel(logging.INFO)
        try:
            yield caplog
        finally:
            target.removeHandler(caplog.handler)
            target.setLevel(old_level)

    def _backend(self, config, bv):
        tabs = [BinaryTab(0, "ls-a", "/bin/ls-a", bv)]
        return PluginBackend(config, tabs_provider=lambda: tabs)

    def test_a_call_announces_itself_before_running(self, config, bv, records):
        """Said up front, not only on the way out: a script that never returns
        would otherwise leave no trace of what it was doing."""
        backend = self._backend(config, bv)
        backend.execute("bv.rename('x')", None, "rename five functions")
        assert records.records[0].getMessage() == (
            "running on ls-a — rename five functions"
        )

    def test_the_end_reports_the_verdict_and_elapsed(self, config, bv, records):
        backend = self._backend(config, bv)
        backend.execute("pass")
        assert "ok in " in records.records[-1].getMessage()

    def test_a_failure_says_it_rolled_back(self, config, bv, records):
        backend = self._backend(config, bv)
        backend.execute("bv.rename('x')\nraise ValueError('boom')")
        assert "failed, rolled back" in records.records[-1].getMessage()

    def test_a_refused_target_is_logged_too(self, config, bv, records):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "ls-a", "/bin/ls-a", bv),
            BinaryTab(1, "ls-b", "/bin/ls-b", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        backend.execute("pass")
        assert any("refused" in record.getMessage() for record in records.records)


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

    def test_a_clean_read_still_reverts(self, config, bv):
        """Correctness does not depend on proving that the view stayed clean."""
        backend, other = self._two(config, bv, watcher=_never_written)
        assert backend.execute('h.read_only_view("other")', "target").success
        assert other.committed == 0
        assert other.reverted == 1

    def test_no_watcher_still_cannot_turn_read_only_into_writable(self, config, bv):
        backend, other = self._two(config, bv)
        result = backend.execute(
            'src = h.read_only_view("other")\nsrc.rename("sneaky")', "target"
        )
        assert result.success
        assert other.reverted == 1
        assert other.renames == []

    def test_an_undetected_write_cannot_persist(self, config, bv):
        backend, other = self._two(config, bv, watcher=_never_written)
        result = backend.execute(
            'src = h.read_only_view("other")\nsrc.rename("sneaky")', "target"
        )
        assert result.success
        assert other.reverted == 1
        assert other.renames == []

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

    def test_query_mode_ignores_secondary_analysis_notifications(self, config, bv):
        backend, other = self._two(config, bv, watcher=_always_written)
        result = backend.execute(
            'src = h.read_only_view("other")\nprint(len(src.functions))',
            "target",
            read_only=True,
        )
        assert result.success
        assert other.reverted == 1


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
    def test_define_call_list_and_remove(self, backend):
        message = backend.define_lib_function(
            'def double(value=2):\n    """Double a value."""\n    return value * 2'
        )
        assert "Defined h.lib.double(value=2)" in message
        assert backend.execute("print(h.lib.double(6))").output.strip() == "12"

        listing = backend.list_lib_functions()
        assert "h.lib.double(value=2) — Double a value." in listing
        assert "def double(value=2):" in listing

        assert backend.remove_lib_function("double") == "Removed h.lib.double."
        result = backend.execute("h.lib.double()")
        assert not result.success
        assert "No library function 'double'" in (result.error or "")

    def test_definition_does_not_need_an_open_binary(self, config):
        backend = PluginBackend(config, tabs_provider=list)
        assert "Defined" in backend.define_lib_function("def answer():\n    return 42")
        assert "h.lib.answer()" in backend.list_lib_functions()

    def test_a_saved_function_uses_the_calling_targets_view(self, config, bv):
        other = type(bv)("other")
        tabs = [
            BinaryTab(0, "target", "/bin/target", bv),
            BinaryTab(1, "other", "/bin/other", other),
        ]
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        backend.define_lib_function(
            "def port():\n    bv.rename('by_library')\n    return bv.file.filename"
        )

        result = backend.execute("print(h.lib.port())", "other")
        assert result.output.strip() == "/bin/other"
        assert other.renames == ["by_library"]
        assert bv.renames == []

    def test_print_uses_the_running_calls_output(self, backend):
        backend.define_lib_function('def shout():\n    print("from the library")')
        assert "from the library" in backend.execute("h.lib.shout()").output

    def test_timeout_checks_are_compiled_into_library_functions(self, config, tabs):
        config.execution_timeout_s = 0.2
        backend = PluginBackend(config, tabs_provider=lambda: tabs)
        backend.define_lib_function("def spin():\n    while True:\n        pass")

        result = backend.execute("h.lib.spin()")
        assert result.timed_out
        assert backend.executor.wait_for_idle(timeout=3)
        assert tabs[0].bv.reverted == 1

    def test_local_imports_and_helpers_work(self, backend):
        backend.define_lib_function(
            "def dump(value):\n"
            "    import json\n"
            "    def wrapped():\n"
            "        return {'value': value}\n"
            "    return json.dumps(wrapped())"
        )
        result = backend.execute("print(h.lib.dump(7))")
        assert result.success
        assert result.output.strip() == '{"value": 7}'

    def test_top_level_dependencies_are_refused(self, backend):
        with pytest.raises(ValueError, match="LIMIT"):
            backend.define_lib_function("def over(value):\n    return value > LIMIT")
        with pytest.raises(ValueError, match="helper"):
            backend.define_lib_function("def top():\n    return helper()")

    def test_nested_function_global_reads_are_checked(self, backend):
        with pytest.raises(ValueError, match="LIMIT"):
            backend.define_lib_function(
                "def top():\n"
                "    def helper():\n"
                "        return LIMIT\n"
                "    return helper()"
            )

    def test_library_functions_call_each_other_dynamically(self, backend):
        backend.define_lib_function("def base():\n    return 6")
        backend.define_lib_function("def doubled():\n    return h.lib.base() * 2")
        assert backend.execute("print(h.lib.doubled())").output.strip() == "12"

        backend.define_lib_function("def base():\n    return 7")
        assert backend.execute("print(h.lib.doubled())").output.strip() == "14"

        backend.remove_lib_function("base")
        result = backend.execute("h.lib.doubled()")
        assert not result.success
        assert "No library function 'base'" in (result.error or "")

    def test_replacing_a_definition_reports_it(self, backend):
        backend.define_lib_function("def answer():\n    return 1")
        message = backend.define_lib_function("def answer():\n    return 2")
        assert message.startswith("Replaced h.lib.answer()")
        assert backend.execute("print(h.lib.answer())").output.strip() == "2"

    def test_library_is_read_only_inside_execute(self, backend):
        assignment = backend.execute("def nope():\n    return 1\nh.lib['nope'] = nope")
        assert not assignment.success
        assert "define_lib_function" in (assignment.error or "")

        deletion = backend.execute("del h.lib['anything']")
        assert not deletion.success
        assert "remove_lib_function" in (deletion.error or "")

        replacement = backend.execute("h.lib = {}")
        assert not replacement.success
        assert "define_lib_function" in (replacement.error or "")

    def test_immutable_literal_defaults_are_accepted(self, backend):
        backend.define_lib_function(
            "def options(config=(-1, (2, None), b'x')):\n    return config"
        )
        result = backend.execute("print(h.lib.options())")
        assert result.output.strip() == "(-1, (2, None), b'x')"

    @pytest.mark.parametrize(
        "source",
        [
            "def bad(value=[]):\n    return value",
            "def bad(value={}):\n    return value",
            "def bad(value=set()):\n    return value",
            "def bad(value=range(3)):\n    return value",
        ],
    )
    def test_stateful_or_evaluated_defaults_are_refused(self, backend, source):
        with pytest.raises(ValueError, match="immutable literal"):
            backend.define_lib_function(source)

    def test_annotations_are_deferred_and_not_retained(self, backend):
        backend.define_lib_function(
            "def identity(value: MissingType) -> OtherType:\n    return value"
        )
        result = backend.execute("print(h.lib.identity.__annotations__)")
        assert result.success
        assert result.output.strip() == "{}"

    @pytest.mark.parametrize(
        ("source", "message"),
        [
            ("x = 1", "exactly one"),
            ("def a():\n    pass\ndef b():\n    pass", "exactly one"),
            ("async def a():\n    pass", "exactly one"),
            ("@staticmethod\ndef a():\n    pass", "decorators"),
            ("def _private():\n    pass", "underscore"),
            ("def broken(", "Syntax error"),
        ],
    )
    def test_invalid_definition_shapes_are_refused(self, backend, source, message):
        with pytest.raises(ValueError, match=message):
            backend.define_lib_function(source)

    def test_remove_unknown_names_lists_what_exists(self, backend):
        backend.define_lib_function("def real():\n    return 1")
        with pytest.raises(ValueError, match="real"):
            backend.remove_lib_function("missing")

    def test_empty_listing_is_explicit(self, backend):
        assert backend.list_lib_functions() == "No library functions are defined."

    def test_source_is_available_on_the_runtime_function(self, backend):
        backend.define_lib_function('def probe():\n    """Doc."""\n    return 1')
        output = backend.execute("print(h.lib.probe.source)").output
        assert "def probe():" in output
        assert '"""Doc."""' in output

    def test_an_old_function_traceback_still_quotes_source(self, backend):
        backend.define_lib_function('def boom():\n    raise ValueError("inside")')
        for i in range(20):
            backend.execute(f"x = {i}")
        result = backend.execute("h.lib.boom()")
        assert not result.success
        assert 'raise ValueError("inside")' in (result.error or "")

    def test_retrieved_keyword_defaults_are_copies(self, backend):
        backend.define_lib_function("def top(*, limit=5):\n    return limit")
        result = backend.execute(
            "h.lib.top.__kwdefaults__['limit'] = 99\nprint(h.lib.top())"
        )
        assert result.output.strip() == "5"

    def test_footer_always_lists_saved_names(self, backend):
        backend.define_lib_function("def one():\n    return 1")
        assert backend.execute("pass").lib == ("one",)

    def test_subscript_lookup_remains_available(self, backend):
        backend.define_lib_function("def answer():\n    return 42")
        assert backend.execute("print(h.lib['answer']())").output.strip() == "42"

    def test_concurrent_definitions_do_not_lose_entries(self, backend):
        sources = [f"def helper_{i}():\n    return {i}" for i in range(20)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(backend.define_lib_function, sources))
        assert set(backend.helpers.lib._names()) == {f"helper_{i}" for i in range(20)}


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
