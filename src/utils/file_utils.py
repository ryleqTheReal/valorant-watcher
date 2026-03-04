from functools import cache
from pathlib import Path
import platform
import logging

logger: logging.Logger = logging.getLogger(__name__)

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
      
@cache  
def get_config_path() -> Path:
    """Returns a Path object to the app config json

    Raises:
        FileNotFoundError: The config file does not exist

    Returns:
        Path: The path object to the config file
    """
    config_path: Path = Path(__file__).resolve().parents[2] / "config.json"
    if not config_path.exists():
        logger.error("The config file does not exist, cannot launch properly.")
        raise FileNotFoundError("The config file does not exist, cannot launch properly.")
    return config_path