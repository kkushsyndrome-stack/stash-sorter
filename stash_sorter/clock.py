"""Terror zone clock: pick a terror zone and move the PC clock to its most recent session.

In single-player/offline D2R the active terror zone follows a fixed schedule based on the PC's clock, so moving the
clock to a session start makes that zone active. Ported from the D2R Terror Zone Clock app.

Only setting the clock needs administrator rights: Stash Sorter itself runs normally and asks Windows (UAC) to run
a tiny elevated helper (`set-clock`) for each change. The real time comes from time.windows.com (SNTP).
Schedule: https://d2emu.com/data/tz-2023-localized.json (cached for offline use).
"""

import ctypes
import datetime as dt
import json
import os
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

from . import paths

SCHEDULE_URL = "https://d2emu.com/data/tz-2023-localized.json"
NTP_HOST = "time.windows.com"
OLD_APP_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "D2RTerrorZoneClock"
SHIFT_TOLERANCE = 5  # seconds of difference that still count as "the clock is right"

# Zone order as listed on d2emu.com/tz-sp
ZONES = [
    "Blood Moor, Den of Evil", "Burial Grounds, Crypt, Mausoleum", "Stony Field, Tristram", "Cold Plains, Cave",
    "Dark Wood, Underground Passage", "Black Marsh, The Hole, Forgotten Tower",
    "Tamoe Highland, Outer Cloister, Pit, Monastery Gate", "Barracks, Jail", "Cathedral, Catacombs, Inner Cloister",
    "The Secret Cow Level", "Lut Gholein Sewers", "Rocky Waste, Stony Tomb", "Dry Hills, Halls of the Dead",
    "Far Oasis, Maggot Lair", "Lost City, Valley of Snakes, Claw Viper Temple, Ancient Tunnels",
    "Arcane Sanctuary, Harem, Palace Cellar", "Tal Rasha's Tombs, Tal Rasha's Chamber, Canyon of the Magi",
    "Spider Forest, Arachnid Lair, Spider Cavern", "Great Marsh", "Flayer Jungle, Flayer Dungeon, Swampy Pit",
    "Kurast Bazaar, Lower Kurast, Upper Kurast, Kurast Causeway, Kurast Sewers, Ruined Temple, Disused Fane, "
    "Forgotten Reliquary, Forgotten Temple, Ruined Fane, Disused Reliquary",
    "Travincal", "Durance of Hate", "Outer Steppes, Plains of Despair", "City of the Damned, River of Flame",
    "Chaos Sanctuary", "Bloody Foothills, Frigid Highlands, Abaddon", "Frozen Tundra, Infernal Pit",
    "Arreat Plateau, Pit of Acheron", "Crystalline Passage, Frozen River", "Nihlathak's Temple, Temple Halls",
    "Glacial Trail, Drifter Cavern", "Ancient's Way, Icy Cellar",
    "Worldstone Keep, Throne of Destruction, Worldstone Chamber",
]
IMMUNITY_NAMES = {"c": "Cold", "f": "Fire", "l": "Lightning", "p": "Poison", "m": "Magic", "ph": "Physical"}


class ClockError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- real time
def parse_ntp(packet):
    """UTC datetime from an SNTP response packet."""
    if len(packet) < 48:
        raise ClockError("short NTP reply")
    seconds, fraction = struct.unpack("!II", packet[40:48])
    if seconds == 0:
        raise ClockError("empty NTP reply")
    epoch = dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)
    return epoch + dt.timedelta(seconds=seconds + fraction / 2 ** 32)


def ntp_utc(host=NTP_HOST, timeout=3.0):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        data, _ = s.recvfrom(512)
    return parse_ntp(data)


# ---------------------------------------------------------------------------------------------- setting the clock
class _SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint16) for n in ("wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute",
                                                 "wSecond", "wMilliseconds")]


