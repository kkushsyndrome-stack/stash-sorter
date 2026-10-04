"""Terror zone clock tests. Nothing here changes the PC clock."""

import datetime as dt
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import clock  # noqa: E402

UTC = dt.timezone.utc


def schedule(start, zones):
    return json.dumps([{"datetime": (start + dt.timedelta(minutes=30 * i)).isoformat(),
                        "zone": {"enUS": z}, "immunities": ["c", "l", "l"], "numBossPacks": [15, 20],
                        "superuniques": []} for i, z in enumerate(zones)])


class ClockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="stash-sorter-clock-"))
        self.old_dir = clock.OLD_APP_DIR
        clock.OLD_APP_DIR = self.tmp / "old-app"
        self.now = dt.datetime(2026, 10, 4, 12, 10, tzinfo=UTC)
        self.tc = clock.TerrorClock(cache_dir=self.tmp, settings_file=self.tmp / "tz.json", clock=lambda: self.now)
        start = dt.datetime(2026, 10, 4, 11, 0, tzinfo=UTC)
        (self.tmp / "tz-schedule.json").write_text(schedule(start, ["A", "B", "C", "A", "D"]), encoding="utf-8")
        self.tc.load_schedule(download=False)

    def tearDown(self):
        clock.OLD_APP_DIR = self.old_dir

    def test_ntp_packet(self):
        secs = int((dt.datetime(2026, 1, 1, tzinfo=UTC) - dt.datetime(1900, 1, 1, tzinfo=UTC)).total_seconds())
        packet = bytes(40) + struct.pack("!II", secs, 2 ** 31)
        self.assertEqual(clock.parse_ntp(packet), dt.datetime(2026, 1, 1, 0, 0, 0, 500000, tzinfo=UTC))
        with self.assertRaises(clock.ClockError):
            clock.parse_ntp(bytes(48))

    def test_schedule_and_sessions(self):
        self.assertEqual(self.tc.session_length, dt.timedelta(minutes=30))
        self.assertEqual(self.tc.sessions[0]["immunities"], ["Cold", "Lightning"])  # duplicates dropped
        # 12:10 -> sessions 11:00 A, 11:30 B, 12:00 C (active), 12:30 A, 13:00 D
        s, state = self.tc.zone_session("C")
        self.assertEqual((s["start"].hour, s["start"].minute, state), (12, 0, "active"))
        s, state = self.tc.zone_session("A")
        self.assertEqual((s["start"].hour, s["start"].minute, state), (11, 0, "last"))
        s, state = self.tc.zone_session("D")
        self.assertEqual((s["start"].hour, state), (13, "next"))
        self.assertEqual(self.tc.zone_session("Nowhere"), (None, None))
        self.assertEqual(self.tc.sessions[self.tc.current()]["zone"], "C")
        self.assertEqual([s["zone"] for s in self.tc.upcoming(3)], ["C", "A", "D"])

    def test_real_time_uses_offset(self):
        self.tc.offset = dt.timedelta(hours=-2)  # PC clock is 2 hours ahead of the real time
        self.assertTrue(self.tc.shifted)
        self.assertEqual(self.tc.real_utc(), self.now - dt.timedelta(hours=2))
        self.tc.offset = dt.timedelta(seconds=3)
        self.assertFalse(self.tc.shifted)

    def test_favourites_first_and_settings_saved(self):
        self.tc.settings["favourites"] = ["Travincal"]
        self.assertEqual(self.tc.zones()[0], "Travincal")
        self.tc.save_settings()
        again = clock.TerrorClock(cache_dir=self.tmp, settings_file=self.tmp / "tz.json", clock=lambda: self.now)
        self.assertEqual(again.settings["favourites"], ["Travincal"])

    def test_imports_old_app_settings(self):
        clock.OLD_APP_DIR.mkdir()
        (clock.OLD_APP_DIR / "settings.txt").write_text(
            "﻿zone=Chaos Sanctuary\nfavourites=Travincal|Chaos Sanctuary\nmusic=1\nautoRevert=0\n", encoding="utf-8")
        tc = clock.TerrorClock(cache_dir=self.tmp, settings_file=self.tmp / "fresh.json", clock=lambda: self.now)
        self.assertEqual(tc.settings["zone"], "Chaos Sanctuary")
        self.assertEqual(tc.settings["favourites"], ["Travincal", "Chaos Sanctuary"])
        self.assertFalse(tc.settings["auto_revert"])

    def test_old_app_schedule_is_an_offline_fallback(self):
        clock.OLD_APP_DIR.mkdir()
        start = dt.datetime(2026, 10, 4, 11, 0, tzinfo=UTC)
        (clock.OLD_APP_DIR / "tz-schedule.json").write_text(schedule(start, ["X", "Y"]), encoding="utf-8")
        tc = clock.TerrorClock(cache_dir=self.tmp / "empty", settings_file=self.tmp / "s.json", clock=lambda: self.now)
        tc.load_schedule(download=False)
        self.assertEqual([s["zone"] for s in tc.sessions], ["X", "Y"])
        self.assertIn("offline", tc.source)

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_helper_cannot_change_the_clock_without_admin(self):
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("running as administrator: won't touch the real clock")
        now = dt.datetime.now().replace(microsecond=0).isoformat()
        self.assertEqual(clock.helper_main(now), 1314)  # ERROR_PRIVILEGE_NOT_HELD


if __name__ == "__main__":
    unittest.main()
