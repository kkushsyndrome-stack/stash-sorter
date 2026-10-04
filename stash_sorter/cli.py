"""Command line interface. Run `python -m stash_sorter --help`."""

import argparse
import json
import sys

from . import __version__
from .apply import ApplyError
from .rules import RulesError


def _session(args):
    from .service import Session
    return Session(saves=args.saves, install=args.install, data_dir=args.data_dir, backups=args.backups,
                   sessions=args.sessions)


def _split(s):
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def _plan_opts(args):
    return {
        "mode": args.mode, "rename": args.rename, "use_stackables": args.stackables,
        "stash_file": args.stash, "source_stash": args.source_stash, "mule_max_level": args.mule_level,
        "mule_name_hint": not args.no_name_hint, "mules": _split(args.mules) or None,
        "exclude": _split(args.exclude), "keep_in_stash": _split(args.keep), "limit": args.limit,
        "create_mules": args.create_mules, "crafter_level": args.crafter_level, "compact": not args.no_compact,
    }


def cmd_scan(args):
    s = _session(args)
    st = s.state()
    print(f"Save folder : {st['save_dir']}\nGame data   : {st['data_source']}\nArtwork     : "
          f"{'available' if st['art'] else 'not found (needs extracted game files)'}\nBackups     : {st['backup_dir']}")
    if st["journal"]:
        print("\n!! A previous change was interrupted. Run `recover` to roll it back.")
    for x in st["stashes"]:
        tabs = ", ".join(f"{'stackables' if t['type'] == 1 else 'chronicle' if t['type'] == 2 else 'tab'}:{t['items']}"
                         for t in x["tabs"])
        mark = "  <- default" if x["file"] == st["default_stash"] else ""
        print(f"Stash       : {x['file']}  [{tabs}] gold {x['gold']:,}{mark}")
    print(f"\n{'Name':16s} {'Class':12s} {'Lvl':>3s}  {'Items':>5s} {'Cells':>7s} {'Gold':>10s}  Mostly / notes")
    for c in sorted(st["characters"], key=lambda c: (not c["mule_ok"], c["name"].lower())):
        note = st["categories"].get(c["top_category"], "empty") if c["items"] else "empty"
        if not c["mule_ok"]:
            note = c["reason"]
        print(f"{c['name']:16s} {c['class']:12s} {c['level']:3d}  {c['items']:5d} {c['cells']:3d}/140 {c['gold']:10,d}  {note}")
    for f, e in st["errors"]:
        print(f"! could not read {f}: {e}")


def _print_plan(p):
    print(f"Plan for {p['stash']} ({p['mode']}): {p['moves']} moves, {len(p['merges'])} stacked, "
          f"{len(p['renames'])} renames, {len(p['new_mules'])} new mules, {len(p['unplaced'])} without room")
    for n in p["notes"]:
        print(f"  note: {n}")
    for dest, items in sorted(p["by_dest"].items(), key=lambda kv: -len(kv[1])):
        print(f"\n  -> {dest} ({len(items)} items)")
        for i in items:
            print(f"       {i['name']:42s} from {i['from']}")
    if p["merges"]:
        print("\n  Stacked into the Stackables tab:")
        for m in p["merges"]:
            print(f"       {m['name']:42s} from {m['from']}")
    if p["new_mules"]:
        print("\n  New mules:")
        for n in p["new_mules"]:
            print(f"       {n['name']:16s} (copy of {n['template']}, empty)")
    if p["renames"]:
        print("\n  Renames:")
        for r in p["renames"]:
            print(f"       {r['old']:16s} -> {r['new']}")
    if p["unplaced"]:
        print("\n  No room for (stays where it is):")
        for u in p["unplaced"]:
            print(f"       {u['name']:42s} {u['from']}")
    if p["left_in_place"]:
        print(f"\n  {p['left_in_place']} misplaced item(s) stay put (no room on a better mule).")
    if p["emptied"]:
        print(f"\n  These mules will be empty afterwards: {', '.join(p['emptied'])}")
    for d in p["deletions"]:
        print(f"  DELETE item {d['name']:36s} {d['where']}")
    for d in p["delete_chars"]:
        print(f"  DELETE mule {d['name']} ({d['file']} and its side files)")


def cmd_plan(args):
    s = _session(args)
    _print_plan(s.make_plan(_plan_opts(args)))


