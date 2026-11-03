"""Release identity and writable runtime paths."""
import os
from pathlib import Path
import sys

VERSION = "0.1.0-alpha.1"
REPOSITORY = "LoidInfinity/Reconnect"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "NetworkGuard" if FROZEN else APP_DIR


def worker_command(config_path: Path) -> list[str]:
    prefix = [sys.executable] if FROZEN else [sys.executable, "-u", str(APP_DIR / "gui.py")]
    return prefix + ["--worker", "--config", str(config_path)]
