"""Session tracker: watches the save folder (read-only) and logs every Save & Exit as a run.

D2R writes a character to disk when you Save & Exit, so each write of a played character is one "run".
A run records what appeared (loot), what disappeared (sold, used, dropped), experience, levels and gold.

Items are recognised by their 32-bit item id, which survives durability loss, tome charges and socketing.
Runes, gems and other stackables have no id and are counted by type (a Stackables-tab stack counts as its size),
so moving items between characters or into the Stackables tab is never mistaken for loot.
"""

import datetime as dt
import json
import struct
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import catalog as C
from .items import _V100_STACKABLE
from .planner import Options, is_mule_candidate
from .savefiles import parse_character, parse_stash, checksum, TAB_STACKABLES, STAT_GOLD, STAT_GOLD_BANK

SETTLE_SECONDS = 3.0  # the game writes several files on exit; wait until they stop changing
HIDDEN_TYPES = ("poti", "scro", "book")  # consumables picked up and used up every run: not worth listing
STAT_LEVEL, STAT_EXPERIENCE = 12, 13


def _identity(it, in_stack_tab=False):
    """(key, weight) under which an item is counted."""
    if it.code in _V100_STACKABLE or it.item_id is None:
        return ("c", it.code), ((it.stack_count or 0) if in_stack_tab else 1)
    return ("id", it.item_id, it.code), 1


def _walk(items):
    for it in items:
        yield it
        yield from _walk(it.children)


@dataclass
class FileState:
    stamp: tuple
    kind: str
    name: str
    counts: Counter = field(default_factory=Counter)
    examples: dict = field(default_factory=dict)  # identity key -> item (to describe it)
    level: int = 0
    xp: int = 0
    gold: int = 0
    mule: bool = False


class Snapshotter:
    """Parses save files into item counts, re-parsing only files whose size or time changed."""

    def __init__(self, save_dir, gd):
        self.save_dir, self.gd = Path(save_dir), gd
        self.cache = {}

    def stamps(self):
        out = {}
        for p in list(self.save_dir.glob("*.d2s")) + list(self.save_dir.glob("*.d2i")):
            try:
                st = p.stat()
                out[p] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
        return out

    def snapshot(self, stamps=None):
        """{path: FileState}, or None if a file is mid-write (try again shortly)."""
        stamps = stamps or self.stamps()
        snap = {}
        for p, stamp in stamps.items():
            cached = self.cache.get(p)
            if cached and cached.stamp == stamp:
                snap[p] = cached
                continue
            try:
                data = p.read_bytes()
                if (p.stat().st_mtime_ns, p.stat().st_size) != stamp:
                    return None
                state = self._parse(p, data, stamp)
            except Exception:
                return None  # half-written or locked: the next poll will see it complete
            self.cache[p] = snap[p] = state
        for p in list(self.cache):
            if p not in stamps:
                del self.cache[p]
        return snap

    def _parse(self, p, data, stamp):
        if p.suffix.lower() == ".d2s":
            if struct.unpack_from("<I", data, 12)[0] != checksum(data):
                raise ValueError("checksum mismatch (file still being written)")
            ch = parse_character(p, self.gd, data)
            st = FileState(stamp, "char", ch.name, level=ch.stats.get(STAT_LEVEL, ch.level),
                           xp=ch.stats.get(STAT_EXPERIENCE, 0),
                           gold=ch.stats.get(STAT_GOLD, 0) + ch.stats.get(STAT_GOLD_BANK, 0),
                           mule=is_mule_candidate(ch, Options()))
            for it in _walk(ch.items):
                self._count(st, it, False)
            return st
        stash = parse_stash(p, self.gd, data)
        st = FileState(stamp, "stash", p.stem, gold=sum(t.gold for t in stash.tabs))
        for t in stash.tabs:
            for it in _walk(t.items):
                self._count(st, it, t.type == TAB_STACKABLES)
        return st

    def _count(self, st, it, in_stack_tab):
        key, weight = _identity(it, in_stack_tab)
        if weight:
            st.counts[key] += weight
            st.examples.setdefault(key, it)


