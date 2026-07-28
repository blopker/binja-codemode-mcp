"""Conventions Binary Ninja imposes on plugins, guarded here because breaking
one of them fails at load time inside the GUI where no test can see it.
"""

import ast
import json
from pathlib import Path

# tomllib is 3.11+; the project pins 3.10 to match Binary Ninja's interpreter.
import tomli

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "binja_codemode_mcp"


class TestImportOrder:
    def test_binaryninjaui_is_imported_before_pyside6(self):
        """Binary Ninja's docs require this order.

        Importing binaryninjaui first is what selects the matching PySide6
        build; the other order can load the wrong one and crash. Import sorters
        happen to produce the right order today, which is exactly why it needs
        a guard.
        """
        for path in PACKAGE.rglob("*.py"):
            # Parse: comparing raw substring positions would match the comment
            # explaining this rule, which sits above the imports and makes the
            # assertion pass no matter how the imports are ordered.
            tree = ast.parse(path.read_text())
            order = [
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            ] + [
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ]
            if "PySide6" not in order:
                continue
            assert "binaryninjaui" in order, (
                f"{path.name} imports PySide6 without binaryninjaui"
            )
            assert order.index("binaryninjaui") < order.index("PySide6"), (
                f"{path.name} must import binaryninjaui before PySide6"
            )


class TestPackaging:
    def test_entry_point_exists(self):
        """Binary Ninja loads the plugin directory as a package."""
        assert (PACKAGE / "__init__.py").is_file()

    def test_entry_point_survives_binary_ninja_being_absent(self):
        """Tooling must be able to import the package outside the GUI."""
        source = (PACKAGE / "__init__.py").read_text()
        assert "except ImportError" in source
        assert "core_ui_enabled()" in source

    def test_guide_ships_inside_the_package(self):
        """The plugin folder is what gets installed; guide.md must be in it."""
        assert (PACKAGE / "plugin" / "guide.md").is_file()

    def test_plugin_json_is_valid_and_matches_the_project_version(self):
        metadata = json.loads((ROOT / "plugin.json").read_text())
        project = tomli.loads((ROOT / "pyproject.toml").read_text())

        for field in ("name", "type", "api", "description", "license", "version"):
            assert field in metadata, field
        assert metadata["api"] == ["python3"]
        assert metadata["version"] == project["project"]["version"]

    def test_no_runtime_dependencies(self):
        """There is no requirements.txt, so the plugin must need nothing beyond
        Binary Ninja's bundled stdlib."""
        project = tomli.loads((ROOT / "pyproject.toml").read_text())
        assert project["project"]["dependencies"] == []
        assert not (ROOT / "requirements.txt").exists()
