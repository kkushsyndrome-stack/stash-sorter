"""Render docs/social-preview.png (1280x640), the image GitHub shows when the repo link is shared.

    python tools/make_preview.py

Uses item artwork from your extracted D2R install and Microsoft Edge (or Chrome) in headless mode.
Contains no save data: the stash shown is a hand-picked selection of items.
"""

import base64
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stash_sorter import gamedata, paths  # noqa: E402
from stash_sorter.art import ArtIndex  # noqa: E402
from stash_sorter.planner import Grid  # noqa: E402

OUT = ROOT / "docs" / "social-preview.png"
CELL, COLS, ROWS = 52, 10, 8
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]

# (x, y, base code, unique name or None, picture variant (0-based, like the saves), quality) - hand placed on a 10x8 grid, leaving the
# lower-right block free for the hover tooltip next to the highlighted item.
ITEMS = [
    (0, 0, "6lw", "Windforce", None, "unique"), (2, 0, "pa9", "Herald of Zakarum", None, "unique"),
    (4, 0, "uar", "Tyrael's Might", None, "unique"), (6, 0, "oba", "The Oculus", None, "unique"),
    (7, 0, "cm3", "Gheed's Fortune", None, "unique"), (8, 0, "cm3", None, 1, "magic"), (9, 0, "cm3", None, 2, "magic"),
    (4, 3, "ulc", "Arachnid Mesh", None, "unique"), (6, 3, "rin", "The Stone of Jordan", 2, "unique"),
    (7, 3, "rin", "Bul-Kathos' Wedding Band", 3, "unique"), (8, 3, "amu", "Mara's Kaleidoscope", 0, "unique"),
    (9, 3, "amu", "Highlord's Wrath", 1, "unique"), (4, 4, "uap", "Harlequin Crest", None, "unique"),
    (0, 4, "xtb", "War Traveler", None, "unique"), (2, 4, "usk", "Andariel's Visage", None, "unique"),
    (0, 6, "cm2", "Hellfire Torch", None, "unique"), (1, 6, "pk1", None, None, "normal"),
    (2, 6, "pk2", None, None, "normal"), (3, 6, "pk3", None, None, "normal"), (4, 6, "cm1", "Annihilus", None, "unique"),
    (5, 6, "r30", None, None, "rune"), (4, 7, "r31", None, None, "rune"), (5, 7, "r33", None, None, "rune"),
]
HIGHLIGHT = "Harlequin Crest"
COLORS = {"unique": "#c9a96b", "magic": "#7b8cff", "rare": "#f2e05a", "normal": "#bdb6aa", "rune": "#e08a3c",
          "set": "#4fd15a"}

FEATURES = [
    ("Empty &amp; sort", "stash onto your mules by category, rename mules to match"),
    ("See everything", "the game's own item art and tooltips, search, holy grail"),
    ("Item levels", "craft bait, rerolls, sockets, keepers"),
    ("Safe", "preview first, full backups, verified all-or-nothing writes, undo"),
]


def build_html(gd, art):
    by_name = {v[0]: k for k, v in gd.uniques.items()}
    grid = Grid(COLS, ROWS)
    cells = []
    for x, y, code, uname, gfx, quality in ITEMS:
        base = gd.items[code]
        if not grid.fits(x, y, base.width, base.height):
            raise ValueError(f"{uname or code} overlaps another item at ({x},{y})")
        grid.mark(x, y, base.width, base.height)
        uid = by_name.get(uname) if uname else None
        key = art.key_for(code, uid, None, gfx, base.tier)
        png = art.png(key, low=False) if key else None
        src = "data:image/png;base64," + base64.b64encode(png).decode() if png else ""
        hot = " hot" if uname == HIGHLIGHT else ""
        cells.append(f'<div class="it{hot}" style="left:{x * CELL}px;top:{y * CELL}px;width:{base.width * CELL - 2}px;'
                     f'height:{base.height * CELL - 2}px;--q:{COLORS[quality]}"><img src="{src}"></div>')
    feats = "".join(f"<li><b>{t}</b> {d}</li>" for t, d in FEATURES)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: 1280px; height: 640px; overflow: hidden; }}
