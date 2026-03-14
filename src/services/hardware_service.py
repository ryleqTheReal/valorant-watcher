"""
One-shot hardware collection service.

Detects the current platform (Windows / macOS), collects hardware
specs, and emits HARDWARE_COLLECTED with the snapshot.
"""

from __future__ import annotations

import logging
import platform

from services.event_bus import EventBus, Event

logger = logging.getLogger(__name__)


async def collect_and_emit(bus: EventBus) -> None:
    """Collect hardware specs for the current platform and emit the event."""
    system = platform.system()

    if system == "Windows":
        from utils.hardware import collect_hardware
        snapshot = await collect_hardware()
    elif system == "Darwin":
        from utils.hardware_mac import collect_hardware_mac
        snapshot = await collect_hardware_mac()
    else:
        logger.warning(f"Unsupported platform '{system}', skipping hardware collection")
        return

    _ = await bus.emit(Event.HARDWARE_COLLECTED, snapshot)
