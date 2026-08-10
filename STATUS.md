# Status / handoff

Snapshot of where this project stands, what is proven and by what method, and
what is still open — enough for a fresh session to pick up without re-deriving
anything. The README is the user-facing document; this one is for whoever works
on the code next.

## Where things stand

Clean tree, no remote. Everything below is verified unless it says otherwise.

| | |
|---|---|
| Scenarios extracted | 96 |
| Briefings compiled into maps | 58 of 96 (27 placeholders, 11 already authored) |
| `verify_maps.py` | 96/96 |
| Loads in StarEdit | yes |
| **Briefing plays in `StarCraft.exe`** | **yes** (Blizzard-authored MBRF, on *Rage*) |
| BOLT entries decompressed | 2111/2111, each exactly its declared length |
| Upstream PR | [eagleflo/mpyq#39](https://github.com/eagleflo/mpyq/pull/39), open |

```bash
python sc64.py                 # everything, with sensible defaults
python sc64.py --install       # and copy into the game
python sc64.py --roms          # what ROMs it can see
```

## Facts worth not re-deriving

**ROMs.** All four releases work. USA and Australia have byte-identical BOLT
archives. The "Germany (Proto)" cart is *not* a prototype — its archive is
stamped 2000-06-05, seven months after retail, so it is a late localisation.
The USA beta (1999-09-29) is the genuine early build, and notably has **zero
terrain differences** from retail: only units, strings and triggers moved in
those last six weeks. The beta's ROM header is unfinished, so its internal name
field is not ASCII; that is expected, not corruption.

**BOLT.** Archive at `0x12CA10` in the USA ROM, 2111 files, 23 directories, two
levels deep. Offsets are relative to the `BOLT` magic. Filenames are not
recoverable — only hashes, and that was chased and abandoned upstream.
`BoltEntry.file_hash` is a reliable "definitely unchanged" filter but a poor
change detector, and it is **not unique** — never use it as a dict key.

**Directory map.** `008/008`–`008/067` are the 96 scenarios. `007/000`–`007/05F`
are the 96 briefing scripts and `007/060`–`007/076` are the 23 portrait
bitmaps. `003` is 61 establishing-shot scripts plus the credits, `004` is 13
slideshows; both are followed by binary assets. Do **not** select scripts by
`file_type` — `0x0A` covers both scripts and 48-byte font ramps.

**Pairing.** `003/i ↔ 007/i ↔ 008/(i+8)`, covering the 60 campaign maps.

**Why any of this works.** The N64 build stores CHK scenarios byte-identical to
the PC format, little-endian fields included. No data conversion is needed.

**Names are unreliable.** 45 of the 60 campaign CHK names are dev-era working
titles; six carry StarEdit's default. Real titles are in a ROM table at
`0x0D1010`.

See `docs/FORMAT.md` for the formats themselves.

## What is verified, and how

- **In-engine.** A generated map opens in StarEdit; a briefing renders correctly
  in `StarCraft.exe`. The portrait that displayed matched the `PORTn` table
  derived independently from the N64 text scripts — a genuine cross-check.
- **Against 323 retail maps.** Archive geometry matches all 323. The places this
  project differs are legal per StormLib *and* precedented in shipped content.
- **Two independent pipelines agree.** All 68 nameless campaign blocks recovered
  by `mpq_keycrack.py` are terrain-identical to the 68 campaign files reached
  through `casc_read.py`, which shares no code with the MPQ path.
- **Briefing conversion against ground truth.** Two maps kept both a
  Blizzard-authored `MBRF` and an N64 script; regenerating them reproduces 7 of
  8 and 7 of 12 strings byte-for-byte, objectives byte-identical. The misses are
  places where the ROM's own two copies disagree.
- **mpyq's sector bug** is fixed and the fix regresses nothing on upstream's own
  fixtures.

### Not verified

- **No *generated* briefing has been watched playing.** *Rage* proved the format
  works, but that was Blizzard's own `MBRF`. Try a converted one — *Guardians*
  is single-player-friendly with 2 transmissions. This is the cheapest
  remaining risk to close.
- **Timing is synthesised**, since the N64 format carries none. Fitted over 336
  lines matched to genuine campaign transmissions: `t = 73.08·chars + 517` ms,
  median residual ~1.2 s, p90 ~3.6 s. A mistimed line reads as a pause or a
  clipped card, not a crash.
- LZ4, recursive and Salsa20 BLTE frames, the scattered-guarded-block `.idx`
  variant, index revision 5, and online CASC storages are **unimplemented and
  have never executed**. None occur in this build.

## A bug class worth remembering

The briefing injector allocated new `STR` entries, so it had to know which were
already in use. It located the custom-unit-name `u16[228]` array in
`UNIS`/`UNIx` by measuring back from the section end — but that array is not the
last field; weapon-damage and upgrade arrays follow it. The read landed 400–520
bytes late, missed every real unit name, and handed live slots to briefing
dialogue. **12 unit names across 8 maps were overwritten** and would have shown
dialogue as a unit's name in game.

The offset is a fixed **3192**. It survived testing because the bundled
validator computed its reference set by calling the same buggy function — a
circular check that could only agree with itself. `check_string_reuse.py` exists
because of this: it hardcodes its own offset and re-reads originals from the
ROM, sharing no code with the injector. **Keep it that way.**

## Open items

1. **Watch a generated briefing play.** The one gap between "structurally
   indistinguishable from shipped data" and "it works".
2. **Settle wav-less opcode 8.** All 433 genuine campaign Transmissions carry a
   wav index, so the converter uses Blizzard's wav-free sequence instead. If a
   wav-less Transmission does play, each line drops from 3 actions to 1 and
   matches the campaign opcode sequence exactly. Needs the engine to decide.
3. **`docs/FORMAT.md` has no CASC section** and only a short one on `MBRF`.
4. **mpyq PR #39** — awaiting upstream. It has been quiet since 2020.
5. **Optional:** a PKWARE implode compressor would cut output size ~3.9× and
   make the maps byte-level conventional. Decoders exist; no encoder does.
6. **Unowned questions:** what binds a dir-004 slideshow to a campaign point (no
   mission id or ordering field exists in the scripts, so the dispatch must be
   in game code); the RGBA5551 palette quantiser is unidentified; slide 3 is
   referenced by nothing; the Terran mission numbering gap at 7 and 10 is
   unexplained.

## Legal

The repository contains **no game data** and must not. `gamedata/` is ignored
wholesale, with extension rules as a second line of defence. Verify with
`git check-ignore` before committing if that list changes.

Licence is **GPL-3.0-or-later**, inherited from BOLTextract. Vendored StormLib
and CascLib are MIT; vendored mpyq is BSD. The MPQ writer, CHK handling, CASC
reader, key crack and briefing compiler are original to this project.