def _totals(snap):
    counts, examples = Counter(), {}
    for st in snap.values():
        counts.update(st.counts)
        for k, it in st.examples.items():
            examples.setdefault(k, it)
    return counts, examples


class Tracker:
    def _now(self):
        return self.now().isoformat(timespec="seconds")

    def __init__(self, save_dir, gd, names, store_dir, describe, now=None):
        self.gd, self.names = gd, names
        self.now = now or dt.datetime.now  # real local time (the PC clock may be moved for terror zones)
        self.last_mono = time.monotonic()
        self.snapshotter = Snapshotter(save_dir, gd)
        self.store = Path(store_dir)
        self.describe = describe  # item -> dict for display
        self.session = None
        self.last = None          # snapshot at the last recorded run
        self.last_stamps = None
        self.changed_at = None
        self.known_grail = set()
        self._resume()

    # ---------- persistence
    def _path(self, sid):
        return self.store / f"session-{sid}.json"

    def _save(self):
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self._path(self.session["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(self.session, indent=1), encoding="utf-8")
        tmp.replace(self._path(self.session["id"]))

    def _resume(self):
        if not self.store.is_dir():
            return
        for p in sorted(self.store.glob("session-*.json"), reverse=True):
            try:
                s = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not s.get("ended"):
                self.session = s
                s.setdefault("notes", []).append(
                    f"{self._now()}: resumed after Stash Sorter was closed; games played meanwhile aren't counted.")
                self._baseline()
                self._save()
                return

    def history(self):
        out = []
        if self.store.is_dir():
            for p in sorted(self.store.glob("session-*.json"), reverse=True):
                try:
                    s = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if s.get("ended"):
                    out.append({k: s.get(k) for k in ("id", "started", "ended", "totals", "characters")})
        return out

    def load(self, sid):
        return json.loads(self._path(sid).read_text(encoding="utf-8"))

    def delete(self, sid):
        if self.session and self.session["id"] == sid:
            raise ValueError("End the session before deleting it.")
        self._path(sid).unlink(missing_ok=True)

    # ---------- lifecycle
    def _baseline(self):
        snap = None
        for _ in range(20):
            snap = self.snapshotter.snapshot()
            if snap is not None:
                break
            time.sleep(0.5)
        if snap is None:
            raise RuntimeError("Couldn't read the save folder (is the game saving right now?)")
        self.last = snap
        self.last_stamps = {p: s.stamp for p, s in snap.items()}
        self.changed_at = None
        counts, examples = _totals(snap)
        self.known_grail = {self._grail_key(it) for it in examples.values()} - {None}
        for run in self.session.get("runs", []):
            self.known_grail |= set(map(tuple, run.get("grail_keys", [])))

    def start(self):
        if self.session:
            return self.session
        self.last_mono = time.monotonic()
        self.session = {"id": self.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
                        "started": self._now(), "ended": None, "runs": [], "notes": [], "characters": [],
                        "totals": {}}
        self._baseline()
        self._update_totals()
        self._save()
        return self.session

    def end(self):
        if not self.session:
            return None
        self.poll(force=True)
        self.session["ended"] = self._now()
        self._update_totals()
        self._save()
        done, self.session = self.session, None
        return done

    # ---------- watching
    def poll(self, force=False, now=None):
        """Call every couple of seconds. Records a run once changed files have settled. Returns the new run, if any."""
        if not self.session:
            return None
        now = now if now is not None else time.monotonic()
        stamps = self.snapshotter.stamps()
        if stamps != self.last_stamps:
            self.last_stamps = stamps
            self.changed_at = now
            if not force:
                return None
        if self.changed_at is None or (not force and now - self.changed_at < SETTLE_SECONDS):
            return None
        snap = self.snapshotter.snapshot(stamps)
        if snap is None:
            return None
        self.changed_at = None
        run = self._diff(self.last, snap)
        self.last = snap
        if run:
            self.session["runs"].append(run)
            self._update_totals()
            self._save()
        return run

    def _grail_key(self, it):
        if it.quality == 7 and it.unique_id in self.gd.uniques and not it.runeword:
            return ("u", it.unique_id)
        if it.quality == 5 and it.set_id in self.gd.set_items:
            return ("s", it.set_id)
        return None

    def _item_list(self, delta, examples):
        shown, hidden = [], 0
        for key, n in sorted(delta.items(), key=lambda kv: str(kv[0])):
            it = examples.get(key)
            if it is None:
                continue
            base = self.gd.items.get(it.code)
            if base is not None and base.has(*HIDDEN_TYPES):
                hidden += n
                continue
            d = self.describe(it)
            d["count"] = n
            shown.append(d)
        rank = {"unique": 0, "set": 1, "runeword": 2, "rare": 4, "crafted": 4, "magic": 5}
        shown.sort(key=lambda d: (rank.get(d.get("quality"), 3 if d.get("category") == "runes" else 6), d["name"]))
        return shown, hidden

    def _diff(self, old, new):
        old_counts, old_ex = _totals(old)
        new_counts, new_ex = _totals(new)
        gained, lost = new_counts - old_counts, old_counts - new_counts
        played, mules, xp, levels = [], [], 0, 0
        for p, st in new.items():
            before = old.get(p)
            if st.kind != "char" or (before and before.stamp == st.stamp):
                continue
            (mules if st.mule and (not before or st.xp == before.xp) else played).append(st.name)
            if before:
                xp += st.xp - before.xp
                levels += st.level - before.level
        gold = sum(s.gold for s in new.values()) - sum(s.gold for s in old.values())
        found, found_hidden = self._item_list(gained, new_ex)
        left, left_hidden = self._item_list(lost, old_ex)
        if not (played or mules) and not found and not left and not gold:
            return None
        grail = []
        for key in gained:
            gk = self._grail_key(new_ex[key])
            if gk and gk not in self.known_grail:
                self.known_grail.add(gk)
                grail.append(gk)
        runs = [r for r in self.session["runs"] if r["kind"] == "run"]
        kind = "run" if played else "mule"
        # run lengths use a steady timer, so moving the PC clock for terror zones doesn't distort them
        seconds = int(time.monotonic() - self.last_mono)
        if kind == "run":
            self.last_mono = time.monotonic()
        for name in played:
            if name not in self.session["characters"]:
                self.session["characters"].append(name)
        return {
            "n": len(runs) + 1 if kind == "run" else None, "kind": kind, "at": self._now(),
            "seconds": seconds,
            "characters": played or mules, "xp": xp, "levels": levels, "gold": gold,
            "found": found, "found_hidden": found_hidden, "left": left, "left_hidden": left_hidden,
            "grail": [self._grail_name(g) for g in grail], "grail_keys": [list(g) for g in grail],
        }

    def _grail_name(self, gk):
        kind, i = gk
        return self.gd.uniques[i][0] if kind == "u" else self.gd.set_items[i][0]

    def _update_totals(self):
        s = self.session
        runs = [r for r in s["runs"] if r["kind"] == "run"]
        end = s["ended"] or self._now()
        seconds = max(1, int((dt.datetime.fromisoformat(end) - dt.datetime.fromisoformat(s["started"])).total_seconds()))
        notable = [f for r in runs for f in r["found"]
                   if f.get("quality") in ("unique", "set", "runeword") or f.get("category") == "runes"]
        s["totals"] = {
            "runs": len(runs), "seconds": seconds,
            "runs_per_hour": round(len(runs) * 3600 / seconds, 1),
            "avg_run_seconds": int(sum(r["seconds"] for r in runs) / len(runs)) if runs else 0,
            "found": sum(f["count"] for r in runs for f in r["found"]),
            "left": sum(f["count"] for r in s["runs"] for f in r["left"]),
            "notable": [f["name"] for f in notable][:30],
            "grail": [g for r in s["runs"] for g in r["grail"]],
            "xp": sum(r["xp"] for r in s["runs"]), "levels": sum(r["levels"] for r in s["runs"]),
            "gold": sum(r["gold"] for r in s["runs"]),
        }
