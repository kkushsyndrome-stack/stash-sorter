"""Glue between the engine and the user interfaces (CLI and GUI): turns world/plan objects into JSON."""

import datetime as dt
import json
from pathlib import Path
from typing import NamedTuple

from . import catalog as C
from . import gamedata, planner, paths, apply as applier
from .art import ArtIndex, norm
from .artmod import ArtMod, ART_FILES
from .describe import Describer
from .items import MODE_STORED, MODE_EQUIPPED, MODE_BELT, PAGE_INVENTORY, PAGE_STASH, PAGE_CUBE
from .rules import Ruleset, load_default_rules, validate, RulesError
from .tracker import Tracker
from .clock import TerrorClock, ClockError
from . import launcher
from .savefiles import TAB_NORMAL, TAB_STACKABLES, empty_status
from .world import find_save_dir, load_world, mule_status

GRID_PAGES = (PAGE_INVENTORY, PAGE_STASH, PAGE_CUBE)


class Found(NamedTuple):
    """One item somewhere in the saves, as `Session.walk` reports it."""
    item: object
    owner: str  # character name, or "Shared stash (RotW)"
    place: str  # "stash", "inventory", "equipped"... on a character; "tab 2" or "Stackables tab" in a stash
    key: str    # planner.item_key (None for a Stackables-tab stack)
    kind: str   # "char", "tab" or "stack"
    file: str

    @property
    def where(self):
        return f"{self.owner} - {self.place}"

    @property
    def on_grid(self):
        """Stored in a stash, inventory or cube (not equipped, in the belt or a stack): can be moved or deleted."""
        return self.kind == "tab" or (self.kind == "char" and self.item.mode == MODE_STORED
                                      and self.item.page in GRID_PAGES)


