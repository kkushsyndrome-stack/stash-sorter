"""Discovers and loads every character and shared stash in a save folder."""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog
from .savefiles import parse_character, parse_stash, SaveFormatError

_FOLDERID_SAVED_GAMES = "{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}"


def _known_saved_games_dir():
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

        guid = GUID()
        ctypes.oledll.ole32.CLSIDFromString(_FOLDERID_SAVED_GAMES, ctypes.byref(guid))
        path_ptr = ctypes.c_wchar_p()
        ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
        path = path_ptr.value
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return Path(path) if path else None
    except Exception:
        return None


def find_save_dir(explicit=None):
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_dir():
            raise FileNotFoundError(f"Save folder not found: {p}")
        return p
    candidates = []
    env = os.environ.get("D2R_SAVE_DIR")
    if env:
        candidates.append(Path(env))
    known = _known_saved_games_dir()
    if known:
        candidates.append(known / "Diablo II Resurrected")
    candidates.append(Path.home() / "Saved Games" / "Diablo II Resurrected")
    for c in candidates:
        if c.is_dir():
            return c
    raise FileNotFoundError("Could not find the D2R save folder; pass --saves <folder>.")


@dataclass
class World:
    save_dir: Path
    gd: object
    names: object
    characters: list = field(default_factory=list)
    stashes: list = field(default_factory=list)
    errors: list = field(default_factory=list)  # (file name, message)
    originals: dict = field(default_factory=dict)  # path -> bytes as loaded

    def character(self, name):
        for c in self.characters:
            if c.name.lower() == name.lower() or c.path.stem.lower() == name.lower():
                return c
        return None

    def stash(self, file_name):
        for s in self.stashes:
            if s.path.name.lower() == file_name.lower():
                return s
        return None

    def default_stash(self):
        """The stash most characters can use (ties: softcore, then more items)."""
        def users(s):
            v = s.tabs[0].version
            return sum(1 for c in self.characters if c.version == v and c.era == s.era and c.hardcore == s.hardcore)
        return max(self.stashes, default=None,
                   key=lambda s: (users(s), not s.hardcore, sum(len(t.items) for t in s.normal_tabs())))

    def changed_on_disk(self):
        return [p for p, data in self.originals.items() if not p.is_file() or p.read_bytes() != data]


def load_world(save_dir, gd):
    w = World(save_dir=Path(save_dir), gd=gd, names=catalog.load_names(gd))
    for p in sorted(w.save_dir.glob("*.d2s")):
        data = p.read_bytes()
        try:
            w.characters.append(parse_character(p, gd, data))
            w.originals[p] = data
        except (SaveFormatError, Exception) as e:  # one bad file must not hide the rest
            w.errors.append((p.name, str(e)))
    for p in sorted(w.save_dir.glob("*.d2i")):
        data = p.read_bytes()
        try:
            w.stashes.append(parse_stash(p, gd, data))
            w.originals[p] = data
        except (SaveFormatError, Exception) as e:
            w.errors.append((p.name, str(e)))
    return w


def mule_status(ch, stash, max_level):
    """(eligible, reason) for using `ch` as a mule for items from `stash`."""
    version = stash.tabs[0].version
    if ch.version != version:
        return False, (f"save format v{ch.version} differs from the stash (v{version}); "
                       "log in with this character once so the game upgrades it")
    if ch.hardcore != stash.hardcore:
        return False, "hardcore/softcore mismatch"
    if ch.era != stash.era:
        return False, f"{ch.era_name} character can't use the {('RotW' if stash.modern else 'Resurrected')} stash"
    if ch.level > max_level:
        return False, f"level {ch.level} (above mule level limit {max_level})"
    return True, "mule"
