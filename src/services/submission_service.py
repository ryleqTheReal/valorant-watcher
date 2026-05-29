"""submission service: bridges Riot data collection with the backend API.

polls GET /v1/tasks for server-assigned work (match-details, match-history, competitive-updates)
and dispatches at task-priority. batches results to the backend every 20s or at buffer cap.
offline-resilient: failed batches spill to JSONL and are drained on the next successful cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from services.assembler import CompetitiveUpdateAssembler, HistoryAssembler
from services.auth_service import RiotSession
from services.backend_service import BackendCommunicationService
from services.event_bus import Event, EventBus
from services.request_scheduler import RequestScheduler
from utils.file_utils import (
    get_pending_competitive_updates_path,
    get_pending_histories_path,
    get_pending_matches_path,
)
from utils.models import (
    AccountXPResponse,
    BalancesResponse,
    CompetitiveUpdateEvent,
    IngameLoadoutsEvent,
    MatchDetailEvent,
    MatchHistoryEvent,
    OwnedItemsResponse,
    PenaltiesResponse,
    PregameMatchResponse,
    StorefrontResponse,
)

logger: logging.Logger = logging.getLogger(__name__)

# Match-details payloads can be megabytes each; the backend's HTTP body
# limit kicks in well before the API's 100-item cap. We start small and
# rely on _submit_with_split to halve further on 413.
_MATCH_BATCH_MAX: int = 15
_HISTORY_BATCH_MAX: int = 50
_COMP_UPDATE_BATCH_MAX: int = 50
_FLUSH_INTERVAL_SEC: float = 20.0
_TASK_POLL_INTERVAL_SEC: float = 20.0
_TASK_REFILL_THRESHOLD: int = 5
_TASKS_NEEDED: int = 41
_RATELIMIT_RETRY_DELAY: float = 5.0  # local cushion before re-enqueueing a 429


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

        self._matches_buffer: list[bytes] = []
        self._histories_buffer: list[bytes] = []
        self._comp_updates_buffer: list[bytes] = []
        self._buffer_lock: asyncio.Lock = asyncio.Lock()
        # Serialize submission per endpoint so the periodic flush and the
        # size-cap flush cannot both drain the pending file at the same time.
        self._match_submit_lock: asyncio.Lock = asyncio.Lock()
        self._history_submit_lock: asyncio.Lock = asyncio.Lock()
        self._comp_update_submit_lock: asyncio.Lock = asyncio.Lock()

        self._pending_matches_path: Path = get_pending_matches_path()
        self._pending_histories_path: Path = get_pending_histories_path()
        self._pending_comp_updates_path: Path = get_pending_competitive_updates_path()

        self._assembler: HistoryAssembler | None = None
        self._comp_update_assembler: CompetitiveUpdateAssembler | None = None

        self._flush_task: asyncio.Task[None] | None = None
        self._task_task: asyncio.Task[None] | None = None
        self._cancelled: asyncio.Event = asyncio.Event()

        # count of server tasks still in flight (queued + mid-assembly);
        # scheduler's task_queue_size misses multi-page assemblies, so we track separately to prevent double-claiming
        self._outstanding_tasks: int = 0

        self._register()

    def _register(self) -> None:
        _ = self._bus.on(Event.AUTH_SUCCESS, self._on_auth_success, priority=4)
        _ = self._bus.on(Event.RSO_LOGOUT, self._on_rso_logout, priority=4)
        _ = self._bus.on(Event.SHUTDOWN, self._on_shutdown, priority=0)
        _ = self._bus.on(Event.MATCH_DETAIL_FETCHED, self._on_match_detail, priority=0)
        _ = self._bus.on(Event.MATCH_HISTORY_FETCHED, self._on_match_history, priority=0)
        _ = self._bus.on(Event.COMPETITIVE_UPDATE_FETCHED, self._on_competitive_update, priority=0)
        _ = self._bus.on(Event.USER_XP_UPDATED, self._on_user_xp_updated, priority=0)
        _ = self._bus.on(Event.OWNED_ITEMS_UPDATED, self._on_owned_items_updated, priority=0)
        _ = self._bus.on(Event.INGAME_LOADOUTS_FETCHED, self._on_ingame_loadouts_fetched, priority=0)
        _ = self._bus.on(Event.PENALTIES_UPDATED, self._on_penalties_updated, priority=0)
        _ = self._bus.on(Event.BALANCES_UPDATED, self._on_balances_updated, priority=0)
        _ = self._bus.on(Event.PREGAME_MATCH_UPDATED, self._on_pregame_match_updated, priority=0)
        _ = self._bus.on(Event.STORE_OFFERS_UPDATED, self._on_store_offers_updated, priority=0)

    # ------------------- Event handlers -------------------

    async def _on_auth_success(self, data: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        self._session = data["session"]
        self._assembler = HistoryAssembler(self._session, self._scheduler, self._bus)
        self._comp_update_assembler = CompetitiveUpdateAssembler(
            self._session, self._scheduler, self._bus,
        )
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._task_task is None or self._task_task.done():
            self._task_task = asyncio.create_task(self._task_loop())

    async def _on_rso_logout(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        await self._flush_all()
        self._session = None
        self._assembler = None
        self._comp_update_assembler = None

    async def _on_shutdown(self, data: Any = None) -> None:  # pyright: ignore[reportExplicitAny, reportAny, reportUnusedParameter]
        self._cancelled.set()
        await self._flush_all()
        await self._client.aclose()

    async def _on_match_detail(self, ev: MatchDetailEvent) -> None:
        # riot_status == 0 is an internal transport-failure sentinel; the
        # backend's schema rejects it. Drop silently: the task will be
        # re-issued on the next /v1/tasks poll.
        if ev.riot_status == 0:
            logger.debug(f"Dropping match-detail {ev.match_id[:8]} with sentinel riot_status=0")
            return
        # game_start_millis is required for backend expiry pruning.
        # Prefer the value threaded from the source (history/comp-update
        # entry) so non-200 responses still carry a timestamp.
        game_start_millis: int | None = ev.game_start_millis
        if game_start_millis is None and isinstance(ev.match_details, dict):
            match_info = ev.match_details.get("matchInfo")
            if isinstance(match_info, dict):
                raw = match_info.get("gameStartMillis")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                if isinstance(raw, int) and raw > 0:
                    game_start_millis = raw
        if game_start_millis is None:
            logger.debug(f"Dropping match-detail {ev.match_id[:8]}: no game_start_millis")
            return
        item_bytes: bytes = json.dumps({
            "match_id": ev.match_id,
            "shard": ev.shard,
            "riot_status": ev.riot_status,
            "game_start_millis": game_start_millis,
            "match_details": ev.match_details,
        }, separators=(",", ":")).encode()
        should_flush: bool = False
        async with self._buffer_lock:
            self._matches_buffer.append(item_bytes)
            should_flush = len(self._matches_buffer) >= _MATCH_BATCH_MAX
        if should_flush:
            _ = asyncio.create_task(self._flush_matches())

    async def _on_competitive_update(self, ev: CompetitiveUpdateEvent) -> None:
        # assembler already filters BAD_CLAIMS/persistent-429; events here are either
        # 200+payload or a confirmed failure (non-200, payload=None) the backend needs to see
        if ev.riot_status == 0:
            logger.debug(f"Dropping comp-update {ev.puuid[:8]} with sentinel riot_status=0")
            return
        item_bytes: bytes = json.dumps({
            "shard": ev.shard,
            "puuid": ev.puuid,
            "riot_status": ev.riot_status,
            "fetch_time": ev.fetch_time_ms,
            "competitive_updates": ev.competitive_updates,
        }, separators=(",", ":")).encode()
        should_flush: bool = False
        async with self._buffer_lock:
            self._comp_updates_buffer.append(item_bytes)
            should_flush = len(self._comp_updates_buffer) >= _COMP_UPDATE_BATCH_MAX
        if should_flush:
            _ = asyncio.create_task(self._flush_comp_updates())

    async def _on_match_history(self, ev: MatchHistoryEvent) -> None:
        if ev.riot_status == 0:
            logger.debug(f"Dropping match-history {ev.puuid[:8]} with sentinel riot_status=0")
            return
        item_bytes: bytes = json.dumps({
            "puuid": ev.puuid,
            "shard": ev.shard,
            "riot_status": ev.riot_status,
            "match_history": ev.match_history,
            "fetch_time": ev.fetch_time_ms,
        }, separators=(",", ":")).encode()
        should_flush: bool = False
        async with self._buffer_lock:
            self._histories_buffer.append(item_bytes)
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

    async def _on_balances_updated(self, data: BalancesResponse) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Balances update received but backend game token unavailable; skipping")
            return
        body: dict[str, Any] = asdict(data)  # pyright: ignore[reportExplicitAny]
        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/balances",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/balances transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted balances snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/balances -> HTTP {response.status_code} body={response.text[:200]!r}"
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
        
    async def _on_store_offers_updated(self, data: StorefrontResponse) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Storefront update received but backend game token unavailable; skipping")
            return

        # Optional store sections (BonusStore, AccessoryStore, etc.) come
        # and go between rotations. Drop None entries so absent sections
        # are absent in the payload rather than serialized as nulls.
        body: dict[str, Any] = {k: v for k, v in asdict(data).items() if v is not None}  # pyright: ignore[reportExplicitAny, reportAny]

        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/account/storefront",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/account/storefront transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted storefront snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/account/storefront -> HTTP {response.status_code} body={response.text[:200]!r}"
        )

    async def _on_pregame_match_updated(self, data: PregameMatchResponse) -> None:
        headers = self._backend.game_headers
        if headers is None:
            logger.info("Pregame match update received but backend game token unavailable; skipping")
            return

        body: dict[str, Any] = asdict(data)  # pyright: ignore[reportExplicitAny]

        try:
            response = await self._client.post(
                f"{self._backend.base_url}/v1/pregame",
                json=body,
                headers=headers,
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST /v1/pregame transport error: {e}")
            return
        if response.status_code in (202, 204):
            logger.info(f"Submitted pregame match snapshot (HTTP {response.status_code})")
            return
        logger.warning(
            f"POST /v1/pregame -> HTTP {response.status_code} body={response.text[:200]!r}"
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
                await self._flush_matches()
                await self._flush_histories()
                await self._flush_comp_updates()
            except Exception:  # noqa: BLE001
                logger.exception("Periodic flush failed")

    async def _flush_all(self) -> None:
        await self._flush_matches()
        await self._flush_histories()
        await self._flush_comp_updates()

    async def _flush_matches(self) -> None:
        async with self._match_submit_lock:
            await self._drain_pending(self._pending_matches_path, "/v1/matches", _MATCH_BATCH_MAX)
            async with self._buffer_lock:
                if not self._matches_buffer:
                    return
                batch = self._matches_buffer[:_MATCH_BATCH_MAX]
                self._matches_buffer = self._matches_buffer[_MATCH_BATCH_MAX:]
            if not await self._post_batch_raw("/v1/matches", batch):
                self._spill_raw(self._pending_matches_path, batch)

    async def _flush_histories(self) -> None:
        async with self._history_submit_lock:
            await self._drain_pending(self._pending_histories_path, "/v1/histories", _HISTORY_BATCH_MAX)
            async with self._buffer_lock:
                if not self._histories_buffer:
                    return
                batch = self._histories_buffer[:_HISTORY_BATCH_MAX]
                self._histories_buffer = self._histories_buffer[_HISTORY_BATCH_MAX:]
            if not await self._post_batch_raw("/v1/histories", batch):
                self._spill_raw(self._pending_histories_path, batch)

    async def _flush_comp_updates(self) -> None:
        async with self._comp_update_submit_lock:
            await self._drain_pending(
                self._pending_comp_updates_path,
                "/v1/competitive-updates",
                _COMP_UPDATE_BATCH_MAX,
            )
            async with self._buffer_lock:
                if not self._comp_updates_buffer:
                    return
                batch = self._comp_updates_buffer[:_COMP_UPDATE_BATCH_MAX]
                self._comp_updates_buffer = self._comp_updates_buffer[_COMP_UPDATE_BATCH_MAX:]
            if not await self._post_batch_raw("/v1/competitive-updates", batch):
                self._spill_raw(self._pending_comp_updates_path, batch)

    async def _post_batch_raw(self, path: str, batch: list[bytes]) -> bool:
        """POST a batch of pre-serialized JSON items; builds the array from raw bytes to avoid round-tripping through dicts"""
        if not batch:
            return True
        headers = self._backend.game_headers
        if headers is None:
            logger.info(f"Backend game token unavailable; spilling {len(batch)} item(s) to disk ({path})")
            return False
        body = b"[" + b",".join(batch) + b"]"
        try:
            response = await self._client.post(
                f"{self._backend.base_url}{path}",
                content=body,
                headers={**headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError as e:
            logger.warning(f"POST {path} transport error ({len(batch)} item(s)): {type(e).__name__}: {e!r}")
            return False
        if response.status_code != 202:
            logger.warning(
                f"POST {path} -> HTTP {response.status_code} ({len(batch)} item(s)) body={response.text[:200]!r}"
            )
            return False
        logger.info(f"Submitted {len(batch)} item(s) to {path}")
        return True

    @staticmethod
    def _spill_raw(path: Path, batch: list[bytes]) -> None:
        """Spill pre-serialized JSON items to a JSONL file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as f:
                for item in batch:
                    _ = f.write(item)
                    _ = f.write(b"\n")
            logger.info(f"Spilled {len(batch)} item(s) to {path}")
        except OSError as e:
            logger.warning(f"Could not spill {len(batch)} item(s) to {path}: {e}")

    async def _drain_pending(self, path: Path, endpoint: str, batch_max: int) -> None:
        """drain pending items from the spill file in bounded chunks (batch_max per iteration);
        keeps raw bytes to minimize memory use; loops until empty or POST fails"""
        if self._backend.game_headers is None:
            return

        is_matches = endpoint == "/v1/matches"

        while path.exists():
            batch: list[bytes] = []
            file_exhausted: bool = False

            try:
                with path.open("rb") as f:
                    while True:
                        raw_line = f.readline()
                        if not raw_line:
                            file_exhausted = True
                            break
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        # Lightweight validation without full parse
                        try:
                            probe: dict[str, Any] = json.loads(stripped)  # pyright: ignore[reportAny, reportExplicitAny]
                        except json.JSONDecodeError:
                            logger.warning(f"Dropping malformed pending entry in {path.name}")
                            continue
                        if probe.get("riot_status") == 0:
                            continue
                        if is_matches and not probe.get("game_start_millis"):
                            continue
                        del probe
                        batch.append(stripped)
                        if len(batch) >= batch_max:
                            break

                    tail = f.read() if not file_exhausted else b""
            except OSError as e:
                logger.warning(f"Could not read pending file {path}: {e}")
                return

            if not batch:
                try:
                    path.unlink()
                except OSError:
                    pass
                return

            logger.info(f"Draining {len(batch)} pending item(s) from {path.name}")

            if not await self._post_batch_raw(endpoint, batch):
                return

            if file_exhausted or not tail.strip():
                try:
                    path.unlink()
                except OSError as e:
                    logger.debug(f"Could not remove drained pending file {path}: {e}")
                return

            try:
                tmp = path.with_suffix(path.suffix + ".tmp")
                _ = tmp.write_bytes(tail)
                _ = tmp.replace(path)
            except OSError as e:
                logger.warning(f"Could not rewrite pending file {path}: {e}")
                return

    # ------------------- Task loop -------------------

    async def _task_loop(self) -> None:
        consumed = self._scheduler.task_consumed_event
        while not self._cancelled.is_set():
            # Wake on either: cancellation, a task-priority dequeue (refill
            # opportunity), or the periodic fallback timer.
            cancel_wait = asyncio.create_task(self._cancelled.wait())
            consumed_wait = asyncio.create_task(consumed.wait())
            done, pending = await asyncio.wait(  # pyright: ignore[reportUnusedVariable]
                {cancel_wait, consumed_wait},
                timeout=_TASK_POLL_INTERVAL_SEC,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                _ = p.cancel()
                try:
                    await p
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            if self._cancelled.is_set():
                return
            consumed.clear()
            try:
                await self._fetch_and_dispatch_tasks()
            except Exception:  # noqa: BLE001
                logger.exception("Task poll iteration failed")

    async def _fetch_and_dispatch_tasks(self) -> None:
        if self._session is None:
            return
        if self._outstanding_tasks >= _TASK_REFILL_THRESHOLD:
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
            tasks: list[dict[str, Any]] = response.json()  # pyright: ignore[reportAny, reportExplicitAny]
        except ValueError:
            logger.warning("GET /v1/tasks returned non-JSON body")
            return

        if not tasks:
            return

        counts: dict[str, int] = {}
        for task in tasks:
            it = task.get("item_type")
            key = it if isinstance(it, str) else "unknown"
            counts[key] = counts.get(key, 0) + 1
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        logger.info(
            f"Received {len(tasks)} task(s) from backend ({breakdown}); "  # pyright: ignore[reportImplicitStringConcatenation]
            f"outstanding before dispatch={self._outstanding_tasks}"
        )
        for task in tasks:
            self._dispatch_task(task)

    def _dispatch_task(self, task: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        item_type = task.get("item_type")
        shard = task.get("shard")
        target = task.get("target_id")
        if not (isinstance(item_type, str) and isinstance(shard, str) and isinstance(target, str)):
            logger.warning(f"Skipping malformed task: {task}")
            return

        if item_type == "match_details":
            self._outstanding_tasks += 1
            self._scheduler.enqueue_match_details(
                lambda mid=target, sh=shard: self._run_detail_task(mid, sh),
                "task",
                f"task detail {target[:8]}",
            )
        elif item_type == "match_history":
            if self._assembler is None:
                logger.warning(f"Dropping match_history task for {target[:8]}: assembler not ready")
                return
            assembler = self._assembler
            self._outstanding_tasks += 1
            _ = asyncio.create_task(self._tracked_assemble(assembler, target, shard))
        elif item_type == "competitive_updates":
            if self._comp_update_assembler is None:
                logger.warning(
                    f"Dropping competitive_updates task for {target[:8]}: assembler not ready"
                )
                return
            cu_assembler = self._comp_update_assembler
            self._outstanding_tasks += 1
            _ = asyncio.create_task(self._tracked_assemble(cu_assembler, target, shard))
        else:
            logger.warning(f"Unknown task item_type: {item_type!r}")

    async def _tracked_assemble(
        self,
        assembler: HistoryAssembler | CompetitiveUpdateAssembler,
        target: str,
        shard: str,
    ) -> None:
        try:
            _ = await assembler.assemble(target, shard, priority="task")
        finally:
            self._outstanding_tasks -= 1

    # ------------------- Match-details task -------------------

    async def _run_detail_task(self, match_id: str, shard: str) -> None:
        if self._session is None:
            self._outstanding_tasks -= 1
            return
        try:
            payload, status = await self._session.general_get_details_raw(match_id, shard=shard)
        except Exception:  # noqa: BLE001
            logger.exception(f"Task detail {match_id[:8]} on {shard} raised")
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=shard, match_id=match_id, riot_status=0, match_details=None,
            ))
            self._outstanding_tasks -= 1
            return

        if status == 429:
            logger.info(f"Task detail {match_id[:8]} got 429; re-enqueueing")
            await asyncio.sleep(_RATELIMIT_RETRY_DELAY)
            # Outstanding count stays the same: this task isn't done yet.
            self._scheduler.enqueue_match_details(
                lambda mid=match_id, sh=shard: self._run_detail_task(mid, sh),
                "task",
                f"task detail {match_id[:8]} (retry)",
            )
            return

        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=shard, match_id=match_id, riot_status=status, match_details=payload,
        ))
        self._outstanding_tasks -= 1

    # Match-history task assembly is delegated to HistoryAssembler.
