"""
Backend communication service.

Two token scopes: app (user info) and game (app + bound PUUID, for match/history/task).
Both have access/refresh pairs (access ~1d, refresh ~30d); access tokens refreshed at 75% of lifetime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from services.auth_service import RiotSession
from services.event_bus import Event, EventBus
from utils.exceptions import BackendAuthError
from utils.file_utils import get_auth_tokens_path
from utils.hardware import HardwareSnapshot
from utils.models import UserInfo, UserInfoResponse
from utils.hardware_mac import MacHardwareSnapshot



logger: logging.Logger = logging.getLogger(__name__)

# error codes indicating a permanently dead token; no amount of retrying/refreshing will recover it
_DEAD_TOKEN_ERRORS: set[str] = {
    "REFRESH_TOKEN_EXPIRED",
    "TOKEN_DECODE_ERROR",
    "WRONG_TOKEN_TYPE",
    "WRONG_TOKEN_SCOPE",
    "USER_NOT_FOUND",
    "USER_IS_BANNED",
}


@dataclass
class TokenPair:
    """access/refresh token pair; expiry timestamps are Unix seconds (UTC)"""

    access_token: str
    access_token_expires_at: int
    refresh_token: str
    refresh_token_expires_at: int

    def access_expires_in(self) -> float:
        return self.access_token_expires_at - time.time()

    def refresh_expires_in(self) -> float:
        return self.refresh_token_expires_at - time.time()

    def is_refresh_expired(self, skew: float = 60.0) -> bool:
        return self.refresh_expires_in() <= skew


@dataclass
class StoredSession:
    """persisted session holding the app token pair"""
    app: TokenPair


class BackendCommunicationService:
    """drives the backend auth lifecycle off the event bus"""

    def __init__(
        self,
        bus: EventBus,
        server_base_url: str,
        tokens_path: Path | None = None,
    ) -> None:
        self.bus: EventBus = bus
        self.base_url: str = self._normalize_base_url(server_base_url)
        self.tokens_path: Path = tokens_path or get_auth_tokens_path()

        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=20.0)

        self._hwid_hash: str | None = None
        self._session: RiotSession | None = None

        self._app_tokens: TokenPair | None = None
        self._game_tokens: TokenPair | None = None
        self._bound_puuid: str | None = None
        self._bound_shard: str | None = None

        self._auth_lock: asyncio.Lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None
        self._cancelled: asyncio.Event = asyncio.Event()

        self._register()

    # ------------------- Registration -------------------

    def _register(self) -> None:
        _ = self.bus.on(Event.HARDWARE_COLLECTED, self._on_hardware, priority=5)
        _ = self.bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=5)
        _ = self.bus.on(Event.USERINFO_FETCHED, self._on_userinfo, priority=5)
        _ = self.bus.on(Event.RSO_LOGOUT, self._on_logout, priority=0)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_hardware(self, snapshot: MacHardwareSnapshot | HardwareSnapshot) -> None:
        hwid: str | None = snapshot.hwid_hash
        if not hwid:
            logger.warning("HARDWARE_COLLECTED arrived without hwid_hash; backend auth will be blocked.")
            return
        self._hwid_hash = hwid
        async with self._auth_lock:
            _ = await self._ensure_app_token()

    async def _on_auth_success(self, data: dict[str, RiotSession]) -> None:
        session: RiotSession | None = data.get("session")
        self._session = session

    async def _on_userinfo(self, userinfo: UserInfoResponse) -> None:
        """mint a game token bound to the just-logged-in Riot account"""

        if not self._session:
            return
    
        puuid = self._extract_puuid(userinfo) or self._session.puuid
        if not puuid:
            logger.error("Could not determine puuid from USERINFO_FETCHED; game token blocked.")
            return

        async with self._auth_lock:
            # hardware event may not have fired yet or may have failed -> retry now
            if self._app_tokens is None and not await self._ensure_app_token():
                logger.error("Cannot mint game token: app token unavailable.")
                return
            _ = await self._ensure_game_token(puuid=puuid, shard=self._session.region.pd_shard)

    async def _on_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        # drop game token, keep app token alive
        async with self._auth_lock:
            self._game_tokens = None
            self._bound_puuid = None
            self._bound_shard = None
            self._save_session()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        self._cancelled.set()
        await self._teardown()
        await self._client.aclose()

    # ------------------- Authentication -------------------

    async def _ensure_app_token(self) -> bool:
        """ensure app tokens are set; refreshes from disk if available, otherwise runs Discord OAuth -> POST /v1/login"""
        if self._app_tokens is not None and not self._app_tokens.is_refresh_expired():
            return True

        stored = self._load_session()

        if stored is not None and not stored.app.is_refresh_expired():
            logger.info("Refreshing backend app token using stored refresh_token")
            try:
                self._app_tokens = await self._post_refresh_app(stored.app.refresh_token)
            except BackendAuthError as e:
                self._log_refresh_failure("app", e)
                self._app_tokens = None
                if e.error_code not in _DEAD_TOKEN_ERRORS:
                    return False
            else:
                self._save_session()
                self._start_proactive_refresh()
                logger.info(
                    f"App token refreshed. Access valid {self._app_tokens.access_expires_in()}s, "  # pyright: ignore[reportImplicitStringConcatenation]
                    f"refresh valid {self._app_tokens.refresh_expires_in()}s."
                )
                return True

        logger.info("Running Discord OAuth loopback login for app token")
        proof = await self._discord_login()
        if proof is None:
            logger.error("Discord login was aborted; app token unavailable.")
            return False

        provider, provider_id, oauth_access = proof
        try:
            if not self._hwid_hash:
                logger.error("HWID was not available during login request building.")
                return False
            
            self._app_tokens = await self._post_login(
                hwid=self._hwid_hash,
                provider=provider,
                provider_id=provider_id,
                access_token=oauth_access,
            )
        except BackendAuthError as e:
            logger.error(f"POST /v1/login rejected the request: HTTP {e.status_code} error_code={e.error_code}")
            return False

        # fresh login invalidates any previously-bound game token
        self._game_tokens = None
        self._bound_puuid = None
        self._bound_shard = None
        self._save_session()
        self._start_proactive_refresh()
        logger.info(
            f"App login complete. Access valid {self._app_tokens.access_expires_in()}s, "  # pyright: ignore[reportImplicitStringConcatenation]
            f"refresh valid {self._app_tokens.refresh_expires_in() / 86400}d."
        )
        return True

    async def _ensure_game_token(self, puuid: str, shard: str) -> bool:
        """ensure game tokens are bound to (puuid, shard)"""
        assert self._app_tokens is not None

        # same account with a healthy refresh token -> refresh rather than minting a new one
        if (
            self._game_tokens is not None
            and self._bound_puuid == puuid
            and self._bound_shard == shard
            and not self._game_tokens.is_refresh_expired()
        ):
            try:
                self._game_tokens = await self._post_refresh_game(self._game_tokens.refresh_token)
            except BackendAuthError as e:
                self._log_refresh_failure("game", e)
                self._game_tokens = None
            else:
                self._save_session()
                logger.info(
                    f"Game token refreshed for same account. Access valid {self._game_tokens.access_expires_in()}s."
                )
                return True

        logger.info(f"Minting fresh game token (puuid={puuid[:8]}…, shard={shard})")
        try:
            self._game_tokens = await self._post_game_token(
                app_access_token=self._app_tokens.access_token,
                puuid=puuid,
                shard=shard,
            )
        except BackendAuthError as e:
            logger.error(
                f"POST /v1/auth/game-token rejected the request: HTTP {e.status_code} error_code={e.error_code}"
            )
            self._game_tokens = None
            self._bound_puuid = None
            self._bound_shard = None
            return False

        self._bound_puuid = puuid
        self._bound_shard = shard
        self._save_session()
        logger.info(
            f"Game token issued. Access valid {self._game_tokens.access_expires_in()}s, "  # pyright: ignore[reportImplicitStringConcatenation]
            f"refresh valid {self._game_tokens.refresh_expires_in() / 86400}d."
        )
        return True

    @staticmethod
    def _log_refresh_failure(scope: str, e: BackendAuthError) -> None:
        if e.error_code in _DEAD_TOKEN_ERRORS:
            logger.warning(f"Stored {scope} refresh_token is dead ({e.error_code}).")
        else:
            logger.warning(f"{scope.capitalize()} refresh failed: HTTP {e.status_code} error_code={e.error_code}")

    async def _discord_login(self) -> tuple[str, str, str] | None:
        """Discord OAuth via loopback redirect; binds a local HTTP server, opens the auth URL in the browser,
        and waits for the callback with provider/provider_id/access_token in the query string"""
        loop = asyncio.get_running_loop()
        received: asyncio.Future[dict[str, str]] = loop.create_future()

        server = await asyncio.start_server(
            lambda r, w: self._handle_loopback(r, w, received),
            host="127.0.0.1",
            port=0,
        )
        sockets = server.sockets
        if not sockets:
            server.close()
            await server.wait_closed()
            logger.error("Loopback server failed to bind a socket.")
            return None

        actual_port: int = sockets[0].getsockname()[1]  # pyright: ignore[reportAny]
        loopback_url = f"http://127.0.0.1:{actual_port}/callback"

        try:
            try:
                url_response = await self._client.get(
                    f"{self.base_url}/v1/auth/discord",
                    params={"redirect": "false", "loopback_redirect": loopback_url},
                )
                _ = url_response.raise_for_status()
            except httpx.HTTPError as e:
                logger.error(f"Could not fetch Discord auth URL from backend: {e}")
                return None

            auth_url = url_response.text.strip().strip('"')

            logger.info(f"Opening Discord OAuth in browser; loopback {loopback_url}")
            try:
                _ = webbrowser.open(auth_url, new=2)
            except Exception:
                logger.warning(f"Could not open browser automatically. Open this URL manually: {auth_url}")

            try:
                result = await asyncio.wait_for(received, timeout=_LOGIN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.error(
                    f"OAuth loopback timed out after {_LOGIN_TIMEOUT_SECONDS}s; aborting login."
                )
                return None

            return result["provider"], result["provider_id"], result["access_token"]
        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  
                pass

    @staticmethod
    async def _handle_loopback(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        received: asyncio.Future[dict[str, str]],
    ) -> None:
        """one-shot HTTP handler for the OAuth loopback callback"""
        try:
            request_line_bytes = await reader.readline()
            request_line = request_line_bytes.decode("latin-1", errors="replace")
            parts = request_line.split()
            target = parts[1] if len(parts) >= 2 else ""

            # drain headers; we don't need them
            while True:
                header = await reader.readline()
                if not header or header in (b"\r\n", b"\n"):
                    break

            parsed = urlparse(target)
            if parsed.path != "/callback":
                _write_http_response(writer, 404, "text/plain; charset=utf-8", b"Not found.")
                await writer.drain()
                return

            params = parse_qs(parsed.query)
            provider = params.get("provider", [""])[0]
            provider_id = params.get("provider_id", [""])[0]
            access_token = params.get("access_token", [""])[0]

            if provider and provider_id and access_token:
                if not received.done():
                    received.set_result({
                        "provider": provider,
                        "provider_id": provider_id,
                        "access_token": access_token,
                    })
                _write_http_response(
                    writer, 200, "text/html; charset=utf-8",
                    _LOGIN_SUCCESS_HTML.encode("utf-8"),
                )
            else:
                _write_http_response(
                    writer, 400, "text/html; charset=utf-8",
                    _LOGIN_FAILURE_HTML.encode("utf-8"),
                )
            await writer.drain()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Loopback handler error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # ------------------- HTTP -------------------

    async def _post_login(
        self,
        hwid: str,
        provider: str,
        provider_id: str,
        access_token: str,
    ) -> TokenPair:
        return await self._post_token(
            "/v1/login",
            json={
                "hwid": hwid,
                "provider": provider,
                "provider_id": provider_id,
                "access_token": access_token,
            },
        )

    async def _post_game_token(
        self,
        app_access_token: str,
        puuid: str,
        shard: str,
    ) -> TokenPair:
        return await self._post_token(
            "/v1/auth/game-token",
            json={"puuid": puuid, "shard": shard},
            headers={"Authorization": f"Bearer {app_access_token}"},
        )

    async def _post_refresh_app(self, refresh_token: str) -> TokenPair:
        return await self._post_token(
            "/v1/auth/refresh/app",
            json={"refresh_token": refresh_token},
        )

    async def _post_refresh_game(self, refresh_token: str) -> TokenPair:
        return await self._post_token(
            "/v1/auth/refresh/game",
            json={"refresh_token": refresh_token},
        )

    async def _post_token(
        self,
        path: str,
        json: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        headers: dict[str, str] | None = None,
    ) -> TokenPair:
        """POST helper; raises BackendAuthError on transport failure"""
        try:
            response = await self._client.post(f"{self.base_url}{path}", json=json, headers=headers)
        except httpx.HTTPError as e:
            raise BackendAuthError(status_code=None, error_code="NETWORK_ERROR", message=str(e)) from None
        return self._parse_token_response(response)

    @staticmethod
    def _parse_token_response(response: httpx.Response) -> TokenPair:
        if response.status_code != 200:
            error_code: str | None = None
            try:
                body = response.json()  # pyright: ignore[reportAny]
                raw = body.get("error_code")
                if isinstance(raw, str):
                    error_code = raw
            except Exception:  # noqa: BLE001
                pass
            raise BackendAuthError(status_code=response.status_code, error_code=error_code)

        body: dict[str, Any] = response.json()  # pyright: ignore[reportExplicitAny, reportAny]
        return TokenPair(
            access_token=str(body["access_token"]),  # pyright: ignore[reportAny]
            access_token_expires_at=int(body["access_token_expires_at"]),  # pyright: ignore[reportAny]
            refresh_token=str(body["refresh_token"]),  # pyright: ignore[reportAny]
            refresh_token_expires_at=int(body["refresh_token_expires_at"]),  # pyright: ignore[reportAny]
        )

    # ------------------- Persistence -------------------

    def _load_session(self) -> StoredSession | None:
        if not self.tokens_path.exists():
            return None
        try:
            raw: dict[str, Any] = json.loads(self.tokens_path.read_text(encoding="utf-8"))  # pyright: ignore[reportExplicitAny, reportAny]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read tokens file {self.tokens_path}: {e}")
            return None
        try:
            app_raw: dict[str, Any] = raw["app"]  # pyright: ignore[reportExplicitAny, reportAny]
            app = TokenPair(
                access_token=str(app_raw["access_token"]),  # pyright: ignore[reportAny]
                access_token_expires_at=int(app_raw["access_token_expires_at"]),  # pyright: ignore[reportAny]
                refresh_token=str(app_raw["refresh_token"]),  # pyright: ignore[reportAny]
                refresh_token_expires_at=int(app_raw["refresh_token_expires_at"]),  # pyright: ignore[reportAny]
            )
            
            return StoredSession(app=app)
            
        except (KeyError, TypeError, ValueError) as e:
            logger.warning(f"Stored app token at {self.tokens_path} is malformed: {e}")
            return None

    def _save_session(self) -> None:
        if self._app_tokens is None:
            return
        payload: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
            "app": asdict(self._app_tokens),
        }
        try:
            self.tokens_path.parent.mkdir(parents=True, exist_ok=True)
            _ = self.tokens_path.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning(f"Could not persist app token to {self.tokens_path}: {e}")

    # ------------------- Proactive Refresh -------------------

    def _start_proactive_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            _ = self._refresh_task.cancel()
        self._refresh_task = asyncio.create_task(self._proactive_refresh_loop())

    async def _proactive_refresh_loop(self) -> None:
        """refresh whichever access token expires soonest"""
        while not self._cancelled.is_set():
            if self._app_tokens is None:
                return

            # pick the pair whose access token expires soonest
            app_remaining = self._app_tokens.access_expires_in()
            game_remaining = (
                self._game_tokens.access_expires_in()
                if self._game_tokens is not None
                else float("inf")
            )

            if app_remaining <= game_remaining:
                sleep_for = max(app_remaining * 0.75, 0.0)
                scope = "app"
            else:
                sleep_for = max(game_remaining * 0.75, 0.0)
                scope = "game"

            if await self._cancellable_sleep(sleep_for):
                return

            if self._app_tokens is None:  # pyright: ignore[reportUnnecessaryComparison]
                return

            try:
                if scope == "app":
                    self._app_tokens = await self._post_refresh_app(self._app_tokens.refresh_token)
                else:
                    assert self._game_tokens is not None
                    self._game_tokens = await self._post_refresh_game(self._game_tokens.refresh_token)
            except BackendAuthError as e:
                if e.error_code in _DEAD_TOKEN_ERRORS:
                    logger.error(
                        f"Proactive {scope} refresh permanently failed ({e.error_code}); session is dead."
                    )
                    self._app_tokens = None
                    self._game_tokens = None
                    return
                logger.warning(
                    f"Proactive {scope} refresh failed (HTTP {e.status_code} {e.error_code}); retrying in 60s."
                )
                if await self._cancellable_sleep(60.0):
                    return
                continue

            self._save_session()
            logger.info(
                f"Backend {scope} tokens refreshed proactively. "  # pyright: ignore[reportImplicitStringConcatenation]
                f"Access valid {(self._app_tokens if scope == 'app' else self._game_tokens).access_expires_in():.0f}s."  # pyright: ignore[reportOptionalMemberAccess]
            )

    async def _cancellable_sleep(self, seconds: float) -> bool:
        """sleep for `seconds`; returns True if cancelled, False on timeout"""
        if seconds <= 0:
            return self._cancelled.is_set()
        try:
            _ = await asyncio.wait_for(self._cancelled.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _teardown(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            _ = self._refresh_task.cancel()
        self._refresh_task = None
        self._session = None
        self._app_tokens = None
        self._game_tokens = None
        self._bound_puuid = None
        self._bound_shard = None

    # ------------------- Helpers -------------------

    @staticmethod
    def _normalize_base_url(url: str) -> str:
        url = url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        return url

    @staticmethod
    def _extract_puuid(userinfo: UserInfoResponse) -> str | None:
        info: UserInfo | str | None = userinfo.userInfo
        if isinstance(info, UserInfo):
            sub = info.sub
            if isinstance(sub, str) and sub:
                return sub
        return None

    @property
    def app_access_token(self) -> str | None:
        """current app access token, or None if not authenticated"""
        return self._app_tokens.access_token if self._app_tokens else None

    @property
    def game_access_token(self) -> str | None:
        """current game access token, or None if not authenticated"""
        return self._game_tokens.access_token if self._game_tokens else None

    @property
    def game_headers(self) -> dict[str, str] | None:
        """headers for game-scoped backend endpoints, or None if no game token held"""
        if self._game_tokens is None or self._bound_puuid is None or self._bound_shard is None:
            return None
        return {
            "Authorization": f"Bearer {self._game_tokens.access_token}",
            "puuid": self._bound_puuid,
            "shard": self._bound_shard,
        }


# ------------------- Module-level helpers -------------------

_LOGIN_TIMEOUT_SECONDS: float = 300.0


def _write_http_response(
    writer: asyncio.StreamWriter,
    status_code: int,
    content_type: str,
    body: bytes,
) -> None:
    """write a minimal HTTP/1.1 response to `writer`"""
    reason = {
        200: "OK",
        303: "See Other",
        400: "Bad Request",
        404: "Not Found",
    }.get(status_code, "Status")
    head = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Cache-Control: no-store\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("ascii")
    writer.write(head + body)


_LOGIN_SUCCESS_HTML = (
    "<!doctype html>"
    "<html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Signed in</title>"
    "<style>"
    "html,body{height:100%;margin:0}"
    "body{font:16px/1.4 -apple-system,'Segoe UI',Roboto,sans-serif;"
    "background:#0f1115;color:#e6e6e6;display:grid;place-items:center}"
    ".card{padding:2rem 2.5rem;border:1px solid #262b36;border-radius:12px;text-align:center;max-width:24rem}"
    "h1{margin:0 0 .5rem;color:#4ade80;font-size:1.4rem}"
    "p{margin:0;color:#9aa0aa}"
    "</style></head><body>"
    "<div class=\"card\"><h1>You're signed in</h1>"
    "<p>You can close this tab and return to the app.</p></div>"
    "</body></html>"
)

_LOGIN_FAILURE_HTML = (
    "<!doctype html>"
    "<html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>Login error</title>"
    "<style>"
    "html,body{height:100%;margin:0}"
    "body{font:16px/1.4 -apple-system,'Segoe UI',Roboto,sans-serif;"
    "background:#0f1115;color:#e6e6e6;display:grid;place-items:center}"
    ".card{padding:2rem 2.5rem;border:1px solid #262b36;border-radius:12px;text-align:center;max-width:24rem}"
    "h1{margin:0 0 .5rem;color:#f87171;font-size:1.4rem}"
    "p{margin:0;color:#9aa0aa}"
    "</style></head><body>"
    "<div class=\"card\"><h1>Login failed</h1>"
    "<p>The OAuth callback didn't include the expected fields. Try again.</p></div>"
    "</body></html>"
)
