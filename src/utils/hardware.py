"""
Collects PC hardware specs and computes a stable HWID hash.

Windows-only for now. Runs once at startup via a single PowerShell
call that queries WMI/CIM and returns structured JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("hardware")


# ── Component dataclasses ────────────────────────────────────────

@dataclass
class CpuInfo:
    manufacturer: str = ""
    model: str = ""
    base_clock_ghz: float = 0.0
    cores: int = 0
    threads: int = 0


@dataclass
class GpuInfo:
    manufacturer: str = ""
    model: str = ""
    vram_mb: int = 0
    driver_version: str = ""


@dataclass
class RamStick:
    manufacturer: str = ""
    capacity_gb: float = 0.0
    speed_mhz: int = 0


@dataclass
class RamInfo:
    total_gb: float = 0.0
    sticks: list[RamStick] = field(default_factory=list)


@dataclass
class StorageDevice:
    model: str = ""
    capacity_gb: float = 0.0
    media_type: str = ""   # SSD | HDD | Unspecified
    bus_type: str = ""     # NVMe | SATA | USB | ...


@dataclass
class MotherboardInfo:
    manufacturer: str = ""
    model: str = ""


@dataclass
class OsInfo:
    name: str = ""
    version: str = ""
    build: str = ""
    architecture: str = ""


@dataclass
class DisplayInfo:
    adapter: str = ""
    horizontal_resolution: int = 0
    vertical_resolution: int = 0
    refresh_rate_hz: int = 0


@dataclass
class AudioDevice:
    name: str = ""


@dataclass
class InputDevice:
    name: str = ""
    device_type: str = ""  # keyboard | mouse
    manufacturer: str = ""


# ── Top-level snapshot ───────────────────────────────────────────

@dataclass
class HardwareSnapshot:
    device_type: str = ""  # Desktop | Laptop | Tablet | Unknown
    cpu: CpuInfo = field(default_factory=CpuInfo)
    gpus: list[GpuInfo] = field(default_factory=list)
    ram: RamInfo = field(default_factory=RamInfo)
    storage: list[StorageDevice] = field(default_factory=list)
    motherboard: MotherboardInfo = field(default_factory=MotherboardInfo)
    os: OsInfo = field(default_factory=OsInfo)
    displays: list[DisplayInfo] = field(default_factory=list)
    audio_devices: list[AudioDevice] = field(default_factory=list)
    input_devices: list[InputDevice] = field(default_factory=list)
    hwid_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        """Flat-ish dict ready for JSON serialisation / server submission."""
        return {
            "hwid_hash": self.hwid_hash,
            "device_type": self.device_type,
            "cpu": {
                "manufacturer": self.cpu.manufacturer,
                "model": self.cpu.model,
                "base_clock_ghz": self.cpu.base_clock_ghz,
                "cores": self.cpu.cores,
                "threads": self.cpu.threads,
            },
            "gpus": [
                {
                    "manufacturer": g.manufacturer,
                    "model": g.model,
                    "vram_mb": g.vram_mb,
                    "driver_version": g.driver_version,
                }
                for g in self.gpus
            ],
            "ram": {
                "total_gb": self.ram.total_gb,
                "sticks": [
                    {
                        "manufacturer": s.manufacturer,
                        "capacity_gb": s.capacity_gb,
                        "speed_mhz": s.speed_mhz,
                    }
                    for s in self.ram.sticks
                ],
            },
            "storage": [
                {
                    "model": d.model,
                    "capacity_gb": d.capacity_gb,
                    "media_type": d.media_type,
                    "bus_type": d.bus_type,
                }
                for d in self.storage
            ],
            "motherboard": {
                "manufacturer": self.motherboard.manufacturer,
                "model": self.motherboard.model,
            },
            "os": {
                "name": self.os.name,
                "version": self.os.version,
                "build": self.os.build,
                "architecture": self.os.architecture,
            },
            "displays": [
                {
                    "adapter": d.adapter,
                    "horizontal_resolution": d.horizontal_resolution,
                    "vertical_resolution": d.vertical_resolution,
                    "refresh_rate_hz": d.refresh_rate_hz,
                }
                for d in self.displays
            ],
            "audio_devices": [{"name": a.name} for a in self.audio_devices],
            "input_devices": [
                {
                    "name": i.name,
                    "device_type": i.device_type,
                    "manufacturer": i.manufacturer,
                }
                for i in self.input_devices
            ],
        }


# ── PowerShell query (single call, all data) ────────────────────

_PS_SCRIPT = r"""
$vramRegistry = @{}
try {
    $regPath = 'HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    Get-ChildItem $regPath -ErrorAction SilentlyContinue | ForEach-Object {
        $props = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
        if ($props.'HardwareInformation.qwMemorySize' -and $props.DriverDesc) {
            $vramRegistry[$props.DriverDesc] = $props.'HardwareInformation.qwMemorySize'
        }
    }
} catch {}

