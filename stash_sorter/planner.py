"""Works out where every item should go. Pure computation: nothing here touches the disk.

Modes
  stash       move everything from the shared stash onto mules that already hold that kind of item
  tidy        like stash, and also move items that sit on the "wrong" mule; items already in the right
              place stay exactly where they are (fewest files changed)
  reorganize  pool the stash and all mules and lay everything out again, category by category
  migrate     move the older (Resurrected-era) shared stash into the newer (RotW) one, as the game's own
              character transfer allows; it only goes forward in time
"""

from dataclasses import dataclass, field

from . import catalog as C
from .items import MODE_STORED, PAGE_STASH, PAGE_INVENTORY, PAGE_CUBE
from .rules import Ruleset
from .savefiles import TAB_NORMAL, TAB_STACKABLES, valid_character_name
from .world import mule_status

DEFAULT_GRIDS = {PAGE_STASH: (10, 10), PAGE_INVENTORY: (10, 4), PAGE_CUBE: (3, 4)}
STASH_TAB_GRID = (10, 10)
PAGE_LABEL = {PAGE_STASH: "stash", PAGE_INVENTORY: "inventory", PAGE_CUBE: "cube"}
STACK_MAX = 99
MODES = ("stash", "tidy", "reorganize", "migrate")

NUMBER_WORDS = ["One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
                "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen",
                "Twenty"]

# Words in a mule's current name that hint at what it was meant for (used to pick empty mules).
NAME_HINTS = {
    "craftbait": ("craft", "ring", "amul", "ammy", "ammie"), "jewelry": ("ring", "amul", "ammy", "ammie", "jewel"),
    "charms": ("charm",), "unicharms": ("charm", "torch", "anni"), "junkcharms": ("junk",), "jewels": ("jewel",),
    "runewords": ("runeword", "rw"), "uniques": ("unique", "uni"), "sets": ("set",), "rares": ("rare",),
    "bases": ("base",), "ethbases": ("eth", "base"), "magicgear": ("magic",), "runes": ("rune",), "gems": ("gem",),
    "mats": ("mat", "key", "essence", "organ"),
}


@dataclass
class Options:
    mode: str = "stash"
    stash_file: str = None          # default: the stash most characters can use
    source_stash: str = None         # migrate: the older stash to empty (default: the other one of the same core)
    mule_max_level: int = 1          # characters at or below this level count as mules...
    mule_name_hint: bool = True      # ...and so do characters with "mule" in their name
    mules: list = None               # explicit mule names (overrides the two rules above)
    exclude: list = field(default_factory=list)
    use_stackables: bool = False     # (beta) merge runes/gems/materials into the RotW Stackables tab
    rename: str = "none"             # none | new | all
    keep_in_stash: list = field(default_factory=list)  # categories to leave in the stash
    keep_items: list = field(default_factory=list)     # item keys to leave where they are
    limit: int = 0                   # test run: only this many moves (and this many stack merges)
    create_mules: int = 0            # (experimental) create up to this many new mules when space runs out
    rules: dict = None               # sorting rules (None: defaults)
    crafter_level: int = 99


class Grid:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[False] * w for _ in range(h)]

    def fits(self, x, y, iw, ih):
        if x < 0 or y < 0 or x + iw > self.w or y + ih > self.h:
            return False
        return not any(self.cells[y + dy][x + dx] for dy in range(ih) for dx in range(iw))

    def mark(self, x, y, iw, ih, value=True):
        for dy in range(ih):
            for dx in range(iw):
                if 0 <= y + dy < self.h and 0 <= x + dx < self.w:
                    self.cells[y + dy][x + dx] = value

    def find(self, iw, ih):
        for y in range(self.h - ih + 1):
            for x in range(self.w - iw + 1):
                if self.fits(x, y, iw, ih):
                    return x, y
        return None

    def used(self):
        return sum(row.count(True) for row in self.cells)


@dataclass
class PoolItem:
    item: object
    key: str
    src_file: str
    src_where: str
    category: str
    name: str
    origin: object = None  # the _Container it currently sits in, if any


