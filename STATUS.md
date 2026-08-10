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

### Not verified

**No map has been loaded in StarCraft itself.** This is the one real gap. The
game is installed at `I:\Blizzard\StarCraft` (`StarEdit.exe`, `storm.dll`, and
163 genuine `.scm` + 160 `.scx` under `Maps\`), which arrived late in the
session, so the comparison against real Blizzard maps had only just been
launched when work stopped. Try one map in StarEdit or the game before trusting
all 96.

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

## Open items

1. **Load a map in StarCraft.** The outstanding verification gap. Highest value
   for the least effort.
2. **Compare against genuine Blizzard maps** at `I:\Blizzard\StarCraft\Maps\` —
   block flags, compression method, sector size, hash table size. Confirms or
   corrects the decision above.
3. **Do the SC64 CHKs carry a populated `MBRF` section?** Unchecked. If they do,
   PC-format briefings may already be inside the extracted maps and the text
   scripts are a bonus rather than the only copy. Check this before designing
   any briefing conversion.
4. **Portrait ids.** `<PORTn>` uses 0, 1, 2, 3, 4, 6, 7, 8, 9, 12–22 — no
   `PORT5`, `PORT10` or `PORT11`. What each depicts is not in the scripts. It
   may be inferable by cross-referencing which missions each id speaks in, or
   recoverable from portrait GRPs in the ROM or in `StarDat.mpq`. Treat any
   character-name mapping as inference, not fact, unless proven.
5. **Converting N64 briefings into PC `MBRF` triggers.** Feasibility unknown and
   gated on item 3. Blockers to expect: STR table growth, portrait id mapping,
   and timing values the N64 format does not carry.
6. **Directories 003 and 004** (61 establishing-shot scripts, 13 slideshow
   scripts) are documented but not extracted. Different markup from 007. Neither
   count matches 96, so they sit on a different axis from the mission pairing.
7. **mpyq PR #39** — awaiting maintainer. Upstream is quiet (newest merged PR is
   2020; #35/#36/#37 open). Their CI is `on: [push]`, so fork PRs get no checks.

An analysis workflow covering items 2, 3 and 4 was running when the session
ended and did not report back. Its script is under
`.claude/.../workflows/scripts/sc64-briefings-and-mpq-validation-*.js` and can be
re-run; nothing depends on recovering that particular run.

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
