"""two-phase match collection pipeline.

Phase 1 (Fresh): on each launch, assemble the player's full history, fan out details
for every unseen match, and harvest players for dig + competitive updates.

Phase 2 (Dig): randomized DFS over the match-player graph; each visited player triggers
a full history assembly. 15% teleport chance restarts from a new random unvisited player.

All collector requests run at "dig" priority so server tasks always pre-empt them.
Central dedup sets (fetched_matches, dig_visited, dig_visited_matches) persist across sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from pathlib import Path
from typing import Any

from services.assembler import CompetitiveUpdateAssembler, HistoryAssembler
from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from services.request_scheduler import RequestScheduler
from utils.exceptions import LeaderboardFallbackError
from utils.models import (
    AccountProgress,
    CompetitiveUpdateEvent,
    LeaderboardResponse,
    MatchDetailEvent,
    MatchHistoryEvent,
    MatchWatermark,
)

logger: logging.Logger = logging.getLogger(__name__)

DIG_RESTART_CHANCE: float = 0.15
MAX_UNVISITED_PLAYERS: int = 5000
_COLLECTOR_PRIORITY = "dig"  # all collector requests yield to "task"

# Riot expires match details 90 days after start; drop with a 1h buffer
# so we never enqueue a fetch that races the expiry
_MATCH_MAX_AGE_MS: int = (90 * 24 - 1) * 60 * 60 * 1000

# dedup windows; large enough to remember in-flight matches/players;
# a tiny window defeats dedup and drives dig-queue growth
_FETCHED_MATCHES_CAP: int = 20_000
_DIG_VISITED_CAP: int = 20_000
_DIG_VISITED_MATCHES_CAP: int = 20_000
_COMP_UPDATE_SEEN_CAP: int = 10_000
_COMP_UPDATE_QUEUE_MAX: int = 25

# ceiling on the dig match-detail backlog; the chain stops enqueueing past it
# "task"/"self" work and the dig-walk driver are exempt
_DIG_DETAIL_QUEUE_MAX: int = 200


def _cap_set(s: set[str], limit: int) -> None:
    """prune a set to half its limit when exceeded; re-fetching evicted entries is harmless (dedup only)"""
    if len(s) <= limit:
        return
    target = limit // 2
    excess = len(s) - target
    victims: list[str] = []
    for item in s:
        if len(victims) >= excess:
            break
        victims.append(item)
    s.difference_update(victims)
    logger.info(f"Pruned set from {len(s) + excess} to {len(s)} entries")


def _is_expired(start_time_ms: int | None) -> bool:
    """true if the match start time is known and older than 89d 23h"""
    if not start_time_ms or start_time_ms <= 0:
        return False
    return (int(time.time() * 1000) - start_time_ms) >= _MATCH_MAX_AGE_MS


class MatchCollector:
    """progressively collects match history and details across sessions; see module docstring for pipeline details"""

    def __init__(
        self,
        session: RiotSession,
        bus: EventBus,
        scheduler: RequestScheduler,
        watermark_path: Path,
    ) -> None:
        self._session: RiotSession = session
        self._bus: EventBus = bus
        self._scheduler: RequestScheduler = scheduler
        self._watermark_path: Path = watermark_path
        self._assembler: HistoryAssembler = HistoryAssembler(session, scheduler, bus)
        self._comp_update_assembler: CompetitiveUpdateAssembler = (
            CompetitiveUpdateAssembler(session, scheduler, bus)
        )

        # Collection state
        self._phase: str = "fresh"  # fresh | dig | done
        self._running: bool = False
        self._fresh_task: asyncio.Task[None] | None = None

        # Central state, loaded from watermark
        self._watermark: MatchWatermark = MatchWatermark()

        # Dig phase: in-memory unvisited pool + per-walk pending detail queue
        self._unvisited_players: set[str] = set()
        self._dig_detail_queue: deque[tuple[str, int | None]] = deque()

        # in-session dedup for competitive-update assemblies (per-puuid)
        self._comp_update_seen: set[str] = set()
        self._comp_update_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_COMP_UPDATE_QUEUE_MAX)
        self._comp_update_worker: asyncio.Task[None] | None = None

        self._load_watermark()
        self._register_chain_subscribers()

    def _register_chain_subscribers(self) -> None:
        """subscribe to all three fetch events to fan out the collection chain:
        history -> comp-update + details; comp-update -> details; details -> harvest + comp-update per player"""
        _ = self._bus.on(Event.MATCH_HISTORY_FETCHED, self._on_history_event, priority=5)
        _ = self._bus.on(
            Event.COMPETITIVE_UPDATE_FETCHED, self._on_competitive_update_event, priority=5,
        )
        _ = self._bus.on(Event.MATCH_DETAIL_FETCHED, self._on_match_detail_event, priority=5)

    # --------- Cross-collector chain handlers ---------

    async def _on_history_event(self, ev: MatchHistoryEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.match_history:
            return
        # same puuid -> comp-update assembly
        self._enqueue_competitive_update(ev.puuid)
        # history entries -> match-detail fetches
        entries: list[Any] = ev.match_history.get("History") or []  # pyright: ignore[reportExplicitAny]
        for entry in entries:  # pyright: ignore[reportAny]
            # stop fanning out once the dig backlog is saturated
            if self._scheduler.match_details_dig_queue_size >= _DIG_DETAIL_QUEUE_MAX:
                break
            if not isinstance(entry, dict):
                continue
            mid = entry.get("MatchID")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            start = entry.get("GameStartTime")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            start_ms = start if isinstance(start, int) else None
            if _is_expired(start_ms):
                continue
            if isinstance(mid, str) and mid and mid not in self._watermark.fetched_matches:
                self._watermark.fetched_matches.add(mid)
                self._enqueue_chain_detail(mid, ev.shard, start_ms)

    async def _on_competitive_update_event(self, ev: CompetitiveUpdateEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.competitive_updates:
            return
        matches: list[Any] = ev.competitive_updates.get("Matches") or []  # pyright: ignore[ reportExplicitAny]
        for entry in matches:  # pyright: ignore[reportAny]
            # stop fanning out once the dig backlog is saturated
            if self._scheduler.match_details_dig_queue_size >= _DIG_DETAIL_QUEUE_MAX:
                break
            if not isinstance(entry, dict):
                continue
            mid = entry.get("MatchID")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            start = entry.get("MatchStartTime")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            start_ms = start if isinstance(start, int) else None
            if _is_expired(start_ms):
                continue
            if isinstance(mid, str) and mid and mid not in self._watermark.fetched_matches:
                self._watermark.fetched_matches.add(mid)
                self._enqueue_chain_detail(mid, ev.shard, start_ms)

    async def _on_match_detail_event(self, ev: MatchDetailEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.match_details:
            return
        self._harvest_players(ev.match_details)
        self._fanout_comp_updates(ev.match_details)
        self._prune_sets()

    def _enqueue_chain_detail(
        self, match_id: str, shard: str, game_start_millis: int | None = None,
    ) -> None:
        """enqueue a match-detail fetch on a foreign shard at dig priority"""
        self._scheduler.enqueue_match_details(
            lambda mid=match_id, sh=shard, gs=game_start_millis: self._fetch_chain_detail(mid, sh, gs),
            _COLLECTOR_PRIORITY,
            f"chain detail {match_id[:8]}",
        )

    async def _fetch_chain_detail(
        self, match_id: str, shard: str, game_start_millis: int | None = None,
    ) -> None:
        if not self._running:
            return
        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=shard,
            )
        except Exception:
            logger.exception(f"Chain: detail {match_id[:8]} raised")
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=shard, match_id=match_id, riot_status=0, match_details=None,
                game_start_millis=game_start_millis,
            ))
            return

        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=shard, match_id=match_id, riot_status=status, match_details=payload,
            game_start_millis=game_start_millis,
        ))

    @property
    def puuid(self) -> str:
        return self._session.puuid

    @property
    def _shard(self) -> str:
        return self._session.region.pd_shard

    @property
    def _account(self) -> AccountProgress:
        if self.puuid not in self._watermark.accounts:
            self._watermark.accounts[self.puuid] = AccountProgress()
        return self._watermark.accounts[self.puuid]

    # --------- Public Interface ---------

    def start(self) -> None:
        """begin match collection"""
        if self._running:
            return
        self._running = True
        self._phase = "fresh"
        logger.info(
            f"Match collector started "  # pyright: ignore[reportImplicitStringConcatenation]
            f"(central_matches={len(self._watermark.fetched_matches)}, "
            f"unvisited={len(self._unvisited_players)}, "
            f"visited_players={len(self._watermark.dig_visited)}, "
            f"visited_matches={len(self._watermark.dig_visited_matches)})"
        )
        self._comp_update_worker = asyncio.create_task(self._run_comp_update_worker())
        self._fresh_task = asyncio.create_task(self._run_fresh())

    def stop(self) -> None:
        """stop collection and persist progress"""
        if not self._running:
            return
        self._running = False
        if self._fresh_task and not self._fresh_task.done():
            _ = self._fresh_task.cancel()
        if self._comp_update_worker and not self._comp_update_worker.done():
            _ = self._comp_update_worker.cancel()
            self._comp_update_worker = None
        self._save_watermark()
        logger.info("Match collector stopped")

    # --------- Phase 1: Fresh ---------

    async def _run_fresh(self) -> None:
        """assemble the entire history for this account and fan out details"""
        if not self._running:
            return

        try:
            event = await self._assembler.assemble(
                self.puuid, self._shard, priority=_COLLECTOR_PRIORITY,
            )
        except Exception:
            logger.exception("Fresh: assembler raised; transitioning to dig")
            self._transition_to_dig()
            return

        if event.riot_status != 200 or not event.match_history:
            logger.warning(
                f"Fresh: history assembly failed with status {event.riot_status}; "  # pyright: ignore[reportImplicitStringConcatenation]
                "transitioning to dig"
            )
            self._transition_to_dig()
            return

        history_entries: list[dict[str, Any]] = event.match_history.get("History") or []   # pyright: ignore[reportExplicitAny]

        if not history_entries:
            logger.info(
                "Fresh: account has no match history, falling back to leaderboard to seed dig"
            )
            try:
                match_ids = await self._fallback_leaderboard()
            except LeaderboardFallbackError as e:
                logger.warning(f"Leaderboard fallback failed: {e.message}")
                match_ids = []

            for mid in match_ids:
                if mid not in self._watermark.fetched_matches:
                    self._watermark.fetched_matches.add(mid)
                    self._enqueue_self_detail(mid)
            self._save_watermark()
            self._transition_to_dig()
            return

        new_count = 0
        for entry in history_entries:
            mid = entry.get("MatchID")
            if not isinstance(mid, str):
                continue
            if mid in self._watermark.fetched_matches:
                continue
            start = entry.get("GameStartTime")
            start_ms = start if isinstance(start, int) else None
            if _is_expired(start_ms):
                continue
            self._watermark.fetched_matches.add(mid)
            self._enqueue_self_detail(mid, start_ms)
            new_count += 1

        logger.info(
            f"Fresh: assembled {len(history_entries)} entries, enqueued {new_count} new detail(s)"
        )
        self._save_watermark()
        self._transition_to_dig()

    def _enqueue_self_detail(
        self, match_id: str, game_start_millis: int | None = None,
    ) -> None:
        self._scheduler.enqueue_match_details(
            lambda mid=match_id, gs=game_start_millis: self._fetch_self_detail(mid, gs),
            _COLLECTOR_PRIORITY,
            f"fresh detail {match_id[:8]}",
        )

    async def _fetch_self_detail(
        self, match_id: str, game_start_millis: int | None = None,
    ) -> None:
        """fetch a fresh-phase match detail, emit event, harvest players"""
        if not self._running:
            return

        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=self._shard,
            )
        except Exception:
            logger.exception(f"Fresh: detail {match_id[:8]} raised")
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=self._shard, match_id=match_id, riot_status=0, match_details=None,
                game_start_millis=game_start_millis,
            ))
            return

        # harvest + comp-update fanout happen via the MATCH_DETAIL_FETCHED bus subscriber
        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=self._shard, match_id=match_id, riot_status=status, match_details=payload,
            game_start_millis=game_start_millis,
        ))

    # --------- Phase 2: Dig (randomized DFS) ---------

    def _transition_to_dig(self) -> None:
        if not self._running:
            return
        self._phase = "dig"
        logger.info(
            f"Starting dig phase ({len(self._unvisited_players)} unvisited players, "  # pyright: ignore[reportImplicitStringConcatenation]
            f"{len(self._watermark.dig_visited)} already visited)"
        )
        self._dig_walk_start()

    def _dig_walk_start(self) -> None:
        """pick a random unvisited player and begin a new DFS walk"""
        if not self._running:
            return

        self._unvisited_players -= self._watermark.dig_visited

        if not self._unvisited_players:
            self._phase = "done"
            self._save_watermark()
            logger.info(
                "Dig phase complete: no unvisited players remain "  # pyright: ignore[reportImplicitStringConcatenation]
                "(mathematically highly improbable)"
            )
            return

        target: str = random.choice(list(self._unvisited_players))
        self._unvisited_players.discard(target)
        self._watermark.dig_visited.add(target)

        logger.debug(f"DFS walk starting from root {target[:8]}")
        _ = asyncio.create_task(self._run_dig_history(target))

    async def _run_dig_history(self, target_puuid: str) -> None:
        """assemble a player's full history, then walk their unvisited matches"""
        if not self._running:
            return

        try:
            event = await self._assembler.assemble(
                target_puuid, self._shard, priority=_COLLECTOR_PRIORITY,
            )
        except Exception:
            logger.exception(f"Dig: assembler raised for {target_puuid[:8]}")
            self._dig_walk_start()
            return

        if event.riot_status != 200 or not event.match_history:
            logger.debug(
                f"Dig: history assembly failed for {target_puuid[:8]} "  # pyright: ignore[reportImplicitStringConcatenation]
                f"(status={event.riot_status})"
            )
            self._dig_walk_start()
            return

        history_entries: list[dict[str, Any]] = event.match_history.get("History") or []  # pyright: ignore[reportExplicitAny]
        unvisited: list[tuple[str, int | None]] = []
        for entry in history_entries:
            mid = entry.get("MatchID")  
            start = entry.get("GameStartTime")
            start_ms = start if isinstance(start, int) else None
            if _is_expired(start_ms):
                continue
            if isinstance(mid, str) and mid not in self._watermark.dig_visited_matches:
                unvisited.append((mid, start_ms))

        if not unvisited:
            logger.debug(f"DFS dead end at player {target_puuid[:8]} (all matches visited)")
            self._dig_walk_start()
            return

        for mid, _start in unvisited:
            self._watermark.fetched_matches.add(mid)
            self._dig_detail_queue.append((mid, _start))

        logger.info(
            f"Dig: queued {len(unvisited)} match(es) from {target_puuid[:8]} "  # pyright: ignore[reportImplicitStringConcatenation]
            f"({len(self._dig_detail_queue)} pending)"
        )
        self._dig_drain_detail()

    def _dig_drain_detail(self) -> None:
        if not self._running:
            return

        if self._dig_detail_queue:
            match_id, game_start_millis = self._dig_detail_queue.popleft()
            self._scheduler.enqueue_match_details(
                lambda mid=match_id, gs=game_start_millis: self._fetch_dig_detail(mid, gs),
                _COLLECTOR_PRIORITY,
                f"dig detail {match_id[:8]}",
            )
        else:
            self._dig_walk_start()

    async def _fetch_dig_detail(
        self, match_id: str, game_start_millis: int | None = None,
    ) -> None:
        """fetch a dig-phase match detail, harvest players, then drain or walk"""
        if not self._running:
            return

        payload: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        status: int = 0
        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=self._shard,
            )
        except Exception:
            logger.exception(f"Dig: detail {match_id[:8]} raised")

        # harvest + comp-update fanout happen via the MATCH_DETAIL_FETCHED bus subscriber
        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=self._shard, match_id=match_id, riot_status=status, match_details=payload,
            game_start_millis=game_start_millis,
        ))

        if status == 200 and payload is not None:
            self._watermark.dig_visited_matches.add(match_id)

        # Drain remaining queued details first
        if self._dig_detail_queue:
            self._dig_drain_detail()
            return

        # 15% teleportation chance
        if random.random() < DIG_RESTART_CHANCE:
            logger.debug(f"DFS teleport after match {match_id[:8]}")
            self._dig_walk_start()
            return

        # Pick a random unvisited player from this match to go deeper
        if payload is None:
            self._dig_walk_start()
            return

        players: list[dict[str, Any]] = payload.get("players", []) or []  # pyright: ignore[reportExplicitAny]
        candidates: list[str] = []
        for p in players:
            puuid = p.get("subject", "")  # pyright: ignore[reportAny]
            if (isinstance(puuid, str) and puuid
                    and puuid != self.puuid
                    and puuid not in self._watermark.dig_visited):
                candidates.append(puuid)

        if not candidates:
            logger.debug(f"DFS dead end at match {match_id[:8]} (all players visited)")
            self._dig_walk_start()
            return

        next_player: str = random.choice(candidates)
        self._unvisited_players.discard(next_player)
        self._watermark.dig_visited.add(next_player)
        logger.debug(f"DFS continuing: {match_id[:8]} => player {next_player[:8]}")
        _ = asyncio.create_task(self._run_dig_history(next_player))

    # --------- Player harvesting ---------

    def _harvest_players(self, match_response: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """add unvisited PUUIDs from a match-details payload to the dig pool"""
        if len(self._unvisited_players) >= MAX_UNVISITED_PLAYERS:
            return

        players: list[dict[str, Any]] = match_response.get("players", []) or []    # pyright: ignore[reportExplicitAny]
        for player in players:
            puuid = player.get("subject", "")  # pyright: ignore[reportAny]
            if (isinstance(puuid, str) and puuid
                    and puuid != self.puuid
                    and puuid not in self._watermark.dig_visited
                    and puuid not in self._unvisited_players):
                self._unvisited_players.add(puuid)

    def _fanout_comp_updates(self, match_response: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """enqueue a competitive-update assembly per player in this match"""
        players: list[dict[str, Any]] = match_response.get("players", []) or []    # pyright: ignore[reportExplicitAny]
        for player in players:
            puuid = player.get("subject", "")  # pyright: ignore[reportAny]
            if isinstance(puuid, str) and puuid:
                self._enqueue_competitive_update(puuid)

    def _enqueue_competitive_update(self, puuid: str) -> None:
        """enqueue a competitive update assembly at "dig" priority; deduped per-session to avoid 20+ redundant paginated
        fetches; processed by a single worker to prevent coroutine pile-up on the assembler lock"""
        if puuid in self._comp_update_seen:
            return
        self._comp_update_seen.add(puuid)
        _cap_set(self._comp_update_seen, _COMP_UPDATE_SEEN_CAP)
        try:
            self._comp_update_queue.put_nowait(puuid)
        except asyncio.QueueFull:
            pass

    async def _run_comp_update_worker(self) -> None:
        """single worker that drains the comp-update queue sequentially"""
        while self._running:
            try:
                puuid = await asyncio.wait_for(self._comp_update_queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            try:
                _ = await self._comp_update_assembler.assemble(
                    puuid, self._shard, priority=_COLLECTOR_PRIORITY,
                )
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception(f"Comp-update worker: assembly for {puuid[:8]} raised")

    def _prune_sets(self) -> None:
        """cap all dedup sets to prevent unbounded memory growth"""
        _cap_set(self._watermark.fetched_matches, _FETCHED_MATCHES_CAP)
        _cap_set(self._watermark.dig_visited, _DIG_VISITED_CAP)
        _cap_set(self._watermark.dig_visited_matches, _DIG_VISITED_MATCHES_CAP)
        _cap_set(self._comp_update_seen, _COMP_UPDATE_SEEN_CAP)

    # --------- Watermark Persistence ---------

    def _load_watermark(self) -> None:
        """load central collection state from disk; unknown keys are ignored"""
        try:
            if not self._watermark_path.exists():
                return

            raw: dict[str, Any] = json.loads(  # pyright: ignore[reportExplicitAny, reportAny]
                self._watermark_path.read_text(encoding="utf-8")
            )

            self._watermark.fetched_matches = set(raw.get("fetched_matches", []))  # pyright: ignore[reportAny]
            self._watermark.dig_visited = set(raw.get("dig_visited", []))  # pyright: ignore[reportAny]
            self._watermark.dig_visited_matches = set(raw.get("dig_visited_matches", []))  # pyright: ignore[reportAny]

            dig_unvisited: list[str] = raw.get("dig_queue", [])  # pyright: ignore[reportAny]
            self._unvisited_players = set(dig_unvisited) - self._watermark.dig_visited

            # per-account map kept for forward-compat; AccountProgress is empty, unknown keys ignored
            accounts_raw: dict[str, Any] = raw.get("accounts", {})  # pyright: ignore[reportExplicitAny, reportAny]
            for puuid_key, _acct_data in accounts_raw.items():  # pyright: ignore[reportAny]
                self._watermark.accounts[puuid_key] = AccountProgress()

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load watermark, starting fresh: {e}")

    def _save_watermark(self) -> None:
        try:
            unvisited_snapshot: list[str] = sorted(self._unvisited_players)[:500]

            data: dict[str, object] = {
                "accounts": {puuid: {} for puuid in self._watermark.accounts},
                "fetched_matches": sorted(self._watermark.fetched_matches),
                "dig_queue": unvisited_snapshot,
                "dig_visited": sorted(self._watermark.dig_visited),
                "dig_visited_matches": sorted(self._watermark.dig_visited_matches),
            }

            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path = self._watermark_path.with_suffix(".tmp")
            _ = tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _ = tmp_path.replace(self._watermark_path)

            logger.debug(
                f"Watermark saved ({len(self._watermark.fetched_matches)} matches, "  # pyright: ignore[reportImplicitStringConcatenation]
                f"{len(self._watermark.dig_visited)} visited players, "
                f"{len(self._watermark.dig_visited_matches)} visited match vertices)"
            )
        except OSError as e:
            logger.warning(f"Failed to save watermark: {e}")

    # --------- Leaderboard fallback ---------

    async def _fallback_leaderboard(self) -> list[str]:
        """seed dig from top-500 leaderboard when account has no history; tries random players until one has matches;
        raises LeaderboardFallbackError if all 500 are exhausted"""
        leaderboard: LeaderboardResponse = await self._session.general_get_leaderboard(
            start_index=0, size=510,
        )
        _ = await self._bus.emit(Event.LEADERBOARD_FETCHED, leaderboard)
        players = [
            p for p in (leaderboard.Players or []) 
            if hasattr(p, "puuid") and p.puuid  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
        ]

        if not players:
            raise LeaderboardFallbackError(message="Leaderboard returned no players")

        # seed the unvisited pool with all leaderboard players
        for p in players:
            p_puuid: str = p.puuid  # pyright: ignore[reportUnknownMemberType, reportAssignmentType, reportUnknownVariableType, reportAttributeAccessIssue]
            if (p_puuid and p_puuid != self.puuid
                    and p_puuid not in self._watermark.dig_visited):
                self._unvisited_players.add(p_puuid)
        logger.info(
            f"Fallback: seeded {len(self._unvisited_players)} leaderboard players into pool"
        )

        random.shuffle(players)

        for player in players:
            puuid: str = player.puuid  # pyright: ignore[reportUnknownMemberType, reportAssignmentType, reportUnknownVariableType, reportAttributeAccessIssue]
            logger.debug(f"Fallback: trying leaderboard player {puuid[:8]}")

            try:
                event = await self._assembler.assemble(
                    puuid, self._shard, priority=_COLLECTOR_PRIORITY,
                )
            except Exception as e:
                logger.debug(f"Fallback: assembly raised for {puuid[:8]}: {e}")
                continue

            if event.riot_status != 200 or not event.match_history:
                continue

            history_entries: list[dict[str, Any]] = event.match_history.get("History") or []  # pyright: ignore[reportExplicitAny]
            match_ids: list[str] = []
            for entry in history_entries:
                mid = entry.get("MatchID") 
                if isinstance(mid, str):
                    match_ids.append(mid)

            if match_ids:
                logger.info(
                    f"Fallback: found {len(match_ids)} match(es) from leaderboard player "  # pyright: ignore[reportImplicitStringConcatenation]
                    f"{puuid[:8]}"
                )
                return match_ids

        raise LeaderboardFallbackError(
            message="No leaderboard player had accessible match history"
        )
