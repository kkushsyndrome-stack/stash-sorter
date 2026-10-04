"""Turns stored item stats into the game's tooltip lines ("+20% Faster Cast Rate").

Driven by the desc* / dgrp* columns of itemstatcost.txt and the game's string tables, so new stats in a patch
mostly describe themselves. A few stat families the game combines in code (damage ranges, enhanced damage,
all resistances / all attributes) are combined here too.
"""

import re

from .gamedata import _int

CLASS_CODES = ["ama", "sor", "nec", "pal", "bar", "dru", "ass", "war"]

_TOKEN = re.compile(r"%(\+?)d|%s|%%|%(\d)")

# (min stat, max stat, length stat or None, range string, single-value string)
_DAMAGE_PAIRS = [
    (48, 49, None, "strModFireDamageRange", "strModFireDamage"),
    (50, 51, None, "strModLightningDamageRange", "strModLightningDamage"),
    (52, 53, None, "strModMagicDamageRange", "strModMagicDamage"),
    (54, 55, 56, "strModColdDamageRange", "strModColdDamage"),
    (57, 58, 59, "strModPoisonDamageRange", "strModPoisonDamage"),
    (21, 22, None, "strModMinDamageRange", "strModMinDamage"),
]
# copies of the min/max damage stats the game keeps for other weapon modes; never shown separately
_HIDDEN_DUPLICATES = {23, 24, 159, 160}


def fmt(template, *args):
    """printf-style %d / %+d / %s / %% and D2R's positional %0 / %1."""
    if template is None:
        return ""
    it = iter(args)

    def sub(m):
        if m.group(0) == "%%":
            return "%"
        if m.group(2) is not None:
            i = int(m.group(2))
            return str(args[i]) if i < len(args) else ""
        v = next(it, "")
        if m.group(1) and isinstance(v, int):
            return f"{v:+d}"
        return str(v)

    return _TOKEN.sub(sub, template)


