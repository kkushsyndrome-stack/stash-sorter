"""Carries out a plan. This is the only module that writes to the save folder.

Safety rules:
  * refuses while Diablo II: Resurrected is running (the game would overwrite our changes on exit);
  * refuses if any save changed on disk since it was loaded;
  * zips the whole save folder before touching anything;
  * builds every new file in memory and proves it is valid before writing (see `_verify`);
  * commits as one journaled transaction: temp files first, then a journal, then the swap. Mules and new
    files are swapped in before the stash and old names are removed last, so an interruption can duplicate
    an item but never lose one, and the journal lets the next start roll back to the backup automatically.
"""

import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path

from . import catalog as C
from .items import MODE_STORED, PAGE_STASH, identity as _identity
from .planner import DEFAULT_GRIDS, STASH_TAB_GRID, STACK_MAX
from .savefiles import parse_character, parse_stash, valid_character_name, empty_status, TAB_STACKABLES, TAB_NORMAL

GAME_PROCESSES = ("D2R.exe",)
JOURNAL = "journal.json"
TMP_SUFFIX = ".stash-sorter.new"


class ApplyError(Exception):
    pass


def game_running():
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False
    return any(f'"{p.lower()}"' in out for p in GAME_PROCESSES)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------- backups
def _check_backup_location(save_dir, backup_root):
    saves, backups = Path(save_dir).resolve(), Path(backup_root).resolve()
    if backups == saves or saves in backups.parents:
        raise ApplyError("The backup folder must not be inside the save folder.")


def backup_save_dir(save_dir, backup_root, label="before-apply"):
    """Zip every file in the save folder (including subfolders such as mods/)."""
    _check_backup_location(save_dir, backup_root)
    backup_root = Path(backup_root)
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_root / f"saves-{stamp}-{label}.zip"
    n = 1
    while target.exists():
        n += 1
        target = backup_root / f"saves-{stamp}-{label}-{n}.zip"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(save_dir).rglob("*")):
            if p.is_file() and not p.name.endswith(TMP_SUFFIX):
                z.write(p, p.relative_to(save_dir))
                count += 1
    with zipfile.ZipFile(target) as z:  # make sure the backup is readable before relying on it
        if z.testzip() is not None or len(z.namelist()) != count:
            raise ApplyError(f"Backup {target} failed verification")
    return target


# ---------------------------------------------------------------------------------------------- transactions
def _write_tmp(path, data):
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if tmp.read_bytes() != data:
        raise ApplyError(f"{tmp.name}: write verification failed")
    return tmp


def _commit(save_dir, backup_root, backup_zip, writes, renames=(), deletes=(), log=print):
    """Apply a set of file changes as one recoverable transaction.

    writes:  {Path: bytes} (new or replaced files), renames: [(Path, Path)], deletes: [Path]
    """
    save_dir = Path(save_dir)
    rel = lambda p: Path(p).relative_to(save_dir).as_posix()  # noqa: E731
    existed = {rel(p) for p in list(writes) + [s for s, _ in renames] + list(deletes) if Path(p).exists()}
    created = {rel(p) for p in writes if not Path(p).exists()} | {rel(d) for _, d in renames}
    tmps = {}
    try:
        for path, data in writes.items():
            tmps[path] = _write_tmp(Path(path), data)
    except Exception:
        for t in tmps.values():
            t.unlink(missing_ok=True)
        raise
    journal = Path(backup_root) / JOURNAL
    journal.write_text(json.dumps({"state": "prepared", "save_dir": str(save_dir), "backup": str(backup_zip),
                                   "restore": sorted(existed), "remove": sorted(created),
                                   "tmp": [str(t) for t in tmps.values()]}, indent=1), encoding="utf-8")
    # characters (including new ones) first, stashes after, renames next, old names last
    for path in sorted(tmps, key=lambda p: Path(p).suffix.lower() == ".d2i"):
        os.replace(tmps[path], path)
    staged = []
    for i, (src, dst) in enumerate(renames):  # two-phase so swaps (A->B, B->A) can't collide
        t = Path(src).with_name(f"__stash_sorter_{i}{Path(src).suffix}")
        os.replace(src, t)
        staged.append((t, dst))
    for t, dst in staged:
        os.replace(t, dst)
    for p in deletes:
        Path(p).unlink(missing_ok=True)
    # read everything back from disk; on any surprise, roll back from the backup before reporting
    problems = [Path(p).name for p, data in writes.items() if not Path(p).is_file() or Path(p).read_bytes() != data]
    problems += [Path(d).name for _, d in renames if not Path(d).is_file()]
    problems += [Path(p).name for p in deletes if Path(p).exists()]
    if problems:
        recover(backup_root, log=log)
        raise ApplyError(f"After writing, {', '.join(problems[:5])} did not match what was intended. "
                         "Everything was rolled back to the backup.")
    journal.unlink()
    return existed, created


