"""
Two-phase match data collection plan:

Phase 1 (Fresh):  on every app launch, assemble the player's *entire*
                  current match history via HistoryAssembler (emits
                  MATCH_HISTORY_FETCHED once for the full payload),
                  then fan out match-details for every match not yet
                  in the central dedupe set. Each detail emits
                  MATCH_DETAIL_FETCHED and harvests players into the
                  unvisited pool (for dig) and into the competitive-
                  updates pipeline

Phase 2 (Dig):    once every locally-known match is collected we explore
                  other-player matches via randomized DFS on the
                  match-player graph. Each visited player triggers a
                  FULL history assembly (also emitted via
                  MATCH_HISTORY_FETCHED), then matches are walked.

All collector requests are enqueued at priority "dig" so server-issued
tasks always win the scheduler. The collector is a gap filler.

State is split into two scopes:

- Per-account watermark: not currently needed. fresh re-pulls the
  entire history every launch so the backend always sees the latest
  state. (Old fields like newest_known_time/oldest_fetched_time were
  removed; their JSON keys, if present in older watermark files, are
  ignored on load.)
- Central: fetched_matches (detail dedupe) and dig_visited/
  dig_visited_matches (DFS state), shared across all accounts so a
  detail is never fetched twice.

The dig phase uses a randomized DFS inspired by google's random surfer:

    - Visited matches and players are tracked. Hitting a visited
      node is a dead end that triggers a restart from a new random root
    - Players are selected randomly from each match (no fixed index)
    - A 15% teleportation chance at each step abandons the current walk
      and restarts from a new random unvisited player to cover as many
      graphs as possible

Pretty cool thing if you ask me :P
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
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
_COLLECTOR_PRIORITY = "dig"  # All collector requests yield to "task"


class MatchCollector:
    """Progressively collects match history and details across app sessions.

    Two-phase pipeline:

    - **Fresh** runs once per launch: assemble the player's full history,
      fetch details for every match not in the central dedupe set, and
      harvest players for dig + competitive updates.
    - **Dig** runs forever after that: randomized DFS over the
      match-player graph, assembling each visited player's full history
      and walking matches.

    Every paced request enqueued by the collector uses priority "dig"
    so server tasks always pre-empt collector work.
    """

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
        self._dig_detail_queue: deque[str] = deque()

        # In-session dedup for competitive-update assemblies (per-puuid).
        self._comp_update_seen: set[str] = set()

        self._load_watermark()
        self._register_chain_subscribers()

    def _register_chain_subscribers(self) -> None:
        """Wire up the cross-collector chain.

        Every fetched history/comp-update/detail event (regardless of
        source — server task, fresh, dig, or chain) is fanned out into
        the other two queues so all three rate-limited workers stay
        saturated:

          - history(puuid) -> comp-update(puuid) + details(History[].MatchID)
          - comp-update(puuid) -> details(Matches[].MatchID)
          - details(match) -> harvest players + comp-update per player

        Dedup against fetched_matches / _comp_update_seen prevents the
        chain from looping or duplicating work.
        """
        _ = self._bus.on(Event.MATCH_HISTORY_FETCHED, self._on_history_event, priority=5)
        _ = self._bus.on(
            Event.COMPETITIVE_UPDATE_FETCHED, self._on_competitive_update_event, priority=5,
        )
        _ = self._bus.on(Event.MATCH_DETAIL_FETCHED, self._on_match_detail_event, priority=5)

    # --------- Cross-collector chain handlers ---------

    async def _on_history_event(self, ev: MatchHistoryEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.match_history:
            return
        # Same puuid -> comp-update assembly.
        self._enqueue_competitive_update(ev.puuid)
        # History entries -> match-detail fetches.
        entries: list[Any] = ev.match_history.get("History") or []  # pyright: ignore[reportAny]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("MatchID")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(mid, str) and mid and mid not in self._watermark.fetched_matches:
                self._watermark.fetched_matches.add(mid)
                self._enqueue_chain_detail(mid, ev.shard)

    async def _on_competitive_update_event(self, ev: CompetitiveUpdateEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.competitive_updates:
            return
        matches: list[Any] = ev.competitive_updates.get("Matches") or []  # pyright: ignore[reportAny]
        for entry in matches:
            if not isinstance(entry, dict):
                continue
            mid = entry.get("MatchID")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(mid, str) and mid and mid not in self._watermark.fetched_matches:
                self._watermark.fetched_matches.add(mid)
                self._enqueue_chain_detail(mid, ev.shard)

    async def _on_match_detail_event(self, ev: MatchDetailEvent) -> None:
        if not self._running or ev.riot_status != 200 or not ev.match_details:
            return
        self._harvest_players(ev.match_details)
        self._fanout_comp_updates(ev.match_details)

    def _enqueue_chain_detail(self, match_id: str, shard: str) -> None:
        """Enqueue a match-detail fetch on a foreign shard at dig priority."""
        self._scheduler.enqueue_match_details(
            lambda mid=match_id, sh=shard: self._fetch_chain_detail(mid, sh),
            _COLLECTOR_PRIORITY,
            f"chain detail {match_id[:8]}",
        )

    async def _fetch_chain_detail(self, match_id: str, shard: str) -> None:
        if not self._running:
            return
        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=shard,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"Chain: detail {match_id[:8]} raised")
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=shard, match_id=match_id, riot_status=0, match_details=None,
            ))
            return

        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=shard, match_id=match_id, riot_status=status, match_details=payload,
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
        """Begin match collection."""
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
        self._fresh_task = asyncio.create_task(self._run_fresh())

    def stop(self) -> None:
        """Stop collection and persist progress."""
        if not self._running:
            return
        self._running = False
        if self._fresh_task and not self._fresh_task.done():
            _ = self._fresh_task.cancel()
        self._save_watermark()
        logger.info("Match collector stopped")

    # --------- Phase 1: Fresh ---------

    async def _run_fresh(self) -> None:
        """Assemble the entire history for this account and fan out details."""
        if not self._running:
            return

        try:
            event = await self._assembler.assemble(
                self.puuid, self._shard, priority=_COLLECTOR_PRIORITY,
            )
        except Exception:  # noqa: BLE001
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
            self._watermark.fetched_matches.add(mid)
            self._enqueue_self_detail(mid)
            new_count += 1

        logger.info(
            f"Fresh: assembled {len(history_entries)} entries, enqueued {new_count} new detail(s)"
        )
        self._save_watermark()
        self._transition_to_dig()

    def _enqueue_self_detail(self, match_id: str) -> None:
        self._scheduler.enqueue_match_details(
            lambda mid=match_id: self._fetch_self_detail(mid),
            _COLLECTOR_PRIORITY,
            f"fresh detail {match_id[:8]}",
        )

    async def _fetch_self_detail(self, match_id: str) -> None:
        """Fetch a fresh-phase match detail, emit event, harvest players."""
        if not self._running:
            return

        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=self._shard,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"Fresh: detail {match_id[:8]} raised")
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
                shard=self._shard, match_id=match_id, riot_status=0, match_details=None,
            ))
            return

        # Harvest + comp-update fanout happen via the MATCH_DETAIL_FETCHED
        # bus subscriber (so server-task details also fan out).
        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=self._shard, match_id=match_id, riot_status=status, match_details=payload,
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
        """Pick a random unvisited player and begin a new DFS walk."""
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
        """Assemble a player's full history, then walk their unvisited matches."""
        if not self._running:
            return

        try:
            event = await self._assembler.assemble(
                target_puuid, self._shard, priority=_COLLECTOR_PRIORITY,
            )
        except Exception:  # noqa: BLE001
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

        history_entries: list[dict[str, Any]] = event.match_history.get("History") or []  # pyright: ignore[reportAny]
        unvisited: list[str] = []
        for entry in history_entries:
            mid = entry.get("MatchID")  # pyright: ignore[reportAny]
            if isinstance(mid, str) and mid not in self._watermark.dig_visited_matches:
                unvisited.append(mid)

        if not unvisited:
            logger.debug(f"DFS dead end at player {target_puuid[:8]} (all matches visited)")
            self._dig_walk_start()
            return

        for mid in unvisited:
            self._watermark.fetched_matches.add(mid)
            self._dig_detail_queue.append(mid)

        logger.info(
            f"Dig: queued {len(unvisited)} match(es) from {target_puuid[:8]} "  # pyright: ignore[reportImplicitStringConcatenation]
            f"({len(self._dig_detail_queue)} pending)"
        )
        self._dig_drain_detail()

    def _dig_drain_detail(self) -> None:
        if not self._running:
            return

        if self._dig_detail_queue:
            match_id: str = self._dig_detail_queue.popleft()
            self._scheduler.enqueue_match_details(
                lambda mid=match_id: self._fetch_dig_detail(mid),
                _COLLECTOR_PRIORITY,
                f"dig detail {match_id[:8]}",
            )
        else:
            self._dig_walk_start()

    async def _fetch_dig_detail(self, match_id: str) -> None:
        """Fetch a dig-phase match detail, harvest players, then drain or walk."""
        if not self._running:
            return

        payload: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        status: int = 0
        try:
            payload, status = await self._session.general_get_details_raw(
                match_id, shard=self._shard,
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"Dig: detail {match_id[:8]} raised")

        # Harvest + comp-update fanout happen via the MATCH_DETAIL_FETCHED
        # bus subscriber.
        _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, MatchDetailEvent(
            shard=self._shard, match_id=match_id, riot_status=status, match_details=payload,
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
        """Add unvisited PUUIDs from a match-details payload to the dig pool."""
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
        """Enqueue a competitive-update assembly per player in this match."""
        players: list[dict[str, Any]] = match_response.get("players", []) or []    # pyright: ignore[reportExplicitAny]
        for player in players:
            puuid = player.get("subject", "")  # pyright: ignore[reportAny]
            if isinstance(puuid, str) and puuid:
                self._enqueue_competitive_update(puuid)

    def _enqueue_competitive_update(self, puuid: str) -> None:
        """Kick off a CompetitiveUpdateAssembler at "dig" priority.

        Deduped per-session: re-fetching the same player's comp updates
        wastes 20+ paginated calls for unchanged data, and once we've
        already reported a status to the backend for this puuid the
        item is no longer claimable by us anyway.
        """
        if puuid in self._comp_update_seen:
            return
        self._comp_update_seen.add(puuid)
        _ = asyncio.create_task(
            self._comp_update_assembler.assemble(
                puuid, self._shard, priority=_COLLECTOR_PRIORITY,
            )
        )

    # --------- Watermark Persistence ---------

    def _load_watermark(self) -> None:
        """Load central collection state from disk. Unknown keys are ignored."""
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

            # Per-account map: kept for forward-compat even though
            # AccountProgress is currently empty. Any unknown keys in older
            # files are simply ignored.
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
        """Seed the dig phase from top-500 leaderboard players when the account
        has no match history.

        Picks a random player from the leaderboard, fetches their match history,
        and returns the match IDs. If that player has no history, tries the next
        random player until one works or all 500 are exhausted.

        Raises:
            LeaderboardFallbackError: If no leaderboard player yields any history.
        """
        leaderboard: LeaderboardResponse = await self._session.general_get_leaderboard(
            start_index=0, size=510,
        )
        _ = await self._bus.emit(Event.LEADERBOARD_FETCHED, leaderboard)
        players = [
            p for p in (leaderboard.Players or [])  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            if hasattr(p, "puuid") and p.puuid
        ]

        if not players:
            raise LeaderboardFallbackError(message="Leaderboard returned no players")

        # Seed the unvisited pool with all leaderboard players
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
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Fallback: assembly raised for {puuid[:8]}: {e}")
                continue

            if event.riot_status != 200 or not event.match_history:
                continue

            history_entries: list[dict[str, Any]] = event.match_history.get("History") or []  # pyright: ignore[reportAny]
            match_ids: list[str] = []
            for entry in history_entries:
                mid = entry.get("MatchID")  # pyright: ignore[reportAny]
                if isinstance(mid, str):
                    match_ids.append(mid)

            if match_ids:
                logger.info(
                    f"Fallback: found {len(match_ids)} match(es) from leaderboard player "
                    f"{puuid[:8]}"
                )
                return match_ids

        raise LeaderboardFallbackError(
            message="No leaderboard player had accessible match history"
        )
