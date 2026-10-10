"""Art Mod: edits to a pretend game Data folder laid out like the real one (the real game folder is never touched)."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stash_sorter import artmod as M  # noqa: E402


def art_json(entries):
    """uniques/sets style: CRLF, entries laid out like the game's own file."""
    parts = []
    for key, vals in entries:
        body = ",\r\n".join(f'  "{t}": "{v}"' for t, v in vals.items())
        parts.append(f'  {{ "{key}": {{\r\n{body}\r\n}} }}')
    return "[\r\n" + ",\r\n".join(parts) + "\r\n]"


def same(v):
    return {"normal": v, "uber": v, "ultra": v}


UNIQUES = art_json([("the_gnasher", same("axe/the_gnasher")), ("harlequin_crest", same("helmet/cap_hat")),
                    ("stone_of_jordan", same("ring/ring1")), ("arkaines_valor", same("armor/ancient_armor"))])
SETS = art_json([("civerbs_ward", same("shield/stormguild")), ("tal_rashas_horadric_crest", same("helmet/mask"))])
ITEMS = ("[\r\n  { \"hax\": { \"asset\": \"axe/hand_axe\" } },\r\n  { \"cap\": { \"asset\": \"helmet/cap\" } },\r\n"
         "  { \"uap\": { \"asset\": \"helmet/cap_hat\" } }\r\n]")
PRESETS = json.dumps([
    {"name": "Quilted Armor", "components": {"Torso": 0, "Left Arm": 0, "Legs": 0}},
    {"name": "Dusk Shroud", "components": {"Torso": 2, "Left Arm": 2, "Legs": 1}},
    {"name": "Sacred Armor", "components": {"Torso": 3, "Left Arm": 4, "Legs": 5}}], indent=4)


class ArtModTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artmod-"))
        self.items = self.tmp / "Data" / "hd" / "items"
        self.items.mkdir(parents=True)
        for name, text in (("uniques.json", UNIQUES), ("sets.json", SETS), ("items.json", ITEMS),
                           (M.PRESETS, PRESETS)):
            (self.items / name).write_bytes(text.encode())
        self.running = False
        self.mod = M.ArtMod(self.tmp / "Data", self.tmp / "store", game_running=lambda: self.running)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, name):
        return (self.items / name).read_bytes().decode()

    def test_available(self):
        self.assertTrue(self.mod.available)
        self.assertFalse(M.ArtMod(self.tmp / "nowhere", self.tmp / "s").available)

    def test_changing_a_unique_touches_only_its_three_lines(self):
        self.mod.set_art("unique", "harlequin_crest", "helmet/crown")
        new = self.raw("uniques.json")
        old = UNIQUES.split("\r\n")
        changed = [(a, b) for a, b in zip(old, new.split("\r\n")) if a != b]
        self.assertEqual(len(changed), 3)
        self.assertTrue(all("cap_hat" in a and "crown" in b for a, b in changed))
        self.assertEqual(len(new.split("\r\n")), len(old))
        self.assertIn("\r\n", new)  # line endings kept
        data = {k: v for e in json.loads(new) for k, v in e.items()}
        self.assertEqual(data["harlequin_crest"], same("helmet/crown"))
        self.assertEqual(data["the_gnasher"], same("axe/the_gnasher"))

    def test_sets_and_base_items(self):
        self.mod.set_art("set", "civerbs_ward", "shield/kite_shield")
        self.mod.set_art("base", "uap", "helmet/crown")
        self.assertIn('"normal": "shield/kite_shield"', self.raw("sets.json"))
        self.assertEqual(self.raw("items.json").replace("helmet/crown", "helmet/cap_hat"), ITEMS)
        self.assertEqual(self.raw("uniques.json"), UNIQUES)  # untouched files stay byte-identical

    def test_reset_one_item_and_restore_everything(self):
        before = {n: self.raw(n) for n in ("uniques.json", "sets.json", "items.json", M.PRESETS)}
        self.mod.set_art("unique", "harlequin_crest", "helmet/crown")
        self.mod.set_art("unique", "stone_of_jordan", "ring/ring3")
        self.mod.reset_art("unique", "harlequin_crest")
        now = {c["key"] for c in self.mod.changes()}
        self.assertEqual(now, {"stone_of_jordan"})
        self.mod.set_look("Dusk Shroud", "Sacred Armor")
        self.mod.set_art("base", "uap", "helmet/crown")
        self.assertEqual({c["kind"] for c in self.mod.changes()}, {"unique", "look", "base"})
        self.assertEqual(sorted(self.mod.restore_all()), sorted(["uniques.json", "items.json", M.PRESETS]))
        self.assertEqual({n: self.raw(n) for n in before}, before)  # byte for byte
        self.assertEqual(self.mod.changes(), [])

    def test_armour_look(self):
        self.mod.set_look("Dusk Shroud", "Sacred Armor")
        by = {e["name"]: e["components"] for e in json.loads(self.raw(M.PRESETS))}
        self.assertEqual(by["Dusk Shroud"], by["Sacred Armor"])
        self.assertEqual(by["Quilted Armor"], {"Torso": 0, "Left Arm": 0, "Legs": 0})
        look = next(x for x in self.mod.looks() if x["name"] == "Dusk Shroud")
        self.assertTrue(look["changed"])
        self.assertEqual(look["original"], {"Torso": 2, "Left Arm": 2, "Legs": 1})
        self.mod.reset_look("Dusk Shroud")
        self.assertEqual(self.raw(M.PRESETS), PRESETS)
        with self.assertRaises(M.ArtModError):
            self.mod.set_look("Nope", "Sacred Armor")

    def test_refuses_while_the_game_runs(self):
        self.running = True
        with self.assertRaises(M.ArtModError):
            self.mod.set_art("unique", "harlequin_crest", "helmet/crown")
        self.assertEqual(self.raw("uniques.json"), UNIQUES)
        self.assertFalse((self.tmp / "store" / "originals").exists())

    def test_refuses_when_something_else_changed_the_file(self):
        self.mod.set_art("unique", "harlequin_crest", "helmet/crown")
        (self.items / "uniques.json").write_bytes(UNIQUES.replace("ring1", "ring9").encode())  # e.g. a game update
        with self.assertRaises(M.ArtModError):
            self.mod.set_art("unique", "the_gnasher", "axe/axe")
        with self.assertRaises(M.ArtModError):
            self.mod.restore_all()
        self.mod.forget()  # accept the files as they are
        self.mod.set_art("unique", "the_gnasher", "axe/axe")
        self.assertIn("ring9", self.raw("uniques.json"))

    def test_bad_input(self):
        for args in (("unique", "No Such Unique!", "helmet/crown"), ("unique", "harlequin_crest", 'x"y'),
                     ("unique", "harlequin_crest", ""), ("weird", "x", "a/b")):
            with self.assertRaises(M.ArtModError, msg=str(args)):
                self.mod.set_art(*args)
        self.assertEqual(self.raw("uniques.json"), UNIQUES)

    def test_every_entry_with_the_same_key_changes(self):
        (self.items / "uniques.json").write_bytes((UNIQUES[:-3] + ",\r\n" + art_json(
            [("harlequin_crest", same("helmet/other"))])[3:]).encode())
        self.mod.set_art("unique", "harlequin_crest", "helmet/crown")
        values = [e["harlequin_crest"]["normal"] for e in json.loads(self.raw("uniques.json")) if "harlequin_crest" in e]
        self.assertEqual(values, ["helmet/crown", "helmet/crown"])

    def test_adds_an_entry_for_items_the_file_does_not_list_and_removes_it_again(self):
        self.mod.set_art("unique", "constricting_ring", "ring/ring3")
        self.mod.set_art("base", "xyz", "ring/ring3")
        data = {k: v for e in json.loads(self.raw("uniques.json")) for k, v in e.items()}
        self.assertEqual(data["constricting_ring"], same("ring/ring3"))
        self.assertEqual(len(data), 5)
        self.assertEqual([c["key"] for c in self.mod.changes()], ["constricting_ring", "xyz"])
        self.mod.reset_art("unique", "constricting_ring")
        self.mod.reset_art("base", "xyz")
        self.assertEqual(self.raw("uniques.json"), UNIQUES)
        self.assertEqual(self.raw("items.json"), ITEMS)
        self.assertEqual(self.mod.changes(), [])

    def test_find_key_matches_loosely(self):
        self.assertEqual(self.mod.find_key("unique", "Harlequin Crest"), "harlequin_crest")
        self.assertEqual(self.mod.find_key("set", "Tal Rasha's Horadric Crest"), "tal_rashas_horadric_crest")
        self.assertIsNone(self.mod.find_key("unique", "Nonexistent Thing"))

    def test_odd_layout_is_refused_not_mangled(self):
        (self.items / M.PRESETS).write_bytes(PRESETS.replace("    ", "  ").encode())
        with self.assertRaises(M.ArtModError):
            self.mod.set_look("Dusk Shroud", "Sacred Armor")


if __name__ == "__main__":
    unittest.main()