def pending_journal(backup_root):
    """The journal of an interrupted apply, or None."""
    j = Path(backup_root) / JOURNAL
    if not j.is_file():
        return None
    try:
        return json.loads(j.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"state": "unreadable", "path": str(j)}


def recover(backup_root, log=print):
    """Roll back an interrupted apply using its journal and backup."""
    j = pending_journal(backup_root)
    if not j:
        return None
    if j.get("state") != "prepared":
        raise ApplyError(f"Journal {Path(backup_root) / JOURNAL} is unreadable; restore a backup manually.")
    save_dir = Path(j["save_dir"])
    with zipfile.ZipFile(j["backup"]) as z:
        names = set(z.namelist())
        for r in j["restore"]:
            if r in names:
                dest = save_dir / r
                dest.write_bytes(z.read(r))
        for r in j["remove"]:
            if r not in names:
                (save_dir / r).unlink(missing_ok=True)
    for t in j.get("tmp", []):
        Path(t).unlink(missing_ok=True)
    for p in save_dir.glob("__stash_sorter_*"):
        p.unlink()
    (Path(backup_root) / JOURNAL).unlink()
    log(f"Rolled back the interrupted change using {j['backup']}.")
    return j


# ---------------------------------------------------------------------------------------------- building
def _clone(it):
    c = copy.copy(it)
    c.raw = bytearray(it.raw)
    c.children = [_clone(ch) for ch in it.children]
    return c


def _with_name(ch, new_name):
    head = bytearray(ch.head)
    encoded = new_name.encode("ascii")
    if not valid_character_name(new_name) or len(encoded) >= ch.name_len:
        raise ApplyError(f"'{new_name}' is not a valid character name")
    head[ch.name_offset:ch.name_offset + ch.name_len] = encoded + b"\0" * (ch.name_len - len(encoded))
    c = copy.copy(ch)
    c.head = bytes(head)
    c.name = new_name
    return c


def _check_grids(ch, gd):
    grids = {page: [[None] * w for _ in range(h)] for page, (w, h) in DEFAULT_GRIDS.items()}
    carry = Counter()
    for it in ch.items:
        g = C.carry_one_group(it, gd)
        if g:
            carry[g] += 1
        if it.mode != MODE_STORED or it.page not in grids:
            continue
        problem = _mark(grids[it.page], it, gd)
        if problem:
            return f"{ch.name}: {problem}"
    for g, n in carry.items():
        if n > 1:
            return f"{ch.name}: carries {n} items of carry-one group {g}"
    return None


def _mark(grid, it, gd):
    w, h = C.item_size(it, gd)
    for dy in range(h):
        for dx in range(w):
            x, y = it.x + dx, it.y + dy
            if y >= len(grid) or x >= len(grid[0]):
                return f"{it.code} at ({it.x},{it.y}) sticks out of the grid"
            if grid[y][x] is not None:
                return f"{it.code} overlaps {grid[y][x]} at ({x},{y})"
            grid[y][x] = it.code
    return None


