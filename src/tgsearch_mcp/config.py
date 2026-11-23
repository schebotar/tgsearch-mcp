from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import __version__
from .errors import TgSearchError

ENV_HOME = "TGSEARCH_HOME"


def home_dir() -> Path:
    raw = os.environ.get(ENV_HOME, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".tgsearch-mcp"


def config_path() -> Path:
    return home_dir() / "config.json"


def index_path() -> Path:
    raw = os.environ.get("TGSEARCH_INDEX", "").strip()
    return Path(raw).expanduser() if raw else home_dir() / "index.sqlite3"


@dataclass
class Config:
    api_id: int | None = None
    api_hash: str | None = None
    session: str | None = None
    timeout: float = 60.0
    dialog_limit: int = 1000
    include_archived: bool = True
    dialog_ttl: float = 300.0
    device_model: str = "tgsearch-mcp"
    system_version: str = "Linux"
    app_version: str = __version__

    @property
    def api_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def session_configured(self) -> bool:
        return bool(self.session)

    def require_api(self) -> None:
        if self.api_configured:
            return
        raise TgSearchError(
            "not_configured",
            "Telegram API credentials are missing.",
            "Create an app at https://my.telegram.org (API development tools), "
            "then run `tgsearch-mcp login` or set TG_API_ID and TG_API_HASH.",
        )


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _as_bool(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _apply_env(cfg: Config) -> None:
    api_id = _env("TG_API_ID")
    if api_id:
        try:
            cfg.api_id = int(api_id)
        except ValueError as exc:
            raise TgSearchError("not_configured", "TG_API_ID must be an integer.") from exc
    api_hash = _env("TG_API_HASH")
    if api_hash:
        cfg.api_hash = api_hash
    session = _env("TG_SESSION_STRING")
    if session:
        cfg.session = session
    timeout = _env("TGSEARCH_TIMEOUT")
    if timeout:
        cfg.timeout = float(timeout)
    dialog_limit = _env("TGSEARCH_DIALOG_LIMIT")
    if dialog_limit:
        cfg.dialog_limit = int(dialog_limit)
    include_archived = _env("TGSEARCH_INCLUDE_ARCHIVED")
    if include_archived is not None:
        cfg.include_archived = _as_bool(include_archived)
    dialog_ttl = _env("TGSEARCH_DIALOG_TTL")
    if dialog_ttl:
        cfg.dialog_ttl = float(dialog_ttl)
    device_model = _env("TGSEARCH_DEVICE_MODEL")
    if device_model:
        cfg.device_model = device_model


def load_config() -> Config:
    cfg = Config()
    path = config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for field_name in (
                "api_id",
                "api_hash",
                "session",
                "timeout",
                "dialog_limit",
                "include_archived",
                "dialog_ttl",
                "device_model",
                "system_version",
                "app_version",
            ):
                value = data.get(field_name)
                if value is not None:
                    setattr(cfg, field_name, value)
    _apply_env(cfg)
    return cfg


def save_config(cfg: Config) -> None:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(home, 0o700)
    except OSError:
        pass
    path = config_path()
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(asdict(cfg), indent=2, sort_keys=True) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