@dataclass
class Move:
    key: str
    name: str
    category: str
    src_file: str
    src_where: str
    dst_file: str
    dst_char: str        # character name, or "" when the destination is a stash tab
    page: int
    x: int
    y: int
    dst_tab: int = None  # stash tab index when moving into a stash
    item: object = field(repr=False, default=None)

    @property
    def dst_where(self):
        if self.dst_tab is not None:
            return f"{self.dst_file} tab {self.dst_tab + 1} ({self.x},{self.y})"
        return f"{self.dst_char} {PAGE_LABEL[self.page]} ({self.x},{self.y})"


@dataclass
class StackMerge:
    key: str
    name: str
    code: str
    src_file: str
    src_where: str
    stash_file: str
    item: object = field(repr=False, default=None)
    stack: object = field(repr=False, default=None)


@dataclass
class Rename:
    old: str
    new: str
    category: str
    file: str = ""  # the character's current file name (names alone can be ambiguous)


@dataclass
class NewMule:
    name: str
    template: str  # file name of the character it is cloned from (without items)
    category: str


@dataclass
class MulePlan:
    name: str
    file: str
    category: str
    claimed: bool
    items_before: int
    items_after: int
    cells_used: int
    cells_total: int
    new: bool = False


@dataclass
class Plan:
    options: Options
    stash_file: str
    labels: dict = field(default_factory=dict)
    moves: list = field(default_factory=list)
    merges: list = field(default_factory=list)
    renames: list = field(default_factory=list)
    new_mules: list = field(default_factory=list)
    unplaced: list = field(default_factory=list)  # PoolItem (stash items with no room)
    left_in_place: int = 0                        # tidy: misplaced mule items with nowhere better to go
    mules: list = field(default_factory=list)     # MulePlan
    skipped_chars: list = field(default_factory=list)  # (name, reason)
    stack_counts: dict = field(default_factory=dict)   # code -> (before, after)
    notes: list = field(default_factory=list)

    @property
    def is_empty(self):
        return not (self.moves or self.merges or self.renames or self.new_mules)


class _Container:
    """A mule character (or, when migrating, a stash tab) with free-space bookkeeping."""

    def __init__(self, gd, name, file, grids, ch=None, tab=None, new=False):
        self.gd, self.name, self.file, self.ch, self.tab, self.new = gd, name, file, ch, tab, new
        self.grids = grids
        self.pages = list(grids)
        self.carry = {}  # carry-one group -> count
        self.contents = []  # categories of items that end up stored here
        self.original = []  # categories it held before planning
        self.category = None
        self.claimed = False
        self.claim_order = 0
        self.items_before = 0

    @classmethod
    def for_character(cls, gd, ch, new=False):
        has_cube = any(i.code == "box" for i in ch.items)
        pages = [p for p in (PAGE_STASH, PAGE_INVENTORY, PAGE_CUBE) if p != PAGE_CUBE or has_cube]
        return cls(gd, ch.name, ch.path.name, {p: Grid(*DEFAULT_GRIDS[p]) for p in pages}, ch=ch, new=new)

    def occupy(self, it, page, x, y, value=True):
        w, h = C.item_size(it, self.gd)
        if page in self.grids:
            self.grids[page].mark(x, y, w, h, value)

    def add_carry(self, it, n=1):
        g = C.carry_one_group(it, self.gd)
        if g:
            self.carry[g] = self.carry.get(g, 0) + n

    def place(self, it, category):
        group = C.carry_one_group(it, self.gd)
        if group and self.carry.get(group):
            return None
        w, h = C.item_size(it, self.gd)
        for page in self.pages:
            spot = self.grids[page].find(w, h)
            if spot:
                self.grids[page].mark(spot[0], spot[1], w, h)
                self.add_carry(it)
                self.contents.append(category)
                return page, spot[0], spot[1]
        return None

    def cells(self):
        return sum(g.used() for g in self.grids.values()), sum(g.w * g.h for g in self.grids.values())

    def dominant(self, majority=True):
        if not self.contents:
            return None
        best = max(set(self.contents), key=lambda c: (self.contents.count(c), c))
        if majority and self.contents.count(best) * 2 < len(self.contents):
            return None
        return best

    def original_dominant(self):
        if not self.original:
            return None
        best = max(set(self.original), key=lambda c: (self.original.count(c), c))
        return best if self.original.count(best) * 2 >= len(self.original) else None