class _Change:
    """Everything a plan does to the save folder, built in memory."""

    def __init__(self, world, plan):
        self.world, self.plan, self.gd = world, plan, world.gd
        self.chars = {}    # id(original Character) -> (original, new Character)
        self.stashes = {}  # id(original SharedStash) -> (original, new SharedStash)
        self.created = {}  # new mule name -> (template Character, new Character)
        self.renames = {(r.file or r.old + '.d2s').lower(): r.new for r in plan.renames}

    def _char_copy(self, ch):
        if id(ch) not in self.chars:
            c = copy.copy(ch)
            c.items = [_clone(it) for it in ch.items]
            self.chars[id(ch)] = (ch, c)
        return self.chars[id(ch)][1]

    def _stash_copy(self, st):
        if id(st) not in self.stashes:
            s = copy.copy(st)
            s.tabs = []
            for t in st.tabs:
                tc = copy.copy(t)
                tc.items = [_clone(it) for it in t.items]
                s.tabs.append(tc)
            self.stashes[id(st)] = (st, s)
        return self.stashes[id(st)][1]

    def _remove(self, item):
        for ch in self.world.characters:
            for i, it in enumerate(ch.items):
                if it is item:
                    c = self._char_copy(ch)
                    c.items[i] = None
                    return
        for st in self.world.stashes:
            for ti, t in enumerate(st.tabs):
                for i, it in enumerate(t.items):
                    if it is item:
                        s = self._stash_copy(st)
                        s.tabs[ti].items[i] = None
                        return
        raise ApplyError("an item in the plan no longer exists")

    def build(self):
        w, plan = self.world, self.plan
        for nm in plan.new_mules:
            template = next(c for c in w.characters if c.path.name == nm.template)
            if not template.can_be_template:
                raise ApplyError(f"{template.name} can't be used as a template for new mules")
            c = _with_name(template, nm.name)
            c.items = []
            c.path = template.path.with_name(nm.name + ".d2s")
            self.created[nm.name] = (template, c)
        for d in plan.delete_chars:
            ch = next((c for c in w.characters if c.path.name == d.file), None)
            if ch is None:
                raise ApplyError(f"{d.name} no longer exists")
            ok, reason = empty_status(ch)
            if not ok:
                raise ApplyError(f"{d.name} is not empty any more: {reason}")
        for m in plan.moves + plan.merges + plan.deletions:
            self._remove(m.item)
        for m in plan.moves:
            it = _clone(m.item)
            if m.dst_tab is not None:
                st = w.stash(m.dst_file)
                it.set_location(MODE_STORED, PAGE_STASH, m.x, m.y, 0)
                self._stash_copy(st).tabs[m.dst_tab].items.append(it)
            elif m.dst_char in self.created:
                it.set_location(MODE_STORED, m.page, m.x, m.y, 0)
                self.created[m.dst_char][1].items.append(it)
            else:
                ch = next(c for c in w.characters if c.path.name == m.dst_file)
                it.set_location(MODE_STORED, m.page, m.x, m.y, 0)
                self._char_copy(ch).items.append(it)
        bumps = Counter(m.code for m in plan.merges)
        if bumps:
            st = self._stash_copy(w.stash(plan.stash_file))
            for t in st.tabs:
                if t.type != TAB_STACKABLES:
                    continue
                for i, it in enumerate(t.items):
                    if it is not None and it.stack_count is not None and bumps.get(it.code):
                        n = it.stack_count + bumps.pop(it.code)
                        if n > STACK_MAX:
                            raise ApplyError(f"stack of {it.code} would exceed {STACK_MAX}")
                        it.set_stack_count(n)
            if bumps:
                raise ApplyError(f"no stack found for {sorted(bumps)}")
        for ch in [o for o, _ in self.chars.values()] + [c for c in w.characters if c.path.name.lower() in self.renames]:
            c = self._char_copy(ch)
            c.items = [it for it in c.items if it is not None]
            if ch.path.name.lower() in self.renames:
                new = self.renames[ch.path.name.lower()]
                renamed = _with_name(c, new)
                renamed.path = ch.path.with_name(new + ".d2s")
                self.chars[id(ch)] = (ch, renamed)
        for _, s in self.stashes.values():
            for t in s.tabs:
                t.items = [it for it in t.items if it is not None]

    def files(self):
        """(writes {Path: bytes}, renames [(src, dst)], deletes [Path])"""
        writes, renames, deletes = {}, [], []
        for ch, c in self.chars.values():
            writes[c.path] = c.to_bytes()
            if c.path != ch.path:
                deletes.append(ch.path)
                for p in self.world.save_dir.iterdir():
                    if p.is_file() and p.stem.lower() == ch.path.stem.lower() and p.suffix.lower() != ".d2s":
                        renames.append((p, p.with_name(c.path.stem + p.suffix)))
        for d in self.plan.delete_chars:  # an empty mule: its save and all of its side files
            for p in self.world.save_dir.iterdir():
                if p.is_file() and p.stem.lower() == Path(d.file).stem.lower():
                    deletes.append(p)
        for name, (template, c) in self.created.items():
            writes[c.path] = c.to_bytes()
            for p in self.world.save_dir.iterdir():  # copy the template's side files (key bindings etc.)
                if p.is_file() and p.stem.lower() == template.path.stem.lower() and p.suffix.lower() != ".d2s":
                    writes[p.with_name(name + p.suffix)] = p.read_bytes()
        for st, s in self.stashes.values():
            writes[s.path] = s.to_bytes()
        # a chain of renames (A -> B while B -> C) writes the very path an old name would delete
        written = {Path(p).name.lower() for p in writes}
        deletes = [p for p in deletes if p.name.lower() not in written]
        existing = {p.name.lower() for p in self.world.save_dir.iterdir()}
        leaving = {p.name.lower() for p in deletes} | {s.name.lower() for s, _ in renames}
        replacing = ({o.path.name.lower() for o, _ in self.chars.values()}
                     | {o.path.name.lower() for o, _ in self.stashes.values()})
        for p in list(writes) + [d for _, d in renames]:
            name = Path(p).name.lower()
            if name in existing and name not in leaving and name not in replacing:
                raise ApplyError(f"{Path(p).name} already exists")
        return writes, renames, deletes

    def verify(self, writes):
        gd, plan = self.gd, self.plan
        before_ids, after_ids = Counter(), Counter()
        before_stacks, after_stacks = Counter(), Counter()
        for ch, c in self.chars.values():
            data = writes[c.path]
            again = parse_character(c.path, gd, data)
            if again.to_bytes() != data:
                raise ApplyError(f"{c.path.name}: rebuilt file does not round-trip")
            same = again.head[:8] == ch.head[:8]
            same &= again.head[16:ch.name_offset] == ch.head[16:ch.name_offset]
            same &= again.head[ch.name_offset + ch.name_len:] == ch.head[ch.name_offset + ch.name_len:]
            if not same or again.tail != ch.tail:
                raise ApplyError(f"{c.path.name}: character data outside the item list changed")
            if again.name != c.name:
                raise ApplyError(f"{c.path.name}: name check failed")
            problem = _check_grids(again, gd)
            if problem:
                raise ApplyError(problem)
            before_ids.update(_identity(it) for it in ch.items)
            after_ids.update(_identity(it) for it in again.items)
        for name, (template, c) in self.created.items():
            again = parse_character(c.path, gd, writes[c.path])
            if again.name != name or again.gold != 0 or again.tail != template.tail or again.level != template.level:
                raise ApplyError(f"new mule {name} failed verification")
            problem = _check_grids(again, gd)
            if problem:
                raise ApplyError(problem)
            after_ids.update(_identity(it) for it in again.items)
        for st, s in self.stashes.values():
            data = writes[s.path]
            again = parse_stash(s.path, gd, data)
            if again.to_bytes() != data:
                raise ApplyError(f"{s.path.name}: rebuilt file does not round-trip")
            if [t.header[:16] for t in again.tabs] != [t.header[:16] for t in st.tabs] or \
                    [t.rest for t in again.tabs] != [t.rest for t in st.tabs]:
                raise ApplyError(f"{s.path.name}: stash headers or chronicle data changed")
            for t_old, t_new in zip(st.tabs, again.tabs):
                for tab, ids, stacks in ((t_old, before_ids, before_stacks), (t_new, after_ids, after_stacks)):
                    for it in tab.items:
                        if tab.type == TAB_STACKABLES:
                            stacks[it.code] += it.stack_count or 0
                        else:
                            ids[_identity(it)] += 1
                if t_new.type == TAB_NORMAL:
                    grid = [[None] * STASH_TAB_GRID[0] for _ in range(STASH_TAB_GRID[1])]
                    for it in t_new.items:
                        problem = _mark(grid, it, gd)
                        if problem and t_new is not None and any(
                                m.dst_file == s.path.name for m in plan.moves):
                            raise ApplyError(f"{s.path.name}: {problem}")
        for m in plan.merges:
            before_ids[_identity(m.item)] -= 1
            before_stacks[m.code] += 1
        for d in plan.deletions:  # deleted on purpose: they must be gone, and nothing else
            before_ids[_identity(d.item)] -= 1
        if +before_ids != +after_ids:
            lost = sum((before_ids - after_ids).values())
            gained = sum((after_ids - before_ids).values())
            raise ApplyError(f"item conservation check failed ({lost} missing, {gained} unexpected)")
        if +before_stacks != +after_stacks:
            raise ApplyError("stack counts do not add up")


