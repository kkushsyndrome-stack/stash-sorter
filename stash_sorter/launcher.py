"""D2R launch options: read and change the "Additional command line arguments" Battle.net uses for D2R.

Battle.net keeps them in %APPDATA%\\Battle.net\\Battle.net.config under Games -> osi (D2R's product code) ->
AdditionalLaunchArguments. Battle.net reads the file when it starts and writes its own copy back when it exits,
so changes are only saved while Battle.net is fully closed. Only that one value is changed; the file is backed
up first and everything else in it is verified to be unchanged.
"""

import datetime as dt
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

GAME_CODE = "osi"
KEY = "AdditionalLaunchArguments"
MAX_SEED = 4294967295
BATTLENET_PROCESS, GAME_PROCESS = "battle.net.exe", "d2r.exe"
DEFAULT_BATTLENET = [r"C:\Program Files (x86)\Battle.net\Battle.net.exe", r"C:\Program Files\Battle.net\Battle.net.exe"]

# switches the panel understands; anything else is kept as "other arguments"
FLAGS = {
    "-direct": "read game data from loose files instead of the CASC archive",
    "-txt": "use the .txt data tables (needed with -direct for extracted data and most mods)",
    "-enablerespec": "free respecs in offline play",
    "-resetofflinemaps": "a fresh map for every new offline game",
}
VALUE_FLAGS = {"-seed": "fixed map seed for offline games", "-mod": "load the mod with this name"}

_KEY_VALUE = re.compile('("' + KEY + r'"\s*:\s*)"(?:[^"\\]|\\.)*"')


class LaunchError(Exception):
    pass


def config_path():
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "Battle.net" / "Battle.net.config"


def battlenet_exe():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Battle.net") as k:
                loc, _ = winreg.QueryValueEx(k, "InstallLocation")
                cand = Path(loc) / "Battle.net.exe"
                if cand.is_file():
                    return cand
        except OSError:
            pass
    return next((Path(p) for p in DEFAULT_BATTLENET if Path(p).is_file()), None)


def running_processes():
    """Lower-case image names of the running programs (empty off Windows or if tasklist fails)."""
    if sys.platform != "win32":
        return set()
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split('","')[0].strip('"') for line in out.splitlines() if line.startswith('"')}


def battlenet_running():
    return BATTLENET_PROCESS in running_processes()


