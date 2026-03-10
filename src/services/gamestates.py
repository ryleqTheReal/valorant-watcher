"""
Game state tracker and data collector.

Monitors presence websocket events for sessionLoopState transitions,
collects relevant data from the Riot API for each transition,
and emits labeled DATA_COLLECTED events for downstream transport.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable
from collections.abc import Awaitable

from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from utils.models import (
    GameStateTransition,
    ItemTypes,
    Presence,
    PresencePrivate,
    PresenceWebsocketEvent,
    SessionLoopState,
    _MatchPresenceData,  # pyright: ignore[reportPrivateUsage]
)

logger: logging.Logger = logging.getLogger(__name__)

# Lookup set for quick validation
_VALID_STATES: set[str] = {s.value for s in SessionLoopState}
_ITEM_TYPE_IDS: set[str] = {s.value for s in ItemTypes}

class GamestateHandler:
    """
    Tracks sessionLoopState transitions and collects data for each.

    Listens for:
    - AUTH_SUCCESS      -> stores session (puuid + fetch capability)
    - WEBSOCKET_EVENT   -> detects state changes, triggers collection
    - VALORANT_CLOSED   -> resets state
    - SHUTDOWN          -> resets state
    """

    def __init__(self, bus: EventBus, ratelimit_offset: int = 60) -> None:
        self.bus: EventBus = bus
        self._ratelimit_offset: int = ratelimit_offset
        self._session: RiotSession | None = None
        self._current_state: SessionLoopState | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._loadout_version: int | None = None
        self._owned_item_count: int | None = None
        self._xp_version: int | None = None
        self._penalties_version: int | None = None
        self._mmr_version: int | None = None
        self._register()

    @property
    def _puuid(self) -> str | None:
        return self._session.puuid if self._session else None

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=5)
        _ = self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=5)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """Store the authenticated session for API access."""
        self._session = data["session"]
        logger.info(f"Gamestate tracker ready for puuid {self._puuid}")

    async def _on_websocket_event(self, data: PresenceWebsocketEvent) -> None:
        """Check each presence event for a sessionLoopState change."""
        if self._session is None:
            return

        presence: Presence | None = self._find_own_presence(data)
        if presence is None:
            return

        new_state_str: str | None = self._extract_loop_state(presence)
        if new_state_str is None or new_state_str not in _VALID_STATES:
            return

        new_state = SessionLoopState(new_state_str)

        # No change
        if new_state == self._current_state:
            return

        previous = self._current_state
        self._current_state = new_state

        transition = GameStateTransition(
            previous=previous,
            current=new_state,
            puuid=self._puuid or "",
            presence=presence,
        )

        if previous is None:
            logger.info(f"Baseline state set: {new_state.value}")
        else:
            logger.info(f"State changed: {previous.value} -> {new_state.value}")

        await self._on_state_changed(transition)

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Valorant closed -> reset tracking state."""
        self._reset()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """App shutting down -> reset tracking state."""
        self._reset()

    # ------------------- State Change Dispatch -------------------

    async def _on_state_changed(self, transition: GameStateTransition) -> None:
        """Dispatch to the appropriate handler based on the new state."""
        self._cancel_pending_task()

        match transition.current:
            case SessionLoopState.MENUS:
                self._pending_task = asyncio.create_task(self._on_enter_menus(transition))
            case SessionLoopState.PREGAME:
                await self._on_enter_pregame(transition)
            case SessionLoopState.INGAME:
                await self._on_enter_ingame(transition)

    async def _on_enter_menus(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player returned to menus (e.g. match just ended).

        Waits for the rate limit offset to let other apps finish their
        burst of requests, then collects post-match data.
        """
        if not self._session:
            return

        logger.info(f"Waiting {self._ratelimit_offset}s for rate limit cooldown before collecting data")
        await asyncio.sleep(self._ratelimit_offset)
        logger.info("Rate limit cooldown finished, collecting data")

        await self._check_owned()
        await self._check_loadout()
        await self._check_xp()
        await self._check_penalties()
        await self._check_mmr()

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player entered agent select.

        Collect pre-match data here:
        - Pregame match info (map, mode, players)
        - Player loadouts / skins
        """
        assert self._session is not None
        # TODO: Implement
        pass

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Match started (loading screen / gameplay).

        Collect live match data here:
        - Current game match info
        - Player loadouts for the round
        - Hardware / performance stats
        """
        assert self._session is not None
        # TODO: Implement
        pass

    # ------------------- Boilerplate -------------------

    async def _check_and_emit[T](
        self,
        fetch: Callable[[], Awaitable[T]],
        get_key: Callable[[T], Any],  # pyright: ignore[reportExplicitAny]
        cache_attr: str,
        event: Event,
        label: str,
    ) -> None:
        """Generic check-diff-emit pattern.

        Fetches data, compares a key value against the cached one,
        and emits an event if it changed.
        """
        if not self._session:
            return

        try:
            data: T = await fetch()
        except Exception as e:
            logger.warning(f"Failed to fetch {label}: {e}")
            return

        new_key: Any = get_key(data)  # pyright: ignore[reportExplicitAny, reportAny]
        old_key: Any = getattr(self, cache_attr)  # pyright: ignore[reportExplicitAny, reportAny]

        if new_key == old_key:
            return

        setattr(self, cache_attr, new_key)

        if old_key is None:
            logger.info(f"Baseline {label} set: {new_key}")
        else:
            logger.info(f"{label} changed: {old_key} -> {new_key}")

        _ = await self.bus.emit(event, data)

    # ------------------- Helpers -------------------

    def _find_own_presence(self, event: PresenceWebsocketEvent) -> Presence | None:
        """Find the presence entry matching our puuid."""
        presences = event.data.data.presences
        if not presences:
            return None

        for presence in presences:
            if not isinstance(presence, Presence):
                continue
            if presence.puuid == self._puuid and presence.product == "valorant":
                return presence

        return None

    @staticmethod
    def _extract_loop_state(presence: Presence) -> str | None:
        """Extract sessionLoopState from a presence object."""
        private: PresencePrivate | str | None = presence.private
        if not isinstance(private, PresencePrivate):
            return None

        match_data = private.matchPresenceData
        if not isinstance(match_data, _MatchPresenceData):
            return None

        return match_data.sessionLoopState or None

    def _cancel_pending_task(self) -> None:
        """Cancel any in-flight delayed collection task."""
        if self._pending_task and not self._pending_task.done():
            self._pending_task.cancel()
            logger.info("Cancelled pending data collection (state changed)")
        self._pending_task = None

    def _reset(self) -> None:
        """Clear all tracking state."""
        self._cancel_pending_task()
        if self._current_state is not None:
            logger.info("Gamestate tracker reset")
        self._session = None
        self._current_state = None
        self._loadout_version = None
        self._owned_item_count = None

# ---------------- Helper API Wrapper Functions ----------------

    async def _check_loadout(self) -> None:
        """Checks whether the user has updated their loadout."""
        await self._check_and_emit(
            fetch=self._session.menus_get_loadout,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda loadout: loadout.Version,
            cache_attr="_loadout_version",
            event=Event.LOADOUT_UPDATED,
            label="loadout version",
        )

    async def _check_owned(self) -> None:
        """Checks whether the user has acquired new items."""
        await self._check_and_emit(
            fetch=self._session.menus_get_owned,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda ownedItems: ownedItems.item_count,
            cache_attr="_owned_item_count",
            event=Event.OWNED_ITEMS_UPDATED,
            label="owned items count",
        )
    
    async def _check_xp(self) -> None:
        """Checks whether the user has gained XP"""
        await self._check_and_emit(
            fetch=self._session.menus_get_xp,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda levelData: levelData.Version,
            cache_attr="_xp_version",
            event=Event.USER_XP_UPDATED,
            label="user xp version",
        )

    async def _check_penalties(self) -> None:
        """Checks whether the user has new penalties"""
        await self._check_and_emit(
            fetch=self._session.menus_get_penalties,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda penaltiesData: penaltiesData.Version,
            cache_attr="_penalties_version",
            event=Event.PENALTIES_UPDATED,
            label="user penalties version",
        )
    
    async def _check_mmr(self) -> None:
        """Checks whether the user's MMR history has updated"""
        await self._check_and_emit(
            fetch=self._session.menus_get_mmr,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda mmrData: mmrData.Version,
            cache_attr="_mmr_version",
            event=Event.MMR_HISTORY_UPDATED,
            label="user mmr version"
        )