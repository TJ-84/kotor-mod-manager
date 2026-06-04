"""
keybif.py — Read KOTOR's chitin.key index and extract resources from .bif archives.

KEY file (BioWare Aurora, V1):
    Header (64 bytes):
        char[8]   "KEY V1  "
        uint32    bif_count
        uint32    key_count
        uint32    file_table_offset
        uint32    key_table_offset
        uint32    build_year
        uint32    build_day
        char[32]  reserved
    File table  (bif_count * 12 bytes):
        uint32 file_size, uint32 filename_offset, uint16 filename_size, uint16 drives
    Key table   (key_count * 22 bytes):
        char[16] resref, uint16 res_type, uint32 res_id
        where res_id = (bif_index << 20) | bif_resource_index

BIF file (V1):
    char[8]   "BIFFV1  "
    uint32    var_count
    uint32    fixed_count   (always 0)
    uint32    var_table_offset
    Variable resource table (var_count * 16 bytes):
        uint32 id, uint32 offset, uint32 size, uint32 res_type
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


RESTYPE_2DA = 2017
RESTYPE_UTI = 2025
RESTYPE_UTC = 2027

RESTYPE_EXT = {
    RESTYPE_2DA: "2da",
    RESTYPE_UTI: "uti",
    RESTYPE_UTC: "utc",
}


@dataclass
class KeyEntry:
    resref: str
    res_type: int
    bif_index: int
    bif_res_index: int


@dataclass
class ChitinIndex:
    bif_paths: List[str]           # relative paths from KEY's file table
    entries: List[KeyEntry]

    def filter(self, res_type: int) -> List[KeyEntry]:
        return [e for e in self.entries if e.res_type == res_type]


def load_chitin(key_path: Path) -> ChitinIndex:
    raw = Path(key_path).read_bytes()
    if not raw.startswith(b"KEY V1"):
        raise ValueError(f"Not a KEY V1 file: {key_path}")

    bif_count, key_count, file_off, key_off = struct.unpack_from("<IIII", raw, 8)

    bif_paths: List[str] = []
    for i in range(bif_count):
        size, name_off, name_size, drives = struct.unpack_from(
            "<IIHH", raw, file_off + i * 12
        )
        name = raw[name_off : name_off + name_size].rstrip(b"\x00").decode(
            "utf-8", errors="replace"
        )
        # KEY uses Windows path separators; normalise
        bif_paths.append(name.replace("\\", "/"))

    entries: List[KeyEntry] = []
    for i in range(key_count):
        pos = key_off + i * 22
        resref = raw[pos : pos + 16].rstrip(b"\x00").decode("utf-8", errors="replace")
        res_type = struct.unpack_from("<H", raw, pos + 16)[0]
        res_id = struct.unpack_from("<I", raw, pos + 18)[0]
        entries.append(
            KeyEntry(
                resref=resref,
                res_type=res_type,
                bif_index=res_id >> 20,
                bif_res_index=res_id & 0xFFFFF,
            )
        )

    return ChitinIndex(bif_paths=bif_paths, entries=entries)


def extract_resource(bif_path: Path, res_index: int) -> Tuple[bytes, int]:
    """Return (data, res_type) for a single resource inside a .bif file."""
    raw = Path(bif_path).read_bytes()
    if not raw.startswith(b"BIFFV1"):
        raise ValueError(f"Not a BIFFV1 archive: {bif_path}")
    var_count, fixed_count, var_off = struct.unpack_from("<III", raw, 8)
    if res_index >= var_count:
        raise IndexError(f"Resource index {res_index} >= {var_count}")
    rid, offset, size, res_type = struct.unpack_from(
        "<IIII", raw, var_off + res_index * 16
    )
    return raw[offset : offset + size], res_type


def vanilla_resources(
    data_dir: Path, res_type: int
) -> Dict[str, Tuple[Path, int]]:
    """Map resref -> (bif file path, resource index) for a given resource type.

    data_dir must contain chitin.key. BIF locations vary by Mac vs PC layout —
    paths inside chitin.key may be relative to the game root, to the .app's
    Contents/Assets dir, or just bare filenames. We probe candidate base dirs
    and finally fall back to a recursive search by filename.
    """
    data_dir = Path(data_dir)
    key = load_chitin(data_dir / "chitin.key")

    bundle_contents = data_dir.parent  # e.g. .../Contents
    candidate_bases = [
        data_dir,                # bif paths might be relative to Assets/
        data_dir.parent,         # or to Contents/
        data_dir.parent.parent,  # or to the .app/
    ]

    def resolve_bif(rel: str) -> Path:
        # Try each candidate base
        for base in candidate_bases:
            p = base / rel
            if p.exists():
                return p
        # Fallback: search recursively for the basename anywhere under the
        # .app bundle. KOTOR ships a handful of .bif files, so this is cheap.
        name = Path(rel).name
        for p in bundle_contents.rglob(name):
            return p
        # Last resort: return the first candidate (will 404 on extract).
        return candidate_bases[0] / rel

    out: Dict[str, Tuple[Path, int]] = {}
    for e in key.filter(res_type):
        bif_rel = key.bif_paths[e.bif_index]
        out[e.resref] = (resolve_bif(bif_rel), e.bif_res_index)
    return out
