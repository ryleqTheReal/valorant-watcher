"""
Starts the event bus, process watcher, and all handlers,
then runs in the background until SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import signal
import sys
import traceback
from pathlib import Path


def _app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


_log_format = "%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s"
_log_datefmt = "%H:%M:%S"

_log_dir = _app_base_dir() / "data"
_log_dir.mkdir(parents=True, exist_ok=True)

_debug_log_path = _log_dir / "debug.log"
_error_log_path = _log_dir / "errors.log"
_fault_log_path = _log_dir / "fault.log"

# Native crashes (segfaults, stack overflows) — written before Python unwinds.
_fault_log_file = open(_fault_log_path, "a", buffering=1, encoding="utf-8")
faulthandler.enable(file=_fault_log_file)

logging.basicConfig(
    level=logging.INFO,
    format=_log_format,
    datefmt=_log_datefmt,
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(_debug_log_path, encoding="utf-8"),
    ],
)

_err_handler = logging.FileHandler(_error_log_path, encoding="utf-8")
_err_handler.setLevel(logging.WARNING)
_err_handler.setFormatter(logging.Formatter(_log_format, datefmt=_log_datefmt))
logging.getLogger().addHandler(_err_handler)

logger: logging.Logger = logging.getLogger("main")


def _excepthook(exc_type, exc, tb) -> None:
    logger.critical(
        "UNCAUGHT EXCEPTION\n%s",
        "".join(traceback.format_exception(exc_type, exc, tb)),
    )


sys.excepthook = _excepthook
logger.info("logging initialized | base_dir=%s | frozen=%s", _app_base_dir(), getattr(sys, "frozen", False))

try:
    from services.event_bus import EventBus, Event
    from services.launch_observer import RiotClientWatcher, ProcessWatcher
    from services.auth_service import AuthHandler
    from services.backend_service import BackendCommunicationService
    from services.gamesocket import GameSocketHandler
    from services.gamestates import GamestateHandler
    from services.session_service import SessionService
    from services.submission_service import SubmissionService
    from services.config_manager import ConfigManager
    from services.hardware_service import collect_and_emit as collect_hardware

    from utils.file_utils import get_config_path, get_watermark_path
except Exception:
    logger.critical("Import failed at startup", exc_info=True)
    raise

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
            match_details_interval_ms=self.cfg.config.match_details_interval_ms,
            match_history_interval_ms=self.cfg.config.match_history_interval_ms,
            competitive_updates_interval_ms=self.cfg.config.competitive_updates_interval_ms,
        )
        server_base_url = self.cfg.config.server_base_url
        if not server_base_url:
            raise RuntimeError("config.json is missing 'server_base_url'; backend auth cannot run.")
        self.backend: BackendCommunicationService = BackendCommunicationService(
            self.bus,
            server_base_url=server_base_url,
        )
        self.session: SessionService = SessionService(self.bus, self.backend)
        self.submission: SubmissionService = SubmissionService(
            self.bus,
            self.backend,
            self.gamestates.scheduler,
        )

    async def run(self) -> None:
        """Start the app and block until a shutdown signal is received."""

        # --- TEMP: log pregame version changes ---
        async def _on_pregame_update(data: object) -> None:
            from utils.models import PregameMatchResponse
            if isinstance(data, PregameMatchResponse):
                logger.info(f"[PREGAME] Version changed: {data.Version} | State: {data.PregameState} | Map: {data.MapID}")

        _ = self.bus.on(Event.PREGAME_MATCH_UPDATED, _on_pregame_update, priority=0)
        # --- END TEMP ---

        # --- TEMP: log ingame match data ---
        async def _on_ingame_update(data: object) -> None:
            from utils.models import IngameMatchResponse
            if isinstance(data, IngameMatchResponse):
                logger.info(f"[INGAME] Match: {data.MatchID} | State: {data.State} | Map: {data.MapID}")

        _ = self.bus.on(Event.INGAME_MATCH_UPDATED, _on_ingame_update, priority=0)
        # --- END TEMP ---

        logger.info("=" * 60)
        logger.info("  Valorant Stats Collector started")
        logger.info("=" * 60)

        _ = await self.bus.emit(Event.STARTUP)

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

        _ = await collect_hardware(self.bus)
        _ = await self.riot_watcher.start_polling()
        _ = await self.process_watcher.start_polling()
        _ = await shutdown_event.wait()

        logger.info("Shutting down ...")
        _ = await self.bus.emit(Event.SHUTDOWN)
        _ = await self.riot_watcher.stop_polling()
        _ = await self.process_watcher.stop_polling()

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
    try:
        main()
    except Exception:
        logger.critical("Fatal error in main()", exc_info=True)
        raise
    finally:
        logging.shutdown()