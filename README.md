# sc64-maps

Extract every scenario from a **StarCraft 64** cartridge dump and play them in
PC StarCraft — with the mission briefings intact.

96 maps come out, including five that never shipped on PC.

The N64 port stores its maps in the *same* CHK scenario format the PC game
uses, byte for byte, little-endian fields and all. They are just buried under
two layers of packaging: a Mass Media **BOLT** archive with an LZSS-style
compressor, and no MPQ wrapper, because the N64 has no Storm. This peels off
the first layer, adds the second, and puts the briefings back where PC
StarCraft expects to find them.

---

## Quick start

```bash
python sc64.py
```

That is the whole thing. It finds your ROM, extracts the 96 scenarios, compiles
each N64 briefing into its map, verifies the results, and tells you where they
landed. To copy them straight into the game as well:

```bash
python sc64.py --install
```

**You need Python 3.9 or newer. That is the entire dependency list.** No pip
packages, no compiler, no MPQ tool. Put a ROM next to the script (or in
`gamedata/roms/`) and run it.

In game: **Single Player → Custom → Use Map Settings**. Maps that need more than
one human slot skip the briefing screen entirely, so start with a one-player
map such as *Rage*.

---

## What is actually in the cartridge

| | |
|---|---|
| Scenarios | **96** — every campaign mission, 27 melee maps, 2 tutorials, 9 bonus |
| Briefings | **96** — 69 written (549 transmissions), 27 unwritten placeholders |
| Portrait art | **23** bitmaps, 60×56, three of which the game never uses |
| Glue scripts | 61 establishing shots + credits, 13 slideshows |
| Files in the archive | 2111, across 23 directories |

### Exclusive to StarCraft 64

These five appear nowhere in a PC install — not in `arr\mapdata.tbl`,
`rez\stat_txt.tbl`, any MPQ listfile, or the scenario name of any installed map.
All carry real authored briefings, unlike the melee maps.

| Map | Size | Tileset | Players | Edition | Briefing |
|---|---|---|---|---|---|
| **Guardians** | 128×128 | Jungle | 2 | StarCraft | 2 msgs |
| **Zerg Troopers** | 96×64 | Badlands | 6 | StarCraft (hybrid) | 5 msgs |
| **Resurrection IV** | 96×192 | Arctic | 4 | Brood War | 14 msgs |
| **Rage** | 96×96 | Badlands | 8 | Brood War | 7 msgs |
| **Mass Hysteria** | 128×128 | Installation | 2 | Brood War | 2 msgs |

Plus a likely sixth: **Tutorial 2**. PC's `arr\mapdata.tbl` has exactly one
tutorial slot; the cartridge ships two, and the second appears in no PC data.

### PC maps that Mass Media wrote briefings for

These exist on PC as plain melee or scenario maps with no briefing at all. The
N64 versions have one, written for the console release.

| Map | PC counterpart | Briefing |
|---|---|---|
| Pro Bowl | `(2)Pro Bowl.scm` | 8 msgs |
| Round-Up | `(4)Zergling Round-Up.scm` | 5 msgs |
| King of the Hill | `(4)King of the Hill.scm` | 2 msgs |
| Old Faithful | `(4)Old Faithful.scm` | 3 msgs |
| Tutorial 1 | `campaign\terran\tutorial` | 3 msgs |

### One map that looked exclusive and is not

`008/043` is untitled, 96×96 Brood War, and sits outside all six campaigns. It
is **Brood War's secret bonus mission** — CASC asset path
`campaign/EXPZerg/Bonus`, ROM title-table entry *Dark Origin*, internally "Zerg
Level 9B". Four independent witnesses agree, including the map's own triggers,
which branch in from `008/041` under a switch its designer named `DisableBonus`.
The N64 version keeps the PC terrain byte-for-byte and re-authors units and
triggers.

Before trusting scenario names as identifiers: **45 of the 60 campaign CHK
names are dev-era working titles** that differ from what the N64 displays, and
six carry StarEdit's default "Untitled Scenario". The real titles live in a
60-entry table in the ROM at `0x0D1010`.

### The briefings

On PC, campaign briefings live inside the map as `MBRF` trigger records. The
N64 build pulled them out into plain-text scripts in their own archive
directory, which is why they do not travel with the maps and why **84 of the 96
scenarios ship a zero-length `MBRF`** — nothing to display.

