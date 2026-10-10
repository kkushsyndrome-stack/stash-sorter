"""Tests that run anywhere: bundled sample saves + the bundled game data snapshot (no game install needed).

Sample saves in tests/fixtures/hlb come from Horadric Loot Box (MIT, see the LICENSE file there).

    python -m unittest discover -s tests -v
"""

import collections
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import gamedata, planner, world as W  # noqa: E402
from stash_sorter import apply as A  # noqa: E402
from stash_sorter.apply import _identity  # noqa: E402
from stash_sorter.bits import BitReader, set_bits, get_bits  # noqa: E402
from stash_sorter.describe import Describer, fmt  # noqa: E402
from stash_sorter.rules import Ruleset, RulesError, load_default_rules, validate  # noqa: E402
from stash_sorter.savefiles import parse_character, parse_stash, valid_character_name, empty_status  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hlb"
GD = gamedata.load(bundled_only=True)


def census(folder):
    w = W.load_world(folder, GD)
    ids, stacks = collections.Counter(), 0
    for c in w.characters:
        ids.update(_identity(i) for i in c.items)
    for s in w.stashes:
        for t in s.tabs:
            for i in t.items:
                if t.type == 1:
                    stacks += i.stack_count or 0
                else:
                    ids[_identity(i)] += 1
    return w, ids, stacks


class Basics(unittest.TestCase):
    def test_bits(self):
        buf = bytearray(8)
        set_bits(buf, 5, 11, 1234)
        self.assertEqual(get_bits(buf, 5, 11), 1234)
        self.assertEqual(BitReader(bytes(buf), 5).read(11), 1234)

    def test_fmt(self):
        self.assertEqual(fmt("%+d to Strength", 5), "+5 to Strength")
        self.assertEqual(fmt("%d%% Faster Cast Rate", 20), "20% Faster Cast Rate")
        self.assertEqual(fmt("%0%% Reanimate as: %1", 10, "Zombie"), "10% Reanimate as: Zombie")

    def test_names(self):
        for good in ("Mule", "CharmsOne", "Bases-A", "Rune_Keeper"):
            self.assertTrue(valid_character_name(good), good)
        for bad in ("M", "Mule1", "TooLongNameForDiablo", "a-b-c", "-Mule"):
            self.assertFalse(valid_character_name(bad), bad)

    def test_rules(self):
        validate(load_default_rules())
        bad = load_default_rules()
        bad["categories"][0]["match"] = {"nonsense": 1}
        with self.assertRaises(RulesError):
            validate(bad)


class Fixtures(unittest.TestCase):
    def test_every_fixture_round_trips(self):
        files = sorted(FIXTURES.glob("*.d2*"))
        self.assertGreater(len(files), 10)
        for p in files:
            data = p.read_bytes()
            o = parse_stash(p, GD, data) if p.suffix == ".d2i" else parse_character(p, GD, data)
            self.assertEqual(o.to_bytes(), data, p.name)

    def test_every_item_describes_and_categorizes(self):
        d = Describer(GD)
        for p in FIXTURES.glob("*.d2s"):
            ch = parse_character(p, GD)
            rs = Ruleset(GD, W.load_world(FIXTURES, GD).names)
            for it in ch.items:
                d.lines(it.stats + it.runeword_stats)
                self.assertIn(rs.categorize(it), rs.order)


