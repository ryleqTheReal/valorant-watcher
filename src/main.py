"""
Starts the event bus, process watcher, and all handlers,
then runs in the background until SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from pathlib import Path

from services.event_bus import EventBus, Event
from services.launch_observer import RiotClientWatcher, ProcessWatcher
from services.auth_service import AuthHandler
from services.gamesocket import GameSocketHandler
from services.gamestates import GamestateHandler
from services.config_manager import ConfigManager

from utils.file_utils import get_config_path, get_watermark_path


_log_format = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
_log_datefmt = "%H:%M:%S"

logging.basicConfig(
    level=logging.INFO,
    format=_log_format,
    datefmt=_log_datefmt,
)

# File handler: warnings and above go to data/errors.log
_error_log_path = Path(__file__).resolve().parents[1] / "data" / "errors.log"
_error_log_path.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(_error_log_path, encoding="utf-8")
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter(_log_format, datefmt=_log_datefmt))
logging.getLogger().addHandler(_file_handler)

logger: logging.Logger = logging.getLogger("main")

class ValorantStatsApp:
    """Orchestrates all components."""

    def __init__(self) -> None:
        self.bus: EventBus = EventBus()
        self.cfg: ConfigManager = ConfigManager(get_config_path())
        self.riot_watcher: RiotClientWatcher = RiotClientWatcher(self.bus, self.cfg.config.poll_interval)
        self.process_watcher: ProcessWatcher = ProcessWatcher(self.bus, self.cfg.config.poll_interval)
        self.auth: AuthHandler = AuthHandler(self.bus)
        self.gamesocket: GameSocketHandler = GameSocketHandler(self.bus)
        self.gamestates: GamestateHandler = GamestateHandler(
            self.bus,
            watermark_path=get_watermark_path(),
            ratelimit_offset=self.cfg.config.ratelimit_offset,
            initial_limit=self.cfg.config.ratelimit_initial_limit,
            sustained_limit=self.cfg.config.ratelimit_sustained_limit,
            aggressive_limit=self.cfg.config.ratelimit_aggressive_limit,
        )

    async def run(self) -> None:
        """Start the app and block until a shutdown signal is received."""

        # --- TEMP: dump match details to local JSONL for overnight testing ---
        match_dump_path = Path(__file__).resolve().parents[1] / "data" / "match_dump.jsonl"
        match_dump_path.parent.mkdir(parents=True, exist_ok=True)
        self._match_dump_file = open(match_dump_path, "a", encoding="utf-8")  # noqa: SIM115  # pyright: ignore[reportUninitializedInstanceVariable]
        self._match_count: int = 0  # pyright: ignore[reportUninitializedInstanceVariable]

        async def _on_match_detail(data: object) -> None:
            self._match_count += 1
            _ = self._match_dump_file.write(json.dumps(data, separators=(",", ":")) + "\n")
            self._match_dump_file.flush()
            if self._match_count % 10 == 0:
                logger.info(f"[DUMP] {self._match_count} matches saved to {match_dump_path.name}")

        _ = self.bus.on(Event.MATCH_DETAIL_FETCHED, _on_match_detail, priority=0)
        # --- END TEMP ---

        logger.info("=" * 60)
        logger.info("  Valorant Stats Collector started")
        logger.info(f"  Match dump: {match_dump_path}")
        logger.info("=" * 60)

        shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _signal_handler() -> None:
            logger.info("Shutdown signal received")
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _signal_handler)
            except NotImplementedError:
                # Windows does not support add_signal_handler
                _ = signal.signal(sig, lambda s, f: shutdown_event.set())

        _ = await self.riot_watcher.start_polling()
        _ = await self.process_watcher.start_polling()
        _ = await shutdown_event.wait()

        logger.info("Shutting down ...")
        _ = await self.bus.emit(Event.SHUTDOWN)
        _ = await self.riot_watcher.stop_polling()
        _ = await self.process_watcher.stop_polling()

        # --- TEMP: close dump file ---
        self._match_dump_file.close()
        logger.info(f"[DUMP] Total: {self._match_count} matches saved")
        # --- END TEMP ---

        logger.info("App terminated.")


def main() -> None:
    while True:
        try:
            app = ValorantStatsApp()
            asyncio.run(app.run())
            break
        except KeyboardInterrupt:
            break
        except Exception:
            logger.exception("Crashed unexpectedly, restarting...")


if __name__ == "__main__":
    main()