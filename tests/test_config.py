import json

import pytest

from tgsearch_mcp.config import Config, load_config, save_config
from tgsearch_mcp.errors import TgSearchError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TGSEARCH_HOME", str(tmp_path))
    for name in (
        "TG_API_ID",
        "TG_API_HASH",
        "TG_SESSION_STRING",
        "TGSEARCH_TIMEOUT",
        "TGSEARCH_DIALOG_LIMIT",
        "TGSEARCH_INCLUDE_ARCHIVED",
        "TGSEARCH_DIALOG_TTL",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_defaults_without_config_file():
    cfg = load_config()
    assert cfg.api_configured is False
    assert cfg.session_configured is False
    assert cfg.timeout == 60.0
    assert cfg.include_archived is True


def test_save_and_load_roundtrip(tmp_path):
    cfg = Config(api_id=123, api_hash="abc", session="sess", timeout=15.0)
    save_config(cfg)
    path = tmp_path / "config.json"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"
    assert json.loads(path.read_text())["api_id"] == 123

    loaded = load_config()
    assert loaded.api_id == 123
    assert loaded.api_hash == "abc"
    assert loaded.session == "sess"
    assert loaded.timeout == 15.0


def test_env_overrides_file(monkeypatch):
    save_config(Config(api_id=1, api_hash="file", timeout=60.0))
    monkeypatch.setenv("TG_API_ID", "42")
    monkeypatch.setenv("TG_API_HASH", "env")
    monkeypatch.setenv("TGSEARCH_TIMEOUT", "5")
    monkeypatch.setenv("TGSEARCH_INCLUDE_ARCHIVED", "false")
    cfg = load_config()
    assert (cfg.api_id, cfg.api_hash) == (42, "env")
    assert cfg.timeout == 5.0
    assert cfg.include_archived is False


def test_invalid_api_id_in_env(monkeypatch):
    monkeypatch.setenv("TG_API_ID", "not-a-number")
    with pytest.raises(TgSearchError) as excinfo:
        load_config()
    assert excinfo.value.code == "not_configured"


def test_require_api_raises_with_hint():
    with pytest.raises(TgSearchError) as excinfo:
        Config().require_api()
    assert excinfo.value.code == "not_configured"
    assert "my.telegram.org" in (excinfo.value.hint or "")


def test_xdg_layout_without_tgsearch_home(monkeypatch, tmp_path):
    from tgsearch_mcp.config import config_path, index_path

    monkeypatch.delenv("TGSEARCH_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert config_path() == tmp_path / "cfg" / "tgsearch-mcp" / "config.json"
    assert index_path() == tmp_path / "data" / "tgsearch-mcp" / "index.sqlite3"


def test_relative_xdg_values_are_ignored(monkeypatch, tmp_path):
    from tgsearch_mcp.config import config_path, index_path

    monkeypatch.delenv("TGSEARCH_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/cfg")
    monkeypatch.setenv("XDG_DATA_HOME", "")

    assert config_path() == tmp_path / ".config" / "tgsearch-mcp" / "config.json"
    assert index_path() == tmp_path / ".local" / "share" / "tgsearch-mcp" / "index.sqlite3"


def test_tgsearch_home_overrides_xdg(monkeypatch, tmp_path):
    from tgsearch_mcp.config import config_path, index_path

    monkeypatch.setenv("TGSEARCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert config_path() == tmp_path / "home" / "config.json"
    assert index_path() == tmp_path / "home" / "index.sqlite3"


def test_index_path_env_override(monkeypatch, tmp_path):
    from tgsearch_mcp.config import index_path

    monkeypatch.setenv("TGSEARCH_INDEX", str(tmp_path / "custom.sqlite3"))
    assert index_path() == tmp_path / "custom.sqlite3"


def test_save_config_creates_private_xdg_dir(monkeypatch, tmp_path):
    import stat

    monkeypatch.delenv("TGSEARCH_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    save_config(Config(api_id=7, api_hash="h", session="s"))

    path = tmp_path / "cfg" / "tgsearch-mcp" / "config.json"
    assert path.exists()
    assert stat.S_IMODE((tmp_path / "cfg" / "tgsearch-mcp").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
