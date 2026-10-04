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
import subprocess
import sys
import time
from pathlib import Path

GAME_CODE = "osi"
KEY = "AdditionalLaunchArguments"
MAX_SEED = 4294967295
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


def battlenet_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return '"battle.net.exe"' in out


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
