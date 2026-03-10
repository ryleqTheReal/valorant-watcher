"""
Priority request scheduler for Riot API requests.

Manages two queues with different lifecycle behaviors:

- **State queue** (high priority): Requests bound to the current game state
  (e.g. pregame match info, core-game data). Purged when the loop state changes.
- **General queue** (low priority): Persistent requests that survive state
  changes (e.g. loadout, owned items, XP). Only processed once the state
  queue is empty.

A single worker coroutine drains the state queue first, then falls back to
the general queue.  The scheduler can be paused (e.g. during the ratelimit
offset window) and resumed without losing queued work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from collections.abc import Awaitable, Callable

logger: logging.Logger = logging.getLogger(__name__)


class RateLimiter:
    """Per-minute request rate limiter with two-phase limits.

    After a gamestate change (reset) the first window uses ``initial_limit``
    (6 req in first minute) because other apps have likely consumed the bulk of the
    30 req/min budget, the following windows use ``sustained_limit``
    (20 req/min) to leave headroom for other apps

    Call :meth:`wait_for_slot` before every outgoing request. It will
    ``asyncio.sleep`` until the current window has enough budget
    """

    def __init__(
        self,
        initial_limit: int = 6,
        sustained_limit: int = 20,
        window_seconds: float = 60.0,
    ) -> None:
        self._initial_limit: int = initial_limit
        self._sustained_limit: int = sustained_limit
        self._window_seconds: float = window_seconds

        self._current_limit: int = initial_limit
        self._request_count: int = 0
        self._window_start: float = 0.0
        self._is_first_window: bool = True

    # ---- public ----

    def reset(self) -> None:
        """Resets the counter and ratelimit timer when gamestate changes"""
        
        self._request_count = 0
        self._window_start = 0.0
        self._is_first_window = True
        self._current_limit = self._initial_limit
        logger.debug(f"rate limiter reset (next window: {self._initial_limit} req)")

    async def wait_for_slot(self) -> None:
        """Block until one request slot is available then consume it"""
        now = time.monotonic()

        # First call after reset start the window
        if self._window_start == 0.0:
            self._window_start = now
            self._request_count = 0

        # Roll the window forward if it has expired
        self._slide_window_if_expired(now)

        if self._request_count >= self._current_limit:
            wait = self._window_seconds - (time.monotonic() - self._window_start)
            if wait > 0:
                logger.info(f"rate-limit reached {self._request_count}/{self._current_limit}: waiting {wait}s for next window")
                await asyncio.sleep(wait)
            self._slide_window()

        self._request_count += 1
        logger.debug(f"request {self._request_count}/{self._current_limit} in current window")

    @property
    def remaining(self) -> int:
        """Returns the amount of requests remaining in the current window"""
        
        self._slide_window_if_expired(time.monotonic())
        return max(0, self._current_limit - self._request_count) 

    # -------- Internal Helpers --------

    def _slide_window_if_expired(self, now: float) -> None:
        """Checks whether the time window has expired, if it did, it slides the window"""
        
        elapsed = now - self._window_start
        if elapsed >= self._window_seconds:
            self._slide_window()

    def _slide_window(self) -> None:
        """Slides the window to the next time-frame"""
        
        if self._is_first_window:
            self._is_first_window = False
            self._current_limit = self._sustained_limit
        self._window_start = time.monotonic()
        self._request_count = 0
        logger.debug(f"new rate-limit window started (ends: {self._current_limit})")


@dataclass(slots=True)
class QueuedRequest:
    """A request waiting to be executed by the scheduler."""

    execute: Callable[[], Awaitable[Any]]  # pyright: ignore[reportExplicitAny]
    label: str = ""


class RequestScheduler:
    """Two-tier priority scheduler for API requests.

    INGAME, PREGAME requests are executed first and purged on loopstate changes.
    General requests (matches, player data etc.) persist across state changes and run when the state
    queue is empty.

    The scheduler can be paused (e.g. during ratelimit offset waits)
    and resumed.  While paused, no requests are processed.
    """

    def __init__(
        self,
        initial_limit: int = 6,
        sustained_limit: int = 20,
    ) -> None:
        self._state_queue: deque[QueuedRequest] = deque()
        self._general_queue: deque[QueuedRequest] = deque()

        # Controls whether the worker is allowed to process requests
        self._active: asyncio.Event = asyncio.Event()
        self._active.set()

        # Wakes the worker when new work arrives or state changes
        self._notify: asyncio.Event = asyncio.Event()

        self._running: bool = False
        self._worker_task: asyncio.Task[None] | None = None

        # Tracks the currently executing request so we can cancel it
        self._current_is_state: bool = False
        self._current_task: asyncio.Task[Any] | None = None  # pyright: ignore[reportExplicitAny]

        # Rate limiter enforces per-minute request budgets
        self._rate_limiter: RateLimiter = RateLimiter(
            initial_limit=initial_limit,
            sustained_limit=sustained_limit,
        )

    # ------------------- Public API -------------------

    @property
    def state_queue_size(self) -> int:
        return len(self._state_queue)

    @property
    def general_queue_size(self) -> int:
        return len(self._general_queue)

    def start(self) -> None:
        """Start the worker loop.  No-op if already running."""
        if self._running:
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker())
        logger.info("Request scheduler started")

    def stop(self) -> None:
        """Stop the worker and purge all queues."""
        self._running = False
        self._wake()
        if self._worker_task and not self._worker_task.done():
            _ = self._worker_task.cancel()
        self._worker_task = None
        self._cancel_current()
        self._state_queue.clear()
        self._general_queue.clear()
        logger.info("Request scheduler stopped")

    def pause(self) -> None:
        """Pause request processing (e.g. during ratelimit offset)."""
        self._active.clear()
        self._wake()

    def resume(self) -> None:
        """Resume request processing."""
        self._active.set()
        self._wake()

    def on_state_change(self) -> None:
        """Handle a game state transition.

        Pauses processing, purges all state-bound requests, and cancels
        any in-flight state request.  General queue is left untouched.
        """
        self.pause()
        self._purge_state_queue()
        self._rate_limiter.reset()
        if self._current_is_state:
            self._cancel_current()

    def enqueue_state(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        """Add a state-bound request (high priority, purged on state change)."""
        self._state_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued state request: {label}")
        self._wake()

    def enqueue_general(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        """Add a general request (low priority, survives state changes)."""
        self._general_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued general request: {label}")
        self._wake()

    # ------------------- Internal -------------------

    def _wake(self) -> None:
        """Signal the worker to re-check state."""
        self._notify.set()

    def _cancel_current(self) -> None:
        """Cancel the currently executing request task."""
        if self._current_task and not self._current_task.done():
            _ = self._current_task.cancel()
            logger.debug("Cancelled in-flight request")

    def _try_dequeue(self) -> tuple[QueuedRequest, bool] | None:
        """Get the next request, preferring state queue.

        Returns:
            (request, is_state) or None if both queues are empty.
        """
        if self._state_queue:
            return self._state_queue.popleft(), True
        if self._general_queue:
            return self._general_queue.popleft(), False
        return None

    def _purge_state_queue(self) -> None:
        """Discard all pending state-bound requests."""
        count = len(self._state_queue)
        self._state_queue.clear()
        if count:
            logger.info(f"Purged {count} state-bound request(s)")

    async def _worker(self) -> None:
        """Main worker loop: process requests in priority order."""
        logger.debug("Scheduler worker started")

        while self._running:
            # Block while paused
            if not self._active.is_set():
                _ = await self._active.wait()
                continue

            # Try to get next request
            result = self._try_dequeue()
            if result is None:
                # Nothing to do, wait for a wake-up signal
                self._notify.clear()
                _ = await self._notify.wait()
                continue

            item, is_state = result
            self._current_is_state = is_state
            queue_type = "state" if is_state else "general"

            try:
                # Wait for a rate-limit slot before executing
                await self._rate_limiter.wait_for_slot()
                logger.debug(f"Executing {queue_type} request: {item.label}")
                self._current_task = asyncio.create_task(item.execute())  # pyright: ignore[reportArgumentType]
                _ = await self._current_task  # pyright: ignore[reportAny]
            except asyncio.CancelledError:
                logger.debug(f"Request cancelled: {item.label}")
            except Exception as e:
                logger.warning(f"Request failed ({item.label}): {e}")
            finally:
                self._current_task = None
                self._current_is_state = False

        logger.debug("Scheduler worker stopped")
