#!/usr/bin/env python3
"""
extract_briefings.py -- extract the StarCraft 64 mission briefings from a ROM
you own and render them as readable text.

    python extract_briefings.py "StarCraft 64 (USA).n64" -o briefings/

No third-party packages required (standard library only).

This tool ships NO game data. The briefings are Blizzard's copyrighted
dialogue -- keep what it produces for yourself, do not redistribute it.

The N64 build keeps mission briefings as plain-text scripts in BOLT directory
007, entirely separate from the maps in directory 008. They are NOT in the
CHK's MBRF section the way PC campaign briefings are, so they do not travel
with the maps that extract_sc64_maps.py produces -- hence this second tool.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

from extract_sc64_maps import (
    BoltArchive, load_rom, looks_like_chk, parse_map,
    rom_cart_id, rom_internal_name, safe_filename,
)

# --------------------------------------------------------------------------
# Briefing script format (BOLT directory 007)
# --------------------------------------------------------------------------
# Plain printable ASCII with CRLF line endings -- across all 96 scripts there
# is not a single byte outside [0x20..0x7E] plus CR/LF, and not one bare CR or
# LF. Markup is a bare tag alone on its own line:
#
#     <OBJECTIVE>          mission objectives; always the first tag
#     <PORTn>              select portrait n for the transmissions that follow
#     <TEXT>               a transmission: speaker line, blank line, then body
#     <TEXTC>              closing screen text, same internal shape
#
# 1084 of the 1085 tags sit alone on their line; the one exception is a
# <PORT0> appended directly to the end of a prose line in 007/017, so the
# tokeniser scans for tags anywhere rather than matching whole lines.
#
# Briefing 007/i belongs to map 008/(i+8): both runs are 96 long, contiguous
# and in the same order. BRIEFING_TO_MAP_OFFSET encodes that.

BRIEFING_TO_MAP_OFFSET = 8

TAG_RE = re.compile(r"<([A-Za-z0-9_]+)>")
PORT_RE = re.compile(r"^PORT(\d*)$")


@dataclass
class Transmission:
    """One <TEXT> or <TEXTC> block."""
    kind: str                  # "TEXT" or "TEXTC"
    portrait: int | None       # portrait id in force, from the last <PORTn>
    speaker: str
    body: list[str] = field(default_factory=list)


@dataclass
class Briefing:
    bolt_path: str
    map_path: str = ""
    map_name: str = ""
    objectives: list[str] = field(default_factory=list)
    transmissions: list[Transmission] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def portraits(self) -> list[int]:
        seen = []
        for t in self.transmissions:
            if t.portrait is not None and t.portrait not in seen:
                seen.append(t.portrait)
        return seen


def tokenize(text: str):
    """Yield (tag_or_None, lines) in document order.

    A leading chunk with no tag yields tag None. Tags are found anywhere, not
    just at line starts, so the one mid-line tag in the corpus parses cleanly.
    """
    pos = 0
    pending_tag = None
    for match in TAG_RE.finditer(text):
        chunk = text[pos:match.start()]
        if chunk.strip() or pending_tag is not None:
            yield pending_tag, _block_lines(chunk)
        pending_tag = match.group(1)
        pos = match.end()
    yield pending_tag, _block_lines(text[pos:])


def _block_lines(chunk: str) -> list[str]:
    """Split a between-tags chunk into lines.

    Exactly one leading newline is dropped -- the one that ended the tag's own
    line. Any further blank is real content, which matters because a
    transmission with no speaker is written as an *empty* speaker line, and
    collapsing all leading blanks would make it indistinguishable from a
    transmission whose speaker is present.
    """
    lines = chunk.replace("\r\n", "\n").split("\n")
    if lines and lines[0] == "":
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def parse_briefing(bolt_path: str, raw: bytes) -> Briefing:
    briefing = Briefing(bolt_path=bolt_path)
    text = raw.decode("ascii", "replace")
    portrait: int | None = None
    seen_objective = False

    for tag, lines in tokenize(text):
        if tag is None:
            if lines:
                briefing.warnings.append(f"{len(lines)} line(s) before any tag")
            continue

        port = PORT_RE.match(tag)
        if port:
            if port.group(1) == "":
                # 007/033 has one of these. The transmission that follows has
                # an empty speaker line, so a bare <PORT> reads as "nobody in
                # particular is speaking".
                briefing.warnings.append("bare <PORT> with no id")
                portrait = None
            else:
                portrait = int(port.group(1))
            if lines:
                # 007/033 also has a <PORT8> whose <TEXT> tag was never
                # written. The block that follows has the usual speaker/blank/
                # body shape, so treat it as an implicit transmission rather
                # than discarding the dialogue.
                speaker, body = _split_transmission(lines)
                briefing.transmissions.append(
                    Transmission(kind="TEXT", portrait=portrait,
                                 speaker=speaker or "", body=body))
                briefing.warnings.append(
                    f"<{tag}> followed by a transmission with no <TEXT> tag; "
                    f"recovered it")
            continue

        if tag == "OBJECTIVE":
            if seen_objective:
                briefing.warnings.append("more than one <OBJECTIVE>")
            seen_objective = True
            briefing.objectives.extend(lines)
            continue

        if tag in ("TEXT", "TEXTC"):
            if not seen_objective:
                briefing.warnings.append(f"<{tag}> before <OBJECTIVE>")
            speaker, body = _split_transmission(lines)
            if speaker is None:
                briefing.warnings.append(f"<{tag}> block has no speaker line")
                speaker = ""
            briefing.transmissions.append(
                Transmission(kind=tag, portrait=portrait, speaker=speaker, body=body))
            continue

        briefing.warnings.append(f"unknown tag <{tag}>")

    if not seen_objective:
        briefing.warnings.append("no <OBJECTIVE> section")
    return briefing


def _split_transmission(lines: list[str]):
    """A block is 'speaker line, blank line, body'. Tolerate a missing blank."""
    if not lines:
        return None, []
    if len(lines) >= 2 and not lines[1].strip():
        return lines[0].strip(), [l for l in lines[2:]]
    return lines[0].strip(), [l for l in lines[1:]]


# --------------------------------------------------------------------------
# Locating briefings in the ROM
# --------------------------------------------------------------------------

def collect(archive: BoltArchive, verbose: bool = False):
    """Return (briefings, maps_by_index) found in the ROM."""
    scripts: dict[int, tuple[str, bytes]] = {}
    maps: dict[int, tuple[str, object]] = {}

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
            except (ValueError, IndexError) as exc:
                if verbose:
                    print(f"  ! {entry.path}: {exc}", file=sys.stderr)
        elif directory == "008" and head[:4] in (b"TYPE", b"VER ", b"IVER"):
            try:
                data = archive.read(entry)
            except (ValueError, IndexError):
                continue
            if looks_like_chk(data):
                maps[index] = (entry.path, parse_map(entry.path, data))

    briefings = []
    for index in sorted(scripts):
        path, raw = scripts[index]
        briefing = parse_briefing(path, raw)
        paired = maps.get(index + BRIEFING_TO_MAP_OFFSET)
        if paired:
            briefing.map_path, info = paired
            briefing.map_name = info.name
        else:
            briefing.warnings.append("no map paired with this briefing")
        briefings.append((briefing, raw))
    return briefings


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render(briefing: Briefing) -> str:
    out = []
    title = briefing.map_name or "(untitled)"
    out.append(title)
    out.append("=" * len(title))
    out.append(f"briefing {briefing.bolt_path}"
               + (f"   map {briefing.map_path}" if briefing.map_path else ""))
    out.append("")

    if briefing.objectives:
        out.append("OBJECTIVES")
        out.append("-" * 10)
        out.extend("  " + line for line in briefing.objectives)
        out.append("")

    for t in briefing.transmissions:
        port = f"portrait {t.portrait}" if t.portrait is not None else "no portrait"
        label = t.speaker or "(no speaker)"
        if t.kind == "TEXTC":
            out.append(f"[closing screen]")
        else:
            out.append(f"{label}   ({port})")
        out.append("-" * max(len(label), 8))
        out.extend("  " + line for line in t.body)
        out.append("")

    if briefing.warnings:
        out.append("; parser notes: " + "; ".join(briefing.warnings))
    return "\n".join(out).rstrip() + "\n"


def to_dict(briefing: Briefing) -> dict:
    return {
        "bolt_path": briefing.bolt_path,
        "map_path": briefing.map_path,
        "map_name": briefing.map_name,
        "objectives": briefing.objectives,
        "portraits": briefing.portraits,
        "transmissions": [
            {"kind": t.kind, "portrait": t.portrait,
             "speaker": t.speaker, "body": t.body}
            for t in briefing.transmissions
        ],
        "warnings": briefing.warnings,
    }


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract StarCraft 64 mission briefings from a ROM you own.",
        epilog="The briefings are Blizzard's copyrighted content. "
               "Keep them to yourself.",
    )
    parser.add_argument("rom", help="StarCraft 64 ROM (.z64, .v64 or .n64)")
    parser.add_argument("-o", "--out", default="briefings",
                        help="output directory (default: briefings)")
    parser.add_argument("-l", "--list", action="store_true",
                        help="list the briefings and exit without writing")
    parser.add_argument("--raw", action="store_true",
                        help="also write the original script bytes as .script")
    parser.add_argument("--json", action="store_true",
                        help="also write briefings.json with the parsed structure")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        rom = load_rom(args.rom)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    name = rom_internal_name(rom)
    print(f"ROM      : {args.rom}")
    print(f"Internal : {name} [{rom_cart_id(rom)}]  {len(rom) / 2**20:.0f} MiB")
    if "STARCRAFT" not in name.upper():
        print(f"warning  : internal name is {name!r}, not StarCraft 64 -- "
              f"continuing anyway", file=sys.stderr)

    try:
        archive = BoltArchive(rom)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"BOLT     : offset {archive.base:#x}, built {archive.build_stamp}")
    print()

    briefings = collect(archive, args.verbose)
    if not briefings:
        print("error: no briefing scripts found in this ROM", file=sys.stderr)
        return 1

    header = f"{'BOLT':9} {'map':9} {'obj':>3} {'msgs':>4} {'portraits':11}  mission"
    print(header)
    print("-" * len(header))
    for briefing, _ in briefings:
        ports = ",".join(str(p) for p in briefing.portraits[:5])
        if len(briefing.portraits) > 5:
            ports += ",..."
        print(f"{briefing.bolt_path:9} {briefing.map_path or '-':9} "
              f"{len(briefing.objectives):3} {len(briefing.transmissions):4} "
              f"{ports:11}  {briefing.map_name}")

    total = sum(len(b.transmissions) for b, _ in briefings)
    flagged = [b for b, _ in briefings if b.warnings]
    print(f"\n{len(briefings)} briefings, {total} transmissions")
    if flagged:
        print(f"{len(flagged)} with parser notes:")
        for briefing in flagged:
            print(f"  {briefing.bolt_path}: {'; '.join(briefing.warnings)}")

    if args.list:
        return 0

    os.makedirs(args.out, exist_ok=True)
    for briefing, raw in briefings:
        stem = (f"{briefing.map_path.replace('/', '-') or briefing.bolt_path.replace('/', '-')}"
                f" {safe_filename(briefing.map_name or 'Untitled')}")
        with open(os.path.join(args.out, stem + ".txt"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(render(briefing))
        if args.raw:
            with open(os.path.join(args.out, stem + ".script"), "wb") as fh:
                fh.write(raw)

    if args.json:
        payload = [to_dict(b) for b, _ in briefings]
        with open(os.path.join(args.out, "briefings.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    print(f"\nwrote {len(briefings)} briefings to {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
