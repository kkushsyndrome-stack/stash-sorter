"""Human-readable item names and a few item facts. Sorting categories live in rules.py."""

from dataclasses import dataclass


@dataclass
class Names:
    prefixes: list
    suffixes: list
    rare_names: list  # 1-based: [None, *raresuffix, *rareprefix]
    runewords: list  # [(name, [rune codes])] in runes.txt order


def load_names(gd):
    def names(table):
        return [gd.text(r.get("Name") or r.get("name", ""), r.get("Name") or r.get("name", ""))
                for r in gd.tables[table]]

    rare = [None] + names("raresuffix") + names("rareprefix")
    rws = []
    for r in gd.tables["runes"]:
        runes = [r.get(f"Rune{i}", "") for i in range(1, 7)]
        rws.append((gd.text(r.get("Name", ""), r.get("*Rune Name", "")), [x for x in runes if x]))
    return Names(prefixes=names("magicprefix"), suffixes=names("magicsuffix"), rare_names=rare, runewords=rws)


def runeword_name(it, names):
    socketed = [c.code for c in it.children]
    for name, runes in names.runewords:
        if runes and runes == socketed:
            return name
    idx = (it.runeword_id or 0) - 27  # runeword ids are runes.txt rows offset by 27
    if 0 <= idx < len(names.runewords):
        return names.runewords[idx][0]
    return "Runeword"


def display_name(it, gd, names):
    if it.code == "ear":
        return "Ear"
    base = gd.items[it.code]
    if it.runeword:
        return f"{runeword_name(it, names)} ({base.name})"
    if it.unique_id is not None:
        return gd.uniques.get(it.unique_id, (f"Unique #{it.unique_id}",))[0]
    if it.set_id is not None:
        return gd.set_items.get(it.set_id, (f"Set item #{it.set_id}",))[0]
    if it.quality == 4:
        pre = names.prefixes[it.prefixes[0]] if it.prefixes and 0 < it.prefixes[0] < len(names.prefixes) else ""
        suf = names.suffixes[it.suffixes[0]] if it.suffixes and 0 < it.suffixes[0] < len(names.suffixes) else ""
        return " ".join(x for x in (pre, base.name, suf) if x)
    if it.quality in (6, 8, 9) and it.rare_name:
        a, b = it.rare_name
        parts = [names.rare_names[i] for i in (a, b) if 0 < i < len(names.rare_names) and names.rare_names[i]]
        if parts:
            return " ".join(parts)
    if it.quality == 3:
        return f"Superior {base.name}"
    if it.quality == 1:
        return f"Low Quality {base.name}"
    return base.name


def item_size(it, gd):
    b = gd.items.get(it.code)
    return (b.width, b.height) if b else (1, 1)


def carry_one_group(it, gd):
    if it.unique_id is None:
        return 0
    return gd.uniques.get(it.unique_id, ("", "", 0))[2]
