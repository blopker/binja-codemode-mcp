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
        assert "hex" in GUIDE_PATH.read_text().lower()

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
