"""Character (.d2s) and shared stash (.d2i) files.

Everything except the item lists is kept as raw bytes and written back unchanged.
"""

import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .bits import BitReader
from .items import read_item_list, write_item_list, MODE_STORED

MAGIC = 0xAA55AA55
ERA_NAMES = {1: "Classic", 2: "Resurrected", 3: "Reign of the Warlock"}
CLASS_NAMES = ["Amazon", "Sorceress", "Necromancer", "Paladin", "Barbarian", "Druid", "Assassin", "Warlock"]

TAB_NORMAL, TAB_STACKABLES, TAB_CHRONICLE = 0, 1, 2

# What follows the item list of a character with no corpse, mercenary, golem (or RotW extra data):
# a character file is only used as a template for new mules when its tail is exactly one of these.
EMPTY_TAILS = (b"JM\x00\x00jfkf\x00\x01\x00lf\x00\x00", b"JM\x00\x00jfkf\x00")
STAT_GOLD, STAT_GOLD_BANK = 14, 15
F_STARTER = 0x00020000  # item flag: part of a new character's starting gear


class SaveFormatError(Exception):
    pass


def checksum(data):
    s = 0
    for i, b in enumerate(data):
        if 12 <= i < 16:
            b = 0
        s = (((s << 1) | (s >> 31)) + b) & 0xFFFFFFFF
    return s


@dataclass
class Character:
    path: Path
    version: int
    name: str
    class_id: int
    level: int
    flags: int
    era: int
    head: bytes
    items: list
    tail: bytes
    name_offset: int
    name_len: int
    stats: dict = field(default_factory=dict)  # character stat id -> value (gold, level, ...)

    @property
    def gold(self):
        return self.stats.get(STAT_GOLD, 0) + self.stats.get(STAT_GOLD_BANK, 0)

    @property
    def can_be_template(self):
        """True when cloning this file (minus its items) can't duplicate anything: no gold, merc, corpse, golem."""
        return self.gold == 0 and self.tail in EMPTY_TAILS

    @property
    def class_name(self):
        return CLASS_NAMES[self.class_id] if 0 <= self.class_id < len(CLASS_NAMES) else f"Class {self.class_id}"

    @property
    def hardcore(self):
        return bool(self.flags & 0x04)

    @property
    def era_name(self):
        return ERA_NAMES.get(self.era, f"Era {self.era}")

    def to_bytes(self):
        out = bytearray(self.head) + write_item_list(self.items) + self.tail
        struct.pack_into("<I", out, 8, len(out))
        struct.pack_into("<I", out, 12, 0)
        struct.pack_into("<I", out, 12, checksum(out))
        return bytes(out)


def parse_character(path, gd, data=None):
    path = Path(path)
    data = data if data is not None else path.read_bytes()
    if len(data) < 64 or struct.unpack_from("<I", data, 0)[0] != MAGIC:
        raise SaveFormatError(f"{path.name}: not a D2 character file")
    version = struct.unpack_from("<I", data, 4)[0]
    if version < 0x61:
        raise SaveFormatError(f"{path.name}: save version {version} predates D2R")
    new_header = version >= 104
    flags_off = 20 if new_header else 36
    flags = struct.unpack_from("<I", data, flags_off)[0]
    class_id = data[flags_off + 4]
    n_skills = data[flags_off + 6]
    level = data[flags_off + 7]
    char_size = 387 if new_header else 319
    preview_off = 16 + char_size - (228 if new_header else 144)
    if new_header:
        name_offset, name_len = preview_off + 124, 96
        era = data[preview_off + 73]
    else:
        name_offset, name_len = 20, 16
        era = 2 if flags & 0x20 else 1
    name = data[name_offset:name_offset + name_len].split(b"\0")[0].decode("utf-8", "replace")
    if not name and not new_header:
        name_offset, name_len = preview_off + 76, 60
        name = data[name_offset:name_offset + name_len].split(b"\0")[0].decode("utf-8", "replace")

    off = 16 + char_size + 298 + 80 + 52
    if data[off:off + 2] != b"gf":
        raise SaveFormatError(f"{path.name}: stats section not found at {off}")
    r = BitReader(data, (off + 2) * 8)
    char_stats = {}
    while True:
        sid = r.read(9)
        if sid == 0x1FF:
            break
        st = gd.stats.get(sid)
        if st is None:
            raise SaveFormatError(f"{path.name}: unknown character stat {sid}")
        if st.csv_param:
            r.read(st.csv_param)
        char_stats[sid] = r.read(st.csv_bits)
    r.align()
    off = r.byte_pos
    if data[off:off + 2] != b"if":
        raise SaveFormatError(f"{path.name}: skills section not found at {off}")
    off += 2 + n_skills
    items, end = read_item_list(data, off, version, gd)
    if data[end:end + 2] != b"JM":
        raise SaveFormatError(f"{path.name}: corpse section not found after items")
    stored_size = struct.unpack_from("<I", data, 8)[0]
    if stored_size != len(data):
        raise SaveFormatError(f"{path.name}: header size {stored_size} != file size {len(data)}")
    return Character(path=path, version=version, name=name, class_id=class_id, level=level, flags=flags,
                     era=era, head=data[:off], items=items, tail=data[end:],
                     name_offset=name_offset, name_len=name_len, stats=char_stats)


