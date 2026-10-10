"""Art Mod: change which picture an item shows in your inventory, and how body armour looks on your character, by
editing the extracted game files the game reads when it runs with -direct -txt (Data/hd/items/*.json).

  * uniques.json / sets.json / items.json say which sprite each unique, set item and base item shows. They are edited
    as text, only the lines of the item you change, and the result is checked to differ from the original in exactly
    that item.
  * armorcomponentpresets.json says which body, arm and leg models a base body armour wears on the character.
    Pointing an armour at another armour's parts makes it look like that armour. It is per base item, so every item
    of that base changes (a Dusk Shroud made to look like Sacred Armor changes every Dusk Shroud).

Every file is copied before its first edit and can be put back. Nothing is written while D2R is running, or when a
file has changed since this program last wrote it (a game update, say).
"""

import copy
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from .art import norm

ART_FILES = {"unique": "uniques.json", "set": "sets.json", "base": "items.json"}
PRESETS = "armorcomponentpresets.json"
TIERS = ("normal", "uber", "ultra")


class ArtModError(Exception):
    pass


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _entries(data, key):
    """The dict(s) stored under `key` in a list of one-key objects."""
    return [e[key] for e in data if isinstance(e, dict) and key in e and isinstance(e[key], dict)]


def _set_fields(text, key, values):
    """`text` with the string fields `values` of every entry called `key` replaced; nothing else touched."""
    rx = re.compile(r'("%s"\s*:\s*\{)([^{}]*)(\})' % re.escape(key))
    count = 0

    def fix(m):
        nonlocal count
        count += 1
        block = m.group(2)
        for name, value in values.items():
            block, n = re.subn(r'("%s"\s*:\s*)"[^"]*"' % re.escape(name),
                               lambda mm: mm.group(1) + json.dumps(value), block, count=1)
            if not n:
                raise ArtModError(f"{key} has no \"{name}\" line to change")
        return m.group(1) + block + m.group(3)

    out = rx.sub(fix, text)
    if not count:
        raise ArtModError(f"{key} isn't in this file")
    return out


def _add_entry(text, key, values):
    """`text` with a new `{ "key": {...} }` entry at the end of the list, laid out like the others."""
    nl = "\r\n" if "\r\n" in text else "\n"
    end = text.rstrip().rfind("]")
    if end < 0:
        raise ArtModError("this file isn't a list of entries")
    head = text[:end].rstrip()
    if set(values) == {"asset"}:
        entry = f'  {{ "{key}": {{ "asset": {json.dumps(values["asset"])} }} }}'
    else:
        body = f",{nl}".join(f'  "{k}": {json.dumps(v)}' for k, v in values.items())
        entry = f'  {{ "{key}": {{{nl}{body}{nl}}} }}'
    return head + ("" if head.endswith("[") else ",") + nl + entry + nl + text[end:]


def _remove_entry(text, key):
    """`text` without the entries called `key` (those this program added at the end of a list)."""
    rx = re.compile(r',?\s*\{\s*"%s"\s*:\s*\{[^{}]*\}\s*\}' % re.escape(key))
    out = rx.sub("", text)
    return out if out.strip().startswith("[") else text


