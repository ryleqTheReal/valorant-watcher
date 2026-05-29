"""singleton configuration manager; first call to ConfigManager() loads config.json, cfg.reload() re-reads from disk"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import final

from utils.models import AppConfig
from utils.file_utils import get_config_path


@final
class ConfigManager:
    """thread-safe, lazy-loading singleton that exposes an AppConfig"""

    _instance: ConfigManager | None = None
    _lock = Lock()
    _path: Path | None = None
    _config: AppConfig | None = None

    def __new__(cls, path: Path | None = None) -> ConfigManager:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._path = get_config_path()
                inst._config = None
                cls._instance = inst
        return cls._instance

    @property
    def config(self) -> AppConfig:
        """current AppConfig; loads from disk on first access"""

        if self._config is None:
            return self._load()
        return self._config

    def reload(self) -> AppConfig:
        """re-read config.json from disk"""
        return self._load()


    def _load(self) -> AppConfig:
        with self._lock:
            if not self._path:
                self._path = get_config_path()
            raw = json.loads(self._path.read_text(encoding="utf-8"))    # pyright: ignore[reportAny]
            self._config = AppConfig.from_config_dict(raw)  # pyright: ignore[reportAny]
            return self._config



    @classmethod
    def _reset(cls) -> None:  # pyright: ignore[reportUnusedFunction]
        """destroy the singleton (testing only)"""
        with cls._lock:
            cls._instance = None
