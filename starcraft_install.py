#!/usr/bin/env python3
"""
starcraft_install.py -- find a PC StarCraft installation and read from it,
whether it is a modern Remastered install or a legacy 1.16.1 one.

    python starcraft_install.py            # search the usual places
    python starcraft_install.py "D:/Games/StarCraft"

The two eras store their data completely differently:

  legacy (1.16.1 and earlier)   StarDat.mpq / BrooDat.mpq, plus loose maps
                                under Maps\\. No CASC.
  Remastered (1.18+)            a CASC storage under Data\\, with the legacy
                                MPQs still present but largely hollowed out --
                                the campaign moved into CASC.

Rather than making callers care, this exposes one interface over both. `read()`
tries CASC first when it exists and falls back to the MPQs, so the same code
works against either era.

Copyright (C) 2026 sc64-maps contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

# Archives worth probing, most complete first. Remastered keeps the legacy
# names around, so presence alone does not tell the eras apart.
KNOWN_ARCHIVES = (
    "StarCraft.mpq", "BroodWar.mpq",      # Remastered-era bulk data
    "StarDat.mpq", "BrooDat.mpq",         # the classic pair
    "patch_rt.mpq", "patch_ed.mpq",
)

# Ordinary install locations, checked in order.
SEARCH_PATHS = (
    r"C:\Program Files (x86)\StarCraft",
    r"C:\Program Files\StarCraft",
    r"C:\StarCraft",
    r"C:\Program Files (x86)\Blizzard\StarCraft",
    r"C:\Program Files\Blizzard\StarCraft",
    r"I:\Blizzard\StarCraft",
    os.path.expanduser("~/StarCraft"),
    "/Applications/StarCraft",
)


class Install:
    """A StarCraft installation of either era."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.maps_dir = self._first_dir("Maps")
        self.archives = [p for p in
                         (os.path.join(self.root, n) for n in KNOWN_ARCHIVES)
                         if os.path.isfile(p)]
        self.has_casc = os.path.isfile(os.path.join(self.root, ".build.info")) \
            and os.path.isdir(os.path.join(self.root, "Data", "config"))
        self._casc = None
        self._mpq: dict[str, object] = {}

    def _first_dir(self, name: str):
        # Windows installs are case-insensitive but this may run elsewhere.
        for candidate in (name, name.lower(), name.upper()):
            path = os.path.join(self.root, candidate)
            if os.path.isdir(path):
                return path
        return None

    # -- identity ---------------------------------------------------------
    @property
    def is_valid(self) -> bool:
        return bool(self.archives or self.has_casc or self.maps_dir)

    @property
    def era(self) -> str:
        if self.has_casc:
            return "Remastered"
        if self.archives:
            return "legacy"
        return "unknown"

    @property
    def version(self) -> str:
        """Version string from .build.info, when the install has one."""
        if not self.has_casc:
            return ""
        try:
            from casc_read import parse_build_info
            rows = parse_build_info(self.root)
            row = next((r for r in rows if r.get("Active") == "1"), rows[0])
            return row.get("Version", "")
        except Exception:
            return ""

    def stock_maps(self) -> list[str]:
        """Every .scm/.scx shipped with the game, excluding our own output."""
        if not self.maps_dir:
            return []
        found = []
        for ext in ("*.scm", "*.scx"):
            found += glob.glob(os.path.join(self.maps_dir, "**", ext), recursive=True)
        return sorted(p for p in found if f"{os.sep}sc64{os.sep}" not in p)

    # -- reading ----------------------------------------------------------
    def read(self, name: str):
        """Read a game file by name, from CASC or the MPQs. None if absent.

        CASC is tried first because on a Remastered install it is the only
        place the campaign still lives.
        """
        if self.has_casc:
            try:
                if self._casc is None:
                    from casc_read import CascStorage
                    self._casc = CascStorage(self.root)
                data = self._casc.read(name)
                if data:
                    return data
            except Exception:
                pass                        # fall through to the MPQs

        from compare_with_stock import StockMpq
        for path in self.archives:
            try:
                if path not in self._mpq:
                    self._mpq[path] = StockMpq(path)
                data = self._mpq[path].read(name)
                if data:
                    return data
            except Exception:
                continue
        return None

    def describe(self) -> str:
        lines = [f"StarCraft install : {self.root}",
                 f"  era             : {self.era}"
                 + (f" ({self.version})" if self.version else "")]
        lines.append(f"  archives        : {len(self.archives)}"
                     + (f"  ({', '.join(os.path.basename(a) for a in self.archives)})"
                        if self.archives else ""))
        lines.append(f"  CASC storage    : {'yes' if self.has_casc else 'no'}")
        maps = self.stock_maps()
        lines.append(f"  Maps folder     : {self.maps_dir or '(none)'}"
                     + (f"  [{len(maps)} stock maps]" if maps else ""))
        return "\n".join(lines)


def find_install(extra: str | None = None) -> Install | None:
    """Return the first plausible installation, or None."""
    candidates = ([extra] if extra else []) + list(SEARCH_PATHS)
    env = os.environ.get("STARCRAFT_DIR")
    if env:
        candidates.insert(0, env)
    for path in candidates:
        if path and os.path.isdir(path):
            install = Install(path)
            if install.is_valid:
                return install
    return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Locate and describe a PC StarCraft installation.")
    parser.add_argument("root", nargs="?", help="install directory")
    args = parser.parse_args(argv)

    install = find_install(args.root)
    if not install:
        print("No StarCraft installation found. Looked in:", file=sys.stderr)
        for path in SEARCH_PATHS:
            print(f"  {path}", file=sys.stderr)
        print("\nPass the directory explicitly, or set STARCRAFT_DIR.",
              file=sys.stderr)
        return 1
    print(install.describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())
