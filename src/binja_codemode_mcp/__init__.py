"""
Code Mode MCP Server for Binary Ninja

An MCP server that enables LLM-assisted reverse engineering through code execution.
"""

try:
    import binaryninja
except ImportError:
    # Not running inside Binary Ninja (pytest, ruff, CI). Importing the package
    # must never require the host application, or the pure modules under it
    # become untestable.
    binaryninja = None  # type: ignore

# Only load GUI components when running with UI
if binaryninja is not None and binaryninja.core_ui_enabled():
    from .plugin.commands import BinjaCodeModeMCP
    from .plugin.widget import init_status_indicator

    plugin_instance = BinjaCodeModeMCP()
    plugin_instance.register_commands()

    # Initialize the status indicator in the Binary Ninja status bar
    init_status_indicator(plugin_instance)
