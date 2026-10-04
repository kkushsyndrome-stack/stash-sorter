"""Battle.net launch-option tests on synthetic settings files (the real Battle.net settings are never touched)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import launcher as L  # noqa: E402

CONFIG = {
    "Client": {"Install": {"DefaultInstallPath": "C:\\Program Files (x86)"}, "Language": "enUS"},
    "Games": {
        "wow": {"ServerUid": "wow", "AdditionalLaunchArguments": "-console"},
        "osi": {"ServerUid": "osi", "LastPlayed": "1791095174",
                "AdditionalLaunchArguments": "-enablerespec -direct txt -seed (number goes here no bracket)",
                "AutoUpdate": "false"},
        "diablo3": {"ServerUid": "diablo3"},
    },
}


def config_text(cfg=CONFIG):
    return json.dumps(cfg, indent=4).replace("\n", "\r\n")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-bnet-"))
        self.cfg = self.tmp / "Battle.net.config"
        self.cfg.write_bytes(config_text().encode())

    def test_parse_finds_the_problems(self):
        p = L.parse_args(CONFIG["Games"]["osi"]["AdditionalLaunchArguments"])
        self.assertEqual(p["flags"], ["-enablerespec", "-direct", "-txt"])
        self.assertIsNone(p["seed"])
        self.assertEqual(len(p["problems"]), 2)
        self.assertIn("missing its dash", p["problems"][0])
        ok = L.parse_args("-direct -txt -mod MyMod -seed 42 -foo bar")
        self.assertEqual((ok["seed"], ok["mod"], ok["other"], ok["problems"]), (42, "MyMod", ["-foo", "bar"], []))

    def test_build(self):
        self.assertEqual(L.build_args(["-txt", "-direct"], seed="7", mod="My Mod", other="-x"),
                         '-direct -txt -mod "My Mod" -seed 7 -x')
        with self.assertRaises(L.LaunchError):
            L.build_args(seed="-1")
        with self.assertRaises(L.LaunchError):
            L.build_args(seed="abc")

    def test_save_changes_only_d2r_and_keeps_a_backup(self):
        before = self.cfg.read_bytes()
        backup = L.write_args("-direct -txt -seed 1234567", self.tmp / "backups", path=self.cfg, check_running=False)
        self.assertEqual(backup.read_bytes(), before)
        after = self.cfg.read_bytes()
        changed = [(a, b) for a, b in zip(before.split(b"\r\n"), after.split(b"\r\n")) if a != b]
        self.assertEqual(len(changed), 1)
        self.assertIn(b"-direct -txt -seed 1234567", changed[0][1])
        cfg = json.loads(after)
        self.assertEqual(cfg["Games"]["wow"]["AdditionalLaunchArguments"], "-console")  # other games untouched
        self.assertEqual(L.read_args(self.cfg), "-direct -txt -seed 1234567")

    def test_adds_the_setting_when_missing(self):
        cfg = json.loads(json.dumps(CONFIG))
        del cfg["Games"]["osi"]["AdditionalLaunchArguments"]
        self.cfg.write_bytes(config_text(cfg).encode())
        L.write_args("-direct -txt", self.tmp / "backups", path=self.cfg, check_running=False)
        new = json.loads(self.cfg.read_bytes())
        self.assertEqual(new["Games"]["osi"]["AdditionalLaunchArguments"], "-direct -txt")
        del new["Games"]["osi"]["AdditionalLaunchArguments"]
        self.assertEqual(new, cfg)
        self.assertIn(b'\r\n            "AdditionalLaunchArguments": "-direct -txt"\r\n        }', self.cfg.read_bytes())

    def test_quotes_and_backslashes_survive(self):
        args = r'-mod "C:\My Mods\x" -direct'
        L.write_args(args, self.tmp / "backups", path=self.cfg, check_running=False)
        self.assertEqual(L.read_args(self.cfg), args)

    def test_refuses_while_battlenet_runs(self):
        real = L.battlenet_running
        L.battlenet_running = lambda: True
        try:
            before = self.cfg.read_bytes()
            with self.assertRaises(L.LaunchError):
                L.write_args("-direct", self.tmp / "backups", path=self.cfg)
            self.assertEqual(self.cfg.read_bytes(), before)
        finally:
            L.battlenet_running = real


class SeedBookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-seeds-"))
        self.path = self.tmp / "seeds.json"

    def test_save_edit_use_delete_and_persist(self):
        book = L.SeedBook(self.path)
        a = book.save("123456", "  Pit by the waypoint ", "Pit / Tombs", "Act 1")
        b = book.save(42, "Short cow run", "Cows")
        self.assertEqual((a["seed"], a["name"], a["purpose"], a["notes"]), (123456, "Pit by the waypoint", "Pit / Tombs", "Act 1"))
        self.assertNotEqual(a["id"], b["id"])
        book.save(654321, "Pit, even closer", "Pit / Tombs", "", seed_id=a["id"])
        book.mark_used(b["id"])
        again = L.SeedBook(self.path)  # reloaded from disk
        self.assertEqual([(s["seed"], s["name"]) for s in again.seeds], [(654321, "Pit, even closer"), (42, "Short cow run")])
        self.assertIsNotNone(again.get(b["id"])["last_used"])
        again.delete(a["id"])
        self.assertEqual([s["seed"] for s in L.SeedBook(self.path).seeds], [42])
        with self.assertRaises(L.LaunchError):
            again.get(a["id"])

    def test_rejects_bad_input(self):
        book = L.SeedBook(self.path)
        for seed, name in (("abc", "x"), (-1, "x"), (L.MAX_SEED + 1, "x"), (5, "   "), ("", "x")):
            with self.assertRaises(L.LaunchError, msg=(seed, name)):
                book.save(seed, name)
        self.assertFalse(self.path.exists())
        self.assertEqual(book.save(L.MAX_SEED, "x" * 200, "p" * 100, "n" * 900)["name"], "x" * 80)

    def test_purposes_put_yours_first_without_repeats(self):
        book = L.SeedBook(self.path)
        book.save(1, "a", "Uber Tristram")
        book.save(2, "b", "Cows")
        p = book.purposes()
        self.assertEqual(p[:2], ["Uber Tristram", "Cows"])
        self.assertEqual(len(p), len(set(p)))
        self.assertTrue(set(L.PURPOSE_SUGGESTIONS) <= set(p))

    def test_unreadable_file_is_kept_not_overwritten(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        book = L.SeedBook(self.path)
        self.assertEqual(book.seeds, [])
        book.save(7, "new")
        kept = list(self.tmp.glob("seeds.unreadable-*.json"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(encoding="utf-8"), "{ this is not json")
        self.assertEqual([s["seed"] for s in L.SeedBook(self.path).seeds], [7])


class SeedRunTests(unittest.TestCase):
    """Start with -seed, play, take -seed off, start again: driven by a fake process list and clock."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-seedrun-"))
        self.cfg = self.tmp / "Battle.net.config"
        self.cfg.write_bytes(config_text().encode())
        self.saves = self.tmp / "saves"
        self.saves.mkdir()
        (self.saves / "Sorc.d2s").write_bytes(b"old")
        (self.saves / "Mule.d2s").write_bytes(b"old")
        self.running, self.launches, self.clock = set(), [], [1000.0]

    def run_(self, launch=None):
        return L.SeedRun(self.tmp / "seedrun.json", saves_dir=self.saves, processes=lambda: set(self.running),
                         config=self.cfg, launch=launch or (lambda: self.launches.append(L.read_args(self.cfg))),
                         backup_dir=self.tmp / "backups", now=lambda: self.clock[0])

    def args(self):
        return L.read_args(self.cfg)

    def test_strip_seed(self):
        self.assertEqual(L.strip_seed("-direct -seed 123 -txt"), "-direct -txt")
        self.assertEqual(L.strip_seed("-enablerespec -direct txt -seed (number goes here no bracket)"),
                         "-enablerespec -direct txt")
        self.assertEqual(L.strip_seed('-mod "My Mod" -SEED 5'), '-mod "My Mod"')
        self.assertEqual(L.strip_seed("-seed"), "")
        self.assertEqual(L.strip_seed(""), "")

    def test_full_run(self):
        self.running = {L.BATTLENET_PROCESS}
        run = self.run_()
        self.assertEqual(run.start(4242)["stage"], "wait_closed")
        self.assertIn("(number goes here", self.args())  # nothing touched while Battle.net is open
        self.running = set()
        self.assertEqual(run.tick()["stage"], "starting")
        self.assertEqual(self.launches, ["-enablerespec -direct txt -seed 4242"])  # Battle.net started with the seed
        self.running = {L.BATTLENET_PROCESS, L.GAME_PROCESS}
        self.assertEqual(run.tick()["stage"], "playing")
        (self.saves / "Sorc.d2s").write_bytes(b"new map")
        self.running = {L.BATTLENET_PROCESS}
        st = run.tick()
        self.assertEqual((st["stage"], st["changed"]), ("wait_closed_after", ["Sorc"]))
        self.assertEqual(self.args(), "-enablerespec -direct txt -seed 4242")  # Battle.net would write it back
        self.running = set()
        self.assertEqual(run.tick()["stage"], "done")
        self.assertEqual(self.args(), "-enablerespec -direct txt")
        self.assertEqual(self.launches[1], "-enablerespec -direct txt")  # started again, without the seed
        self.assertEqual(json.loads(self.cfg.read_bytes())["Games"]["wow"]["AdditionalLaunchArguments"], "-console")
        self.assertFalse(run.active)
        run.dismiss()
        self.assertFalse((self.tmp / "seedrun.json").exists())

    def test_carries_on_after_a_restart(self):
        self.run_().start(7)
        self.running = {L.BATTLENET_PROCESS, L.GAME_PROCESS}
        self.run_().tick()
        self.running = set()  # app was closed while D2R and Battle.net were closed too
        again = self.run_()
        self.assertTrue(again.active)
        self.assertEqual(again.tick()["stage"], "done")
        self.assertNotIn("-seed", self.args())
        self.assertEqual(len(self.launches), 2)

    def test_cancel(self):
        self.running = {L.BATTLENET_PROCESS}
        run = self.run_()
        run.start(1)
        self.assertEqual(run.cancel()["stage"], "cancelled")
        self.assertIn("(number goes here", self.args())  # untouched
        self.running = set()
        run = self.run_()
        run.dismiss()
        run.start(2)
        self.running = {L.BATTLENET_PROCESS, L.GAME_PROCESS}
        run.tick()
        self.assertEqual(run.cancel()["stage"], "wait_closed_after")  # can't change anything while it's open
        self.running = set()
        self.assertEqual(run.tick()["stage"], "cancelled")
        self.assertEqual(self.args(), "-enablerespec -direct txt")
        self.assertEqual(len(self.launches), 1)  # not started again

    def test_start_again_when_no_game_was_entered(self):
        run = self.run_()
        run.start(3)
        self.running = {L.BATTLENET_PROCESS, L.GAME_PROCESS}
        run.tick()
        self.running = {L.BATTLENET_PROCESS}
        self.assertEqual(run.tick()["changed"], [])
        self.assertEqual(run.again()["stage"], "starting")
        self.assertEqual(self.launches[-1], "-enablerespec -direct txt -seed 3")
        with self.assertRaises(L.LaunchError):
            run.again()

    def test_battlenet_closed_before_d2r_started(self):
        run = self.run_()
        run.start(9)
        self.clock[0] += L.GIVE_UP_STARTING + 1
        st = run.tick()
        self.assertEqual(st["stage"], "cancelled")
        self.assertNotIn("-seed", self.args())

    def test_launch_failure_takes_the_seed_off(self):
        def fail():
            raise L.LaunchError("Battle.net isn't installed in the usual place.")
        run = self.run_(launch=fail)
        st = run.start(5)
        self.assertEqual(st["stage"], "cancelled")
        self.assertNotIn("-seed", self.args())
        self.assertTrue(any("isn't installed" in n for n in st["notes"]))

    def test_refusals(self):
        run = self.run_()
        for seed in ("abc", -1, L.MAX_SEED + 1):
            with self.assertRaises(L.LaunchError):
                run.start(seed)
        L.write_args("-resetofflinemaps", self.tmp / "backups", path=self.cfg, check_running=False)
        with self.assertRaises(L.LaunchError):
            run.start(1)
        self.assertFalse(run.active)
        L.write_args("-direct -txt", self.tmp / "backups", path=self.cfg, check_running=False)
        self.running = {L.BATTLENET_PROCESS}
        run.start(1)
        with self.assertRaises(L.LaunchError):
            run.start(2)


if __name__ == "__main__":
    unittest.main()