def cmd_apply(args):
    s = _session(args)
    p = s.make_plan(_plan_opts(args))
    _print_plan(p)
    _confirm_and_apply(s, p, args.yes)


def _confirm_and_apply(s, p, yes):
    if not (p["moves"] or p["merges"] or p["renames"] or p["new_mules"] or p["deletions"] or p["delete_chars"]):
        print("\nNothing to do.")
        return
    if not yes:
        ans = input("\nClose Diablo II: Resurrected completely, then type 'apply' to continue: ")
        if ans.strip().lower() != "apply":
            print("Cancelled; nothing was changed.")
            return
    try:
        res = s.apply(p["id"])
    except ApplyError as e:
        print(f"\nNothing was changed: {e}")
        sys.exit(2)
    print(f"Backup: {res['backup']}\nLog   : {res['log']}\nUndo  : python -m stash_sorter undo {res['log'].split(chr(92))[-1].split('/')[-1]}")


def cmd_assess(args):
    s = _session(args)
    crafter = args.crafter_level or s.max_char_level()
    rows = s.assess_rows(crafter, "all" if args.all else "stash")
    if args.kind:
        rows = [r for r in rows if r["assess"]["kind"] == args.kind]
    rows.sort(key=lambda r: (r["assess"]["kind"], -r["assess"]["score"], -r["ilvl"]))
    print(f"Crafting character level: {crafter}\n")
    for r in rows:
        a = r["assess"]
        res = (f"crafts at ilvl {a['crafted_ilvl']}" if a["kind"] == "craft" else
               f"affix lvl {a['alvl']}" if a["kind"] == "reroll" else f"{a['max_sockets']} sockets max")
        stars = "*" * a["score"] + "." * (3 - a["score"])
        keep = {"keep": "KEEP", "maybe": "keep?"}.get(a["as_is"], "")
        print(f"{stars} {a['tier']:4s} {keep:5s} ilvl {r['ilvl']:2d}  {res:18s} {r['name'][:40]:40s} "
              f"{r['where'][:24]:24s} {a['note']}")


def cmd_find(args):
    s = _session(args)
    words = [w.lower() for w in args.query]
    for it in s.all_items():
        text = " ".join([it["name"], it["base"], it["where"]] + it["stats"]).lower()
        if all(w in text for w in words):
            print(f"{it['name'][:40]:40s} {it['where'][:34]:34s} ilvl {it['ilvl']:2d}  {'; '.join(it['stats'][:3])}")


def cmd_dupes(args):
    s = _session(args)
    groups = s.duplicates()
    print(f"{len(groups)} unique/set items you have more than once "
          f"({sum(len(g['copies']) - 1 for g in groups)} extra copies). Delete with: delete-items KEY [KEY ...]\n")
    for g in groups:
        print(f"{g['name']} ({g['kind']}, {len(g['copies'])} copies)")
        for c in g["copies"]:
            flag = "" if c["deletable"] else "  [equipped: can't delete]"
            print(f"    {c['key']:44s} ilvl {c['ilvl']:2d}  {c['where'][:30]:30s} {'; '.join(c['stats'][:3])}{flag}")


def cmd_delete_items(args):
    s = _session(args)
    try:
        p = s.plan_delete_items(args.keys)
    except (ValueError, KeyError) as e:
        print(f"Nothing was changed: {e}")
        sys.exit(2)
    _print_plan(p)
    _confirm_and_apply(s, p, args.yes)


def cmd_empty_mules(args):
    s = _session(args)
    for m in s.empty_mules():
        state = "EMPTY" if m["empty"] else m["reason"]
        print(f"{m['name']:16s} {m['class']:12s} lvl {m['level']:2d}  {state}")


def cmd_delete_mules(args):
    s = _session(args)
    try:
        p = s.plan_delete_mules(args.names)
    except ValueError as e:
        print(f"Nothing was changed: {e}")
        sys.exit(2)
    _print_plan(p)
    _confirm_and_apply(s, p, args.yes)


