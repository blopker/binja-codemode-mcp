"""Bounded, non-overwriting files for complete script output."""

from __future__ import annotations

import os
import re
import secrets
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
WRITE_CHARS = 64 * 1024
_EXTENSION = re.compile(r"[a-z0-9]{1,16}\Z")
_UNSAFE_SLUG = re.compile(r"[^a-z0-9._-]+")
_REPEATED_DASH = re.compile(r"-+")


class ArtifactError(RuntimeError):
    """Artifact creation or publication failed."""


class ArtifactLimitError(ArtifactError):
    """The complete output reached its disk limit."""


@dataclass(frozen=True)
class ArtifactSpec:
    directory: Path
    extension: str
    target_slug: str
    target_id: str

    @classmethod
    def build(
        cls,
        directory: str,
        extension: str,
        *,
        target_name: str,
        target_path: str,
        target_id: str,
    ) -> ArtifactSpec:
        path = Path(directory)
        if not path.is_absolute():
            raise ValueError("`output_directory` must be an absolute path.")
        if not path.is_dir():
            raise ValueError(
                "`output_directory` must name an existing directory; "
                "the server does not create directories."
            )
        normalized_extension = extension.lower()
        if not _EXTENSION.fullmatch(normalized_extension):
            raise ValueError(
                "`output_extension` must contain 1–16 ASCII letters or digits "
                "without a leading dot."
            )
        source = Path(target_path).name or target_name
        if source.lower().endswith(".bndb"):
            source = source[:-5]
        return cls(
            directory=path,
            extension=normalized_extension,
            target_slug=_slug(source),
            target_id=target_id,
        )


def _slug(name: str) -> str:
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    )
    cleaned = _REPEATED_DASH.sub("-", _UNSAFE_SLUG.sub("-", ascii_name))
    return cleaned.strip("._-")[:48].rstrip("._-") or "binary"


class ArtifactSink:
    """Streams output to one exclusively-created file.

    Callers serialize writes and finalization. A sink itself never retries a
    collision: a random-name collision is exceptional and must be visible.
    """

    def __init__(
        self,
        spec: ArtifactSpec,
        *,
        timestamp: str | None = None,
        nonce: str | None = None,
        max_bytes: int = MAX_ARTIFACT_BYTES,
    ) -> None:
        timestamp = timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        nonce = nonce or secrets.token_hex(4)
        base = (
            f"binja-{spec.target_slug}-{spec.target_id}-"
            f"{timestamp}-{nonce}.{spec.extension}"
        )
        self.partial_path = spec.directory / f"{base}.partial"
        self.success_path = spec.directory / base
        self.failed_path = spec.directory / f"{base}.failed"
        self.max_bytes = max_bytes
        self.bytes_written = 0
        self.status = "partial"
        self.path = self.partial_path
        self._closed = False
        try:
            fd = os.open(
                self.partial_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as e:
            raise ArtifactError(
                f"Generated artifact path already exists: {self.partial_path}. "
                "No file was overwritten."
            ) from e
        except OSError as e:
            raise ArtifactError(
                f"Could not create artifact {self.partial_path}: "
                f"{type(e).__name__}: {e}"
            ) from e
        self._file = os.fdopen(fd, "wb", buffering=0)

    def write(self, text: str) -> None:
        """Write bounded UTF-8 chunks without retaining the complete output."""
        if self.status != "partial":
            return
        for start in range(0, len(text), WRITE_CHARS):
            encoded = text[start : start + WRITE_CHARS].encode("utf-8", "replace")
            remaining = self.max_bytes - self.bytes_written
            if len(encoded) > remaining:
                safe = encoded[: max(remaining, 0)].decode("utf-8", "ignore").encode()
                if safe:
                    self._file.write(safe)
                    self.bytes_written += len(safe)
                raise ArtifactLimitError(
                    f"Artifact output reached the {self.max_bytes // (1024 * 1024)} "
                    "MiB limit."
                )
            self._file.write(encoded)
            self.bytes_written += len(encoded)

    def finish(self, success: bool) -> None:
        """Close and exclusively publish the completed or failed artifact."""
        if self.status != "partial":
            return
        if not self._closed:
            self._file.flush()
            self._file.close()
            self._closed = True
        destination = self.success_path if success else self.failed_path
        try:
            # Same-directory hard linking gives atomic visibility and fails
            # rather than replacing an existing destination.
            os.link(self.partial_path, destination)
            os.unlink(self.partial_path)
        except FileExistsError as e:
            raise ArtifactError(
                f"Generated artifact destination already exists: {destination}. "
                f"Partial output remains at {self.partial_path}; nothing was "
                "overwritten."
            ) from e
        except OSError as e:
            raise ArtifactError(
                f"Could not publish artifact {destination}: "
                f"{type(e).__name__}: {e}. Partial output remains at "
                f"{self.partial_path}."
            ) from e
        self.status = "success" if success else "failed"
        self.path = destination

    def discard(self) -> None:
        """Remove an artifact when the script never started."""
        if self.status != "partial":
            return
        if not self._closed:
            self._file.close()
            self._closed = True
        with suppress(FileNotFoundError):
            self.partial_path.unlink()
        self.status = "discarded"
