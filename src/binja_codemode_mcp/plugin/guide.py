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
    """One-screen orientation block describing the live session."""
    lines: list[str] = []

    switched = status.get("switched")
    if switched:
        # First line, before the binary it describes: the model is reading this
        # precisely because something changed under it.
        lines.append(
            f"NOTE: {switched['from']} is no longer open — now selected: "
            f"{switched['to']}. Your next script will be refused once so you "
            "can confirm the target before writing."
        )

    binary = status.get("binary")
    if binary:
        lines.append(
            f"Binary: {binary['name']} "
            f"({binary.get('view_type', '?')}, {binary.get('arch', '?')}, "
            f"{binary.get('platform', '?')}) — {binary.get('functions', 0):,} functions"
        )
        lines.append(f"Address range: {binary.get('start')} – {binary.get('end')}")
        lines.append(
            f"Analysis: {binary.get('analysis', 'unknown')}. "
            f"Entry point: {binary.get('entry', 'none')}."
        )
    else:
        lines.append("No binary is open in Binary Ninja.")

    version = status.get("binja_version")
    if version:
        docs = ".".join(version.split(".")[:2])
        lines.append(f"Binary Ninja {version} — API docs: api.binary.ninja ({docs})")

    tabs = status.get("tabs") or []
    if tabs:
        rendered = "  ".join(
            f"[{t['index']}] {t['name']}" + (" (selected)" if t["selected"] else "")
            for t in tabs
        )
        lines.append(f"Open tabs: {rendered}")
        if len(tabs) > 1:
            lines.append(
                "Several binaries are open. The selection above is the target — not "
                "necessarily the first one opened — and it stays put even if the user "
                "switches tabs; use h.select(<index>) to change it."
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
