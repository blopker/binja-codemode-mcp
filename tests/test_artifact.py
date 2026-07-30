"""Complete-output artifact naming, limits, and publication."""

from pathlib import Path

import pytest

from binja_codemode_mcp.plugin.artifact import (
    ArtifactError,
    ArtifactLimitError,
    ArtifactSink,
    ArtifactSpec,
)


def spec(tmp_path: Path) -> ArtifactSpec:
    return ArtifactSpec.build(
        str(tmp_path),
        "JSONL",
        target_name="ignored",
        target_path="/build/My Firmware.arm64e.bndb",
        target_id="binary-2",
    )


def sink(tmp_path: Path, max_bytes: int = 1024) -> ArtifactSink:
    return ArtifactSink(
        spec(tmp_path),
        timestamp="20260730T212231Z",
        nonce="8f31c2a4",
        max_bytes=max_bytes,
    )


class TestSpec:
    def test_requires_an_existing_absolute_directory(self, tmp_path):
        with pytest.raises(ValueError, match="absolute"):
            ArtifactSpec.build(
                "relative",
                "txt",
                target_name="x",
                target_path="x",
                target_id="binary-1",
            )
        with pytest.raises(ValueError, match="existing"):
            ArtifactSpec.build(
                str(tmp_path / "missing"),
                "txt",
                target_name="x",
                target_path="x",
                target_id="binary-1",
            )

    @pytest.mark.parametrize("extension", ["", ".txt", "tar.gz", "x" * 17, "💥"])
    def test_rejects_unsafe_extensions(self, tmp_path, extension):
        with pytest.raises(ValueError, match="output_extension"):
            ArtifactSpec.build(
                str(tmp_path),
                extension,
                target_name="x",
                target_path="x",
                target_id="binary-1",
            )

    def test_sanitizes_and_bounds_the_target_slug(self, tmp_path):
        built = ArtifactSpec.build(
            str(tmp_path),
            "txt",
            target_name="ignored",
            target_path="/tmp/Fïrm ware!?-" + "z" * 100 + ".bndb",
            target_id="binary-1",
        )
        assert built.target_slug.startswith("firm-ware-z")
        assert len(built.target_slug) <= 48


class TestSink:
    def test_name_records_target_time_nonce_extension_and_partial_state(self, tmp_path):
        artifact = sink(tmp_path)
        assert artifact.path.name == (
            "binja-my-firmware.arm64e-binary-2-20260730T212231Z-8f31c2a4.jsonl.partial"
        )
        assert artifact.path.exists()
        artifact.discard()

    def test_success_is_atomically_published_without_the_state_suffix(self, tmp_path):
        artifact = sink(tmp_path)
        artifact.write("one\n")
        artifact.finish(True)
        assert artifact.status == "success"
        assert artifact.path.suffix == ".jsonl"
        assert artifact.path.read_text() == "one\n"
        assert not artifact.partial_path.exists()

    def test_failure_is_published_with_a_failed_suffix(self, tmp_path):
        artifact = sink(tmp_path)
        artifact.write("partial\n")
        artifact.finish(False)
        assert artifact.status == "failed"
        assert artifact.path.name.endswith(".jsonl.failed")
        assert artifact.path.read_text() == "partial\n"

    def test_creation_collision_errors_without_overwriting(self, tmp_path):
        first = sink(tmp_path)
        with pytest.raises(ArtifactError, match="already exists"):
            sink(tmp_path)
        assert first.path.read_bytes() == b""
        first.discard()

    def test_publication_collision_errors_without_overwriting(self, tmp_path):
        artifact = sink(tmp_path)
        artifact.success_path.write_text("old")
        artifact.write("new")
        with pytest.raises(ArtifactError, match="already exists"):
            artifact.finish(True)
        assert artifact.success_path.read_text() == "old"
        assert artifact.partial_path.read_text() == "new"

    def test_limit_counts_utf8_bytes_and_preserves_valid_text(self, tmp_path):
        artifact = sink(tmp_path, max_bytes=5)
        with pytest.raises(ArtifactLimitError, match="0 MiB limit"):
            artifact.write("漢漢")
        artifact.finish(False)
        assert artifact.bytes_written == 3
        assert artifact.path.read_text() == "漢"

    def test_discard_removes_a_call_that_never_started(self, tmp_path):
        artifact = sink(tmp_path)
        path = artifact.path
        artifact.discard()
        assert artifact.status == "discarded"
        assert not path.exists()
