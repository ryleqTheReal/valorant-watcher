"""
Collects Mac hardware specs and computes a stable HWID hash.

Uses a single `system_profiler -json` call to gather all data.
Handles both Apple Silicon and Intel Macs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("hardware_mac")


# ── Component dataclasses ────────────────────────────────────────

@dataclass
class MacMachineInfo:
    model_name: str = ""          # "MacBook Pro"
    model_identifier: str = ""    # "Mac14,5"
    chip: str = ""                # "Apple M2 Pro" or "Intel Core i9-9980HK"
    is_apple_silicon: bool = False


@dataclass
class MacCpuInfo:
    model: str = ""               # "Apple M2 Pro" or "3.6 GHz 8-Core Intel Core i9"
    total_cores: int = 0
    performance_cores: int = 0    # Apple Silicon only
    efficiency_cores: int = 0     # Apple Silicon only


@dataclass
class MacGpuInfo:
    model: str = ""               # "Apple M2 Pro" or "AMD Radeon Pro 5500M"
    cores: int = 0                # GPU core count (Apple Silicon)
    vram_mb: int = 0              # Discrete GPU only (Intel Macs)


@dataclass
class MacMemoryInfo:
    total_gb: float = 0.0
    memory_type: str = ""         # "Unified" (Apple Silicon) | "DDR4" | "LPDDR4" etc.


@dataclass
class MacStorageDevice:
    model: str = ""
    capacity_gb: float = 0.0
    media_type: str = ""          # SSD | HDD
    protocol: str = ""            # "Apple Fabric" | "SATA" | "NVMe" | "USB"


@dataclass
class MacOsInfo:
    name: str = ""                # "macOS"
    version: str = ""             # "14.2.1"
    build: str = ""               # "23C71"
    architecture: str = ""        # "arm64" | "x86_64"


@dataclass
class MacDisplayInfo:
    name: str = ""                # "Built-in Retina Display" or "LG UltraFine"
    resolution: str = ""          # "3024 x 1964"
    refresh_rate_hz: int = 0
    adapter: str = ""             # GPU driving this display


@dataclass
class MacAudioDevice:
    name: str = ""


@dataclass
class MacInputDevice:
    name: str = ""
    device_type: str = ""         # keyboard | mouse | trackpad
    connection: str = ""          # USB | Bluetooth | Built-in


# ── Top-level snapshot ───────────────────────────────────────────

@dataclass
class MacHardwareSnapshot:
    device_type: str = ""         # Laptop | Desktop
    machine: MacMachineInfo = field(default_factory=MacMachineInfo)
    cpu: MacCpuInfo = field(default_factory=MacCpuInfo)
    gpus: list[MacGpuInfo] = field(default_factory=list)
    memory: MacMemoryInfo = field(default_factory=MacMemoryInfo)
    storage: list[MacStorageDevice] = field(default_factory=list)
    os: MacOsInfo = field(default_factory=MacOsInfo)
    displays: list[MacDisplayInfo] = field(default_factory=list)
    audio_devices: list[MacAudioDevice] = field(default_factory=list)
    input_devices: list[MacInputDevice] = field(default_factory=list)
    hwid_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        """Flat-ish dict ready for JSON serialisation / server submission."""
        return {
            "hwid_hash": self.hwid_hash,
            "device_type": self.device_type,
            "machine": {
                "model_name": self.machine.model_name,
                "model_identifier": self.machine.model_identifier,
                "chip": self.machine.chip,
                "is_apple_silicon": self.machine.is_apple_silicon,
            },
            "cpu": {
                "model": self.cpu.model,
                "total_cores": self.cpu.total_cores,
                "performance_cores": self.cpu.performance_cores,
                "efficiency_cores": self.cpu.efficiency_cores,
            },
            "gpus": [
                {
                    "model": g.model,
                    "cores": g.cores,
                    "vram_mb": g.vram_mb,
                }
                for g in self.gpus
            ],
            "memory": {
                "total_gb": self.memory.total_gb,
                "memory_type": self.memory.memory_type,
            },
            "storage": [
                {
                    "model": d.model,
                    "capacity_gb": d.capacity_gb,
                    "media_type": d.media_type,
                    "protocol": d.protocol,
                }
                for d in self.storage
            ],
            "os": {
                "name": self.os.name,
                "version": self.os.version,
                "build": self.os.build,
                "architecture": self.os.architecture,
            },
            "displays": [
                {
                    "name": d.name,
                    "resolution": d.resolution,
                    "refresh_rate_hz": d.refresh_rate_hz,
                    "adapter": d.adapter,
                }
                for d in self.displays
            ],
            "audio_devices": [{"name": a.name} for a in self.audio_devices],
            "input_devices": [
                {
                    "name": i.name,
                    "device_type": i.device_type,
                    "connection": i.connection,
                }
                for i in self.input_devices
            ],
        }


# ── system_profiler query (single call, all data types) ─────────

_SP_DATA_TYPES = [
    "SPHardwareDataType",
    "SPDisplaysDataType",
    "SPStorageDataType",
    "SPAudioDataType",
    "SPUSBDataType",
    "SPBluetoothDataType",
    "SPSoftwareDataType",
]


async def _run_system_profiler() -> dict[str, object]:
    """Run system_profiler with JSON output and return parsed data."""
    proc = await asyncio.create_subprocess_exec(
        "system_profiler", "-json", *_SP_DATA_TYPES,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("system_profiler exited %d: %s", proc.returncode, stderr.decode("utf-8", errors="replace"))
        return {}

    try:
        return json.loads(stdout.decode("utf-8"))  # type: ignore[no-any-return]  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        logger.error("Failed to parse system_profiler JSON output")
        return {}


# ── Parsing helpers ──────────────────────────────────────────────

def _str(val: object) -> str:
    return str(val).strip() if val is not None else ""


def _int(val: object) -> int:
    try:
        return int(val)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0


def _first(raw: dict[str, object], key: str) -> dict[str, object]:
    """Get the first item from a system_profiler data type array."""
    items = raw.get(key)
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]  # pyright: ignore[reportUnknownVariableType]
    return {}


def _all(raw: dict[str, object], key: str) -> list[dict[str, object]]:
    """Get all items from a system_profiler data type array."""
    items = raw.get(key)
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]  # pyright: ignore[reportUnknownVariableType]
    return []


def _parse_machine(hw: dict[str, object]) -> MacMachineInfo:
    chip = _str(hw.get("chip_type"))
    is_as = bool(chip)
    return MacMachineInfo(
        model_name=_str(hw.get("machine_name")),
        model_identifier=_str(hw.get("machine_model")),
        chip=chip if is_as else _str(hw.get("cpu_type")),
        is_apple_silicon=is_as,
    )


def _parse_cpu(hw: dict[str, object]) -> MacCpuInfo:
    is_as = bool(hw.get("chip_type"))

    total_cores = 0
    perf_cores = 0
    eff_cores = 0

    if is_as:
        # Apple Silicon: "number_processors" = "proc 10:6:4" (total:perf:eff)
        proc_str = _str(hw.get("number_processors"))
        match = re.search(r"(\d+):(\d+):(\d+)", proc_str)
        if match:
            total_cores = int(match.group(1))
            perf_cores = int(match.group(2))
            eff_cores = int(match.group(3))
        else:
            # Fallback: might just be "proc 8" or an integer
            digits = re.search(r"(\d+)", proc_str)
            total_cores = int(digits.group(1)) if digits else 0
    else:
        # Intel: "number_processors" is an int or "proc 8"
        proc_val = hw.get("number_processors")
        if isinstance(proc_val, int):
            total_cores = proc_val
        else:
            digits = re.search(r"(\d+)", _str(proc_val))
            total_cores = int(digits.group(1)) if digits else 0

    model = _str(hw.get("chip_type")) if is_as else _str(hw.get("cpu_type"))

    return MacCpuInfo(
        model=model,
        total_cores=total_cores,
        performance_cores=perf_cores,
        efficiency_cores=eff_cores,
    )


def _parse_gpus(displays_data: list[dict[str, object]]) -> tuple[list[MacGpuInfo], list[MacDisplayInfo]]:
    """Parse SPDisplaysDataType — contains both GPU adapters and attached displays."""
    gpus: list[MacGpuInfo] = []
    displays: list[MacDisplayInfo] = []

    for adapter in displays_data:
        model = _str(adapter.get("sppci_model") or adapter.get("chipset_model"))
        cores = _int(adapter.get("sppci_cores"))

        # VRAM: Intel Macs report "sppci_vram" like "4096 MB"
        vram_str = _str(adapter.get("sppci_vram"))
        vram_match = re.search(r"(\d+)", vram_str)
        vram_mb = int(vram_match.group(1)) if vram_match else 0

        gpus.append(MacGpuInfo(model=model, cores=cores, vram_mb=vram_mb))

        # Each adapter lists its connected displays under "spdisplays_ndrvs"
        for disp in adapter.get("spdisplays_ndrvs", []):  # pyright: ignore[reportGeneralTypeIssues, reportUnknownVariableType]
            if not isinstance(disp, dict):
                continue

            name = _str(disp.get("_name"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

            # Resolution: "_spdisplays_pixels" = "3024 x 1964" (native)
            resolution = _str(disp.get("_spdisplays_pixels"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

            # Refresh rate: "_spdisplays_resolution" = "1512 x 982 @ 120.00Hz"
            refresh_hz = 0
            res_line = _str(disp.get("_spdisplays_resolution"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            hz_match = re.search(r"@\s*(\d+)", res_line)
            if hz_match:
                refresh_hz = int(hz_match.group(1))

            displays.append(MacDisplayInfo(
                name=name,
                resolution=resolution,
                refresh_rate_hz=refresh_hz,
                adapter=model,
            ))

    return gpus, displays


def _parse_memory(hw: dict[str, object]) -> MacMemoryInfo:
    is_as = bool(hw.get("chip_type"))

    # "physical_memory" = "16 GB" or "32 GB"
    mem_str = _str(hw.get("physical_memory"))
    gb_match = re.search(r"([\d.]+)", mem_str)
    total_gb = float(gb_match.group(1)) if gb_match else 0.0

    if is_as:
        memory_type = "Unified"
    else:
        # Intel Macs: SPMemoryDataType would have this, but we can infer from
        # SPHardwareDataType "physical_memory_type" if present, else "Unknown"
        memory_type = _str(hw.get("physical_memory_type")) or "Unknown"

    return MacMemoryInfo(total_gb=total_gb, memory_type=memory_type)


def _parse_storage(storage_data: list[dict[str, object]]) -> list[MacStorageDevice]:
    devices: list[MacStorageDevice] = []
    for vol in storage_data:
        phys = vol.get("physical_drive")
        if not isinstance(phys, dict):
            continue

        # Size: "size_in_bytes" or "size" (string like "500.07 GB")
        size_bytes = _int(phys.get("size_in_bytes"))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        if size_bytes:
            cap_gb = round(size_bytes / (1024 ** 3), 2)
        else:
            size_str = _str(phys.get("size"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            gb_match = re.search(r"([\d.]+)\s*GB", size_str, re.IGNORECASE)
            tb_match = re.search(r"([\d.]+)\s*TB", size_str, re.IGNORECASE)
            if tb_match:
                cap_gb = round(float(tb_match.group(1)) * 1024, 2)
            elif gb_match:
                cap_gb = round(float(gb_match.group(1)), 2)
            else:
                cap_gb = 0.0

        media = _str(phys.get("medium_type")).upper()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        if "SSD" in media or "SOLID" in media:
            media_type = "SSD"
        elif "HDD" in media or "ROTATIONAL" in media:
            media_type = "HDD"
        else:
            media_type = media or "Unknown"

        devices.append(MacStorageDevice(
            model=_str(phys.get("device_name")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            capacity_gb=cap_gb,
            media_type=media_type,
            protocol=_str(phys.get("protocol")),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        ))

    return devices


def _parse_os(sw: dict[str, object]) -> MacOsInfo:
    # "os_version" = "macOS 14.2.1 (23C71)"
    os_str = _str(sw.get("os_version"))
    name = "macOS"
    version = ""
    build = ""

    ver_match = re.search(r"([\d.]+)", os_str)
    if ver_match:
        version = ver_match.group(1)
    build_match = re.search(r"\(([^)]+)\)", os_str)
    if build_match:
        build = build_match.group(1)

    # Architecture: "kernel_version" doesn't reliably tell us, use chip presence
    arch = "arm64" if sw.get("chip_type") else "x86_64"
    # Fallback: check from hardware data passed in separately
    return MacOsInfo(name=name, version=version, build=build, architecture=arch)


def _parse_audio(audio_data: list[dict[str, object]]) -> list[MacAudioDevice]:
    return [MacAudioDevice(name=_str(item.get("_name"))) for item in audio_data]


def _parse_peripherals(
    usb_data: list[dict[str, object]],
    bt_data: list[dict[str, object]],
) -> list[MacInputDevice]:
    """Extract input devices from USB and Bluetooth trees."""
    devices: list[MacInputDevice] = []

    _INPUT_KEYWORDS = {
        "keyboard": "keyboard",
        "mouse": "mouse",
        "trackpad": "trackpad",
        "magic trackpad": "trackpad",
        "magic mouse": "mouse",
        "magic keyboard": "keyboard",
    }

    def _classify(name: str) -> str | None:
        lower = name.lower()
        for keyword, dtype in _INPUT_KEYWORDS.items():
            if keyword in lower:
                return dtype
        return None

    def _walk_usb(items: list[dict[str, object]]) -> None:
        """USB tree is nested — walk recursively."""
        for item in items:
            if not isinstance(item, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
                continue
            name = _str(item.get("_name"))
            dtype = _classify(name)
            if dtype:
                devices.append(MacInputDevice(
                    name=name,
                    device_type=dtype,
                    connection="USB",
                ))
            # Recurse into USB hubs
            children = item.get("_items")
            if isinstance(children, list):
                _walk_usb(children)  # pyright: ignore[reportUnknownArgumentType]

    _walk_usb(usb_data)

    # Bluetooth devices are also nested under controllers
    for controller in bt_data:
        if not isinstance(controller, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            continue
        # Connected devices are under various keys
        for key in ("device_connected", "device_not_connected"):
            bt_devices = controller.get(key)
            if not isinstance(bt_devices, list):
                continue
            for item in bt_devices:  # pyright: ignore[reportUnknownVariableType]
                if not isinstance(item, dict):
                    continue
                name = _str(item.get("device_name") or item.get("_name"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                dtype = _classify(name)
                if dtype:
                    devices.append(MacInputDevice(
                        name=name,
                        device_type=dtype,
                        connection="Bluetooth",
                    ))

    # Built-in trackpad and keyboard (not in USB/BT, always present on laptops)
    # These show up in SPHardwareDataType indirectly — we infer from machine_name
    return devices


def _parse_device_type(machine_name: str) -> str:
    lower = machine_name.lower()
    if "macbook" in lower:
        return "Laptop"
    if any(kw in lower for kw in ("imac", "mac mini", "mac pro", "mac studio")):
        return "Desktop"
    return "Unknown"


# ── HWID hashing ─────────────────────────────────────────────────

def _compute_hwid(hw: dict[str, object]) -> str:
    """
    SHA-256 of stable Mac identifiers:
    platform UUID + serial number.

    Returns hex digest. Empty string if nothing could be read.
    """
    parts: list[str] = []
    parts.append(_str(hw.get("platform_UUID")))
    parts.append(_str(hw.get("serial_number")))

    parts = sorted(p for p in parts if p)
    if not parts:
        return ""

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ── Public API ───────────────────────────────────────────────────

async def collect_hardware_mac() -> MacHardwareSnapshot:
    """Collect all Mac hardware specs. Call once at startup."""
    raw = await _run_system_profiler()
    if not raw:
        logger.warning("Hardware collection returned empty — using defaults")
        return MacHardwareSnapshot()

    hw = _first(raw, "SPHardwareDataType")
    sw = _first(raw, "SPSoftwareDataType")
    displays_data = _all(raw, "SPDisplaysDataType")
    storage_data = _all(raw, "SPStorageDataType")
    audio_data = _all(raw, "SPAudioDataType")
    usb_data = _all(raw, "SPUSBDataType")
    bt_data = _all(raw, "SPBluetoothDataType")

    gpus, displays = _parse_gpus(displays_data)
    machine = _parse_machine(hw)

    # Pass Apple Silicon flag to OS parser for architecture detection
    os_info = _parse_os(sw)
    if machine.is_apple_silicon:
        os_info.architecture = "arm64"

    snapshot = MacHardwareSnapshot(
        device_type=_parse_device_type(machine.model_name),
        machine=machine,
        cpu=_parse_cpu(hw),
        gpus=gpus,
        memory=_parse_memory(hw),
        storage=_parse_storage(storage_data),
        os=os_info,
        displays=displays,
        audio_devices=_parse_audio(audio_data),
        input_devices=_parse_peripherals(usb_data, bt_data),
        hwid_hash=_compute_hwid(hw),
    )

    logger.info(
        "Hardware collected: %s | %s | %.1f GB %s | HWID=%s…",
        snapshot.cpu.model,
        snapshot.gpus[0].model if snapshot.gpus else "no GPU",
        snapshot.memory.total_gb,
        snapshot.memory.memory_type,
        snapshot.hwid_hash[:12],
    )
    return snapshot


if __name__ == "__main__":
    import asyncio as _asyncio

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main() -> None:
        snap = await collect_hardware_mac()
        print(json.dumps(snap.to_dict(), indent=2, ensure_ascii=False))

    _asyncio.run(_main())
