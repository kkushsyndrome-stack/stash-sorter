"""Diablo II: Resurrected item (de)serialisation, save versions 0x61 (97) to 0x69 (105).

Items are bit-packed with no length prefix, so the whole item has to be decoded to
find where it ends. Each item keeps its original bytes and only the fixed-position
location fields are ever patched, so moving an item never re-encodes its stats.

Field order for v105 (Reign of the Warlock) was cross-checked against the MIT-licensed
Horadric Loot Box parser (github.com/pyrosplat/Horadric-Loot-Box).
"""

from dataclasses import dataclass, field

from .bits import BitReader, get_bits, set_bits

# Item code alphabet, Huffman-coded since D2R 1.0 (save version 0x61).
_HUFFMAN_TREE = [[[[["w", "u"], [["8", ["y", ["5", ["j", []]]]], "h"]], ["s", [["2", "n"], "x"]]],
                  [[["c", ["k", "f"]], "b"], [["t", "m"], ["9", "7"]]]],
                 [" ", [[[["e", "d"], "p"], ["g", [[["z", "q"], "3"], ["v", "6"]]]],
                        [["r", "l"], ["a", [["1", ["4", "0"]], ["i", "o"]]]]]]]

# Item flags (first 32 bits).
F_IDENTIFIED = 0x00000010
F_SOCKETED = 0x00000800
F_EAR = 0x00010000
F_COMPACT = 0x00200000
F_ETHEREAL = 0x00400000
F_PERSONALIZED = 0x01000000
F_LOW_QUALITY = 0x02000000  # gamble preview items: nothing stored after the code
F_RUNEWORD = 0x04000000
F_CHRONICLE = 0x10000000
F_CHRONICLE_COMPACT = 0x20000000

# Fixed bit offsets inside every D2R item record.
POS_MODE = 35       # 3 bits
POS_EQUIPPED = 38   # 4 bits
POS_X = 42          # 4 bits
POS_Y = 46          # 4 bits
POS_PAGE = 50       # 3 bits

MODE_STORED, MODE_EQUIPPED, MODE_BELT, MODE_GROUND, MODE_CURSOR, MODE_DROPPING, MODE_SOCKETED = range(7)
# page field as stored in the file
PAGE_NONE, PAGE_INVENTORY, PAGE_CUBE, PAGE_STASH = 0, 1, 4, 5

QUALITY_NAMES = {1: "inferior", 2: "normal", 3: "superior", 4: "magic", 5: "set",
                 6: "rare", 7: "unique", 8: "crafted", 9: "tempered"}

# Stats whose value is followed by extra values that carry no stat id of their own.
_STAT_FOLLOWERS = {17: (18,), 48: (49,), 50: (51,), 52: (53,), 54: (55, 56), 57: (58, 59)}

# v100/v101 stored a stack count on these codes unconditionally; v102+ uses a presence bit.
_V100_STACKABLE = frozenset(
    "rvs rvl gcv gfv gsv gzv gpv gcy gfy gsy gly gpy gcb gfb gsb glb gpb gcg gfg gsg glg gpg gcr gfr gsr glr "
    "gpr gcw gfw gsw glw gpw skc skf sku skl skz r01 r02 r03 r04 r05 r06 r07 r08 r09 r10 r11 r12 r13 r14 r15 "
    "r16 r17 r18 r19 r20 r21 r22 r23 r24 r25 r26 r27 r28 r29 r30 r31 r32 r33 pk1 pk2 pk3 dhn bey mbr toa tes "
    "ceh bet fed".split())


class ItemParseError(Exception):
    pass