# ---------------------------------------------------------------------------------------------- public API
def apply_plan(world, plan, backup_root, log=print, skip_game_check=False):
    """Executes `plan`. Returns a summary dict. Raises ApplyError (with nothing written) on any problem."""
    if plan.is_empty:
        return {"changed_files": 0, "backup": None}
    if pending_journal(backup_root):
        raise ApplyError("A previous change was interrupted. Roll it back first (Backups tab or `recover`).")
    if not skip_game_check and game_running():
        raise ApplyError("Diablo II: Resurrected is running. Close the game completely and try again.")
    changed = world.changed_on_disk()
    if changed:
        raise ApplyError("These saves changed since they were loaded (reload and re-plan): " +
                         ", ".join(p.name for p in changed[:10]))
    change = _Change(world, plan)
    change.build()
    writes, renames, deletes = change.files()
    log(f"Built {len(writes)} file(s); verifying...")
    change.verify(writes)
    log("Verification passed. Backing up the save folder...")
    backup = backup_save_dir(world.save_dir, backup_root)
    log(f"Backup written: {backup}")
    existed, created = _commit(world.save_dir, backup_root, backup, writes, renames, deletes, log)

    after = {}
    for p in list(writes) + [d for _, d in renames]:
        after[Path(p).relative_to(world.save_dir).as_posix()] = _sha(Path(p).read_bytes())
    summary = {
        "time": dt.datetime.now().isoformat(timespec="seconds"),
        "backup": str(backup),
        "changed_files": len(writes) + len(renames) + len(deletes),
        "mode": plan.options.mode,
        "moves": [{"item": m.name, "from": m.src_where, "to": m.dst_where} for m in plan.moves],
        "stacked": [{"item": m.name, "from": m.src_where} for m in plan.merges],
        "renames": [{"old": r.old, "new": r.new} for r in plan.renames],
        "new_mules": [{"name": n.name, "template": n.template} for n in plan.new_mules],
        "deleted_items": [{"item": d.name, "from": d.where} for d in plan.deletions],
        "deleted_mules": [d.name for d in plan.delete_chars],
        "files_before": sorted(existed),
        "files_after": after,
        "written": {Path(p).name: _sha(d) for p, d in writes.items()},
    }
    log_path = Path(backup_root) / (Path(backup).stem + "-log.json")
    log_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["log"] = str(log_path)
    log(f"Done: {len(plan.moves)} moved, {len(plan.merges)} stacked, {len(plan.renames)} renamed, "
        f"{len(plan.new_mules)} new mule(s), {len(plan.deletions)} item(s) and {len(plan.delete_chars)} mule(s) "
        "deleted.")
    return summary