`sc64.py` compiles them back in. 58 maps gain a briefing; the rest are left
alone, because 27 pair with unwritten placeholders and 11 already carry a
briefing Blizzard authored, the only PC-side briefing data in the cartridge.

The speaker's name is a literal line in each script, so every `<PORTn>` id
resolves to a character by measurement rather than guesswork:

| id | Character | | id | Character | | id | Character |
|---|---|---|---|---|---|---|---|
| 0 | Advisor | | 8 | Jim Raynor | | 17 | Artanis |
| 1 | Zerg Overmind | | 9 | Kerrigan | | 18 | Raszagal |
| 2 | Aldaris | | 12 | Infested Kerrigan | | 19 | Stukov |
| 3 | General Duke | | 13 | Mengsk | | 20 | DuGalle |
| 4 | Daggoth | | 14 | Tassadar | | 21 | Infested Duran |
| 6 | Fenix (Dragoon) | | 15 | Zasz | | 22 | Mr. Slate |
| 7 | Fenix (Zealot) | | 16 | Zeratul | | | |

Ids 5, 10 and 11 have finished artwork in the cartridge that no script ever
uses. 10 and 11 are a near-identical pair of pale crystals, most likely Uraj
and Khalis; 5 is an unidentified Zerg portrait.

---

## Faithfulness

Where the N64 maps overlap with maps still shipping on PC, they are close to
identical. Of the 22 that match by name:

- **18 have byte-identical terrain.** The other four differ by 2 to 192 tiles
  out of ~16,000.
- **Unit data differs in exactly one field of sixteen.** Across 1,735 differing
  records the only thing that ever changes is the 4-byte serial (class instance
  id). Position, type, owner, hit points, resources, flags and unit links are
  identical in every record.
- Triggers were rewritten, as you would expect from a console port.

