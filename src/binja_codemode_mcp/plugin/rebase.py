"""Preflight and verify the view-replacing Binary Ninja rebase operation."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

SAMPLE_BYTES = 16
MAX_SAMPLED_REGIONS = 128


@dataclass(frozen=True)
class RegionState:
    name: str
    start: int
    length: int
    flags: int
    enabled: bool
    rebaseable: bool


@dataclass(frozen=True)
class ByteSample:
    region: int
    offset: int
    data: bytes


@dataclass(frozen=True)
class AnnotationCounts:
    user_symbols: int
    user_data_variables: int
    user_types: int
    user_function_variables: int
    address_comments: int
    function_comments: int
    user_tags: int
    user_function_tags: int


@dataclass(frozen=True)
class RebaseState:
    start: int
    end: int
    entry_point: int
    regions: tuple[RegionState, ...]
    samples: tuple[ByteSample, ...]
    annotations: AnnotationCounts
    entry_functions: tuple[int, ...]
    modified: bool
    has_database: bool
    snapshot_id: int | None
    relocatable: bool
    relocation_count: int


def validate_rebase_request(
    before: RebaseState,
    new_base: int,
    *,
    entry_point: int | None,
    allow_non_relocatable: bool = False,
) -> None:
    """Refuse a rebase whose original database is not a safe recovery point."""
    if not before.has_database:
        raise ValueError(
            "Rebasing requires a saved BNDB. Save the binary as a database first."
        )
    if before.modified:
        raise ValueError(
            "The database has unsaved changes. Save it before rebasing so closing "
            "without saving remains a reliable recovery path."
        )
    if new_base == before.start:
        raise ValueError(f"The view already starts at {new_base:#x}.")
    if not before.relocatable and not allow_non_relocatable:
        raise ValueError(
            "Binary Ninja marks this image non-relocatable and reports "
            f"{before.relocation_count} relocation ranges. Prefer `bv.memory_map` "
            "for a fresh raw image, or retry with `allow_non_relocatable=true` "
            "only after verifying that embedded absolute values already use the "
            "intended address space."
        )
    if entry_point is not None:
        projected_end = new_base + (before.end - before.start)
        if not new_base <= entry_point < projected_end:
            raise ValueError(
                f"entry_point {entry_point:#x} is outside the projected rebased "
                f"range {new_base:#x}–{projected_end:#x}."
            )


def rebase_backup_path(filename: str, now: datetime | None = None) -> Path:
    """A generated sibling path which can never overwrite an existing backup."""
    source = Path(filename)
    if not source.is_absolute():
        raise ValueError("The database filename is not an absolute path.")
    timestamp = (now or datetime.now().astimezone()).strftime("%Y%m%dT%H%M%S%z")
    stem = source.stem if source.suffix.lower() == ".bndb" else source.name
    backup = source.with_name(f"{stem}.pre-rebase-{timestamp}.bndb")
    if backup.exists():
        raise ValueError(f"Refusing to overwrite backup collision: {backup}")
    return backup


def _user_function_tags(function: Any) -> int:
    total = len(function.get_function_tags(auto=False))
    seen: set[tuple[str, int]] = set()
    for arch, address, _tag in function.tags:
        key = (str(arch.name), int(address))
        if key in seen:
            continue
        seen.add(key)
        total += len(function.get_tags_at(address, arch, auto=False))
    return total


def capture_rebase_state(view: Any) -> RebaseState:
    regions = tuple(
        RegionState(
            name=str(region.name),
            start=int(region.start),
            length=int(region.length),
            flags=int(region.flags),
            enabled=bool(region.enabled),
            rebaseable=bool(region.rebaseable),
        )
        for region in view.memory_map.regions
    )

    samples: list[ByteSample] = []
    for index, region in enumerate(regions[:MAX_SAMPLED_REGIONS]):
        if not region.enabled or region.length <= 0:
            continue
        offsets = {0, region.length // 2, max(0, region.length - SAMPLE_BYTES)}
        for offset in sorted(offsets):
            size = min(SAMPLE_BYTES, region.length - offset)
            data = bytes(view.read(region.start + offset, size))
            if data:
                samples.append(ByteSample(index, offset, data))

    functions = list(view.functions)
    annotations = AnnotationCounts(
        user_symbols=sum(not symbol.auto for symbol in view.get_symbols()),
        user_data_variables=sum(
            not variable.auto_discovered for variable in view.data_vars.values()
        ),
        user_types=len(view.user_type_container.types),
        user_function_variables=sum(
            function.is_var_user_defined(variable)
            for function in functions
            for variable in function.vars
        ),
        address_comments=len(view.address_comments),
        function_comments=sum(len(function.comments) for function in functions),
        user_tags=len(view.get_tags(auto=False)),
        user_function_tags=sum(_user_function_tags(function) for function in functions),
    )
    try:
        snapshot_id = int(view.file.database.current_snapshot.id)
    except Exception:
        snapshot_id = None
    return RebaseState(
        start=int(view.start),
        end=int(view.end),
        entry_point=int(view.entry_point),
        regions=regions,
        samples=tuple(samples),
        annotations=annotations,
        entry_functions=tuple(sorted(int(fn.start) for fn in view.entry_functions)),
        modified=bool(view.modified),
        has_database=bool(view.has_database),
        snapshot_id=snapshot_id,
        relocatable=bool(view.relocatable),
        relocation_count=len(view.relocation_ranges),
    )


def verify_rebase(
    before: RebaseState,
    after: RebaseState,
    new_base: int,
    *,
    requested_entry_point: int | None = None,
) -> tuple[list[str], list[str]]:
    """Return (failed postconditions, informational notes).

    Empty problems means verification passed; notes are observations the
    result should carry without failing the rebase.
    """
    problems: list[str] = []
    notes: list[str] = []
    delta = new_base - before.start
    if after.start != new_base:
        problems.append(f"view starts at {after.start:#x}, expected {new_base:#x}")

    if len(before.regions) != len(after.regions):
        problems.append(
            f"memory region count changed from {len(before.regions)} "
            f"to {len(after.regions)}"
        )

    for old, new in zip(before.regions, after.regions, strict=False):
        expected_start = old.start + delta if old.rebaseable else old.start
        if new.start != expected_start:
            problems.append(
                f"region {old.name!r} starts at {new.start:#x}, "
                f"expected {expected_start:#x}"
            )
        if (
            new.length,
            new.flags,
            new.enabled,
            new.rebaseable,
        ) != (
            old.length,
            old.flags,
            old.enabled,
            old.rebaseable,
        ):
            problems.append(f"region {old.name!r} changed shape, flags, or state")

    # Rebasing a relocatable image legitimately rewrites relocated pointers
    # (including chained fixups that relocation_ranges does not list), so a
    # changed sample only condemns a non-relocatable rebase, where mapped
    # bytes are supposed to move untouched.
    changed_samples = problems if not before.relocatable else notes
    for sample in before.samples:
        if sample.region >= len(after.regions):
            continue
        old_region = before.regions[sample.region]
        actual = next(
            (
                item.data
                for item in after.samples
                if item.region == sample.region and item.offset == sample.offset
            ),
            None,
        )
        if actual != sample.data:
            changed_samples.append(
                f"mapped bytes changed in region {old_region.name!r} "
                f"at offset {sample.offset:#x}"
            )

    # Zero is a "no loader entry" sentinel on Mapped views and remains zero.
    # A real loader entry inside a rebaseable region translates normally.
    expected_entry = before.entry_point
    if expected_entry != 0:
        for region in before.regions:
            if region.start <= expected_entry < region.start + region.length:
                if region.rebaseable:
                    expected_entry += delta
                break
    if after.entry_point != expected_entry:
        problems.append(
            f"loader entry point is {after.entry_point:#x}, "
            f"expected {expected_entry:#x}"
        )

    if after.annotations != before.annotations:
        problems.append(
            f"user annotation counts changed from {before.annotations} "
            f"to {after.annotations}"
        )
    if requested_entry_point is not None and requested_entry_point not in (
        after.entry_functions
    ):
        problems.append(
            f"requested analysis entry point {requested_entry_point:#x} "
            "is not present in entry_functions"
        )
    return problems, notes


def format_rebase_result(
    name: str,
    before: RebaseState,
    after: RebaseState,
    *,
    requested_entry_point: int | None,
    backup_path: Path,
    notes: list[str] | None = None,
) -> str:
    delta = after.start - before.start
    lines = [
        f"Rebased {name!r}: {before.start:#x} -> {after.start:#x} (delta {delta:+#x}).",
        f"Verified {len(after.regions)} memory regions, "
        f"{len(after.samples)} byte samples, and user annotation counts.",
        f"Loader entry point: {before.entry_point:#x} -> {after.entry_point:#x}.",
    ]
    for note in notes or []:
        lines.append(
            f"Note: {note} — expected on a relocatable image, where the rebase "
            "rewrites relocated pointers."
        )
    if requested_entry_point is not None:
        lines.append(f"Analysis entry point added at {requested_entry_point:#x}.")
    if not before.relocatable:
        lines.append(
            "Warning: Binary Ninja marked the image non-relocatable; embedded "
            "absolute values were not adjusted."
        )
    lines.append(f"Pre-rebase backup: {backup_path}.")
    if before.snapshot_id is not None or after.snapshot_id is not None:
        lines.append(f"Database snapshot: {before.snapshot_id} -> {after.snapshot_id}.")
    state = "modified; save to persist it" if after.modified else "saved"
    lines.append(f"Database state: {state}.")
    return "\n".join(lines)
