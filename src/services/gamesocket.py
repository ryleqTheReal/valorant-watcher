"""
Local Riot Client WebSocket listener.

Connects to the Riot Client's local WebSocket API using lockfile
credentials and forwards game events to the application event bus.
Runs in a dedicated daemon thread with its own asyncio event loop.
"""

import asyncio
import json
from dataclasses import dataclass, field
import ssl
import threading
import websockets
from websockets.asyncio.client import ClientConnection
import logging
from typing import Any

from services.event_bus import Event, EventBus

from utils.models import (
    LockfileData,
    PresenceWebsocketEvent,
    FriendRequestWebsocketEvent,
    FriendWebsocketEvent,
    WebsocketEventWrapper,
)

logger: logging.Logger = logging.getLogger(__name__)

RETRY_TIMER = 3
# Riot client websocket uses WAMP v1.0 protocol where you can subscribe to receive
# certain events using an array [operation: int, subscription_type: str]
# Operations: 5 -> subscribe, 6 -> unsubscribe
# subscription_type: OnJsonApiEvent -> everything. All other events can be obtained
# using the local /help endpoint
WAMP_SUBSCRIPTIONS = [
    [5, "OnJsonApiEvent_chat_v4_presences"],        # /chat/v4/presences
    [5, "OnJsonApiEvent_chat_v4_friendrequests"],   # /chat/v4/friendrequests
    [5, "OnJsonApiEvent_chat_v4_friends"],           # /chat/v4/friends
]

# Map WAMP topic URIs to (parser, event) pairs
_TOPIC_PRESENCE = "OnJsonApiEvent_chat_v4_presences"
_TOPIC_FRIEND_REQUESTS = "OnJsonApiEvent_chat_v4_friendrequests"
_TOPIC_FRIENDS = "OnJsonApiEvent_chat_v4_friends"


@dataclass
class GameSocket:
    """WebSocket client for the local Riot Client API."""

    lockfile: LockfileData
    bus: EventBus
    main_loop: asyncio.AbstractEventLoop
    retry_timer: int = 3
    websocket: ClientConnection | None = field(init=False, default=None)
    _cancelled: threading.Event = field(init=False)
    _wss_url: str = field(init=False)
    _ssl_ctx: ssl.SSLContext = field(init=False)

    def __post_init__(self) -> None:
        self._cancelled = threading.Event()
        self._wss_url = self.lockfile.wss_url
        self._ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    async def connect(self) -> None:
        """Connect to the local websocket with retry polling, then listen for events."""

        while not self._cancelled.is_set():
            try:
                async with websockets.connect(self._wss_url, 
                                              ssl=self._ssl_ctx, 
                                              additional_headers={"Authorization": self.lockfile.auth_header
                                            }) as websocket:
                    self.websocket = websocket
                    for sub in WAMP_SUBSCRIPTIONS:
                        await websocket.send(json.dumps(sub))
                    logger.info(f"Connected to Riot Client WebSocket on port {self.lockfile.port} ({len(WAMP_SUBSCRIPTIONS)} subscriptions)")

                    # Recv loop
                    while not self._cancelled.is_set():
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                            self._handle_message(raw)

                        except asyncio.TimeoutError:
                            continue  # No message, check stop flag and loop back
                        except UnicodeDecodeError:
                            logger.debug("Skipping non-UTF-8 binary websocket frame")

                    logger.info("WebSocket connection closed")
                    return

            except (OSError, websockets.WebSocketException) as e:
                logger.debug(f"WebSocket not ready: {e}. Retrying in {self.retry_timer}s")
                await asyncio.sleep(self.retry_timer)

    def _handle_message(self, raw: str | bytes) -> None:
        """Parse a WAMP message and route to the correct event based on topic."""

        try:
            parsed: list[object] = json.loads(str(raw))  # pyright: ignore[reportAny]
            wrapper = WebsocketEventWrapper.from_raw(parsed)  # pyright: ignore[reportUnknownVariableType]
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.debug(f"Non-JSON websocket message: {raw[:200] if raw else raw}")
            return

        topic: str = wrapper.topic

        if topic == _TOPIC_PRESENCE:
            data = PresenceWebsocketEvent.from_raw_string(raw=str(raw))
            event = Event.WEBSOCKET_EVENT
        elif topic == _TOPIC_FRIEND_REQUESTS:
            data = FriendRequestWebsocketEvent.from_raw_wamp(wrapper)  # pyright: ignore[reportUnknownArgumentType]
            event = Event.FRIEND_REQUEST_EVENT
        elif topic == _TOPIC_FRIENDS:
            data = FriendWebsocketEvent.from_raw_wamp(wrapper)   # pyright: ignore[reportUnknownArgumentType]
            event = Event.FRIEND_EVENT
        else:
            logger.debug(f"Unhandled websocket topic: {topic}")
            return

        _ = asyncio.run_coroutine_threadsafe(
            self.bus.emit(event, data),
            self.main_loop,
        )

    def close(self) -> None:
        """Signal the websocket listener to stop."""
        self._cancelled.set()


class GameSocketHandler:
    """
    Manages GameSocket lifecycle via the event bus.

    The websocket session is created when the lockfile becomes available
    and destroyed when Valorant closes or the app shuts down.
    """

    def __init__(self, bus: EventBus, retry_timer: int = RETRY_TIMER) -> None:
        self.bus: EventBus = bus
        self.retry_timer: int = retry_timer
        self.gamesocket: GameSocket | None = None
        self._thread: threading.Thread | None = None
        self._register()

    def _register(self) -> None:
        """Subscribe to relevant events."""
        _ = self.bus.on(Event.VALORANT_OPENED, self._on_valorant_open, priority=10)
        _ = self.bus.on(Event.VALORANT_CLOSED, self._on_valorant_close, priority=10)
        _ = self.bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)

    async def _on_valorant_open(self, data: LockfileData) -> None:
        """Start websocket when Valorant opens and lockfile is available."""

        logger.info("Establishing websocket connection ...")

        main_loop = asyncio.get_running_loop()
        self.gamesocket = GameSocket(data, self.bus, main_loop, self.retry_timer)

        self._thread = threading.Thread(target=self._run_socket, name="GameSocket", daemon=True)
        self._thread.start()

    def _run_socket(self) -> None:
        """Thread entry point => creates a new event loop and runs the websocket listener"""
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self.gamesocket.connect())  # pyright: ignore[reportOptionalMemberAccess]
        except Exception as e:
            logger.error(f"GameSocket loop crashed: {e}")
        finally:
            loop.close()

    async def _on_valorant_close(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """Valorant closed -> tear down websocket."""
        await self._cleanup()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportUnusedParameter, reportAny]
        """App shutting down -> tear down websocket."""
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Close the websocket and wait for the thread to finish."""
        if self.gamesocket:
            self.gamesocket.close()
            self.gamesocket = None

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("GameSocket thread did not stop in time")
            else:
                logger.info("GameSocket thread stopped")

        self._thread = None