class Session:
    def __init__(self, saves=None, install=None, data_dir=None, backups=None, rules_path=None, sessions=None):
        self.save_dir = find_save_dir(saves)
        self.install = install
        self.gd = gamedata.load(install_dir=install, data_dir=data_dir)
        self.describer = Describer(self.gd)
        self.backup_dir = Path(backups) if backups else paths.backups_dir()
        self.rules_path = Path(rules_path) if rules_path else paths.rules_file()
        self.art = ArtIndex(self.gd, paths.cache_dir() / "art", install)
        root = gamedata.find_install_dir(install)
        self.artmod = ArtMod(root / "Data" if root else None, paths.app_dir() / "artmod",
                             game_running=applier.game_running)
        self.crafter_level = 99
        self.plan = None
        self.plan_id = 0
        self.reload()
        self.crafter_level = self.max_char_level()
        self.clock = TerrorClock()
        self.seed_book = launcher.SeedBook(paths.app_dir() / "seeds.json")
        self.seed_run = launcher.SeedRun(paths.app_dir() / "seedrun.json", saves_dir=self.save_dir,
                                         backup_dir=self.backup_dir)
        self.clock_checked = False
        self.tracker = Tracker(self.save_dir, self.gd, self.world.names,
                               Path(sessions) if sessions else paths.app_dir() / "sessions", self._describe_found,
                               now=self.clock.real_local)

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
            "gfx": it.gfx, "skinnable": planner.skinnable(it, gd),
        }

    def state(self):
        w = self.world
        stash = w.default_stash()
        rs = self.ruleset()
        chars = []
        for c in w.characters:
            usable, reason = mule_status(c, stash, 99) if stash else (False, "no stash")
            ok = usable and planner.is_mule_candidate(c, planner.Options())
            if usable and not ok:
                reason = f"level {c.level}"
            stored = [i for i in c.items if i.mode == MODE_STORED and i.page in planner.MULE_PAGES]  # 140 cells
            cats = [rs.categorize(i) for i in stored]
            top = max(set(cats), key=cats.count) if cats else ""
            chars.append({
                "name": c.name, "file": c.path.name, "class": c.class_name, "level": c.level,
                "era": c.era_name, "version": c.version, "hardcore": c.hardcore, "items": len(stored),
                "cells": sum(C.item_size(i, self.gd)[0] * C.item_size(i, self.gd)[1] for i in stored),
                "mule_ok": ok, "reason": reason, "top_category": top, "gold": c.gold,
                "top_share": round(cats.count(top) / len(cats), 2) if cats else 0,
                "blocked": not usable,
            })
        return {
            "clock_shift": int(self.clock.offset.total_seconds()) if self.clock.shifted else 0,
            "save_dir": str(self.save_dir), "data_source": self.gd.source,
            "backup_dir": str(self.backup_dir), "game_running": applier.game_running(),
            "default_stash": stash.path.name if stash else None, "art": self.art.available,
            "journal": applier.pending_journal(self.backup_dir),
            "stale": [p.name for p in w.changed_on_disk()][:10],
            "stashes": [{"file": s.path.name, "modern": s.modern, "hardcore": s.hardcore, "era": s.era,
                         "items": sum(len(t.items) for t in s.normal_tabs()),
                         "gold": sum(t.gold for t in s.tabs),
                         "tabs": [{"type": t.type, "items": len(t.items), "gold": t.gold} for t in s.tabs]}
                        for s in w.stashes],
            "characters": chars, "errors": w.errors, "categories": rs.labels(), "category_order": rs.order,
            "max_level": self.max_char_level(), "crafter_level": self.crafter_level,
            "variants": self.variants(),
        }

    def mule_defaults(self, level=1, hint=True):
        """{character name: counts as a mule by default} for the GUI's starting roles."""
        stash = self.world.default_stash()
        opts = planner.Options(mule_max_level=level, mule_name_hint=hint)
        return {c.name: bool(stash) and mule_status(c, stash, 99)[0] and planner.is_mule_candidate(c, opts)
                for c in self.world.characters}

    def stash_json(self, file_name=None, crafter=None):
        s = self.world.stash(file_name) if file_name else self.world.default_stash()
        rs = self.ruleset(crafter)
        tabs = []
        for ti, t in enumerate(s.tabs):
            if t.type == TAB_NORMAL:
                items = []
                for i, it in enumerate(t.items):
                    j = self.item_json(it, f"tab {ti + 1}", planner.item_key(s.path.name, f"tab{ti}", it), rs=rs)
                    j["legacy_key"] = f"{s.path.name}|tab{ti}|{i}"  # how 0.7.0 and older named it (keep flags)
                    items.append(j)
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
        for it in c.items:
            items.append(self.item_json(it, _where(it), planner.item_key(c.path.name, "items", it), rs=rs))
        for j in items:
            j["owner"] = c.name
        return {"name": c.name, "class": c.class_name, "level": c.level, "items": items, "gold": c.gold,
                "has_cube": any(i.code == "box" for i in c.items)}

    def variants(self):
        """{base code: {name, art: [artwork key for each picture]}} for items whose picture the save stores."""
        out = {}
        for code, b in self.gd.items.items():
            if b.pictures and not b.quest:
                art = [self.art.key_for(code, None, None, n, b.tier) if self.art.available else None
                       for n in range(b.pictures)]
                out[code] = {"name": b.name, "art": art}
        return out

    def walk(self, stacks=False):
        """Every top-level item in every save, as `Found` (Stackables-tab stacks only when `stacks`)."""
        for c in self.world.characters:
            for it in c.items:
                yield Found(it, c.name, _where(it), planner.item_key(c.path.name, "items", it), "char", c.path.name)
        for s in self.world.stashes:
            owner = _stash_label(s)
            for ti, t in enumerate(s.tabs):
                if t.type == TAB_NORMAL:
                    for it in t.items:
                        yield Found(it, owner, f"tab {ti + 1}", planner.item_key(s.path.name, f"tab{ti}", it), "tab",
                                    s.path.name)
                elif t.type == TAB_STACKABLES and stacks:
                    for it in t.items:
                        if it.stack_count:
                            yield Found(it, owner, "Stackables tab", None, "stack", s.path.name)

    def all_items(self, crafter=None):
        """Every item in every save (for search / collection)."""
        rs = self.ruleset(crafter)
        out = []
        for f in self.walk(stacks=True):
            j = self.item_json(f.item, f.where, f.key, rs=rs)
            j.update(owner=f.owner, place=f.place, stash_tab=f.kind == "tab")
            if f.kind == "stack":
                j["count"] = f.item.stack_count
            out.append(j)
        return out

    def assess_rows(self, crafter, scope="stash"):
        rs = self.ruleset(crafter)
        stash = self.world.default_stash()
        rows = []
        for f in self.walk():
            in_stash = f.kind == "tab" and f.file == stash.path.name
            on_char = scope == "all" and f.kind == "char" and f.item.mode == MODE_STORED
            if in_stash or on_char:
                j = self.item_json(f.item, f.where, f.key, rs=rs)
                if j["assess"]:
                    rows.append(j)
        return rows

    def collection(self):
        """Holy grail: every unique and set item, and where you have it."""
        gd = self.gd
        owned_u, owned_s = {}, {}
        rw_made = {}
        for j, it in ((f.where, f.item) for f in self.walk()):
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
            compact=bool(o.get("compact", True)),
            mules=o.get("mules") or None,
            exclude=o.get("exclude") or [],
            from_chars=o.get("from_chars") or [],
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
                 | ({p.stash_file} if p.merges else set()) | {d.file for d in p.deletions}
                 | {d.file for d in p.delete_chars} | {r.file for r in p.restyles})
        return {
            "id": self.plan_id, "stash": p.stash_file, "mode": p.options.mode, "existing": existing,
            "moves": len(p.moves), "by_dest": by_dest, "notes": p.notes, "left_in_place": p.left_in_place,
            "merges": [{"name": m.name, "from": m.src_where} for m in p.merges],
            "stack_counts": {k: list(v) for k, v in p.stack_counts.items()},
            "renames": [{"old": r.old, "new": r.new, "category": r.category} for r in p.renames],
            "new_mules": [{"name": n.name, "template": n.template, "category": n.category} for n in p.new_mules],
            "deletions": [{"name": d.name, "where": d.where, "key": d.key} for d in p.deletions],
            "restyles": [{"name": r.name, "where": r.where, "old": r.old + 1, "new": r.new + 1} for r in p.restyles],
            "delete_chars": [{"name": d.name, "file": d.file, "level": d.level} for d in p.delete_chars],
            "emptied": p.emptied,
            "unplaced": [{"name": u.name, "category": u.category, "from": u.src_where} for u in p.unplaced],
            "mules": [vars(m) for m in p.mules], "skipped": p.skipped_chars, "labels": p.labels,
            "files_touched": len(files) + len(p.renames) + len(p.new_mules),
        }

    # ---------- terror zone clock
    def _tz_ready(self):
        if not self.clock_checked:
            self.clock.measure_offset()
            try:
                self.clock.load_schedule()
            except ClockError:
                pass
            self.clock_checked = True

    def tz_state(self):
        self._tz_ready()
        c = self.clock
        zone = c.settings.get("zone") or c.zones()[0]

        def sess(s):
            return s and {"start": s["start"].isoformat(), "zone": s["zone"], "immunities": s["immunities"],
                          "boss_packs": s["boss_packs"], "superuniques": s["superuniques"]}

        picked, state = c.zone_session(zone)
        cur = c.current()
        return {
            "loaded": bool(c.sessions), "source": c.source, "count": len(c.sessions),
            "session_minutes": int(c.session_length.total_seconds() // 60),
            "real_now": c.real_utc().isoformat(), "pc_now": c.system_utc().isoformat(),
            "shifted": c.shifted, "offset_seconds": int(c.offset.total_seconds()), "offset_known": c.offset_known,
            "zones": c.zones(), "favourites": c.settings.get("favourites", []), "zone": zone,
            "auto_revert": c.settings.get("auto_revert", True),
            "picked": sess(picked), "picked_state": state,
            "current": sess(c.sessions[cur]) if cur is not None else None,
            "upcoming": [sess(s) for s in c.upcoming(10)],
        }

    def tz_update(self, body):
        c = self.clock
        if "zone" in body:
            c.settings["zone"] = body["zone"]
        if "favourite" in body:
            fav = c.settings.setdefault("favourites", [])
            z = body["favourite"]
            fav.remove(z) if z in fav else fav.append(z)
        if "auto_revert" in body:
            c.settings["auto_revert"] = bool(body["auto_revert"])
        c.save_settings()
        return self.tz_state()

    def tz_set(self, zone):
        self._tz_ready()
        s = self.clock.set_to(zone)
        return {"zone": s["zone"], "start": s["start"].isoformat(), **self.tz_state()}

    def tz_revert(self):
        ok = self.clock.revert()
        return {"restored": ok, **self.tz_state()}

    def tz_refresh(self):
        self.clock_checked = False
        return self.tz_state()

    def revert_clock_on_exit(self):
        """Called when Horadric Toolkit closes: put the real time back if the user asked for that."""
        c = self.clock
        if c.shifted and c.settings.get("auto_revert", True):
            try:
                c.revert()
                return True
            except ClockError:
                return False
        return None

    def backup_now(self):
        return {"backup": str(applier.backup_save_dir(self.save_dir, self.backup_dir, label="manual"))}

    # ---------- Battle.net launch options
    def _install_dir(self):
        return gamedata.find_install_dir(self.install)

    def launch_state(self):
        L = launcher
        st = {"config": str(L.config_path()), "found": L.config_path().is_file(), "running": L.battlenet_running(),
              "battlenet": str(L.battlenet_exe() or ""), "mods": L.list_mods(self._install_dir()),
              "install": str(self._install_dir() or ""), "flags": L.FLAGS, "value_flags": L.VALUE_FLAGS,
              "current": None, "parsed": None, "error": None, "backups": [], "seed_run": self.seed_run.view()}
        try:
            st["current"] = L.read_args()
            st["parsed"] = L.parse_args(st["current"])
        except (L.LaunchError, ValueError) as e:
            st["error"] = str(e)
        if self.backup_dir.is_dir():
            st["backups"] = [p.name for p in sorted(self.backup_dir.glob("Battle.net.config-*.bak"), reverse=True)]
        return st

    def launch_preview(self, body):
        args = launcher.build_args(body.get("flags") or [], body.get("seed"), body.get("mod"), body.get("other") or "")
        return {"args": args, "problems": launcher.parse_args(args)["problems"]}

    def _no_seed_run(self):
        if self.seed_run.active:
            raise launcher.LaunchError("A seed run is going: let it finish (or cancel it) first.")

    def launch_save(self, body):
        self._no_seed_run()
        args = body.get("args")
        if args is None:
            args = self.launch_preview(body)["args"]
        backup = launcher.write_args(args, self.backup_dir)
        return {"saved": args, "backup": str(backup), **self.launch_state()}

    def launch_restore(self, name):
        self._no_seed_run()
        p = (self.backup_dir / name).resolve()
        if p.parent != self.backup_dir.resolve() or not p.name.startswith("Battle.net.config-") or not p.is_file():
            raise launcher.LaunchError("Unknown backup")
        args = launcher.read_args(p)
        backup = launcher.write_args(args, self.backup_dir)
        return {"saved": args, "backup": str(backup), **self.launch_state()}

    def seeds_state(self):
        try:
            current = launcher.parse_args(launcher.read_args())["seed"]
        except (launcher.LaunchError, ValueError, OSError):
            current = None
        return {"seeds": self.seed_book.seeds, "purposes": self.seed_book.purposes(), "in_use": current}

    def seeds_save(self, body):
        self.seed_book.save(body.get("seed"), body.get("name"), body.get("purpose"), body.get("notes"),
                            body.get("id") or None)
        return self.seeds_state()

    def seeds_delete(self, seed_id):
        self.seed_book.delete(seed_id)
        return self.seeds_state()

    def launch_game(self):
        self._no_seed_run()
        launcher.launch_d2r()
        return {"launched": True}

    # ---------- seed run: start D2R once with -seed, then take it off and start again
    def seedrun_state(self):
        self.seed_run.tick()
        return {"run": self.seed_run.view()}

    def seedrun_start(self, body):
        seed_id = body.get("id") or None
        if seed_id:
            entry = self.seed_book.get(seed_id)
            seed = entry["seed"]
        else:
            seed = body.get("seed")
        self.seed_run.start(seed, seed_id)
        if seed_id:
            self.seed_book.mark_used(seed_id)
        return self.seedrun_state()

    def seedrun_again(self):
        self.seed_run.again()
        return self.seedrun_state()

    def seedrun_cancel(self):
        self.seed_run.cancel()
        return self.seedrun_state()

    def seedrun_dismiss(self):
        self.seed_run.dismiss()
        return self.seedrun_state()

    # ---------- art mod: item pictures and body-armour looks, by editing the extracted game files
    def _reload_art(self):
        self.art = ArtIndex(self.gd, paths.cache_dir() / "art", self.install)

    def artmod_state(self):
        am, gd, art = self.artmod, self.gd, self.art
        out = {"available": am.available, "data_dir": str(am.data_dir or ""), "game_running": applier.game_running(),
               "changes": [], "items": [], "looks": [], "sprites": []}
        if not am.available:
            return out
        keys = {kind: am.art_keys(kind) for kind in ART_FILES}
        changes = am.changes()
        changed = {(c["kind"], c["key"]) for c in changes}

        def row(kind, name, base, idx, art_key):
            key = am.find_key(kind, idx, keys[kind])
            exists = key is not None
            key = key or norm(idx)
            return {"kind": kind, "key": key, "name": name, "base": base, "art": art_key, "exists": exists,
                    "changed": (kind, key) in changed}

        for r in gd.tables["uniqueitems"]:
            if r.get("*ID", "") == "" or r.get("disabled") == "1" or not r.get("code"):
                continue
            uid = int(r["*ID"])
            b = gd.items.get(r["code"])
            out["items"].append(row("unique", gd.uniques[uid][0], b.name if b else r["code"], gd.uniques[uid][3],
                                    art.key_for(r["code"], uid, None, None, b.tier if b else None)))
        for r in gd.tables["setitems"]:
            if r.get("*ID", "") == "" or r.get("disabled") == "1":
                continue
            sid = int(r["*ID"])
            b = gd.items.get(r.get("item", ""))
            out["items"].append(row("set", gd.set_items[sid][0], b.name if b else r.get("item", ""),
                                    gd.set_items[sid][3], art.key_for(r.get("item", ""), None, sid, None,
                                                                      b.tier if b else None)))
        for code, b in gd.items.items():
            if code in keys["base"]:
                out["items"].append({"kind": "base", "key": code, "name": b.name, "base": f"{b.tier} {b.type}".strip(),
                                     "art": art.key_for(code, None, None, None, b.tier), "exists": True,
                                     "changed": ("base", code) in changed})
        for key in sorted(art.sprites):
            root, _, rest = key.partition("/")
            if root in ("armor", "weapon", "misc") and rest:
                out["sprites"].append({"key": key, "value": rest, "group": rest.rsplit("/", 1)[0] if "/" in rest else root,
                                       "name": rest.rsplit("/", 1)[-1]})
        out["changes"], out["looks"] = changes, am.looks()
        return out

    def _artmod(self, fn, *args):
        fn(*args)
        self._reload_art()
        return self.artmod_state()

    def artmod_set_art(self, body):
        return self._artmod(self.artmod.set_art, body.get("kind"), body.get("key"), body.get("sprite"))

    def artmod_reset_art(self, body):
        return self._artmod(self.artmod.reset_art, body.get("kind"), body.get("key"))

    def artmod_set_look(self, body):
        return self._artmod(self.artmod.set_look, body.get("armor"), body.get("like"))

    def artmod_reset_look(self, body):
        return self._artmod(self.artmod.reset_look, body.get("armor"))

    def artmod_restore(self, body):
        done = []
        state = self._artmod(lambda: done.extend(self.artmod.restore_all(bool(body.get("force")))))
        return {**state, "restored": done}

    def artmod_forget(self):
        return self._artmod(self.artmod.forget)

    # ---------- session tracker
    def _describe_found(self, it):
        j = self.item_json(it, "")
        return {k: j[k] for k in ("name", "base", "quality", "category", "ilvl", "art", "stats", "ethereal")}

    def session_state(self):
        t = self.tracker
        if t.session:
            t._update_totals()
        return {"active": t.session, "history": t.history()}

    def session_start(self):
        self.tracker.start()
        return self.session_state()

    def session_end(self):
        self.tracker.end()
        return self.session_state()

    def session_load(self, sid):
        return self.tracker.load(sid)

    def session_delete(self, sid):
        self.tracker.delete(sid)
        return self.session_state()

    # ---------- clean-up: duplicates and empty mules
    def duplicates(self):
        """Unique and set items you have more than once (top-level items only; socketed ones can't be deleted)."""
        rs = self.ruleset()
        groups = {}
        for f in self.walk():
            self._dup_add(groups, f.item, f.where, f.key, f.on_grid, rs)
        out = []
        for (kind, _), copies in groups.items():
            if len(copies) > 1:
                first = copies[0]
                out.append({"kind": kind, "name": first["name"], "base": first["base"], "art": first["art"],
                            "quality": first["quality"], "copies": copies})
        out.sort(key=lambda g: (g["kind"], g["name"]))
        return out

    def _dup_add(self, groups, it, where, key, deletable, rs):
        gd = self.gd
        if it.quality == 7 and it.unique_id in gd.uniques and not it.runeword:
            k = ("unique", it.unique_id)
        elif it.quality == 5 and it.set_id in gd.set_items:
            k = ("set", it.set_id)
        else:
            return
        j = self.item_json(it, where, key, rs=rs)
        j["deletable"] = deletable
        groups.setdefault(k, []).append(j)

    def empty_mules(self):
        """Characters with nothing worth keeping on them, and whether each can be deleted."""
        opts = planner.Options()
        out = []
        for c in self.world.characters:
            if not planner.is_mule_candidate(c, opts):
                continue
            stored = sum(1 for i in c.items if i.mode == MODE_STORED)
            ok, reason = empty_status(c)
            out.append({"name": c.name, "class": c.class_name, "level": c.level, "era": c.era_name,
                        "file": c.path.name, "stored": stored, "empty": ok, "reason": reason})
        out.sort(key=lambda m: (not m["empty"], m["stored"], m["name"].lower()))
        return out

    def plan_delete_items(self, keys):
        self.plan = planner.plan_item_deletions(self.world, keys)
        self.plan_id += 1
        return self.plan_json()

    def plan_delete_mules(self, names):
        self.plan = planner.plan_mule_deletions(self.world, names)
        self.plan_id += 1
        return self.plan_json()

    def plan_restyle(self, changes):
        self.plan = planner.plan_restyles(self.world, [(c["key"], c["gfx"]) for c in changes])
        self.plan_id += 1
        return self.plan_json()

    def plan_rename_char(self, name, new_name):
        self.plan = planner.plan_character_rename(self.world, name, new_name)
        self.plan_id += 1
        return self.plan_json()

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
                    parts = [f"{len(info.get('moves', []))} moved", f"{len(info.get('stacked', []))} stacked",
                             f"{len(info.get('renames', []))} renamed", f"{len(info.get('new_mules', []))} new mule(s)",
                             f"{len(info.get('deleted_items', []))} item(s) deleted",
                             f"{len(info.get('deleted_mules', []))} mule(s) deleted"]
                    entry["summary"] = f"{info.get('mode', 'stash')}: " + ", ".join(
                        x for x in parts if not x.startswith("0 "))
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


def _stash_label(s):
    era = "RotW" if s.modern else "Resurrected"
    return f"Shared stash ({era}{', hardcore' if s.hardcore else ''})"


def _where(it):
    return {MODE_EQUIPPED: "equipped", MODE_BELT: "belt"}.get(
        it.mode, {PAGE_INVENTORY: "inventory", PAGE_STASH: "stash", PAGE_CUBE: "cube"}.get(it.page, "?"))
