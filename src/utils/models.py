"""
Shared data models for the Valorant Stats App.

Dataclasses used across multiple modules live here.
Internal dataclasses (e.g. Listener in the EventBus)
stay in their own module.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LockfileData:
    """
    Parsed data from the Riot Client lockfile.

    The lockfile contains a single line in the format:
        name:pid:port:password:protocol
        e.g.: riot:12345:52735:abcdefg123:https
    """

    name: str
    pid: int
    port: int
    password: str
    protocol: str

    @classmethod
    def from_file(cls, path: Path) -> LockfileData:
        """Read and parse the lockfile"""
        
        content = path.read_text(encoding="utf-8").strip()
        parts = content.split(":")

        if len(parts) != 5:
            raise ValueError(
                f"Unexpected lockfile format (expected 5 parts, received {len(parts)}): {content}")

        return cls(
            name=parts[0],
            pid=int(parts[1]),
            port=int(parts[2]),
            password=parts[3],
            protocol=parts[4],
        )

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://127.0.0.1:{self.port}"

    @property
    def auth_header(self) -> str:
        """Basic auth header for the local Riot API."""
        token = base64.b64encode(f"riot:{self.password}".encode()).decode()
        return f"Basic {token}"


@dataclass
class AppConfig:
    """Application config loaded from config.json."""

    server_base_url: str | None = None 
    poll_interval: int = 3
    collect_interval: int = 60
    enable_data_sending: bool = True

    @classmethod
    def from_config_dict(cls, data: dict[str, object]) -> AppConfig:
        """Create an AppConfig from a config dictionary obtained by config.json

        Args:
            data (dict[str, object]): The parsed config.json dict

        Returns:
            AppConfig: Returns the AppConfig object
        """
        
        # The defined fields of the dataclass: server_base_url, poll_interval ...
        known_fields = cls.__dataclass_fields__
        kwargs = {k: v for k, v in data.items() if k in known_fields}
        return cls(**kwargs)    # pyright: ignore[reportArgumentType]
