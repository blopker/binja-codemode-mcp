"""Configuration for the Code Mode MCP server.

Pure module: importable without Binary Ninja.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

# Localhost-only access token. Not a security boundary on its own — the server
# binds 127.0.0.1 and validates Origin. The token stops other local software
# from stumbling into the endpoint.
DEFAULT_API_KEY = "binja-codemode-local"


def default_data_dir() -> Path:
    """Resolve Binary Ninja's user directory.

    Ask Binary Ninja rather than guessing per-platform; it knows where its own
    user folder is, which differs between installs (this machine uses
    ~/.binaryninja, not ~/Library/Application Support).
    """
    try:
        import binaryninja

        user_dir = binaryninja.user_directory()
        if user_dir:
            return Path(user_dir) / "codemode_mcp"
    except Exception:
        pass
    return Path.home() / ".binaryninja" / "codemode_mcp"


def load_api_key(data_dir: Path) -> str:
    """Read the API key from config.json, falling back to the default."""
    config_file = data_dir / "config.json"
    if not config_file.exists():
        return DEFAULT_API_KEY
    try:
        with open(config_file) as f:
            return json.load(f).get("api_key") or DEFAULT_API_KEY
    except (json.JSONDecodeError, OSError):
        return DEFAULT_API_KEY


@dataclass
class Config:
    """Runtime configuration."""

    host: str = "127.0.0.1"
    port: int = 42069
    api_key: str = ""

    # ~8k tokens. 100 KB fits in memory fine but overruns a client's
    # per-result budget, which spills the response to a file where the model
    # cannot see it inline — a cap that prevents a hang but not a lost answer.
    max_output_bytes: int = 32_000
    execution_timeout_s: float = 30.0

    data_dir: Path = field(default_factory=default_data_dir)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = load_api_key(self.data_dir)

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
