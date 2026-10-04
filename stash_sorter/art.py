"""Item artwork from the game's own HD inventory sprites (Data/hd/global/ui/items/**.sprite).

Only available when the game files have been extracted; otherwise the GUI draws coloured boxes. Decoded images
are cached as PNG files in the user's cache folder; the install folder is only read.

Sprite format ("SpA1"), as documented by Horadric Loot Box (MIT): u16 version at 4, u16 frame width at 6,
u32 width at 8, u32 height at 12, pixels from 0x28. Version 31 is raw RGBA, version 61 is DXT5/BC3.
"""

import json
import re
import struct
import threading
import zlib
from pathlib import Path

from .gamedata import find_install_dir


def norm(s):
    s = s.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def decode_sprite(data):
    if len(data) < 0x28 or data[:4].lower() != b"spa1":
        raise ValueError("not a SpA1 sprite")
    version, frame_w, width, height = struct.unpack_from("<HHII", data, 4)
    if not (0 < width <= 4096 and 0 < height <= 4096):
        raise ValueError("bad sprite size")
    if version == 31:
        px = data[0x28:0x28 + width * height * 4]
        if len(px) < width * height * 4:
            raise ValueError("sprite is truncated")
    elif version == 61:
        blocks = ((width + 3) // 4) * ((height + 3) // 4) * 16
        px = _decode_bc3(data[0x28:0x28 + blocks], width, height)
    else:
        raise ValueError(f"sprite version {version} is not supported")
    fw = frame_w if 0 < frame_w < width else width
    if fw != width:  # sprite sheet: keep the first frame
        px = b"".join(px[y * width * 4:y * width * 4 + fw * 4] for y in range(height))
    return fw, height, bytes(px)


def _decode_bc3(src, w, h):
    out = bytearray(w * h * 4)
    bw = (w + 3) // 4

    def rgb(v):
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))

    for bi in range(len(src) // 16):
        blk = src[bi * 16:bi * 16 + 16]
        bx, by = (bi % bw) * 4, (bi // bw) * 4
        a0, a1 = blk[0], blk[1]
        alpha = [a0, a1]
        for i in range(2, 8):
            if a0 > a1:
                alpha.append(((8 - i) * a0 + (i - 1) * a1) // 7)
            else:
                alpha.append(((6 - i) * a0 + (i - 1) * a1) // 5 if i < 6 else (0 if i == 6 else 255))
        abits = int.from_bytes(blk[2:8], "little")
        c0, c1 = rgb(blk[8] | blk[9] << 8), rgb(blk[10] | blk[11] << 8)
        cols = [c0, c1, tuple((2 * a + b) // 3 for a, b in zip(c0, c1)), tuple((a + 2 * b) // 3 for a, b in zip(c0, c1))]
        cbits = int.from_bytes(blk[12:16], "little")
        for py in range(4):
            for pxi in range(4):
                x, y = bx + pxi, by + py
                if x >= w or y >= h:
                    continue
                i = py * 4 + pxi
                c = cols[(cbits >> (2 * i)) & 3]
                o = (y * w + x) * 4
                out[o:o + 4] = bytes((c[0], c[1], c[2], alpha[(abits >> (3 * i)) & 7]))
    return bytes(out)


def encode_png(width, height, rgba):
    raw = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def _asset_map(path):
    """key -> asset path from items.json / uniques.json / sets.json (list of one-key objects)."""
    out = {}
    if not path.is_file():
        return out
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = json.loads(re.sub(r",(\s*[}\]])", r"\1", text))
    for entry in data if isinstance(data, list) else [data]:
        for k, v in entry.items():
            if isinstance(v, str):
                out[k] = v
            elif isinstance(v, dict):
                a = v.get("asset") or v.get("normal") or next((x for x in v.values() if isinstance(x, str)), None)
                if a:
                    out[k] = a
    return out


class ArtIndex:
    def __init__(self, gd, cache_dir, install_dir=None):
        self.gd = gd
        self.cache_dir = Path(cache_dir)
        self.sprites = {}  # key ("misc/amulet/amulet1") -> {"hd": path, "low": path}
        self.lock = threading.Lock()
        root = find_install_dir(install_dir)
        ui = root / "Data" / "hd" / "global" / "ui" / "items" if root else None
        self.available = bool(ui and ui.is_dir())
        self.by_base = {}
        self.items, self.uniques, self.sets = {}, {}, {}
        if not self.available:
            return
        for p in ui.rglob("*.sprite"):
            rel = p.relative_to(ui).as_posix().lower()
            low = rel.endswith(".lowend.sprite")
            key = rel[:-len(".lowend.sprite")] if low else rel[:-len(".sprite")]
            self.sprites.setdefault(key, {})["low" if low else "hd"] = p
        for key in self.sprites:
            self.by_base.setdefault(key.rsplit("/", 1)[-1], []).append(key)
        hd_items = root / "Data" / "hd" / "items"
        self.items = {k: v.lower() for k, v in _asset_map(hd_items / "items.json").items()}
        self.uniques = {norm(k): v.lower() for k, v in _asset_map(hd_items / "uniques.json").items()}
        self.sets = {norm(k): v.lower() for k, v in _asset_map(hd_items / "sets.json").items()}
        self._key_cache = {}

    def _resolve(self, asset):
        a = asset.lower().replace("\\", "/")
        for cand in (a, "misc/" + a, "weapon/" + a, "armor/" + a):
            if cand in self.sprites:
                return cand
        return None

    def key_for(self, code, unique_id=None, set_id=None, gfx=None, tier=None):
        ck = (code, unique_id, set_id, gfx)
        if ck in self._key_cache:
            return self._key_cache[ck]
        key = None
        tier_field = {"exceptional": "uber", "elite": "ultra"}.get(tier, "normal")
        if unique_id is not None and unique_id in self.gd.uniques:
            key = self._named(self.uniques, self.gd.uniques[unique_id][3])
        if key is None and set_id is not None and set_id in self.gd.set_items:
            key = self._named(self.sets, self.gd.set_items[set_id][3])
        base = self.items.get(code)
        if key is None and base and gfx is not None:
            key = self._resolve(f"{base}{gfx + 1}")
        if key is None and base:
            key = self._resolve(base)
        if key is None and code in self.gd.items:
            for k in self.by_base.get(norm(self.gd.items[code].name), []):
                key = k
                break
        self._key_cache[ck] = key
        return key

    def _named(self, table, name):
        n = norm(name)
        for cand in (n, n.replace("_", "")):
            if cand in table:
                k = self._resolve(table[cand])
                if k:
                    return k
        return None

    def png(self, key, low=True):
        """PNG bytes for a sprite key (cached on disk), or None."""
        entry = self.sprites.get(key)
        if not entry:
            return None
        variant = "low" if low and "low" in entry else "hd"
        if variant not in entry:
            variant = next(iter(entry))
        cached = self.cache_dir / variant / (key.replace("/", "__") + ".png")
        if cached.is_file():
            return cached.read_bytes()
        w, h, px = decode_sprite(entry[variant].read_bytes())
        data = encode_png(w, h, px)
        with self.lock:
            cached.parent.mkdir(parents=True, exist_ok=True)
            tmp = cached.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(cached)
        return data
