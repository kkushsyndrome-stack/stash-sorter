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
from stash_sorter.savefiles import parse_character, parse_stash, valid_character_name  # noqa: E402

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

    def test_refuses_backups_inside_saves(self):
        with self.assertRaises(A.ApplyError):
            A.backup_save_dir(self.saves, self.saves / "backups")


if __name__ == "__main__":
    unittest.main()
