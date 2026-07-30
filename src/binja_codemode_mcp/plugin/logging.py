"""Consistent labels for messages written to Binary Ninja's log."""

LOG_PREFIX = "Code Mode MCP: "


def log_message(message: str) -> str:
    return f"{LOG_PREFIX}{message}"
