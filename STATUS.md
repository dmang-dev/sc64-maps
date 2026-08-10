# Status / handoff

Written 2026-08-09. Snapshot of where this project stands, what is proven, and
what is still open — enough for a fresh session to pick up without re-deriving
anything.

## Where things stand

Three commits, clean tree, no remote. Everything below is verified unless it
says otherwise.

| | |
|---|---|
| Maps extracted | 96 (59 vanilla, 6 hybrid, 31 Brood War) |
| Maps verified | 96/96 via `verify_maps.py` |
| Briefings extracted | 96, 576 transmissions |
| BOLT entries decompressed | 2111/2111, every one exactly its declared length |
| Upstream PR | [eagleflo/mpyq#39](https://github.com/eagleflo/mpyq/pull/39), open |

```bash
python extract_sc64_maps.py "StarCraft 64 (USA).n64" -o maps/
python verify_maps.py maps/
python extract_briefings.py "StarCraft 64 (USA).n64" -o briefings/
```

Nothing but CPython 3.9+ is required.

## Facts worth not re-deriving

**The ROM.** `StarCraft 64 (USA).n64` is **v64** (byte-swapped) data despite the
extension — header magic `37 80 40 12`. Detect by magic, never by extension.
Internal name `STARCRAFT 64`, cart id `NSQE`, 32 MiB.

**BOLT archive** at `0x12CA10`, built 1999-11-08 14:13:43, 2111 files in 23
directories, two levels deep. All offsets are relative to the `BOLT` magic.
Filenames are not recoverable — only hashes are stored, and that was chased and
abandoned upstream.

**Maps** are in directory `008`, indices `0x008`–`0x067`, contiguous. Directory
008 holds 176 entries total, so the first 8 are something else.

**Briefings** are in directory `007`, indices `0x000`–`0x05F`. That directory
holds **119** entries: the 96 scripts have `file_type` 10 and start with `<`;
the other 23 are unrelated binaries. Filtering on directory alone mixes them and
produces a byte histogram that makes the scripts look binary — they are not.

**Pairing** is `007/i` ↔ `008/(i+8)`, asserted across all 96.

**The reason any of this works:** the N64 build stores CHK scenarios
byte-identical to the PC format, little-endian integer fields included. No data
conversion is needed — the maps only had to be unpacked from BOLT and rewrapped
in an MPQ.

**Briefings are not in the maps.** PC StarCraft keeps campaign briefings in the
CHK's `MBRF` section; the N64 build keeps them as separate text scripts. That is
why they need their own tool.

See `docs/FORMAT.md` for the full format writeups (BOLT container and its LZSS
variant, CHK, MPQ, and the briefing script grammar including its two
data-losing edge cases).

## What is actually verified, and how

- **BOLT decompression** — all 2111 entries decode to exactly the byte count
  their header declares. Independently cross-checked by
  `reference/bolt_extract_all.py`, a separate rewrite, which finds the same 96
  CHKs.
- **MPQ output** — `verify_maps.py` implements StormLib's semantics (read from
  `reference/StormLib/src/SFileReadFile.cpp`: sector offset table consulted only
  when a compression flag is set, line 56; plain size is
  `min(sector_size, remaining)`, lines 108–121; decompress only when stored size
  is *strictly less*, line 165). 96/96 pass.
- **MPQ output, second opinion** — with the sector fix applied, mpyq reads all
  96 and the recovered `staredit\scenario.chk` is byte-identical to the CHK
  taken from the ROM. Without the fix, all 96 fail.
- **The mpyq fix regresses nothing** — stock vs patched extract all 52 files
  from upstream's own fixtures identically, including
  `test/last_sector_compression.s2ma`, added by PR #26 for the last-sector case.

- **Measured against 323 genuine maps** from the retail install. Format
  version, header size, sector size and archive geometry match in all 323. All
  96 of ours also pass a full emulation of StormLib's acceptance checks. The
  places we differ are legal *and* precedented in shipped content — see
  `docs/FORMAT.md` §4.2.

- **Diffed against the stock PC maps.** Of the 22 N64 scenarios that also ship
  with PC StarCraft, 18 have byte-identical terrain, and the only unit field
  that ever differs is the serial (class instance id) — position, type, owner,
  hit points, resources, flags and links match in every record. See
  `docs/FORMAT.md` §4.4. Campaign missions are unreachable: they live in the
  CASC store, not the legacy MPQs.
- **Opened in Blizzard's own editor.** `008-00A T1) Wasteland.scm` loads in
  StarEdit with no errors — the retail `storm.dll` path, not a
  reimplementation. Terrain, 49 units, player types, custom force names
  resolved from the string table, and the trigger list all read correctly.
  This is what finally settles the MPQ-format question.

### Not verified

Playing a map through to completion in `StarCraft.exe`. Loading is confirmed
by StarEdit, and gameplay uses the same CHK data, so the remaining risk is
about mission design (N64 balance tweaks, unit counts) rather than file format.

## Design decision worth understanding

Files are written with `MPQ_FILE_EXISTS | MPQ_FILE_COMPRESS` (`0x80000200`),
sector shift 3 (4096-byte sectors), a sector offset table, and **sectors stored
verbatim**.

The `COMPRESS` flag is what makes a reader consult the sector offset table at
all. The sectors themselves are uncompressed, which is legal and is exactly the
path Storm takes for any sector that failed to compress — so it is universally
supported and no compressor has to be shipped.

This was chosen over the alternatives deliberately: setting no compression flag
also works in StormLib but skips the sector table and exercises a much rarer
path, and actually compressing would have meant either zlib (uncertain support
in StarCraft-era Storm) or implementing PKWARE DCL. **The pending real-map
comparison may argue for changing this** — if genuine maps turn out to use
implode universally, that is worth reconsidering.

