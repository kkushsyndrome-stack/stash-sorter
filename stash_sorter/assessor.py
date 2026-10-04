"""Item level assessment: what an item's ilvl lets it (or a craft/reroll made from it) roll.

All thresholds come from the game's own affix tables, so they stay correct across patches.

  affix level (alvl): if ilvl < 99 - qlvl/2: alvl = ilvl - qlvl/2, else alvl = 2*ilvl - 99
  crafted item ilvl:  floor(character level / 2) + floor(ingredient ilvl / 2)
"""

from dataclasses import dataclass

from .gamedata import _int

CLASS_CODES = {"ama": "Amazon", "sor": "Sorceress", "nec": "Necromancer", "pal": "Paladin",
               "bar": "Barbarian", "dru": "Druid", "ass": "Assassin", "war": "Warlock"}

_MOD_LABELS = {
    "res-all": "{} All Resistances", "hp": "+{} Life", "mana": "+{} Mana", "str": "+{} Strength",
    "dex": "+{} Dexterity", "lifesteal": "{}% Life Stolen", "manasteal": "{}% Mana Stolen",
    "mag%": "{}% Magic Find", "gold%": "{}% Extra Gold", "dmg-min": "+{} Min Damage", "dmg-max": "+{} Max Damage",
    "att": "+{} Attack Rating", "ac": "+{} Defense", "regen": "Replenish Life +{}", "dmg%": "+{}% Enhanced Damage",
    "cast1": "{}% Faster Cast Rate", "cast2": "{}% Faster Cast Rate", "cast3": "{}% Faster Cast Rate",
    "swing1": "{}% IAS", "swing2": "{}% IAS", "swing3": "{}% IAS", "move1": "{}% FRW", "move2": "{}% FRW",
    "move3": "{}% FRW", "balance1": "{}% FHR", "balance2": "{}% FHR", "balance3": "{}% FHR",
    "red-dmg": "Damage Reduced by {}", "red-mag": "Magic Damage Reduced by {}", "allskills": "+{} All Skills",
    "skilltab": "+{} to a Skill Tab", "res-fire": "{}% Fire Res", "res-cold": "{}% Cold Res",
    "res-ltng": "{}% Lightning Res", "res-pois": "{}% Poison Res", "dmg-pois": "Poison Damage",
    "fire-min": "Fire Damage", "cold-min": "Cold Damage", "ltng-min": "Lightning Damage", "cold-len": "Cold Damage",
    "charged": "Skill Charges", "att-skill": "Chance to Cast",
}

NOTABLE_MIN_LEVEL = 45

# Mod codes (magicprefix/magicsuffix mod1code) that make a magic item worth keeping when rolled at the best tier.
VALUABLE_MODS = tuple(CLASS_CODES) + (
    "skilltab", "allskills", "cast1", "cast2", "cast3", "balance1", "balance2", "balance3", "move1", "move2",
    "move3", "res-all", "hp", "mana", "mag%", "lifesteal", "manasteal", "dmg%", "att", "dmg-max", "dmg-min",
    "red-dmg", "ac%")


@dataclass
class Affix:
    name: str
    level: int
    max_level: int
    rare: bool
    itypes: tuple
    etypes: tuple
    desc: str
    group: str


def _describe(r):
    code = r.get("mod1code", "")
    lo, hi = r.get("mod1min", ""), r.get("mod1max", "")
    rng = lo if lo == hi or not hi else f"{lo}-{hi}"
    if code in CLASS_CODES:
        return f"+{rng} to Class Skills (any class)"
    fmt = _MOD_LABELS.get(code)
    if fmt:
        return fmt.format(rng) if "{}" in fmt else fmt
    return f"{code} {rng}".strip()


def load_affixes(gd):
    out = []
    for table in ("magicprefix", "magicsuffix"):
        for r in gd.tables[table]:
            if r.get("spawnable") != "1" or not r.get("level"):
                continue
            name = gd.text(r["Name"], r["Name"])
            if r.get("mod1code") in CLASS_CODES:
                name = "Class prefix"
            out.append(Affix(
                name=name,
                level=_int(r.get("level")),
                max_level=_int(r.get("maxlevel"), 999) or 999,
                rare=r.get("rare") == "1",
                itypes=tuple(x for x in (r.get(f"itype{i}", "") for i in range(1, 8)) if x),
                etypes=tuple(x for x in (r.get(f"etype{i}", "") for i in range(1, 6)) if x),
                desc=_describe(r),
                group="class" if r.get("mod1code") in CLASS_CODES else f"{r.get('mod1code')}:{r.get('mod1param', '')}",
            ))
    return out