def _fmt_seconds(n):
    m, sec = divmod(int(n), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def cmd_track(args):
    import time
    s = _session(args)
    t = s.tracker
    sess = t.start()
    print(f"Tracking session {sess['id']} - play as normal; every Save & Exit is logged. Ctrl+C ends the session.")
    try:
        while True:
            time.sleep(2)
            run = t.poll()
            if not run:
                continue
            who = ", ".join(run["characters"])
            head = f"Run {run['n']}" if run["kind"] == "run" else "Mule visit"
            print(f"\n{head} - {who} - {_fmt_seconds(run['seconds'])} - XP {run['xp']:+,} - gold {run['gold']:+,}")
            for f in run["found"]:
                star = " NEW!" if f["name"] in run["grail"] else ""
                print(f"   + {f['count']}x {f['name']} [{f['quality']}]{star}")
            for f in run["left"]:
                print(f"   - {f['count']}x {f['name']}")
    except KeyboardInterrupt:
        done = t.end()
        tot = done["totals"]
        print(f"\nSession ended: {tot['runs']} runs in {_fmt_seconds(tot['seconds'])} ({tot['runs_per_hour']}/h), "
              f"{tot['found']} items found, XP {tot['xp']:+,}, gold {tot['gold']:+,}")


def cmd_tz(args):
    from .clock import TerrorClock, ClockError
    tc = TerrorClock()
    tc.measure_offset()
    try:
        tc.load_schedule()
    except ClockError as e:
        print(e)
        sys.exit(1)
    print(f"Schedule {tc.source}. " + (f"Your PC clock is off by {tc.offset}." if tc.shifted else "Your PC clock is right."))
    for s in tc.upcoming(args.count):
        start = s["start"].astimezone().strftime("%a %H:%M")
        imm = ", ".join(s["immunities"]) or "none"
        print(f"{start}  {s['zone'][:70]:70s} immunities: {imm}")


def cmd_launch_args(args):
    from . import launcher as L
    try:
        current = L.read_args()
    except L.LaunchError as e:
        print(e)
        sys.exit(1)
    print(f"Battle.net settings: {L.config_path()}")
    print(f"Current D2R launch arguments: {current or '(none)'}")
    for prob in L.parse_args(current)["problems"]:
        print(f"  ! {prob}")
    if args.set is not None:
        s = _session(args)
        try:
            backup = L.write_args(args.set, s.backup_dir)
        except L.LaunchError as e:
            print(f"Nothing was saved: {e}")
            sys.exit(2)
        print(f"Saved: {args.set}\nBackup of the previous settings: {backup}")


def cmd_launch(args):
    from . import launcher as L
    try:
        L.launch_d2r()
    except L.LaunchError as e:
        print(e)
        sys.exit(1)
    print("Starting Diablo II: Resurrected through Battle.net...")


SEED_RUN_SAY = {
    "wait_closed": "Close D2R and quit Battle.net completely (right-click its icon next to the clock, then Exit).",
    "starting": "Battle.net is starting D2R with the seed (log in if it asks)...",
    "playing": "D2R is running with the seed: load the character, enter a game, then Save & Exit and close D2R.",
    "wait_closed_after": "Now quit Battle.net completely again so the seed can come off.",
}


def cmd_seed_run(args):
    """Start D2R once with -seed, then take it off and start D2R normally (follows along until it's done)."""
    import time
    from pathlib import Path
    from . import launcher as L
    from .paths import app_dir, backups_dir
    from .world import find_save_dir
    try:
        saves = find_save_dir(args.saves)
    except FileNotFoundError:
        saves = None
    run = L.SeedRun(app_dir() / "seedrun.json", saves_dir=saves,
                    backup_dir=Path(args.backups) if args.backups else backups_dir())
    try:
        if args.cancel:
            run.cancel()
        elif args.seed is not None:
            run.start(args.seed)
        elif not run.active:
            print("No seed run is going. Start one with: seed-run SEED")
            return
    except L.LaunchError as e:
        print(e)
        sys.exit(1)
    shown, notes = None, 0
    try:
        while True:
            st = run.tick()
            for n in st["notes"][notes:]:
                print(f"  {n}")
            notes = len(st["notes"])
            if st["stage"] != shown:
                shown = st["stage"]
                if shown == "wait_closed_after" and st["relaunch"] and st["changed"] is not None:
                    print("  Saved during the seeded game: " + (", ".join(st["changed"]) or
                          "nobody. If you didn't enter a game the map didn't change: press Play in Battle.net "
                          "(it still has the seed), play a game, close D2R, then quit Battle.net."))
                if shown in SEED_RUN_SAY:
                    print(SEED_RUN_SAY[shown])
            if st.get("error"):
                print(f"  ! {st['error']}")
            if not run.active:
                print("Done: the seed is off" + (" and D2R is starting normally." if shown == "done" else "."))
                run.dismiss()
                return
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopped watching. The seed run carries on next time (seed-run, or the Launch tab) and "
              "seed-run --cancel takes the seed off.")