def set_local_time(local):
    """Set the Windows clock (needs administrator rights). Returns 0 or the Win32 error code."""
    st = _SYSTEMTIME(local.year, local.month, 0, local.day, local.hour, local.minute, local.second,
                     local.microsecond // 1000)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return 0 if kernel32.SetLocalTime(ctypes.byref(st)) else ctypes.get_last_error()


def _helper_command():
    """(program, arguments, working folder) that runs `set-clock` in this same Stash Sorter."""
    if getattr(sys, "frozen", False):
        return sys.executable, "", str(paths.app_dir())
    return sys.executable, "-m stash_sorter", str(paths.app_dir())


def helper_main(when, real_epoch=None, system_epoch=None):
    """Body of the elevated `set-clock` helper. `when` is a local ISO time, or "real" to restore the real time
    (fetched from time.windows.com right now; otherwise extrapolated from the real/PC times passed in)."""
    if when == "real":
        try:
            real = ntp_utc()
        except (OSError, ClockError):
            if real_epoch is None or system_epoch is None:
                return 1
            real = dt.datetime.fromtimestamp(real_epoch + (time.time() - system_epoch), dt.timezone.utc)
        local = real.astimezone().replace(tzinfo=None)
    else:
        local = dt.datetime.fromisoformat(when)
    return set_local_time(local)


def set_clock_elevated(when, timeout_ms=120000):
    """Ask Windows (UAC) to run the elevated set-clock helper; raises ClockError if it didn't work.
    `when` is a local datetime, or ("real", real_epoch, system_epoch) to restore the real time."""
    if sys.platform != "win32":
        raise ClockError("Changing the clock is only supported on Windows.")

    class SHELLEXECUTEINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.c_void_p),
                    ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                    ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.c_void_p),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.c_void_p),
                    ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p), ("hProcess", ctypes.c_void_p)]

    program, prefix, folder = _helper_command()
    stamp = " ".join(str(x) for x in when) if isinstance(when, tuple) else when.strftime("%Y-%m-%dT%H:%M:%S")
    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb, info.lpFile = "runas", program
    info.lpParameters = f"{prefix} set-clock {stamp}".strip()
    info.lpDirectory, info.nShow = folder, 0  # SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:
            raise ClockError("You declined the Windows permission prompt, so the clock wasn't changed.")
        raise ClockError(f"Windows couldn't start the clock helper (error {err}).")
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        kernel32.CloseHandle(info.hProcess)
    if code.value != 0:
        raise ClockError(f"Windows couldn't set the clock (error {code.value}).")


# ---------------------------------------------------------------------------------------------- schedule
def parse_schedule(text):
    sessions = []
    for entry in json.loads(text):
        zone = (entry.get("zone") or {}).get("enUS")
        if not zone or not entry.get("datetime"):
            continue
        sessions.append({
            "start": dt.datetime.fromisoformat(entry["datetime"].replace("Z", "+00:00")).astimezone(dt.timezone.utc),
            "zone": zone,
            "immunities": list(dict.fromkeys(IMMUNITY_NAMES.get(i, i) for i in entry.get("immunities") or [])),
            "boss_packs": entry.get("numBossPacks") or [],
            "superuniques": entry.get("superuniques") or [],
        })
    sessions.sort(key=lambda s: s["start"])
    return sessions


