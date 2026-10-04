"""Session tracker tests on temporary copies of the bundled sample saves (runs anywhere)."""

import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import gamedata, planner, world as W  # noqa: E402
from stash_sorter import apply as A  # noqa: E402
from stash_sorter.bits import set_bits  # noqa: E402
from stash_sorter.tracker import Tracker, FileState, SETTLE_SECONDS  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hlb"
GD = gamedata.load(bundled_only=True)


def describe(it):
    return {"name": it.code, "quality": "unique" if it.quality == 7 else "other", "category": "", "stats": []}


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-tracker-"))
        self.saves = self.tmp / "saves"
        self.saves.mkdir()
        for p in FIXTURES.glob("*.d2*"):
            if "v99" not in p.name:
                shutil.copy(p, self.saves / p.name)
        self.tracker = Tracker(self.saves, GD, None, self.tmp / "sessions", describe)
        self.tracker.start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _char(self, name):
        w = W.load_world(self.saves, GD)
        return w, next(c for c in w.characters if c.path.name == name)

    def test_nothing_changed_records_nothing(self):
        self.assertIsNone(self.tracker.poll(force=True))

    def test_waits_for_files_to_settle(self):
        w, ch = self._char("ChaosSC.d2s")
        ch.items = ch.items[:-1]
        ch.path.write_bytes(ch.to_bytes())
        self.assertIsNone(self.tracker.poll(now=100.0))           # just changed: not yet
        self.assertIsNone(self.tracker.poll(now=100.5))
        self.assertIsNotNone(self.tracker.poll(now=100.0 + SETTLE_SECONDS + 0.1))

    def test_sold_item_is_reported_as_left(self):
        w, ch = self._char("Roka.d2s")
        gone = next(i for i in ch.items if i.mode == 0 and i.item_id is not None)
        ch.items = [i for i in ch.items if i is not gone]
        ch.path.write_bytes(ch.to_bytes())
        run = self.tracker.poll(force=True)
        self.assertEqual([f["count"] for f in run["left"]], [1])
        self.assertEqual(run["found"], [])

    def test_moving_items_between_characters_is_not_loot(self):
        w = W.load_world(self.saves, GD)
        mules = [c.name for c in w.characters if c.era == 3]
        plan = planner.make_plan(w, planner.Options(mode="reorganize", stash_file="ModernSharedStashSoftCoreV2.d2i",
                                                    mules=mules))
        self.assertTrue(plan.moves)
        A.apply_plan(w, plan, self.tmp / "backups", log=lambda m: None, skip_game_check=True)
        run = self.tracker.poll(force=True)
        self.assertTrue(run is None or (not run["found"] and not run["left"]), run and (run["found"], run["left"]))

    def test_new_item_found_and_grail(self):
        w, ch = self._char("Soska.d2s")
        loot = next(i for i in ch.items if i.quality == 7 and i.unique_id in GD.uniques and i.id_bit is not None)
        self.tracker.known_grail.discard(("u", loot.unique_id))  # pretend it wasn't owned when the session began
        ch.items = [i for i in ch.items if i is not loot]          # ...and that this run dropped a fresh copy
        fresh = A._clone(loot)
        set_bits(fresh.raw, fresh.id_bit, 32, loot.item_id ^ 0x5A5A5A5A)
        ch.items.append(fresh)
        ch.path.write_bytes(ch.to_bytes())
        run = self.tracker.poll(force=True)
        self.assertEqual([f["count"] for f in run["found"]], [1])
        self.assertEqual([f["count"] for f in run["left"]], [1])
        self.assertEqual(run["grail"], [GD.uniques[loot.unique_id][0]])

    def test_xp_level_gold_maths(self):
        t = self.tracker
        old = {Path("a.d2s"): FileState((1, 1), "char", "Hero", level=80, xp=1000, gold=50),
               Path("m.d2s"): FileState((1, 1), "char", "Mule", level=1, xp=0, gold=0, mule=True),
               Path("s.d2i"): FileState((1, 1), "stash", "stash", gold=500)}
        new = {Path("a.d2s"): FileState((2, 2), "char", "Hero", level=81, xp=5000, gold=10),
               Path("m.d2s"): FileState((2, 2), "char", "Mule", level=1, xp=0, gold=0, mule=True),
               Path("s.d2i"): FileState((2, 2), "stash", "stash", gold=600)}
        run = t._diff(old, new)
        self.assertEqual((run["kind"], run["xp"], run["levels"], run["gold"]), ("run", 4000, 1, 60))
        self.assertEqual(run["characters"], ["Hero"])

    def test_stack_counts_net_out(self):
        old = {Path("a.d2s"): FileState((1, 1), "char", "Mule", counts=Counter({("c", "r30"): 1}), mule=True),
               Path("s.d2i"): FileState((1, 1), "stash", "stash", counts=Counter({("c", "r30"): 4}))}
        new = {Path("a.d2s"): FileState((2, 2), "char", "Mule", counts=Counter(), mule=True),
               Path("s.d2i"): FileState((2, 2), "stash", "stash", counts=Counter({("c", "r30"): 5}))}
        run = self.tracker._diff(old, new)
        self.assertTrue(run is None or (not run["found"] and not run["left"]))

    def test_history_end_and_delete(self):
        done = self.tracker.end()
        self.assertTrue(done["ended"])
        hist = self.tracker.history()
        self.assertEqual(len(hist), 1)
        self.tracker.delete(hist[0]["id"])
        self.assertEqual(self.tracker.history(), [])


if __name__ == "__main__":
    unittest.main()
