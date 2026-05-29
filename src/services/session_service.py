"""session lifetime service; maintains an app-scoped backend session with 30s pings.

lifecycle: open on app token -> flush pending states -> emit state changes as events fire ->
ping every 30s -> end on shutdown; re-open on 404/410.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

from services.backend_service import BackendCommunicationService
from services.event_bus import Event, EventBus

logger: logging.Logger = logging.getLogger(__name__)

_PING_INTERVAL: float = 30.0

_EVENT_MAP: dict[Event, str] = {
    Event.STARTUP: "STARTUP",
    Event.RSO_LOGIN: "RSO_LOGIN",
    Event.RSO_LOGOUT: "RSO_LOGOUT",
    Event.VALORANT_OPENED: "VALORANT_OPENED",
    Event.VALORANT_CLOSED: "VALORANT_CLOSED",
    Event.PREGAME_STARTED: "PREGAME_STARTED",
    Event.PREGAME_ENDED: "PREGAME_ENDED",
    Event.MATCH_STARTED: "MATCH_STARTED",
    Event.MATCH_ENDED: "MATCH_ENDED",
    Event.SHUTDOWN: "SHUTDOWN",
}


class SessionService:
    """Drives the backend session lifecycle off the event bus."""

    def __init__(
        self,
        bus: EventBus,
        backend: BackendCommunicationService,
    ) -> None:
        self._bus = bus  # pyright: ignore[reportUnannotatedClassAttribute]
        self._backend = backend  # pyright: ignore[reportUnannotatedClassAttribute]
        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=10.0)

        self._session_id: str | None = None
        self._last_state: str | None = None
        self._pending_states: list[str] = []
        self._ping_task: asyncio.Task[None] | None = None
        self._cancelled = asyncio.Event()  # pyright: ignore[reportUnannotatedClassAttribute]

        self._register()

    def _register(self) -> None:
        for event in _EVENT_MAP:
            _ = self._bus.on(event, self._make_event_handler(event), priority=1)
        _ = self._bus.on(Event.SHUTDOWN, self._on_shutdown, priority=1)

    def _make_event_handler(self, event: Event):
        state = _EVENT_MAP[event]
        async def handler(data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
            await self._send_state(state)
        return handler

    # ------------------- Session open / close -------------------

    async def _open_session(self) -> bool:
        token = self._backend.app_access_token
        if not token:
            return False
        try:
            resp = await self._client.post(
                f"{self._backend.base_url}/v1/sessions",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as e:
            logger.warning(f"Failed to open session: {e}")
            return False

        if resp.status_code != 201:
            logger.warning(f"POST /v1/sessions returned {resp.status_code}")
            return False

        body: dict[str, Any] = resp.json()  # pyright: ignore[reportExplicitAny, reportAny]
        self._session_id = str(body["session_id"])  # pyright: ignore[reportAny]
        self._start_ping_loop()
        logger.info(f"Session opened: {self._session_id}")
        return True

    async def _end_session(self) -> None:
        if not self._session_id:
            return
        self._stop_ping_loop()
        token = self._backend.app_access_token
        if token:
            try:
                _ = await self._client.post(
                    f"{self._backend.base_url}/v1/sessions/{self._session_id}/end",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError:
                pass
        logger.info(f"Session ended: {self._session_id}")
        self._session_id = None
        self._last_state = None

    # ------------------- State events -------------------

    async def _send_state(self, state: str) -> None:
        if not self._session_id:
            if not await self._open_session():
                self._pending_states.append(state)
                return
            await self._flush_pending()

        token = self._backend.app_access_token
        if not token:
            return

        body = {
            "state": state,
            "timestamp_unix_ms": int(time.time() * 1000),
            "event_id": str(uuid.uuid4()),
        }
        try:
            resp = await self._client.post(
                f"{self._backend.base_url}/v1/sessions/{self._session_id}/event",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
        except httpx.HTTPError as e:
            logger.warning(f"Session event failed: {e}")
            return

        if resp.status_code == 204:
            self._last_state = state
            return

        if resp.status_code in (404, 410):
            logger.info(f"Session {self._session_id} lost ({resp.status_code}), reopening")
            self._session_id = None
            if await self._open_session():
                await self._send_state(state)

    async def _flush_pending(self) -> None:
        pending = self._pending_states
        self._pending_states = []
        for state in pending:
            await self._send_state(state)

    # ------------------- Ping loop -------------------

    def _start_ping_loop(self) -> None:
        self._stop_ping_loop()
        self._cancelled.clear()
        self._ping_task = asyncio.create_task(self._ping_loop())

    def _stop_ping_loop(self) -> None:
        self._cancelled.set()
        if self._ping_task and not self._ping_task.done():
            _ = self._ping_task.cancel()
        self._ping_task = None

    async def _ping_loop(self) -> None:
        while not self._cancelled.is_set():
            try:
                _ = await asyncio.wait_for(self._cancelled.wait(), timeout=_PING_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass

            if not self._session_id:
                return

            token = self._backend.app_access_token
            if not token:
                continue

            try:
                resp = await self._client.post(
                    f"{self._backend.base_url}/v1/sessions/{self._session_id}/ping",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as e:
                logger.warning(f"Session ping failed: {e}")
                continue

            if resp.status_code in (404, 410):
                logger.info(f"Session lost during ping ({resp.status_code}), reopening")
                self._session_id = None
                if await self._open_session() and self._last_state:
                    await self._send_state(self._last_state)

    # ------------------- Shutdown -------------------

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        await self._end_session()
        await self._client.aclose()