@dataclass
class Item:
    raw: bytearray
    save_version: int
    flags: int
    mode: int
    equipped: int
    x: int
    y: int
    page: int
    code: str = ""
    item_id: int = None
    gfx: int = None  # picture variant (rings, amulets, charms, jewels)
    ilvl: int = 0
    quality: int = 2
    unique_id: int = None
    set_id: int = None
    runeword_id: int = None
    prefixes: tuple = ()
    suffixes: tuple = ()
    rare_name: tuple = ()
    defense: int = None
    quantity: int = None
    stack_count: int = None  # RotW Stackables-tab count
    stack_flag_bit: int = None  # bit offset of the stack flag inside raw (v102+)
    total_sockets: int = 0
    stats: list = field(default_factory=list)  # [(stat_id, param, value)]
    runeword_stats: list = field(default_factory=list)
    children: list = field(default_factory=list)
    n_children: int = 0

    @property
    def compact(self):
        return bool(self.flags & F_COMPACT)

    @property
    def identified(self):
        return bool(self.flags & F_IDENTIFIED)

    @property
    def ethereal(self):
        return bool(self.flags & F_ETHEREAL)

    @property
    def socketed(self):
        return bool(self.flags & F_SOCKETED)

    @property
    def runeword(self):
        return bool(self.flags & F_RUNEWORD)

    @property
    def personalized(self):
        return bool(self.flags & F_PERSONALIZED)

    @property
    def quality_name(self):
        return QUALITY_NAMES.get(self.quality, str(self.quality))

    def set_location(self, mode, page, x, y, equipped=0):
        set_bits(self.raw, POS_MODE, 3, mode)
        set_bits(self.raw, POS_EQUIPPED, 4, equipped)
        set_bits(self.raw, POS_X, 4, x)
        set_bits(self.raw, POS_Y, 4, y)
        set_bits(self.raw, POS_PAGE, 3, page)
        self.mode, self.equipped, self.x, self.y, self.page = mode, equipped, x, y, page

    def set_stack_count(self, n):
        if self.stack_count is None or self.stack_flag_bit is None:
            raise ValueError(f"{self.code} is not a Stackables-tab stack")
        set_bits(self.raw, self.stack_flag_bit + 1, 8, n)
        self.stack_count = n

    def to_bytes(self):
        out = bytearray(self.raw)
        for c in self.children:
            out += c.to_bytes()
        return bytes(out)


def _read_code(r):
    out = []
    for _ in range(4):
        node = _HUFFMAN_TREE
        while isinstance(node, list):
            if not node:
                raise ItemParseError("invalid item code bits")
            node = node[r.read(1)]
        out.append(node)
    return "".join(out).rstrip()


def _read_string(r, char_bits):
    chars = []
    for _ in range(16):
        c = r.read(char_bits)
        if c == 0:
            break
        chars.append(chr(c))
    return "".join(chars)


def _read_stat_list(r, gd):
    out = []
    while True:
        sid = r.read(9)
        if sid == 0x1FF:
            return out
        for s in (sid,) + _STAT_FOLLOWERS.get(sid, ()):
            st = gd.stats.get(s)
            if st is None or st.save_bits == 0:
                raise ItemParseError(f"stat {s} has no save bits")
            param = r.read(st.save_param_bits) if st.save_param_bits else 0
            out.append((s, param, r.read(st.save_bits) - st.save_add))


def _read_realm(r):
    if r.read(1):
        r.read(32), r.read(32), r.read(32), r.read(32)


def _read_adv_stack(r, it, version, start_bit):
    if version <= 99:
        return
    if version <= 101:
        if it.code not in _V100_STACKABLE:
            return
    else:
        it.stack_flag_bit = r.pos - start_bit
        if not r.read(1):
            return
    it.stack_count = r.read(8)


def _read_gold(r):
    v = r.read(32) if r.read(1) else r.read(12)
    r.read(1)
    return v


def read_item(data, byte_pos, version, gd):
    """Parse one item (and its socketed children) starting at byte_pos. Returns (item, next_byte_pos)."""
    if version < 0x61:
        raise ItemParseError(f"save version {version} predates D2R; not supported")
    r = BitReader(data, byte_pos * 8)
    flags = r.read(32)
    r.read(3)  # item format version
    mode = r.read(3)
    if mode in (MODE_GROUND, MODE_DROPPING):
        raise ItemParseError("ground items are not stored in saves")
    it = Item(raw=bytearray(), save_version=version, flags=flags, mode=mode,
              equipped=r.read(4), x=r.read(4), y=r.read(4), page=r.read(3))
    char_bits = 8 if version > 97 else 7

    try:
        if flags & F_COMPACT:
            if flags & F_EAR:
                it.code = "ear"
                r.read(3), r.read(7), _read_string(r, char_bits)
                base = None
            else:
                it.code = _read_code(r)
                base = gd.items.get(it.code)
                if base is None:
                    raise ItemParseError(f"unknown item code {it.code!r}")
                if base.has("gold"):
                    _read_gold(r)
            if base is not None and base.quest and base.quest_diff:
                r.read(gd.stats[356].save_bits)
            _read_realm(r)
            _read_adv_stack(r, it, version, byte_pos * 8)
        else:
            it.code = _read_code(r)
            base = gd.items.get(it.code)
            if base is None:
                raise ItemParseError(f"unknown item code {it.code!r}")
            if flags & F_LOW_QUALITY:
                it.quality = 1
            else:
                _read_complete(r, it, base, version, char_bits, gd, byte_pos * 8)
    except (ItemParseError, EOFError, KeyError) as e:
        raise ItemParseError(f"item {it.code or '?'} at byte {byte_pos}: {e}") from None

    r.align()
    end = r.byte_pos
    it.raw = bytearray(data[byte_pos:end])
    pos = end
    for _ in range(it.n_children):
        child, pos = read_item(data, pos, version, gd)
        it.children.append(child)
    return it, pos


