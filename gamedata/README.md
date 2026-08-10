# gamedata/

Everything in this directory belongs to Blizzard or Nintendo, or is derived
from their work. **None of it is tracked by git and none of it may be
redistributed.** Only this README is committed; `.gitignore` excludes the rest
of the tree wholesale.

The repository ships tools. This directory is what you point them at, and what
they produce. It should exist only on machines whose owner already has the
originals.

```
gamedata/
  roms/         StarCraft 64 cartridge dumps you supply
  installers/   PC StarCraft installers / patch executables you supply
  mpq/          MPQ archives pulled out of those installers
  maps/         extracted scenarios (.scm / .scx)   <- tool output
  briefings/    extracted mission briefings         <- tool output
```

## Why it is laid out this way

Keeping game data in one ignored subtree means a single rule protects it, and
a glance at `git status` is enough to confirm nothing has leaked. The
extension rules in `.gitignore` (`*.n64`, `*.mpq`, `*.scm`, `*.exe`, …) stay in
place as a second line of defence for anything that lands elsewhere.

`reference/` is deliberately *not* here: it holds third-party **open source**
that this project is derived from or checked against — StormLib and CascLib
(MIT), BOLTextract (GPL-3.0), and several mpyq versions (BSD). That code is
tracked on purpose, with provenance and licences recorded in
`reference/README.md`.

## Usage

The tools take paths as arguments, so nothing depends on these locations:

```bash
python extract_sc64_maps.py "gamedata/roms/StarCraft 64 (USA).n64" -o gamedata/maps
python extract_briefings.py "gamedata/roms/StarCraft 64 (USA).n64" -o gamedata/briefings
python verify_maps.py gamedata/maps
```

## Verifying nothing leaked

```bash
git check-ignore -v gamedata/roms/*.n64
git status --short
```

If a game file ever shows as untracked rather than ignored, stop and fix
`.gitignore` before committing.
