"""
Lightweight event bus for the Valorant Stats App.

Events are registered as enum members, listeners are arbitrary callables.
Supports sync and async handlers, priorities, and one-shot listeners.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class Event(Enum):
    """All events that can occur in the system."""

    RSO_LOGIN = "RSO_LOGIN"                     # Riot Client logged in (lockfile + RSO 200)
    RSO_LOGOUT = "RSO_LOGOUT"                   # Riot Client logged out (RSO non-200 / lockfile gone)
    VALORANT_OPENED = "VALORANT_OPENED"         # Valorant process detected, lockfile read
    VALORANT_CLOSED = "VALORANT_CLOSED"         # Valorant process terminated
    AUTH_SUCCESS = "AUTH_SUCCESS"               # Riot auth succeeded
    AUTH_FAILED = "AUTH_FAILED"                 # Riot auth failed
    MATCH_STARTED = "MATCH_STARTED"             # New match detected
    MATCH_ENDED = "MATCH_ENDED"                 # Match finished
    DATA_COLLECTED = "DATA_COLLECTED"           # New stats data collected
    DATA_SENT = "DATA_SENT"                     # Data sent to remote server
    DATA_QUEUED = "DATA_QUEUED"                 # Data queued locally (offline)
    WEBSOCKET_EVENT = "WEBSOCKET_EVENT"         # Raw event from Riot Client WebSocket
    GAME_STATE_CHANGED = "GAME_STATE_CHANGED"   # sessionLoopState transition detected
    SHUTDOWN = "SHUTDOWN"                       # App is shutting down
    LOADOUT_UPDATED = "LOADOUT_UPDATED"         # User has updated their own equipped loadout
    OWNED_ITEMS_UPDATED = "OWNED_ITEMS_UPDATED" # User has acquired or lost items
    USER_XP_UPDATED = "USER_XP_UPDATED"         # The user's XP has been updated
    PENALTIES_UPDATED = "PENALTIES_UPDATED"     # Emitted when the user's penalties updated
    MMR_HISTORY_UPDATED = "MMR_HISTORY_UPDATED" # The player's MMR history has updated
    MATCH_DETAIL_FETCHED = "MATCH_DETAIL_FETCHED" # A match's full details have been fetched
    LEADERBOARD_FETCHED = "LEADERBOARD_FETCHED" # Leaderboard data has been retrieved
    PREGAME_MATCH_UPDATED = "PREGAME_MATCH_UPDATED" # Pregame match data fetched during agent select
    INGAME_MATCH_UPDATED = "INGAME_MATCH_UPDATED"   # Ingame match data fetched when match starts
    INGAME_LOADOUTS_FETCHED = "INGAME_LOADOUTS_FETCHED" # Player loadouts fetched for active match
    STORE_OFFERS_UPDATED = "STORE_OFFERS_UPDATED"       # Daily skin offers have rotated
    ACCOUNT_ALIASES_FETCHED = "ACCOUNT_ALIASES_FETCHED" # Player's name/tag alias history fetched

    # Proactive collector pause/resume events
    COLLECTOR_PAUSED_PREGAME = "COLLECTOR_PAUSED_PREGAME"               # Paused for entire pregame duration
    COLLECTOR_PAUSED_MATCH_POINT = "COLLECTOR_PAUSED_MATCH_POINT"       # A team is 1 round from winning
    COLLECTOR_RESUMED_NOT_MATCH_POINT = "COLLECTOR_RESUMED_NOT_MATCH_POINT" # Overtime or no longer match point
    COLLECTOR_PAUSED_USER_ACTIVITY = "COLLECTOR_PAUSED_USER_ACTIVITY"   # User activity detected in menus
    COLLECTOR_RESUMED_IDLE = "COLLECTOR_RESUMED_IDLE"                   # User idle for 60s in menus

IGNORE_EVENTS = [
    "AUTH_SUCCESS",
    "WEBSOCKET_EVENT",
    "LOADOUT_UPDATED",
    "OWNED_ITEMS_UPDATED",
    "USER_XP_UPDATED",
    "PENALTIES_UPDATED",
    "MMR_HISTORY_UPDATED",
    "MATCH_DETAIL_FETCHED",
    "LEADERBOARD_FETCHED",
    "PREGAME_MATCH_UPDATED",
    "INGAME_MATCH_UPDATED",
    "INGAME_LOADOUTS_FETCHED",
    "STORE_OFFERS_UPDATED",
    "ACCOUNT_ALIASES_FETCHED",
    ]

@dataclass
class Listener:
    callback: Callable[..., object]
    priority: int = 0       # Higher = executed first
    once: bool = False       # One-shot listener (removed after first call)
    name: str = ""           # Debug label




class EventBus:
    """
    Central event bus.

    Usage:
        bus = EventBus()
        bus.on(Event.VALORANT_CLOSED, my_handler)
        await bus.emit(Event.VALORANT_CLOSED, lockfile_data)
    """

    def __init__(self) -> None:
        self._listeners: dict[Event, list[Listener]] = {e: [] for e in Event}

    # -- Registration ----------------------------------------------------------

    def on(
        self,
        event: Event,
        callback: Callable[..., object],
        priority: int = 0,
        once: bool = False,
        name: str = "",
    ) -> Callable[..., object]:
        """Register a listener for an event."""
        
        listener = Listener(
            callback=callback,
            priority=priority,
            once=once,
            name=name or getattr(callback, "__name__", "anonymous"),
        )
        self._listeners[event].append(listener)
        self._listeners[event].sort(key=lambda ln: ln.priority, reverse=True)
        logger.debug(f"Registered listener '{listener.name}' for {event.name}")
        return callback

    def once(self, event: Event, callback: Callable[..., object], priority: int = 0) -> Callable[..., object]:
        """Register a one-shot listener (removed after first invocation)."""
        return self.on(event, callback, priority=priority, once=True)

    def off(self, event: Event, callback: Callable[..., object]) -> None:
        """Remove a listener."""
        self._listeners[event] = [
            ln for ln in self._listeners[event] if ln.callback != callback
        ]



    def listen(self, event: Event, priority: int = 0, once: bool = False):
        """
        Decorator for registering listeners:

            @bus.listen(Event.VALORANT_CLOSED)
            async def handle_lockfile(data):
                ...
        """
        def decorator(func: Callable[..., object]) -> Callable[..., object]:
            _ = self.on(event, func, priority=priority, once=once)
            return func
        return decorator

    async def emit(self, event: Event, data: object = None) -> list[object]:
        """
        Fire an event and invokes all registered listeners in priority order.
        Returns a list of return values from each listener.
        """
        
        if event.name not in IGNORE_EVENTS:
            logger.info(f"Event: {event.name}" + (f" | data={data}" if data else ""))

        results: list[object] = []
        to_remove: list[Listener] = []

        for listener in self._listeners[event]:
            try:
                result: object
                if inspect.iscoroutinefunction(listener.callback):
                    result = await listener.callback(data)  # pyright: ignore[reportAny]
                else:
                    result = listener.callback(data)
                results.append(result)
            except Exception as e:
                logger.error(
                    f"Error in listener '{listener.name}' for {event.name}: {e}",
                    exc_info=True,
                )

            if listener.once:
                to_remove.append(listener)

        for listener in to_remove:
            self._listeners[event].remove(listener)

        return results




    def listener_count(self, event: Event) -> int:
        return len(self._listeners[event])