def undo_status(log_file, save_dir):
    """(can_undo, reason) for the apply described by `log_file`."""
    try:
        info = json.loads(Path(log_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "log unreadable"
    if "files_after" not in info:
        return False, "made by an older version (use full restore)"
    if not Path(info["backup"]).is_file():
        return False, "its backup zip is missing"
    for rel, sha in info["files_after"].items():
        p = Path(save_dir) / rel
        if not p.is_file() or _sha(p.read_bytes()) != sha:
            return False, f"{rel} has changed since (played since then?) - use full restore instead"
    return True, "ok"


def undo_apply(log_file, save_dir, backup_root, log=print, skip_game_check=False):
    """Undo exactly one apply: put back the files it changed, remove the ones it created."""
    if not skip_game_check and game_running():
        raise ApplyError("Diablo II: Resurrected is running. Close the game completely and try again.")
    ok, reason = undo_status(log_file, save_dir)
    if not ok:
        raise ApplyError(f"Can't undo: {reason}")
    info = json.loads(Path(log_file).read_text(encoding="utf-8"))
    save_dir = Path(save_dir)
    writes = {}
    with zipfile.ZipFile(info["backup"]) as z:
        names = set(z.namelist())
        for rel in info["files_before"]:
            if rel not in names:
                raise ApplyError(f"{rel} is missing from the backup")
            writes[save_dir / rel] = z.read(rel)
    deletes = [save_dir / rel for rel in info["files_after"] if rel not in info["files_before"]]
    safety = backup_save_dir(save_dir, backup_root, label="before-undo")
    _commit(save_dir, backup_root, safety, writes, (), deletes, log)
    done = Path(log_file).with_name(Path(log_file).stem + "-undone.json")
    Path(log_file).replace(done)
    log(f"Undone. A backup of the state before undoing is at {safety}.")
    return {"safety_backup": str(safety)}


def restore_backup(backup_zip, save_dir, backup_root, skip_game_check=False):
    """Restore a backup zip over the save folder (after backing up the current state too)."""
    if not skip_game_check and game_running():
        raise ApplyError("Diablo II: Resurrected is running. Close the game completely and try again.")
    _check_backup_location(save_dir, backup_root)
    backup_zip = Path(backup_zip)
    with zipfile.ZipFile(backup_zip) as z:
        if z.testzip() is not None:
            raise ApplyError(f"{backup_zip.name} is damaged")
        for name in z.namelist():
            dest = (Path(save_dir) / name).resolve()
            if Path(save_dir).resolve() not in dest.parents:
                raise ApplyError(f"refusing to extract {name} outside the save folder")
        safety = backup_save_dir(save_dir, backup_root, label="before-restore")
        listed = {Path(n).as_posix().lower() for n in z.namelist()}
        for p in Path(save_dir).rglob("*"):
            if p.is_file() and p.relative_to(save_dir).as_posix().lower() not in listed:
                p.unlink()
        z.extractall(save_dir)
    return safety