def cmd_set_clock(args):
    """Elevated helper started by the Terror Zones tab (Windows asks for permission first)."""
    from .clock import helper_main
    real = float(args.real_epoch) if args.real_epoch else None
    system = float(args.system_epoch) if args.system_epoch else None
    sys.exit(helper_main(args.when, real, system))


def cmd_backups(args):
    s = _session(args)
    for b in s.backups():
        undo = "undo: " + b["log"] if b["can_undo"] else ""
        print(f"{b['time']}  {b['size'] // 1024:6d} KB  {b['file']:44s} {b['summary']}  {undo}")


def cmd_restore(args):
    s = _session(args)
    if not args.yes and input(f"Restore {args.file} over {s.save_dir}? type 'restore': ").strip() != "restore":
        print("Cancelled.")
        return
    print(s.restore(args.file))


def cmd_undo(args):
    s = _session(args)
    if not args.yes and input(f"Undo the change logged in {args.log}? type 'undo': ").strip() != "undo":
        print("Cancelled.")
        return
    try:
        print(s.undo(args.log))
    except ApplyError as e:
        print(f"Nothing was changed: {e}")
        sys.exit(2)


def cmd_recover(args):
    s = _session(args)
    print(s.recover() if s.state()["journal"] else "Nothing to recover.")


def cmd_rules(args):
    s = _session(args)
    if args.reset:
        s.reset_rules()
        print("Rules reset to defaults.")
    elif args.load:
        try:
            s.save_rules(json.loads(open(args.load, encoding="utf-8").read()))
        except (RulesError, json.JSONDecodeError) as e:
            print(f"Rules not saved: {e}")
            sys.exit(2)
        print(f"Rules saved to {s.rules_path}")
    else:
        print(json.dumps(s.rules_json()["rules"], indent=2))


def cmd_gui(args):
    from .server import serve
    serve(_session(args), port=args.port, open_browser=not args.no_browser)


