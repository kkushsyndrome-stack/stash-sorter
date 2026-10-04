"""Sorting rules: which category each item belongs to, how categories are named and how mules are ordered.

Defaults live in default_rules.json; a user's own copy (rules.json in the app folder) overrides them and can be
edited from the GUI.
"""

import copy
import json
from pathlib import Path

from . import catalog as C
from .assessor import Assessor

DEFAULT_RULES_PATH = Path(__file__).parent / "default_rules.json"

QUALITY = {1: "inferior", 2: "normal", 3: "superior", 4: "magic", 5: "set", 6: "rare", 7: "unique", 8: "crafted",
           9: "tempered"}
_CONDITIONS = {"types", "not_types", "codes", "not_codes", "quality", "not_quality", "runeword", "ethereal",
               "socketed", "carry_one", "tier", "min_ilvl", "max_ilvl", "as_is", "assess_tier", "name_contains"}
_SORTS = {"size", "ilvl", "name", "set", "slot"}
SLOT_ORDER = ["helm", "circ", "pelt", "phlm", "tors", "shie", "ashd", "head", "glov", "boot", "belt",
              "h2h", "h2h2", "orb", "wand", "staf", "bow", "xbow", "jav", "spea", "pole", "swor", "knif", "axe",
              "club", "scep", "mace", "hamm", "abow", "aspe", "weap", "armo"]


class RulesError(ValueError):
    pass


def load_default_rules():
    return json.loads(DEFAULT_RULES_PATH.read_text(encoding="utf-8"))


def validate(rules):
    """Raises RulesError with a readable message if the rules are malformed."""
    if not isinstance(rules, dict) or not isinstance(rules.get("categories"), list) or not rules["categories"]:
        raise RulesError("rules need a non-empty 'categories' list")
    keys = set()
    for i, cat in enumerate(rules["categories"]):
        where = f"category #{i + 1}"
        if not isinstance(cat, dict):
            raise RulesError(f"{where} must be an object")
        key = cat.get("key")
        if not key or not isinstance(key, str) or not key.isidentifier():
            raise RulesError(f"{where}: 'key' must be a short identifier (letters, digits, _)")
        if key in keys:
            raise RulesError(f"duplicate category key '{key}'")
        keys.add(key)
        stem = cat.get("stem", "")
        if not (isinstance(stem, str) and 2 <= len(stem) <= 13 and stem.isalpha()):
            raise RulesError(f"'{key}': 'stem' (mule name start) must be 2-13 letters")
        if cat.get("sort", "size") not in _SORTS:
            raise RulesError(f"'{key}': sort must be one of {sorted(_SORTS)}")
        match = cat.get("match", {})
        for alt in (match if isinstance(match, list) else [match]):
            if not isinstance(alt, dict):
                raise RulesError(f"'{key}': each match must be an object")
            unknown = set(alt) - _CONDITIONS
            if unknown:
                raise RulesError(f"'{key}': unknown condition(s) {sorted(unknown)}")
    for cat in rules["categories"]:
        for other in cat.get("share_with", []):
            if other not in keys:
                raise RulesError(f"'{cat['key']}': share_with refers to unknown category '{other}'")
    if not any(c.get("match") in ({}, [{}]) for c in rules["categories"] if c.get("enabled", True)):
        raise RulesError("the last category should be a catch-all with an empty match {} so every item has a home")
    return rules


class Ruleset:
    def __init__(self, gd, names, rules=None, crafter_level=99):
        self.gd, self.names = gd, names
        self.rules = validate(copy.deepcopy(rules) if rules else load_default_rules())
        self.cats = [c for c in self.rules["categories"] if c.get("enabled", True)]
        self.by_key = {c["key"]: c for c in self.cats}
        self.assessor = Assessor(gd, valuable_mods=self.rules.get("valuable_mods"))
        self.crafter_level = crafter_level
        self._assess_cache = {}

    # ---- category metadata
    @property
    def order(self):
        return [c["key"] for c in self.cats]

    def label(self, key):
        return self.by_key.get(key, {}).get("label", key)

    def stem(self, key):
        return self.by_key.get(key, {}).get("stem", "Mule")

    def share_with(self, key):
        return self.by_key.get(key, {}).get("share_with", [])

    def stackable(self, key):
        return bool(self.by_key.get(key, {}).get("stackable"))

    def labels(self):
        return {c["key"]: c.get("label", c["key"]) for c in self.cats}

    # ---- classification
    def assess(self, it):
        k = id(it)
        if k not in self._assess_cache:
            self._assess_cache[k] = self.assessor.assess(it, self.crafter_level)
        return self._assess_cache[k]

    def _fits(self, it, cond):
        gd = self.gd
        base = gd.items.get(it.code)
        anc = base.ancestors if base else frozenset()
        q = QUALITY.get(it.quality, "")
        if "types" in cond and not anc & set(cond["types"]):
            return False
        if "not_types" in cond and anc & set(cond["not_types"]):
            return False
        if "codes" in cond and it.code not in cond["codes"]:
            return False
        if "not_codes" in cond and it.code in cond["not_codes"]:
            return False
        if "quality" in cond and (q not in cond["quality"] or it.runeword):
            return False
        if "not_quality" in cond and q in cond["not_quality"]:
            return False
        if "runeword" in cond and it.runeword != cond["runeword"]:
            return False
        if "ethereal" in cond and it.ethereal != cond["ethereal"]:
            return False
        if "socketed" in cond and (it.total_sockets > 0) != cond["socketed"]:
            return False
        if "carry_one" in cond and bool(C.carry_one_group(it, gd)) != cond["carry_one"]:
            return False
        if "tier" in cond and (not base or base.tier not in cond["tier"]):
            return False
        if "min_ilvl" in cond and it.ilvl < cond["min_ilvl"]:
            return False
        if "max_ilvl" in cond and it.ilvl > cond["max_ilvl"]:
            return False
        if "as_is" in cond or "assess_tier" in cond:
            a = self.assess(it)
            if "as_is" in cond and ((a or {}).get("as_is") or "none") not in cond["as_is"]:
                return False
            if "assess_tier" in cond and (not a or a.get("tier") not in cond["assess_tier"]):
                return False
        if "name_contains" in cond and cond["name_contains"].lower() not in C.display_name(it, gd, self.names).lower():
            return False
        return True

    def categorize(self, it):
        if it.code == "ear" or it.code not in self.gd.items:
            return self.order[-1]
        for cat in self.cats:
            match = cat.get("match", {})
            alts = match if isinstance(match, list) else [match]
            if any(self._fits(it, alt) for alt in alts):
                return cat["key"]
        return self.order[-1]

    # ---- ordering on a mule (tall items first packs tightest, then the category's own order)
    def sort_key(self, it, key):
        gd = self.gd
        b = gd.items.get(it.code)
        w, h = (b.width, b.height) if b else (1, 1)
        name = C.display_name(it, gd, self.names)
        mode = self.by_key.get(key, {}).get("sort", "size")
        if mode == "ilvl":
            return (-h, -w, b.type if b else "", -it.ilvl, name)
        if mode == "name":
            return (-h, -w, name, -it.ilvl)
        if mode == "set":
            set_name = gd.set_items.get(it.set_id, ("", ""))[1] if it.set_id is not None else ""
            return (-h, -w, set_name, name)
        if mode == "slot":
            anc = b.ancestors if b else frozenset()
            slot = next((i for i, t in enumerate(SLOT_ORDER) if t in anc), len(SLOT_ORDER))
            return (-h, -w, slot, name, -it.ilvl)
        return (-h, -w, b.type if b else "", it.quality, name, -it.ilvl)
