"""
Endpoint-aware request scheduler for Riot API requests.

Three independent workers run in parallel, each draining its own queue(s):

- **Unlimited worker**: drains the state queue first, then the userinfo
  queue. No pacing; the underlying endpoints (presence, owned, loadout,
  XP, penalties, store, task list, task ack, etc.) have no Riot
  rate-limit. The state queue is purged on game-state transitions; the
  userinfo queue persists across them. NOT affected by pause/resume —
  the rationale for pausing is to yield rate-limit budget to other apps,
  and these endpoints don't consume any.

- **match-details worker**: paces calls at a minimum interval (default
  1700ms) measured between successive requests. Three sub-priority
  queues: ``self`` (own freshly-completed match) > ``task`` (server-
  issued task) > ``dig`` (background sweep). Strict priority, FIFO
  within a tier. Pausable.

- **match-history worker**: same shape as match-details, separate
  pacer and separate sub-queues. Pausable.

- **competitive-updates worker**: same shape, separate pacer
  (default 2050ms) and separate sub-queues. Pausable.

Pause/resume affects only the three paced workers. Stop tears down all
four. Game-state transitions purge the state queue (and cancel the
in-flight state request) but leave the paced queues intact, so a
freshly-finished match still submits after the transition.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal
from collections.abc import Awaitable, Callable

logger: logging.Logger = logging.getLogger(__name__)


Priority = Literal["self", "task", "dig"]


class IntervalPacer:
    """Minimum-interval pacer for a single endpoint.

    Records the monotonic timestamp of the last release and forces the
    next caller to wait until ``min_interval_seconds`` has elapsed since
    that timestamp.
    """

    def __init__(self, min_interval_seconds: float, label: str = "") -> None:
        self._min_interval: float = min_interval_seconds
        self._last_sent: float = 0.0
        self._label: str = label

    async def acquire(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_sent
        if self._last_sent > 0.0 and elapsed < self._min_interval:
            wait = self._min_interval - elapsed
            logger.debug(f"pacer[{self._label}] sleeping {wait:.3f}s")
            await asyncio.sleep(wait)
        self._last_sent = time.monotonic()


@dataclass(slots=True)
class QueuedRequest:
    """A request waiting to be executed by the scheduler."""

    execute: Callable[[], Awaitable[Any]]  # pyright: ignore[reportExplicitAny]
    label: str = ""


class RequestScheduler:
    """Four-worker priority scheduler for API requests.

    See module docstring for the queue/worker layout. Public surface:

    - ``enqueue_state(execute, label)``: unlimited, purged on state change
    - ``enqueue_userinfo(execute, label)``: unlimited, persistent
    - ``enqueue_match_details(execute, priority, label)``: paced
    - ``enqueue_match_history(execute, priority, label)``: paced
    - ``enqueue_competitive_updates(execute, priority, label)``: paced
    - ``pause()`` / ``resume()``: affects paced workers only
    - ``on_state_change()``: purges state queue, cancels in-flight state req
    - ``start()`` / ``stop()``: lifecycle
    """

    def __init__(
        self,
        match_details_interval_s: float,
        match_history_interval_s: float,
        competitive_updates_interval_s: float,
    ) -> None:
        # Unlimited worker queues
        self._state_queue: deque[QueuedRequest] = deque()
        self._userinfo_queue: deque[QueuedRequest] = deque()

        # match-details sub-priority queues
        self._md_self_queue: deque[QueuedRequest] = deque()
        self._md_task_queue: deque[QueuedRequest] = deque()
        self._md_dig_queue: deque[QueuedRequest] = deque()

        # match-history sub-priority queues
        self._mh_self_queue: deque[QueuedRequest] = deque()
        self._mh_task_queue: deque[QueuedRequest] = deque()
        self._mh_dig_queue: deque[QueuedRequest] = deque()

        # competitive-updates sub-priority queues
        self._cu_self_queue: deque[QueuedRequest] = deque()
        self._cu_task_queue: deque[QueuedRequest] = deque()
        self._cu_dig_queue: deque[QueuedRequest] = deque()

        # Pacers
        self._md_pacer: IntervalPacer = IntervalPacer(match_details_interval_s, "match-details")
        self._mh_pacer: IntervalPacer = IntervalPacer(match_history_interval_s, "match-history")
        self._cu_pacer: IntervalPacer = IntervalPacer(competitive_updates_interval_s, "competitive-updates")

        # Per-worker wake-up signals
        self._unlimited_notify: asyncio.Event = asyncio.Event()
        self._md_notify: asyncio.Event = asyncio.Event()
        self._mh_notify: asyncio.Event = asyncio.Event()
        self._cu_notify: asyncio.Event = asyncio.Event()

        # Paced-worker pause gate (shared by md + mh; unlimited ignores it)
        self._paced_active: asyncio.Event = asyncio.Event()
        self._paced_active.set()

        self._running: bool = False
        self._workers: list[asyncio.Task[None]] = []

        # In-flight tracking for state-request cancellation on state change
        self._current_state_task: asyncio.Task[Any] | None = None  # pyright: ignore[reportExplicitAny]

    # ------------------- Public API -------------------

    @property
    def state_queue_size(self) -> int:
        return len(self._state_queue)

    @property
    def userinfo_queue_size(self) -> int:
        return len(self._userinfo_queue)

    @property
    def match_details_task_queue_size(self) -> int:
        return len(self._md_task_queue)

    @property
    def match_history_task_queue_size(self) -> int:
        return len(self._mh_task_queue)

    @property
    def competitive_updates_task_queue_size(self) -> int:
        return len(self._cu_task_queue)

    @property
    def task_queue_size(self) -> int:
        """Combined count of outstanding ``task``-priority paced requests."""
        return (
            len(self._md_task_queue)
            + len(self._mh_task_queue)
            + len(self._cu_task_queue)
        )

    def start(self) -> None:
        """Start the four worker loops. No-op if already running."""
        if self._running:
            return
        self._running = True
        self._paced_active.set()
        self._workers = [
            asyncio.create_task(self._unlimited_worker()),
            asyncio.create_task(self._match_details_worker()),
            asyncio.create_task(self._match_history_worker()),
            asyncio.create_task(self._competitive_updates_worker()),
        ]
        logger.info("Request scheduler started (4 workers)")

    def stop(self) -> None:
        """Stop all workers and purge every queue."""
        self._running = False
        # Wake every worker so they re-check _running and exit
        self._unlimited_notify.set()
        self._md_notify.set()
        self._mh_notify.set()
        self._cu_notify.set()
        self._paced_active.set()  # unblock any pause waits so the worker can exit
        for w in self._workers:
            if not w.done():
                _ = w.cancel()
        self._workers = []
        self._cancel_current_state()
        self._state_queue.clear()
        self._userinfo_queue.clear()
        self._md_self_queue.clear()
        self._md_task_queue.clear()
        self._md_dig_queue.clear()
        self._mh_self_queue.clear()
        self._mh_task_queue.clear()
        self._mh_dig_queue.clear()
        self._cu_self_queue.clear()
        self._cu_task_queue.clear()
        self._cu_dig_queue.clear()
        logger.info("Request scheduler stopped")

    def pause(self) -> None:
        """Pause the paced workers. Unlimited worker keeps draining."""
        if self._paced_active.is_set():
            logger.debug("Paced workers paused")
        self._paced_active.clear()

    def resume(self) -> None:
        """Resume the paced workers."""
        if not self._paced_active.is_set():
            logger.debug("Paced workers resumed")
        self._paced_active.set()
        self._md_notify.set()
        self._mh_notify.set()
        self._cu_notify.set()

    def on_state_change(self) -> None:
        """Handle a game-state transition.

        Purges all state-bound requests and cancels the in-flight state
        request (if any). Userinfo and paced queues are untouched, so a
        freshly-completed match still submits after the transition.
        """
        self._purge_state_queue()
        self._cancel_current_state()

    def enqueue_state(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        self._state_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued state request: {label}")
        self._unlimited_notify.set()

    def enqueue_userinfo(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        label: str = "",
    ) -> None:
        self._userinfo_queue.append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued userinfo request: {label}")
        self._unlimited_notify.set()

    def enqueue_match_details(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        priority: Priority,
        label: str = "",
    ) -> None:
        self._priority_queue_md(priority).append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued match-details({priority}) request: {label}")
        self._md_notify.set()

    def enqueue_match_history(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        priority: Priority,
        label: str = "",
    ) -> None:
        self._priority_queue_mh(priority).append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued match-history({priority}) request: {label}")
        self._mh_notify.set()

    def enqueue_competitive_updates(
        self,
        execute: Callable[[], Awaitable[Any]],  # pyright: ignore[reportExplicitAny]
        priority: Priority,
        label: str = "",
    ) -> None:
        self._priority_queue_cu(priority).append(QueuedRequest(execute=execute, label=label))
        logger.debug(f"Enqueued competitive-updates({priority}) request: {label}")
        self._cu_notify.set()

    # ------------------- Internal -------------------

    def _priority_queue_md(self, priority: Priority) -> deque[QueuedRequest]:
        match priority:
            case "self":
                return self._md_self_queue
            case "task":
                return self._md_task_queue
            case "dig":
                return self._md_dig_queue

    def _priority_queue_mh(self, priority: Priority) -> deque[QueuedRequest]:
        match priority:
            case "self":
                return self._mh_self_queue
            case "task":
                return self._mh_task_queue
            case "dig":
                return self._mh_dig_queue

    def _priority_queue_cu(self, priority: Priority) -> deque[QueuedRequest]:
        match priority:
            case "self":
                return self._cu_self_queue
            case "task":
                return self._cu_task_queue
            case "dig":
                return self._cu_dig_queue

    def _cancel_current_state(self) -> None:
        if self._current_state_task and not self._current_state_task.done():
            _ = self._current_state_task.cancel()
            logger.debug("Cancelled in-flight state request")

    def _purge_state_queue(self) -> None:
        count = len(self._state_queue)
        self._state_queue.clear()
        if count:
            logger.info(f"Purged {count} state-bound request(s)")

    @staticmethod
    def _pop_first_nonempty(queues: list[deque[QueuedRequest]]) -> QueuedRequest | None:
        for q in queues:
            if q:
                return q.popleft()
        return None

    async def _unlimited_worker(self) -> None:
        """Drains state then userinfo. No pacing, no pause gate."""
        logger.debug("Unlimited worker started")
        while self._running:
            # State takes precedence over userinfo
            item: QueuedRequest | None
            from_state: bool = False
            if self._state_queue:
                item = self._state_queue.popleft()
                from_state = True
            elif self._userinfo_queue:
                item = self._userinfo_queue.popleft()
            else:
                item = None

            if item is None:
                self._unlimited_notify.clear()
                _ = await self._unlimited_notify.wait()
                continue

            try:
                task: asyncio.Task[Any] = asyncio.create_task(coro=item.execute())  # pyright: ignore[reportExplicitAny, reportArgumentType, reportUnknownVariableType]
                if from_state:
                    self._current_state_task = task
                logger.debug(f"Executing {'state' if from_state else 'userinfo'} request: {item.label}")
                _ = await task  # pyright: ignore[reportAny]
            except asyncio.CancelledError:
                logger.debug(f"Request cancelled: {item.label}")
                if not self._running:
                    break
            except Exception as e:
                logger.warning(f"Request failed ({item.label}): {e}")
            finally:
                if from_state:
                    self._current_state_task = None
        logger.debug("Unlimited worker stopped")

    async def _paced_worker(
        self,
        name: str,
        pacer: IntervalPacer,
        queues_in_priority_order: Callable[[], list[deque[QueuedRequest]]],
        notify: asyncio.Event,
    ) -> None:
        logger.debug(f"{name} worker started")
        while self._running:
            # Honor pause
            if not self._paced_active.is_set():
                _ = await self._paced_active.wait()
                continue

            item = self._pop_first_nonempty(queues_in_priority_order())
            if item is None:
                notify.clear()
                _ = await notify.wait()
                continue

            try:
                await pacer.acquire()
                # Re-check pause after acquire (it may have flipped while sleeping)
                if not self._paced_active.is_set():
                    # Put item back at the front of its priority tier -> simplest
                    # is to re-enqueue at the head of the highest-priority queue
                    # it would belong to. Since we lost the original tier, just
                    # push to "self" so it runs ASAP on resume. Conservatively
                    # safe because pause-during-acquire is rare.
                    queues_in_priority_order()[0].appendleft(item)
                    continue
                logger.debug(f"Executing {name} request: {item.label}")
                _ = await item.execute()  # pyright: ignore[reportAny]
            except asyncio.CancelledError:
                logger.debug(f"Request cancelled: {item.label}")
                if not self._running:
                    break
            except Exception as e:
                logger.warning(f"Request failed ({item.label}): {e}")
        logger.debug(f"{name} worker stopped")

    async def _match_details_worker(self) -> None:
        await self._paced_worker(
            name="match-details",
            pacer=self._md_pacer,
            queues_in_priority_order=lambda: [
                self._md_self_queue,
                self._md_task_queue,
                self._md_dig_queue,
            ],
            notify=self._md_notify,
        )

    async def _match_history_worker(self) -> None:
        await self._paced_worker(
            name="match-history",
            pacer=self._mh_pacer,
            queues_in_priority_order=lambda: [
                self._mh_self_queue,
                self._mh_task_queue,
                self._mh_dig_queue,
            ],
            notify=self._mh_notify,
        )

    async def _competitive_updates_worker(self) -> None:
        await self._paced_worker(
            name="competitive-updates",
            pacer=self._cu_pacer,
            queues_in_priority_order=lambda: [
                self._cu_self_queue,
                self._cu_task_queue,
                self._cu_dig_queue,
            ],
            notify=self._cu_notify,
        )