def cmd_build_gamedata(args):
    from .gamedata import build_bundle, BUNDLE_PATH
    p = build_bundle(args.out or BUNDLE_PATH, install_dir=args.install, data_dir=args.data_dir,
                     game_version=args.game_version)
    print(f"Wrote {p}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="stash-sorter", description="Empty and organise your D2R shared stash across mules.")
    ap.add_argument("--version", action="version", version=f"Stash Sorter {__version__}")
    ap.add_argument("--saves", help="save folder (default: auto-detect Saved Games/Diablo II Resurrected)")
    ap.add_argument("--install", help="D2R install folder, used to read extracted game tables (default: auto-detect)")
    ap.add_argument("--data-dir", help="folder with extracted data/global/excel tables (e.g. for a mod)")
    ap.add_argument("--backups", help="where backups are written (default: ./backups next to the tool)")
    ap.add_argument("--sessions", help="where session history is kept (default: ./sessions next to the tool)")
    sub = ap.add_subparsers(dest="cmd")

    def plan_args(p):
        p.add_argument("--mode", choices=["stash", "tidy", "reorganize", "migrate"], default="stash",
                       help="stash: empty the shared stash onto mules; tidy: also move misplaced mule items; "
                            "reorganize: re-sort stash + all mules; migrate: old stash -> RotW stash")
        p.add_argument("--rename", choices=["none", "new", "all"], default="none", help="rename mules by category")
        p.add_argument("--stackables", action="store_true", help="(beta) stack runes/gems/materials into the RotW Stackables tab")
        p.add_argument("--stash", help="stash file name (default: the one most characters use)")
        p.add_argument("--source-stash", help="migrate: the older stash file to move from")
        p.add_argument("--mule-level", type=int, default=1, help="characters at or below this level are mules (default 1)")
        p.add_argument("--no-name-hint", action="store_true", help="don't treat characters with 'mule' in the name as mules")
        p.add_argument("--mules", help="comma-separated mule names (overrides the rules above)")
        p.add_argument("--exclude", help="comma-separated character names never to touch")
        p.add_argument("--keep", help="comma-separated categories to leave in the stash (e.g. runes,gems)")
        p.add_argument("--limit", type=int, default=0, help="test run: only move this many items")
        p.add_argument("--create-mules", type=int, default=0, help="(experimental) create up to N new mules if needed")
        p.add_argument("--crafter-level", type=int, help="crafting character level for the craft-bait rules")
        p.add_argument("--no-compact", action="store_true", help="tidy: don't pack categories onto fewer mules")

    p = sub.add_parser("gui", help="open the browser GUI (default)")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_gui)
    sub.add_parser("scan", help="list characters and stashes").set_defaults(func=cmd_scan)
    p = sub.add_parser("plan", help="show what would happen (changes nothing)")
    plan_args(p)
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("apply", help="back up, then carry out the plan")
    plan_args(p)
    p.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_apply)
    p = sub.add_parser("assess", help="item level assessment for crafting / rerolling / sockets")
    p.add_argument("--crafter-level", type=int, help="level of the character doing the crafting (default: your highest)")
    p.add_argument("--all", action="store_true", help="include items on characters, not just the shared stash")
    p.add_argument("--kind", choices=["craft", "reroll", "base"])
    p.set_defaults(func=cmd_assess)
    p = sub.add_parser("find", help="search every item by name, stats or location")
    p.add_argument("query", nargs="+")
    p.set_defaults(func=cmd_find)
    sub.add_parser("track", help="session tracker in the terminal: logs every Save & Exit").set_defaults(func=cmd_track)
    p = sub.add_parser("tz", help="current and upcoming terror zones (offline schedule)")
    p.add_argument("--count", type=int, default=8)
    p.set_defaults(func=cmd_tz)
    p = sub.add_parser("launch-args", help="show (or --set) D2R's launch arguments in Battle.net")
    p.add_argument("--set", help='new arguments, e.g. "-direct -txt -seed 12345" (Battle.net must be closed)')
    p.set_defaults(func=cmd_launch_args)
    sub.add_parser("launch", help="start D2R through Battle.net").set_defaults(func=cmd_launch)
    p = sub.add_parser("seed-run", help="give a character a map seed: start D2R once with -seed, then without it")
    p.add_argument("seed", nargs="?", type=int, help="map seed (leave out to carry on with an unfinished run)")
    p.add_argument("--cancel", action="store_true", help="take the seed back off (once Battle.net is closed)")
    p.set_defaults(func=cmd_seed_run)
    p = sub.add_parser("set-clock", help="(used by the Terror Zones tab; needs administrator rights)")
    p.add_argument("when", help="local time YYYY-MM-DDTHH:MM:SS, or 'real'")
    p.add_argument("real_epoch", nargs="?")
    p.add_argument("system_epoch", nargs="?")
    p.set_defaults(func=cmd_set_clock)
    sub.add_parser("dupes", help="list unique and set items you have more than once").set_defaults(func=cmd_dupes)
    p = sub.add_parser("delete-items", help="delete items by key (see `dupes`); backed up and undoable")
    p.add_argument("keys", nargs="+")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_delete_items)
    sub.add_parser("empty-mules", help="list mules and whether they are empty").set_defaults(func=cmd_empty_mules)
    p = sub.add_parser("delete-mules", help="delete empty mules (save + side files); backed up and undoable")
    p.add_argument("names", nargs="+")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_delete_mules)
    sub.add_parser("backups", help="list backups (and which changes can be undone)").set_defaults(func=cmd_backups)
    p = sub.add_parser("undo", help="undo one apply, given its -log.json file name")
    p.add_argument("log")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_undo)
    p = sub.add_parser("restore", help="restore a whole backup zip")
    p.add_argument("file")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_restore)
    sub.add_parser("recover", help="roll back an interrupted change").set_defaults(func=cmd_recover)
    p = sub.add_parser("rules", help="show, load or reset the sorting rules")
    p.add_argument("--load", help="JSON file with rules to use")
    p.add_argument("--reset", action="store_true")
    p.set_defaults(func=cmd_rules)
    p = sub.add_parser("build-gamedata", help="refresh the bundled game data snapshot from extracted tables")
    p.add_argument("--out")
    p.add_argument("--game-version", default="")
    p.set_defaults(func=cmd_build_gamedata)

    for stream in (sys.stdout, sys.stderr):  # item names can contain non-ASCII characters
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = ap.parse_args(argv)
    if not args.cmd:
        args = ap.parse_args((argv if argv is not None else sys.argv[1:]) + ["gui"])
    try:
        args.func(args)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
