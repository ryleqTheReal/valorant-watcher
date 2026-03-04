"""
Authentication handler for the local Riot/Valorant API.

Reacts to VALORANT process events and builds an authenticated session
that can be used to query match data, stats, etc.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from services.event_bus import EventBus, Event
from utils.models import LockfileData

logger: logging.Logger = logging.getLogger(__name__)

#NOTE: Do not move this into models.py, this is the main instance
@dataclass
class RiotSession:
    """Authenticated session to the local Riot API."""

    lockfile: LockfileData
    client: httpx.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=self.lockfile.base_url,
            headers={"Authorization": self.lockfile.auth_header},
            verify=False,  # Local self-signed cert
            timeout=10.0,
        )

    # async def get_current_summoner(self) -> Dict[str, Any]:
    #     """Query the current player."""
    #     resp = await self.client.get("/chat/v1/session")
    #     resp.raise_for_status()
    #     return resp.json()

    async def close(self) -> None:
        await self.client.aclose()


class AuthHandler:
    """
    Manages the auth lifecycle based on event bus events.

    Registers itself with the bus and reacts to:
    - VALORANT_OPENED   -> build session
    - VALORANT_CLOSED -> tear down session
    - SHUTDOWN            -> cleanup
    """

    def __init__(self, bus: EventBus) -> None:
        self.bus: EventBus = bus
        self.session: RiotSession | None = None
        self._register()

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.VALORANT_OPENED, self._op_valorant_open, priority=10)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._op_valorant_close, priority=10)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers: ------------------- 
    # IMPORTANT: Make sure that each handdler funtion allows for a data parameter even if it's not used IMPORTANT

    async def _op_valorant_open(self, data: LockfileData) -> None:
        """Valorant started -> authenticate"""
        
        logger.info(f"Authenticating against {data.base_url} ...")

        try:
            self.session = RiotSession(lockfile=data)

            # TODO: Maybe add a test to check whether it actually works
            logger.info("Auth successful")
            _ = await self.bus.emit(Event.AUTH_SUCCESS, {"session": self.session})

        except Exception as e:
            logger.error(f"Auth failed: {e}")
            await self._cleanup_session()
            _ = await self.bus.emit(Event.AUTH_FAILED, {"error": str(e)})

    async def _op_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """Valorant closed -> tear down session"""
        logger.info("Valorant closed - cleaning up session")
        await self._cleanup_session()

    async def _on_shutdown(self, data: Any = None) -> None: # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """App is shutting down """
        await self._cleanup_session()

    async def _cleanup_session(self, data: Any = None) -> None: # pyright: ignore[reportAny, reportUnusedParameter, reportExplicitAny]
        """Cleans up the session"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("Session closed")