class Sandbox(unittest.TestCase):
    """Every write path, on a temporary copy of the v105 fixtures."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-fixtures-"))
        self.saves = self.tmp / "saves"
        self.saves.mkdir()
        for p in FIXTURES.glob("*.d2*"):
            if "v99" not in p.name:
                shutil.copy(p, self.saves / p.name)
        self.backups = self.tmp / "backups"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _apply(self, **opts):
        w, before, stacks0 = census(self.saves)
        mules = [c.name for c in w.characters if c.era == w.stash("ModernSharedStashSoftCoreV2.d2i").era]
        opts.setdefault("mules", mules)
        plan = planner.make_plan(w, planner.Options(stash_file="ModernSharedStashSoftCoreV2.d2i", **opts))
        res = A.apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        w2, after, stacks1 = census(self.saves)
        merged = collections.Counter(_identity(m.item) for m in plan.merges)
        self.assertEqual(+(before - merged), +after, "items lost or created")
        self.assertEqual(stacks0 + len(plan.merges), stacks1)
        for c in w2.characters + w2.stashes:
            self.assertEqual(c.to_bytes(), w2.originals[c.path])
        return plan, res

    def _snapshot(self):
        return {p.name: p.read_bytes() for p in self.saves.iterdir() if p.is_file()}

    def test_stash_mode_accounts_for_everything(self):
        w = W.load_world(self.saves, GD)
        n = sum(len(t.items) for t in w.stash("ModernSharedStashSoftCoreV2.d2i").normal_tabs())
        plan, _ = self._apply(mode="stash", rename="new")
        self.assertEqual(len(plan.moves) + len(plan.unplaced), n)

    def test_reorganize_then_undo(self):
        before = self._snapshot()
        plan, res = self._apply(mode="reorganize", rename="all")
        self.assertTrue(plan.moves)
        ok, why = A.undo_status(res["log"], self.saves)
        self.assertTrue(ok, why)
        A.undo_apply(res["log"], self.saves, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(self._snapshot(), before)

    def test_reorganize_rename_all(self):
        plan, _ = self._apply(mode="reorganize", rename="all")
        w = W.load_world(self.saves, GD)
        targets = {r.new.lower() + ".d2s" for r in plan.renames}
        for r in plan.renames:
            self.assertTrue((self.saves / (r.new + ".d2s")).is_file(), r.new)
            if r.file.lower() not in targets:
                self.assertFalse((self.saves / r.file).exists(), r.file)
        self.assertEqual(len(w.characters), len(list(self.saves.glob("*.d2s"))))

    def test_tidy_and_limit(self):
        plan, _ = self._apply(mode="tidy", limit=1)
        self.assertLessEqual(len(plan.moves), 1)

    def test_migrate(self):
        plan, _ = self._apply(mode="migrate")
        w = W.load_world(self.saves, GD)
        self.assertEqual(sum(len(t.items) for t in w.stash("SharedStashSoftCoreV2.d2i").normal_tabs()),
                         len(plan.unplaced))

    def test_crash_recovery(self):
        w, before, _ = census(self.saves)
        plan = planner.make_plan(w, planner.Options(mode="reorganize", rename="all",
                                                    stash_file="ModernSharedStashSoftCoreV2.d2i",
                                                    mules=[c.name for c in w.characters]))
        change = A._Change(w, plan)
        change.build()
        writes, renames, deletes = change.files()
        backup = A.backup_save_dir(self.saves, self.backups)
        real, calls = A.os.replace, [0]

        def flaky(a, b):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("simulated crash")
            return real(a, b)
        A.os.replace = flaky
        try:
            with self.assertRaises(OSError):
                A._commit(self.saves, self.backups, backup, writes, renames, deletes)
        finally:
            A.os.replace = real
        self.assertTrue(A.pending_journal(self.backups))
        A.recover(self.backups, log=lambda m: None)
        _, after, _ = census(self.saves)
        self.assertEqual(before, after)
        self.assertFalse(list(self.saves.glob("*.stash-sorter.new")))

    def _mules(self):
        w = W.load_world(self.saves, GD)
        return w, [c.name for c in w.characters if c.era == w.stash("ModernSharedStashSoftCoreV2.d2i").era]

    def test_reorganize_uses_one_mule_for_small_categories(self):
        w, mules = self._mules()
        pl = planner._Planner(w, planner.Options(mode="reorganize", stash_file="ModernSharedStashSoftCoreV2.d2i",
                                                 mules=mules))
        pl.run()
        by = collections.defaultdict(list)
        for m in pl.containers:
            if m.held and m.category:  # mules holding only a Horadric Cube (never moved) have no category
                by[m.category].append(m)
        for cat, ms in by.items():
            if sum(m.cells()[0] for m in ms) <= 70 and cat != "unicharms":
                self.assertEqual(len(ms), 1, f"{cat} spread over {len(ms)} mules")

    def test_tidy_compacts_and_reports_emptied_mules(self):
        plan, _ = self._apply(mode="tidy", compact=True)
        w = W.load_world(self.saves, GD)
        for file in plan.emptied_files:
            ch = next(c for c in w.characters if c.path.name == file)
            self.assertFalse([i for i in ch.items if i.mode == 0 and i.page in (1, 4, 5)], file)

    def _empty_mule(self, name):
        """Write an empty copy of a fixture character (like a freshly made level-1 mule)."""
        w = W.load_world(self.saves, GD)
        template = next((c for c in w.characters if c.can_be_template and c.era == 3), None)
        if template is None:
            self.skipTest("no fixture can serve as an empty mule")
        c = A._with_name(template, name)
        c.items = []
        (self.saves / f"{name}.d2s").write_bytes(c.to_bytes())
        for p in self.saves.glob(template.path.stem + ".*"):
            if p.suffix != ".d2s":
                (self.saves / f"{name}{p.suffix}").write_bytes(p.read_bytes())

    def test_kept_item_stays_kept_after_other_items_leave(self):
        # 0.7.0 named items by list position, so after a test run the flag pointed at nothing (or another item)
        w = W.load_world(self.saves, GD)
        st = w.stash("SharedStashSoftCoreV2.d2i")
        mules = [c.name for c in w.characters if c.era == st.era and c.version == st.tabs[0].version]
        base = dict(mode="stash", stash_file=st.path.name, mules=mules)
        movable = {m.key for m in planner.make_plan(w, planner.Options(**base)).moves}
        kept = [planner.item_key(st.path.name, "tab0", it) for it in st.tabs[0].items]
        kept = [k for k in kept if k in movable][-1]  # the last one, so earlier ones leaving would shift it
        plan = planner.make_plan(w, planner.Options(**base, keep_items=[kept], limit=5))
        self.assertTrue(plan.moves)
        A.apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        w2 = W.load_world(self.saves, GD)
        it, owner, _, _ = planner.find_item(w2, kept)
        self.assertEqual(owner.path.name, st.path.name)
        again = planner.make_plan(w2, planner.Options(**base, keep_items=[kept]))
        self.assertTrue(again.moves)
        self.assertFalse(any(m.key == kept for m in again.moves))

    def test_mule_cubes_are_left_alone(self):
        w = W.load_world(self.saves, GD)
        in_cube = {c.path.name: sorted(_identity(i) for i in c.items if i.mode == 0 and i.page == 4) for c in w.characters}
        self.assertTrue(in_cube["demo_Amazon.d2s"] and in_cube["demo_Warlock.d2s"])  # items in two mules' cubes
        plan, _ = self._apply(mode="reorganize", mules=["barbrotw", "ChaosSC", "Amazon", "Druid", "Warlock"])
        self.assertTrue(plan.moves)
        self.assertFalse([m for m in plan.moves if m.page == 4 or " cube " in m.src_where])
        w2 = W.load_world(self.saves, GD)
        for c in w2.characters:
            self.assertEqual(sorted(_identity(i) for i in c.items if i.mode == 0 and i.page == 4), in_cube[c.path.name],
                             c.path.name)
        self.assertNotIn("Warlock", plan.emptied)  # still has items in its cube

    def test_restyle_changes_only_the_picture_then_undo(self):
        before = self._snapshot()
        w = W.load_world(self.saves, GD)
        picks = [(c, i, it) for c in w.characters for i, it in enumerate(c.items) if planner.skinnable(it, GD)]
        self.assertGreaterEqual(len(picks), 2, "the fixtures need two rings, amulets, charms or jewels with a picture")
        chosen = [picks[0], picks[-1]]
        want = [(it.gfx + 1) % GD.items[it.code].pictures for _, _, it in chosen]
        keys = [planner.item_key(c.path.name, "items", it) for c, _, it in chosen]
        fingerprint = lambda x: (x.stats, x.prefixes, x.suffixes, x.ilvl, x.quality, x.x, x.y, x.page, x.code)
        plan = planner.plan_restyles(w, list(zip(keys, want)))
        self.assertEqual(len(plan.restyles), 2)
        res = A.apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(len(res["restyled"]), 2)
        w2 = W.load_world(self.saves, GD)
        for (c, i, it), gfx in zip(chosen, want):
            got = next(x for x in w2.characters if x.path.name == c.path.name).items[i]
            self.assertEqual(got.gfx, gfx)
            self.assertEqual(fingerprint(got), fingerprint(it))  # stats, affixes, level, place: all untouched
            self.assertEqual(len(got.raw), len(it.raw))
            changed = [n for n, (a, b) in enumerate(zip(got.raw, it.raw)) if a != b]
            self.assertTrue(changed and all(it.gfx_bit // 8 <= n <= (it.gfx_bit + 2) // 8 for n in changed), changed)
            with self.assertRaises(ValueError):  # it already shows that picture now
                planner.plan_restyles(w2, [(planner.item_key(c.path.name, "items", got), gfx)])
        A.undo_apply(res["log"], self.saves, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(self._snapshot(), before)

    def test_restyle_refuses_what_has_no_picture_choice(self):
        w = W.load_world(self.saves, GD)
        for ch in w.characters:
            for it in ch.items:
                if it.mode == 0 and (not planner.skinnable(it, GD)):
                    key = planner.item_key(ch.path.name, "items", it)
                    with self.assertRaises(ValueError):
                        planner.plan_restyles(w, [(key, 0)])
                    return
        self.fail("no unskinnable item in the fixtures")

    def test_restyle_range_is_checked(self):
        w = W.load_world(self.saves, GD)
        ch, it = next((c, i) for c in w.characters for i in c.items if planner.skinnable(i, GD))
        with self.assertRaises(ValueError):
            planner.plan_restyles(w, [(planner.item_key(ch.path.name, "items", it), GD.items[it.code].pictures)])

    def test_personal_stash_goes_to_mules(self):
        w = W.load_world(self.saves, GD)
        sorc = w.character("Sorceress")
        stash_codes = [i.code for i in sorc.items if i.mode == 0 and i.page == 5]
        plan, _ = self._apply(mode="tidy", mules=["barbrotw", "ChaosSC", "Amazon", "Druid", "Warlock"],
                              from_chars=["Sorceress", "Roka"])
        from_sorc = [m for m in plan.moves if m.src_file == sorc.path.name]
        self.assertTrue(from_sorc)
        self.assertTrue(all(m.dst_file != sorc.path.name for m in plan.moves))
        self.assertTrue(any("Roka's stash stays put" in n for n in plan.notes))  # Resurrected-era character
        left = [i.code for i in W.load_world(self.saves, GD).character("Sorceress").items if i.mode == 0 and i.page == 5]
        self.assertIn("mss", left)  # quest items stay with their character
        self.assertEqual(len(left), len(stash_codes) - len(from_sorc))

    def test_rename_character_then_undo(self):
        before = self._snapshot()
        w = W.load_world(self.saves, GD)
        for bad in ("R", "Roka2", "a-b-c", "Soska", "ROKA"):
            with self.assertRaises(ValueError, msg=bad):
                planner.plan_character_rename(w, "Roka", bad)
        plan = planner.plan_character_rename(w, "Roka", "Rokanew")
        res = A.apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertFalse((self.saves / "Roka.d2s").exists())
        w2 = W.load_world(self.saves, GD)
        renamed = w2.character("Rokanew")
        self.assertEqual(renamed.path.name, "Rokanew.d2s")
        self.assertEqual(renamed.level, 99)
        self.assertEqual([_identity(i) for i in renamed.items], [_identity(i) for i in w.character("Roka").items])
        A.undo_apply(res["log"], self.saves, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(self._snapshot(), before)

    def test_delete_empty_mule_then_undo(self):
        self._empty_mule("SpareMule")
        before = self._snapshot()
        w = W.load_world(self.saves, GD)
        self.assertTrue(empty_status(w.character("SpareMule"))[0])
        busy = next(c for c in w.characters if c.items and c.level > 1)
        with self.assertRaises(ValueError):
            planner.plan_mule_deletions(w, [busy.name], max_level=99)
        plan = planner.plan_mule_deletions(w, ["SpareMule"])
        res = A.apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertFalse(list(self.saves.glob("SpareMule.*")))
        A.undo_apply(res["log"], self.saves, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(self._snapshot(), before)

    def test_duplicates_and_delete_one_copy(self):
        from stash_sorter.service import Session
        w = W.load_world(self.saves, GD)
        src = max((c for c in w.characters if c.era == 3),
                  key=lambda c: sum(1 for i in c.items if i.quality in (5, 7) and i.mode == 0))
        twin = A._with_name(src, "TwinMule")  # same items twice -> every unique/set on it is a duplicate
        (self.saves / "TwinMule.d2s").write_bytes(twin.to_bytes())
        s = Session(saves=self.saves, backups=self.backups)
        s.gd = GD
        s.reload()
        groups = s.duplicates()
        if not groups:
            self.skipTest("fixture has no stored unique or set items")
        copy_ = next(c for g in groups for c in g["copies"] if c["deletable"])
        _, before, _ = census(self.saves)
        before_files = self._snapshot()
        p = s.plan_delete_items([copy_["key"]])
        self.assertEqual(len(p["deletions"]), 1)
        res = A.apply_plan(s.world, s.plan, self.backups, log=lambda m: None, skip_game_check=True)
        _, after, _ = census(self.saves)
        self.assertEqual(sum(before.values()) - 1, sum(after.values()))
        A.undo_apply(res["log"], self.saves, self.backups, log=lambda m: None, skip_game_check=True)
        self.assertEqual(self._snapshot(), before_files)

    def test_refuses_backups_inside_saves(self):
        with self.assertRaises(A.ApplyError):
            A.backup_save_dir(self.saves, self.saves / "backups")


if __name__ == "__main__":
    unittest.main()
