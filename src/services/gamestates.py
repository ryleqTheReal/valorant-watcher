"""game state tracker; monitors sessionLoopState transitions, collects Riot API data per state, emits events downstream"""

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
    IngameLoadoutsEvent,
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

# lookup sets for quick validation
_VALID_STATES: set[str] = {s.value for s in SessionLoopState}
_ITEM_TYPE_IDS: set[str] = {s.value for s in ItemTypes}


@dataclass(frozen=True, slots=True)
class _GameModeRules:
    """game mode pause rules; pause when max(ally, enemy) >= target - threshold
    (threshold ~ 1 min of scoring); always_paused modes never run the collector
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
_USERINFO_POLL_INTERVAL: float = 60.0

class GamestateHandler:
    """tracks sessionLoopState transitions and drives data collection for each state"""

    def __init__(
        self,
        bus: EventBus,
        watermark_path: Path,
        ratelimit_offset: int = 60,
        match_details_interval_ms: int = 1700,
        match_history_interval_ms: int = 2000,
        competitive_updates_interval_ms: int = 2050,
    ) -> None:
        self.bus: EventBus = bus
        self._watermark_path: Path = watermark_path
        self._ratelimit_offset: int = ratelimit_offset
        self._session: RiotSession | None = None
        self._current_state: SessionLoopState | None = None
        self._pending_task: asyncio.Task[None] | None = None
        self._scheduler: RequestScheduler = RequestScheduler(
            match_details_interval_s=match_details_interval_ms / 1000.0,
            match_history_interval_s=match_history_interval_ms / 1000.0,
            competitive_updates_interval_s=competitive_updates_interval_ms / 1000.0,
        )
        self._match_collector: MatchCollector | None = None
        self._valorant_open: bool = False
        self._presence_poll_task: asyncio.Task[None] | None = None
        self._pregame_poll_task: asyncio.Task[None] | None = None
        self.active_match_id: str | None = None
        self._loadout_version: int | None = None
        self._owned_item_count: int | None = None
        self._xp_version: int | None = None
        self._penalties_version: int | None = None
        self._mmr_version: int | None = None
        self._balances_snapshot: dict[str, int] | None = None
        self._store_poll_task: asyncio.Task[None] | None = None
        self._userinfo_poll_task: asyncio.Task[None] | None = None
        self._collector_paused: bool = False
        self._active_queue_id: str | None = None
        self._menu_idle_timer: asyncio.TimerHandle | None = None
        self._last_activity_snapshot: dict[str, object] | None = None
        self._register()

    @property
    def _puuid(self) -> str | None:
        return self._session.puuid if self._session else None

    @property
    def scheduler(self) -> RequestScheduler:
        """shared request scheduler; exposed so other services (e.g. SubmissionService) can enqueue task-priority fetches"""
        return self._scheduler

    def _register(self) -> None:
        _ = self.bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=5)
        _ = self.bus.on(Event.VALORANT_OPENED, self._on_valorant_open, priority=5)
        _ = self.bus.on(Event.WEBSOCKET_EVENT, self._on_websocket_event, priority=5)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=5)
        _ = self.bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=5)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    # ------------------- Event Handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """store session and start match collector; if Valorant is already open, scheduler pauses 60s for other apps"""
        self._session = data["session"]
        self._scheduler.start()
        # TODO: Also start history collector and Competitive Updates collector
        self._match_collector = MatchCollector(
            session=self._session,  # pyright: ignore[reportArgumentType]
            bus=self.bus,
            scheduler=self._scheduler,
            watermark_path=self._watermark_path,
        )
        self._match_collector.start()
        logger.info(f"Gamestate tracker ready for puuid {self._puuid}, match collector started")

        self._store_poll_task = asyncio.create_task(self._poll_store())
        self._userinfo_poll_task = asyncio.create_task(self._poll_userinfo())

        # VALORANT_OPENED may have fired before the session was ready,
        # in which case the presence poll was skipped. Start it now.
        if self._valorant_open and self._current_state is None:
            logger.info("Valorant already open at auth time, starting presence poll now")
            self._presence_poll_task = asyncio.create_task(self._on_valorant_open_sequence())

    async def _on_valorant_open(self, data: LockfileData) -> None:  # pyright: ignore[reportUnusedParameter]
        """Valorant launched -> pause scheduler 60s to yield rate-limit budget to other apps, then poll for initial gamestate"""
        self._valorant_open = True
        if self._session is None or self._current_state is not None:
            return

        self._presence_poll_task = asyncio.create_task(self._on_valorant_open_sequence())

    async def _on_valorant_open_sequence(self) -> None:
        """pause scheduler and start presence polling; local endpoint has no rate limit so polling begins immediately"""
        if self._session is None:
            return

        logger.info("Valorant opened pausing scheduler, polling for initial gamestate")
        self._scheduler.pause()

        await self._poll_initial_presence()

    async def _poll_initial_presence(self) -> None:
        """poll /chat/v4/presences until we find our own sessionLoopState"""
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

                            # set activity baseline so menu monitoring can detect changes from the first websocket event
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
        """check each presence event for a sessionLoopState change"""
        if self._session is None:
            return

        presence: Presence | None = self._find_own_presence(data)
        if presence is None:
            return

        new_state_str: str | None = self._extract_loop_state(presence)
        if new_state_str is None or new_state_str not in _VALID_STATES:
            return

        new_state = SessionLoopState(new_state_str)

        # no state change; still process presence for active monitors
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
            # poll lost the race -> set activity snapshot from this websocket event
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
        """Valorant closed -> clear game-state tracking but keep session and match collector alive"""
        self._valorant_open = False
        await self._clear_game_state()

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Riot Client logged out -> reset tracking state"""
        await self._reset()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """app shutting down -> reset tracking state"""
        await self._reset()

    # ------------------- State Change Dispatch -------------------

    async def _on_state_changed(self, transition: GameStateTransition) -> None:
        """cancel pending tasks, purge stale scheduled requests, then dispatch to the appropriate state handler"""
        self._cancel_pending_task()
        self._cancel_pregame_poll()
        self._cancel_menu_idle_timer()
        self._active_queue_id = None
        # clear state queue and pause scheduler
        self._scheduler.on_state_change()

        if transition.previous == SessionLoopState.PREGAME:
            _ = await self.bus.emit(Event.PREGAME_ENDED)
        elif transition.previous == SessionLoopState.INGAME:
            _ = await self.bus.emit(Event.MATCH_ENDED)

        if transition.current == SessionLoopState.PREGAME:
            _ = await self.bus.emit(Event.PREGAME_STARTED)
        elif transition.current == SessionLoopState.INGAME:
            _ = await self.bus.emit(Event.MATCH_STARTED)

        # first presence received -> fetch account aliases once
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
        """player entered menus -> pause collector and start 60s idle timer; activity resets it, expiry resumes collector"""
        if not self._session:
            return

        self._pause_collector(Event.COLLECTOR_PAUSED_USER_ACTIVITY)
        self._reset_menu_idle_timer()

    async def _on_enter_pregame(self, transition: GameStateTransition) -> None:  # pyright: ignore[reportUnusedParameter]
        """player entered agent select -> pause collector for ~80s, start GLZ pregame polling (not rate-limited)"""
        if not self._session:
            return

        self._pause_collector(Event.COLLECTOR_PAUSED_PREGAME)

        # GLZ not rate-limited, poll independently
        self._pregame_poll_task = asyncio.create_task(self._poll_pregame_match())

    async def _on_enter_ingame(self, transition: GameStateTransition) -> None:
        """match started -> fetch GLZ data (not rate-limited), wait ratelimit offset, resume collector;
        score monitoring via presence pauses/resumes based on game mode rules"""
        if not self._session:
            return

        # determine the game mode for score monitoring
        private = transition.presence.private if transition.presence else None
        if isinstance(private, PresencePrivate):
            self._active_queue_id = private.queueId

        rules = _GAME_MODE_RULES.get(self._active_queue_id or "")

        # always-paused modes (custom, deathmatch): never run the collector
        if rules and rules.always_paused:
            self._pause_collector(Event.COLLECTOR_PAUSED_MATCH_POINT)
            logger.info(f"Collector stays paused for always-paused mode: {self._active_queue_id}")
            # still fetch GLZ data (not rate-limited)
            await self._fetch_ingame_match()
            await self._fetch_ingame_loadouts()
            return

        # GLZ not rate-limited, fetch directly
        await self._fetch_ingame_match()
        await self._fetch_ingame_loadouts()

        await self._wait_ratelimit_offset()

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
        """fetch data, compare key against cache, emit event if changed"""
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
        """enqueue all general-purpose data checks onto the unlimited userinfo queue (no Riot rate-limit)"""
        self._scheduler.enqueue_userinfo(self._check_owned, "owned items")
        self._scheduler.enqueue_userinfo(self._check_loadout, "loadout")
        self._scheduler.enqueue_userinfo(self._check_xp, "xp")
        self._scheduler.enqueue_userinfo(self._check_penalties, "penalties")
        self._scheduler.enqueue_userinfo(self._check_balances, "balances")

    async def _poll_userinfo(self) -> None:
        """re-enqueue userinfo checks every 60s; runs independently of game-state transitions"""
        while self._session is not None:
            self._enqueue_general_checks()
            try:
                await asyncio.sleep(_USERINFO_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise

    # ------------------- Proactive Collector Pause/Resume -------------------

    def _pause_collector(self, event: Event) -> None:
        """pause the scheduler and emit a diagnostic event"""
        if not self._collector_paused:
            self._collector_paused = True
            self._scheduler.pause()
            _ = asyncio.ensure_future(self.bus.emit(event))
            logger.info(f"Collector paused: {event.value}")

    def _resume_collector(self, event: Event) -> None:
        """resume the scheduler and emit a diagnostic event"""
        if self._collector_paused:
            self._collector_paused = False
            self._scheduler.resume()
            _ = asyncio.ensure_future(self.bus.emit(event))
            logger.info(f"Collector resumed: {event.value}")

    def _process_presence_for_state(self, private: PresencePrivate) -> None:
        """route presence updates to the appropriate monitor for the current state"""
        if self._current_state == SessionLoopState.INGAME:
            self._on_ingame_score_update(private)
        elif self._current_state == SessionLoopState.MENUS:
            self._on_menus_presence_update(private)

    def _on_ingame_score_update(self, private: PresencePrivate) -> None:
        """check presence scores against game mode rules for match point detection"""
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
        """true when max(ally, enemy) >= target - threshold -> time to stop collecting"""
        return max(ally, enemy) >= rules.target - rules.threshold

    def _on_menus_presence_update(self, private: PresencePrivate) -> None:
        """pause collector on any activity or MATCHMAKING; leaving queue resets the 60s idle timer"""
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
            # _pause_collector is idempotent; emit explicitly so the event log shows leaving-queue pauses
            if already_paused:
                _ = asyncio.ensure_future(self.bus.emit(Event.COLLECTOR_PAUSED_USER_ACTIVITY))
            self._reset_menu_idle_timer()

        self._last_activity_snapshot = snapshot

    @staticmethod
    def _build_activity_snapshot(private: PresencePrivate) -> dict[str, object]:
        """extract fields that indicate user activity in menus"""
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
        """cancel any existing idle timer and start a new one"""
        self._cancel_menu_idle_timer()
        loop = asyncio.get_running_loop()
        self._menu_idle_timer = loop.call_later(_MENU_IDLE_TIMEOUT, self._on_menu_idle_expired)

    def _cancel_menu_idle_timer(self) -> None:
        """cancel the menu idle timer if running"""
        if self._menu_idle_timer is not None:
            self._menu_idle_timer.cancel()
            self._menu_idle_timer = None

    def _on_menu_idle_expired(self) -> None:
        """60s of menu inactivity -> resume collector (idle timer doubles as rate-limit offset)"""
        logger.info(f"Menu idle timer fired (state={self._current_state}, paused={self._collector_paused})")
        if self._current_state == SessionLoopState.MENUS:
            self._resume_collector(Event.COLLECTOR_RESUMED_IDLE)

    # ------------------- Helpers -------------------

    def _find_own_presence(self, event: PresenceWebsocketEvent) -> Presence | None:
        """find the presence entry matching our puuid"""
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
        """extract sessionLoopState from a presence object"""
        private: PresencePrivate | str | None = presence.private
        if not isinstance(private, PresencePrivate):
            return None

        match_data = private.matchPresenceData
        if not isinstance(match_data, _MatchPresenceData):
            return None

        return match_data.sessionLoopState or None

    def _cancel_pending_task(self) -> None:
        """cancel any in-flight delayed collection task"""
        if self._pending_task and not self._pending_task.done():
            _ = self._pending_task.cancel()
            logger.info("Cancelled pending data collection (state changed)")
        self._pending_task = None

    def _cancel_pregame_poll(self) -> None:
        """cancel the pregame polling task if running"""
        if self._pregame_poll_task and not self._pregame_poll_task.done():
            _ = self._pregame_poll_task.cancel()
        self._pregame_poll_task = None

    async def _clear_game_state(self) -> None:
        """clear game-state without touching session/match collector (used when Valorant closes but Riot Client stays open)"""
        if self._presence_poll_task and not self._presence_poll_task.done():
            _ = self._presence_poll_task.cancel()
            self._presence_poll_task = None
        self._cancel_pregame_poll()
        self._cancel_menu_idle_timer()
        self._last_activity_snapshot = None
        # purge state-bound requests but keep the scheduler alive
        self._scheduler.on_state_change()
        # resume so the match collector's paced requests keep flowing
        self._collector_paused = False
        self._scheduler.resume()
        self._cancel_pending_task()
        if self._current_state == SessionLoopState.PREGAME:
            _ = await self.bus.emit(Event.PREGAME_ENDED)
        elif self._current_state == SessionLoopState.INGAME:
            _ = await self.bus.emit(Event.MATCH_ENDED)
        if self._current_state is not None:
            logger.info("Game state cleared (Valorant closed, match collector still active)")
        self._current_state = None
        self.active_match_id = None
        self._active_queue_id = None
        self._loadout_version = None
        self._owned_item_count = None
        self._xp_version = None
        self._penalties_version = None
        self._mmr_version = None
        self._balances_snapshot = None

    async def _reset(self) -> None:
        """full teardown including session and match collector; used on RSO_LOGOUT/SHUTDOWN"""
        await self._clear_game_state()
        if self._store_poll_task and not self._store_poll_task.done():
            _ = self._store_poll_task.cancel()
            self._store_poll_task = None
        if self._userinfo_poll_task and not self._userinfo_poll_task.done():
            _ = self._userinfo_poll_task.cancel()
            self._userinfo_poll_task = None
        if self._match_collector:
            self._match_collector.stop()
            self._match_collector = None
        self._scheduler.stop()
        if self._session is not None:
            logger.info("Gamestate tracker reset (session ended)")
        self._session = None

# ---------------- Helper API Wrapper Functions ----------------

    async def _poll_pregame_match(self) -> None:
        """poll pregame match at 0.1s intervals (GLZ not rate-limited); emits PREGAME_MATCH_UPDATED on Version change;
        stops on 404 (pregame ended) or cancellation"""
        
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
                    if match_data.PregameState == "character_select_finished":
                        logger.info("Pregame poll ended because everybody locked their character")
                        return

                await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info("Pregame poll cancelled (state changed)")
        except Exception as e:
            logger.warning(f"Pregame poll failed: {type(e).__name__}: {e}")

    async def _fetch_ingame_match(self) -> None:
        """fetch ingame match data once (GLZ not rate-limited); stores match ID and emits INGAME_MATCH_UPDATED"""
        if not self._session:
            return

        try:
            match_id = await self._session.ingame_get_player()
            if not match_id:
                logger.warning("No ingame match ID returned")
                return

            self.active_match_id = match_id
            match_data = await self._session.ingame_get_match(match_id)
            _ = await self.bus.emit(Event.INGAME_MATCH_UPDATED, match_data)
            logger.info(f"Ingame match loaded: {match_id}")
        except Exception as e:
            logger.warning(f"Failed to fetch ingame match: {type(e).__name__}: {e}")

    async def _fetch_ingame_loadouts(self) -> None:
        """fetch player loadouts for the active match (GLZ not rate-limited); retries every 2s until success or match ends"""
        if not self._session or not self.active_match_id:
            return

        match_id = self.active_match_id

        while True:
            try:
                loadouts = await self._session.ingame_get_loadouts(match_id)
                _ = await self.bus.emit(
                    Event.INGAME_LOADOUTS_FETCHED,
                    IngameLoadoutsEvent(match_id=match_id, loadouts=loadouts),
                )
                logger.info(f"Ingame loadouts fetched for match {match_id}")
                return
            except asyncio.CancelledError:
                logger.info("Ingame loadouts fetch cancelled (state changed)")
                raise
            except Exception as e:
                logger.warning(f"Failed to fetch ingame loadouts: {type(e).__name__}: {e}, retrying in 2s")
                await asyncio.sleep(2)

                # check if match is still active (state may have changed during sleep)
                if self.active_match_id != match_id:
                    logger.info("Ingame loadouts fetch aborted: match ended")
                    return

    async def _fetch_account_aliases(self) -> None:
        """fetch the player's name/tag alias history once (local, not rate-limited)"""
        if not self._session:
            return

        try:
            aliases = await self._session.local_get_aliases()
            _ = await self.bus.emit(Event.ACCOUNT_ALIASES_FETCHED, aliases)
            logger.info(f"Account aliases fetched: {len(aliases)} alias(es)")
        except Exception as e:
            logger.warning(f"Failed to fetch account aliases: {type(e).__name__}: {e}")

    async def _check_loadout(self) -> None:
        """emit LOADOUT_UPDATED if loadout Version changed"""
        await self._check_and_emit(
            fetch=self._session.general_get_loadout,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda loadout: loadout.Version,
            cache_attr="_loadout_version",
            event=Event.LOADOUT_UPDATED,
            label="loadout version",
        )

    async def _check_owned(self) -> None:
        """emit OWNED_ITEMS_UPDATED if owned item count changed"""
        await self._check_and_emit(
            fetch=self._session.general_get_owned,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda ownedItems: ownedItems.item_count,
            cache_attr="_owned_item_count",
            event=Event.OWNED_ITEMS_UPDATED,
            label="owned items count",
        )
    
    async def _check_xp(self) -> None:
        """emit USER_XP_UPDATED if xp Version changed"""
        await self._check_and_emit(
            fetch=self._session.general_get_xp,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda levelData: levelData.Version,
            cache_attr="_xp_version",
            event=Event.USER_XP_UPDATED,
            label="user xp version",
        )

    async def _check_penalties(self) -> None:
        """emit PENALTIES_UPDATED if penalties Version changed"""
        await self._check_and_emit(
            fetch=self._session.general_get_penalties,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda penaltiesData: penaltiesData.Version,
            cache_attr="_penalties_version",
            event=Event.PENALTIES_UPDATED,
            label="user penalties version",
        )
    
    async def _check_balances(self) -> None:
        """emit BALANCES_UPDATED if wallet balances changed (no Version field -> compares dict directly)"""
        await self._check_and_emit(
            fetch=self._session.general_get_balances,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda balances: balances.Balances,
            cache_attr="_balances_snapshot",
            event=Event.BALANCES_UPDATED,
            label="user balances",
        )

    async def _check_mmr(self) -> None:
        """emit MMR_HISTORY_UPDATED if mmr Version changed"""
        await self._check_and_emit(
            fetch=self._session.general_get_mmr,  # pyright: ignore[reportOptionalMemberAccess]
            get_key=lambda mmrData: mmrData.Version,
            cache_attr="_mmr_version",
            event=Event.MMR_HISTORY_UPDATED,
            label="user mmr version"
        )
        
    async def _fetch_initial_friends(self) -> None:
        """fetch the full friend list once after first presence is received"""
        if not self._session:
            return
        try:
            friends = await self._session.local_get_friends()
            _ = await self.bus.emit(Event.FRIENDS_LIST_FETCHED, friends)
        except Exception:
            logger.warning("Failed to fetch initial friend list", exc_info=True)

    async def _poll_store(self) -> None:
        """fetch storefront, emit STORE_OFFERS_UPDATED, then sleep until next rotation; repeats until cancelled"""
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

                self._scheduler.enqueue_userinfo(_fetch_store, "store offers")

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