$gpus = @(Get-CimInstance Win32_VideoController |
          Select-Object AdapterCompatibility, Name, AdapterRAM, DriverVersion,
                        CurrentHorizontalResolution, CurrentVerticalResolution,
                        CurrentRefreshRate)
foreach ($g in $gpus) {
    if ($vramRegistry.ContainsKey($g.Name)) {
        $g | Add-Member -NotePropertyName 'RegistryVRAM' -NotePropertyValue $vramRegistry[$g.Name] -Force
    }
}

$d = @{
    cpu = Get-CimInstance Win32_Processor |
          Select-Object Manufacturer, Name, MaxClockSpeed,
                        NumberOfCores, NumberOfLogicalProcessors, ProcessorId
    gpu = $gpus
    ram = @(Get-CimInstance Win32_PhysicalMemory |
            Select-Object Manufacturer, Capacity, Speed)
    storage = @(Get-PhysicalDisk |
                Select-Object FriendlyName, Size, MediaType, BusType)
    motherboard = Get-CimInstance Win32_BaseBoard |
                  Select-Object Manufacturer, Product, SerialNumber
    os = Get-CimInstance Win32_OperatingSystem |
         Select-Object Caption, Version, BuildNumber, OSArchitecture
    audio = @(Get-CimInstance Win32_SoundDevice |
              Select-Object Name)
    keyboards = @(Get-CimInstance Win32_Keyboard |
                  Select-Object Name, Description)
    mice = @(Get-CimInstance Win32_PointingDevice |
             Select-Object Name, Manufacturer)
    chassis = Get-CimInstance Win32_SystemEnclosure |
              Select-Object ChassisTypes
}
$d | ConvertTo-Json -Depth 3 -Compress
"""

# Type alias for raw JSON dicts from PowerShell.  json.loads returns
# dict[str, ...] but after isinstance-narrowing basedpyright infers
# dict[Unknown, Unknown].  Using a dedicated alias + casts keeps the
# ignores concentrated in one place.
_RawDict = dict[str, object]


def _as_raw(val: object) -> _RawDict | None:
    """Return val as _RawDict if it's a dict, else None."""
    if isinstance(val, dict):
        return val  # type: ignore[return-value]  # pyright: ignore[reportUnknownVariableType]
    return None


async def _run_powershell(script: str) -> _RawDict:
    """Execute a PowerShell script and return parsed JSON."""
    # Force UTF-8 output so non-ASCII characters (e.g. Chinese, German umlauts) survive
    utf8_prefix = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive", "-Command", utf8_prefix + script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        logger.error("PowerShell exited %d: %s", proc.returncode, stderr.decode("utf-8", errors="replace"))
        return {}

    try:
        return json.loads(stdout.decode("utf-8"))  # type: ignore[no-any-return]  # pyright: ignore[reportAny]
    except json.JSONDecodeError:
        logger.error("Failed to parse PowerShell JSON output")
        return {}


# ── Parsing helpers ──────────────────────────────────────────────

def _str(val: object) -> str:
    return str(val).strip() if val is not None else ""


def _int(val: object) -> int:
    try:
        return int(val)  # type: ignore[arg-type]  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return 0


