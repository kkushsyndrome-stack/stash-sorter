"""Glue between the engine and the user interfaces (CLI and GUI): turns world/plan objects into JSON."""

import datetime as dt
import json
from pathlib import Path

from . import catalog as C
from . import gamedata, planner, paths, apply as applier
from .art import ArtIndex
from .describe import Describer
from .items import MODE_STORED, MODE_EQUIPPED, MODE_BELT, PAGE_INVENTORY, PAGE_STASH, PAGE_CUBE
from .rules import Ruleset, load_default_rules, validate, RulesError
from .savefiles import TAB_NORMAL, TAB_STACKABLES
from .world import find_save_dir, load_world, mule_status

GRID_PAGES = (PAGE_INVENTORY, PAGE_STASH, PAGE_CUBE)


class Session:
    def __init__(self, saves=None, install=None, data_dir=None, backups=None, rules_path=None):
        self.save_dir = find_save_dir(saves)
        self.install = install
        self.gd = gamedata.load(install_dir=install, data_dir=data_dir)
        self.describer = Describer(self.gd)
        self.backup_dir = Path(backups) if backups else paths.backups_dir()
        self.rules_path = Path(rules_path) if rules_path else paths.rules_file()
        self.art = ArtIndex(self.gd, paths.cache_dir() / "art", install)
        self.crafter_level = 99
        self.plan = None
        self.plan_id = 0
        self.reload()
        self.crafter_level = self.max_char_level()

    # ---------- loading
    def reload(self):
        self.world = load_world(self.save_dir, self.gd)
        self.rules_dict = self._load_rules()
        self.plan = None
        self._ruleset = None

    def _load_rules(self):
        if self.rules_path.is_file():
            try:
                return validate(json.loads(self.rules_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, RulesError):
                return load_default_rules()
        return load_default_rules()

    def ruleset(self, crafter=None):
        crafter = crafter or self.crafter_level
        if self._ruleset is None or self._ruleset.crafter_level != crafter:
            self._ruleset = Ruleset(self.gd, self.world.names, self.rules_dict, crafter)
        return self._ruleset

    def max_char_level(self):
        return max((c.level for c in self.world.characters), default=99)

    # ---------- descriptions
    def item_json(self, it, where, key=None, crafter=None, rs=None):
        gd, names = self.gd, self.world.names
        rs = rs or self.ruleset(crafter)
        w, h = C.item_size(it, gd)
        base = gd.items.get(it.code)
        a = rs.assess(it)
        lines = []
        if it.defense is not None:
            lines.append(f"Defense: {it.defense}")
        if it.quantity:
            lines.append(f"Quantity: {it.quantity}")
        lines += self.describer.lines(it.stats + it.runeword_stats)
        if it.total_sockets:
            lines.append(f"Socketed ({it.total_sockets})")
        if it.ethereal:
            lines.append("Ethereal (Cannot be Repaired)")
        art = self.art.key_for(it.code, it.unique_id, it.set_id, it.gfx, base.tier if base else None) \
            if self.art.available else None
        return {
            "key": key, "code": it.code, "name": C.display_name(it, gd, names),
            "base": base.name if base else it.code, "quality": "runeword" if it.runeword else it.quality_name,
            "category": rs.categorize(it), "ilvl": it.ilvl, "x": it.x, "y": it.y, "w": w, "h": h,
            "page": it.page, "mode": it.mode, "ethereal": it.ethereal, "sockets": it.total_sockets,
            "socketed": [C.display_name(c, gd, names) for c in it.children],
            "stack": it.stack_count, "quantity": it.quantity, "where": where,
            "tier": base.tier if base else "", "assess": a, "stats": lines, "art": art,
        }

    def state(self):
        w = self.world
        stash = w.default_stash()
        rs = self.ruleset()
        chars = []
        for c in w.characters:
            ok, reason = mule_status(c, stash, 99) if stash else (False, "no stash")
            candidate = planner.is_mule_candidate(c, planner.Options())
            if ok and not candidate:
                ok, reason = False, f"level {c.level}"
            stored = [i for i in c.items if i.mode == MODE_STORED and i.page in GRID_PAGES]
            cats = [rs.categorize(i) for i in stored]
            top = max(set(cats), key=cats.count) if cats else ""
            chars.append({
                "name": c.name, "file": c.path.name, "class": c.class_name, "level": c.level,
                "era": c.era_name, "version": c.version, "hardcore": c.hardcore, "items": len(stored),
                "cells": sum(C.item_size(i, self.gd)[0] * C.item_size(i, self.gd)[1] for i in stored),
                "mule_ok": ok, "reason": reason, "top_category": top, "gold": c.gold,
                "top_share": round(cats.count(top) / len(cats), 2) if cats else 0,
                "blocked": not mule_status(c, stash, 99)[0] if stash else True,
            })
        return {
            "save_dir": str(self.save_dir), "data_source": self.gd.source,
            "backup_dir": str(self.backup_dir), "game_running": applier.game_running(),
            "default_stash": stash.path.name if stash else None, "art": self.art.available,
            "journal": applier.pending_journal(self.backup_dir),
            "stashes": [{"file": s.path.name, "modern": s.modern, "hardcore": s.hardcore, "era": s.era,
                         "items": sum(len(t.items) for t in s.normal_tabs()),
                         "gold": sum(t.gold for t in s.tabs),
                         "tabs": [{"type": t.type, "items": len(t.items), "gold": t.gold} for t in s.tabs]}
                        for s in w.stashes],
            "characters": chars, "errors": w.errors, "categories": rs.labels(), "category_order": rs.order,
            "max_level": self.max_char_level(), "crafter_level": self.crafter_level,
        }

    def stash_json(self, file_name=None, crafter=None):
        s = self.world.stash(file_name) if file_name else self.world.default_stash()
        rs = self.ruleset(crafter)
        tabs = []
        for ti, t in enumerate(s.tabs):
            if t.type == TAB_NORMAL:
                items = [self.item_json(it, f"tab {ti + 1}", planner.item_key(s.path.name, f"tab{ti}", i), rs=rs)
                         for i, it in enumerate(t.items)]
            elif t.type == TAB_STACKABLES:
                items = [{"code": it.code, "name": C.display_name(it, self.gd, self.world.names),
                          "count": it.stack_count or 0, "category": rs.categorize(it),
                          "art": self.art.key_for(it.code) if self.art.available else None}
                         for it in t.items]
            else:
                items = []
            tabs.append({"type": t.type, "gold": t.gold, "items": items})
        return {"file": s.path.name, "modern": s.modern, "tabs": tabs}

    def character_json(self, name, crafter=None):
        c = self.world.character(name)
        rs = self.ruleset(crafter)
        items = []
        for i, it in enumerate(c.items):
            items.append(self.item_json(it, _where(it), planner.item_key(c.path.name, "items", i), rs=rs))
        return {"name": c.name, "class": c.class_name, "level": c.level, "items": items, "gold": c.gold,
                "has_cube": any(i.code == "box" for i in c.items)}

    def all_items(self, crafter=None):
        """Every item in every save (for search / collection)."""
        rs = self.ruleset(crafter)
        out = []
        for c in self.world.characters:
            for i, it in enumerate(c.items):
                out.append(self.item_json(it, f"{c.name} - {_where(it)}", planner.item_key(c.path.name, "items", i),
                                          rs=rs))
        for s in self.world.stashes:
            for ti, t in enumerate(s.tabs):
                if t.type == TAB_NORMAL:
                    for i, it in enumerate(t.items):
                        out.append(self.item_json(it, f"{s.path.stem} tab {ti + 1}",
                                                  planner.item_key(s.path.name, f"tab{ti}", i), rs=rs))
                elif t.type == TAB_STACKABLES:
                    for it in t.items:
                        if it.stack_count:
                            j = self.item_json(it, f"{s.path.stem} Stackables", rs=rs)
                            j["count"] = it.stack_count
                            out.append(j)
        return out

    def assess_rows(self, crafter, scope="stash"):
        rs = self.ruleset(crafter)
        s = self.world.default_stash()
        sources = []
        for ti, t in enumerate(s.tabs):
            if t.type == TAB_NORMAL:
                sources += [(it, f"Shared stash tab {ti + 1}", planner.item_key(s.path.name, f"tab{ti}", i))
                            for i, it in enumerate(t.items)]
        if scope == "all":
            for c in self.world.characters:
                sources += [(it, c.name, planner.item_key(c.path.name, "items", i))
                            for i, it in enumerate(c.items) if it.mode == MODE_STORED]
        return [j for j in (self.item_json(it, where, key, rs=rs) for it, where, key in sources) if j["assess"]]

    def collection(self):
        """Holy grail: every unique and set item, and where you have it."""
        gd = self.gd
        owned_u, owned_s = {}, {}
        rw_made = {}
        for j, it in self._iter_items():
            if it.unique_id is not None:
                owned_u.setdefault(it.unique_id, []).append(j)
            if it.set_id is not None:
                owned_s.setdefault(it.set_id, []).append(j)
            if it.runeword:
                n = C.runeword_name(it, self.world.names)
                rw_made.setdefault(n, []).append(j)
        uniques, sets = [], []
        for r in gd.tables["uniqueitems"]:
            if r.get("*ID", "") == "" or r.get("disabled") == "1" or not r.get("code"):
                continue
            uid = int(r["*ID"])
            base = gd.items.get(r["code"])
            uniques.append({"name": gd.uniques[uid][0], "base": base.name if base else r["code"],
                            "tier": base.tier if base else "", "level": gamedata._int(r.get("lvl")),
                            "found": owned_u.get(uid, []),
                            "art": self.art.key_for(r["code"], uid, None, None, base.tier if base else None)
                            if self.art.available else None})
        for r in gd.tables["setitems"]:
            if r.get("*ID", "") == "" or r.get("disabled") == "1":
                continue
            sid = int(r["*ID"])
            base = gd.items.get(r.get("item", ""))
            sets.append({"name": gd.set_items[sid][0], "set": gd.set_items[sid][1],
                         "base": base.name if base else r.get("item", ""), "found": owned_s.get(sid, []),
                         "art": self.art.key_for(r.get("item", ""), None, sid) if self.art.available else None})
        runewords = [{"name": n, "found": rw_made.get(n, [])} for n in sorted({n for n, _ in
                     self.world.names.runewords if n})]
        return {"uniques": uniques, "sets": sets, "runewords": runewords}

    def _iter_items(self):
        for c in self.world.characters:
            for it in c.items:
                yield f"{c.name} - {_where(it)}", it
        for s in self.world.stashes:
            for ti, t in enumerate(s.tabs):
                if t.type == TAB_NORMAL:
                    for it in t.items:
                        yield f"{s.path.stem} tab {ti + 1}", it

    # ---------- rules
    def rules_json(self):
        return {"rules": self.rules_dict, "defaults": load_default_rules(),
                "custom": self.rules_path.is_file(), "path": str(self.rules_path)}

    def save_rules(self, rules):
        validate(rules)
        Ruleset(self.gd, self.world.names, rules)  # make sure it loads
        self.rules_path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
        self.rules_dict = rules
        self._ruleset = None
        self.plan = None
        return self.rules_json()

    def reset_rules(self):
        if self.rules_path.is_file():
            self.rules_path.unlink()
        self.rules_dict = load_default_rules()
        self._ruleset = None
        self.plan = None
        return self.rules_json()

    # ---------- planning
    def make_plan(self, o):
        opts = planner.Options(
            mode=o.get("mode", "stash"),
            stash_file=o.get("stash_file") or None,
            source_stash=o.get("source_stash") or None,
            mule_max_level=int(o.get("mule_max_level", 1)),
            mule_name_hint=bool(o.get("mule_name_hint", True)),
            mules=o.get("mules") or None,
            exclude=o.get("exclude") or [],
            use_stackables=bool(o.get("use_stackables", False)),
            rename=o.get("rename", "none"),
            keep_in_stash=o.get("keep_in_stash") or [],
            keep_items=o.get("keep_items") or [],
            limit=int(o.get("limit") or 0),
            create_mules=int(o.get("create_mules") or 0),
            rules=self.rules_dict,
            crafter_level=int(o.get("crafter_level") or self.crafter_level),
        )
        self.plan = planner.make_plan(self.world, opts)
        self.plan_id += 1
        return self.plan_json()

    def plan_json(self):
        p = self.plan
        gd, names = self.gd, self.world.names
        art = self.art.available

        def art_for(it):
            b = gd.items.get(it.code)
            return self.art.key_for(it.code, it.unique_id, it.set_id, it.gfx, b.tier if b else None) if art else None

        by_dest = {}
        for m in p.moves:
            dest = m.dst_char or f"{Path(m.dst_file).stem} tab {m.dst_tab + 1}"
            by_dest.setdefault(dest, []).append({
                "name": m.name, "category": m.category, "from": m.src_where, "to": m.dst_where,
                "page": m.page if m.dst_tab is None else 5, "x": m.x, "y": m.y,
                "w": C.item_size(m.item, gd)[0], "h": C.item_size(m.item, gd)[1], "art": art_for(m.item),
                "quality": "runeword" if m.item.runeword else m.item.quality_name, "ilvl": m.item.ilvl,
                "stats": self.describer.lines(m.item.stats + m.item.runeword_stats)})
        leaving = {id(m.item) for m in p.moves} | {id(m.item) for m in p.merges}
        existing = {}
        for dest in by_dest:
            ch = self.world.character(dest)
            if ch is not None:
                existing[dest] = [
                    {"name": C.display_name(it, gd, names), "page": it.page, "x": it.x, "y": it.y,
                     "w": C.item_size(it, gd)[0], "h": C.item_size(it, gd)[1], "ilvl": it.ilvl, "art": art_for(it),
                     "quality": "runeword" if it.runeword else it.quality_name}
                    for it in ch.items
                    if it.mode == MODE_STORED and it.page in GRID_PAGES and id(it) not in leaving]
            elif " tab " in dest:
                stem, tab = dest.rsplit(" tab ", 1)
                st = next((s for s in self.world.stashes if s.path.stem == stem), None)
                if st:
                    existing[dest] = [{"name": C.display_name(it, gd, names), "page": 5, "x": it.x, "y": it.y,
                                       "w": C.item_size(it, gd)[0], "h": C.item_size(it, gd)[1], "ilvl": it.ilvl,
                                       "art": art_for(it), "quality": it.quality_name}
                                      for it in st.tabs[int(tab) - 1].items]
        files = ({m.dst_file for m in p.moves} | {m.src_file for m in p.moves} | {m.src_file for m in p.merges}
                 | ({p.stash_file} if p.merges else set()))
        return {
            "id": self.plan_id, "stash": p.stash_file, "mode": p.options.mode, "existing": existing,
            "moves": len(p.moves), "by_dest": by_dest, "notes": p.notes, "left_in_place": p.left_in_place,
            "merges": [{"name": m.name, "from": m.src_where} for m in p.merges],
            "stack_counts": {k: list(v) for k, v in p.stack_counts.items()},
            "renames": [{"old": r.old, "new": r.new, "category": r.category} for r in p.renames],
            "new_mules": [{"name": n.name, "template": n.template, "category": n.category} for n in p.new_mules],
            "unplaced": [{"name": u.name, "category": u.category, "from": u.src_where} for u in p.unplaced],
            "mules": [vars(m) for m in p.mules], "skipped": p.skipped_chars, "labels": p.labels,
            "files_touched": len(files) + len(p.renames) + len(p.new_mules),
        }

    def apply(self, plan_id, log=print):
        if self.plan is None or plan_id != self.plan_id:
            raise applier.ApplyError("The plan is out of date. Preview it again before applying.")
        result = applier.apply_plan(self.world, self.plan, self.backup_dir, log=log)
        self.reload()
        return result

    # ---------- backups / undo / recovery
    def backups(self):
        if not self.backup_dir.is_dir():
            return []
        out = []
        for p in sorted(self.backup_dir.glob("saves-*.zip"), reverse=True):
            entry = {"file": p.name, "size": p.stat().st_size,
                     "time": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                     "log": None, "summary": "", "can_undo": False, "undo_reason": ""}
            log = p.with_name(p.stem + "-log.json")
            if log.is_file():
                try:
                    info = json.loads(log.read_text(encoding="utf-8"))
                    entry["log"] = log.name
                    entry["summary"] = (f"{info.get('mode', 'stash')}: {len(info.get('moves', []))} moved, "
                                        f"{len(info.get('stacked', []))} stacked, {len(info.get('renames', []))} "
                                        f"renamed, {len(info.get('new_mules', []))} new mule(s)")
                    entry["can_undo"], entry["undo_reason"] = applier.undo_status(log, self.save_dir)
                except (OSError, json.JSONDecodeError):
                    pass
            elif p.with_name(p.stem + "-log-undone.json").is_file():
                entry["summary"] = "undone"
            out.append(entry)
        return out

    def _backup_path(self, name):
        target = (self.backup_dir / name).resolve()
        if target.parent != self.backup_dir.resolve() or not target.is_file():
            raise applier.ApplyError("Unknown backup")
        return target

    def restore(self, file_name):
        safety = applier.restore_backup(self._backup_path(file_name), self.save_dir, self.backup_dir)
        self.reload()
        return {"restored": file_name, "safety_backup": str(safety)}

    def undo(self, log_name):
        res = applier.undo_apply(self._backup_path(log_name), self.save_dir, self.backup_dir)
        self.reload()
        return res

    def recover(self):
        res = applier.recover(self.backup_dir)
        self.reload()
        return {"recovered": bool(res)}


def _where(it):
    return {MODE_EQUIPPED: "equipped", MODE_BELT: "belt"}.get(
        it.mode, {PAGE_INVENTORY: "inventory", PAGE_STASH: "stash", PAGE_CUBE: "cube"}.get(it.page, "?"))
