#!/usr/bin/env python3
"""
merge_players.py -- fold one player's assets into another so a co-operative
map can be played solo.

    python merge_players.py "gamedata/maps/008-065 Resurrection IV.scx" \
        --from 2 --into 1 -o "gamedata/maps-solo"

Some StarCraft 64 missions are two-player co-op. Resurrection IV is the clear
case: player 1 is Raynor (Terran) and player 2 is Taldarin (Protoss), and its
defeat triggers require both heroes alive. Launched alone under Use Map
Settings, the second slot is never filled, that hero never spawns, and the
"must survive" trigger fires within a second of the map starting.

This rewrites the scenario so one player owns both sides:

  * every unit and sprite owned by the donor moves to the recipient
  * trigger and briefing player references are remapped, including the
    27-byte "executed for player" array on each record
  * the donor's slot is set inactive so the lobby stops asking for a human
  * force membership is moved across

What it does NOT do is rebalance anything. You end up controlling both armies,
which is easier than the mission was designed to be -- the point is to see
content that is otherwise unreachable alone, not to preserve the challenge.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from extract_sc64_maps import build_map_file, chk_sections, looks_like_chk, parse_map
from verify_maps import MpqReader

# CHK slot ownership
OWNR_INACTIVE, OWNR_COMPUTER, OWNR_HUMAN = 0, 5, 6
OWNR_NAMES = {0: "inactive", 1: "computer(game)", 2: "occupied", 3: "rescuable",
              4: "unused", 5: "computer", 6: "human", 7: "neutral"}

UNIT_SIZE, UNIT_OWNER = 36, 16
THG2_SIZE, THG2_OWNER = 10, 8

# TRIG/MBRF record geometry
RECORD_SIZE = 2400
COND_SIZE, COND_COUNT = 20, 16
ACT_BASE, ACT_SIZE, ACT_COUNT = 320, 32, 64
EXEC_BASE = 2368 + 4          # 27-byte "executed for player" array


def sections(chk: bytes):
    """Yield (tag, start_of_payload, length) for every section, in order."""
    pos = 0
    while pos + 8 <= len(chk):
        tag = chk[pos:pos + 4]
        length = struct.unpack_from("<i", chk, pos + 4)[0]
        yield tag, pos + 8, length
        pos += 8 + length


def replace_section(chk: bytes, tag: bytes, payload: bytes) -> bytes:
    """Replace the last occurrence of `tag` -- later sections win in CHK."""
    target = None
    for name, start, length in sections(chk):
        if name == tag:
            target = (start, length)
    if target is None:
        return chk
    start, length = target
    if len(payload) != length:
        raise ValueError(f"{tag!r}: replacement is {len(payload)} bytes, "
                         f"section is {length}")
    return chk[:start] + payload + chk[start + length:]


def _remap_records(payload: bytes, donor: int, into: int,
                   is_briefing: bool = False) -> tuple[bytes, int]:
    """Remap player references inside a TRIG or MBRF payload.

    `is_briefing` matters more than it looks. In TRIG, an action's group1 and
    group2 fields hold player groups, so they must be remapped. In MBRF they
    do NOT: group1 is the *portrait slot* (0-3) for ShowPortrait,
    HidePortrait, DisplaySpeakingPortrait and Transmission, and is unused
    otherwise. Remapping it there silently rewrites every portrait in slot N
    to slot N-1 -- merging player 2 into player 1 collapses slot 1 onto slot 0
    and the briefing loses a portrait frame. MBRF conditions are likewise
    always the single "always" opcode with a zero body, so only the
    executed-for-player array carries a real player reference.
    """
    data = bytearray(payload)
    changed = 0
    for record in range(len(data) // RECORD_SIZE):
        base = record * RECORD_SIZE

        if not is_briefing:
            # Conditions: the group/player field is a u32 at +4.
            for i in range(COND_COUNT):
                off = base + i * COND_SIZE
                if data[off + 15] == 0:          # condition type 0 = unused
                    continue
                if struct.unpack_from("<I", data, off + 4)[0] == donor:
                    struct.pack_into("<I", data, off + 4, into)
                    changed += 1

            # Actions: group1 at +16 and group2 at +20 hold player groups.
            for i in range(ACT_COUNT):
                off = base + ACT_BASE + i * ACT_SIZE
                if data[off + 26] == 0:          # action type 0 = unused
                    break
                for field in (16, 20):
                    if struct.unpack_from("<I", data, off + field)[0] == donor:
                        struct.pack_into("<I", data, off + field, into)
                        changed += 1

        # "Executed for player": one byte per player/force slot. Real in both.
        exec_off = base + EXEC_BASE
        if data[exec_off + donor]:
            data[exec_off + donor] = 0
            data[exec_off + into] = 1
            changed += 1

    return bytes(data), changed


def merge(chk: bytes, donor: int, into: int) -> tuple[bytes, dict]:
    """Move everything owned by `donor` (0-based) to `into`. Returns (chk, stats)."""
    stats = {"units": 0, "sprites": 0, "trig": 0, "mbrf": 0, "forces": 0}
    payloads = {}
    for tag, start, length in sections(chk):
        payloads[tag] = chk[start:start + length]

    # Units
    if b"UNIT" in payloads:
        data = bytearray(payloads[b"UNIT"])
        for off in range(0, len(data) - UNIT_SIZE + 1, UNIT_SIZE):
            if data[off + UNIT_OWNER] == donor:
                data[off + UNIT_OWNER] = into
                stats["units"] += 1
        chk = replace_section(chk, b"UNIT", bytes(data))

    # Sprites
    if b"THG2" in payloads:
        data = bytearray(payloads[b"THG2"])
        for off in range(0, len(data) - THG2_SIZE + 1, THG2_SIZE):
            if data[off + THG2_OWNER] == donor:
                data[off + THG2_OWNER] = into
                stats["sprites"] += 1
        chk = replace_section(chk, b"THG2", bytes(data))

    # Triggers and briefing triggers
    for tag, key in ((b"TRIG", "trig"), (b"MBRF", "mbrf")):
        if tag in payloads and payloads[tag]:
            data, n = _remap_records(payloads[tag], donor, into,
                                     is_briefing=(tag == b"MBRF"))
            stats[key] = n
            chk = replace_section(chk, tag, data)

    # Force membership: FORC's first 8 bytes map player -> force.
    if b"FORC" in payloads and len(payloads[b"FORC"]) >= 8:
        data = bytearray(payloads[b"FORC"])
        if data[donor] != data[into]:
            data[donor] = data[into]
            stats["forces"] += 1
        chk = replace_section(chk, b"FORC", bytes(data))

    # Retire the donor slot so the lobby stops requiring a human for it.
    for tag in (b"OWNR", b"IOWN"):
        if tag in payloads and len(payloads[tag]) > donor:
            data = bytearray(payloads[tag])
            data[donor] = OWNR_INACTIVE
            chk = replace_section(chk, tag, bytes(data))

    return chk, stats


def human_slots(chk: bytes) -> list[int]:
    """1-based player numbers whose slot is an open human slot."""
    for tag, start, length in sections(chk):
        if tag == b"OWNR":
            payload = chk[start:start + length]
            return [i + 1 for i, v in enumerate(payload[:8]) if v == OWNR_HUMAN]
    return []


def computer_slots(chk: bytes) -> list[int]:
    for tag, start, length in sections(chk):
        if tag == b"OWNR":
            payload = chk[start:start + length]
            return [i + 1 for i, v in enumerate(payload[:8]) if v == OWNR_COMPUTER]
    return []


def make_solo(chk: bytes) -> tuple[bytes, dict] | None:
    """Fold every extra human slot into the first one.

    Returns None when the map already has at most one human slot, or when it
    has none at all -- there would be nothing to merge into.
    """
    humans = human_slots(chk)
    if len(humans) < 2:
        return None
    keep, donors = humans[0], humans[1:]
    totals = {"units": 0, "sprites": 0, "trig": 0, "mbrf": 0, "forces": 0,
              "merged": donors}
    for donor in donors:
        chk, stats = merge(chk, donor - 1, keep - 1)
        for key in ("units", "sprites", "trig", "mbrf", "forces"):
            totals[key] += stats[key]
    return chk, totals


def describe_slots(chk: bytes) -> str:
    payload = b""
    for tag, start, length in sections(chk):
        if tag == b"OWNR":
            payload = chk[start:start + length]
    out = []
    for i, value in enumerate(payload[:8]):
        out.append(f"P{i+1}={OWNR_NAMES.get(value, value)}")
    return "  ".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Fold one player's assets into another so a co-op map "
                    "can be played solo.")
    parser.add_argument("map", help="a .scm/.scx produced by this project")
    parser.add_argument("--from", dest="donor", type=int, required=True,
                        help="player number to empty out (1-8)")
    parser.add_argument("--into", type=int, default=1,
                        help="player number that inherits (default 1)")
    parser.add_argument("-o", "--out", default=None,
                        help="output directory (default: alongside the input)")
    args = parser.parse_args(argv)

    if not 1 <= args.donor <= 8 or not 1 <= args.into <= 8:
        print("error: player numbers must be 1-8", file=sys.stderr)
        return 1
    if args.donor == args.into:
        print("error: --from and --into must differ", file=sys.stderr)
        return 1

    chk = MpqReader(args.map).read("staredit\\scenario.chk")
    if not chk or not looks_like_chk(chk):
        print(f"error: no usable scenario in {args.map!r}", file=sys.stderr)
        return 1

    info = parse_map(os.path.basename(args.map), chk)
    print(f"{info.name}  {info.width}x{info.height} {info.tileset_name}")
    print(f"  before: {describe_slots(chk)}")

    merged, stats = merge(chk, args.donor - 1, args.into - 1)
    if not looks_like_chk(merged):
        print("error: the rewritten scenario is malformed; nothing written",
              file=sys.stderr)
        return 1

    print(f"  after : {describe_slots(merged)}")
    print(f"  moved : {stats['units']} units, {stats['sprites']} sprites, "
          f"{stats['trig']} trigger refs, {stats['mbrf']} briefing refs, "
          f"{stats['forces']} force entries")

    out_dir = args.out or os.path.dirname(os.path.abspath(args.map))
    os.makedirs(out_dir, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(args.map))
    dest = os.path.join(out_dir, f"{stem} (solo){ext}")
    with open(dest, "wb") as fh:
        fh.write(build_map_file(merged))
    print(f"\nwrote {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
