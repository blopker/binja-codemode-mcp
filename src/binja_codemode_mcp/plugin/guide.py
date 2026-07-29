"""Assembles the usage guide: a live session header plus guide.md.

The header is generated per call so the model gets facts about the binary that
is actually loaded, rather than a static document that goes stale.

Pure module: the status dict is injected.
"""

from pathlib import Path
from typing import Any

GUIDE_PATH = Path(__file__).with_name("guide.md")


def sections(markdown: str) -> dict[str, str]:
    """Split the guide on `## ` headings, preserving the preamble under ''."""
    out: dict[str, str] = {}
    title = ""
    buf: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            out[title] = "\n".join(buf).strip()
            title = line[3:].strip()
            buf = [line]
        else:
            buf.append(line)
    out[title] = "\n".join(buf).strip()
    return out


def topics(markdown: str) -> list[str]:
    return [t for t in sections(markdown) if t]


def render_header(status: dict[str, Any]) -> str:
    """One-screen orientation block describing the live session.

    Every open binary is described, not just one: the model chooses a `target`
    per call, so the name it needs and the facts it would otherwise ask for
    belong together.
    """
    lines: list[str] = []
    binaries = status.get("binaries") or []

    if not binaries:
        lines.append("No binary is open in Binary Ninja.")
    for binary in binaries:
        name = binary["name"]
        kind = (
            f"{binary.get('view_type', '?')}, {binary.get('arch', '?')}, "
            f"{binary.get('platform', '?')}"
        )
        counted = f"{binary.get('functions', 0):,} functions"
        lines.append(
            f'Binary "{name}" ({kind}) — {counted}, '
            f"{binary.get('start')} – {binary.get('end')}, "
            f"analysis {binary.get('analysis', 'unknown')}, "
            f"entry {binary.get('entry', 'none')}"
        )

    version = status.get("binja_version")
    if version:
        docs = ".".join(version.split(".")[:2])
        lines.append(f"Binary Ninja {version} — API docs: api.binary.ninja ({docs})")

    if len(binaries) > 1:
        names = ", ".join(f'"{b["name"]}"' for b in binaries)
        lines.append(
            f"More than one binary is open, so every execute call must name its "
            f"`target`: {names}. The target is the only view you can write to; "
            "reach the other with h.read_only_view(name)."
        )

    return "\n".join(lines)


def render(status: dict[str, Any], topic: str | None = None) -> str:
    """Live header plus the guide, or a single section of it."""
    markdown = GUIDE_PATH.read_text(encoding="utf-8")
    header = render_header(status)

    if topic:
        found = sections(markdown)
        for name, body in found.items():
            if name and name.lower() == topic.lower():
                return f"{header}\n\n{body}"
        available = ", ".join(repr(t) for t in topics(markdown))
        return (
            f"{header}\n\nNo section named {topic[:60]!r}. "
            f"Available sections: {available}. "
            "Omit `topic` for the whole guide."
        )

    return f"{header}\n\n{markdown}"
