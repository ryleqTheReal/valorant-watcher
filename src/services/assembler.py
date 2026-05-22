"""
Assembler module: aggregates paginated Riot responses into single payloads.

Today this module exposes HistoryAssembler, which probes match-history
page 0, fans out the remaining pages into the request scheduler, merges
the results in arrival order, and emits Event.MATCH_HISTORY_FETCHED
exactly once per assembly (success or failure).

A future CompetitiveUpdateAssembler will live alongside it once that
endpoint is wired up — see docs/TODO_COMPETITIVE_UPDATES.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from dataclasses import asdict

from services.event_bus import Event, EventBus
from services.request_scheduler import Priority, RequestScheduler
from utils.models import (
    CompetitiveUpdate,
    CompetitiveUpdateEvent,
    MatchHistoryEvent,
    ValorantCompetitiveUpdatesResponse,
)

logger: logging.Logger = logging.getLogger(__name__)

HISTORY_PAGE_SIZE: int = 20
COMPETITIVE_UPDATES_PAGE_SIZE: int = 20
_RATELIMIT_RETRY_DELAY: float = 5.0


@dataclass(slots=True)
class _HistoryAssembly:
    """In-flight aggregation state for a single match-history assembly."""

    puuid: str
    shard: str
    priority: Priority
    future: asyncio.Future[MatchHistoryEvent]
    max_pages: int | None
    pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    max_total: int = 0
    max_end_index: int = 0
    failed_status: int | None = None
    next_start: int = 0
    pages_fetched: int = 0


class HistoryAssembler:
    """Drives multi-page match-history assemblies via the request scheduler.

    Each assemble() call walks pages sequentially: probe page 0, learn
    Total, then fetch pages 20-40, 40-60, ... one at a time, never
    enqueueing the next page until the current one returns. Emits one
    MATCH_HISTORY_FETCHED on completion.

    Concurrent assemble() calls on the same instance are serialized
    via an internal asyncio.Lock — assembly N+1 only begins once
    assembly N has finalized. This prevents the queue from filling
    up with partial assemblies sharing pacer slots.

    429s do NOT count as failure: the affected page re-enqueues itself
    and the assembly stays open until it resolves.

    Priority is per-call so the same assembler instance can serve
    server tasks ("task"), the collector's own account ("dig" — gap
    filler), and DFS dig walks ("dig").
    """

    def __init__(
        self,
        session: Any,  # RiotSession (typed Any to avoid import cycle)
        scheduler: RequestScheduler,
        bus: EventBus,
        *,
        page_size: int = HISTORY_PAGE_SIZE,
    ) -> None:
        self._session: Any = session
        self._scheduler: RequestScheduler = scheduler
        self._bus: EventBus = bus
        self._page_size: int = page_size
        self._serial_lock: asyncio.Lock = asyncio.Lock()

    async def assemble(
        self,
        puuid: str,
        shard: str,
        *,
        priority: Priority,
        max_pages: int | None = None,
    ) -> MatchHistoryEvent:
        """Assemble a player's match history and emit MATCH_HISTORY_FETCHED.

        Concurrent calls serialize — N+1 waits for N to finalize before
        enqueueing its first page.

        Args:
            puuid: Subject PUUID.
            shard: Region shard (pd_shard) the history belongs to.
            priority: Scheduler priority for every page request.
            max_pages: Optional cap on pages fetched (incl. the probe).
                None means fetch all pages up to Total.

        Returns:
            The same MatchHistoryEvent that was emitted on the bus.
        """
        async with self._serial_lock:
            fut: asyncio.Future[MatchHistoryEvent] = (
                asyncio.get_running_loop().create_future()
            )
            asm = _HistoryAssembly(
                puuid=puuid,
                shard=shard,
                priority=priority,
                future=fut,
                max_pages=max_pages,
            )
            self._enqueue_page(asm, 0, self._page_size)
            return await fut

    def _enqueue_page(self, asm: _HistoryAssembly, start: int, end: int) -> None:
        self._scheduler.enqueue_match_history(
            lambda a=asm, s=start, e=end: self._run_page(a, s, e),
            asm.priority,
            f"assemble page {asm.puuid[:8]} [{start},{end})",
        )

    async def _run_page(
        self,
        asm: _HistoryAssembly,
        start: int,
        end: int,
    ) -> None:
        # Session may have been torn down (RSO_LOGOUT / SHUTDOWN) while
        # the page was queued. Abort silently in that case so we don't
        # raise "Cannot send a request, as the client has been closed."
        if self._session.client.is_closed:  # pyright: ignore[reportAny]
            if not asm.future.done():
                asm.future.cancel()
            return
        try:
            _parsed, raw, status = await self._session.general_get_history_raw(
                asm.puuid, start_index=start, end_index=end, shard=asm.shard,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"Assembler page {asm.puuid[:8]} [{start},{end}) raised")
            if asm.failed_status is None:
                asm.failed_status = 0
            await self._finalize(asm)
            return

        if status == 429:
            logger.info(f"Assembler page {asm.puuid[:8]} [{start},{end}) got 429; re-enqueueing")
            await asyncio.sleep(_RATELIMIT_RETRY_DELAY)
            self._enqueue_page(asm, start, end)
            return

        if status != 200 or raw is None:
            if asm.failed_status is None:
                asm.failed_status = status
            await self._finalize(asm)
            return

        history_entries = raw.get("History") or []
        total = int(raw.get("Total", 0))  # pyright: ignore[reportAny]
        end_index_val = int(raw.get("EndIndex", end))  # pyright: ignore[reportAny]

        page_list = history_entries if isinstance(history_entries, list) else []
        asm.pages[start] = page_list
        asm.pages_fetched += 1
        if total > asm.max_total:
            asm.max_total = total
        if end_index_val > asm.max_end_index:
            asm.max_end_index = end_index_val

        # Decide whether to keep paginating.
        next_start = end
        # Stop conditions: reached Total, short page returned, page-cap hit.
        if total > 0 and next_start >= total:
            await self._finalize(asm)
            return
        if len(page_list) < self._page_size:
            await self._finalize(asm)
            return
        if asm.max_pages is not None and asm.pages_fetched >= asm.max_pages:
            await self._finalize(asm)
            return

        ceiling = total if total > 0 else next_start + self._page_size
        next_end = min(next_start + self._page_size, ceiling)
        if next_end <= next_start:
            await self._finalize(asm)
            return
        self._enqueue_page(asm, next_start, next_end)

    async def _finalize(self, asm: _HistoryAssembly) -> None:
        if asm.future.done():
            return

        fetch_time_ms = int(time.time() * 1000)

        if asm.failed_status is not None:
            logger.warning(
                f"Assembly for {asm.puuid[:8]} on {asm.shard} aborted with status {asm.failed_status}"
            )
            event = MatchHistoryEvent(
                shard=asm.shard,
                puuid=asm.puuid,
                riot_status=asm.failed_status,
                match_history=None,
                fetch_time_ms=fetch_time_ms,
            )
            await self._bus.emit(Event.MATCH_HISTORY_FETCHED, event)
            asm.future.set_result(event)
            return

        merged: list[dict[str, Any]] = []
        for page_start in sorted(asm.pages.keys()):
            merged.extend(asm.pages[page_start])

        full_payload: dict[str, Any] = {
            "Subject": asm.puuid,
            "BeginIndex": 0,
            "EndIndex": asm.max_end_index,
            "Total": asm.max_total,
            "History": merged,
        }
        logger.info(
            f"Assembly complete for {asm.puuid[:8]} on {asm.shard}: "
            f"{len(merged)} entries (Total={asm.max_total}, EndIndex={asm.max_end_index})"
        )
        event = MatchHistoryEvent(
            shard=asm.shard,
            puuid=asm.puuid,
            riot_status=200,
            match_history=full_payload,
            fetch_time_ms=fetch_time_ms,
        )
        await self._bus.emit(Event.MATCH_HISTORY_FETCHED, event)
        asm.future.set_result(event)


@dataclass(slots=True)
class _CompetitiveUpdateAssembly:
    """In-flight aggregation state for a single competitive-updates assembly.

    Pagination is sequential (Riot doesn't expose Total) so we keep one
    cursor and walk it forward 20 at a time until a short page returns
    or the server says BAD_PARAMETER.
    """
    puuid: str
    shard: str
    priority: Priority
    future: asyncio.Future[CompetitiveUpdateEvent | None]
    matches: list[CompetitiveUpdate] = field(default_factory=list)
    version: int = 0
    subject: str = ""
    next_start: int = 0


class CompetitiveUpdateAssembler:
    """Drives sequential competitive-updates pagination via the scheduler.

    Riot's GET /mmr/v1/players/{puuid}/competitiveupdates endpoint does
    NOT return Total/StartIndex/EndIndex, so we cannot fan out pages the
    way HistoryAssembler does. We walk 20-by-20 instead, stopping when
    either:

      - a page contains < 20 entries (last page reached), OR
      - the server returns 400 with errorCode == "BAD_PARAMETER"
        (Riot's "no more pages" signal).

    429 is retried indefinitely (the page request re-enqueues itself).
    Any other non-200 emits COMPETITIVE_UPDATE_FETCHED with the failing
    status and `competitive_updates=None` — the partial work is
    discarded. Two statuses are NEVER reported to the backend (they are
    silently discarded inside the assembler) because they don't tell us
    anything about whether the item exists:

      - 400 BAD_CLAIMS (entitlement issue, even after auto-refresh)
      - persistent 429 (rate-limit handler exhausted before success)

    Successful assemblies emit one COMPETITIVE_UPDATE_FETCHED with the
    merged ValorantCompetitiveUpdatesResponse as a dict.

    The returned future resolves to the emitted event, or to None when
    the assembly was discarded (so the caller can also "forget" it).
    """

    def __init__(
        self,
        session: Any,  # RiotSession (typed Any to avoid import cycle)
        scheduler: RequestScheduler,
        bus: EventBus,
        *,
        page_size: int = COMPETITIVE_UPDATES_PAGE_SIZE,
    ) -> None:
        self._session: Any = session
        self._scheduler: RequestScheduler = scheduler
        self._bus: EventBus = bus
        self._page_size: int = page_size
        # Serialize concurrent assemble() calls so only one player's
        # pagination is in flight at a time. Without this, every player
        # discovered through the chain would queue up a probe page, and
        # the queue would fill with partial assemblies that each get one
        # page processed per round before yielding.
        self._serial_lock: asyncio.Lock = asyncio.Lock()

    async def assemble(
        self,
        puuid: str,
        shard: str,
        *,
        priority: Priority,
    ) -> CompetitiveUpdateEvent | None:
        """Walk pages 20-by-20 until the end. Returns the emitted event,
        or None when the assembly was discarded.

        Concurrent calls serialize via an internal lock — assembly N+1
        only starts once N has finalized.
        """
        async with self._serial_lock:
            fut: asyncio.Future[CompetitiveUpdateEvent | None] = (
                asyncio.get_running_loop().create_future()
            )
            asm = _CompetitiveUpdateAssembly(
                puuid=puuid,
                shard=shard,
                priority=priority,
                future=fut,
            )
            self._enqueue_page(asm, asm.next_start)
            return await fut

    def _enqueue_page(self, asm: _CompetitiveUpdateAssembly, start: int) -> None:
        end = start + self._page_size
        self._scheduler.enqueue_competitive_updates(
            lambda a=asm, s=start, e=end: self._run_page(a, s, e),
            asm.priority,
            f"comp-update page {asm.puuid[:8]} [{start},{end})",
        )

    async def _run_page(
        self,
        asm: _CompetitiveUpdateAssembly,
        start: int,
        end: int,
    ) -> None:
        # Session may have been torn down while the page was queued —
        # discard the assembly silently rather than raising.
        if self._session.client.is_closed:  # pyright: ignore[reportAny]
            await self._discard(asm)
            return
        try:
            payload, status, error_code = (
                await self._session.general_get_competitive_updates_raw(
                    asm.puuid, start_index=start, end_index=end, shard=asm.shard,
                )
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Comp-update page {asm.puuid[:8]} [{start},{end}) raised"
            )
            await self._fail(asm, riot_status=0)
            return

        # 429 — keep trying, never fail.
        if status == 429:
            logger.info(
                f"Comp-update page {asm.puuid[:8]} [{start},{end}) got 429; re-enqueueing"
            )
            await asyncio.sleep(_RATELIMIT_RETRY_DELAY)
            self._enqueue_page(asm, start)
            return

        # 400 BAD_PARAMETER => end of pagination; finalize with what we have.
        if status == 400 and error_code == "BAD_PARAMETER":
            logger.debug(
                f"Comp-update {asm.puuid[:8]}: BAD_PARAMETER at [{start},{end}); finalizing"
            )
            await self._succeed(asm)
            return

        # 400 BAD_CLAIMS => discard silently, do NOT report to backend.
        if status == 400 and error_code == "BAD_CLAIMS":
            logger.warning(
                f"Comp-update {asm.puuid[:8]}: BAD_CLAIMS at [{start},{end}); discarding"
            )
            await self._discard(asm)
            return

        if status != 200 or payload is None:
            logger.warning(
                f"Comp-update {asm.puuid[:8]} failed at [{start},{end}) "
                f"with status {status} errorCode={error_code!r}; discarding partial work"
            )
            await self._fail(asm, riot_status=status)
            return

        try:
            page = ValorantCompetitiveUpdatesResponse.from_dict(payload)
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Comp-update {asm.puuid[:8]}: failed to parse page [{start},{end})"
            )
            await self._fail(asm, riot_status=0)
            return

        if not asm.subject and page.Subject:
            asm.subject = page.Subject
        if page.Version:
            asm.version = page.Version
        asm.matches.extend(page.Matches)

        # Short page => last page reached.
        if len(page.Matches) < self._page_size:
            logger.debug(
                f"Comp-update {asm.puuid[:8]}: short page ({len(page.Matches)} < "
                f"{self._page_size}) at [{start},{end}); finalizing"
            )
            await self._succeed(asm)
            return

        # Full page: keep walking.
        asm.next_start = end
        self._enqueue_page(asm, asm.next_start)

    async def _succeed(self, asm: _CompetitiveUpdateAssembly) -> None:
        if asm.future.done():
            return
        merged = ValorantCompetitiveUpdatesResponse(
            Subject=asm.subject or asm.puuid,
            Version=asm.version,
            Matches=asm.matches,
        )
        # Parse-then-serialize round trip acts as a schema guard before
        # the payload reaches the backend.
        payload_dict: dict[str, Any] = asdict(merged)  # pyright: ignore[reportExplicitAny]
        logger.info(
            f"Comp-update assembly complete for {asm.puuid[:8]} on {asm.shard}: "
            f"{len(asm.matches)} match(es)"
        )
        event = CompetitiveUpdateEvent(
            shard=asm.shard,
            puuid=asm.puuid,
            riot_status=200,
            competitive_updates=payload_dict,
            fetch_time_ms=int(time.time() * 1000),
        )
        await self._bus.emit(Event.COMPETITIVE_UPDATE_FETCHED, event)
        asm.future.set_result(event)

    async def _fail(self, asm: _CompetitiveUpdateAssembly, *, riot_status: int) -> None:
        """Emit the failure to the backend so the work item can be confirmed
        non-existent / broken by multiple nodes. Partial matches are discarded.
        """
        if asm.future.done():
            return
        event = CompetitiveUpdateEvent(
            shard=asm.shard,
            puuid=asm.puuid,
            riot_status=riot_status,
            competitive_updates=None,
            fetch_time_ms=int(time.time() * 1000),
        )
        await self._bus.emit(Event.COMPETITIVE_UPDATE_FETCHED, event)
        asm.future.set_result(event)

    async def _discard(self, asm: _CompetitiveUpdateAssembly) -> None:
        """Silently drop the assembly without emitting an event. Used for
        statuses that don't tell us anything about the item's existence
        (BAD_CLAIMS, persistent 429).
        """
        if asm.future.done():
            return
        asm.future.set_result(None)
