"""
Submission service: bridges local Riot data collection with the backend API.

Responsibilities
================

- **Server tasks first.** Polls `GET /v1/tasks` whenever a game token is
  available and the scheduler's task queue is low. Each task is dispatched
  as a `task`-priority scheduler request so it pre-empts DFS work but
  still yields to in-game state requests.
- **Match-details tasks** are single Riot fetches. The result (including
  non-200 status) is emitted as MATCH_DETAIL_FETCHED and buffered for
  POST /v1/matches.
- **Match-history tasks** require a *complete* history. We probe page 0
  for `Total`, enqueue every remaining page, then merge them into a
  single payload (concat `History`, max `EndIndex`, max `Total`) before
  emitting MATCH_HISTORY_FETCHED. If any page fails (status != 200) the
  assembly aborts and reports the failure status with `match_history`
  set to None. 429s do NOT count as failure: the page task is
  re-enqueued and the assembly stays open until it eventually resolves.
- **Batched submission** of all collected matches/histories. Buffers
  flush every 30 s or when the API max (100 matches / 50 histories) is
  hit; SHUTDOWN forces a final flush.
- **Offline-resilient.** If the backend rejects or is unreachable the
  batch is appended to `data/pending/{matches,histories}.jsonl`. On the
  next successful submission cycle those files are drained first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from services.auth_service import RiotSession
from services.backend_service import BackendCommunicationService
from services.event_bus import Event, EventBus
from services.gamestates import GamestateHandler
from services.request_scheduler import RequestScheduler
from utils.file_utils import get_pending_histories_path, get_pending_matches_path
from utils.models import AccountXPResponse, IngameLoadoutsEvent, MatchDetailEvent, MatchHistoryEvent, OwnedItemsResponse, PenaltiesResponse

logger: logging.Logger = logging.getLogger(__name__)

# Match-details payloads can be megabytes each; the backend's HTTP body
# limit kicks in well before the API's 100-item cap. We start small and
# rely on _submit_with_split to halve further on 413.
_MATCH_BATCH_MAX: int = 10
_HISTORY_BATCH_MAX: int = 50
_FLUSH_INTERVAL_SEC: float = 30.0
_TASK_POLL_INTERVAL_SEC: float = 60.0
_TASK_REFILL_THRESHOLD: int = 5
_TASKS_NEEDED: int = 15
_HISTORY_PAGE_SIZE: int = 20
_RATELIMIT_RETRY_DELAY: float = 5.0  # local cushion before re-enqueueing a 429


@dataclass(slots=True)
class _HistoryAssembly:
    """Per-PUUID aggregation state for an in-flight match_history task."""

    puuid: str
    shard: str
    pages: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    max_total: int = 0
    max_end_index: int = 0
    pending_pages: int = 0
    failed_status: int | None = None  # First non-200/non-429 status seen
    probe_done: bool = False


class SubmissionService:
    """Orchestrates server tasks + batched submission of collected data."""

    def __init__(
        self,
        bus: EventBus,
        backend: BackendCommunicationService,
        scheduler: RequestScheduler,
    ) -> None:
        self._bus: EventBus = bus
        self._backend: BackendCommunicationService = backend
        self._scheduler: RequestScheduler = scheduler
        self._session: RiotSession | None = None

        self._client: httpx.AsyncClient = httpx.AsyncClient(timeout=20.0)

        self._matches_buffer: list[dict[str, Any]] = []
        self._histories_buffer: list[dict[str, Any]] = []
        self._buffer_lock: asyncio.Lock = asyncio.Lock()
        # Serialize submission per endpoint so the periodic flush and the
        # size-cap flush cannot both drain the pending file at the same time.
        self._match_submit_lock: asyncio.Lock = asyncio.Lock()
        self._history_submit_lock: asyncio.Lock = asyncio.Lock()

        self._pending_matches_path: Path = get_pending_matches_path()
        self._pending_histories_path: Path = get_pending_histories_path()

        self._assemblies: dict[str, _HistoryAssembly] = {}

        self._flush_task: asyncio.Task[None] | None = None
        self._task_task: asyncio.Task[None] | None = None
        self._cancelled: asyncio.Event = asyncio.Event()

        self._register()

    def _register(self) -> None:
        _ = self._bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=4)
        _ = self._bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=4)
        _ = self._bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)
        _ = self._bus.on(Event.MATCH_DETAIL_FETCHED, self._on_match_detail, priority=0)
        _ = self._bus.on(Event.MATCH_HISTORY_FETCHED, self._on_match_history, priority=0)
        _ = self._bus.on(Event.USER_XP_UPDATED, self._on_user_xp_updated, priority=0)
        _ = self._bus.on(Event.OWNED_ITEMS_UPDATED, self._on_owned_items_updated, priority=0)
        _ = self._bus.on(Event.INGAME_LOADOUTS_FETCHED, self._on_ingame_loadouts_fetched, priority=0)
        _ = self._bus.on(Event.PENALTIES_UPDATED, self._on_penalties_updated, priority=0)

    # ------------------- Event handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        self._session = data["session"]
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._task_task is None or self._task_task.done():
            self._task_task = asyncio.create_task(self._task_loop())

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        await self._flush_all()
        self._session = None
        self._assemblies.clear()

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        self._cancelled.set()
        await self._flush_all()
        await self._client.aclose()

    async def _on_match_detail(self, ev: MatchDetailEvent) -> None:
        # riot_status == 0 is an internal transport-failure sentinel; the
        # backend's schema rejects it. Drop silently — the task will be
        # re-issued on the next /v1/tasks poll.
        if ev.riot_status == 0:
            logger.debug(f"Dropping match-detail {ev.match_id[:8]} with sentinel riot_status=0")
            return
        item: dict[str, Any] = {
            "match_id": ev.match_id,
            "shard": ev.shard,
            "riot_status": ev.riot_status,
            "match_details": ev.match_details,
        }
        should_flush: bool = False
        async with self._buffer_lock:
            self._matches_buffer.append(item)
            should_flush = len(self._matches_buffer) >= _MATCH_BATCH_MAX
        if should_flush:
            _ = asyncio.create_task(self._flush_matches())

    async def _on_match_history(self, ev: MatchHistoryEvent) -> None:
        if ev.riot_status == 0:
            logger.debug(f"Dropping match-history {ev.puuid[:8]} with sentinel riot_status=0")
            return
        item: dict[str, Any] = {
            "puuid": ev.puuid,
            "shard": ev.shard,
            "riot_status": ev.riot_status,
            "match_history": ev.match_history,
            "fetch_time": ev.fetch_time_ms,
        }
        should_flush: bool = False
        async with self._buffer_lock:
            self._histories_buffer.append(item)
            should_flush = len(self._histories_buffer) >= _HISTORY_BATCH_MAX
        if should_flush:
            _ = asyncio.create_task(self._flush_histories())

    async def _on_user_xp_updated(self, data: AccountXPResponse) -> None:
        # Fire-and-forget: the server keeps only the latest snapshot per puuid,
        # so a missed intermediate update is recovered by the next successful
        # submission (which yields a single larger delta). No disk spill needed.
        headers = self._backend.game_headers
        if headers is None:
            logger.info("XP update received but backend game token unavailable; skipping")
            return
        body: dict[str, Any] = asdict(data)  # pyright: ignore[reportExplicitAny]
        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/xp",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/xp transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted XP snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/xp -> HTTP {response.status_code} body={response.text[:200]!r}"
        )

    async def _on_penalties_updated(self, data: PenaltiesResponse) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Penalties update received but backend game token unavailable; skipping")
            return
        body: dict[str, Any] = asdict(data)  # pyright: ignore[reportExplicitAny]
        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/penalties",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/penalties transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted penalties snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/penalties -> HTTP {response.status_code} body={response.text[:200]!r}"
        )   

    async def _on_owned_items_updated(self, data: OwnedItemsResponse) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Owned items received but backend game token unavailable; skipping")
            return
        
        body: dict[str, Any] = asdict(data)  # pyright: ignore[reportExplicitAny]
        
        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/owned-items",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/owned-items transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted owned items snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/owned-items -> HTTP {response.status_code} body={response.text[:200]!r}"
        )
        
        
    async def _on_ingame_loadouts_fetched(self, data: IngameLoadoutsEvent) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Ingame loadouts received but backend game token unavailable; skipping")
            return

        body: dict[str, Any] = asdict(data.loadouts)  # pyright: ignore[reportExplicitAny]

        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/match-loadouts",
                params={"match_id": data.match_id},
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/match-loadouts transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted ingame loadout snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/match-loadouts -> HTTP {response.status_code} body={response.text[:200]!r}"
        )
    # ------------------- Flush loop -------------------

    async def _flush_loop(self) -> None:
        while not self._cancelled.is_set():
            try:
                _ = await asyncio.wait_for(
                    self._cancelled.wait(), timeout=_FLUSH_INTERVAL_SEC,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._flush_all()
            except Exception:  # noqa: BLE001
                logger.exception("Periodic flush failed")

    async def _flush_all(self) -> None:
        await self._flush_matches()
        await self._flush_histories()

    async def _flush_matches(self) -> None:
        async with self._match_submit_lock:
            await self._drain_pending(self._pending_matches_path, "/v1/matches", _MATCH_BATCH_MAX)
            async with self._buffer_lock:
                if not self._matches_buffer:
                    return
                batch = self._matches_buffer[:_MATCH_BATCH_MAX]
                self._matches_buffer = self._matches_buffer[_MATCH_BATCH_MAX:]
            if not await self._post_batch("/v1/matches", batch):
                self._spill(self._pending_matches_path, batch)

    async def _flush_histories(self) -> None:
        async with self._history_submit_lock:
            await self._drain_pending(self._pending_histories_path, "/v1/histories", _HISTORY_BATCH_MAX)
            async with self._buffer_lock:
                if not self._histories_buffer:
                    return
                batch = self._histories_buffer[:_HISTORY_BATCH_MAX]
                self._histories_buffer = self._histories_buffer[_HISTORY_BATCH_MAX:]
            if not await self._post_batch("/v1/histories", batch):
                self._spill(self._pending_histories_path, batch)

    async def _post_batch(self, path: str, batch: list[dict[str, Any]]) -> bool:
        if not batch:
            return True
        headers = self._backend.game_headers
        if headers is None:
            logger.info(f"Backend game token unavailable; spilling {len(batch)} item(s) to disk ({path})")
            return False
        try:
            response = await self._client.post(
                f"{self._backend.base_url}{path}",
                json=batch,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST {path} transport error ({len(batch)} item(s)): {e}")
            return False
        if response.status_code != 202:
            logger.warning(
                f"POST {path} -> HTTP {response.status_code} ({len(batch)} item(s)) body={response.text[:200]!r}"
            )
            return False
        logger.info(f"Submitted {len(batch)} item(s) to {path}")
        return True

    @staticmethod
    def _spill(path: Path, batch: list[dict[str, Any]]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for item in batch:
                    _ = f.write(json.dumps(item, separators=(",", ":")) + "\n")
            logger.info(f"Spilled {len(batch)} item(s) to {path}")
        except OSError as e:
            logger.warning(f"Could not spill {len(batch)} item(s) to {path}: {e}")

    async def _drain_pending(self, path: Path, endpoint: str, batch_max: int) -> None:
        if self._backend.game_headers is None:
            return
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning(f"Could not read pending file {path}: {e}")
            return

        items: list[dict[str, Any]] = []
        dropped_sentinel = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry: dict[str, Any] = json.loads(line)  # pyright: ignore[reportAny]
            except json.JSONDecodeError:
                logger.warning(f"Dropping malformed pending entry in {path.name}")
                continue
            if entry.get("riot_status") == 0:
                dropped_sentinel += 1
                continue
            items.append(entry)
        if dropped_sentinel:
            logger.warning(
                f"Dropped {dropped_sentinel} pending entr(ies) from {path.name} with sentinel riot_status=0"
            )

        if not items:
            try:
                path.unlink()
            except OSError:
                pass
            return

        logger.info(f"Draining {len(items)} pending item(s) from {path.name}")
        idx = 0
        while idx < len(items):
            chunk = items[idx:idx + batch_max]
            if not await self._post_batch(endpoint, chunk):
                # Server still rejecting or unreachable; rewrite the unsent tail.
                remaining = items[idx:]
                self._rewrite_pending(path, remaining)
                return
            idx += batch_max

        try:
            path.unlink()
        except OSError as e:
            logger.debug(f"Could not remove drained pending file {path}: {e}")

    @staticmethod
    def _rewrite_pending(path: Path, items: list[dict[str, Any]]) -> None:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            lines = "\n".join(json.dumps(item, separators=(",", ":")) for item in items)
            _ = tmp.write_text(lines + ("\n" if lines else ""), encoding="utf-8")
            _ = tmp.replace(path)
        except OSError as e:
            logger.warning(f"Could not rewrite pending file {path}: {e}")

    # ------------------- Task loop -------------------

    async def _task_loop(self) -> None:
        while not self._cancelled.is_set():
            try:
                _ = await asyncio.wait_for(
                    self._cancelled.wait(), timeout=_TASK_POLL_INTERVAL_SEC,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self._fetch_and_dispatch_tasks()
            except Exception:  # noqa: BLE001
                logger.exception("Task poll iteration failed")

    async def _fetch_and_dispatch_tasks(self) -> None:
        if self._session is None:
            return
        if self._scheduler.task_queue_size >= _TASK_REFILL_THRESHOLD:
            return
        headers = self._backend.game_headers
        if headers is None:
            return

        try:
            response = await self._client.get(
                f"{self._backend.base_url}/v1/tasks",
                params={"needed": str(_TASKS_NEEDED)},
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"GET /v1/tasks transport error: {e}")
            return

        if response.status_code != 200:
            logger.warning(f"GET /v1/tasks -> HTTP {response.status_code} {response.text[:200]!r}")
            return

        try:
            tasks: list[dict[str, Any]] = response.json()  # pyright: ignore[reportAny]
        except ValueError:
            logger.warning("GET /v1/tasks returned non-JSON body")
            return

        if not isinstance(tasks, list):  # pyright: ignore[reportUnnecessaryIsInstance]
            return

        if not tasks:
            return

        logger.info(f"Received {len(tasks)} task(s) from backend")
        for task in tasks:
            self._dispatch_task(task)

    def _dispatch_task(self, task: dict[str, Any]) -> None:
        item_type = task.get("item_type")
        shard = task.get("shard")
        target = task.get("target_id")
        if not (isinstance(item_type, str) and isinstance(shard, str) and isinstance(target, str)):
            logger.warning(f"Skipping malformed task: {task}")
            return

        if item_type == "match_details":
            self._scheduler.enqueue_task(
                lambda mid=target, sh=shard: self._run_detail_task(mid, sh),
                f"task detail {target[:8]}",
            )
        elif item_type == "match_history":
            self._begin_history_task(target, shard)
        else:
            logger.warning(f"Unknown task item_type: {item_type!r}")

    # ------------------- Match-details task -------------------

    async def _run_detail_task(self, match_id: str, shard: str) -> None:
        if self._session is None:
            return
        try:
            payload, status = await self._session.general_get_details_raw(match_id, shard=shard)
        except Exception:  # noqa: BLE001
            logger.exception(f"Task detail {match_id[:8]} on {shard} raised")
            await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=shard, match_id=match_id, riot_status=0, match_details=None,
            ))
            return

        if status == 429:
            logger.info(f"Task detail {match_id[:8]} got 429; re-enqueueing")
            await asyncio.sleep(_RATELIMIT_RETRY_DELAY)
            self._scheduler.enqueue_task(
                lambda mid=match_id, sh=shard: self._run_detail_task(mid, sh),
                f"task detail {match_id[:8]} (retry)",
            )
            return

        await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=shard, match_id=match_id, riot_status=status, match_details=payload,
        ))

    # ------------------- Match-history task -------------------

    def _begin_history_task(self, puuid: str, shard: str) -> None:
        if puuid in self._assemblies:
            logger.debug(f"History task for {puuid[:8]} already in progress, ignoring duplicate")
            return
        asm = _HistoryAssembly(puuid=puuid, shard=shard, pending_pages=1)
        self._assemblies[puuid] = asm
        self._scheduler.enqueue_task(
            lambda a=asm, s=0, e=_HISTORY_PAGE_SIZE: self._run_history_page(a, s, e, is_probe=True),
            f"task history probe {puuid[:8]}",
        )

    def _enqueue_history_page(self, asm: _HistoryAssembly, start: int, end: int) -> None:
        asm.pending_pages += 1
        self._scheduler.enqueue_task(
            lambda a=asm, s=start, e=end: self._run_history_page(a, s, e, is_probe=False),
            f"task history page {asm.puuid[:8]} [{start},{end})",
        )

    async def _run_history_page(
        self,
        asm: _HistoryAssembly,
        start: int,
        end: int,
        is_probe: bool,
    ) -> None:
        if self._session is None:
            asm.pending_pages -= 1
            await self._maybe_finalize_history(asm)
            return

        try:
            _parsed, raw, status = await self._session.general_get_history_raw(
                asm.puuid, start_index=start, end_index=end, shard=asm.shard,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"History page {asm.puuid[:8]} [{start},{end}) raised")
            if asm.failed_status is None:
                asm.failed_status = 0
            asm.pending_pages -= 1
            await self._maybe_finalize_history(asm)
            return

        # Retry 429s indefinitely until the page succeeds or fails another way.
        if status == 429:
            logger.info(f"History page {asm.puuid[:8]} [{start},{end}) got 429; re-enqueueing")
            await asyncio.sleep(_RATELIMIT_RETRY_DELAY)
            self._scheduler.enqueue_task(
                lambda a=asm, s=start, e=end, p=is_probe: self._run_history_page(a, s, e, p),
                f"task history page {asm.puuid[:8]} [{start},{end}) (retry)",
            )
            return

        if status != 200 or raw is None:
            if asm.failed_status is None:
                asm.failed_status = status
            asm.pending_pages -= 1
            await self._maybe_finalize_history(asm)
            return

        history_entries = raw.get("History") or []
        total = int(raw.get("Total", 0))  # pyright: ignore[reportAny]
        end_index_val = int(raw.get("EndIndex", end))  # pyright: ignore[reportAny]

        asm.pages[start] = history_entries if isinstance(history_entries, list) else []
        if total > asm.max_total:
            asm.max_total = total
        if end_index_val > asm.max_end_index:
            asm.max_end_index = end_index_val

        # Fan out remaining pages once we know Total from the probe.
        if is_probe and not asm.probe_done:
            asm.probe_done = True
            for page_start in range(_HISTORY_PAGE_SIZE, total, _HISTORY_PAGE_SIZE):
                page_end = min(page_start + _HISTORY_PAGE_SIZE, total)
                self._enqueue_history_page(asm, page_start, page_end)

        asm.pending_pages -= 1
        await self._maybe_finalize_history(asm)

    async def _maybe_finalize_history(self, asm: _HistoryAssembly) -> None:
        if asm.pending_pages > 0:
            return
        # Re-check via pop to avoid races with duplicate finalize calls.
        if self._assemblies.pop(asm.puuid, None) is None:
            return

        fetch_time_ms = int(time.time() * 1000)

        if asm.failed_status is not None:
            logger.warning(
                f"History assembly for {asm.puuid[:8]} on {asm.shard} aborted with status {asm.failed_status}"
            )
            await self._bus.emit(Event.MATCH_HISTORY_FETCHED, MatchHistoryEvent(
                shard=asm.shard,
                puuid=asm.puuid,
                riot_status=asm.failed_status,
                match_history=None,
                fetch_time_ms=fetch_time_ms,
            ))
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
            f"History assembly complete for {asm.puuid[:8]} on {asm.shard}: "
            f"{len(merged)} entries (Total={asm.max_total}, EndIndex={asm.max_end_index})"
        )
        await self._bus.emit(Event.MATCH_HISTORY_FETCHED, MatchHistoryEvent(
            shard=asm.shard,
            puuid=asm.puuid,
            riot_status=200,
            match_history=full_payload,
            fetch_time_ms=fetch_time_ms,
        ))
