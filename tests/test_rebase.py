"""Guardrails around the dedicated, view-replacing rebase operation."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from binja_codemode_mcp.config import Config
from binja_codemode_mcp.plugin.backend import PluginBackend
from binja_codemode_mcp.plugin.rebase import (
    capture_rebase_state,
    rebase_backup_path,
    validate_rebase_request,
    verify_rebase,
)
from binja_codemode_mcp.plugin.session import BinaryTab


@dataclass
class Region:
    name: str = "origin<Mapped>@0x0"
    start: int = 0
    length: int = 0x100
    flags: int = 5
    enabled: bool = True
    rebaseable: bool = True


class RebaseView:
    def __init__(
        self,
        start: int = 0,
        *,
        modified: bool = False,
        has_database: bool = True,
        filename: str = "/tmp/firmware.bndb",
    ) -> None:
        self.view_type = "Mapped"
        self.start = start
        self.end = start + 0x100
        self.entry_point = start + 0x40
        self.memory_map = type("MemoryMap", (), {"regions": [Region(start=start)]})()
        self.modified = modified
        self.has_database = has_database
        self.relocatable = True
        self.relocation_ranges: list[Any] = []
        self.data_vars: dict[int, Any] = {}
        self.functions: list[Any] = []
        self.address_comments: dict[int, str] = {}
        self.user_type_container = type("Types", (), {"types": {}})()
        self.entry_functions: list[Any] = []
        self.created_databases: list[str] = []
        self.file = type(
            "File",
            (),
            {
                "session_id": 7,
                "filename": filename,
                "database": type(
                    "Database",
                    (),
                    {"current_snapshot": type("Snapshot", (), {"id": 1})()},
                )(),
            },
        )()

    def read(self, address: int, length: int) -> bytes:
        offset = address - self.start
        if not 0 <= offset < 0x100:
            return b""
        return bytes((offset + i) & 0xFF for i in range(length))

    def get_symbols(self) -> list[Any]:
        return []

    def get_tags(self, auto: bool | None = None) -> list[Any]:
        return []

    def add_entry_point(self, address: int) -> None:
        self.entry_functions.append(type("Function", (), {"start": address})())

    def update_analysis(self) -> None:
        pass

    def create_database(self, filename: str) -> bool:
        self.created_databases.append(filename)
        return True


class CorruptRebaseView(RebaseView):
    def read(self, address: int, length: int) -> bytes:
        return b"\xff" * length


def state(start: int = 0, **kwargs: Any):
    return capture_rebase_state(RebaseView(start, **kwargs))


def test_preflight_requires_a_clean_saved_database():
    with pytest.raises(ValueError, match="saved BNDB"):
        validate_rebase_request(state(has_database=False), 0x08004000, entry_point=None)
    with pytest.raises(ValueError, match="unsaved changes"):
        validate_rebase_request(state(modified=True), 0x08004000, entry_point=None)


def test_preflight_checks_the_projected_entry_point():
    with pytest.raises(ValueError, match="outside"):
        validate_rebase_request(state(), 0x08004000, entry_point=0x08005000)


def test_preflight_requires_opt_in_for_non_relocatable_images():
    view = RebaseView()
    view.relocatable = False
    before = capture_rebase_state(view)
    with pytest.raises(ValueError, match="allow_non_relocatable"):
        validate_rebase_request(before, 0x08004000, entry_point=None)
    validate_rebase_request(
        before,
        0x08004000,
        entry_point=None,
        allow_non_relocatable=True,
    )


def test_backup_path_is_timestamped_non_overwriting_sibling(tmp_path):
    source = tmp_path / "firmware.bndb"
    now = datetime(2026, 7, 30, 12, 34, 56, tzinfo=timezone.utc)
    backup = rebase_backup_path(str(source), now)
    assert backup == tmp_path / "firmware.pre-rebase-20260730T123456+0000.bndb"
    backup.touch()
    with pytest.raises(ValueError, match="collision"):
        rebase_backup_path(str(source), now)
    raw = rebase_backup_path(str(tmp_path / "firmware.bin"), now)
    assert raw.name == "firmware.bin.pre-rebase-20260730T123456+0000.bndb"


def test_verification_accepts_a_uniform_rebase():
    before = state()
    after = state(0x08004000, modified=True)
    assert verify_rebase(before, after, 0x08004000) == []


def test_verification_preserves_a_zero_loader_entry_sentinel():
    original = RebaseView()
    original.entry_point = 0
    replacement = RebaseView(0x08004000, modified=True)
    replacement.entry_point = 0
    assert (
        verify_rebase(
            capture_rebase_state(original),
            capture_rebase_state(replacement),
            0x08004000,
        )
        == []
    )


def test_verification_detects_changed_mapped_bytes():
    before = state()
    replacement = CorruptRebaseView(0x08004000, modified=True)
    problems = verify_rebase(before, capture_rebase_state(replacement), 0x08004000)
    assert any("mapped bytes changed" in problem for problem in problems)


def test_backend_rebases_replacement_and_adds_entry(tmp_path):
    original = RebaseView(filename=str(tmp_path / "firmware.bndb"))
    tabs = [BinaryTab(0, "firmware", original.file.filename, original)]
    called: list[tuple[Any, int]] = []

    def rebase(view: Any, address: int) -> RebaseView:
        called.append((view, address))
        return RebaseView(address, modified=True)

    backend = PluginBackend(
        Config(api_key="k", data_dir=tmp_path),
        tabs_provider=lambda: tabs,
        rebase_provider=rebase,
    )
    result = backend.rebase_view(None, 0x08004000, 0x080040D0)
    assert called == [(original, 0x08004000)]
    assert len(original.created_databases) == 1
    assert ".pre-rebase-" in original.created_databases[0]
    assert "0x8004000" in result
    assert "0x80040d0" in result
    assert original.created_databases[0] in result