class ArtMod:
    def __init__(self, data_dir, store_dir, game_running=lambda: False):
        self.data_dir = Path(data_dir) if data_dir else None
        self.items_dir = self.data_dir / "hd" / "items" if self.data_dir else None
        self.store = Path(store_dir)
        self.game_running = game_running

    # ---------- files and saved originals
    @property
    def available(self):
        return bool(self.items_dir and self.items_dir.is_dir()
                    and all((self.items_dir / f).is_file() for f in (*ART_FILES.values(), PRESETS)))

    @property
    def _state_path(self):
        return self.store / "state.json"

    def _state(self):
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        self.store.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _path(self, name):
        return self.items_dir / name

    def _read(self, name):
        return self._path(name).read_bytes().decode("utf-8")

    def _original(self, name):
        """The file as it was before Art Mod first changed it (or as it is now, if it hasn't been)."""
        saved = self.store / "originals" / name
        return saved.read_bytes().decode("utf-8") if saved.is_file() else self._read(name)

    def _check_closed(self):
        if self.game_running():
            raise ArtModError("Diablo II: Resurrected is running. Close it completely and try again.")

    def _write(self, name, text):
        """Save `text` as the file, after copying the original the first time and checking nothing else changed it."""
        self._check_closed()
        path = self._path(name)
        current = path.read_bytes()
        state = self._state()
        entry = state.get(name)
        if entry is None:
            (self.store / "originals").mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, self.store / "originals" / name)
            entry = state[name] = {"original_sha": _sha(current), "written_sha": None}
        elif _sha(current) not in (entry["original_sha"], entry["written_sha"]):
            raise ArtModError(f"{name} has changed since Horadric Toolkit last wrote it (a game update?). "
                              "Use \"Accept the files as they are\" first, then make your changes again.")
        data = text.encode("utf-8")
        tmp = path.with_name(path.name + ".horadric.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        entry["written_sha"] = _sha(data)
        self._save_state(state)

    # ---------- inventory pictures
    def art_keys(self, kind):
        """{JSON key: [first entry]} for a picture file: the keys exactly as written (for editing)."""
        data = json.loads(self._read(ART_FILES[kind]))
        return {next(iter(e)): e[next(iter(e))] for e in data if isinstance(e, dict) and e}

    def find_key(self, kind, name, keys=None):
        """The JSON key for an item called `name` (the game matches loosely on case, punctuation and underscores)."""
        keys = keys if keys is not None else self.art_keys(kind)
        n = norm(name)
        for k in keys:
            if norm(k) in (n, n.replace("_", "")) or norm(k).replace("_", "") == n.replace("_", ""):
                return k
        return None

    def set_art(self, kind, key, sprite):
        """Show `sprite` (a value like "helmet/cap_hat") for the unique, set item or base item `key`."""
        if kind not in ART_FILES:
            raise ArtModError(f"unknown kind {kind!r}")
        if not isinstance(sprite, str) or not re.fullmatch(r"[\w./ -]+", sprite):
            raise ArtModError("that isn't a picture name")
        name = ART_FILES[kind]
        old = self._read(name)
        values = {"asset": sprite} if kind == "base" else {t: sprite for t in TIERS}
        expected = json.loads(old)
        if _entries(expected, key):
            new = _set_fields(old, key, values)
            for block in _entries(expected, key):
                block.update(values)
        else:  # the game's file has no entry for it (it shows its base item's picture): add one
            if not re.fullmatch(r"[a-z0-9_]+", key):
                raise ArtModError(f"{key} isn't in this file")
            new = _add_entry(old, key, values)
            expected.append({key: values})
        if json.loads(new) != expected:
            raise ArtModError(f"couldn't change {key} cleanly; nothing was written")
        self._write(name, new)

    def reset_art(self, kind, key):
        name = ART_FILES[kind]
        original = _entries(json.loads(self._original(name)), key)
        old = self._read(name)
        if not original:  # an entry this program added: take it out again
            new = _remove_entry(old, key)
            expected = [e for e in json.loads(old) if key not in e]
            if json.loads(new) != expected:
                raise ArtModError(f"couldn't take {key} out cleanly; nothing was written")
        else:
            new = _set_fields(old, key, {k: v for k, v in original[0].items() if isinstance(v, str)})
        if new != old:
            self._write(name, new)

    # ---------- how body armour looks on the character
    def looks(self):
        """[{name, components, original, changed}] for every body armour preset."""
        now = json.loads(self._read(PRESETS))
        was = {e["name"]: e["components"] for e in json.loads(self._original(PRESETS))}
        return [{"name": e["name"], "components": e["components"], "original": was.get(e["name"]),
                 "changed": was.get(e["name"]) != e["components"]} for e in now]

    def _dump_presets(self, data):
        return json.dumps(data, indent=4)

    def set_look(self, armor, like):
        """Make `armor` wear the body, arm and leg models of `like`."""
        raw = self._read(PRESETS)
        data = json.loads(raw)
        if self._dump_presets(data) != raw:
            raise ArtModError(f"{PRESETS} isn't laid out the way this version expects, so it can't be rewritten safely")
        by = {e["name"]: e for e in data}
        if armor not in by or like not in by:
            raise ArtModError("unknown armour")
        by[armor]["components"] = copy.deepcopy(by[like]["components"])
        self._write(PRESETS, self._dump_presets(data))

    def reset_look(self, armor):
        raw = self._read(PRESETS)
        data = json.loads(raw)
        was = {e["name"]: e["components"] for e in json.loads(self._original(PRESETS))}
        if armor not in was:
            raise ArtModError("unknown armour")
        for e in data:
            if e["name"] == armor:
                e["components"] = copy.deepcopy(was[armor])
        if self._dump_presets(data) != raw:
            self._write(PRESETS, self._dump_presets(data))

    # ---------- what has changed, and putting everything back
    def changes(self):
        """Everything that differs from the saved originals: [{kind, key, was, now}]."""
        out = []
        for kind, name in ART_FILES.items():
            if name not in self._state():
                continue
            was = {k: v for k, v in self._pairs(self._original(name))}
            for k, v in self._pairs(self._read(name)):
                if was.get(k) != v:
                    out.append({"kind": kind, "key": k, "was": was.get(k), "now": v})
        if PRESETS in self._state():
            for look in self.looks():
                if look["changed"]:
                    out.append({"kind": "look", "key": look["name"], "was": look["original"], "now": look["components"]})
        return out

    @staticmethod
    def _pairs(text):
        for e in json.loads(text):
            for k, v in e.items():
                yield k, v

    def restore_all(self, force=False):
        """Put every changed file back as it was. Returns the file names restored."""
        self._check_closed()
        state = self._state()
        done = []
        for name, entry in list(state.items()):
            saved = self.store / "originals" / name
            if not saved.is_file():
                continue
            current = _sha(self._path(name).read_bytes())
            if current not in (entry["original_sha"], entry["written_sha"]) and not force:
                raise ArtModError(f"{name} has changed since Horadric Toolkit last wrote it, so it wasn't put back. "
                                  "Use \"Accept the files as they are\" to stop tracking it.")
            tmp = self._path(name).with_name(name + ".horadric.tmp")
            shutil.copyfile(saved, tmp)
            os.replace(tmp, self._path(name))
            saved.unlink()
            del state[name]
            done.append(name)
        self._save_state(state)
        return done

    def forget(self):
        """Stop tracking: the files as they are now become the baseline (the saved originals are deleted)."""
        shutil.rmtree(self.store / "originals", ignore_errors=True)
        self._save_state({})