def _read_complete(r, it, base, version, char_bits, gd, start_bit):
    it.n_children = r.read(3)
    it.item_id = r.read(32)
    it.ilvl = r.read(7)
    it.quality = q = r.read(4)
    if r.read(1):  # alternate graphic
        it.gfx = r.read(3)
    if r.read(1):  # class-specific auto affix
        r.read(11)

    if q in (1, 3):
        r.read(3)
    elif q == 4:
        it.prefixes, it.suffixes = (r.read(11),), (r.read(11),)
    elif q == 5:
        it.set_id = r.read(12)
    elif q == 7:
        it.unique_id = r.read(12)
    elif q in (6, 8):
        it.rare_name = (r.read(8), r.read(8))
        pre, suf = [], []
        for _ in range(3):
            pre.append(r.read(11) if r.read(1) else 0)
            suf.append(r.read(11) if r.read(1) else 0)
        it.prefixes, it.suffixes = tuple(pre), tuple(suf)
    elif q == 9:
        it.rare_name = (r.read(8), r.read(8))
    elif q == 2:
        if base.has("char"):
            r.read(1), r.read(11)
        elif base.has("body") and not base.has("play"):
            r.read(10)
        elif base.has("scro", "book"):
            r.read(5)
    else:
        raise ItemParseError(f"unknown quality {q}")

    if it.flags & F_RUNEWORD:
        it.runeword_id = r.read(16) & 0xFFF
    if it.flags & F_EAR:
        r.read(3), r.read(7), _read_string(r, char_bits)
    elif it.flags & F_PERSONALIZED:
        _read_string(r, char_bits)
    _read_realm(r)

    if base.has("armo"):
        ac = gd.stats[31]
        it.defense = r.read(ac.save_bits) - ac.save_add
    if base.has("armo", "weap"):
        mx, cur = gd.stats[73], gd.stats[72]
        if r.read(mx.save_bits) - mx.save_add > 0:
            r.read(cur.save_bits)
    elif base.has("gold"):
        _read_gold(r)

    if version > 104:
        if r.read(1):
            it.quantity = r.read(9)
    elif base.stackable:
        it.quantity = r.read(9)

    if it.flags & F_SOCKETED:
        it.total_sockets = r.read(gd.stats[194].save_bits)
    set_mask = r.read(5) if q == 5 else 0

    it.stats = _read_stat_list(r, gd)
    for i in range(5):
        if set_mask & (1 << i):
            _read_stat_list(r, gd)
    if it.flags & F_RUNEWORD:
        it.runeword_stats = _read_stat_list(r, gd)

    if version > 99 and it.flags & F_CHRONICLE:
        r.read(16)
        compact = it.flags & F_CHRONICLE_COMPACT
        if not compact:
            r.read(32)
        count = 1 if compact else min(r.read(4), 8)
        for _ in range(count):
            r.read(32), r.read(32)
    _read_adv_stack(r, it, version, start_bit)


def read_items(data, byte_pos, count, version, gd):
    items, pos = [], byte_pos
    for _ in range(count):
        it, pos = read_item(data, pos, version, gd)
        items.append(it)
    return items, pos


def read_item_list(data, byte_pos, version, gd):
    """Parse a 'JM' + count item list. Returns (items, next_byte_pos)."""
    if data[byte_pos:byte_pos + 2] != b"JM":
        raise ItemParseError(f"expected 'JM' at byte {byte_pos}, found {data[byte_pos:byte_pos + 2]!r}")
    count = int.from_bytes(data[byte_pos + 2:byte_pos + 4], "little")
    return read_items(data, byte_pos + 4, count, version, gd)


def write_item_list(items):
    out = bytearray(b"JM")
    out += len(items).to_bytes(2, "little")
    for it in items:
        out += it.to_bytes()
    return bytes(out)


def location_of(raw):
    return (get_bits(raw, POS_MODE, 3), get_bits(raw, POS_PAGE, 3),
            get_bits(raw, POS_X, 4), get_bits(raw, POS_Y, 4))
