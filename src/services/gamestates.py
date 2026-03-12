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

from pathlib import Path

import httpx

from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from services.match_collector import MatchCollector
from services.request_scheduler import RequestScheduler
from utils.models import (
    GameStateTransition,
    ItemTypes,
    LockfileData,
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

    def __init__(
        self,
        bus: EventBus,
        watermark_path: Path,
        ratelimit_offset: int = 60,
        initial_limit: int = 6,
        sustained_limit: int = 20,
        aggressive_limit: int = 24,
    ) -> None:
        self.bus: EventBus = bus
        self._watermark_path: Path = watermark_path
        self._ratelimit_offset: int = ratelimit_offset
        self._session: RiotSession | None = None
        self._current_state: SessionLoopState | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._scheduler: RequestScheduler = RequestScheduler(
            initial_limit=initial_limit,
            sustained_limit=sustained_limit,
            aggressive_limit=aggressive_limit,
        )
        self._match_collector: MatchCollector | None = None
        self._valorant_open: bool = False
        self._presence_poll_task: asyncio.Task[None] | None = None
        self._pregame_poll_task: asyncio.Task[None] | None = None
        self._active_match_id: str | None = None
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
        _ = self.bus.on(Event.VALORANT_OPENED, self._on_valorant_open, priority=5)
        _ = self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=5)
        _ = self.bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=5)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """Store the authenticated session,start match collection immediately

        The match collector begins as soon as we're authenticated: no need
        to wait for VALORANT to launch. 
        If VALORANT opens later, the
        scheduler is paused for 60s to let other apps do their thing
        """
        self._session = data["session"]
        self._scheduler.start(aggressive=not self._valorant_open)
        self._match_collector = MatchCollector(
            session=self._session,  # pyright: ignore[reportArgumentType]
            bus=self.bus,
            scheduler=self._scheduler,
            watermark_path=self._watermark_path,
        )
        self._match_collector.start()
        logger.info(f"Gamestate tracker ready for puuid {self._puuid}, match collector started")

    async def _on_valorant_open(self, data: LockfileData) -> None:  # pyright: ignore[reportUnusedParameter]
        """Valorant launched -> pause scheduler for 60s, then poll for initial gamestate.

        Pauses the match collector to let other apps (tracker.gg, etc.)
        make their initial burst of requests without competing for rate
        limit budget.  After the cooldown, polls /chat/v4/presences to
        get the baseline sessionLoopState.
        """
        self._valorant_open = True
        if self._session is None or self._current_state is not None:
            return

        self._presence_poll_task = asyncio.create_task(self._on_valorant_open_sequence())

    async def _on_valorant_open_sequence(self) -> None:
        """Pause scheduler and poll for initial gamestate immediately

        The presence endpoint is local (no rate limit), so we start
        polling right away while the scheduler is paused.  Once the
        initial state is found, _on_state_changed triggers the normal
        state handler which waits its own offset and then resumes.
        """
        if self._session is None:
            return

        logger.info("Valorant opened pausing scheduler, polling for initial gamestate")
        self._scheduler.pause()

        await self._poll_initial_presence()

    async def _poll_initial_presence(self) -> None:
        """Poll /chat/v4/presences until we find our own sessionLoopState."""
        if self._session is None:
            return

        logger.info("Polling /chat/v4/presences for initial gamestate...")

        while self._current_state is None:
            try:
                presence_data = await self._session.local_get_presences()

                if presence_data.presences:
                    for p in presence_data.presences:
                        if not isinstance(p, Presence):
                            continue
                        if p.puuid != self._puuid or p.product != "valorant":
                            continue

                        state_str = self._extract_loop_state(p)
                        if state_str and state_str in _VALID_STATES:
                            new_state = SessionLoopState(state_str)
                            self._current_state = new_state

                            transition = GameStateTransition(
                                previous=None,
                                current=new_state,
                                puuid=self._puuid or "",
                                presence=p,
                            )
                            logger.info(f"Initial gamestate from presence poll: {new_state.value}")
                            await self._on_state_changed(transition)
                            return

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"Presence poll attempt failed: {e}")

            await asyncio.sleep(5)

        if self._current_state is None:  # pyright: ignore[reportUnnecessaryComparison]
            logger.debug("Presence polling stopped (session closed or state already set)")

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
        self._valorant_open = False
        self._reset()

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Riot Client logged out -> reset tracking state."""
        self._reset()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """App shutting down -> reset tracking state."""
        self._reset()

    # ------------------- State Change Dispatch -------------------

    async def _on_state_changed(self, transition: GameStateTransition) -> None:
        """Dispatch to the appropriate handler based on the new state.

        Cancels the pending offset-wait task and tells the scheduler to
        purge stale state-bound requests before dispatching the new handler.
        """
        self._cancel_pending_task()
        self._cancel_pregame_poll()
        # This makes the state queue be cleared and go to sleep
        self._scheduler.on_state_change()

        match transition.current:
            case SessionLoopState.MENUS:
                self._pending_task = asyncio.create_task(self._on_enter_menus(transition))
            case SessionLoopState.PREGAME:
                self._pending_task = asyncio.create_task(self._on_enter_pregame(transition))
            case SessionLoopState.INGAME:
                self._pending_task = asyncio.create_task(self._on_enter_ingame(transition))

    async def _on_enter_menus(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player returned to menus (e.g. match just ended).

        Waits for the rate limit offset to let other apps finish their
        burst of requests, then enqueues general data checks.
        """
        if not self._session:
            return

        await self._wait_ratelimit_offset()

        # No state-specific requests for MENUS currently
        self._enqueue_general_checks()
        # This is mandatory to resume the state queue
        self._scheduler.resume()

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player entered agent select.

        GLZ endpoints are not rate-limited, so we spawn a 1s
        polling coroutine that runs independently of the scheduler.
        It dies on 404 (pregame ended server-side) or state change
        (cancelled by _on_state_changed)

        The general-check pipeline still runs after the offset wait.
        """
        if not self._session:
            return

        # GLZ endpoints are not rate-limited, poll independently
        self._pregame_poll_task = asyncio.create_task(self._poll_pregame_match())

        await self._wait_ratelimit_offset()

        self._enqueue_general_checks()
        # This is mandatory to resume the state queue
        self._scheduler.resume()

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Match started (loading screen / gameplay).

        GLZ ingame endpoints are not rate-limited, so we fetch the match
        data once directly and store the match ID for the duration of the
        match. The general-check pipeline runs after the offset wait.
        """
        if not self._session:
            return

        # GLZ endpoints are not rate-limited, fetch directly
        await self._fetch_ingame_match()
        await self._fetch_ingame_loadouts()

        await self._wait_ratelimit_offset()

        self._enqueue_general_checks()
        # This is mandatory to resume the state queue
        self._scheduler.resume()

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
    
    async def _wait_ratelimit_offset(self) -> None:
        logger.info(f"Waiting {self._ratelimit_offset}s for rate limit cooldown before collecting data")
        await asyncio.sleep(self._ratelimit_offset)
        logger.info("Rate limit cooldown finished, collecting data")

    def _enqueue_general_checks(self) -> None:
        """Enqueue all general-purpose data checks (low priority).

        Also starts the match collector if it hasn't been started yet.
        Match collection requests are enqueued after the general checks,
        so player data is always fetched first.
        """
        self._scheduler.enqueue_general(self._check_owned, "owned items")
        self._scheduler.enqueue_general(self._check_loadout, "loadout")
        self._scheduler.enqueue_general(self._check_xp, "xp")
        self._scheduler.enqueue_general(self._check_penalties, "penalties")
        self._scheduler.enqueue_general(self._check_mmr, "mmr")

        # Match collector is already running (started on AUTH_SUCCESS).
        # Its self-scheduled requests interleave with these general checks.

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
            _ = self._pending_task.cancel()
            logger.info("Cancelled pending data collection (state changed)")
        self._pending_task = None

    def _cancel_pregame_poll(self) -> None:
        """Cancel the pregame polling coroutine if running"""
        if self._pregame_poll_task and not self._pregame_poll_task.done():
            _ = self._pregame_poll_task.cancel()
        self._pregame_poll_task = None

    def _reset(self) -> None:
        """Clear all tracking state."""
        if self._match_collector:
            self._match_collector.stop()
            self._match_collector = None
        if self._presence_poll_task and not self._presence_poll_task.done():
            _ = self._presence_poll_task.cancel()
            self._presence_poll_task = None
        self._cancel_pregame_poll()
        self._scheduler.stop()
        self._cancel_pending_task()
        if self._current_state is not None:
            logger.info("Gamestate tracker reset")
        self._session = None
        self._current_state = None
        self._active_match_id = None
        self._loadout_version = None
        self._owned_item_count = None