def affix_level(ilvl, qlvl, magic_lvl=0):
    ilvl = min(max(ilvl, qlvl), 99)
    if magic_lvl:
        return min(ilvl + magic_lvl, 99)
    if ilvl < 99 - qlvl // 2:
        return ilvl - qlvl // 2
    return 2 * ilvl - 99


def crafted_ilvl(ingredient_ilvl, char_level):
    return min(99, char_level // 2 + ingredient_ilvl // 2)


class Assessor:
    def __init__(self, gd, affixes=None, valuable_mods=None):
        self.gd = gd
        self.valuable_mods = set(valuable_mods if valuable_mods is not None else VALUABLE_MODS)
        self.affixes = affixes if affixes is not None else load_affixes(gd)
        self.max_sockets = {}
        for r in gd.tables["itemtypes"]:
            if r.get("Code"):
                self.max_sockets[r["Code"]] = (
                    _int(r.get("MaxSockets1")), _int(r.get("MaxSocketsLevelThreshold1"), 25),
                    _int(r.get("MaxSockets2")), _int(r.get("MaxSocketsLevelThreshold2"), 40),
                    _int(r.get("MaxSockets3")))
        self._pools = {}
        # magic affix ids in item data are row indexes into these tables
        self.affix_rows = {"prefix": gd.tables["magicprefix"], "suffix": gd.tables["magicsuffix"]}

    def _affix_tier(self, kind, idx, base):
        """(row, is_top_tier) for the affix with this id, judged among the tiers this item type can roll."""
        rows = self.affix_rows[kind]
        if not idx or idx >= len(rows):
            return None, False
        row = rows[idx]
        group = row.get("group")
        tiers = []
        for r in rows:
            # tiers of the same mod: same affix group, mod and parameter (a group can mix e.g. +3 tab and +2 class)
            if (r.get("group") != group or r.get("spawnable") != "1" or r.get("mod1code") != row.get("mod1code")
                    or r.get("mod1param") != row.get("mod1param")):
                continue
            itypes = [r.get(f"itype{i}", "") for i in range(1, 8)]
            etypes = [r.get(f"etype{i}", "") for i in range(1, 6)]
            if any(t and t in base.ancestors for t in itypes) and not any(t and t in base.ancestors for t in etypes):
                lvl = _int(r.get("level"))
                if lvl <= 99:  # tiers above 99 can never spawn
                    tiers.append(lvl)
        top = _int(row.get("level")) >= max(tiers, default=0)
        return row, top

    def as_is_value(self, it, base):
        """For a magic item: is it worth keeping as it is instead of crafting/rerolling it?

        An affix is "strong" when it is a valuable kind of mod (see VALUABLE_MODS) at the best tier this item
        type can roll.
          "keep"  - both affixes are strong
          "maybe" - one affix is strong and the other is a valuable kind of mod
        """
        if it.quality != 4:
            return None, ""
        found = []
        for kind, ids in (("prefix", it.prefixes), ("suffix", it.suffixes)):
            row, top = self._affix_tier(kind, ids[0] if ids else 0, base)
            if row is not None:
                valuable = row.get("mod1code") in self.valuable_mods
                found.append((row, top and valuable, valuable, top))
        note = " + ".join(f"{self.gd.text(r['Name'], r['Name'])} ({'best tier' if top else 'lower tier'})"
                          for r, _, _, top in found)
        if len(found) == 2:
            strong = sum(1 for f in found if f[1])
            if strong == 2:
                return "keep", note
            if strong == 1 and all(f[2] for f in found):
                return "maybe", note
        return None, note

    def pool(self, base, rare_only):
        key = (base.code, rare_only)
        if key not in self._pools:
            self._pools[key] = [
                a for a in self.affixes
                if (a.rare or not rare_only)
                and any(t in base.ancestors for t in a.itypes)
                and not any(t in base.ancestors for t in a.etypes)
            ]
        return self._pools[key]

    def _milestones(self, base, alvl, rare_only):
        """Notable affixes, best per mod group, split into unlocked and locked at this alvl."""
        best = {}
        for a in self.pool(base, rare_only):
            if a.level < NOTABLE_MIN_LEVEL:
                continue
            cur = best.get(a.group + a.desc)
            if cur is None or a.level > cur.level:
                best[a.group + a.desc] = a
        unlocked = sorted((a for a in best.values() if a.level <= alvl <= a.max_level), key=lambda a: -a.level)
        locked = sorted((a for a in best.values() if a.level > alvl), key=lambda a: a.level)
        return unlocked, locked

    def socket_cap(self, base, ilvl):
        ms = self.max_sockets.get(base.type)
        if not ms:
            return base.max_sockets
        m1, t1, m2, t2, m3 = ms
        cap = m1 if ilvl <= t1 else m2 if ilvl <= t2 else m3
        return min(cap, base.max_sockets) if base.max_sockets else cap

    def assess(self, it, char_level=99):
        """Returns a dict describing what this item's ilvl means, or None if ilvl doesn't matter for it."""
        gd = self.gd
        base = gd.items.get(it.code)
        if base is None:
            return None
        q = it.quality
        qlvl = base.qlvl
        res = {"ilvl": it.ilvl, "kind": None, "alvl": None, "tier": None, "score": 0,
               "unlocked": [], "locked": [], "note": "", "as_is": None, "as_is_note": ""}
        res["as_is"], res["as_is_note"] = self.as_is_value(it, base)

        if base.has("amul", "ring") and q == 4:
            # Crafting ingredient: what will a crafted item made from this be able to roll?
            c_ilvl = crafted_ilvl(it.ilvl, char_level)
            alvl = affix_level(c_ilvl, qlvl)
            unlocked, locked = self._milestones(base, alvl, rare_only=True)
            top = max([a.level for a in unlocked + locked] or [0])
            levels = sorted({a.level for a in unlocked + locked}, reverse=True)
            res.update(kind="craft", alvl=alvl, crafted_ilvl=c_ilvl)
            if alvl >= top:
                res["tier"], res["score"] = "Top", 3
            elif len(levels) > 1 and alvl >= levels[1]:
                res["tier"], res["score"] = "Good", 2
            elif alvl >= 60:
                res["tier"], res["score"] = "OK", 1
            else:
                res["tier"], res["score"] = "Low", 0
            if not locked:
                res["note"] = "crafts can roll every notable affix"
            else:
                need = locked[-1].level  # the highest locked milestone
                needed_ilvl = 2 * (need - char_level // 2)
                res["note"] = (f"needs ingredient ilvl {needed_ilvl}+ to unlock everything"
                               if needed_ilvl <= 99 else
                               f"top affixes need a higher-level crafter (alvl {need})")
            res["unlocked"] = [f"{a.name}: {a.desc} (alvl {a.level})" for a in unlocked[:6]]
            res["locked"] = [f"{a.name}: {a.desc} (alvl {a.level})" for a in locked[:6]]
            return res

        if base.has("char", "jewl") and q == 4:
            # Magic charms/jewels can be cube-rerolled; the reroll keeps the ilvl.
            alvl = affix_level(it.ilvl, qlvl)
            unlocked, locked = self._milestones(base, alvl, rare_only=False)
            res.update(kind="reroll", alvl=alvl)
            res["note"] = ("rerolls can roll every notable affix" if not locked else
                           f"can't roll {locked[0].name} ({locked[0].desc}) until affix level {locked[0].level}")
            if not locked:
                res["tier"], res["score"] = "Top", 3
            elif len(locked) <= 2:
                res["tier"], res["score"] = "Good", 2
            elif unlocked:
                res["tier"], res["score"] = "OK", 1
            else:
                res["tier"], res["score"] = "Low", 0
            res["unlocked"] = [f"{a.name}: {a.desc} (alvl {a.level})" for a in unlocked[:6]]
            res["locked"] = [f"{a.name}: {a.desc} (alvl {a.level})" for a in locked[:6]]
            return res

        if base.has("weap", "armo") and q in (1, 2, 3) and not it.runeword:
            cap = self.socket_cap(base, it.ilvl)
            res.update(kind="base", max_sockets=cap, sockets=it.total_sockets)
            best = base.max_sockets
            res["tier"], res["score"] = ("Top", 3) if cap >= best else ("OK", 1) if cap >= best - 1 else ("Low", 0)
            res["note"] = f"ilvl allows up to {cap} socket(s) (base max {best})"
            return res
        return None
