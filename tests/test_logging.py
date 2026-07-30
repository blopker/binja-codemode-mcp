"""Standard logging integration with Binary Ninja."""

import logging

import pytest

from binja_codemode_mcp.plugin.logging import (
    LOGGER_NAME,
    configure_binary_ninja_logging,
)


@pytest.fixture
def package_logger():
    logger = logging.getLogger(LOGGER_NAME)
    handlers = logger.handlers[:]
    level = logger.level
    propagate = logger.propagate
    logger.handlers.clear()
    try:
        yield logger
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _configure(lines, *, bad_sink=None):
    configure_binary_ninja_logging(
        debug=lambda message: lines.append(("debug", message)),
        info=lambda message: lines.append(("info", message)),
        warning=lambda message: lines.append(("warning", message)),
        error=bad_sink or (lambda message: lines.append(("error", message))),
    )


def test_levels_and_prefix_are_mapped_to_binary_ninja(package_logger):
    lines = []
    _configure(lines)
    logger = logging.getLogger(f"{LOGGER_NAME}.test")

    logger.debug("detail")
    logger.info("ready")
    logger.warning("careful")
    logger.error("broken")

    assert lines == [
        ("debug", "Code Mode MCP: detail"),
        ("info", "Code Mode MCP: ready"),
        ("warning", "Code Mode MCP: careful"),
        ("error", "Code Mode MCP: broken"),
    ]


def test_reconfiguration_replaces_the_handler(package_logger):
    first = []
    second = []
    _configure(first)
    _configure(second)

    logging.getLogger(f"{LOGGER_NAME}.test").info("once")

    assert first == []
    assert second == [("info", "Code Mode MCP: once")]
    assert (
        sum(
            bool(getattr(handler, "_binja_codemode_mcp_handler", False))
            for handler in package_logger.handlers
        )
        == 1
    )


def test_exceptions_include_the_traceback(package_logger):
    lines = []
    _configure(lines)
    logger = logging.getLogger(f"{LOGGER_NAME}.test")

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("operation failed")

    assert lines[0][0] == "error"
    assert "Code Mode MCP: operation failed" in lines[0][1]
    assert "Traceback (most recent call last)" in lines[0][1]
    assert "ValueError: boom" in lines[0][1]


def test_a_binary_ninja_logging_failure_is_swallowed(package_logger):
    def fail(_message):
        raise RuntimeError("log unavailable")

    _configure([], bad_sink=fail)
    logging.getLogger(f"{LOGGER_NAME}.test").error("still safe")
