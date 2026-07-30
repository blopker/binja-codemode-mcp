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

    Ask Binary Ninja rather than guessing a platform- or install-specific path.
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
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError):
        return DEFAULT_API_KEY
    # Valid JSON that is not an object — `[]`, `null`, a bare number — used to
    # reach `.get()` and raise, which the caller turned into "failed to start".
    # A config file the server cannot read should never stop it running.
    if not isinstance(loaded, dict):
        return DEFAULT_API_KEY
    key = loaded.get("api_key")
    return key if isinstance(key, str) and key else DEFAULT_API_KEY


@dataclass
class Config:
    """Runtime configuration."""

    host: str = "127.0.0.1"
    port: int = 42069
    api_key: str = ""

    # The print() cap, and the write-time memory guard that stops an abandoned
    # timed-out script growing a buffer forever. Distinct from the response
    # budget, which is MAX_RESULT_BYTES in plugin/mcp.py — they share a scale,
    # not a purpose. Keep max_output_bytes + MAX_ERROR_BYTES < MAX_RESULT_BYTES
    # or a result gets truncated twice, with two notices.
    #
    # ~8k tokens. 100 KB fits in memory fine but overruns a client's per-result
    # budget, which spills the response to a file where the model cannot see it
    # inline — a cap that prevents a hang but not a lost answer.
    max_output_bytes: int = 32_000
    execution_timeout_s: float = 30.0
    # How long a second call waits for the first rather than being refused.
    # Clients issue tool calls in parallel, and the ordinary script finishes in
    # well under a second, so a short queue turns a spurious failure into a
    # brief wait. Past this, the script is genuinely long-running and saying so
    # is more useful than blocking.
    queue_wait_s: float = 5.0

    data_dir: Path = field(default_factory=default_data_dir)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = load_api_key(self.data_dir)

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