class TerrorClock:
    def __init__(self, cache_dir=None, settings_file=None, clock=None):
        self.cache_file = Path(cache_dir or paths.cache_dir()) / "tz-schedule.json"
        self.settings_file = Path(settings_file or paths.app_dir() / "terrorzone.json")
        self.sessions, self.session_length = [], dt.timedelta(minutes=30)
        self.source = ""
        self.offset = dt.timedelta(0)  # real time - PC time
        self.offset_known = False
        self.system_utc = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.settings = self._load_settings()
        if self.settings.get("offset_seconds"):
            self.offset = dt.timedelta(seconds=self.settings["offset_seconds"])

    # ---------- settings
    def _load_settings(self):
        if self.settings_file.is_file():
            try:
                return json.loads(self.settings_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        s = {"favourites": [], "zone": ZONES[0], "auto_revert": True, "offset_seconds": 0}
        old = OLD_APP_DIR / "settings.txt"  # carry over choices from the stand-alone Terror Zone Clock
        if old.is_file():
            for line in old.read_text(encoding="utf-8-sig", errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key == "zone" and value:
                    s["zone"] = value
                elif key == "favourites" and value:
                    s["favourites"] = [v for v in value.split("|") if v]
                elif key == "autoRevert":
                    s["auto_revert"] = value != "0"
        return s

    def save_settings(self):
        self.settings["offset_seconds"] = int(self.offset.total_seconds()) if self.shifted else 0
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        self.settings_file.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")

    # ---------- time
    def measure_offset(self):
        """Compare the PC clock with time.windows.com. Returns True if the real time could be fetched."""
        try:
            real = ntp_utc()
        except (OSError, ClockError):
            return False
        self.offset = real - self.system_utc()
        if abs(self.offset.total_seconds()) <= SHIFT_TOLERANCE:
            self.offset = dt.timedelta(0)
        self.offset_known = True
        return True

    def real_utc(self):
        return self.system_utc() + self.offset

    def real_local(self):
        return self.real_utc().astimezone().replace(tzinfo=None)

    @property
    def shifted(self):
        return abs(self.offset.total_seconds()) > SHIFT_TOLERANCE

    # ---------- schedule
    def load_schedule(self, download=True):
        text, source = None, ""
        if download:
            try:
                req = urllib.request.Request(SCHEDULE_URL, headers={"User-Agent": "StashSorter"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    text = r.read().decode("utf-8-sig")
                parse_schedule(text)
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(text, encoding="utf-8")
                source = "downloaded from d2emu.com"
            except Exception:
                text = None
        if text is None:
            for cand in (self.cache_file, OLD_APP_DIR / "tz-schedule.json"):
                if cand.is_file():
                    text = cand.read_text(encoding="utf-8-sig")
                    saved = dt.datetime.fromtimestamp(cand.stat().st_mtime).strftime("%d %b %Y")
                    source = f"offline: using the schedule saved on {saved}"
                    break
        if text is None:
            raise ClockError("Couldn't download the terror zone schedule and there's no saved copy yet.")
        self.sessions = parse_schedule(text)
        if len(self.sessions) >= 2:
            self.session_length = self.sessions[1]["start"] - self.sessions[0]["start"]
        self.source = source
        return self.sessions

    def zones(self):
        names = list(ZONES)
        for s in self.sessions:
            if s["zone"] not in names:
                names.append(s["zone"])
        fav = self.settings.get("favourites", [])
        return [z for z in names if z in fav] + [z for z in names if z not in fav]

    def current(self, now=None):
        now = now or self.real_utc()
        for i, s in enumerate(self.sessions):
            if s["start"] <= now < s["start"] + self.session_length:
                return i
        return None

    def zone_session(self, zone, now=None):
        """The zone's most recent session (which may be active now), or its next one."""
        now = now or self.real_utc()
        mine = [s for s in self.sessions if s["zone"] == zone]
        past = [s for s in mine if s["start"] <= now]
        if past:
            s = past[-1]
            state = "active" if now < s["start"] + self.session_length else "last"
            return s, state
        future = [s for s in mine if s["start"] > now]
        return (future[0], "next") if future else (None, None)

    def upcoming(self, count=8, now=None):
        now = now or self.real_utc()
        return [s for s in self.sessions if s["start"] + self.session_length > now][:count]

    # ---------- actions
    def set_to(self, zone):
        session, _ = self.zone_session(zone)
        if session is None:
            raise ClockError("That zone isn't in the current schedule.")
        real_before, started = self.real_utc(), time.monotonic()
        set_clock_elevated(session["start"].astimezone().replace(tzinfo=None))
        real_now = real_before + dt.timedelta(seconds=time.monotonic() - started)  # however long UAC took
        self.offset = real_now - self.system_utc()
        self.save_settings()
        return session

    def revert(self):
        real, system = self.real_utc(), self.system_utc()
        set_clock_elevated(("real", real.timestamp(), system.timestamp()))
        self.offset = dt.timedelta(0)
        self.measure_offset()  # double-check against time.windows.com
        self.save_settings()
        return not self.shifted