## Resolved since the first draft

- **Real-map comparison — done.** Our format choice is validated; see
  `docs/FORMAT.md` §4.2. No change needed.
- **Does SC64 already carry PC briefings? — No.** All 96 CHKs have an `MBRF`
  section header, but 84 are zero-length. Only 12 are populated and just 10 are
  usable. So the briefings genuinely had to be extracted separately, and a
  converter would need to *build* 67 of them. Usefully, the surviving 10 contain
  the same dialogue as their paired 007 script, which makes them a ready-made
  regression corpus for any converter — and proves the scripts are a
  re-encoding of the PC briefings rather than N64-original text.
- **Portrait ids — solved, by measurement.** The speaker's name is a literal
  line in each `<TEXT>` block, so no inference was needed. Table in
  `docs/FORMAT.md` §5.2. The artwork is at BOLT `007/(0x60+n)`.
- **Two bugs found and fixed in this project's own code:** `verify_maps.py`
  rejected every genuine map (hard `dwArchiveSize` equality; no masking of
  inflated hash table sizes) — now 40/40 sampled real maps parse. And the
  briefing parser dropped an unclosed `<PORT12` in 007/025, silently giving the
  following dialogue the wrong speaker's portrait.

## Open items

1. **Converting N64 briefings into PC `MBRF` triggers.** Feasible and fully
   specified — `MBRF` records are byte-identical to `TRIG` (2400 bytes), the
   action opcodes and field layout are known, and the string budget is a
   non-issue (largest resulting `STR` is 18 KB against a 64 KB ceiling). Two
   real blockers: the N64 format carries **no timing information at all**, so
   every duration must be synthesised; and five portraits map to unit ids that
   only exist in Brood War, so any briefing using ids 17–21 must be written as
   a `.scx`. Validate a converter by regenerating the 10 surviving originals.
2. **Play a map through in `StarCraft.exe`.** Loading is settled; this is about
   mission design rather than file format.
3. **Directories 003 and 004** (61 establishing-shot scripts, 13 slideshow
   scripts) are documented but not extracted. Different markup from 007, and
   003 is cp1252 rather than ASCII.
4. **mpyq PR #39** — awaiting maintainer. Upstream is quiet (newest merged PR is
   2020; #35/#36/#37 open). Their CI is `on: [push]`, so fork PRs get no checks.
5. **Optional polish:** a PKWARE implode compressor would cut output size ~3.9×
   and make our maps byte-level conventional. Decoders exist; no encoder does.

## Resolved: the campaign maps are reachable now

Two independent pipelines both reach them, and they cross-check each other.

- **`mpq_keycrack.py`** recovers an MPQ file's key from content rather than its
  filename (StormLib's `DetectFileKeyBySectorSize`). 4,732 of 4,746 encrypted
  blocks corpus-wide, 99.7%. The 14 failures are 2-byte uncompressed stubs with
  no sector offset table to attack — the whole failure class, and unfixable by
  this technique.
- **`casc_read.py`** completes the local CASC path and decodes all 21,161
  locally-present root names with zero failures and zero MD5 mismatches.

Correcting the earlier note: the campaign is **not** in `StarDat.mpq` or
`BrooDat.mpq` — 3,826 blocks there, zero CHKs. It is 68 nameless blocks in the
two ~480 MiB installer archives, listed by no listfile anywhere. All 68 are
MTXM-identical to the 68 enUS campaign files reached via CASC, which shares no
code with the MPQ path.

That unblocks the highest-fidelity route for the briefing conversion: original
`MBRF` records can now be read verbatim rather than synthesised.

## Resolved: 008/043 is "Dark Origin"

The untitled Brood War map is the **secret bonus mission** — CASC asset path
`campaign/EXPZerg/Bonus/staredit/scenario.chk`, ROM title-table entry 59 "Dark
Origin", internally "Zerg Level 9B". Four independent witnesses agree: the CASC
asset path, a cracked `BroodWar.mpq` block, the map's own triggers (which name
`Bonus.scx` and branch from 008/041 under a `DisableBonus` switch), and a
60-entry mission-title block in the ROM at `0x0D1010`.

The earlier "variant of *The Insurgent*" guess is refuted: 96×96 Arctic vs
128×128 Twilight, best positional terrain agreement 1.37% (chance level), 4
shared unit ids, 4 shared strings of which 3 are boilerplate. The resemblance
was entirely a shared Zeratul portrait, which 11 of the 96 briefings use.

The N64 version keeps the PC terrain byte-for-byte and re-authors units and
triggers — so it is *not* byte-identical to PC `Bonus.scx`.

The other two untitled maps are ordinary campaign missions carrying StarEdit's
default name: 008/039 = `EXPZerg/Zerg01` ("Vile Disruption"), 008/03A =
`EXPZerg/Zerg02` ("Reign of Fire"). Six of the 96 CHKs carry the default name,
and 45 of the 60 campaign CHK names are dev-era working titles that differ from
what the N64 displays — **the CHK name field is not a reliable identifier for
this ROM.**

## Legal

The repository contains **no game data** and must not. `.gitignore` excludes
ROMs (`*.n64/.z64/.v64`), map output (`*.scm/.scx/.chk`, `maps/`) and briefing
output (`briefings/`, `*.script`, `briefings.json`). Verify with
`git check-ignore` before committing if that list changes.

Licence is **GPL-3.0-or-later**, inherited: the BOLT container walk and
decompressor derive from [BOLTextract](https://github.com/heinermann/BOLTextract).
Vendored StormLib is MIT; vendored mpyq is BSD. The MPQ writer and the briefing
parser are original to this project.

Extracted maps and briefings are Blizzard's copyrighted work. The tools are
publishable; their output is not redistributable.