@dataclass
class StashTab:
    header: bytes
    version: int
    type: int
    items: list
    rest: bytes  # chronicle body, or any bytes after the item list

    @property
    def gold(self):
        return struct.unpack_from("<I", self.header, 12)[0]


@dataclass
class SharedStash:
    path: Path
    tabs: list = field(default_factory=list)
    trailing: bytes = b""

    @property
    def hardcore(self):
        return "hardcore" in self.path.name.lower()

    @property
    def modern(self):
        return self.path.name.lower().startswith("modern") or any(t.type != TAB_NORMAL for t in self.tabs)

    @property
    def era(self):
        return 3 if self.modern else 2

    def normal_tabs(self):
        return [t for t in self.tabs if t.type == TAB_NORMAL]

    def to_bytes(self):
        out = bytearray()
        for t in self.tabs:
            body = t.rest if t.type == TAB_CHRONICLE else write_item_list(t.items) + t.rest
            header = bytearray(t.header)
            size = 64 + len(body)
            if size > 0xFFFF:
                raise SaveFormatError("stash tab too large")
            struct.pack_into("<H", header, 16, size)
            out += header + body
        return bytes(out + self.trailing)


def parse_stash(path, gd, data=None):
    path = Path(path)
    data = data if data is not None else path.read_bytes()
    st = SharedStash(path=path)
    off = 0
    while len(data) - off >= 64 and struct.unpack_from("<I", data, off)[0] == MAGIC:
        header = data[off:off + 64]
        fmt, version = struct.unpack_from("<II", header, 4)
        size = struct.unpack_from("<H", header, 16)[0]
        if size < 64 or off + size > len(data):
            raise SaveFormatError(f"{path.name}: tab {len(st.tabs) + 1} has invalid size {size}")
        tab_type = header[20] if fmt >= 2 else TAB_NORMAL
        body_start, body_end = off + 64, off + size
        if tab_type == TAB_CHRONICLE:
            items, rest = [], data[body_start:body_end]
        else:
            items, end = read_item_list(data, body_start, version, gd)
            if end > body_end:
                raise SaveFormatError(f"{path.name}: tab {len(st.tabs) + 1} items overflow the tab")
            rest = data[end:body_end]
        st.tabs.append(StashTab(header=header, version=version, type=tab_type, items=items, rest=rest))
        off = body_end
    if not st.tabs:
        raise SaveFormatError(f"{path.name}: not a shared stash file")
    st.trailing = data[off:]
    return st


VALID_NAME = re.compile(r"^(?=.{2,15}$)[A-Za-z]+(?:[-_][A-Za-z]+)?$")


def valid_character_name(name):
    return bool(VALID_NAME.match(name))


def empty_status(ch):
    """(is_empty, reason): nothing stored, no gold, no merc/corpse/golem, and only the starting gear on it."""
    stored = sum(1 for i in ch.items if i.mode == MODE_STORED)
    if stored:
        return False, f"holds {stored} item(s)"
    other = sum(1 for i in ch.items if not i.flags & F_STARTER)
    if other:
        return False, f"wears or carries {other} item(s) that aren't starting gear"
    if ch.gold:
        return False, f"has {ch.gold:,} gold"
    if ch.tail not in EMPTY_TAILS:
        return False, "has a mercenary, corpse or golem"
    return True, "empty"
