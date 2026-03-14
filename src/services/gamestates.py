"""
Game state tracker and data collector

Monitors presence websocket events for sessionLoopState transitions,
collects relevant data from the Riot API for each transition,
and emits labeled DATA_COLLECTED events for downstream transport.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
    StorefrontResponse,
    _MatchPresenceData,  # pyright: ignore[reportPrivateUsage]
    _PartyPresenceData,  # pyright: ignore[reportPrivateUsage]
)

logger: logging.Logger = logging.getLogger(__name__)

# Lookup set for quick validation
_VALID_STATES: set[str] = {s.value for s in SessionLoopState}
_ITEM_TYPE_IDS: set[str] = {s.value for s in ItemTypes}


@dataclass(frozen=True, slots=True)
class _GameModeRules:
    """Defines when the collector should proactively pause for a game mode.

    Pause when max(ally, enemy) >= target - threshold.
    Threshold is based on ~1 minute of average scoring rate so we
    stop collecting before the match ends and other apps need the budget.

    always_paused modes (custom, deathmatch) never run the collector
    because the user can leave at any time.
    """
    target: int
    threshold: int = 1
    always_paused: bool = False

# TODO: all gamemodes have a fixed time limit
_GAME_MODE_RULES: dict[str, _GameModeRules] = {
    "newmap":       _GameModeRules(target=13, threshold=1),   # standard rules
    "competitive":  _GameModeRules(target=13, threshold=1),
    "unrated":      _GameModeRules(target=13, threshold=1),
    "swiftplay":    _GameModeRules(target=5,  threshold=1),
    "spikerush":    _GameModeRules(target=4,  threshold=1),
    "onefa":        _GameModeRules(target=5,  threshold=1),   # replication
    "valaram":      _GameModeRules(target=5,  threshold=1),   # all random one site
    "skirmish2v2":  _GameModeRules(target=10, threshold=1),
    "ggteam":       _GameModeRules(target=12, threshold=1),   # escalation, ~0.5 lvl/min
    "snowball":     _GameModeRules(target=50, threshold=9),   # ~9 pts/min
    "hurm":         _GameModeRules(target=100, threshold=12), # team deathmatch, ~12 pts/min
    "deathmatch":   _GameModeRules(target=0, always_paused=True),
    "custom":       _GameModeRules(target=0, always_paused=True),
    "":             _GameModeRules(target=0, always_paused=True),
}

_MENU_IDLE_TIMEOUT: float = 60.0

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
        self._store_poll_task: asyncio.Task[None] | None = None
        self._collector_paused: bool = False
        self._active_queue_id: str | None = None
        self._menu_idle_timer: asyncio.TimerHandle | None = None
        self._last_activity_snapshot: dict[str, object] | None = None
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

        self._store_poll_task = asyncio.create_task(self._poll_store())

        # VALORANT_OPENED may have fired before the session was ready,
        # in which case the presence poll was skipped. Start it now.
        if self._valorant_open and self._current_state is None:
            logger.info("Valorant already open at auth time, starting presence poll now")
            self._presence_poll_task = asyncio.create_task(self._on_valorant_open_sequence())

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

                            # Set activity baseline from poll so menus
                            # monitoring can detect changes from the
                            # very first websocket event
                            if new_state == SessionLoopState.MENUS:
                                private = p.private
                                if isinstance(private, PresencePrivate):
                                    self._last_activity_snapshot = self._build_activity_snapshot(private)
                                    logger.info("Activity snapshot baseline set from presence poll")

                            transition = GameStateTransition(
                                previous=None,
                                current=new_state,
                                puuid=self._puuid or "",
                                presence=p,
                            )
                            logger.info(f"Initial gamestate from presence poll: {new_state.value}")
                            await self._on_state_changed(transition)
                            await self._fetch_initial_friends()
                            return

            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as e:
                logger.warning(
                    f"Presence poll failed: {e.response.status_code} {e.response.text[:200]}"
                )
            except Exception as e:
                logger.warning(f"Presence poll failed: {type(e).__name__}: {e}")

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

        # No state change, but still process presence for active monitors
        if new_state == self._current_state:
            private = presence.private
            if isinstance(private, PresencePrivate):
                party = private.partyPresenceData
                party_state = party.partyState if isinstance(party, _PartyPresenceData) else None
                logger.info(f"Own presence update (no state change): partyState={party_state}, queueId={private.queueId}")
                self._process_presence_for_state(private)
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
            # Poll lost the race, set activity snapshot from this websocket
            # event so menus monitoring has a baseline to compare against
            if new_state == SessionLoopState.MENUS:
                private = presence.private
                if isinstance(private, PresencePrivate):
                    self._last_activity_snapshot = self._build_activity_snapshot(private)
                    logger.info("Activity snapshot baseline set from websocket (poll lost race)")
            await self._fetch_initial_friends()
        else:
            logger.info(f"State changed: {previous.value} -> {new_state.value}")

        await self._on_state_changed(transition)

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Valorant closed -> clear game-state tracking but keep match collector alive.

        The session and match collector survive because the user is still
        logged into the Riot Client. Only RSO_LOGOUT/SHUTDOWN kills those.
        """
        self._valorant_open = False
        self._clear_game_state()

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
        self._cancel_menu_idle_timer()
        self._active_queue_id = None
        # This makes the state queue be cleared and go to sleep
        self._scheduler.on_state_change()

        # First presence received -> fetch account aliases once
        if transition.previous is None:
            _ = asyncio.create_task(self._fetch_account_aliases())

        match transition.current:
            case SessionLoopState.MENUS:
                self._pending_task = asyncio.create_task(self._on_enter_menus(transition))
            case SessionLoopState.PREGAME:
                self._pending_task = asyncio.create_task(self._on_enter_pregame(transition))
            case SessionLoopState.INGAME:
                self._pending_task = asyncio.create_task(self._on_enter_ingame(transition))

    async def _on_enter_menus(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player entered menus.

        Pauses the collector and starts the 60s idle timer.
        The timer IS the rate-limit offset, when it fires, the
        collector resumes in aggressive mode and general checks
        are enqueued. Any user activity resets the timer.
        """
        if not self._session:
            return

        self._pause_collector(Event.COLLECTOR_PAUSED_USER_ACTIVITY)
        self._reset_menu_idle_timer()

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """Player entered agent select.

        GLZ endpoints are not rate-limited, so we spawn a 1s
        polling coroutine that runs independently of the scheduler.

        The collector stays paused for the entire pregame (~80s).
        Pregame is a highly competed window and not worth risking
        rate limits for other apps.
        """
        if not self._session:
            return

        self._pause_collector(Event.COLLECTOR_PAUSED_PREGAME)

        # GLZ endpoints are not rate-limited, poll independently
        self._pregame_poll_task = asyncio.create_task(self._poll_pregame_match())

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:
        """Match started (loading screen / gameplay).

        GLZ ingame endpoints are not rate-limited, so we fetch the match
        data once directly and store the match ID for the duration of the
        match. Score monitoring from presence data will pause/resume
        the collector as match point approaches.

        The queue ID is extracted from the transition presence to look up
        the game mode rules for match point detection.
        """
        if not self._session:
            return

        # Determine the game mode for score monitoring
        private = transition.presence.private if transition.presence else None
        if isinstance(private, PresencePrivate):
            self._active_queue_id = private.queueId

        rules = _GAME_MODE_RULES.get(self._active_queue_id or "")

        # Always-paused modes (custom, deathmatch): never run the collector
        if rules and rules.always_paused:
            self._pause_collector(Event.COLLECTOR_PAUSED_MATCH_POINT)
            logger.info(f"Collector stays paused for always-paused mode: {self._active_queue_id}")
            # Still fetch GLZ data (not rate-limited)
            await self._fetch_ingame_match()
            await self._fetch_ingame_loadouts()
            return

        # GLZ endpoints are not rate-limited, fetch directly
        await self._fetch_ingame_match()
        await self._fetch_ingame_loadouts()

        await self._wait_ratelimit_offset()

        self._enqueue_general_checks()
        self._resume_collector(Event.COLLECTOR_RESUMED_NOT_MATCH_POINT)

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

    # ------------------- Proactive Collector Pause/Resume -------------------

    def _pause_collector(self, event: Event) -> None:
        """Pause the scheduler and emit a diagnostic event."""
        if not self._collector_paused:
            self._collector_paused = True
            self._scheduler.pause()
            _ = asyncio.ensure_future(self.bus.emit(event))
            logger.info(f"Collector paused: {event.value}")

    def _resume_collector(self, event: Event) -> None:
        """Resume the scheduler and emit a diagnostic event."""
        if self._collector_paused:
            self._collector_paused = False
            self._scheduler.resume()
            _ = asyncio.ensure_future(self.bus.emit(event))
            logger.info(f"Collector resumed: {event.value}")

    def _process_presence_for_state(self, private: PresencePrivate) -> None:
        """Route presence updates to the appropriate monitor for the current state."""
        if self._current_state == SessionLoopState.INGAME:
            self._on_ingame_score_update(private)
        elif self._current_state == SessionLoopState.MENUS:
            self._on_menus_presence_update(private)

    def _on_ingame_score_update(self, private: PresencePrivate) -> None:
        """Check presence scores against game mode rules for match point detection."""
        ally = private.partyOwnerMatchScoreAllyTeam
        enemy = private.partyOwnerMatchScoreEnemyTeam
        if ally is None or enemy is None:
            return

        rules = _GAME_MODE_RULES.get(self._active_queue_id or "")
        if rules is None or rules.always_paused:
            return

        if self._is_near_end(ally, enemy, rules):
            self._pause_collector(Event.COLLECTOR_PAUSED_MATCH_POINT)
        else:
            self._resume_collector(Event.COLLECTOR_RESUMED_NOT_MATCH_POINT)

    @staticmethod
    def _is_near_end(ally: int, enemy: int, rules: _GameModeRules) -> bool:
        """True when either team is close enough to winning that we should stop collecting."""
        return max(ally, enemy) >= rules.target - rules.threshold

    def _on_menus_presence_update(self, private: PresencePrivate) -> None:
        """Manage collector pause/resume in menus based on user activity.

        Three rules:
        1. User activity always resets the 60s idle timer.
        2. While in queue (MATCHMAKING), collector stays paused.
        3. Leaving queue counts as activity (rule 1).
        """
        party = private.partyPresenceData
        in_queue = isinstance(party, _PartyPresenceData) and party.partyState == "MATCHMAKING"

        if in_queue:
            self._cancel_menu_idle_timer()
            self._pause_collector(Event.COLLECTOR_PAUSED_USER_ACTIVITY)
            self._last_activity_snapshot = None
            return

        snapshot = self._build_activity_snapshot(private)

        if self._last_activity_snapshot is None or snapshot != self._last_activity_snapshot:
            already_paused = self._collector_paused
            logger.info("Menu activity detected, resetting idle timer")
            self._pause_collector(Event.COLLECTOR_PAUSED_USER_ACTIVITY)
            # _pause_collector is idempotent, if already paused (e.g.
            # leaving queue), emit explicitly so the event log shows it
            if already_paused:
                _ = asyncio.ensure_future(self.bus.emit(Event.COLLECTOR_PAUSED_USER_ACTIVITY))
            self._reset_menu_idle_timer()

        self._last_activity_snapshot = snapshot

    @staticmethod
    def _build_activity_snapshot(private: PresencePrivate) -> dict[str, object]:
        """Extract fields that indicate user activity in menus."""
        party = private.partyPresenceData
        return {
            "queueId": private.queueId,
            "partySize": private.partySize,
            "partyState": party.partyState if isinstance(party, _PartyPresenceData) else None,
            "partyId": private.partyId,
            "queueEntryTime": party.queueEntryTime if isinstance(party, _PartyPresenceData) else None,
            "isIdle": private.isIdle,
        }

    def _reset_menu_idle_timer(self) -> None:
        """Cancel any existing idle timer and start a new one."""
        self._cancel_menu_idle_timer()
        loop = asyncio.get_running_loop()
        self._menu_idle_timer = loop.call_later(_MENU_IDLE_TIMEOUT, self._on_menu_idle_expired)

    def _cancel_menu_idle_timer(self) -> None:
        """Cancel the menu idle timer if running."""
        if self._menu_idle_timer is not None:
            self._menu_idle_timer.cancel()
            self._menu_idle_timer = None

    def _on_menu_idle_expired(self) -> None:
        """Called after 60s of no user activity in menus.

        Resume the collector in aggressive mode and enqueue general
        checks. The idle timer serves as the rate-limit offset.
        """
        logger.info(f"Menu idle timer fired (state={self._current_state}, paused={self._collector_paused})")
        if self._current_state == SessionLoopState.MENUS:
            self._scheduler.set_rate_mode("aggressive")
            self._resume_collector(Event.COLLECTOR_RESUMED_IDLE)
            self._enqueue_general_checks()

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

    def _clear_game_state(self) -> None:
        """Clear game-state tracking without touching the session or match collector.

        Used when Valorant closes but the Riot Client is still logged in.
        The match collector keeps running so it can continue fetching
        match history while the user is AFK in the Riot Client.
        """
        if self._presence_poll_task and not self._presence_poll_task.done():
            _ = self._presence_poll_task.cancel()
            self._presence_poll_task = None
        self._cancel_pregame_poll()
        self._cancel_menu_idle_timer()
        self._last_activity_snapshot = None
        # Purge state-bound requests but keep the scheduler alive
        self._scheduler.on_state_change()
        # Resume immediately so the match collector's general-queue requests keep flowing
        self._scheduler.set_rate_mode("aggressive")
        self._collector_paused = False
        self._scheduler.resume()
        self._cancel_pending_task()
        if self._current_state is not None:
            logger.info("Game state cleared (Valorant closed, match collector still active)")
        self._current_state = None
        self._active_match_id = None
        self._active_queue_id = None
        self._loadout_version = None
        self._owned_item_count = None
        self._xp_version = None
        self._penalties_version = None
        self._mmr_version = None

    def _reset(self) -> None:
        """Full teardown: clear everything including session and match collector.

        Used on RSO_LOGOUT and SHUTDOWN when the session is no longer valid.
        """
        self._clear_game_state()
        if self._store_poll_task and not self._store_poll_task.done():
            _ = self._store_poll_task.cancel()
            self._store_poll_task = None
        if self._match_collector:
            self._match_collector.stop()
            self._match_collector = None
        self._scheduler.stop()
        if self._session is not None:
            logger.info("Gamestate tracker reset (session ended)")
        self._session = None

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

    async def _fetch_account_aliases(self) -> None:
        """Fetch the player's name/tag alias history once (local, not rate-limited)."""
        if not self._session:
            return

        try:
            aliases = await self._session.local_get_aliases()
            _ = await self.bus.emit(Event.ACCOUNT_ALIASES_FETCHED, aliases)
            logger.info(f"Account aliases fetched: {len(aliases)} alias(es)")
        except Exception as e:
            logger.warning(f"Failed to fetch account aliases: {type(e).__name__}: {e}")

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
        
    async def _fetch_initial_friends(self) -> None:
        """Fetch the full friend list once after the first presence is received."""
        if not self._session:
            return
        try:
            friends = await self._session.local_get_friends()
            _ = await self.bus.emit(Event.FRIENDS_LIST_FETCHED, friends)
        except Exception:
            logger.warning("Failed to fetch initial friend list", exc_info=True)

    async def _poll_store(self) -> None:
        """Fetch the storefront, emit, then sleep until the offers rotate. Repeats until cancelled.

        The actual API call is enqueued through the scheduler so it
        counts against the shared rate-limit budget.
        """
        if not self._session:
            return

        try:
            while True:
                future: asyncio.Future[StorefrontResponse] = asyncio.get_running_loop().create_future()

                async def _fetch_store(fut: asyncio.Future[StorefrontResponse] = future) -> None:
                    try:
                        result = await self._session.general_get_store()  # pyright: ignore[reportOptionalMemberAccess]
                        fut.set_result(result)
                    except Exception as e:
                        fut.set_exception(e)

                self._scheduler.enqueue_general(_fetch_store, "store offers")

                try:
                    store = await future
                except Exception as e:
                    logger.warning(f"Store fetch failed: {type(e).__name__}: {e}, retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                _ = await self.bus.emit(Event.STORE_OFFERS_UPDATED, store)
                remaining = store.single_item_offers_remaining_seconds
                if remaining is None or remaining <= 0:
                    logger.warning("Store rotation timer missing or expired, retrying in 60s")
                    await asyncio.sleep(60)
                    continue

                logger.info(f"Store offers fetched, next rotation in {remaining}s")
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            logger.info("Store poll cancelled")