The two published community recreations of the exclusives are, by contrast,
hand-rebuilt.
[Resurrection IV](https://staredit.net/sc1db/file/4856/) reproduces the shell
exactly — 96×192, Arctic, `VER` 205/`RAWB` — but only **6.5% of terrain tiles
match** and 13 strings are shared.
[Wanna Be Zerg Troopers](http://staredit.net/sc1db/file/3526/) diverges further
still: 128×128 against 96×64, 438 units against 272. Neither is ROM-derived.

---

## Supported ROMs and StarCraft versions

All four known cartridge releases work. `python sc64.py --roms` lists what it
found; `--all-roms` processes every one into its own folder.

| Variant | Notes |
|---|---|
| **USA (retail)** | the default when several are present |
| Australia / PAL | BOLT archive byte-identical to USA retail |
| Germany | a *later* build than retail (2000-06-05) — German text, retuned triggers, a few terrain edits |
| USA (beta) | six weeks before retail; identical terrain, but 25 scenarios differ in units, strings and triggers |

Any of `.z64`, `.v64` or `.n64` works — the header magic decides, not the
extension. The widely circulated `StarCraft 64 (USA).n64` is actually
byte-swapped `v64` data.

On the PC side, both eras are supported. `starcraft_install.py` finds the game
and reads from it whichever way it stores data:

```bash
python starcraft_install.py
```

| Era | Data layout |
|---|---|
| **1.16.1 and earlier** | `StarDat.mpq` / `BrooDat.mpq` plus loose maps |
| **Remastered (1.18+)** | a CASC storage under `Data\`, campaign included |

Set `STARCRAFT_DIR` or pass `--starcraft DIR` if it is somewhere unusual.

---

## Doing it by hand

`sc64.py` just chains these; each works on its own.

```bash
python extract_sc64_maps.py ROM --briefings -o maps/   # maps, briefings inside
python extract_briefings.py ROM -o briefings/          # briefings as text
python extract_glue.py      ROM -o glue/               # establishing shots
python verify_maps.py maps/                            # check they load
python compare_with_stock.py ROM --stock "C:/StarCraft"  # diff against PC
python casc_read.py "C:/StarCraft" --list "*campaign*"   # read a CASC install
```

Useful flags: `--list` anywhere shows contents without writing; `--chk` keeps
raw scenario chunks; `--json` gives parsed structure; `--dump-all DIR` extracts
all 2111 archive files.

---

## How it works

```
ROM (.z64/.v64/.n64)
  └─ normalise byte order to big-endian z64
     └─ find the embedded BOLT archive
        └─ walk its directory tree, LZSS-decompress each entry
           └─ keep entries that are a well-formed CHK section chain
              └─ compile the paired briefing script into an MBRF section
                 └─ wrap in a minimal MPQ as staredit\scenario.chk
                    └─ .scm / .scx
```

The MPQ writer emits format version 1 with a hash table, block table and sector
offset table built from scratch, including Storm's crypt table and the table
encryption. Sectors are stored verbatim: a reader only decompresses a sector
when its stored length is *shorter* than its plain length, which is the same
path Storm takes for any sector that failed to compress. That makes it the most
portable option and means no compressor has to ship.

Briefing timing is the one synthesised part — the N64 format carries none — and
is fitted against Blizzard's own recorded `.wav` lengths for the same sentences
(`t = 73.08 × characters + 517` ms). Everything else is copied from genuine
campaign briefings rather than invented.

See [docs/FORMAT.md](docs/FORMAT.md) for the formats and
[reference/](reference/) for the prior art.

---

## Verification

- **It runs in the real game.** A generated map opens in **StarEdit** and a
  briefing plays in **StarCraft.exe** — terrain, units, force names, triggers,
  portrait and objectives all correct.
- **`verify_maps.py` passes 96/96**, using an MPQ reader written to match
  StormLib's semantics exactly.
- **Measured against the 323 maps shipped with a retail install.** Format
  version, header size, sector size and archive geometry match all 323. Where
  this project differs — uncompressed, unencrypted, a 16-slot hash table — the
  choice is legal per StormLib *and* precedented in shipped content: verbatim
  sectors occur in Blizzard's own `(3)Triad.scm` and `(4)Inferno.scm`, and four
  genuine ladder maps ship an unencrypted `scenario.chk`.
- **Extraction is cross-checked two ways.** Every one of the archive's 2111
  entries decompresses to exactly its declared length, and an independent
  rewrite in `reference/` finds the same 96 scenarios.
- **`check_string_reuse.py`** guards briefing injection against overwriting a
  string some other section still owns.

A note if you try to check the output with **mpyq**: it will fail, and the maps
are fine — the bug is mpyq's, and the fix is
[eagleflo/mpyq#39](https://github.com/eagleflo/mpyq/pull/39). See
[reference/README.md](reference/README.md).

---

## Legal

**This repository contains no game data, and you must not redistribute what it
produces.** The maps, briefings and artwork are Blizzard's copyrighted work.
This is a tool for people who already own a StarCraft 64 cartridge and a copy of
PC StarCraft. You supply the ROM; nothing is downloaded.

Everything the tools read or write lives under `gamedata/`, which is ignored by
git in its entirety — see [gamedata/README.md](gamedata/README.md).

The code is **GPL-3.0-or-later**, because the BOLT container walk and
decompressor derive from
[BOLTextract](https://github.com/heinermann/BOLTextract), which is GPL-3.0.

---

## Layout

```
sc64.py                  one command: ROM -> playable maps with briefings
starcraft_install.py     find a PC StarCraft install, legacy or Remastered

extract_sc64_maps.py     ROM -> .scm/.scx  (--briefings compiles MBRF in)
extract_briefings.py     ROM -> briefings as readable text
extract_glue.py          ROM -> establishing-shot and slideshow scripts
briefing_to_mbrf.py      briefing script -> PC MBRF trigger records

patch_scenario.py        read/edit the melee Scenario list in the ROM
n64crc.py                detect and repair the N64 boot checksum

verify_maps.py           StormLib-faithful validator for the output
check_string_reuse.py    regression guard for briefing injection
compare_with_stock.py    diff the output against an installed PC StarCraft
casc_read.py             read a CASC storage (Remastered and later)
mpq_keycrack.py          read MPQ files whose name is unknown
pkware_explode.py        PKWARE DCL, for reading genuine maps

docs/FORMAT.md           BOLT, CHK, MPQ, CASC and briefing-script notes
reference/               vendored prior art, with provenance and licences
gamedata/                everything game-derived; gitignored wholesale
```

## Credits

- [Adam Heinermann](https://github.com/heinermann) — reverse-engineered the
  BOLT format and its N64 compressor, which is the hard part of this problem.
- [Ladislav Zezula](https://github.com/ladislav-zezula) — StormLib and CascLib,
  the reference for everything MPQ and CASC.
- The [staredit.net](https://staredit.net/topic/18209/) threads where the format
  was worked out in the open.
