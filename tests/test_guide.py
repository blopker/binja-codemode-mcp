"""Guide assembly: the live session header, section lookup, and content rules."""

from typing import Any

from binja_codemode_mcp.plugin.guide import (
    GUIDE_PATH,
    render,
    render_header,
    sections,
    topics,
)


def _binary(name: str, functions: int = 1284) -> dict[str, Any]:
    return {
        "id": f"binary-{len(name)}",
        "name": name,
        "path": f"/bin/{name}",
        "view_type": "Mach-O",
        "arch": "aarch64",
        "platform": "macos-aarch64",
        "functions": functions,
        "start": "0x100000000",
        "end": "0x100004000",
        "entry": "0x100001000",
        "analysis": "complete",
    }


FULL_STATUS: dict[str, Any] = {
    "binja_version": "5.3.9757",
    "binaries": [_binary("ls"), _binary("libfoo", 42)],
}
ONE_BINARY: dict[str, Any] = {
    "binja_version": "5.3.9757",
    "binaries": [_binary("ls")],
}


class TestHeader:
    def test_describes_the_loaded_binary(self):
        header = render_header(FULL_STATUS)
        assert "ls" in header
        assert "aarch64" in header
        assert "1,284 functions" in header

    def test_points_at_the_matching_docs_version(self):
        assert "api.binary.ninja (5.3)" in render_header(FULL_STATUS)

    def test_describes_every_open_binary(self):
        """The model picks a target per call, so the name it needs and the facts
        it would otherwise ask for belong together."""
        header = render_header(FULL_STATUS)
        assert '"ls"' in header and '"libfoo"' in header
        assert "42 functions" in header
        assert "binary-" in header

    def test_demands_a_target_only_when_more_than_one_is_open(self):
        assert "`target`" in render_header(FULL_STATUS)
        assert "`target`" not in render_header(ONE_BINARY)

    def test_points_at_the_read_only_helper_when_it_applies(self):
        assert "h.read_only_view" in render_header(FULL_STATUS)

    def test_no_binary_open_says_so(self):
        assert "No binary is open" in render_header({"binaries": []})

    def test_tolerates_a_sparse_status(self):
        assert render_header({}) != ""


class TestSections:
    def test_guide_splits_into_named_sections(self):
        found = topics(GUIDE_PATH.read_text())
        assert "Safety" in found
        assert "Types and data" in found
        assert "Functions" in found

    def test_preamble_is_kept_under_the_empty_key(self):
        parsed = sections("intro text\n\n## One\nbody")
        assert parsed[""].startswith("intro text")
        assert parsed["One"] == "## One\nbody"


class TestRender:
    def test_full_guide_includes_header_and_body(self):
        out = render(FULL_STATUS)
        assert "1,284 functions" in out
        assert "## Safety" in out

    def test_topic_returns_one_section_with_the_header(self):
        out = render(FULL_STATUS, topic="Types and data")
        assert "1,284 functions" in out
        assert "## Types and data" in out
        assert "## Functions" not in out

    def test_topic_lookup_is_case_insensitive(self):
        assert "## Types and data" in render(FULL_STATUS, topic="types AND DATA")

    def test_unknown_topic_lists_the_real_ones(self):
        out = render(FULL_STATUS, topic="nonsense")
        assert "No section named" in out
        assert "'Types and data'" in out


class TestGuideSize:
    def test_the_rendered_guide_stays_concise(self):
        """The guide is injected into model context, so growth is a product cost."""
        rendered = len(render(FULL_STATUS).encode())
        assert rendered < 8_000, f"the rendered guide grew to {rendered} bytes"

    def test_an_absurd_topic_is_not_echoed_whole(self):
        out = render(FULL_STATUS, topic="z" * 10_000)
        assert len(out.encode()) < 5_000


def guide_text() -> str:
    """Guide text with whitespace flattened.

    These tests pin that a rule is present, not how it happens to wrap — a
    reflow should not fail them, and deleting the rule should.
    """
    return " ".join(GUIDE_PATH.read_text().split())


