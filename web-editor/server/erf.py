"""
erf.py — Read and write BioWare ERF archives (.erf, .mod, .sav, .rim).

KOTOR save games store the PC, current module state, and equipped items
inside a SAVEGAME.sav file, which is an ERF archive.

ERF V1.0 layout:
    Header (160 bytes):
        char[4]   file_type ("ERF ", "MOD ", "SAV ")
        char[4]   version  ("V1.0")
        uint32    language_count
        uint32    localized_string_size  (bytes)
        uint32    entry_count
        uint32    offset_to_localized_strings
        uint32    offset_to_key_list
        uint32    offset_to_resource_list
        uint32    build_year       (since 1900)
        uint32    build_day
        uint32    description_strref
        char[116] reserved (zeros)

    Localized strings: language_count × { uint32 language_id, uint32 size, char[size] }

    Key list (entry_count × 24 bytes):
        char[16]  resref
        uint32    resource_id
        uint16    resource_type
        uint16    unused

    Resource list (entry_count × 8 bytes):
        uint32    offset
        uint32    size

    Resources: raw bytes at the recorded offsets
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Common resource types
RES_2DA = 2017
RES_UTI = 2025
RES_UTC = 2027
RES_RES = 2013
RES_IFO = 2014
RES_GIT = 2023

# Reverse-map for friendly extensions
RES_EXT = {
    2017: "2da", 2014: "ifo", 2015: "are", 2023: "git",
    2025: "uti", 2027: "utc", 2029: "utd", 2031: "ute",
    2033: "utm", 2035: "utp", 2036: "uts", 2037: "utt",
    2038: "utw", 2013: "res",
    2032: "dlg", 2026: "fac",
    2010: "ncs", 2009: "nss",
    2030: "btt", 2052: "jrl",
}


@dataclass
class ErfEntry:
    resref: str
    res_type: int
    data: bytes = b""

    @property
    def extension(self) -> str:
        return RES_EXT.get(self.res_type, str(self.res_type))

    @property
    def filename(self) -> str:
        return f"{self.resref}.{self.extension}"


@dataclass
class ERF:
    file_type: str = "ERF "      # 4 chars
    version: str = "V1.0"
    description_strref: int = 0xFFFFFFFF
    build_year: int = 125         # preserved from original on load
    build_day: int = 1
    # Localized description strings: language_id -> text
    localized: Dict[int, str] = field(default_factory=dict)
    entries: List[ErfEntry] = field(default_factory=list)

    def find(self, resref: str, res_type: Optional[int] = None) -> Optional[ErfEntry]:
        rl = resref.lower()
        for e in self.entries:
            if e.resref.lower() == rl and (res_type is None or e.res_type == res_type):
                return e
        return None


def load(path: Path) -> ERF:
    raw = Path(path).read_bytes()
    file_type = raw[0:4].decode("ascii", errors="replace")
    version = raw[4:8].decode("ascii", errors="replace")
    if version != "V1.0":
        raise ValueError(f"Unsupported ERF version: {version!r}")

    (
        language_count, locstr_size, entry_count,
        locstr_off, keylist_off, reslist_off,
        build_year, build_day, desc_strref,
    ) = struct.unpack_from("<9I", raw, 8)

    erf = ERF(
        file_type=file_type,
        version=version,
        description_strref=desc_strref,
        build_year=build_year,
        build_day=build_day,
    )

    # Localized strings
    pos = locstr_off
    for _ in range(language_count):
        if pos + 8 > len(raw):
            break
        lang_id, size = struct.unpack_from("<II", raw, pos)
        pos += 8
        text = raw[pos:pos + size].rstrip(b"\x00").decode("utf-8", errors="replace")
        erf.localized[lang_id] = text
        pos += size

    # Key list
    keys: List[Tuple[str, int, int]] = []
    for i in range(entry_count):
        kp = keylist_off + i * 24
        resref = raw[kp:kp + 16].rstrip(b"\x00").decode("utf-8", errors="replace")
        res_id, res_type = struct.unpack_from("<IH", raw, kp + 16)
        keys.append((resref, res_id, res_type))

    # Resource list
    for i, (resref, _, res_type) in enumerate(keys):
        rp = reslist_off + i * 8
        offset, size = struct.unpack_from("<II", raw, rp)
        data = raw[offset:offset + size]
        erf.entries.append(ErfEntry(resref=resref, res_type=res_type, data=data))

    return erf


def save(path: Path, erf: ERF) -> None:
    Path(path).write_bytes(dumps(erf))


def dumps(erf: ERF) -> bytes:
    # Layout: header(160) | localized | keys(24*N) | reslist(8*N) | data
    entries = erf.entries
    n = len(entries)

    # Build localized strings block
    loc_block = b""
    for lang_id, text in erf.localized.items():
        payload = text.encode("utf-8")
        loc_block += struct.pack("<II", lang_id, len(payload)) + payload
    language_count = len(erf.localized)

    locstr_off = 160
    keylist_off = locstr_off + len(loc_block)
    reslist_off = keylist_off + n * 24
    data_off = reslist_off + n * 8

    # Resource data + offsets
    data_block = bytearray()
    res_offsets: List[Tuple[int, int]] = []
    for e in entries:
        off = data_off + len(data_block)
        data_block.extend(e.data)
        res_offsets.append((off, len(e.data)))

    # Header
    header = (
        erf.file_type.encode("ascii").ljust(4)[:4]
        + erf.version.encode("ascii").ljust(4)[:4]
        + struct.pack(
            "<9I",
            language_count, len(loc_block), n,
            locstr_off, keylist_off, reslist_off,
            erf.build_year, erf.build_day,
            erf.description_strref & 0xFFFFFFFF,
        )
        + b"\x00" * 116
    )

    # Keys
    key_block = bytearray()
    for i, e in enumerate(entries):
        resref_bytes = e.resref.encode("utf-8")[:16].ljust(16, b"\x00")
        key_block += resref_bytes + struct.pack("<IHH", i, e.res_type & 0xFFFF, 0)

    # Resource list (offset, size)
    res_block = b"".join(struct.pack("<II", o, s) for o, s in res_offsets)

    return bytes(header) + loc_block + bytes(key_block) + res_block + bytes(data_block)


def replace_entry(erf: ERF, resref: str, res_type: int, data: bytes) -> None:
    """In-place replacement of one entry's bytes."""
    rl = resref.lower()
    for e in erf.entries:
        if e.resref.lower() == rl and e.res_type == res_type:
            e.data = data
            return
    raise KeyError(f"{resref}.{RES_EXT.get(res_type, res_type)} not in archive")
