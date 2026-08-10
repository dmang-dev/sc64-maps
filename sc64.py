#!/usr/bin/env python3
"""
sc64.py -- one command to turn a StarCraft 64 cartridge dump into maps you can
play, with the mission briefings inside them.

    python sc64.py

That is usually the whole thing. It finds the ROM, extracts the 96 scenarios,
compiles each N64 briefing into the map, checks the results, and offers to copy
them into your StarCraft Maps folder. Everything it does can be done by hand
with the individual tools; this just removes the steps.

    python sc64.py --rom "StarCraft 64 (USA).n64"   # if it cannot find it
    python sc64.py --install                        # copy to Maps\\sc64 too
    python sc64.py --list                           # show what is in the ROM
    python sc64.py --no-briefings                   # maps only

You supply the ROM. Nothing is downloaded, and the maps it produces are
Blizzard's copyrighted work -- keep them to yourself.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROM_EXTENSIONS = ("*.n64", "*.z64", "*.v64")
ROM_SEARCH_DIRS = (".", "gamedata/roms", "roms", "..")

MIN_PYTHON = (3, 9)


# Every known release. All four extract to 96 scenarios and 96 briefings; the
# cartridge id identifies three of them and the beta is recognised by its BOLT
# build stamp, its own header being unfinished (the name field is not ASCII).
#
# Ranked so that, given several ROMs, the one you almost certainly want wins.
# USA and Australia carry byte-identical BOLT archives, so either is equally
# good as a source; the beta and the German build genuinely differ.
ROM_VARIANTS = {
    "NSQE": (0, "USA (retail)", ""),
    "NSQP": (1, "Australia / PAL",
             "BOLT archive byte-identical to USA retail"),
    "NSQD": (2, "Germany",
             "later build than retail (2000-06-05); German text, some retuned "
             "triggers and a few terrain edits"),
}
BETA_STAMP = "1999-09-29"
BETA_NOTE = ("pre-release build, six weeks before retail; identical terrain "
             "but 25 scenarios differ in units, strings and triggers")


def identify_rom(path: str):
    """Return (rank, label, note) for a ROM, without fully parsing it."""
    import extract_sc64_maps as ex
    try:
        data = ex.load_rom(path)
        cart = ex.rom_cart_id(data)
        archive = ex.BoltArchive(data)
        stamp = archive.build_stamp
    except Exception:
        return (9, "unrecognised", "")
    if stamp.startswith(BETA_STAMP):
        return (3, "USA (beta)", BETA_NOTE)
    if cart in ROM_VARIANTS:
        return ROM_VARIANTS[cart]
    return (8, f"unknown region {cart!r}", "")


def find_roms() -> list[str]:
    """Every plausible ROM near the script, best candidate first."""
    seen = []
    for directory in ROM_SEARCH_DIRS:
        base = os.path.join(HERE, directory)
        if not os.path.isdir(base):
            continue
        for pattern in ROM_EXTENSIONS:
            seen += glob.glob(os.path.join(base, pattern))
    seen = sorted({os.path.abspath(p) for p in seen
                   if os.path.getsize(p) >= 8 * 2**20})
    return sorted(seen, key=lambda p: (identify_rom(p)[0], os.path.basename(p)))


def find_rom(explicit: str | None) -> str | None:
    """Locate a StarCraft 64 ROM, preferring an explicit path."""
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    roms = find_roms()
    return roms[0] if roms else None


def make_solo_variants(maps_dir: str, out_dir: str) -> int:
    """Write a single-player variant of every co-operative map.

    A handful of the missions expect two humans. Under Use Map Settings the
    unfilled slot never spawns its hero, and a "hero must survive" trigger
    ends the game seconds in -- Resurrection IV is the obvious case. Folding
    the extra slots into the first makes them reachable alone, at the cost of
    handing you both armies.
    """
    from merge_players import computer_slots, human_slots, make_solo
    from extract_sc64_maps import build_map_file, looks_like_chk
    from verify_maps import MpqReader

    made = 0
    for pattern in ("*.scm", "*.scx"):
        for path in sorted(glob.glob(os.path.join(maps_dir, pattern))):
            try:
                chk = MpqReader(path).read("staredit\\scenario.chk")
            except Exception:
                continue
            if not chk or not looks_like_chk(chk):
                continue
            if len(human_slots(chk)) < 2:
                continue
            # A melee map with several human slots is not a co-op mission --
            # you fill the other slots with computer opponents and play it as
            # designed. Merging there would just hand one player every
            # starting base. What marks a genuine co-op mission is that the
            # second human is *scripted*: its slot appears in trigger
            # conditions, actions or the executed-for-player array. Requiring
            # a computer slot too keeps the result launchable, since Use Map
            # Settings refuses a map with no opponent.
            if not computer_slots(chk):
                continue
            result = make_solo(chk)
            if not result:
                continue
            merged, stats = result
            if stats["trig"] == 0 and stats["mbrf"] == 0:
                continue
            if not looks_like_chk(merged):
                print(f"  ! {os.path.basename(path)}: rewrite was malformed; "
                      f"skipped", file=sys.stderr)
                continue
            os.makedirs(out_dir, exist_ok=True)
            stem, ext = os.path.splitext(os.path.basename(path))
            with open(os.path.join(out_dir, f"{stem} (solo){ext}"), "wb") as fh:
                fh.write(build_map_file(merged))
            made += 1
            print(f"  {stem}: merged player(s) "
                  f"{', '.join(str(p) for p in stats['merged'])} into 1 "
                  f"({stats['units']} units, {stats['trig']} trigger refs)")
    return made


def step(number: int, total: int, text: str) -> None:
    print(f"\n[{number}/{total}] {text}")


def main(argv=None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
              f"this is {sys.version.split()[0]}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(
        description="Extract StarCraft 64 maps and briefings in one step.",
        epilog="The maps are Blizzard's copyrighted content. Keep them to "
               "yourself.")
    parser.add_argument("--rom", help="path to the ROM (auto-detected if omitted)")
    parser.add_argument("-o", "--out", default=os.path.join(HERE, "gamedata", "maps"),
                        help="where to write the maps")
    parser.add_argument("--briefings-dir",
                        default=os.path.join(HERE, "gamedata", "briefings"),
                        help="where to write the readable briefing text")
    parser.add_argument("--no-briefings", action="store_true",
                        help="do not compile briefings into the maps")
    parser.add_argument("--no-solo", action="store_true",
                        help="do not make single-player variants of the "
                             "two-player co-op maps")
    parser.add_argument("--install", action="store_true",
                        help="also copy the maps into StarCraft's Maps folder")
    parser.add_argument("--starcraft", help="StarCraft install directory")
    parser.add_argument("--list", action="store_true",
                        help="list what is in the ROM and stop")
    parser.add_argument("--roms", action="store_true",
                        help="list every ROM found and stop")
    parser.add_argument("--all-roms", action="store_true",
                        help="process every ROM found, into per-variant folders")
    args = parser.parse_args(argv)

    if args.roms or args.all_roms:
        found = find_roms()
        if not found:
            print("No ROMs found.", file=sys.stderr)
            return 1
        print(f"{len(found)} ROM(s) found:\n")
        for path in found:
            _rank, label, note = identify_rom(path)
            print(f"  {os.path.basename(path)}")
            print(f"      {label}" + (f" -- {note}" if note else ""))
        if args.roms:
            print("\nThe first is used by default; pass --rom PATH to choose, "
                  "or --all-roms for every one.")
            return 0
        # Process them all, each into its own folder.
        rc = 0
        for path in found:
            _rank, label, _note = identify_rom(path)
            slug = "".join(c if c.isalnum() else "-" for c in label).strip("-")
            print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
            sub = list(argv or sys.argv[1:])
            sub = [a for a in sub if a != "--all-roms"]
            rc |= main(sub + ["--rom", path,
                              "-o", os.path.join(args.out, slug),
                              "--briefings-dir",
                              os.path.join(args.briefings_dir, slug)])
        return rc

    rom = find_rom(args.rom)
    if not rom:
        print("Could not find a StarCraft 64 ROM.\n", file=sys.stderr)
        print("Put one next to this script, or in gamedata/roms/, or pass "
              "--rom PATH.", file=sys.stderr)
        print("Any of .n64, .z64 or .v64 works -- the header decides, not the "
              "extension.", file=sys.stderr)
        return 1

    import extract_sc64_maps as ex
    import extract_briefings as eb
    import verify_maps

    try:
        data = ex.load_rom(rom)
        archive = ex.BoltArchive(data)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    name = ex.rom_internal_name(data)
    _rank, label, note = identify_rom(rom)
    print(f"ROM      : {rom}")
    print(f"Variant  : {label}" + (f"  ({note})" if note else ""))
    print(f"BOLT     : offset {archive.base:#x}, built {archive.build_stamp}")
    # The beta's header is unfinished, so the name field is not ASCII there.
    # That is expected and not a reason to stop.
    if "STARCRAFT" not in name.upper() and not label.startswith("USA (beta)"):
        print("warning  : this does not look like StarCraft 64 -- continuing",
              file=sys.stderr)
    if not args.rom:
        others = [p for p in find_roms() if p != os.path.abspath(rom)]
        if others:
            print(f"           ({len(others)} other ROM(s) present -- "
                  f"--roms to list, --all-roms to do every one)")

    if args.list:
        return ex.main([rom, "--list"])

    total = 3 + (0 if args.no_solo else 1) + (1 if args.install else 0)
    n = 0

    n += 1
    step(n, total, "Extracting maps"
         + ("" if args.no_briefings else " with briefings compiled in"))
    argv_maps = [rom, "-o", args.out]
    if not args.no_briefings:
        argv_maps.append("--briefings")
    rc = ex.main(argv_maps)
    if rc:
        return rc

    n += 1
    step(n, total, "Extracting the briefings as readable text")
    rc = eb.main([rom, "-o", args.briefings_dir])
    if rc:
        return rc

    solo_dir = os.path.join(args.out, "solo")
    if not args.no_solo:
        n += 1
        step(n, total, "Making co-op maps playable alone")
        made = make_solo_variants(args.out, solo_dir)
        print(f"wrote {made} solo variant(s) to {solo_dir}" if made
              else "no co-op maps found; nothing to do")

    n += 1
    step(n, total, "Checking the maps are loadable")
    rc = verify_maps.main([args.out])
    if rc:
        print("\nSome maps failed verification -- do not install these.",
              file=sys.stderr)
        return rc
    if not args.no_solo and os.path.isdir(solo_dir) and os.listdir(solo_dir):
        rc = verify_maps.main([solo_dir])
        if rc:
            print("\nSolo variants failed verification.", file=sys.stderr)
            return rc

    if args.install:
        step(4, total, "Copying into StarCraft")
        from starcraft_install import find_install
        install = find_install(args.starcraft)
        if not install or not install.maps_dir:
            print("Could not find a StarCraft install with a Maps folder.",
                  file=sys.stderr)
            print("Pass --starcraft DIR, or set STARCRAFT_DIR.", file=sys.stderr)
            return 1
        print(install.describe())
        dest = os.path.join(install.maps_dir, "sc64")
        os.makedirs(dest, exist_ok=True)
        copied = 0
        for pattern in ("*.scm", "*.scx"):
            for path in glob.glob(os.path.join(args.out, pattern)):
                shutil.copy2(path, dest)
                copied += 1
        print(f"copied {copied} maps to {dest}")
        # Solo variants go in their own subfolder so the map list is not
        # cluttered with two entries per co-op mission.
        if os.path.isdir(solo_dir):
            solo_dest = os.path.join(dest, "solo")
            os.makedirs(solo_dest, exist_ok=True)
            solo_copied = 0
            for pattern in ("*.scm", "*.scx"):
                for path in glob.glob(os.path.join(solo_dir, pattern)):
                    shutil.copy2(path, solo_dest)
                    solo_copied += 1
            if solo_copied:
                print(f"copied {solo_copied} solo variant(s) to {solo_dest}")

    print("\nDone.")
    print(f"  maps      : {os.path.abspath(args.out)}")
    print(f"  briefings : {os.path.abspath(args.briefings_dir)}")
    if not args.install:
        print("\nCopy the maps into your StarCraft Maps folder to play them, "
              "or re-run with --install.")
    print("\nIn game: Single Player -> Custom -> Use Map Settings. Maps needing "
          "more than one\nhuman slot skip the briefing, so start with a "
          "one-player map such as Rage.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