def _ensure_list(val: object) -> list[_RawDict]:
    """WMI returns a dict for single items, list for multiple."""
    if isinstance(val, dict):
        return [val]  # type: ignore[list-item]
    if isinstance(val, list):
        return [i for i in val if isinstance(i, dict)]  # type: ignore[misc]  # pyright: ignore[reportUnknownVariableType]
    return []


# ── Component parsers ────────────────────────────────────────────

def _parse_cpu(raw: object) -> CpuInfo:
    d = _as_raw(raw)
    if d is None:
        return CpuInfo()
    return CpuInfo(
        manufacturer=_str(d.get("Manufacturer")),
        model=_str(d.get("Name")),
        base_clock_ghz=round(_int(d.get("MaxClockSpeed")) / 1000, 2),
        cores=_int(d.get("NumberOfCores")),
        threads=_int(d.get("NumberOfLogicalProcessors")),
    )


def _parse_gpus(raw: object) -> tuple[list[GpuInfo], list[DisplayInfo]]:
    gpus: list[GpuInfo] = []
    displays: list[DisplayInfo] = []
    for item in _ensure_list(raw):
        # Prefer registry VRAM (accurate) over WMI AdapterRAM (capped at 4 GB)
        registry_vram = _int(item.get("RegistryVRAM"))
        wmi_vram = _int(item.get("AdapterRAM"))
        vram_bytes = registry_vram if registry_vram else wmi_vram
        vram_mb = vram_bytes // (1024 * 1024) if vram_bytes else 0

        gpus.append(GpuInfo(
            manufacturer=_str(item.get("AdapterCompatibility")),
            model=_str(item.get("Name")),
            vram_mb=vram_mb,
            driver_version=_str(item.get("DriverVersion")),
        ))
        h_res = _int(item.get("CurrentHorizontalResolution"))
        v_res = _int(item.get("CurrentVerticalResolution"))
        if h_res and v_res:
            displays.append(DisplayInfo(
                adapter=_str(item.get("Name")),
                horizontal_resolution=h_res,
                vertical_resolution=v_res,
                refresh_rate_hz=_int(item.get("CurrentRefreshRate")),
            ))
    return gpus, displays


def _parse_ram(raw: object) -> RamInfo:
    sticks: list[RamStick] = []
    total = 0.0
    for item in _ensure_list(raw):
        cap_bytes = _int(item.get("Capacity"))
        cap_gb = round(cap_bytes / (1024 ** 3), 2) if cap_bytes else 0.0
        total += cap_gb
        sticks.append(RamStick(
            manufacturer=_str(item.get("Manufacturer")),
            capacity_gb=cap_gb,
            speed_mhz=_int(item.get("Speed")),
        ))
    return RamInfo(total_gb=round(total, 2), sticks=sticks)


def _parse_storage(raw: object) -> list[StorageDevice]:
    devices: list[StorageDevice] = []
    for item in _ensure_list(raw):
        size_bytes = _int(item.get("Size"))
        cap_gb = round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0
        raw_media = item.get("MediaType")
        if isinstance(raw_media, str):
            media_str = raw_media  # Get-PhysicalDisk returns "SSD", "HDD", "Unspecified"
        else:
            media_int = _int(raw_media)
            media_str = {0: "Unspecified", 3: "HDD", 4: "SSD"}.get(media_int, f"Unknown({media_int})")
        bus = _str(item.get("BusType"))
        devices.append(StorageDevice(
            model=_str(item.get("FriendlyName")),
            capacity_gb=cap_gb,
            media_type=media_str,
            bus_type=bus,
        ))
    return devices


def _parse_motherboard(raw: object) -> MotherboardInfo:
    d = _as_raw(raw)
    if d is None:
        return MotherboardInfo()
    return MotherboardInfo(
        manufacturer=_str(d.get("Manufacturer")),
        model=_str(d.get("Product")),
    )


def _parse_os(raw: object) -> OsInfo:
    d = _as_raw(raw)
    if d is None:
        return OsInfo()
    return OsInfo(
        name=_str(d.get("Caption")),
        version=_str(d.get("Version")),
        build=_str(d.get("BuildNumber")),
        architecture=_str(d.get("OSArchitecture")),
    )


