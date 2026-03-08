"""
Shared data models for the Valorant Stats App.

Dataclasses used across multiple modules live here.
Internal dataclasses (e.g. Listener in the EventBus)
stay in their own module.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Literal, TypeVar

from urllib.parse import urlparse, ParseResult
from utils.exceptions import EndpointValidationError

T = TypeVar("T")

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
                f"Unexpected lockfile format (expected 5 parts, received {len(parts)}): {content}"
            )

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
    def wss_url(self) -> str:
        return f"wss://127.0.0.1:{self.port}"
    
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
    ratelimit_timeout: int = 60

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
        return cls(**kwargs)  # pyright: ignore[reportArgumentType]


@dataclass(frozen=True)
class RegionInfo:
    """Region data extracted from Valorant log files.

    Attributes:
        pd_shard:  Platform-data shard  (e.g. "eu", "na", "ap", "kr")
        glz_shard: GLZ shard            (e.g. "eu-1", "na-1")
        glz_region: GLZ region          (e.g. "eu", "na")
    """

    pd_shard: str
    glz_shard: str
    glz_region: str


@dataclass(frozen=True)
class EndpointURI:
    uri: str
    parsed_endpoint: ParseResult

    def __init__(self, endpoint: str) -> None:
        parsed = urlparse(endpoint)

        if not parsed.path.startswith("/"):
            raise EndpointValidationError(
                message=f"Endpoint must start with '/': '{endpoint}'"
            )
        if parsed.scheme or parsed.netloc:
            raise EndpointValidationError(
                message=f"Endpoint must be a path, not a full URL: '{endpoint}'"
            )

        # Not using self.uri because that would raise Error due to frozen=True immutability !
        object.__setattr__(self, "uri", endpoint)
        object.__setattr__(self, "parsed_endpoint", parsed)


# ------------ API Response dataclasses ------------


@dataclass
class ValorantApiResponse[T]:
    """Generic wrapper for valorant-api.com responses.

    Attributes:
        data: The parsed response payload, typed per endpoint.
        status: The HTTP status code of the response.
        error: Optional error message if the request failed.
    """

    data: T
    status: HTTPStatus
    error: str | None = None

@dataclass
class VersionData:
    """Response obtained by https://valorant-api.com/v1/version"""
    
    manifestId: str
    branch: str
    version: str
    buildVersion: str
    engineVersion: str
    riotClientVersion: str
    riotClientBuild: str
    buildDate: str 
                
# ------------ Riot API Responses ------------

@dataclass
class EntitlementsTokenResponse:
    """Response obtained by https://127.0.0.1:{port}/entitlements/v1/token"""
    accessToken: str
    entitlements: tuple[Any]  # pyright: ignore[reportExplicitAny]
    issuer: str
    # Player UUID
    subject: str
    # Used as the entitlement in requests
    token: str
    message: Literal["Entitlements token is not ready yet", "Invalid URI format"] | str | None = None


@dataclass
class _JWTPartialPayload:
    """Nested object inside the JWT payload (e.g. "pp", "dat", "plt")."""
    c: str = ""

@dataclass
class _JWTPlatform:
    """Platform info from the JWT payload."""
    dev: str = ""
    id: str = ""

@dataclass
class _JWTDat:
    """Session data from the JWT payload."""
    c: str = ""
    lid: str = ""

@dataclass
class AccessTokenJWT:
    """Decoded payload of the Riot access token JWT.

    Constructed via: AccessTokenJWT(**decoded_payload)
    """
    sub: str
    iss: str
    exp: int
    iat: int
    jti: str
    cid: str
    cty: str = ""
    lcty: str = ""
    acr: str = ""
    prm: str = ""
    pp: _JWTPartialPayload | dict[str, str] | None = None
    scp: list[str] | None = None
    clm: list[str] | None = None
    amr: list[str] | None = None
    dat: _JWTDat | dict[str, str] | None = None
    plt: _JWTPlatform | dict[str, str] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.pp, dict):
            self.pp = _JWTPartialPayload(**self.pp)
        if isinstance(self.dat, dict):
            self.dat = _JWTDat(**self.dat)
        if isinstance(self.plt, dict):
            self.plt = _JWTPlatform(**self.plt)

    