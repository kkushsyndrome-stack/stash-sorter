"""Where Stash Sorter keeps its own files (never inside the save folder or the game install)."""

import os
import sys
from pathlib import Path


def app_dir():
    """Folder next to the program: the project folder when run from source, the .exe's folder when packaged."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir():
    """Bundled read-only resources (web page, default rules, game data snapshot)."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "stash_sorter" if base else Path(__file__).resolve().parent


def backups_dir():
    return app_dir() / "backups"


def rules_file():
    return app_dir() / "rules.json"


def cache_dir():
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "StashSorter" / "cache"