class TestGuideContent:
    """The guide is the product. Guard the rules that earn their place in it."""

    def test_documents_the_gotchas_that_cost_real_time(self):
        text = guide_text()
        assert "BasicTypeParserResult" in text
        assert "QualifiedName" in text
        assert "update_analysis_and_wait" in text

    def test_states_the_error_trim_alongside_the_output_cap(self):
        text = guide_text()
        assert "32 KB" in text
        assert "4 KB" in text

    def test_documents_complete_output_artifacts(self):
        text = guide_text()
        assert "output_directory" in text
        assert "output_extension" in text
        assert "100 MiB" in text
        assert ".partial" in text and ".failed" in text

    def test_documents_stable_ids_and_query_mode(self):
        text = guide_text()
        assert "stable IDs" in text
        assert "read_only=true" in text
        assert "get_disassembly" in text

    def test_tells_the_model_to_print_hex(self):
        assert "Print addresses as hex" in guide_text()

    def test_uses_the_user_level_mutation_apis(self):
        """The auto-level variants are the wrong tool and quietly undo
        themselves: analysis recreates whatever they removed."""
        text = guide_text()
        assert "remove_user_function" in text
        assert "bv.remove_function(" not in text
        assert "undefine_auto_symbol" in text
        assert "blacklist=True" in text

    def test_names_a_function_by_assigning_the_signature_string(self):
        """f.type only applies the name when given a string; handing it a
        parsed Type sets the prototype and leaves sub_xxxx in place."""
        text = guide_text()
        assert 'f.type = "uint32_t parse_record(' in text
        assert "Type` object changes only the prototype" in text

    def test_passes_a_length_to_get_code_refs(self):
        """Without one, only refs to that exact byte are found."""
        import re

        text = guide_text()
        assert not re.search(r"get_code_refs\(\w+\)", text)

    def test_states_the_timeout_and_transaction_rollback(self):
        text = guide_text()
        assert "30 seconds" in text
        assert "exception rolls it back" in text

    def test_covers_the_idioms_a_live_run_had_to_discover(self):
        """Each of these cost round trips in a real session."""
        text = guide_text()
        for idiom in (
            "f.hlil.instructions",  # locating one address in a big body
            "get_ascii_string_at",  # reading a C string at a pointer
            "bv.sections",  # sections is a mapping
            "get_data_refs",  # a pointer table has data refs, not code refs
        ):
            assert idiom in text, idiom

    def test_distinguishes_rebase_memory_map_sections_and_entries(self):
        text = guide_text()
        assert "rebase_view" in text
        assert "timestamped sibling backup" in text
        assert "allow_non_relocatable=true" in text
        assert "bv.memory_map" in text
        assert "section only labels" in text
        assert "bv.entry_point" in text
        assert "bv.entry_functions" in text

    def test_documents_il_traversal_and_database_state(self):
        text = guide_text()
        assert ".traverse" in text
        assert "bv.modified" in text
        assert "bv.has_database" in text
        assert "save_auto_snapshot" in text

    def test_covers_working_across_two_databases(self):
        """The task the tool is uniquely suited to — two binaries open at
        once — had no section at all until a real port needed one."""
        text = guide_text()
        assert "## Multiple binaries" in text
        assert 'define_user_type("config_t", t)' in text

    def test_documents_how_to_tell_user_work_from_auto_analysis(self):
        """Guessing from naming conventions over-reports badly, and writing
        auto-generated names in as annotations is the failure the guide's
        first rule warns about."""
        text = guide_text()
        assert "is_var_user_defined" in text
        assert "user_type_container" in text
        assert "auto_discovered" in text

    def test_warns_against_touching_qt(self):
        """Scripts run on a worker thread; Qt from off the main thread
        segfaults Binary Ninja, and nothing stops the model trying."""
        text = guide_text()
        assert "Do not call Qt" in text
        assert "GUI thread" in text
        assert "binaryninjaui" in text

    def test_tells_the_model_the_filesystem_is_available(self):
        """Without this the model assumes a sandbox and works around it, which
        is both slower and more token-expensive than just reading the file."""
        text = guide_text()
        assert "filesystem access work" in text
        assert "original_filename" in text

    def test_says_the_target_is_the_only_writable_view(self):
        """The one rule the whole two-database story rests on: a write that is
        not to `bv` is not in a transaction."""
        text = guide_text()
        assert "only writable view" in text
        assert "h.read_only_view" in text

    def test_warns_against_using_a_handed_view_as_a_context_manager(self):
        """BinaryView.__exit__ closes the file. The batch docs teach
        `with load(path) as bv:`, which is correct for a file you opened and
        closes the user's tab when applied to one you were handed."""
        text = guide_text()
        assert "context manager" in text
        assert "closes the user's view" in text

    def test_carries_no_project_specific_leftovers(self):
        """Guidance must generalise: no target-specific names, no dead API."""
        text = guide_text().lower()
        for leftover in ("nrf5", "softdevice", "ble_gap", "binja._bv", "binja."):
            assert leftover not in text, leftover

    def test_says_a_read_only_view_always_rolls_back(self):
        text = guide_text()
        assert "transaction always" in text
        assert "after the script finishes" in text
