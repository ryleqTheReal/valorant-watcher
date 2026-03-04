from pathlib import Path
import platform

"""Here we dump all low-level path resolution functinons and anything regarding paths"""

def get_default_lockfile_path() -> Path:
    """Return the platform-dependent default path to the lockfile

    Example:
        path = get_default_lockfile_path()
        lock = LockfileData.from_file(path)
    
    Returns:
        Path: The `Path` object to the lockfile
    """
    
    system = platform.system()
    if system == "Windows":
        # TESTED and confirmed
        return (
            Path.home()
            / "AppData" / "Local"
            / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        )
    elif system == "Darwin":
        # UNTESTED!
        return (
            Path.home()
            / "Library" / "Application Support"
            / "Riot Games" / "Riot Client" / "Config" / "lockfile"
        )
    else:
        # UNTESTED!
        return (
            Path.home()
            / ".local" / "share"
            / "riot-games" / "lockfile"
        )