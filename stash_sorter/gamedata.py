"""Game data tables needed to parse and describe items.

Sources, in order of preference:
  1. an explicit data directory (e.g. a mod's extracted data/global/excel folder),
  2. the extracted tables inside the D2R install (Data/global/excel), found automatically,
  3. the snapshot bundled with Stash Sorter (stash_sorter/data/gamedata.json.gz).

Nothing in the install folder is ever written to.
"""

import gzip
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

BUNDLE_PATH = Path(__file__).parent / "data" / "gamedata.json.gz"

DEFAULT_INSTALL_DIRS = [
    r"C:\Program Files (x86)\Diablo II Resurrected",
    r"C:\Program Files\Diablo II Resurrected",
    r"D:\Program Files (x86)\Diablo II Resurrected",
    r"D:\Games\Diablo II Resurrected",
    r"C:\Games\Diablo II Resurrected",
]

_ITEM_COLS = ["code", "name", "namestr", "type", "type2", "invwidth", "invheight", "stackable", "maxstack",
              "normcode", "ubercode", "ultracode", "levelreq", "gemsockets", "quest", "questdiffcheck",
              "compactsave", "level", "magic lvl"]
_AFFIX_COLS = ["Name", "spawnable", "rare", "level", "maxlevel", "group", "mod1code", "mod1param", "mod1min", "mod1max"] + \
              [f"itype{i}" for i in range(1, 8)] + [f"etype{i}" for i in range(1, 6)]

# table name -> columns kept in the bundled snapshot
TABLE_COLUMNS = {
    "itemstatcost": ["Stat", "*ID", "Save Bits", "Save Add", "Save Param Bits", "CSvBits", "CSvParam", "op", "op param",
                     "descpriority", "descfunc", "descval", "descstrpos", "descstrneg", "descstr2",
                     "dgrp", "dgrpfunc", "dgrpval", "dgrpstrpos", "dgrpstrneg", "dgrpstr2"],
    "armor": _ITEM_COLS,
    "weapons": _ITEM_COLS,
    "misc": _ITEM_COLS,
    "itemtypes": ["Code", "ItemType", "Equiv1", "Equiv2", "MaxSockets1", "MaxSocketsLevelThreshold1",
                  "MaxSockets2", "MaxSocketsLevelThreshold2", "MaxSockets3"],
    "uniqueitems": ["index", "*ID", "code", "carry1", "disabled", "spawnable", "lvl"],
    "setitems": ["index", "*ID", "set", "item", "disabled", "spawnable", "lvl"],
    "skills": ["skill", "*Id", "charclass", "skilldesc"],
    "skilldesc": ["skilldesc", "str name"],
    "charstats": ["class", "StrAllSkills", "StrSkillTab1", "StrSkillTab2", "StrSkillTab3", "StrClassOnly"],
    "monstats": ["Id", "*hcIdx", "NameStr"],
    "runes": ["Name", "*Rune Name", "Rune1", "Rune2", "Rune3", "Rune4", "Rune5", "Rune6"],
    "magicprefix": _AFFIX_COLS,
    "magicsuffix": _AFFIX_COLS,
    "rareprefix": ["name"],
    "raresuffix": ["name"],
}
STRING_FILES = ("item-names.json", "item-runes.json", "item-gems.json", "item-nameaffixes.json",
                "item-modifiers.json", "skills.json", "monsters.json")
# strings referenced from code rather than from a table column
EXTRA_STRING_KEYS = ("strModAllResistances", "strModFireDamageRange", "strModFireDamage", "strModColdDamageRange",
                     "strModColdDamage", "strModLightningDamageRange", "strModLightningDamage",
                     "strModPoisonDamageRange", "strModPoisonDamage", "strModMagicDamageRange", "strModMagicDamage",
                     "strModMinDamageRange", "strModMinDamage", "strModEnhancedDamage", "Moditem2allattrib")


def _registry_install_dirs():
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found = []
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Diablo II Resurrected", "InstallLocation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Diablo II Resurrected", "InstallLocation"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Blizzard Entertainment\Diablo II Resurrected", "InstallPath"),
    ]
    for hive, path, value in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                v, _ = winreg.QueryValueEx(k, value)
                if v:
                    found.append(v)
        except OSError:
            pass
    return found