class Describer:
    def __init__(self, gd):
        self.gd = gd
        self.rows = {}
        for r in gd.tables["itemstatcost"]:
            if r.get("*ID", "") != "":
                self.rows[int(r["*ID"])] = r
        self.skill_names, self.skill_class = {}, {}
        desc = {r["skilldesc"]: r["str name"] for r in gd.tables.get("skilldesc", [])}
        for r in gd.tables.get("skills", []):
            if r.get("*Id", "") == "":
                continue
            sid = int(r["*Id"])
            key = desc.get(r.get("skilldesc", ""), "")
            self.skill_names[sid] = gd.text(key, r["skill"]) if key else r["skill"]
            self.skill_class[sid] = r.get("charclass", "")
        self.classes = [r for r in gd.tables.get("charstats", []) if r.get("StrAllSkills")]
        self.monsters = {}
        for i, r in enumerate(gd.tables.get("monstats", [])):
            mid = _int(r.get("*hcIdx"), i)
            self.monsters[mid] = gd.text(r.get("NameStr", ""), r.get("Id", ""))

    def s(self, key):
        return self.gd.strings.get(key) if key else None

    def skill(self, sid):
        return self.skill_names.get(sid, f"skill #{sid}")

    def class_row(self, idx):
        return self.classes[idx] if 0 <= idx < len(self.classes) else None

    def class_only(self, sid):
        code = self.skill_class.get(sid, "")
        if code in CLASS_CODES:
            row = self.class_row(CLASS_CODES.index(code))
            return self.s(row.get("StrClassOnly")) if row else ""
        return ""

    def line(self, sid, param, value):
        """One tooltip line for a single stat, or None if the game doesn't show it."""
        r = self.rows.get(sid)
        if r is None:
            return None
        func = _int(r.get("descfunc"))
        template = self.s(r.get("descstrneg") if value < 0 else r.get("descstrpos")) or self.s(r.get("descstrpos"))
        extra = self.s(r.get("descstr2"))
        if func == 0 or template is None:
            return None
        if func == 13:
            row = self.class_row(param)
            return fmt(self.s(row.get("StrAllSkills")) if row else template, value)
        if func == 14:
            row = self.class_row(param >> 3)
            tab = param & 7
            key = row.get(f"StrSkillTab{tab + 1}") if row else None
            return fmt(self.s(key) or template, value) + (f" {self.class_only_by_index(param >> 3)}" if row else "")
        if func == 15:
            return fmt(template, value, param & 63, self.skill(param >> 6))
        if func == 16:
            return fmt(template, value, self.skill(param))
        if func == 24:
            return fmt(template, param & 63, self.skill(param >> 6), value & 255, value >> 8)
        if func == 27:
            return fmt(template, value, self.skill(param), self.class_only(param))
        if func == 28:
            return fmt(template, value, self.skill(param))
        if func == 23:
            return fmt(template, value, self.monsters.get(param, f"monster #{param}"))
        if func == 22:
            return fmt(template, value) + f" monster type #{param}"
        if func == 11:
            return f"Repairs 1 Durability in {100 // value} Seconds" if value > 0 else fmt(template, value)
        if func == 5:
            return fmt(template, value * 100 // 128)
        if func in (17, 18):
            return fmt(template, value) + " (varies with time of day)"
        if func == 12 and "%" not in template:
            return template
        # stats that grow with character level are stored in fractions (halves, eighths, ...)
        op, op_param = _int(r.get("op")), _int(r.get("op param"))
        if extra and op in (2, 4, 5) and op_param:
            per = value / (1 << op_param)
            per_txt = f"{per:+g}" if "%+d" in template else f"{per:g}"
            return fmt(template.replace("%+d", "%s").replace("%d", "%s"), per_txt) + " per character level"
        out = fmt(template, value)
        return f"{out} {extra}" if extra else out

    def class_only_by_index(self, idx):
        row = self.class_row(idx)
        return self.s(row.get("StrClassOnly")) if row else ""

    def lines(self, stats):
        """Tooltip lines for a list of (stat id, param, value), highest priority first."""
        by_id = {}
        for sid, param, value in stats:
            by_id.setdefault(sid, []).append((param, value))
        used = set(_HIDDEN_DUPLICATES)
        out = []  # (priority, text)

        def single(sid):
            return by_id[sid][0][1] if sid in by_id and len(by_id[sid]) == 1 else None

        def prio(sid):
            return _int(self.rows.get(sid, {}).get("descpriority"))

        # enhanced damage: min% and max% are stored separately but shown as one line
        if single(17) is not None and single(17) == single(18):
            out.append((prio(17), fmt(self.s("strModEnhancedDamage"), single(17))))
            used |= {17, 18}
        # damage ranges
        for lo, hi, length, rng, one in _DAMAGE_PAIRS:
            a, b = single(lo), single(hi)
            if a is None or b is None:
                continue
            used |= {lo, hi} | ({length} if length else set())
            if length:
                frames = single(length) or 0
                if lo == 57:  # poison: stored per frame in 1/256ths
                    a, b = a * frames // 256, b * frames // 256
                    out.append((prio(lo), fmt(self.s(rng), a, b, max(1, frames // 25))))
                    continue
            out.append((prio(lo), fmt(self.s(one), a) if a == b and self.s(one) else fmt(self.s(rng), a, b)))
        # stat groups (all resistances, all attributes)
        groups = {}
        for sid, r in self.rows.items():
            g = _int(r.get("dgrp"))
            if g:
                groups.setdefault(g, []).append(sid)
        for g, members in groups.items():
            vals = [single(m) for m in members]
            if all(v is not None for v in vals) and len(set(vals)) == 1 and not (set(members) & used):
                r = self.rows[members[0]]
                v = vals[0]
                tmpl = self.s(r.get("dgrpstrneg") if v < 0 else r.get("dgrpstrpos"))
                if tmpl:
                    out.append((max(prio(m) for m in members), fmt(tmpl, v)))
                    used |= set(members)
        for sid, entries in by_id.items():
            if sid in used:
                continue
            for param, value in entries:
                text = self.line(sid, param, value)
                if text:
                    out.append((prio(sid), text))
        out.sort(key=lambda t: -t[0])
        return [t for _, t in out]
