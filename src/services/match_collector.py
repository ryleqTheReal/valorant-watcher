"""
Three-phase match data collection plan:

Phase 1 (Fresh):   fetch newest matches since last session and stopping when
                   we reach the most recent known match timestamp
Phase 2 (Legacy):  continue fetching older matches from where the previous
                   session left off to fill in historical data.
Phase 3 (Dig):     when all of the user's own matches are collected we explore
                   other player matches using randomized DFS on the
                   match-player graph

all API calls are enqueued into the RequestScheduler general queue so
they respect rate limiting and yield to higher-priority state requests

Each completed request enqueues the next one creating a self-sustaining
chain that pauses/resumes with the scheduler

State is split into two scopes:

Per-account: fresh/legacy progress: each PUUID tracks its own
  known-window boundaries
Central: fetched matches, dig graph: shared across all accounts
  so that match details are never fetched twice

The dig phase uses a randomized DFS inspired by google's random surfer algo:

    - Visited matches and players are tracked. Hitting a visited
      node is a dead end that triggers a restart from a new random root
    - Players are selected randomly from each match (no fixed index) => Not Match.Players[0] instead Match.Players[random]
    - A 15% teleportation chance at each step abandons the current walk and
      restarts from a new random unvisited player to cover as many graphs as possible
      
Pretty cool thing if you ask me :P
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from pathlib import Path
from typing import Any

from services.auth_service import RiotSession
from services.event_bus import EventBus, Event
from services.request_scheduler import RequestScheduler
from utils.exceptions import IncorrectPaginationError, LeaderboardFallbackError
from utils.models import AccountProgress, LeaderboardResponse, MatchHistoryEntry, MatchHistoryResponse, MatchWatermark

logger: logging.Logger = logging.getLogger(__name__)

PAGE_SIZE: int = 20
DIG_RESTART_CHANCE: float = 0.15
MAX_UNVISITED_PLAYERS: int = 5000


class MatchCollector:
    """Progressively collects match history and details across different app sessions

    Uses a watermark file to track progress for ever PUUID on the device so collection survives app
    restarts 
    
    deduplicates using a centrally persisted set of match IDs

    The collector is self-scheduling: it enqueues one request at a time
    into the general queue. 
    When that request completes, its callback enqueues the next one. 
    This naturally interleaves with other general
    requests (loadout, XP, ...) and pauses during state changes
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

        # Match IDs waiting for detail fetch (used by fresh/legacy only)
        self._detail_queue: deque[str] = deque()

        # Collection state
        self._phase: str = "fresh"      # fresh | legacy | dig | done => Done is technically mathematically impossible but for the 0.000000001% chance, why not include it
        self._page_index: int = 0
        self._running: bool = False
        self._first_fresh_time: int = 0  # Track newest GameStartTime this session

        # central state, loaded from watermark
        self._watermark: MatchWatermark = MatchWatermark()

        # Dig phase: unvisited player pool and pending detail queue
        self._unvisited_players: set[str] = set()
        self._dig_detail_queue: deque[str] = deque()

        self._load_watermark()

    @property
    def puuid(self) -> str:
        return self._session.puuid

    @property
    def _account(self) -> AccountProgress:
        """Get or create the per-account progress entry"""
        
        if self.puuid not in self._watermark.accounts:
            self._watermark.accounts[self.puuid] = AccountProgress()
        return self._watermark.accounts[self.puuid]

    # --------- Public Interface ---------

    def start(self) -> None:
        """Begin match collection by enqueueing the first operation"""
        
        if self._running:
            return
        self._running = True
        self._phase = "fresh"
        self._page_index = 0
        acct = self._account
        newest_known = acct.newest_known_time
        oldest_fetched = acct.oldest_fetched_time
        legacy_complete = acct.legacy_complete
        central_matches = len(self._watermark.fetched_matches)
        unvisited = len(self._unvisited_players)
        visited_players = len(self._watermark.dig_visited)
        visited_matches = len(self._watermark.dig_visited_matches)
        logger.info(f"Match collector started ({newest_known=}, {oldest_fetched=}, {legacy_complete=}, {central_matches=}, {unvisited=}, {visited_players=}, {visited_matches=})")
        self._enqueue_next()

    def stop(self) -> None:
        """Stop collection and persist progress."""
        if not self._running:
            return
        self._running = False
        self._save_watermark()
        logger.info("Match collector stopped")

    # --------- Core Dispatch ---------

    def _enqueue_next(self) -> None:
        """Enqueue the next operation based on current collector state.

        Priority order:
        1. Drain the detail queue (fresh/legacy details before more pages)
        2. Continue the current phase (fresh => legacy => dig)

        The dig phase has its own scheduling chain and does NOT use the
        detail queue, it chains directly: history => detail =>  history => ...
        """
        if not self._running:
            return

        # Drain detail queue for fresh/legacy phases
        if self._phase in ("fresh", "legacy") and self._detail_queue:
            match_id: str = self._detail_queue.popleft()
            self._scheduler.enqueue_general(
                lambda mid=match_id: self._fetch_detail(mid),
                f"detail {match_id[:8]}",
            )
            return

        match self._phase:
            case "fresh":
                self._scheduler.enqueue_general(
                    self._fetch_fresh_page,
                    f"fresh page {self._page_index // PAGE_SIZE}",
                )
            case "legacy":
                if self._account.legacy_complete:
                    self._transition_to_dig()
                else:
                    self._scheduler.enqueue_general(
                        self._fetch_legacy_page,
                        f"legacy page {self._page_index // PAGE_SIZE}",
                    )
            case "dig":
                self._dig_walk_start()
            case "done":
                pass
            case _:
                pass

    # --------- Phase 1: Fresh ---------

    async def _fetch_fresh_page(self) -> None:
        """Fetch one page of match history, collecting new matche"""
        
        if not self._running:
            return

        logger.debug(f"Fresh: requesting startIndex={self._page_index}, endIndex={self._page_index + PAGE_SIZE}")

        try:
            response: MatchHistoryResponse = await self._session.general_get_history(
                puuid=self.puuid,
                start_index=self._page_index,
                end_index=self._page_index + PAGE_SIZE,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch fresh page {self._page_index}: {e}")
            # Don't retry the same page and treat as end of history
            self._phase = "legacy"
            self._enqueue_next()
            return
        
        if response.Total == 0:
            logger.info("Account has no match history, falling back to leaderboard to seed dig phase")
            try:
                match_ids = await self._fallback_leaderboard()
                for mid in match_ids:
                    self._watermark.fetched_matches.add(mid)
                    self._detail_queue.append(mid)
            except LeaderboardFallbackError as e:
                logger.warning(f"Leaderboard fallback failed: {e.message}")

            self._account.legacy_complete = True
            self._save_watermark()
            self._transition_to_dig()
            return

        logger.debug(f"Fresh: response Total={response.Total}, BeginIndex={response.BeginIndex}, EndIndex={response.EndIndex}, History length={len(response.History or [])}")

        history: list[MatchHistoryEntry] = [
            e for e in (response.History or []) if isinstance(e, MatchHistoryEntry)
        ]

        acct = self._account
        hit_known: bool = False
        pre_count: int = len(self._watermark.fetched_matches)

        for entry in history:
            # Track the absolute newest time seen this session
            if entry.GameStartTime > self._first_fresh_time:
                self._first_fresh_time = entry.GameStartTime

            # Have we reached the known window?
            if (acct.newest_known_time > 0
                    and entry.GameStartTime <= acct.newest_known_time):
                hit_known = True
                break

            if entry.MatchID not in self._watermark.fetched_matches:
                self._watermark.fetched_matches.add(entry.MatchID)
                self._detail_queue.append(entry.MatchID)

        self._page_index += PAGE_SIZE

        if hit_known or len(history) < PAGE_SIZE or self._page_index >= response.Total:
            # Fresh phase complete
            if self._first_fresh_time > 0:
                acct.newest_known_time = self._first_fresh_time

            # First-ever session: set oldest_fetched_time from the oldest
            # match we just discovered
            if acct.oldest_fetched_time == 0 and history:
                oldest_in_batch: int = min(
                    (e.GameStartTime for e in history
                     if isinstance(e, MatchHistoryEntry) and e.MatchID in self._watermark.fetched_matches),  # pyright: ignore[reportUnnecessaryIsInstance]
                    default=0,
                )
                if oldest_in_batch > 0:
                    acct.oldest_fetched_time = oldest_in_batch

            new_count = len(self._watermark.fetched_matches) - pre_count
            logger.info(
                f"Fresh phase complete: {new_count} new match(es), page_index={self._page_index}"
            )

            # If we've already covered all matches, skip legacy entirely
            if response.Total == 0 or self._page_index >= response.Total:
                acct.legacy_complete = True
                self._save_watermark()
                logger.info("No legacy matches to fetch (all history covered by fresh phase)")

            self._phase = "legacy"
            # page_index continues from where fresh left off so we can
            # skip through the known window into legacy territory

        self._enqueue_next()

    # --------- Phase 2: Legacy ---------

    async def _fetch_legacy_page(self) -> None:
        """Fetch one page of legacy match history, skipping known matches"""
        if not self._running:
            return

        logger.debug(f"Legacy: requesting startIndex={self._page_index}, endIndex={self._page_index + PAGE_SIZE}")

        try:
            response: MatchHistoryResponse = await self._session.general_get_history(
                puuid=self.puuid,
                start_index=self._page_index,
                end_index=self._page_index + PAGE_SIZE,
            )
        except IncorrectPaginationError:
            logger.warning("Reached end of legacy pages")
            self._account.legacy_complete = True
            self._save_watermark()
            self._transition_to_dig()
            return
        except Exception as e:
            logger.warning(f"Failed to fetch legacy page {self._page_index}: {e}")
            logger.info("Legacy phase stopped due to unexpected error, transitioning to dig to avoid gaps")
            self._transition_to_dig()
            return

        logger.debug(f"Legacy: response Total={response.Total}, BeginIndex={response.BeginIndex}, EndIndex={response.EndIndex}, History length={len(response.History or [])}")

        history: list[MatchHistoryEntry] = [
            e for e in (response.History or []) if isinstance(e, MatchHistoryEntry)
        ]

        acct = self._account

        for entry in history:
            if entry.MatchID in self._watermark.fetched_matches:
                continue

            # Skip matches inside the already-known window
            if (acct.oldest_fetched_time > 0
                    and entry.GameStartTime >= acct.oldest_fetched_time):
                self._watermark.fetched_matches.add(entry.MatchID)
                continue

            self._watermark.fetched_matches.add(entry.MatchID)
            self._detail_queue.append(entry.MatchID)

            # Extend the known window downward
            if (acct.oldest_fetched_time == 0
                    or entry.GameStartTime < acct.oldest_fetched_time):
                acct.oldest_fetched_time = entry.GameStartTime

        self._page_index += PAGE_SIZE

        if len(history) < PAGE_SIZE or self._page_index >= response.Total:
            acct.legacy_complete = True
            self._save_watermark()
            logger.info("Legacy phase complete: all historical matches discovered")

        self._enqueue_next()

    # --------- Detail Fetching (fresh/legacy only) ---------

    async def _fetch_detail(self, match_id: str) -> None:
        """Fetch full details for a single match and emit the event

        Used by fresh/legacy phases The dig phase has its own
        _dig_fetch_detail that handles DFS continuation logic
        """
        if not self._running:
            return

        try:
            response: dict[str, Any] = await self._session.general_get_details(match_id)  # pyright: ignore[reportExplicitAny]
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, response)

            # Harvest player PUUIDs into the unvisited pool for dig phase
            self._harvest_players(response)

        except Exception as e:
            logger.warning(f"Failed to fetch detail for match id {match_id[:8]}: {e}")

        self._enqueue_next()

    def _harvest_players(self, match_response: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        """Extract player PUUIDs from a match detail response and add
        unvisited ones to the dig pool"""

        if len(self._unvisited_players) >= MAX_UNVISITED_PLAYERS:
            return

        players: list[dict[str, Any]] = match_response.get("players", [])  # pyright: ignore[reportExplicitAny, reportAny]
        for player in players:
            puuid: str = player.get("subject", "")  # pyright: ignore[reportAny]
            if (puuid
                    and puuid != self.puuid
                    and puuid not in self._watermark.dig_visited
                    and puuid not in self._unvisited_players):
                self._unvisited_players.add(puuid)

    # ================================================================== #
    #  Phase 3: Randomized DFS on the match-player graph                  #
    #                                                                      #
    #  The graph has two node types: players and matches.                   #
    #  Edges connect players to their matches and matches to their         #
    #  participants. We walk this bipartite graph using three rules:       #
    #                                                                      #
    #  1. Visited tracking: hitting a visited match or player is a dead   #
    #     end that triggers a restart from a new random unvisited root.     #
    #  2. Random selection: players are chosen randomly from each match,  #
    #     not by a fixed index.                                            #
    #  3. Teleportation: 15% chance at each step to abandon the current   #
    #     walk and restart, preventing deep but narrow exploration.         #
    #                                                                      #
    #  The chain is: pick root => fetch history => pick random match =>       #
    #  fetch detail => pick random player => fetch history => ...             #
    #  Each step enqueues exactly one scheduler request.                    #
    # ================================================================== #

    def _transition_to_dig(self) -> None:
        """Switch from legacy to dig phase"""
        
        self._phase = "dig"
        logger.info(
            f"Starting randomized DFS dig phase ({len(self._unvisited_players)} unvisited players, {len(self._watermark.dig_visited)} already visited)"
        )
        self._dig_walk_start()

    def _dig_walk_start(self) -> None:
        """Pick a random unvisited player and begin a new DFS walk

        This is the 'teleportation' entry point which is called at the start of
        dig phase, after dead ends, and on the 15% random restart
        """
        
        if not self._running:
            return

        # Prune any players that were visited since they were added
        self._unvisited_players -= self._watermark.dig_visited

        if not self._unvisited_players:
            self._phase = "done"
            self._save_watermark()
            logger.info("Dig phase complete: no unvisited players remain which is mathematically highly improbable")
            return

        # pick a random root and mark visited
        target: str = random.choice(list(self._unvisited_players))
        self._unvisited_players.discard(target)
        self._watermark.dig_visited.add(target)

        logger.debug(f"DFS walk starting from root {target[:8]}")
        self._scheduler.enqueue_general(
            lambda p=target: self._dig_fetch_history(p),
            f"dig history {target[:8]}",
        )

    async def _dig_fetch_history(self, target_puuid: str) -> None:
        """Fetch a player's match history and pick one random unfetched match"""
        
        if not self._running:
            return

        try:
            response: MatchHistoryResponse = await self._session.general_get_history(
                puuid=target_puuid,
                start_index=0,
                end_index=PAGE_SIZE,
            )
        except Exception as e:
            logger.warning(f"Dig: failed to fetch history for {target_puuid[:8]}: {e}")
            self._dig_walk_start()
            return

        history: list[MatchHistoryEntry] = [
            e for e in (response.History or []) if isinstance(e, MatchHistoryEntry)
        ]

        # Queue matches not yet visited as DFS graph vertices
        unvisited: list[MatchHistoryEntry] = [
            e for e in history if e.MatchID not in self._watermark.dig_visited_matches
        ]

        if not unvisited:
            # Dead end => all matches already visited => restart
            logger.debug(f"DFS dead end at player {target_puuid[:8]} (all matches visited)")
            self._dig_walk_start()
            return

        for entry in unvisited:
            self._watermark.fetched_matches.add(entry.MatchID)
            self._dig_detail_queue.append(entry.MatchID)

        logger.info(f"Dig: queued {len(unvisited)} match(es) from player {target_puuid[:8]} ({len(self._dig_detail_queue)} pending)")

        # Start draining the dig detail queue
        self._dig_drain_detail()

    def _dig_drain_detail(self) -> None:
        """Pop the next match from the dig detail queue and enqueue it

        When the queue is empty, continue the DFS walk to a new player.
        """
        if not self._running:
            return

        if self._dig_detail_queue:
            match_id: str = self._dig_detail_queue.popleft()
            self._scheduler.enqueue_general(
                lambda mid=match_id: self._dig_fetch_detail(mid),
                f"dig detail {match_id[:8]}",
            )
        else:
            # all queued details drained continue the DFS walk
            self._dig_walk_start()

    async def _dig_fetch_detail(self, match_id: str) -> None:
        """Fetch match detail during DFS, harvest players, then drain next

        After fetching:
        1. harvest all players into the unvisited pool
        2. continue draining the dig detail queue
        3. when the queue is empty, roll DFS decision (teleport or walk deeper)
        """

        if not self._running:
            return

        try:
            response: dict[str, Any] = await self._session.general_get_details(match_id)  # pyright: ignore[reportExplicitAny]
            _ = await self._bus.emit(Event.MATCH_DETAIL_FETCHED, response)

            # Mark match as visited in the DFS graph
            self._watermark.dig_visited_matches.add(match_id)

            # Harvest ALL players into the global unvisited pool
            self._harvest_players(response)

        except Exception as e:
            logger.warning(f"Dig: failed to fetch detail for {match_id[:8]}: {e}")

        # If there are more queued details, drain them first
        if self._dig_detail_queue:
            self._dig_drain_detail()
            return

        # Queue drained. DFS decision for next walk step

        # 15% teleportation: restart from a new random root
        if random.random() < DIG_RESTART_CHANCE:
            logger.debug(f"DFS teleport after match {match_id[:8]}")
            self._dig_walk_start()
            return

        # Pick a random unvisited player from the last match to go deeper
        try:
            players: list[dict[str, Any]] = response.get("players", [])  # pyright: ignore[reportExplicitAny, reportAny, reportPossiblyUnboundVariable]
        except UnboundLocalError:
            # response wasn't set because the last detail fetch failed
            self._dig_walk_start()
            return

        candidates: list[str] = [
            p.get("subject", "")
            for p in players
            if p.get("subject", "")
            and p.get("subject", "") != self.puuid
            and p.get("subject", "") not in self._watermark.dig_visited
        ]

        if not candidates:
            # Dead end => all players in this match already visited
            logger.debug(f"DFS dead end at match {match_id[:8]} (all players visited)")
            self._dig_walk_start()
            return

        # Continue the walk deeper with a random player
        next_player: str = random.choice(candidates)
        self._unvisited_players.discard(next_player)
        self._watermark.dig_visited.add(next_player)

        logger.debug(f"DFS continuing: {match_id[:8]} => player {next_player[:8]}")
        self._scheduler.enqueue_general(
            lambda p=next_player: self._dig_fetch_history(p),
            f"dig history {next_player[:8]}",
        )

    # --------- Watermark Persistence ---------

    def _load_watermark(self) -> None:
        """Load collection progress from the local watermark file

        Populates the central watermark and restores the unvisited player pool
        """
        try:
            if not self._watermark_path.exists():
                return

            raw: dict[str, Any] = json.loads(  # pyright: ignore[reportExplicitAny, reportAny]
                self._watermark_path.read_text(encoding="utf-8")
            )

            # --- Central state ---
            fetched: list[str] = raw.get("fetched_matches", [])  # pyright: ignore[reportAny]
            self._watermark.fetched_matches = set(fetched)

            dig_visited: list[str] = raw.get("dig_visited", [])  # pyright: ignore[reportAny]
            self._watermark.dig_visited = set(dig_visited)

            dig_visited_matches: list[str] = raw.get("dig_visited_matches", [])  # pyright: ignore[reportAny]
            self._watermark.dig_visited_matches = set(dig_visited_matches)

            # Restore unvisited pool, pruning any that have since been visited
            dig_unvisited: list[str] = raw.get("dig_queue", [])  # pyright: ignore[reportAny]
            self._unvisited_players = set(dig_unvisited) - self._watermark.dig_visited

            # --- Per-account state ---
            accounts_raw: dict[str, Any] = raw.get("accounts", {})  # pyright: ignore[reportExplicitAny, reportAny]
            for puuid_key, acct_data in accounts_raw.items():  # pyright: ignore[reportAny]
                if not isinstance(acct_data, dict):
                    continue
                self._watermark.accounts[puuid_key] = AccountProgress(
                    newest_known_time=acct_data.get("newest_known_time", 0),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    oldest_fetched_time=acct_data.get("oldest_fetched_time", 0),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    legacy_complete=acct_data.get("legacy_complete", False),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                )

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load watermark, starting fresh: {e}")

    def _save_watermark(self) -> None:
        """Saves the current progress pointers"""
        
        try:
            accounts_out: dict[str, dict[str, object]] = {}
            for puuid_key, acct in self._watermark.accounts.items():
                accounts_out[puuid_key] = {
                    "newest_known_time": acct.newest_known_time,
                    "oldest_fetched_time": acct.oldest_fetched_time,
                    "legacy_complete": acct.legacy_complete,
                }

            # Persist the unvisited pool (capped to prevent unbounded growth)
            unvisited_snapshot: list[str] = sorted(self._unvisited_players)[:500]

            data: dict[str, object] = {
                "accounts": accounts_out,
                "fetched_matches": sorted(self._watermark.fetched_matches),
                "dig_queue": unvisited_snapshot,
                "dig_visited": sorted(self._watermark.dig_visited),
                "dig_visited_matches": sorted(self._watermark.dig_visited_matches),
            }

            self._watermark_path.parent.mkdir(parents=True, exist_ok=True)

            # Atomic write: write to temp file, then rename
            tmp_path: Path = self._watermark_path.with_suffix(".tmp")
            _ = tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _ = tmp_path.replace(self._watermark_path)

            logger.debug(
                f"Watermark saved ({len(self._watermark.fetched_matches)} matches, {len(self._watermark.accounts)} accounts, {len(self._watermark.dig_visited)} visited players, {len(self._watermark.dig_visited_matches)} visited match vertices)"
            )
        except OSError as e:
            logger.warning(f"Failed to save watermark: {e}")
            
    async def _fallback_leaderboard(self) -> list[str]:
        """Seed the dig phase from top-500 leaderboard players when the account has no match history.

        Picks a random player from the leaderboard, fetches their match history,
        and returns the match IDs. If that player has no history, tries the next
        random player until one works or all 500 are exhausted.

        Returns:
            List of match IDs from the first leaderboard player with history.

        Raises:
            RuntimeError: If no leaderboard player yields any match history.
        """
        leaderboard: LeaderboardResponse = await self._session.general_get_leaderboard(start_index=0, size=510)
        _ = await self._bus.emit(Event.LEADERBOARD_FETCHED, leaderboard)
        players = [p for p in (leaderboard.Players or []) if hasattr(p, "puuid") and p.puuid]  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]

        if not players:
            raise LeaderboardFallbackError(message="Leaderboard returned no players")

        # Seed the unvisited pool with all leaderboard players
        for p in players:
            p_puuid: str = p.puuid  # pyright: ignore[reportUnknownMemberType, reportAssignmentType, reportUnknownVariableType, reportAttributeAccessIssue]
            if p_puuid and p_puuid != self.puuid and p_puuid not in self._watermark.dig_visited:
                self._unvisited_players.add(p_puuid)
        logger.info(f"Fallback: seeded {len(self._unvisited_players)} leaderboard players into unvisited pool")

        random.shuffle(players)

        for player in players:
            puuid: str = player.puuid  # pyright: ignore[reportUnknownMemberType, reportAssignmentType, reportUnknownVariableType, reportAttributeAccessIssue]
            logger.debug(f"Fallback: trying leaderboard player {puuid[:8]}")

            try:
                response: MatchHistoryResponse = await self._session.general_get_history(
                    puuid=puuid,
                    start_index=0,
                    end_index=PAGE_SIZE,
                )
            except Exception as e:
                logger.debug(f"Fallback: failed to fetch history for {puuid[:8]}: {e}")
                continue

            history: list[MatchHistoryEntry] = [
                e for e in (response.History or []) if isinstance(e, MatchHistoryEntry)
            ]

            if history:
                match_ids: list[str] = [e.MatchID for e in history]
                logger.info(f"Fallback: found {len(match_ids)} match(es) from leaderboard player {puuid[:8]}")
                return match_ids

        raise LeaderboardFallbackError(message="No leaderboard player had accessible match history")