body {{ background: radial-gradient(ellipse at 75% 40%, #2a2117 0%, #120f0c 55%, #0b0907 100%); color: #e8e0d2;
  font-family: "Segoe UI", system-ui, sans-serif; position: relative; }}
.left {{ position: absolute; left: 64px; top: 70px; width: 540px; }}
.mark {{ color: #c8a45d; font-size: 15px; letter-spacing: 4px; text-transform: uppercase; margin-bottom: 14px; }}
h1 {{ font-family: "Palatino Linotype", "Book Antiqua", Georgia, serif; font-size: 76px; font-weight: 700; line-height: 1;
  background: linear-gradient(180deg, #f3dca0 0%, #c8a45d 55%, #8a6a2e 100%); -webkit-background-clip: text; color: transparent;
  filter: drop-shadow(0 2px 8px #0009); }}
.tag {{ font-size: 23px; line-height: 1.35; color: #e8e0d2; margin: 20px 0 26px; }}
ul {{ list-style: none; }}
li {{ font-size: 17px; color: #a39887; margin: 9px 0; padding-left: 22px; position: relative; }}
li::before {{ content: "\\25C6"; color: #c8a45d; position: absolute; left: 0; font-size: 12px; top: 4px; }}
li b {{ color: #e8e0d2; font-weight: 600; }}
.foot {{ position: absolute; left: 64px; bottom: 44px; font-size: 15px; color: #a39887; }}
.foot span {{ color: #c8a45d; }}
.panel {{ position: absolute; right: 64px; top: 72px; padding: 14px; background: #17130fe6; border: 1px solid #4a3f30;
  border-radius: 10px; box-shadow: 0 20px 60px #000c, inset 0 0 0 1px #00000080; }}
.tabs {{ display: flex; gap: 6px; margin-bottom: 10px; }}
.tabs div {{ font-size: 13px; padding: 4px 12px; border: 1px solid #3a332b; border-radius: 6px; color: #a39887; }}
.tabs div.on {{ border-color: #c8a45d; color: #c8a45d; }}
.inv {{ position: relative; width: {COLS * CELL + 2}px; height: {ROWS * CELL + 2}px; border: 1px solid #3a332b; border-radius: 4px;
  background: repeating-linear-gradient(0deg, transparent 0 {CELL - 1}px, #2c2620 {CELL - 1}px {CELL}px),
    repeating-linear-gradient(90deg, transparent 0 {CELL - 1}px, #2c2620 {CELL - 1}px {CELL}px), #0f0d0b; }}
.it {{ position: absolute; margin: 1px; border: 1px solid color-mix(in srgb, var(--q) 55%, transparent); border-radius: 3px;
  background: color-mix(in srgb, var(--q) 14%, #120f0c); display: flex; align-items: center; justify-content: center; }}
.it img {{ width: 100%; height: 100%; object-fit: contain; }}
.it.hot {{ border-color: #f3dca0; box-shadow: 0 0 0 2px #c8a45d, 0 0 18px #c8a45d88; z-index: 2; }}
.tip {{ position: absolute; left: {6 * CELL + 10}px; top: {4 * CELL + 6}px; width: {4 * CELL - 14}px; background: #0b0a09f2; border: 1px solid #4a3f30;
  border-radius: 6px; padding: 10px 12px; text-align: center; font-size: 13px; box-shadow: 0 10px 30px #000c; }}
.tip .n {{ color: #c9a96b; font-weight: 700; font-size: 15px; }} .tip .b {{ color: #c9a96b; }}
.tip .s {{ color: #8a9cff; margin-top: 6px; line-height: 1.4; font-size: 12px; }}
.tip .m {{ color: #a39887; font-size: 11px; margin-top: 6px; border-top: 1px solid #3a332b; padding-top: 5px; }}
</style></head><body>
<div class="left">
  <div class="mark">Diablo II: Resurrected</div>
  <h1>Horadric Toolkit</h1>
  <p class="tag">Empty and organise your shared stash across your mules, safely.</p>
  <ul>{feats}</ul>
</div>
<div class="foot">Free &amp; open source &middot; offline saves up to Reign of the Warlock &middot;
  <span>github.com/kkushsyndrome-stack/stash-sorter</span></div>
<div class="panel"><div class="tabs"><div class="on">Tab 1</div><div>Tab 2</div><div>Tab 3</div><div>Stackables</div></div>
  <div class="inv">{"".join(cells)}
  <div class="tip"><div class="n">Harlequin Crest</div><div class="b">Shako</div>
    <div class="s">+2 to All Skills<br>+2 to all Attributes<br>Damage Reduced by 10%<br>50% Better Chance of Getting Magic Items</div>
    <div class="m">item level 87 &middot; Unique gear</div></div></div></div>
</body></html>"""


def main():
    gd = gamedata.load()
    art = ArtIndex(gd, paths.cache_dir() / "art")
    if not art.available:
        sys.exit("Item artwork not found: this needs a D2R install with extracted game files.")
    browser = next((b for b in BROWSERS if os.path.isfile(b)), None)
    if not browser:
        sys.exit("Needs Microsoft Edge or Google Chrome to render the image.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        page = Path(tmp) / "preview.html"
        page.write_text(build_html(gd, art), encoding="utf-8")
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                        "--force-device-scale-factor=1", f"--user-data-dir={Path(tmp) / 'profile'}",
                        "--window-size=1280,640", f"--screenshot={OUT}", page.as_uri()], check=True)
        # the browser launcher can return before the screenshot is written
        last = -1
        for _ in range(60):
            size = OUT.stat().st_size if OUT.exists() else -1
            if size > 0 and size == last:
                break
            last = size
            time.sleep(0.5)
    if not OUT.exists():
        sys.exit("The browser did not produce a screenshot.")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