def item_key(file_name, container, index):
    return f"{file_name}|{container}|{index}"


def is_mule_candidate(ch, opts):
    return ch.level <= opts.mule_max_level or (opts.mule_name_hint and "mule" in ch.name.lower())


def _hint_score(name, category):
    n = name.lower()
    return sum(1 for h in NAME_HINTS.get(category, ()) if h in n)


class _Planner:
    def __init__(self, world, opts):
        if opts.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.world, self.opts = world, opts
        self.gd, self.names = world.gd, world.names
        self.rules = Ruleset(self.gd, self.names, opts.rules, opts.crafter_level)
        self.stash = world.stash(opts.stash_file) if opts.stash_file else world.default_stash()
        if self.stash is None:
            raise ValueError("No shared stash found")
        self.plan = Plan(options=opts, stash_file=self.stash.path.name, labels=self.rules.labels())
        self.keep_items = set(opts.keep_items)
        self.containers = []
        self.pool = []
        self.claims = 0
        self.origin_counts = {}

    # ---------- inputs
    def _pool_add(self, it, key, src_file, src_where, origin=None):
        cat = self.rules.categorize(it)
        p = PoolItem(it, key, src_file, src_where, cat, C.display_name(it, self.gd, self.names), origin)
        self.pool.append(p)
        if origin is not None:
            self.origin_counts.setdefault(cat, {}).setdefault(origin.name, 0)
            self.origin_counts[cat][origin.name] += 1
        return p

    def _pool_stash(self, stash):
        for ti, tab in enumerate(stash.tabs):
            if tab.type != TAB_NORMAL:
                continue
            for idx, it in enumerate(tab.items):
                key = item_key(stash.path.name, f"tab{ti}", idx)
                if key in self.keep_items or self.rules.categorize(it) in self.opts.keep_in_stash:
                    continue
                label = "stash" if stash is self.stash else stash.path.stem
                self._pool_add(it, key, stash.path.name, f"{label} tab {ti + 1} ({it.x},{it.y})")

    def select_mules(self):
        opts = self.opts
        excluded = {n.lower() for n in opts.exclude}
        explicit = {n.lower() for n in opts.mules} if opts.mules else None
        for ch in self.world.characters:
            if ch.name.lower() in excluded:
                self.plan.skipped_chars.append((ch.name, "excluded"))
                continue
            ok, reason = mule_status(ch, self.stash, 99)
            if ok and explicit is not None and ch.name.lower() not in explicit:
                ok, reason = False, "not selected"
            if ok and explicit is None and not is_mule_candidate(ch, opts):
                ok, reason = False, f"level {ch.level} (not a mule)"
            if ok:
                self.containers.append(_Container.for_character(self.gd, ch))
            else:
                self.plan.skipped_chars.append((ch.name, reason))

    def load_mule_contents(self, pool_mode):
        """pool_mode: None keeps every mule item in place; 'all' pools every stored item for re-sorting."""
        for m in self.containers:
            for idx, it in enumerate(m.ch.items):
                stored_on_grid = it.mode == MODE_STORED and it.page in m.grids
                key = item_key(m.file, "items", idx)
                cat = self.rules.categorize(it)
                if stored_on_grid:
                    m.items_before += 1
                    m.original.append(cat)
                if pool_mode == "all" and stored_on_grid and it.code != "box" and key not in self.keep_items:
                    self._pool_add(it, key, m.file, f"{m.name} {PAGE_LABEL[it.page]} ({it.x},{it.y})", origin=m)
                    continue
                m.add_carry(it)
                if stored_on_grid:
                    m.occupy(it, it.page, it.x, it.y)
                    m.contents.append(cat)

    # ---------- stacking into the Stackables tab
    def plan_stacking(self):
        if not self.opts.use_stackables:
            return
        stacks = {}
        for tab in self.stash.tabs:
            if tab.type == TAB_STACKABLES:
                for it in tab.items:
                    if it.stack_count is not None and it.code not in stacks:
                        stacks[it.code] = it
        planned = {code: st.stack_count for code, st in stacks.items()}
        rest = []
        for p in self.pool:
            st = stacks.get(p.item.code)
            # loose items carry either no stack field or flag=1/count=0; both mean "one item, not a stack"
            if (st is not None and self.rules.stackable(p.category) and not p.item.stack_count
                    and not p.item.children and planned[p.item.code] < STACK_MAX):
                planned[p.item.code] += 1
                self.plan.merges.append(StackMerge(p.key, p.name, p.item.code, p.src_file, p.src_where,
                                                   self.stash.path.name, item=p.item, stack=st))
            else:
                rest.append(p)
        self.plan.stack_counts = {c: (stacks[c].stack_count, n) for c, n in planned.items()
                                  if n != stacks[c].stack_count}
        self.pool = rest

    # ---------- claiming free mules (existing, or new clones)
    def _templates(self):
        version, era, hc = self.stash.tabs[0].version, self.stash.era, self.stash.hardcore
        return sorted((c for c in self.world.characters if c.can_be_template and c.version == version
                       and c.era == era and c.hardcore == hc), key=lambda c: c.name.lower())

    def claim(self, cat):
        free = [m for m in self.containers if m.category is None and not m.contents]
        if not free and len(self.plan.new_mules) < self.opts.create_mules and self.opts.mode != "migrate":
            m = self._new_mule(cat)
            if m:
                free = [m]
        if not free:
            return None
        oc = self.origin_counts.get(cat, {})

        def pref(m):
            home = m.original_dominant()
            return (m.new,                                 # use existing characters before new ones
                    -oc.get(m.name, 0),                    # already holds most of this category
                    0 if home in (None, cat) else 1,       # don't steal another category's natural home
                    -_hint_score(m.name, cat),             # name suggests this category
                    len(m.original),                       # emptier mules mean fewer moves
                    m.name.lower())
        m = min(free, key=pref)
        self.claims += 1
        m.category, m.claimed, m.claim_order = cat, True, self.claims
        return m

    def _new_mule(self, cat):
        templates = self._templates()
        if not templates:
            note = ("Can't create new mules: there is no empty character (no items needed, but no gold, "
                    "mercenary or corpse) to copy from.")
            if note not in self.plan.notes:
                self.plan.notes.append(note)
            return None
        template = templates[0]
        name = self._free_name(self.rules.stem(cat))
        self.plan.new_mules.append(NewMule(name, template.path.name, cat))
        m = _Container(self.gd, name, name + ".d2s",
                       {p: Grid(*DEFAULT_GRIDS[p]) for p in (PAGE_STASH, PAGE_INVENTORY)}, new=True)
        self.containers.append(m)
        return m

    # ---------- placing
    def _process_order(self):
        """Category order, with categories that share another's mules handled right after it."""
        order = list(self.rules.order)
        for cat in list(order):
            shares = [o for o in self.rules.share_with(cat) if o in order]
            if shares:
                last = max(order.index(o) for o in shares)
                if last > order.index(cat):
                    order.remove(cat)
                    order.insert(last, cat)
        return order

    def place_all(self, items_by_cat, tidy=False):
        """Place pool items category by category. Returns the items that found no spot."""
        rules = self.rules
        left = []
        for cat in self._process_order():
            items = sorted(items_by_cat.get(cat, []), key=lambda p: rules.sort_key(p.item, cat))
            for p in items:
                spot, dest = None, None
                targets = [m for m in self.containers if m.category == cat and m is not p.origin]
                targets += [m for fb in rules.share_with(cat) for m in self.containers
                            if m.category == fb and m is not p.origin]
                for m in targets:
                    spot = m.place(p.item, cat)
                    if spot:
                        dest = m
                        break
                while spot is None:
                    m = self.claim(cat)
                    if m is None:
                        break
                    spot = m.place(p.item, cat)
                    dest = m if spot else None
                if spot is None:
                    left.append(p)
                    continue
                if p.origin is not None and tidy:
                    p.origin.occupy(p.item, p.item.page, p.item.x, p.item.y, False)
                    p.origin.contents.remove(p.category)
                    p.origin.add_carry(p.item, -1)
                self._record_move(p, dest, spot)
        return left

    def _record_move(self, p, dest, spot):
        page, x, y = spot
        if p.src_file == dest.file and dest.tab is None and (p.item.page, p.item.x, p.item.y) == (page, x, y):
            return  # already exactly there
        if dest.tab is not None:
            self.plan.moves.append(Move(p.key, p.name, p.category, p.src_file, p.src_where, dest.file, "",
                                        PAGE_STASH, x, y, dst_tab=dest.tab, item=p.item))
        else:
            self.plan.moves.append(Move(p.key, p.name, p.category, p.src_file, p.src_where, dest.file, dest.name,
                                        page, x, y, item=p.item))

    # ---------- modes
    def run(self):
        mode = self.opts.mode
        if mode == "migrate":
            return self._migrate()
        self.select_mules()
        self._pool_stash(self.stash)
        self.load_mule_contents("all" if mode == "reorganize" else None)
        self.plan_stacking()
        by_cat = {}
        for p in self.pool:
            by_cat.setdefault(p.category, []).append(p)
        if mode == "stash":
            for m in self.containers:
                m.category = m.dominant()
            self.plan.unplaced = self.place_all(by_cat)
        elif mode == "reorganize":
            self.plan.unplaced = self.place_all(by_cat)
        else:
            self._tidy(by_cat)
        self._limit()
        self._renames()
        self._summaries()
        return self.plan

    def _tidy(self, stash_by_cat):
        # each mule keeps the job it mostly does (plurality); items that don't fit that job are candidates to move
        for m in self.containers:
            m.category = m.dominant(majority=False)
        misplaced = []
        for m in self.containers:
            if m.category is None:
                continue
            for idx, it in enumerate(m.ch.items):
                if it.mode != MODE_STORED or it.page not in m.grids or it.code == "box":
                    continue
                key = item_key(m.file, "items", idx)
                cat = self.rules.categorize(it)
                if cat != m.category and key not in self.keep_items:
                    misplaced.append(PoolItem(it, key, m.file, f"{m.name} {PAGE_LABEL[it.page]} ({it.x},{it.y})",
                                              cat, C.display_name(it, self.gd, self.names), origin=m))
        by_cat = {k: list(v) for k, v in stash_by_cat.items()}
        for p in misplaced:
            by_cat.setdefault(p.category, []).append(p)
        left = self.place_all(by_cat, tidy=True)
        while left:  # moving items out can empty a mule or free space; retry until nothing changes
            for m in self.containers:
                if not m.contents and not m.new:
                    m.category = None
            regroup = {}
            for p in left:
                regroup.setdefault(p.category, []).append(p)
            before = len(left)
            left = self.place_all(regroup, tidy=True)
            if len(left) == before:
                break
        self.plan.unplaced = [p for p in left if p.origin is None]
        self.plan.left_in_place = sum(1 for p in left if p.origin is not None)

    def _migrate(self):
        target = self.stash
        sources = [s for s in self.world.stashes if s is not target and s.hardcore == target.hardcore]
        if self.opts.source_stash:
            sources = [s for s in sources if s.path.name.lower() == self.opts.source_stash.lower()]
        source = max(sources, key=lambda s: sum(len(t.items) for t in s.normal_tabs()), default=None)
        if source is None:
            self.plan.notes.append("There is no other shared stash to migrate from.")
            return self.plan
        if source.era >= target.era:
            raise ValueError("Items can only move forward in time: pick the RotW stash as the target "
                             "and the Resurrected-era stash as the source.")
        if source.tabs[0].version != target.tabs[0].version:
            raise ValueError("The two stashes use different save versions; open the game once so it upgrades them.")
        self._pool_stash(source)
        for ti, tab in enumerate(target.tabs):
            if tab.type != TAB_NORMAL:
                continue
            c = _Container(self.gd, f"tab {ti + 1}", target.path.name, {PAGE_STASH: Grid(*STASH_TAB_GRID)}, tab=ti)
            for it in tab.items:
                c.occupy(it, PAGE_STASH, it.x, it.y)
            self.containers.append(c)
        order = self.rules.order
        left = []
        for p in sorted(self.pool, key=lambda p: (order.index(p.category), self.rules.sort_key(p.item, p.category))):
            for c in self.containers:
                spot = c.place(p.item, p.category)
                if spot:
                    self._record_move(p, c, spot)
                    break
            else:
                left.append(p)
        self.plan.unplaced = left
        self.plan.notes.append(f"Moving items from {source.path.name} into {target.path.name} (gold stays put).")
        self._limit()
        return self.plan

    # ---------- finishing touches
    def _limit(self):
        n = self.opts.limit
        if not n:
            return
        self.plan.moves = self.plan.moves[:n]
        self.plan.merges = self.plan.merges[:n]
        used = {m.dst_char for m in self.plan.moves}
        self.plan.new_mules = [nm for nm in self.plan.new_mules if nm.name in used]
        self.plan.stack_counts = {}
        for m in self.plan.merges:
            before = m.stack.stack_count
            cur = self.plan.stack_counts.get(m.code, (before, before))
            self.plan.stack_counts[m.code] = (before, cur[1] + 1)
        self.plan.notes.append(f"Test run: limited to {n} item(s).")

    def _candidate_names(self, stem):
        for n in range(len(NUMBER_WORDS) + 26):
            if n < len(NUMBER_WORDS):
                word = NUMBER_WORDS[n]
                yield stem[:15 - len(word)] + word
            else:
                yield f"{stem[:12]}-{chr(ord('A') + n - len(NUMBER_WORDS))}"

    def _free_name(self, stem):
        taken = {c.name.lower() for c in self.world.characters} | {nm.name.lower() for nm in self.plan.new_mules}
        for cand in self._candidate_names(stem):
            if valid_character_name(cand) and cand.lower() not in taken:
                return cand
        raise ValueError(f"no free character name for {stem}")

    def _renames(self):
        opts = self.opts
        if opts.rename not in ("new", "all"):
            return
        order = self.rules.order
        involved = {m.dst_char for m in self.plan.moves} if opts.limit else None
        to_rename = [m for m in self.containers if m.category in order and not m.new
                     and (m.claimed or opts.rename == "all") and (involved is None or m.name in involved)]
        to_rename.sort(key=lambda m: (order.index(m.category), not m.claimed, m.claim_order, m.name.lower()))
        renaming = {m.name.lower() for m in to_rename}
        taken = ({c.name.lower() for c in self.world.characters if c.name.lower() not in renaming}
                 | {nm.name.lower() for nm in self.plan.new_mules})
        counters = {}
        for m in to_rename:
            new = None
            names = counters.setdefault(m.category, self._candidate_names(self.rules.stem(m.category)))
            for cand in names:
                if valid_character_name(cand) and cand.lower() not in taken:
                    new = cand
                    break
            if new is None:
                continue
            taken.add(new.lower())
            if new != m.name:
                self.plan.renames.append(Rename(m.name, new, m.category, m.file))

    def _summaries(self):
        for m in self.containers:
            used, total = m.cells()
            self.plan.mules.append(MulePlan(m.name, m.file, m.category or m.dominant() or "", m.claimed,
                                            m.items_before, len(m.contents), used, total, new=m.new))


def make_plan(world, opts: Options):
    return _Planner(world, opts).run()
