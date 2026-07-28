"""Guide assembly: the live session header, section lookup, and content rules."""

from typing import Any

from binja_codemode_mcp.plugin.guide import (
    GUIDE_PATH,
    render,
    render_header,
    sections,
    topics,
)

FULL_STATUS: dict[str, Any] = {
    "binja_version": "5.3.9757",
    "binary": {
        "name": "ls",
        "view_type": "Mach-O",
        "arch": "aarch64",
        "platform": "macos-aarch64",
        "functions": 1284,
        "start": "0x100000000",
        "end": "0x100004000",
        "entry": "0x100001000",
        "analysis": "complete",
    },
    "tabs": [
        {"index": 0, "name": "ls", "path": "/bin/ls", "selected": True},
        {"index": 1, "name": "libfoo", "path": "/lib/libfoo", "selected": False},
    ],
}


class TestHeader:
    def test_describes_the_loaded_binary(self):
        header = render_header(FULL_STATUS)
        assert "ls" in header
        assert "aarch64" in header
        assert "1,284 functions" in header

    def test_points_at_the_matching_docs_version(self):
        assert "api.binary.ninja (5.3)" in render_header(FULL_STATUS)

    def test_lists_tabs_and_flags_the_selected_one(self):
        header = render_header(FULL_STATUS)
        assert "[0] ls (selected)" in header
        assert "[1] libfoo" in header

    def test_explains_selection_only_when_it_matters(self):
        assert "h.select" in render_header(FULL_STATUS)
        one_tab = {**FULL_STATUS, "tabs": FULL_STATUS["tabs"][:1]}
        assert "h.select" not in render_header(one_tab)

    def test_no_binary_open_says_so(self):
        assert "No binary is open" in render_header({"binary": None, "tabs": []})

    def test_tolerates_a_sparse_status(self):
        assert render_header({}) != ""


class TestSections:
    def test_guide_splits_into_named_sections(self):
        found = topics(GUIDE_PATH.read_text())
        assert "Ground rules" in found
        assert "Types" in found
        assert "Functions" in found

    def test_preamble_is_kept_under_the_empty_key(self):
        parsed = sections("intro text\n\n## One\nbody")
        assert parsed[""].startswith("intro text")
        assert parsed["One"] == "## One\nbody"


class TestRender:
    def test_full_guide_includes_header_and_body(self):
        out = render(FULL_STATUS)
        assert "1,284 functions" in out
        assert "## Ground rules" in out

    def test_topic_returns_one_section_with_the_header(self):
        out = render(FULL_STATUS, topic="Types")
        assert "1,284 functions" in out
        assert "## Types" in out
        assert "## Comments" not in out

    def test_topic_lookup_is_case_insensitive(self):
        assert "## Types" in render(FULL_STATUS, topic="types")

    def test_unknown_topic_lists_the_real_ones(self):
        out = render(FULL_STATUS, topic="nonsense")
        assert "No section named" in out
        assert "'Types'" in out


class TestGuideContent:
    """The guide is the product. Guard the rules that earn their place in it."""

    def test_documents_the_gotchas_that_cost_real_time(self):
        text = GUIDE_PATH.read_text()
        assert "BasicTypeParserResult" in text
        assert "QualifiedName" in text
        assert "update_analysis_and_wait" in text

    def test_tells_the_model_to_print_hex(self):
        assert "Print addresses as hex" in GUIDE_PATH.read_text()

    def test_uses_the_user_level_mutation_apis(self):
        """The auto-level variants are the wrong tool and quietly undo
        themselves: analysis recreates whatever they removed."""
        text = GUIDE_PATH.read_text()
        assert "remove_user_function" in text
        assert "bv.remove_function(" not in text
        assert "undefine_auto_symbol" in text
        assert "blacklist=True" in text

    def test_names_a_function_by_assigning_the_signature_string(self):
        """func.type only applies the name when given a string; handing it a
        parsed Type sets the prototype and leaves sub_xxxx in place."""
        text = GUIDE_PATH.read_text()
        assert "func.type = signature" in text
        assert "func.type = parsed" not in text

    def test_passes_a_length_to_get_code_refs(self):
        """Without one, only refs to that exact byte are found."""
        import re

        text = GUIDE_PATH.read_text()
        assert not re.search(r"get_code_refs\(\w+\)", text)

    def test_states_the_timeout_and_that_it_discards_the_batch(self):
        text = GUIDE_PATH.read_text()
        assert "30-second limit" in text
        assert "reverted when it eventually finishes" in text

    def test_covers_the_idioms_a_live_run_had_to_discover(self):
        """Each of these cost round trips in a real session."""
        text = GUIDE_PATH.read_text()
        for idiom in (
            "func.hlil.instructions",  # locating one address in a big body
            "get_ascii_string_at",  # reading a C string at a pointer
            "bv.sections.values()",  # sections is a mapping
            "get_comment_at",  # reading a comment back
            "get_data_refs",  # a pointer table has data refs, not code refs
        ):
            assert idiom in text, idiom

    def test_warns_against_touching_qt(self):
        """Scripts run on a worker thread; Qt from off the main thread
        segfaults Binary Ninja, and nothing stops the model trying."""
        text = GUIDE_PATH.read_text()
        assert "Do not touch the GUI" in text
        assert "worker thread" in text
        assert "binaryninjaui" in text

    def test_tells_the_model_the_filesystem_is_available(self):
        """Without this the model assumes a sandbox and works around it, which
        is both slower and more token-expensive than just reading the file."""
        text = GUIDE_PATH.read_text()
        assert "There is no sandbox" in text
        assert "original_filename" in text

    def test_says_select_rebinds_bv_immediately(self):
        assert "rebinds `bv` immediately" in GUIDE_PATH.read_text()

    def test_carries_no_project_specific_leftovers(self):
        """Guidance must generalise: no target-specific names, no dead API."""
        text = GUIDE_PATH.read_text().lower()
        for leftover in ("nrf5", "softdevice", "ble_gap", "binja._bv", "binja."):
            assert leftover not in text, leftover

    def test_tells_the_model_not_to_build_its_own_rollback(self):
        """Transactions make a rollback feature unnecessary; say so."""
        text = GUIDE_PATH.read_text()
        assert "one undo transaction" in text
        assert "should not build your own" in text