def close_program(image, force=False):
    """Ask a program to close, like clicking its X (force=False), or end it as Task Manager does (force=True)."""
    if sys.platform != "win32":
        raise LaunchError("Closing programs is only supported on Windows.")
    args = ["taskkill", "/IM", image] + (["/T", "/F"] if force else [])
    try:
        subprocess.run(args, capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as e:
        raise LaunchError(f"Couldn't close {image}: {e}") from None


def map_seeds(path):
    """Map seeds in a character's .map file, newest first. D2R keeps the last four offline map seeds there:
    a 12, the slot the next seed goes in, then four slots."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    if len(data) != 24:
        return []
    head, nxt, *slots = struct.unpack("<6I", data)
    if head != 12 or nxt > 3:
        return [s for s in slots if s]
    return [s for s in (slots[(nxt - 1 - i) % 4] for i in range(4)) if s]


# ---------------------------------------------------------------------------------------------- arguments
def parse_args(text):
    """Split an argument string into the known switches, a seed, a mod and everything else, plus problems."""
    try:
        tokens = shlex.split(text or "", posix=False)
    except ValueError:
        tokens = (text or "").split()
    out = {"flags": [], "seed": None, "mod": None, "other": [], "problems": []}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        if low in FLAGS:
            if low not in out["flags"]:
                out["flags"].append(low)
        elif low in VALUE_FLAGS:
            value = tokens[i + 1] if i + 1 < len(tokens) and not tokens[i + 1].startswith("-") else None
            if value is not None:
                i += 1
            if low == "-seed":
                if value is not None and value.isdigit() and int(value) <= MAX_SEED:
                    out["seed"] = int(value)
                else:
                    # swallow the rest of a placeholder like "(number goes here no bracket)"
                    while i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                        value = f"{value} {tokens[i + 1]}"
                        i += 1
                    out["problems"].append(f"-seed needs a whole number (0 to {MAX_SEED}), not {value or 'nothing'!r}")
            elif value:
                out["mod"] = value.strip('"')
            else:
                out["problems"].append("-mod needs the name of a mod")
        elif "-" + low in FLAGS:
            out["problems"].append(f"{t!r} is missing its dash: it should be -{low}")
            if "-" + low not in out["flags"]:
                out["flags"].append("-" + low)
        else:
            out["other"].append(t)
        i += 1
    if "-direct" in out["flags"] and "-txt" not in out["flags"]:
        out["problems"].append("-direct is normally used together with -txt")
    if out["seed"] is not None and "-resetofflinemaps" in out["flags"]:
        out["problems"].append("-resetofflinemaps and -seed pull in opposite directions (new maps vs. a fixed map)")
    return out


def build_args(flags=(), seed=None, mod=None, other=""):
    parts = [f for f in ("-direct", "-txt", "-enablerespec", "-resetofflinemaps") if f in flags]
    if mod:
        parts += ["-mod", f'"{mod}"' if " " in mod else mod]
    if seed is not None and seed != "":
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise LaunchError("The seed must be a whole number.") from None
        if not 0 <= seed <= MAX_SEED:
            raise LaunchError(f"The seed must be between 0 and {MAX_SEED}.")
        parts += ["-seed", str(seed)]
    if other and other.strip():
        parts.append(other.strip())
    return " ".join(parts)


def strip_seed(text):
    """The argument string without -seed and its value (anything else is kept exactly as typed)."""
    try:
        tokens = shlex.split(text or "", posix=False)
    except ValueError:
        tokens = (text or "").split()
    kept, i = [], 0
    while i < len(tokens):
        if tokens[i].lower() != "-seed":
            kept.append(tokens[i])
        elif i + 1 < len(tokens) and tokens[i + 1].isdigit():
            i += 1
        else:  # a non-number value (e.g. a leftover placeholder): drop everything up to the next switch
            while i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                i += 1
        i += 1
    return " ".join(kept)


def list_mods(install_dir):
    """Mods installed the standard way: <install>/mods/<name>/<name>.mpq (folder or file)."""
    mods_dir = Path(install_dir) / "mods" if install_dir else None
    if not mods_dir or not mods_dir.is_dir():
        return []
    return sorted(p.name for p in mods_dir.iterdir() if p.is_dir() and (p / f"{p.name}.mpq").exists())


# ---------------------------------------------------------------------------------------------- config file
def read_args(path=None):
    path = Path(path or config_path())
    if not path.is_file():
        raise LaunchError(f"Battle.net settings not found at {path}")
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    game = cfg.get("Games", {}).get(GAME_CODE)
    if game is None:
        raise LaunchError("Battle.net doesn't list Diablo II: Resurrected in its settings yet (open its settings once).")
    return game.get(KEY, "")


def _osi_block(text):
    """(body start, closing-brace position) of D2R's object inside the settings text."""
    m = re.search('"' + GAME_CODE + r'"\s*:\s*\{', text)
    if not m:
        raise LaunchError("Couldn't find Diablo II: Resurrected in the Battle.net settings.")
    depth, j, in_str = 0, m.end() - 1, False
    while j < len(text):
        c = text[j]
        if in_str:
            if c == "\\":
                j += 1  # skip the escaped character
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return m.end(), j
        j += 1
    raise LaunchError("The Battle.net settings file looks damaged.")


def updated_text(text, new_args):
    """The settings text with only D2R's AdditionalLaunchArguments changed (or added)."""
    start, end = _osi_block(text)
    body = text[start:end]
    value = json.dumps(new_args)
    if _KEY_VALUE.search(body):
        body = _KEY_VALUE.sub(lambda m: m.group(1) + value, body, count=1)
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        indent = re.search(r'\n([ \t]+)"', body)
        pad = indent.group(1) if indent else "    "
        line_start = text.rfind("\n", 0, end) + 1
        close_pad = text[line_start:end] if not text[line_start:end].strip() else ""
        kept = body.rstrip()
        sep = "," if kept.strip() else ""
        body = f'{kept}{sep}{newline}{pad}"{KEY}": {value}{newline}{close_pad}'
    new_text = text[:start] + body + text[end:]
    # prove nothing else changed
    old_cfg, new_cfg = json.loads(text), json.loads(new_text)
    if new_cfg["Games"][GAME_CODE].get(KEY) != new_args:
        raise LaunchError("The new settings didn't come out as expected; nothing was saved.")
    new_cfg["Games"][GAME_CODE].pop(KEY, None)
    old_cfg["Games"][GAME_CODE].pop(KEY, None)
    if old_cfg != new_cfg:
        raise LaunchError("Saving would have changed other Battle.net settings; nothing was saved.")
    return new_text


def write_args(new_args, backup_dir, path=None, check_running=True):
    """Save new launch arguments for D2R. Returns the backup file. Raises LaunchError (nothing saved) on problems."""
    path = Path(path or config_path())
    if check_running and battlenet_running():
        raise LaunchError("Battle.net is running. Quit it completely first (right-click its icon next to the clock, "
                          "then Exit), otherwise it overwrites the change when it closes.")
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    new_text = updated_text(text, new_args)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"Battle.net.config-{dt.datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(path, backup)
    data = (b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8")
    tmp = path.with_name(path.name + ".stash-sorter.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if path.read_bytes() != data or read_args(path) != new_args:
        shutil.copy2(backup, path)
        raise LaunchError("The settings didn't read back correctly, so the previous file was put back.")
    return backup


PURPOSE_SUGGESTIONS = ["Cows", "Pit / Tombs", "Countess", "Andariel", "Ancient Tunnels", "Mephisto", "Travincal",
                       "Chaos Sanctuary", "Baal", "Terror zones", "Leveling", "Rune hunting"]


class SeedBook:
    """Favourite map seeds, each with a name, what it's used for and notes. Stored as JSON next to the app."""

    def __init__(self, path):
        self.path = Path(path)
        self.seeds = []
        self._unreadable = False
        if self.path.is_file():
            try:
                seeds = json.loads(self.path.read_text(encoding="utf-8")).get("seeds", [])
                self.seeds = [s for s in seeds if isinstance(s, dict) and {"id", "seed", "name"} <= s.keys()]
            except (OSError, ValueError, AttributeError):
                self._unreadable = True

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._unreadable:  # never overwrite a list we couldn't read: keep it next to the new one
            os.replace(self.path, self.path.with_name(f"{self.path.stem}.unreadable-{int(time.time())}.json"))
            self._unreadable = False
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seeds": self.seeds}, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    @staticmethod
    def _clean(seed, name, purpose, notes):
        try:
            seed = int(str(seed).strip())
        except ValueError:
            raise LaunchError("The seed must be a whole number.") from None
        if not 0 <= seed <= MAX_SEED:
            raise LaunchError(f"The seed must be between 0 and {MAX_SEED}.")
        name = (name or "").strip()
        if not name:
            raise LaunchError("Give the seed a name, e.g. \"Pit next to the waypoint\".")
        return seed, name[:80], (purpose or "").strip()[:60], (notes or "").strip()[:500]

    def save(self, seed, name, purpose="", notes="", seed_id=None):
        seed, name, purpose, notes = self._clean(seed, name, purpose, notes)
        if seed_id:
            entry = self.get(seed_id)
            entry.update(seed=seed, name=name, purpose=purpose, notes=notes)
        else:
            entry = {"id": os.urandom(4).hex(), "seed": seed, "name": name, "purpose": purpose, "notes": notes,
                     "added": dt.date.today().isoformat(), "last_used": None}
            self.seeds.append(entry)
        self._save()
        return entry

    def get(self, seed_id):
        for s in self.seeds:
            if s["id"] == seed_id:
                return s
        raise LaunchError("That favourite seed no longer exists.")

    def delete(self, seed_id):
        self.seeds.remove(self.get(seed_id))
        self._save()

    def mark_used(self, seed_id):
        entry = self.get(seed_id)
        entry["last_used"] = dt.date.today().isoformat()
        self._save()
        return entry

    def purposes(self):
        mine = [s.get("purpose") for s in self.seeds if s.get("purpose")]
        return list(dict.fromkeys(mine + PURPOSE_SUGGESTIONS))


def launch_d2r():
    """Start D2R through Battle.net (which applies the launch arguments)."""
    exe = battlenet_exe()
    if exe is None:
        raise LaunchError("Battle.net isn't installed in the usual place.")
    subprocess.Popen([str(exe), f"--exec=launch {GAME_CODE.upper()}"], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))


# ---------------------------------------------------------------------------------------------- seed run
STAGES = {
    "wait_closed": "Close Battle.net so the seed can be put in",
    "starting": "Starting D2R with the seed",
    "playing": "Play one game with the seed",
    "closing": "The seed is in: closing D2R",
    "wait_closed_after": "Close Battle.net so the seed can be taken off",
    "relaunching": "Starting D2R without the seed",
    "done": "Finished",
    "launch_failed": "D2R did not start",
    "cancelled": "Cancelled",
}
FINAL = ("done", "launch_failed", "cancelled")
RETRY_STARTING = 15
GIVE_UP_STARTING = 90  # seconds without D2R before reporting the launch failure
SETTLE = 6             # seconds to leave D2R after the seed shows up (and after its last save) before closing it
FORCE_AFTER = 20       # seconds to wait for D2R to close normally before ending it
GAME_EXE, BATTLENET_EXE = "D2R.exe", "Battle.net.exe"


class SeedRun:
    """Start D2R once with -seed N so the character's offline map is made from that seed, then take -seed off
    and start the game again normally. Leaving -seed in makes the game's random numbers predictable; the map
    stays because offline characters keep their map.

    Battle.net only reads its settings when it starts and writes its own copy back when it exits, so it has to be
    fully closed before each change: wait_closed -> starting -> playing -> closing -> wait_closed_after -> done.
    tick() looks at what is running and moves on. The run is kept in a file, so one that didn't finish (app closed,
    PC restarted) carries on the next time and -seed is never left behind.

    auto (the default) does the closing too: it ends Battle.net when its settings need changing (closing its window
    only hides it, and an ended Battle.net doesn't write its settings back), notices when the seed reaches a
    character's .map file (D2R writes it when the game is created), closes D2R, takes -seed off and starts D2R again.
    The only thing left to do is load the character and create a game. D2R is never closed before the seed is in.
    """

    def __init__(self, path, saves_dir=None, processes=running_processes, config=None, launch=None,
                 backup_dir=None, now=time.time, closer=close_program, auto=True):
        self.path = Path(path)
        self.saves_dir = Path(saves_dir) if saves_dir else None
        self.processes = processes
        self.config = config  # Battle.net settings file (None = the real one)
        self.launch = launch or launch_d2r
        self.backup_dir = backup_dir
        self.now = now
        self.closer = closer
        self.auto = auto
        self.state = None
        if self.path.is_file():
            try:
                st = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(st, dict) and st.get("stage") in STAGES:
                    self.state = st
            except (OSError, ValueError):
                pass

    @property
    def active(self):
        return bool(self.state) and self.state["stage"] not in FINAL

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def _stage(self, stage, note=None):
        self.state["stage"] = stage
        self.state["since"] = self.now()
        self.state["error"] = None
        if note:
            self.state["notes"].append(note)
        self._save()

    def _snapshot(self, pattern="*.d2s"):
        if not self.saves_dir or not self.saves_dir.is_dir():
            return {}
        return {p.stem: p.stat().st_mtime_ns for p in self.saves_dir.glob(pattern)}

    def _seed_taken(self):
        """The character whose .map file changed since D2R started and now has the seed as its newest map."""
        before = self.state.get("map_snapshot") or {}
        for stem, mtime in self._snapshot("*.map").items():
            if before.get(stem) != mtime and map_seeds(self.saves_dir / f"{stem}.map")[:1] == [self.state["seed"]]:
                return stem
        return None

    def _settled(self):
        """Long enough since the seed showed up, and the character's save hasn't been written for a few seconds."""
        st = self.state
        if self.now() - st["seen_at"] < SETTLE:
            return False
        save = self.saves_dir / f"{st['taken_by']}.d2s"
        try:
            return time.time() - save.stat().st_mtime >= SETTLE
        except OSError:
            return True

    def _end(self, image, force, note):
        try:
            self.closer(image, force=force)
            self.state["notes"].append(note)
        except LaunchError as e:
            self.state["error"] = str(e)
        self._save()

    def start(self, seed, seed_id=None):
        if self.active:
            raise LaunchError("A seed run is already going; finish or cancel it first.")
        try:
            seed = int(str(seed).strip())
        except ValueError:
            raise LaunchError("The seed must be a whole number.") from None
        if not 0 <= seed <= MAX_SEED:
            raise LaunchError(f"The seed must be between 0 and {MAX_SEED}.")
        if self.launch is launch_d2r and battlenet_exe() is None:
            raise LaunchError("Battle.net isn't installed in the usual place, so D2R can't be started.")
        base = strip_seed(read_args(self.config))
        if "-resetofflinemaps" in base.lower().split():
            raise LaunchError("Untick -resetofflinemaps and save first: with it the game makes a new map every game, "
                              "so the seeded map wouldn't stay.")
        notes = []
        if self.auto and self.saves_dir and self.backup_dir:
            from .apply import backup_save_dir  # the run closes D2R by itself: keep a copy of the saves first
            notes.append(f"Saves backed up to {backup_save_dir(self.saves_dir, self.backup_dir, label='before-seed-run').name}")
        self.state = {"seed": seed, "seed_id": seed_id, "base": base, "seeded": f"{base} -seed {seed}".strip(),
                      "relaunch": True, "stage": "wait_closed", "started": self.now(), "since": self.now(),
                      "launched": None, "snapshot": {}, "map_snapshot": {}, "changed": None, "taken_by": None,
                      "seen_at": None, "close_sent": None, "forced": False, "auto": self.auto, "error": None,
                      "launch_attempts": 0, "notes": notes}
        self._save()
        return self.tick()

    def tick(self):
        """Look at what's running and move on as far as possible. Returns the state."""
        if not self.active:
            return self.state
        st = self.state
        try:
            running = self.processes()
            bnet, game = BATTLENET_PROCESS in running, GAME_PROCESS in running
            auto = st.get("auto", False) and self.auto
            if st["stage"] == "wait_closed" and bnet and not game and auto:
                self._end(BATTLENET_EXE, True, "Battle.net closed so the seed can be put in.")
                bnet = BATTLENET_PROCESS in self.processes()
            if st["stage"] == "wait_closed" and not bnet and not game:
                write_args(st["seeded"], self._backups(), path=self.config, check_running=False)
                st["snapshot"] = self._snapshot()
                st["map_snapshot"] = self._snapshot("*.map")
                st["launched"] = self.now()
                self._stage("starting", f"Battle.net now starts D2R with: {st['seeded']}")
                self._launch()
            elif st["stage"] == "starting":
                if game:
                    self._stage("playing")
                elif self.now() - st["launched"] > GIVE_UP_STARTING:
                    st["relaunch"] = False
                    st["launch_failure"] = "D2R didn't start automatically. Close Battle.net to safely remove -seed."
                    self._stage("wait_closed_after", "D2R didn't start with the seed; taking -seed off.")
                    st["error"] = st["launch_failure"]
                elif (st.get("launch_attempts", 0) < 2
                      and self.now() - st["launched"] >= RETRY_STARTING):
                    st["notes"].append("D2R hasn't appeared yet; sending the Battle.net launch request again.")
                    self._launch()
            if st["stage"] == "relaunching":
                if game:
                    self._stage("done", f"D2R started normally without -seed. The seed remains on {st.get('taken_by') or 'the character'}'s map.")
                elif self.now() - st["launched"] > GIVE_UP_STARTING:
                    self._stage("launch_failed", "The seed is off, but D2R did not start automatically.")
                    st["error"] = "The seed is off. D2R did not start after two launch requests; start it from Battle.net."
                elif (st.get("launch_attempts", 0) < 2
                      and self.now() - st["launched"] >= RETRY_STARTING):
                    st["notes"].append("D2R hasn't appeared yet; sending the normal Battle.net launch request again.")
                    self._launch()
            if st["stage"] == "playing" and auto and game and not st.get("taken_by"):
                taken = self._seed_taken()
                if taken:
                    st["taken_by"], st["seen_at"] = taken, self.now()
                    self._stage("closing", f"Seed {st['seed']} is in {taken}'s map.")
            if st["stage"] == "closing" and game:
                if st["close_sent"] is None:
                    if self._settled():
                        st["close_sent"] = self.now()
                        self._end(GAME_EXE, False, "Closing D2R.")
                elif not st["forced"] and self.now() - st["close_sent"] > FORCE_AFTER:
                    st["forced"] = True
                    self._end(GAME_EXE, True, "D2R didn't close by itself: ended it.")
            if st["stage"] in ("playing", "closing") and not game:
                before = st["snapshot"]
                st["changed"] = sorted(n for n, m in self._snapshot().items() if before.get(n) != m)
                if auto and not st.get("taken_by") and self._seed_taken():
                    st["taken_by"] = self._seed_taken()
                if auto and not st.get("taken_by"):
                    st["relaunch"] = False
                    self._stage("wait_closed_after", "D2R closed before a game was created, so the seed didn't take.")
                else:
                    self._stage("wait_closed_after")
                bnet = BATTLENET_PROCESS in self.processes()
            if st["stage"] == "wait_closed_after" and bnet and not game and auto:
                self._end(BATTLENET_EXE, True, "Battle.net closed so the seed can be taken off.")
                bnet = BATTLENET_PROCESS in self.processes()
            if st["stage"] == "wait_closed_after" and not bnet and not game:
                write_args(st["base"], self._backups(), path=self.config, check_running=False)
                if st["relaunch"]:
                    st["launched"] = self.now()
                    st["launch_attempts"] = 0
                    self._stage("relaunching", f"-seed taken off; Battle.net now starts D2R with: {st['base'] or '(nothing)'}")
                    self._launch()
                else:
                    self._stage("cancelled", "-seed taken off again.")
                    if st.get("launch_failure"):
                        st["error"] = st["launch_failure"]
        except (LaunchError, OSError, ValueError) as e:
            st["error"] = str(e)
            self._save()
        return st

    def _launch(self):
        try:
            self.state["launch_attempts"] = self.state.get("launch_attempts", 0) + 1
            self.launch()
        except (LaunchError, OSError) as e:
            self.state["notes"].append(f"Couldn't start D2R: {e}")
            if self.state["stage"] == "starting":  # never ran with the seed: just take it off again
                self.state["relaunch"] = False
                self._stage("wait_closed_after")
            self._save()

    def _backups(self):
        if self.backup_dir is None:
            raise LaunchError("No backup folder set.")
        return self.backup_dir

    def again(self):
        """D2R closed but the seed didn't take (no game entered): start D2R with the seed once more."""
        if not self.active or self.state["stage"] != "wait_closed_after" or not self.state["relaunch"]:
            raise LaunchError("There's nothing to start again right now.")
        self.state["launched"] = self.now()
        self.state["launch_attempts"] = 0
        self._stage("starting")
        self._launch()
        return self.state

    def cancel(self):
        if not self.active:
            return self.state
        if self.state["stage"] == "relaunching":
            self._stage("cancelled", "Cancelled after removing -seed; start D2R manually when ready.")
            return self.state
        if self.state["stage"] == "wait_closed":  # nothing was changed yet
            self._stage("cancelled", "Nothing was changed.")
        else:
            self.state["relaunch"] = False
            self._stage("wait_closed_after", "Cancelled: -seed comes off as soon as Battle.net is closed.")
        return self.tick()

    def dismiss(self):
        if self.active:
            raise LaunchError("The seed run hasn't finished yet.")
        self.state = None
        self.path.unlink(missing_ok=True)

    def view(self):
        if not self.state:
            return None
        return {**{k: v for k, v in self.state.items() if k not in ("snapshot", "map_snapshot")}, "active": self.active,
                "title": STAGES[self.state["stage"]], "elapsed": int(self.now() - self.state["since"])}