# ---------------- Helper API Wrapper Functions ----------------

    async def _poll_pregame_match(self) -> None:
        """Poll pregame match data at 1s intervals (GLZ is not rate-limited)

        Gets the match ID once, then polls the match endpoint every second
        
        Emits PREGAME_MATCH_UPDATED whenever the 'Version' field changes
        
        Stops on 404 (pregame ended server-side) or cancellation (state change)
        """
        
        if not self._session:
            return

        try:
            match_id = await self._session.pregame_get_player()
            if not match_id:
                logger.warning("No pregame match ID returned")
                return

            logger.info(f"Pregame poll started for match {match_id}")
            pregame_version: int | None = None

            while True:
                try:
                    match_data = await self._session.pregame_get_match(match_id)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.info("Pregame poll ended: 404 (match no longer in pregame)")
                        return
                    raise

                if match_data.Version != pregame_version:
                    pregame_version = match_data.Version
                    _ = await self.bus.emit(Event.PREGAME_MATCH_UPDATED, match_data)
                    logger.debug(f"Pregame match updated: version {pregame_version}")

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Pregame poll cancelled (state changed)")
        except Exception as e:
            logger.warning(f"Pregame poll failed: {type(e).__name__}: {e}")

    async def _fetch_ingame_match(self) -> None:
        """Fetch ingame match data once (GLZ is not rate-limited).

        Gets the match ID, stores it on self._active_match_id, fetches
        the full match data, and emits INGAME_MATCH_UPDATED.
        """
        if not self._session:
            return

        try:
            match_id = await self._session.ingame_get_player()
            if not match_id:
                logger.warning("No ingame match ID returned")
                return

            self._active_match_id = match_id
            match_data = await self._session.ingame_get_match(match_id)
            _ = await self.bus.emit(Event.INGAME_MATCH_UPDATED, match_data)
            logger.info(f"Ingame match loaded: {match_id}")
        except Exception as e:
            logger.warning(f"Failed to fetch ingame match: {type(e).__name__}: {e}")

    async def _fetch_ingame_loadouts(self) -> None:
        """Fetch player loadouts for the active match (GLZ is not rate-limited).

        Retries with a 2s delay until successful, match ends, or cancelled.
        This is the most valuable data, we never give up while the match is active.
        """
        if not self._session or not self._active_match_id:
            return

        match_id = self._active_match_id

        while True:
            try:
                loadouts = await self._session.ingame_get_loadouts(match_id)
                _ = await self.bus.emit(Event.INGAME_LOADOUTS_FETCHED, loadouts)
                logger.info(f"Ingame loadouts fetched for match {match_id}")
                return
            except asyncio.CancelledError:
                logger.info("Ingame loadouts fetch cancelled (state changed)")
                raise
            except Exception as e:
                logger.warning(f"Failed to fetch ingame loadouts: {type(e).__name__}: {e}, retrying in 2s")
                await asyncio.sleep(2)

                # Check if match is still active (state may have changed during sleep)
                if self._active_match_id != match_id:
                    logger.info("Ingame loadouts fetch aborted: match ended")
                    return

    async def _check_loadout(self) -> None:
        """Checks whether the user has updated their loadout."""
        await self._check_and_emit(
            fetch=self._session.general_get_loadout,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda loadout: loadout.Version,
            cache_attr="_loadout_version",
            event=Event.LOADOUT_UPDATED,
            label="loadout version",
        )

    async def _check_owned(self) -> None:
        """Checks whether the user has acquired new items."""
        await self._check_and_emit(
            fetch=self._session.general_get_owned,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda ownedItems: ownedItems.item_count,
            cache_attr="_owned_item_count",
            event=Event.OWNED_ITEMS_UPDATED,
            label="owned items count",
        )
    
    async def _check_xp(self) -> None:
        """Checks whether the user has gained XP"""
        await self._check_and_emit(
            fetch=self._session.general_get_xp,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda levelData: levelData.Version,
            cache_attr="_xp_version",
            event=Event.USER_XP_UPDATED,
            label="user xp version",
        )

    async def _check_penalties(self) -> None:
        """Checks whether the user has new penalties"""
        await self._check_and_emit(
            fetch=self._session.general_get_penalties,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda penaltiesData: penaltiesData.Version,
            cache_attr="_penalties_version",
            event=Event.PENALTIES_UPDATED,
            label="user penalties version",
        )
    
    async def _check_mmr(self) -> None:
        """Checks whether the user's MMR history has updated"""
        await self._check_and_emit(
            fetch=self._session.general_get_mmr,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda mmrData: mmrData.Version,
            cache_attr="_mmr_version",
            event=Event.MMR_HISTORY_UPDATED,
            label="user mmr version"
        )