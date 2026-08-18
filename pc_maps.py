"""Read a scenario out of any PC StarCraft map, protected ones included.

verify_maps.MpqReader refuses files with MPQ_FILE_ENCRYPTED, and 115 of the 119
ladder maps on this machine set that flag -- competitive maps are routinely
"protected", and encrypting the scenario is the cheapest form of it. That left
a 4-map sample to test injection against, which is not a sample.

Nothing new has to be written to fix it. mpq_keycrack already carries the Storm
crypt table, the key derivation, the FIX_KEY adjustment and a sector-aware
block reader -- built earlier in this project to crack keys when the FILENAME is
unknown. Here the filename is known ("staredit\\scenario.chk"), so the key is
simply derivable and the hard path is not needed; it is only there as a
fallback for maps whose block flags lie.
"""
from __future__ import annotations

import sys
from pathlib import Path


from compare_with_stock import StockMpq
from extract_sc64_maps import chk_sections, looks_like_chk
from mpq_keycrack import blocks, detect_file_key, expected_key, read_block
from verify_maps import MpqReader

CHK_NAME = "staredit" + chr(92) + "scenario.chk"

# Sections every real scenario carries. Used to recognise a CHK that has been
# deliberately malformed, where a strict check cannot be applied.
ESSENTIAL = {b"VER ", b"DIM ", b"ERA ", b"OWNR", b"MTXM"}


class Unreadable(Exception):
    pass


def is_scenario(data: bytes) -> bool:
    """Tolerant CHK check, for data already known to be a scenario block.

    looks_like_chk() demands that the first tag be a known one and that every
    tag be printable ASCII. That is right when scanning the cartridge, where
    the question is "is this arbitrary blob a CHK" and a false positive is
    expensive. It is wrong here, where the MPQ has already told us this block
    is named scenario.chk and the only question is whether we decoded it.

    Competitive maps are routinely "protected" by interleaving junk sections
    with random four-byte tags between the real ones, and by starting the file
    with one so the first tag is garbage. StarCraft ignores tags it does not
    recognise, so the map plays; a strict parser rejects it outright. 46 of the
    119 ladder maps here are built that way, including five of the seven 2017
    Frontier League maps.

    So walk it the way the game does and ask whether the sections a scenario
    must have are present.
    """
    if looks_like_chk(data):
        return True
    try:
        tags = {tag for tag, _ in chk_sections(data)}
    except Exception:
        return False
    return ESSENTIAL <= tags


def read_chk(path: str | Path) -> bytes:
    """The scenario.chk from a .scm/.scx, decrypting if necessary."""
    path = str(path)

    # Fast path: unencrypted maps go through the ordinary reader.
    try:
        chk = MpqReader(path).read(CHK_NAME)
        if chk and is_scenario(chk):
            return chk
    except Exception:
        pass

    arc = StockMpq(path)
    entry = arc._lookup(CHK_NAME)
    if entry is None:
        raise Unreadable(f"{Path(path).name}: no {CHK_NAME} in the archive")
    block_index = entry[-1] if isinstance(entry, tuple) else entry

    # Known filename -> the key is derivable outright, FIX_KEY included.
    try:
        key = expected_key(arc, block_index, CHK_NAME)
        chk = read_block(arc, block_index, key)
        if chk and is_scenario(chk):
            return chk
    except Exception:
        pass

    # Protectors sometimes leave flags that disagree with the real layout, so
    # fall back to deriving the key from the ciphertext itself.
    try:
        key = detect_file_key(arc, block_index)
        if key is not None:
            chk = read_block(arc, block_index, key)
            if chk and is_scenario(chk):
                return chk
    except Exception as exc:
        raise Unreadable(f"{Path(path).name}: {exc}") from exc

    raise Unreadable(f"{Path(path).name}: could not recover a valid CHK")
