#!/usr/bin/env python3
"""
briefing_to_mbrf.py -- turn a StarCraft 64 briefing script into a PC CHK
``MBRF`` section and inject it into the map it belongs to.

    python briefing_to_mbrf.py "StarCraft 64 (USA).n64" -o maps/

84 of the 96 scenarios in the ROM ship a **zero-length** ``MBRF``, which is
exactly why the extracted maps show no briefing when you launch them.  The
dialogue lives in BOLT directory ``007`` as plain text with no timing, no
portrait unit ids and no ``.wav`` references, so the section has to be built
rather than copied.

Everything this module emits was measured against genuine Blizzard data:

* the 67 enUS campaign ``scenario.chk`` files reachable through
  ``casc_read.py`` (``locales/enUS/Assets/campaign/.../staredit/scenario.chk``)
  -- 67 records, 4288 action slots, 1175 live actions;
* the 12 populated ``MBRF`` sections that survive inside the ROM itself;
* the briefings of the 11 stock UMS maps under ``Maps\\scenario``.

See ``docs/FORMAT.md`` and the module constants below for what each number is
grounded in.

This tool ships NO game data.  Its output is Blizzard's copyrighted dialogue
re-encoded into a map -- keep it for yourself.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from dataclasses import dataclass, field

from extract_sc64_maps import chk_sections, looks_like_chk

# --------------------------------------------------------------------------
# Record layout
# --------------------------------------------------------------------------
# An MBRF record is byte-identical in shape to a TRIG one: 16 conditions of
# 20 bytes, 64 actions of 32 bytes, a u32 of execution flags, a 27-byte
# "executed for player" array and one byte of current-action state.

RECORD_SIZE = 2400
COND_SIZE, COND_COUNT = 20, 16
ACT_SIZE, ACT_COUNT = 32, 64
ACT_BASE = COND_SIZE * COND_COUNT                  # 320
TAIL_BASE = ACT_BASE + ACT_SIZE * ACT_COUNT        # 2368
assert TAIL_BASE + 4 + 27 + 1 == RECORD_SIZE

# Every one of the 67 genuine campaign records, and all 12 in the ROM, carries
# exactly one condition: opcode 13 ("always"), with all other bytes zero.
COND_ALWAYS = 13

# Briefing action opcodes.
ACT_NONE = 0
ACT_WAIT = 1
ACT_PLAY_WAV = 2
ACT_DISPLAY_TEXT = 3
ACT_OBJECTIVES = 4
ACT_SHOW_PORTRAIT = 5
ACT_HIDE_PORTRAIT = 6
ACT_SPEAKING_PORTRAIT = 7
ACT_TRANSMISSION = 8

# Transmission's modifier field.  Measured over 986 genuine campaign
# Transmissions: 974 carry 9, and the dominant whole-record combination --
# 87.7% of them -- is location=0, group2=0, unit=0, modifier=9, flags=0.
#
# The unit field is zero in every single one.  Opcode 8 takes its face from
# the portrait slot named in group1, which an earlier ShowPortrait must have
# filled; it does not carry a unit id of its own.
TRANSMISSION_MODIFIER = 9

# Action flag bits.  0x10 ("unit type field is used") is what the ROM's own
# PC-authored records set on every ShowPortrait, and 109 of the 200 campaign
# ones do too; the other 91 leave it clear and still work, so it is advisory.
FLAG_UNIT_TYPE_USED = 0x10

# Byte 17 of the 27-entry "executed for player" array is *All players*.  Both
# surviving ROM briefings (008/028, 008/02E), the whole Brood War campaign
# (33 records) and the stock (2)Pro Bowl briefing use it.  The original
# StarCraft campaign uses 18 (Force 1) instead, which only works when the
# human happens to be in force 1 -- we do not know that for an arbitrary map.
PLAYER_ALL = 17

# Portrait slots.  The engine has four; Blizzard uses all four freely
# (Show: {0: 56, 1: 46, 2: 48, 3: 50} across the campaign).
PORTRAIT_SLOTS = 4

# --------------------------------------------------------------------------
# Portrait id -> PC unit id
# --------------------------------------------------------------------------
# NOT guessed.  336 lines of SC64 dialogue were fuzzy-matched against the
# genuine campaign MBRF strings and the unit id Blizzard had loaded in the
# speaking slot was read off directly; `n` is how many lines voted.  Where a
# character has more than one plausible unit the vote is listed too -- and in
# every such case the units share a portrait in `arr\units.dat`, so the choice
# is cosmetically moot (units.dat Portrait field, offset 14404, u16[228]):
#     19/20/28 -> portrait 13 (Raynor)   23/29 -> portrait 14 (Duke)
#     99/104   -> portrait 94 (Duran)    147/148 -> portrait 38 (Overmind)
#
#   id  character            unit  stat_txt.tbl name        n   votes
PORTRAIT_UNIT: dict[int, int] = {
    0:  106,   # Advisor              Terran Command Center  23  {106: 23}
    1:  148,   # Zerg Overmind        Zerg Overmind          17  {148: 17}
    2:   87,   # Aldaris              Aldaris                24  {87: 23, 67: 1}
    3:   23,   # Duke                 Edmund Duke             5  {29: 4, 23: 1}
    4:  152,   # Daggoth              Zerg Cerebrate Daggoth  6  {152: 6}
    6:   78,   # Fenix (Dragoon)      Fenix                  14  {78: 14}
    7:   77,   # Fenix (Zealot)       Fenix                   3  {77: 3}
    8:   20,   # Jim Raynor           Jim Raynor             26  {20: 13, 28: 8, 19: 5}
    9:   16,   # Kerrigan             Sarah Kerrigan          4  {16: 4}
    12:  51,   # Infested Kerrigan    Infested Kerrigan      59  {51: 59}
    13:  27,   # Mengsk               Arcturus Mengsk        30  {27: 30}
    14:  79,   # Tassadar             Tassadar               24  {79: 24}
    15: 151,   # Zasz                 Zerg Cerebrate          5  {151: 5}
    16:  75,   # Zeratul              Zeratul                21  {75: 21}
    17:  88,   # Artanis              Artanis                16  {88: 16}   BW only
    18:  98,   # Raszagal             Raszagal               11  {98: 11}   BW only
    19: 100,   # Stukov               Alexei Stukov          10  {100: 10}  BW only
    20: 102,   # DuGalle              Gerard DuGalle         15  {102: 15}  BW only
    21:  99,   # Infested Duran       Samir Duran            23  {99: 16, 104: 7}  BW only
    # 22 is Mr. Slate, an N64-only character with no PC unit.  Deliberately
    # absent: lines under PORT22 are emitted with no portrait rather than with
    # a wrong face.  Only 007/05A uses it (2 blocks), and its map 008/062
    # already carries a PC-authored MBRF, so a default run never hits this.
    # 5, 10 and 11 exist as artwork in the ROM but no script ever selects them.
}

# THE EDITION TRAP.  These unit ids are Brood War characters; in original
# StarCraft the same ids are entirely different units, verified by reading
# rez\stat_txt.tbl out of both BrooDat.mpq and StarDat.mpq:
#     id   BrooDat            StarDat
#     88   Artanis            Merc Biker
#     98   Raszagal           Greedo
#     99   Samir Duran        Boskk
#     100  Alexei Stukov      Peter
#     102  Gerard DuGalle     Unused Neutral 2
#     104  Infested Duran     Unused Neutral 4
# A briefing that uses portraits 17-21 therefore has to land in a .scx.
BROODWAR_ONLY_UNITS = frozenset({88, 98, 99, 100, 102, 104})

# --------------------------------------------------------------------------
# Timing
# --------------------------------------------------------------------------
# The N64 scripts carry no timing whatsoever, so durations are synthesised.
# The model is a least-squares fit of Blizzard's own ms values against the
# very same sentences: 336 SC64 lines matched to campaign transmissions gave
#     t = 73.08 * chars + 517      median |residual| 1238 ms, p90 3653 ms
# (through the origin: 75.7 ms/char, median |residual| 1236 ms).  Blizzard's
# numbers are the recorded .wav lengths, so this is "how long a voice actor
# takes", which is also how long a reader needs.
MS_PER_CHAR = 73.08
MS_BASE = 517
MS_ROUND = 100          # wav-free Blizzard briefings use round hundreds
MS_MIN = 1500
MS_MAX = 45000          # the longest campaign transmission is 52614 ms

# Fixed values lifted verbatim from Blizzard briefings.
MS_OPENING_WAIT = 1000  # after the first ShowPortrait; 136 of 188 campaign Waits
MS_CLOSING_TEXT = 5000  # the "End of Briefing" DisplayText; 64 of 68 campaign ones

# The closing screen.  63 of the 68 campaign DisplayText strings are
# '\r\n\r\n' + 5 spaces + 'End of Briefing[.]'.  The ROM's own two records --
# the ones derived from these very scripts -- use the same shape with 10
# spaces, so that is what we copy; it makes 008/02E byte-identical.  The
# wording itself always comes from the script's <TEXTC> block.
CLOSING_PREFIX = "\r\n\r\n" + " " * 10

STR_CHARSET = "cp1252"
STR_MAX_BYTES = 0x10000   # offsets are u16, so the section cannot reach 64 KiB


def estimate_duration(text: str) -> int:
    """Milliseconds to leave one line of dialogue on screen."""
    ms = MS_BASE + MS_PER_CHAR * len(text)
    ms = int(round(ms / MS_ROUND)) * MS_ROUND
    return max(MS_MIN, min(MS_MAX, ms))


# --------------------------------------------------------------------------
# STR section
# --------------------------------------------------------------------------
# Layout: u16 count, count x u16 offset (from the start of the section), then
# NUL-terminated strings.  All 96 SC64 maps declare 1024 entries and every
# offset points past the header (the smallest is 2050 == 2 + 2*1024), so the
# array itself never has to move.
#
# New strings are given *unreferenced* indices rather than growing the count:
# no shipped map declares anything but 1024, and every SC64 map has at least
# 512 indices that nothing points at.  Only bytes get appended.

_STRING_REF_SECTIONS = (b"SPRP", b"FORC", b"UNIS", b"UNIx", b"SWNM", b"MRGN",
                        b"WAV ", b"TRIG", b"MBRF")


def _referenced_strings(chk: bytes) -> set[int]:
    """Every STR index any section of `chk` points at."""
    sec: dict[bytes, bytes] = {}
    for tag, payload in chk_sections(chk):
        sec[tag] = payload                      # later sections win, as in-game
    ref: set[int] = set()

    payload = sec.get(b"SPRP", b"")
    for i in range(len(payload) // 2):          # scenario name, description
        ref.add(struct.unpack_from("<H", payload, i * 2)[0])

    payload = sec.get(b"FORC", b"")
    if len(payload) >= 16:                      # 4 force names at +8
        for i in range(4):
            ref.add(struct.unpack_from("<H", payload, 8 + i * 2)[0])

    # Custom unit names: a u16[228] at a FIXED offset of 3192 in both UNIS
    # (4048 bytes) and UNIx (4168). It is emphatically NOT the last field --
    # the base-weapon-damage and upgrade-bonus arrays follow it, so deriving
    # the offset from the section length lands 400 / 520 bytes late, inside
    # the weapon tables. Doing that reads damage integers as string indices,
    # misses the real names, and lets the allocator hand a live unit-name slot
    # to briefing dialogue -- which then shows up as a unit's name in game.
    for tag in (b"UNIS", b"UNIx"):
        payload = sec.get(tag, b"")
        base = 3192
        if len(payload) >= base + 228 * 2:
            for i in range(228):
                ref.add(struct.unpack_from("<H", payload, base + i * 2)[0])

    payload = sec.get(b"SWNM", b"")             # 256 switch names, u32 each
    for i in range(len(payload) // 4):
        ref.add(struct.unpack_from("<I", payload, i * 4)[0] & 0xFFFF)

    payload = sec.get(b"MRGN", b"")             # location name at +16 of 20
    for i in range(len(payload) // 20):
        ref.add(struct.unpack_from("<H", payload, i * 20 + 16)[0])

    payload = sec.get(b"WAV ", b"")             # registered wav path indices
    for i in range(len(payload) // 4):
        ref.add(struct.unpack_from("<I", payload, i * 4)[0] & 0xFFFF)

    for tag in (b"TRIG", b"MBRF"):              # action string + wav fields
        payload = sec.get(tag, b"")
        for rec in range(len(payload) // RECORD_SIZE):
            base = rec * RECORD_SIZE + ACT_BASE
            for act in range(ACT_COUNT):
                off = base + act * ACT_SIZE
                ref.add(struct.unpack_from("<I", payload, off + 4)[0] & 0xFFFF)
                ref.add(struct.unpack_from("<I", payload, off + 8)[0] & 0xFFFF)

    ref.discard(0)
    return ref


class StringTable:
    """A CHK ``STR `` section that can take new strings without moving old ones."""

    def __init__(self, payload: bytes, referenced: set[int] | None = None):
        if len(payload) < 2:
            raise ValueError("STR section is too short")
        self.count = struct.unpack_from("<H", payload, 0)[0]
        header = 2 + 2 * self.count
        if header > len(payload):
            raise ValueError(f"STR offset array ({self.count}) overruns the section")
        self.offsets = list(struct.unpack_from("<%dH" % self.count, payload, 2))
        self.data = bytearray(payload[header:])
        self.header_size = header
        self.referenced = set(referenced or range(1, self.count + 1))
        self.added: list[int] = []
        self._by_text: dict[bytes, int] = {}
        for index in range(1, self.count + 1):
            raw = self._raw(index)
            self._by_text.setdefault(raw, index)

    # -- reading -----------------------------------------------------------

    @classmethod
    def from_chk(cls, chk: bytes) -> "StringTable":
        payload = None
        for tag, section in chk_sections(chk):
            if tag == b"STR ":
                payload = section
        if payload is None:
            raise ValueError("CHK has no STR section")
        return cls(payload, _referenced_strings(chk))

    def _raw(self, index: int) -> bytes:
        if not 1 <= index <= self.count:
            return b""
        offset = self.offsets[index - 1] - self.header_size
        if offset < 0 or offset >= len(self.data):
            return b""
        end = self.data.find(b"\x00", offset)
        return bytes(self.data[offset:end if end >= 0 else len(self.data)])

    def get(self, index: int) -> str:
        return self._raw(index).decode(STR_CHARSET, "replace")

    # -- writing -----------------------------------------------------------

    def free_indices(self):
        for index in range(1, self.count + 1):
            if index not in self.referenced:
                yield index

    def add(self, text: str) -> int:
        """Store `text`, returning its 1-based index.  Reuses an identical one."""
        raw = text.encode(STR_CHARSET, "replace")
        existing = self._by_text.get(raw)
        if existing is not None:
            self.referenced.add(existing)
            return existing
        index = next((i for i in self.free_indices()), None)
        if index is None:
            raise ValueError(f"no free STR index (all {self.count} are referenced)")
        offset = self.header_size + len(self.data)
        if offset + len(raw) + 1 > STR_MAX_BYTES:
            raise ValueError("STR section would exceed the 64 KiB u16 offset ceiling")
        self.data += raw + b"\x00"
        self.offsets[index - 1] = offset
        self.referenced.add(index)
        self._by_text[raw] = index
        self.added.append(index)
        return index

    def to_bytes(self) -> bytes:
        out = struct.pack("<H", self.count)
        out += struct.pack("<%dH" % self.count, *self.offsets)
        out += bytes(self.data)
        if len(out) > STR_MAX_BYTES:
            raise ValueError("STR section exceeds the 64 KiB u16 offset ceiling")
        return out


# --------------------------------------------------------------------------
# Action / record assembly
# --------------------------------------------------------------------------

def _action(op: int, *, string: int = 0, wav: int = 0, time: int = 0,
            group1: int = 0, group2: int = 0, unit: int = 0,
            modifier: int = 0, flags: int = 0, location: int = 0) -> bytes:
    """One 32-byte action record."""
    return (struct.pack("<IIIIII", location, string, wav, time, group1, group2)
            + struct.pack("<HBBB", unit, op, modifier, flags)
            + b"\x00\x00\x00")


_EMPTY_ACTION = _action(ACT_NONE)


def _record(actions: list[bytes], player: int = PLAYER_ALL) -> bytes:
    """Pack up to 64 actions into one 2400-byte record."""
    if len(actions) > ACT_COUNT:
        raise ValueError(f"{len(actions)} actions exceeds the {ACT_COUNT} limit")
    out = bytearray(RECORD_SIZE)
    # One "always" condition, everything else zero -- 79/79 genuine records.
    out[15] = COND_ALWAYS
    for i, act in enumerate(actions):
        out[ACT_BASE + i * ACT_SIZE:ACT_BASE + (i + 1) * ACT_SIZE] = act
    for i in range(len(actions), ACT_COUNT):
        out[ACT_BASE + i * ACT_SIZE:ACT_BASE + (i + 1) * ACT_SIZE] = _EMPTY_ACTION
    out[TAIL_BASE + 4 + player] = 1
    return bytes(out)


class _Slots:
    """The four portrait slots, recycled least-recently-spoken first."""

    def __init__(self, count: int = PORTRAIT_SLOTS):
        self.count = count
        self.unit: dict[int, int] = {}      # slot -> unit id
        self.clock = 0
        self.used: dict[int, int] = {}      # slot -> last use
        self.evictions = 0

    def acquire(self, unit: int):
        """Return (slot, show_needed, evicted_slot_or_None)."""
        self.clock += 1
        for slot, held in self.unit.items():
            if held == unit:
                self.used[slot] = self.clock
                return slot, False, None
        for slot in range(self.count):
            if slot not in self.unit:
                self.unit[slot] = unit
                self.used[slot] = self.clock
                return slot, True, None
        victim = min(self.used, key=lambda s: self.used[s])
        self.evictions += 1
        del self.unit[victim]
        self.unit[victim] = unit
        self.used[victim] = self.clock
        return victim, True, victim

    def open_slots(self):
        return sorted(self.unit)


@dataclass
class MbrfBuild:
    """What ``build_mbrf`` produced."""
    mbrf: bytes = b""
    str_table: bytes = b""
    records: int = 0
    actions: int = 0
    lines: int = 0
    strings_added: int = 0
    portrait_units: list = field(default_factory=list)
    slot_evictions: int = 0
    duration_ms: int = 0
    needs_broodwar: bool = False
    warnings: list = field(default_factory=list)

    def __iter__(self):
        # so `mbrf, strtab = build_mbrf(...)` reads naturally
        return iter((self.mbrf, self.str_table))


def build_mbrf(briefing, str_table, *, portrait_unit: dict | None = None,
               player: int = PLAYER_ALL) -> MbrfBuild:
    """Compile one parsed N64 briefing into MBRF records.

    `briefing` is an ``extract_briefings.Briefing``.  `str_table` is either a
    ``StringTable`` or the raw bytes of a ``STR `` section; the returned
    ``MbrfBuild`` carries both the packed records and the rebuilt table.

    The emitted shape:

        MissionObjectives(text)
        ShowPortrait(slot, unit) ; Wait(1000)
        for each line:
            [HidePortrait(victim)] [ShowPortrait(slot, unit)]
            Transmission(slot, text, wav=0, t)   # no portrait: Text ; Wait
        Wait(1000) ; HidePortrait(each open slot) ; DisplayText(closing, 5000)

    Opcode 8 carries the speaking portrait, the text and the hold in one
    action, and it blocks for its own ``time`` -- 273 of the 433 genuine
    campaign transmissions are followed immediately by another one, which
    only works if each holds.  See ``docs/FORMAT.md`` 6.1.

    This used to emit the long-hand ``SpeakingPortrait ; DisplayText ;
    Wait`` instead, because all 433 genuine transmissions carry a .wav and
    derive their duration from it, so the wav-free case was undemonstrated.
    It was then tested directly against StarCraft, with the long-hand
    sequence in the same record as a control, and a ``wav = 0`` transmission
    displays fine.

    Worth knowing, since it argues the other way: where Blizzard themselves
    stripped the wavs -- 008/028 and 008/02E in this very ROM -- they
    disabled every Transmission (flag 0x02) rather than leave it running
    dry.  That is a wav index pointing at audio that is gone, though, which
    is not the same as no wav index at all.  Those two briefings are shipped
    content and are left exactly as they are.
    """
    table = str_table if isinstance(str_table, StringTable) else StringTable(str_table)
    units = PORTRAIT_UNIT if portrait_unit is None else portrait_unit
    build = MbrfBuild()

    if getattr(briefing, "is_stub", False):
        build.warnings.append("placeholder briefing; nothing emitted")
        build.str_table = table.to_bytes()
        return build

    lines = [t for t in briefing.transmissions if t.kind == "TEXT"]
    closers = [t for t in briefing.transmissions if t.kind == "TEXTC"]
    if not lines and not briefing.objectives:
        build.warnings.append("briefing has neither objectives nor dialogue")
        build.str_table = table.to_bytes()
        return build

    groups: list[list[bytes]] = []      # action groups; never split across records
    slots = _Slots()
    used_units: list[int] = []

    text = "\r\n".join(briefing.objectives)          # kept verbatim, as Blizzard does
    if text.strip():
        groups.append([_action(ACT_OBJECTIVES, string=table.add(text))])
    else:
        build.warnings.append("no <OBJECTIVE> text")

    first = True
    for line in lines:
        body = "\r\n".join(line.body).strip()
        if not body:
            build.warnings.append(f"empty body under <{line.kind}>; line dropped")
            continue
        group: list[bytes] = []
        unit = units.get(line.portrait) if line.portrait is not None else None
        if line.portrait is not None and unit is None:
            build.warnings.append(
                f"portrait {line.portrait} has no PC unit; line shown without a face")
        duration = estimate_duration(body)

        slot = None
        if unit is not None:
            slot, show, evicted = slots.acquire(unit)
            if evicted is not None:
                group.append(_action(ACT_HIDE_PORTRAIT, group1=evicted))
            if show:
                group.append(_action(ACT_SHOW_PORTRAIT, group1=slot, unit=unit,
                                     flags=FLAG_UNIT_TYPE_USED))
                if unit not in used_units:
                    used_units.append(unit)
                if first:
                    group.append(_action(ACT_WAIT, time=MS_OPENING_WAIT))
                    build.duration_ms += MS_OPENING_WAIT
        first = False

        if slot is not None:
            # One Transmission carries the speaking portrait, the text and the
            # hold.  It blocks for its own `time`, so nothing waits after it --
            # see the measurement in FORMAT.md 6.1.
            group.append(_action(ACT_TRANSMISSION, group1=slot,
                                 string=table.add(body), wav=0, time=duration,
                                 modifier=TRANSMISSION_MODIFIER))
        else:
            # A line with no portrait has no slot to transmit from, so
            # narration still costs the long-hand pair.
            group.append(_action(ACT_DISPLAY_TEXT, string=table.add(body),
                                 time=duration))
            group.append(_action(ACT_WAIT, time=duration))
        build.duration_ms += duration
        build.lines += 1
        groups.append(group)

    tail: list[bytes] = []
    if slots.open_slots():
        tail.append(_action(ACT_WAIT, time=MS_OPENING_WAIT))
        build.duration_ms += MS_OPENING_WAIT
        for slot in slots.open_slots():
            tail.append(_action(ACT_HIDE_PORTRAIT, group1=slot))
    closing = ""
    if closers:
        parts = [closers[-1].speaker] + list(closers[-1].body)
        closing = "\r\n".join(p for p in parts if p is not None).strip()
    if closing:
        tail.append(_action(ACT_DISPLAY_TEXT, time=MS_CLOSING_TEXT,
                            string=table.add(CLOSING_PREFIX + closing)))
        build.duration_ms += MS_CLOSING_TEXT
    else:
        build.warnings.append("no <TEXTC> closing block")
    if tail:
        groups.append(tail)

    # Pack groups into 64-action records, spilling rather than truncating.
    # Briefing records execute in order -- proven by the shipped
    # Maps\scenario\(2)Pro Bowl.scm, whose 11 records show a portrait in one
    # record and make it speak in the next.
    records: list[bytes] = []
    current: list[bytes] = []
    for group in groups:
        if len(group) > ACT_COUNT:
            raise ValueError("a single line needs more than 64 actions")
        if len(current) + len(group) > ACT_COUNT:
            records.append(_record(current, player))
            current = []
        current += group
    if current:
        records.append(_record(current, player))

    build.mbrf = b"".join(records)
    build.str_table = table.to_bytes()
    build.records = len(records)
    build.actions = sum(len(g) for g in groups)
    build.strings_added = len(table.added)
    build.portrait_units = used_units
    build.slot_evictions = slots.evictions
    build.needs_broodwar = any(u in BROODWAR_ONLY_UNITS for u in used_units)
    return build


# --------------------------------------------------------------------------
# CHK surgery
# --------------------------------------------------------------------------

def _replace_sections(chk: bytes, replacements: dict) -> bytes:
    """Rewrite the payloads of the named sections, keeping the chain intact."""
    out = bytearray()
    pos = 0
    seen = set()
    while pos + 8 <= len(chk):
        tag = chk[pos:pos + 4]
        size = struct.unpack_from("<i", chk, pos + 4)[0]
        payload = chk[pos + 8:pos + 8 + size]
        if tag in replacements:
            payload = replacements[tag]
            seen.add(tag)
        out += tag + struct.pack("<I", len(payload)) + payload
        pos += 8 + size
    missing = set(replacements) - seen
    if missing:
        raise ValueError("CHK is missing section(s): "
                         + ", ".join(t.decode("latin1") for t in sorted(missing)))
    return bytes(out)


def mbrf_length(chk: bytes) -> int:
    """Length of the effective MBRF section, or -1 if there is none."""
    length = -1
    for tag, payload in chk_sections(chk):
        if tag == b"MBRF":
            length = len(payload)
    return length


def mbrf_is_unusable(chk: bytes) -> bool:
    """True if an MBRF has timed actions but not one carries a duration.

    Such a briefing cannot play: every card is dismissed the instant it
    appears, so the whole thing flushes at once. In this ROM exactly one
    section is like that.
    """
    timed = zeroed = 0
    for tag, payload in chk_sections(chk):
        if tag != b"MBRF":
            continue
        for record in range(len(payload) // RECORD_SIZE):
            base = record * RECORD_SIZE + ACT_BASE
            for slot in range(ACT_COUNT):
                off = base + slot * ACT_SIZE
                action_type = payload[off + 26]
                if action_type == ACT_NONE:
                    break
                if action_type in (ACT_WAIT, ACT_PLAY_WAV, ACT_DISPLAY_TEXT,
                                   ACT_SPEAKING_PORTRAIT, ACT_TRANSMISSION):
                    timed += 1
                    if struct.unpack_from("<I", payload, off + 12)[0] == 0:
                        zeroed += 1
    return timed > 0 and timed == zeroed


def patch_zero_durations(chk: bytes) -> tuple[bytes, int]:
    """Fill in missing durations in an existing MBRF, changing nothing else.

    One briefing in the ROM -- Resurrection IV, 008/065 -- ships 24 timed
    actions whose duration is zero, so on PC the whole thing flushes at once
    with nothing readable. The N64 engine paced briefings from the dir-007
    scripts and evidently never consumed these fields.

    Rewriting the section from the script would work but would throw away Mass
    Media's own authoring: their strings, their portrait choices, their action
    order. Patching only the zero durations keeps all of that and fixes the one
    thing that is actually broken.

    A zero-duration DisplayText or Transmission gets a reading time from
    its own string -- both carry the string index at +4; a zero-duration
    Wait or SpeakingPortrait inherits the one it follows, which is what
    actually holds the card on screen.

    Returns (new_chk, actions_patched).
    """
    # Locate the effective MBRF -- the last one, since later sections win.
    offset = size = None
    pos = 0
    while pos + 8 <= len(chk):
        tag = chk[pos:pos + 4]
        length = struct.unpack_from("<i", chk, pos + 4)[0]
        if tag == b"MBRF":
            offset, size = pos, length
        pos += 8 + length
    if offset is None or not size:
        return chk, 0

    mbrf = bytearray(chk[offset + 8:offset + 8 + size])
    strings = StringTable.from_chk(chk)

    patched = 0
    last_ms = MS_MIN
    for record in range(len(mbrf) // RECORD_SIZE):
        base = record * RECORD_SIZE + ACT_BASE
        for slot in range(ACT_COUNT):
            off = base + slot * ACT_SIZE
            action_type = mbrf[off + 26]
            if action_type == 0:
                break
            if action_type not in (ACT_WAIT, ACT_DISPLAY_TEXT,
                                   ACT_SPEAKING_PORTRAIT, ACT_TRANSMISSION):
                continue
            if struct.unpack_from("<I", mbrf, off + 12)[0]:
                continue                      # already timed; leave it alone
            if action_type in (ACT_DISPLAY_TEXT, ACT_TRANSMISSION):
                index = struct.unpack_from("<I", mbrf, off + 4)[0]
                last_ms = estimate_duration(strings.get(index) or "")
            struct.pack_into("<I", mbrf, off + 12, last_ms)
            patched += 1

    if not patched:
        return chk, 0
    return chk[:offset + 8] + bytes(mbrf) + chk[offset + 8 + size:], patched


def inject(chk: bytes, briefing, map_info=None, *, force: bool = False,
           allow_edition_mismatch: bool = False,
           patch_timings: bool = False) -> tuple[bytes, MbrfBuild]:
    """Return (new_chk, build).  `new_chk is chk` when nothing was written.

    Refuses, unless `force`, to overwrite an MBRF that already has content --
    ten of those twelve sections are properly authored and worth keeping.

    The exception is a section that cannot play as it stands: if every timed
    action in it has a zero duration, `patch_timings` fills those in rather
    than either discarding the section or leaving it broken.
    """
    build = MbrfBuild()
    existing = mbrf_length(chk)
    if existing < 0:
        build.warnings.append("no MBRF section to replace")
        return chk, build
    if existing > 0 and not force:
        if not mbrf_is_unusable(chk):
            build.warnings.append(
                f"MBRF already populated ({existing} bytes); kept")
            return chk, build
        # Populated but unplayable on PC: every timed action has a zero
        # duration, so the whole briefing flushes at once with nothing
        # readable. Only Resurrection IV (008/065) is like this.
        #
        # The zero durations are not an oversight. Screenshots of the console
        # briefing screen show a page counter ("1/9") and a Next button: the
        # N64 paced briefings by player input, one page at a time, so there
        # was nothing for a duration field to do. Its single portrait frame,
        # with the speaker's name rendered as text, likewise matches this
        # section driving one slot and swapping the unit id per line. As
        # console data it is coherent and complete.
        #
        # PC has no paged mode -- MBRF is timed-only, with no wait-for-input
        # opcode -- so something has to give either way. Rebuilding from the
        # script is the default because it makes this map consistent with the
        # other 58, which have no authored MBRF to preserve and so are all
        # built the same way. `patch_timings=True` is the faithful
        # alternative: it fills the durations and changes nothing else,
        # keeping Mass Media's one-portrait presentation intact.
        if patch_timings:
            new_chk, patched = patch_zero_durations(chk)
            if patched:
                build.warnings.append(
                    f"MBRF had {patched} zero-duration action(s); timings "
                    f"filled in, rest of the section untouched")
                return new_chk, build
        build.warnings.append(
            f"MBRF was populated but unplayable (every duration zero); "
            f"rebuilt from the briefing script")
    if getattr(briefing, "is_stub", False):
        build.warnings.append("placeholder briefing; MBRF left zero-length")
        return chk, build

    build = build_mbrf(briefing, StringTable.from_chk(chk))
    if not build.mbrf:
        return chk, build

    if build.needs_broodwar and map_info is not None and not map_info.is_broodwar:
        bad = sorted(u for u in build.portrait_units if u in BROODWAR_ONLY_UNITS)
        build.warnings.append(
            f"EDITION CONFLICT: unit id(s) {bad} are Brood War characters but "
            f"the map is {map_info.edition} (VER {map_info.version}, "
            f"TYPE {map_info.type_tag or '-'})")
        if not allow_edition_mismatch:
            build.mbrf = b""
            return chk, build

    new = _replace_sections(chk, {b"MBRF": build.mbrf, b"STR ": build.str_table})
    if not looks_like_chk(new):
        raise ValueError("rewritten CHK no longer parses as a section chain")
    return new, build


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _load(rom_path: str, verbose: bool = False):
    """Return [(MapInfo, chk, Briefing|None)] for every scenario in the ROM."""
    from extract_sc64_maps import BoltArchive, load_rom, parse_map
    from extract_briefings import (BRIEFING_TO_MAP_OFFSET, STUB_MARKER,
                                   parse_briefing)

    archive = BoltArchive(load_rom(rom_path))
    scripts: dict[int, tuple] = {}
    maps: dict[int, tuple] = {}
    for entry in archive.entries():
        directory, _, index_hex = entry.path.partition("/")
        try:
            index = int(index_hex, 16)
        except ValueError:
            continue
        try:
            head = archive.read(entry, limit=4)
        except (ValueError, IndexError):
            continue
        if directory == "007" and head[:1] == b"<":
            try:
                scripts[index] = (entry.path, archive.read(entry))
            except (ValueError, IndexError):
                continue
        elif directory == "008" and head[:4] in (b"TYPE", b"VER ", b"IVER"):
            try:
                data = archive.read(entry)
            except (ValueError, IndexError):
                continue
            if looks_like_chk(data):
                maps[index] = (parse_map(entry.path, data), data)

    out = []
    for index in sorted(maps):
        info, data = maps[index]
        briefing = None
        script = scripts.get(index - BRIEFING_TO_MAP_OFFSET)
        if script:
            briefing = parse_briefing(script[0], script[1])
            briefing.is_stub = STUB_MARKER in script[1]
        elif verbose:
            print(f"  ! {info.bolt_path}: no paired briefing", file=sys.stderr)
        out.append((info, data, briefing))
    return out


def main(argv=None) -> int:
    from extract_sc64_maps import build_map_file, safe_filename

    parser = argparse.ArgumentParser(
        description="Build PC MBRF briefing sections from the StarCraft 64 "
                    "briefing scripts and inject them into the extracted maps.",
        epilog="The output is Blizzard's copyrighted dialogue. Keep it to yourself.",
    )
    parser.add_argument("rom", help="StarCraft 64 ROM (.z64, .v64 or .n64)")
    parser.add_argument("-o", "--out", default="maps",
                        help="output directory (default: maps)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="report what would be built, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="also overwrite the 12 maps that already carry a "
                             "PC-authored MBRF")
    parser.add_argument("--allow-edition-mismatch", action="store_true",
                        help="inject Brood War portrait ids into a vanilla map "
                             "anyway (they render as the wrong unit)")
    parser.add_argument("--chk", action="store_true",
                        help="also write the rewritten .chk alongside each map")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        entries = _load(args.rom, args.verbose)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{len(entries)} scenarios in {args.rom}\n")

    header = (f"{'map':9} {'ext':5} {'rec':>3} {'act':>4} {'ln':>3} {'str':>4} "
              f"{'STR B':>6} {'mm:ss':>6}  name")
    print(header)
    print("-" * len(header))

    written = injected = skipped_stub = skipped_pop = failed = 0
    total_actions = total_strings = total_lines = 0
    largest_str = 0
    largest_str_map = ""
    results = []
    for info, chk, briefing in entries:
        new, build = (chk, MbrfBuild())
        if briefing is not None:
            try:
                new, build = inject(
                    chk, briefing, info, force=args.force,
                    allow_edition_mismatch=args.allow_edition_mismatch)
            except (ValueError, KeyError) as exc:
                build.warnings.append(f"failed: {exc}")
                failed += 1
        changed = new is not chk
        if changed:
            injected += 1
            total_actions += build.actions
            total_strings += build.strings_added
            total_lines += build.lines
        elif briefing is not None and getattr(briefing, "is_stub", False):
            skipped_stub += 1
        elif any("already populated" in w for w in build.warnings):
            skipped_pop += 1

        str_bytes = 0
        for tag, payload in chk_sections(new):
            if tag == b"STR ":
                str_bytes = len(payload)
        if str_bytes > largest_str:
            largest_str, largest_str_map = str_bytes, info.bolt_path

        mm, ss = divmod(build.duration_ms // 1000, 60)
        print(f"{info.bolt_path:9} {info.extension:5} {build.records:3} "
              f"{build.actions:4} {build.lines:3} {build.strings_added:4} "
              f"{str_bytes:6} {mm:3}:{ss:02}  {info.name}"
              + ("" if changed else "   [unchanged]"))
        for warning in build.warnings:
            if "already populated" in warning and not args.verbose:
                continue
            print(f"          ! {warning}")
        results.append((info, new, build, changed))

    print(f"\n{injected} maps got a briefing, {skipped_stub} placeholders left "
          f"zero-length, {skipped_pop} left as shipped, {failed} failed")
    print(f"{total_lines} dialogue lines, {total_actions} actions, "
          f"{total_strings} strings added")
    print(f"largest STR section: {largest_str} bytes ({largest_str_map}), "
          f"ceiling {STR_MAX_BYTES}")

    if args.dry_run:
        return 0

    os.makedirs(args.out, exist_ok=True)
    for info, new, build, changed in results:
        stem = f"{info.bolt_path.replace('/', '-')} {safe_filename(info.name)}"
        with open(os.path.join(args.out, stem + info.extension), "wb") as fh:
            fh.write(build_map_file(new))
        if args.chk:
            with open(os.path.join(args.out, stem + ".chk"), "wb") as fh:
                fh.write(new)
        written += 1
    print(f"\nwrote {written} maps to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
