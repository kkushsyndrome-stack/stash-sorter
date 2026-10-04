"""Tests against your own save files. Your save folder is only ever READ; write tests run on a temporary copy.

    python -m unittest discover -s tests -v

Uses D2R_TEST_SAVES if set, otherwise the auto-detected save folder. Skips if none is found.
"""

import collections
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import gamedata, planner, world as W  # noqa: E402
from stash_sorter.apply import apply_plan, restore_backup, _identity  # noqa: E402
from stash_sorter.bits import BitReader, set_bits, get_bits  # noqa: E402


def _save_dir():
    try:
        return W.find_save_dir(os.environ.get("D2R_TEST_SAVES"))
    except FileNotFoundError:
        return None


SAVES = _save_dir()
GD = gamedata.load()


def _snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in Path(folder).rglob("*") if p.is_file()}


class BitTests(unittest.TestCase):
    def test_roundtrip_bits(self):
        buf = bytearray(8)
        set_bits(buf, 5, 11, 1234)
        set_bits(buf, 35, 3, 6)
        self.assertEqual(get_bits(buf, 5, 11), 1234)
        r = BitReader(bytes(buf), 35)
        self.assertEqual(r.read(3), 6)

    def test_bundle_matches_extracted(self):
        if not gamedata.find_excel_dir():
            self.skipTest("no extracted tables to compare against")
        b = gamedata.load(bundled_only=True)
        self.assertEqual(GD.stats, b.stats)
        self.assertEqual(GD.items, b.items)


@unittest.skipIf(SAVES is None, "no D2R save folder found")
class ReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = _snapshot(SAVES)
        cls.world = W.load_world(SAVES, GD)

    @classmethod
    def tearDownClass(cls):
        assert _snapshot(SAVES) == cls.before, "the real save folder changed during read-only tests!"

    def test_every_file_parses(self):
        self.assertEqual(self.world.errors, [])

    def test_every_file_round_trips(self):
        for c in self.world.characters:
            self.assertEqual(c.to_bytes(), self.world.originals[c.path], c.path.name)
        for s in self.world.stashes:
            self.assertEqual(s.to_bytes(), self.world.originals[s.path], s.path.name)

    def test_plans_are_consistent(self):
        for mode in ("stash", "reorganize"):
            plan = planner.make_plan(self.world, planner.Options(mode=mode, rename="all"))
            names = [r.new.lower() for r in plan.renames]
            self.assertEqual(len(names), len(set(names)), "duplicate rename targets")
            dests = collections.Counter((m.dst_file, m.page, m.x, m.y) for m in plan.moves)
            self.assertTrue(all(n == 1 for n in dests.values()), "two items planned into the same cell")


@unittest.skipIf(SAVES is None, "no D2R save folder found")
class SandboxWriteTests(unittest.TestCase):
    """Applies plans to a temporary COPY of the saves and checks nothing is lost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-test-"))
        self.saves = self.tmp / "saves"
        shutil.copytree(SAVES, self.saves)
        self.backups = self.tmp / "backups"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _census(self, w):
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
        return ids, stacks

    def _apply(self, **opts):
        w = W.load_world(self.saves, GD)
        before, stacks_before = self._census(w)
        plan = planner.make_plan(w, planner.Options(**opts))
        apply_plan(w, plan, self.backups, log=lambda m: None, skip_game_check=True)
        w2 = W.load_world(self.saves, GD)
        self.assertEqual(w2.errors, [])
        after, stacks_after = self._census(w2)
        merged = collections.Counter(_identity(m.item) for m in plan.merges)
        self.assertEqual(+(before - merged), +after, "items lost or created")
        self.assertEqual(stacks_before + len(plan.merges), stacks_after)
        for r in plan.renames:
            self.assertIsNotNone(w2.character(r.new))
        return plan

    def test_small_tidy_then_restore(self):
        original = _snapshot(self.saves)
        plan = self._apply(mode="tidy", rename="new", limit=5)
        if plan.is_empty:
            self.skipTest("nothing to tidy in these saves")
        backups = sorted(self.backups.glob("saves-*-before-apply.zip"))
        restore_backup(backups[0], self.saves, self.backups, skip_game_check=True)
        self.assertEqual(_snapshot(self.saves), original)

    def test_reorganize_with_stacking(self):
        self._apply(mode="reorganize", rename="all", use_stackables=True)


if __name__ == "__main__":
    unittest.main()
