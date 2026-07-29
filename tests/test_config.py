"""Configuration — the API key is the only thing keeping other local software
off a live analysis database, so its fallback logic is worth pinning down.
"""

import json

from binja_codemode_mcp.config import DEFAULT_API_KEY, Config, load_api_key


class TestApiKey:
    def test_a_configured_key_is_used(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"api_key": "s3cret"}))
        assert load_api_key(tmp_path) == "s3cret"
        assert Config(data_dir=tmp_path).api_key == "s3cret"

    def test_missing_file_falls_back_to_the_default(self, tmp_path):
        assert load_api_key(tmp_path) == DEFAULT_API_KEY

    def test_malformed_json_falls_back_rather_than_crashing(self, tmp_path):
        """A broken config must not stop the server starting."""
        (tmp_path / "config.json").write_text("{not json")
        assert load_api_key(tmp_path) == DEFAULT_API_KEY

    def test_a_file_without_the_key_falls_back(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"port": 1}))
        assert load_api_key(tmp_path) == DEFAULT_API_KEY

    def test_an_empty_key_does_not_disable_auth(self, tmp_path):
        """An empty string would make `Bearer ` authenticate every request."""
        (tmp_path / "config.json").write_text(json.dumps({"api_key": ""}))
        assert load_api_key(tmp_path) == DEFAULT_API_KEY
        assert Config(data_dir=tmp_path).api_key == DEFAULT_API_KEY

    def test_an_explicit_key_wins_over_the_file(self, tmp_path):
        (tmp_path / "config.json").write_text(json.dumps({"api_key": "from-file"}))
        assert Config(api_key="explicit", data_dir=tmp_path).api_key == "explicit"


class TestPaths:
    def test_ensure_dirs_is_idempotent(self, tmp_path):
        config = Config(api_key="k", data_dir=tmp_path / "nested" / "deeper")
        config.ensure_dirs()
        config.ensure_dirs()
        assert config.data_dir.is_dir()

    def test_endpoint_matches_host_and_port(self, tmp_path):
        config = Config(api_key="k", data_dir=tmp_path, host="127.0.0.1", port=9)
        assert config.endpoint == "http://127.0.0.1:9/mcp"


class TestHostileConfig:
    """A config file the server cannot make sense of must never stop it
    starting — the caller turns any exception here into "failed to start"."""

    def test_json_that_is_not_an_object_falls_back(self, tmp_path):
        from binja_codemode_mcp.config import DEFAULT_API_KEY, load_api_key

        for content in ("[]", "null", "123", '"hello"'):
            (tmp_path / "config.json").write_text(content)
            assert load_api_key(tmp_path) == DEFAULT_API_KEY, content

    def test_a_non_string_key_falls_back(self, tmp_path):
        """It gets formatted into an Authorization header, so it has to be
        a string or the header is nonsense."""
        from binja_codemode_mcp.config import DEFAULT_API_KEY, load_api_key

        hostile = ('{"api_key": 42}', '{"api_key": {"nested": 1}}', '{"api_key": ""}')
        for content in hostile:
            (tmp_path / "config.json").write_text(content)
            assert load_api_key(tmp_path) == DEFAULT_API_KEY, content