def _parse_audio(raw: object) -> list[AudioDevice]:
    return [AudioDevice(name=_str(item.get("Name"))) for item in _ensure_list(raw)]


def _parse_input_devices(keyboards: object, mice: object) -> list[InputDevice]:
    devices: list[InputDevice] = []
    for item in _ensure_list(keyboards):
        devices.append(InputDevice(
            name=_str(item.get("Name")),
            device_type="keyboard",
            manufacturer=_str(item.get("Description")),
        ))
    for item in _ensure_list(mice):
        devices.append(InputDevice(
            name=_str(item.get("Name")),
            device_type="mouse",
            manufacturer=_str(item.get("Manufacturer")),
        ))
    return devices


def _parse_device_type(raw: object) -> str:
    """Map Win32_SystemEnclosure ChassisTypes to a human-readable device type."""
    _DESKTOP = {3, 4, 5, 6, 7, 15, 16, 24, 35}  # Desktop, Tower, Mini Tower, Pizza Box, etc.
    _LAPTOP = {8, 9, 10, 14, 31, 32}             # Portable, Laptop, Notebook, Sub Notebook, Convertible, Detachable
    _TABLET = {30}                                 # Tablet

    d = _as_raw(raw)
    if d is None:
        return "Unknown"
    codes = d.get("ChassisTypes")
    if not isinstance(codes, list) or not codes:
        return "Unknown"

    code = _int(codes[0])  # pyright: ignore[reportUnknownArgumentType]
    if code in _LAPTOP:
        return "Laptop"
    if code in _DESKTOP:
        return "Desktop"
    if code in _TABLET:
        return "Tablet"
    return "Unknown"


# ── HWID hashing ─────────────────────────────────────────────────

def _compute_hwid(raw: _RawDict) -> str:
    """
    SHA-256 of stable hardware identifiers:
    motherboard serial + CPU ProcessorId + sorted disk model names.

    Returns hex digest. Empty string if nothing could be read.
    """
    parts: list[str] = []

    mobo = _as_raw(raw.get("motherboard"))
    if mobo is not None:
        parts.append(_str(mobo.get("SerialNumber")))

    cpu = _as_raw(raw.get("cpu"))
    if cpu is not None:
        parts.append(_str(cpu.get("ProcessorId")))

    for disk in _ensure_list(raw.get("storage")):
        parts.append(_str(disk.get("FriendlyName")))

    # Drop blanks, sort for determinism, join
    parts = sorted(p for p in parts if p)
    if not parts:
        return ""

    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ── Public API ───────────────────────────────────────────────────

async def collect_hardware() -> HardwareSnapshot:
    """Collect all hardware specs. Call once at startup."""
    raw = await _run_powershell(_PS_SCRIPT)
    if not raw:
        logger.warning("Hardware collection returned empty — using defaults")
        return HardwareSnapshot()

    gpus, displays = _parse_gpus(raw.get("gpu"))

    snapshot = HardwareSnapshot(
        device_type=_parse_device_type(raw.get("chassis")),
        cpu=_parse_cpu(raw.get("cpu")),
        gpus=gpus,
        ram=_parse_ram(raw.get("ram")),
        storage=_parse_storage(raw.get("storage")),
        motherboard=_parse_motherboard(raw.get("motherboard")),
        os=_parse_os(raw.get("os")),
        displays=displays,
        audio_devices=_parse_audio(raw.get("audio")),
        input_devices=_parse_input_devices(raw.get("keyboards"), raw.get("mice")),
        hwid_hash=_compute_hwid(raw),
    )

    logger.info(
        "Hardware collected: %s | %s | %.1f GB RAM | HWID=%s…",
        snapshot.cpu.model,
        snapshot.gpus[0].model if snapshot.gpus else "no GPU",
        snapshot.ram.total_gb,
        snapshot.hwid_hash[:12],
    )
    return snapshot


if __name__ == "__main__":
    import asyncio as _asyncio

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    async def _main() -> None:
        snap = await collect_hardware()
        print(json.dumps(snap.to_dict(), indent=2, ensure_ascii=False))

    _asyncio.run(_main())