def find_install_dir(explicit=None):
    """The D2R install folder, or None. Only used for reading extracted tables."""
    candidates = ([explicit] if explicit else []) + [os.environ.get("D2R_INSTALL_DIR")] + \
        _registry_install_dirs() + DEFAULT_INSTALL_DIRS
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def find_excel_dir(install_dir=None, data_dir=None):
    """Folder holding extracted itemstatcost.txt etc., or None."""
    if data_dir:
        d = Path(data_dir)
        for cand in (d, d / "global" / "excel", d / "data" / "global" / "excel", d / "Data" / "global" / "excel"):
            if (cand / "itemstatcost.txt").is_file():
                return cand
        raise FileNotFoundError(f"No itemstatcost.txt found under {d}")
    root = find_install_dir(install_dir)
    if root and (root / "Data" / "global" / "excel" / "itemstatcost.txt").is_file():
        return root / "Data" / "global" / "excel"
    return None


def read_table(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        cells += [""] * (len(header) - len(cells))
        rows.append(dict(zip(header, cells)))
    return rows


def _strings_dir_for(excel_dir):
    # .../Data/global/excel -> .../Data/local/lng/strings (same layout in mods: data/local/lng/strings)
    data_root = excel_dir.parent.parent
    for cand in (data_root / "local" / "lng" / "strings",):
        if cand.is_dir():
            return cand
    return None


def load_tables(install_dir=None, data_dir=None, bundled_only=False):
    """Returns (tables, strings, source_description)."""
    excel = None if bundled_only else find_excel_dir(install_dir, data_dir)
    if excel is None:
        if not BUNDLE_PATH.is_file():
            raise FileNotFoundError(
                "No extracted D2R data found and no bundled snapshot available. "
                "Point --data-dir at an extracted data/global/excel folder.")
        with gzip.open(BUNDLE_PATH, "rt", encoding="utf-8") as f:
            b = json.load(f)
        return b["tables"], b["strings"], f"bundled snapshot ({b.get('game_version', 'unknown version')})"

    tables = {}
    for name, cols in TABLE_COLUMNS.items():
        path = excel / f"{name}.txt"
        rows = read_table(path) if path.is_file() else []
        tables[name] = [{c: r.get(c, "") for c in cols} for r in rows]
    strings = {}
    sdir = _strings_dir_for(excel)
    if sdir:
        for fname in STRING_FILES:
            p = sdir / fname
            if p.is_file():
                with open(p, encoding="utf-8-sig") as f:
                    for e in json.load(f):
                        if e.get("Key") is not None:
                            strings.setdefault(e["Key"], e.get("enUS", ""))
    return tables, strings, f"extracted tables at {excel}"


def build_bundle(out_path=BUNDLE_PATH, install_dir=None, data_dir=None, game_version=""):
    """Write a compact snapshot of the tables so Stash Sorter works without extracted game files."""
    excel = find_excel_dir(install_dir, data_dir)
    if excel is None:
        raise FileNotFoundError("Need extracted tables to build a snapshot")
    tables, strings, _ = load_tables(install_dir, data_dir)
    wanted = set()
    for t in ("armor", "weapons", "misc"):
        wanted |= {r["namestr"] for r in tables[t]}
    for t, col in (("uniqueitems", "index"), ("setitems", "index"), ("setitems", "set"), ("runes", "Name"),
                   ("magicprefix", "Name"), ("magicsuffix", "Name"), ("rareprefix", "name"), ("raresuffix", "name"),
                   ("skilldesc", "str name"), ("monstats", "NameStr"), ("charstats", "StrAllSkills"),
                   ("charstats", "StrSkillTab1"), ("charstats", "StrSkillTab2"), ("charstats", "StrSkillTab3"),
                   ("charstats", "StrClassOnly"), ("itemstatcost", "descstrpos"), ("itemstatcost", "descstrneg"),
                   ("itemstatcost", "descstr2"), ("itemstatcost", "dgrpstrpos"), ("itemstatcost", "dgrpstrneg"),
                   ("itemstatcost", "dgrpstr2")):
        wanted |= {r[col] for r in tables[t]}
    wanted |= set(EXTRA_STRING_KEYS)
    strings = {k: v for k, v in strings.items() if k in wanted}
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump({"game_version": game_version, "tables": tables, "strings": strings}, f, separators=(",", ":"))
    return out_path


def _int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


@dataclass
class StatDef:
    id: int
    name: str
    save_bits: int
    save_add: int
    save_param_bits: int
    csv_bits: int
    csv_param: int


@dataclass
class BaseItem:
    code: str
    name: str
    kind: str  # armor | weapon | misc
    type: str
    type2: str
    width: int
    height: int
    stackable: bool
    max_stack: int
    tier: str  # normal | exceptional | elite | ""
    level_req: int
    max_sockets: int
    namestr: str
    quest: bool
    quest_diff: bool = False
    compact: bool = False
    qlvl: int = 0
    magic_lvl: int = 0
    ancestors: frozenset = frozenset()

    def has(self, *type_codes):
        return any(t in self.ancestors for t in type_codes)


@dataclass
class GameData:
    tables: dict = field(default_factory=dict)
    source: str = ""
    stats: dict = field(default_factory=dict)
    items: dict = field(default_factory=dict)
    item_types: dict = field(default_factory=dict)  # code -> (equiv1, equiv2, name)
    uniques: dict = field(default_factory=dict)  # id -> (name, base code, carry-one group, table key)
    set_items: dict = field(default_factory=dict)  # id -> (name, set name, base code, table key)
    strings: dict = field(default_factory=dict)

    def type_ancestors(self, type_code):
        """All item type codes `type_code` belongs to (itself plus Equiv chains)."""
        seen, todo = set(), [type_code]
        while todo:
            t = todo.pop()
            if not t or t in seen:
                continue
            seen.add(t)
            eq = self.item_types.get(t)
            if eq:
                todo.extend(eq[:2])
        return seen

    def text(self, key, fallback=None):
        return self.strings.get(key, fallback if fallback is not None else key)


def load(install_dir=None, data_dir=None, bundled_only=False):
    tables, strings, source = load_tables(install_dir, data_dir, bundled_only)
    gd = GameData(tables=tables, source=source, strings=strings)

    for r in tables["itemstatcost"]:
        if r.get("*ID", "") == "":
            continue
        sid = int(r["*ID"])
        gd.stats[sid] = StatDef(
            id=sid, name=r["Stat"], save_bits=_int(r["Save Bits"]), save_add=_int(r["Save Add"]),
            save_param_bits=_int(r["Save Param Bits"]), csv_bits=_int(r["CSvBits"]), csv_param=_int(r["CSvParam"]))

    for r in tables["itemtypes"]:
        if r.get("Code"):
            gd.item_types[r["Code"]] = (r.get("Equiv1", ""), r.get("Equiv2", ""), r.get("ItemType", ""))

    for kind, tname in (("armor", "armor"), ("weapon", "weapons"), ("misc", "misc")):
        for r in tables[tname]:
            code = r.get("code", "").strip()
            if not code or code in gd.items:
                continue
            tier = ""
            if kind != "misc":
                tier = {r.get("normcode"): "normal", r.get("ubercode"): "exceptional",
                        r.get("ultracode"): "elite"}.get(code, "")
            namestr = r.get("namestr") or code
            gd.items[code] = BaseItem(
                code=code, name=gd.strings.get(namestr, r.get("name", code)), kind=kind,
                type=r.get("type", ""), type2=r.get("type2", ""),
                width=_int(r.get("invwidth"), 1), height=_int(r.get("invheight"), 1),
                stackable=r.get("stackable") == "1", max_stack=_int(r.get("maxstack")), tier=tier,
                level_req=_int(r.get("levelreq")), max_sockets=_int(r.get("gemsockets")), namestr=namestr,
                quest=_int(r.get("quest")) > 0, quest_diff=_int(r.get("questdiffcheck")) > 0,
                compact=_int(r.get("compactsave")) > 0, qlvl=_int(r.get("level")), magic_lvl=_int(r.get("magic lvl")))

    for base in gd.items.values():
        anc = gd.type_ancestors(base.type)
        if base.type2:
            anc |= gd.type_ancestors(base.type2)
        base.ancestors = frozenset(anc)

    for r in tables["uniqueitems"]:
        if r.get("*ID", "") != "":
            key = r["index"]
            gd.uniques[int(r["*ID"])] = (gd.strings.get(key, key), r.get("code", ""), _int(r.get("carry1")), key)

    for r in tables["setitems"]:
        if r.get("*ID", "") != "":
            key, set_key = r["index"], r.get("set", "")
            gd.set_items[int(r["*ID"])] = (gd.strings.get(key, key), gd.strings.get(set_key, set_key), r.get("item", ""), key)

    return gd
