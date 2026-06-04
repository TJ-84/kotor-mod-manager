"""
tlk.py — Read BioWare dialog.tlk so StrRef numbers can be resolved to text.

Header (20 bytes):
    char[4]  "TLK "
    char[4]  "V3.0"
    uint32   language_id
    uint32   string_count
    uint32   string_entries_offset

Per entry (40 bytes):
    uint32   flags
    char[16] sound_resref
    uint32   volume_variance
    uint32   pitch_variance
    uint32   offset_to_string  (relative to string_entries_offset)
    uint32   string_size
    float    sound_length
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional


class TLK:
    def __init__(self, path: Path):
        self.path = Path(path)
        raw = self.path.read_bytes()
        if not raw.startswith(b"TLK V3.0"):
            raise ValueError(f"Not a TLK V3.0 file: {path}")
        (self.language_id, self.string_count, self.entries_offset) = struct.unpack_from(
            "<III", raw, 8
        )
        self._raw = raw

    def get(self, strref: int) -> Optional[str]:
        if strref < 0 or strref >= self.string_count:
            return None
        entry_off = 20 + strref * 40
        # offset_to_string at entry_off + 28, string_size at +32
        str_off = struct.unpack_from("<I", self._raw, entry_off + 28)[0]
        str_size = struct.unpack_from("<I", self._raw, entry_off + 32)[0]
        start = self.entries_offset + str_off
        text = self._raw[start : start + str_size]
        return text.decode("utf-8", errors="replace")


_cache: dict[str, TLK] = {}


def load_cached(path: Path) -> TLK:
    key = str(path)
    if key not in _cache:
        _cache[key] = TLK(path)
    return _cache[key]


def find_dialog_tlk(bundle_contents: Path) -> Optional[Path]:
    """Probe likely locations of dialog.tlk inside a Mac KOTOR .app bundle."""
    for candidate in [
        bundle_contents / "Assets" / "dialog.tlk",
        bundle_contents / "KOTOR Data" / "dialog.tlk",
        bundle_contents / "Resources" / "dialog.tlk",
        bundle_contents / "Resources" / "data" / "dialog.tlk",
    ]:
        if candidate.exists():
            return candidate
    # Fallback: recursive search
    for p in bundle_contents.rglob("dialog.tlk"):
        return p
    return None
