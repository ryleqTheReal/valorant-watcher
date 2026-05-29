"""endpoint-aware request scheduler with 4 parallel workers.

- unlimited worker: drains state then userinfo; no pacing, not affected by pause/resume
- match-details worker: paced (default 1700ms), 3 priority tiers (self > task > dig), pausable
- match-history worker: same shape, separate pacer
- competitive-updates worker: same shape, separate pacer (default 2050ms)

pause/resume affects only paced workers; on_state_change() purges the state queue without
touching paced queues so in-progress match submissions survive game-state transitions.
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
    """minimum-interval pacer; blocks the next caller until min_interval_seconds has elapsed since the last release"""

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
    """a request waiting to be executed by the scheduler"""

    execute: Callable[[], Awaitable[Any]]  # pyright: ignore[reportExplicitAny]
    label: str = ""


class RequestScheduler:
    """four-worker priority scheduler for Riot API requests; see module docstring for the worker/queue layout"""

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

        # per-worker wake-up signals
        self._unlimited_notify: asyncio.Event = asyncio.Event()
        self._md_notify: asyncio.Event = asyncio.Event()
        self._mh_notify: asyncio.Event = asyncio.Event()
        self._cu_notify: asyncio.Event = asyncio.Event()

        # fires when a task-priority request is dequeued; signals SubmissionService to refill
        self._task_consumed: asyncio.Event = asyncio.Event()

        # paced-worker pause gate (shared by md + mh; unlimited ignores it)
        self._paced_active: asyncio.Event = asyncio.Event()
        self._paced_active.set()

        self._running: bool = False
        self._workers: list[asyncio.Task[None]] = []

        # in-flight tracking for state-request cancellation on state change
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
    def match_details_dig_queue_size(self) -> int:
        """depth of the dig-priority match-details backlog (for backpressure)"""
        return len(self._md_dig_queue)

    @property
    def match_history_task_queue_size(self) -> int:
        return len(self._mh_task_queue)

    @property
    def competitive_updates_task_queue_size(self) -> int:
        return len(self._cu_task_queue)

    @property
    def task_queue_size(self) -> int:
        """combined count of outstanding task-priority paced requests"""
        return (
            len(self._md_task_queue)
            + len(self._mh_task_queue)
            + len(self._cu_task_queue)
        )

    @property
    def task_consumed_event(self) -> asyncio.Event:
        """set whenever a task-priority paced request is dequeued"""
        return self._task_consumed

    def start(self) -> None:
        """start the four worker loops; no-op if already running"""
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
        """stop all workers and purge every queue"""
        self._running = False
        # wake every worker so they re-check _running and exit
        self._unlimited_notify.set()
        self._md_notify.set()
        self._mh_notify.set()
        self._cu_notify.set()
        self._paced_active.set()  # unblock pause waits so workers can exit
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
        """pause the paced workers; unlimited worker keeps draining"""
        if self._paced_active.is_set():
            logger.debug("Paced workers paused")
        self._paced_active.clear()

    def resume(self) -> None:
        """resume the paced workers"""
        if not self._paced_active.is_set():
            logger.debug("Paced workers resumed")
        self._paced_active.set()
        self._md_notify.set()
        self._mh_notify.set()
        self._cu_notify.set()

    def on_state_change(self) -> None:
        """purge state queue and cancel in-flight state request; paced queues are untouched"""
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
    def _pop_first_nonempty(
        queues: list[deque[QueuedRequest]],
    ) -> tuple[QueuedRequest | None, int]:
        for i, q in enumerate(queues):
            if q:
                return q.popleft(), i
        return None, -1

    async def _unlimited_worker(self) -> None:
        """drain state then userinfo; no pacing, no pause gate"""
        logger.debug("Unlimited worker started")
        while self._running:
            # state takes precedence over userinfo
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
            # honor pause
            if not self._paced_active.is_set():
                _ = await self._paced_active.wait()
                continue

            item, tier_index = self._pop_first_nonempty(queues_in_priority_order())
            if item is None:
                notify.clear()
                _ = await notify.wait()
                continue

            # tier 1 is the task queue (self=0, task=1, dig=2); signal subscribers that it shrank
            if tier_index == 1:
                self._task_consumed.set()

            try:
                await pacer.acquire()
                # re-check pause after acquire (may have flipped while sleeping)
                if not self._paced_active.is_set():
                    # push to "self" so it runs ASAP on resume (pause-during-acquire is rare; original tier is lost)
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
