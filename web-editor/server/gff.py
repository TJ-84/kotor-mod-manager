"""
gff.py — Read and write BioWare GFF files (.uti items, .utc creatures, etc.)

GFF v3.2 layout:
    Header (56 bytes):
        char[4]  file type ("UTI ", "UTC ", ...)
        char[4]  version ("V3.2")
        uint32   struct_offset
        uint32   struct_count
        uint32   field_offset
        uint32   field_count
        uint32   label_offset
        uint32   label_count
        uint32   field_data_offset
        uint32   field_data_count        (bytes)
        uint32   field_indices_offset
        uint32   field_indices_count     (bytes)
        uint32   list_indices_offset
        uint32   list_indices_count      (bytes)

Field types:
    0 BYTE, 1 CHAR, 2 WORD, 3 SHORT, 4 DWORD, 5 INT,
    6 DWORD64, 7 INT64, 8 FLOAT, 9 DOUBLE,
    10 CExoString, 11 ResRef, 12 CExoLocString,
    13 Void, 14 Struct, 15 List

This module exposes a simple JSON-friendly representation:
    { "_type": "UTI ", "_version": "V3.2", "_struct": <field-map> }
where each struct is a dict label -> { "type": <int>, "value": <python> }.
Struct values are nested dicts; lists are arrays of dicts.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple


# Field type constants
BYTE, CHAR, WORD, SHORT, DWORD, INT = 0, 1, 2, 3, 4, 5
DWORD64, INT64, FLOAT, DOUBLE = 6, 7, 8, 9
EXOSTR, RESREF, LOCSTR, VOID, STRUCT, LIST = 10, 11, 12, 13, 14, 15
# BioWare extensions (used by KOTOR / NWN):
ORIENTATION, VECTOR = 16, 17

SIMPLE_TYPE_NAMES = {
    BYTE: "byte", CHAR: "char", WORD: "word", SHORT: "short",
    DWORD: "dword", INT: "int", DWORD64: "dword64", INT64: "int64",
    FLOAT: "float", DOUBLE: "double",
    EXOSTR: "string", RESREF: "resref", LOCSTR: "locstring",
    VOID: "void", STRUCT: "struct", LIST: "list",
    ORIENTATION: "orientation", VECTOR: "vector",
}


# ---------- Reader ---------------------------------------------------------

def load(path: Path) -> dict:
    raw = Path(path).read_bytes()
    return _parse(raw)


def loads(raw: bytes) -> dict:
    return _parse(raw)


def _parse(raw: bytes) -> dict:
    file_type = raw[0:4].decode("ascii", errors="replace")
    version = raw[4:8].decode("ascii", errors="replace")
    if version != "V3.2":
        raise ValueError(f"Unsupported GFF version: {version!r}")

    (
        struct_off, struct_count,
        field_off, field_count,
        label_off, label_count,
        fdata_off, fdata_count,
        findex_off, findex_count,
        lindex_off, lindex_count,
    ) = struct.unpack_from("<12I", raw, 8)

    structs = [
        struct.unpack_from("<III", raw, struct_off + i * 12)
        for i in range(struct_count)
    ]
    fields = [
        struct.unpack_from("<III", raw, field_off + i * 12)
        for i in range(field_count)
    ]
    labels = [
        raw[label_off + i * 16 : label_off + i * 16 + 16].rstrip(b"\x00")
        .decode("utf-8", errors="replace")
        for i in range(label_count)
    ]
    fdata = raw[fdata_off : fdata_off + fdata_count]
    findex = raw[findex_off : findex_off + findex_count]
    lindex = raw[lindex_off : lindex_off + lindex_count]

    def read_struct(idx: int) -> dict:
        s_type, data_or_off, fcount = structs[idx]
        if fcount == 0:
            field_indices = []
        elif fcount == 1:
            field_indices = [data_or_off]
        else:
            field_indices = list(
                struct.unpack_from(f"<{fcount}I", findex, data_or_off)
            )
        out: Dict[str, Any] = {"_structType": s_type}
        for fi in field_indices:
            ftype, label_idx, dval = fields[fi]
            label = labels[label_idx]
            # KOTOR's GFFs sometimes repeat the same label at the same struct
            # level (e.g. BonusForcePoints, AssignedPup, PlayerCreated). To
            # represent that in a plain dict we suffix duplicates with #N.
            # The writer strips the suffix back off before emitting.
            final_label = label
            n = 1
            while final_label in out:
                final_label = f"{label}#{n}"
                n += 1
            out[final_label] = {"type": ftype, "value": read_field_value(ftype, dval)}
        return out

    def read_field_value(ftype: int, dval: int) -> Any:
        if ftype == BYTE:
            return dval & 0xFF
        if ftype == CHAR:
            v = dval & 0xFF
            return v - 256 if v >= 128 else v
        if ftype == WORD:
            return dval & 0xFFFF
        if ftype == SHORT:
            v = dval & 0xFFFF
            return v - 65536 if v >= 32768 else v
        if ftype == DWORD:
            return dval
        if ftype == INT:
            return struct.unpack("<i", struct.pack("<I", dval))[0]
        if ftype == FLOAT:
            return struct.unpack("<f", struct.pack("<I", dval))[0]
        if ftype == DWORD64:
            return struct.unpack_from("<Q", fdata, dval)[0]
        if ftype == INT64:
            return struct.unpack_from("<q", fdata, dval)[0]
        if ftype == DOUBLE:
            return struct.unpack_from("<d", fdata, dval)[0]
        if ftype == EXOSTR:
            (length,) = struct.unpack_from("<I", fdata, dval)
            return fdata[dval + 4 : dval + 4 + length].decode("utf-8", errors="replace")
        if ftype == RESREF:
            length = fdata[dval]
            return fdata[dval + 1 : dval + 1 + length].decode("utf-8", errors="replace")
        if ftype == LOCSTR:
            total, strref, count = struct.unpack_from("<III", fdata, dval)
            pos = dval + 12
            substrings = []
            for _ in range(count):
                sid, slen = struct.unpack_from("<II", fdata, pos)
                pos += 8
                text = fdata[pos : pos + slen].decode("utf-8", errors="replace")
                pos += slen
                substrings.append({"id": sid, "text": text})
            return {"strref": strref, "substrings": substrings}
        if ftype == VOID:
            (length,) = struct.unpack_from("<I", fdata, dval)
            return fdata[dval + 4 : dval + 4 + length].hex()
        if ftype == ORIENTATION:
            return list(struct.unpack_from("<4f", fdata, dval))
        if ftype == VECTOR:
            return list(struct.unpack_from("<3f", fdata, dval))
        if ftype == STRUCT:
            if dval == 0xFFFFFFFF:
                return {"_structType": 0}
            return read_struct(dval)
        if ftype == LIST:
            if dval == 0xFFFFFFFF:
                return []
            (lcount,) = struct.unpack_from("<I", lindex, dval)
            if lcount == 0:
                return []
            sids = struct.unpack_from(f"<{lcount}I", lindex, dval + 4)
            return [read_struct(sid) for sid in sids]
        raise ValueError(f"Unknown field type: {ftype}")

    root = read_struct(0)
    return {"_type": file_type, "_version": version, "_struct": root}


# ---------- Writer ---------------------------------------------------------

def save(path: Path, gff: dict) -> None:
    Path(path).write_bytes(dumps(gff))


def dumps(gff: dict) -> bytes:
    structs: List[Tuple[int, int, int]] = []   # (type, data_or_off, fcount)
    fields: List[Tuple[int, int, int]] = []    # (type, label_idx, dval)
    labels: List[str] = []
    fdata = bytearray()
    findex = bytearray()
    lindex = bytearray()

    label_idx_for: Dict[str, int] = {}

    def label_index(name: str) -> int:
        if name in label_idx_for:
            return label_idx_for[name]
        label_idx_for[name] = len(labels)
        labels.append(name)
        return label_idx_for[name]

    def add_fdata_aligned(payload: bytes) -> int:
        off = len(fdata)
        fdata.extend(payload)
        return off

    def write_field_value(ftype: int, value: Any) -> int:
        # Raise a clear error rather than silently wrapping; the previous
        # behaviour was wrap-around which can corrupt a save (e.g. HP=99999
        # wraps to 34463 for a SHORT field).
        def _check(lo: int, hi: int, label: str) -> int:
            try:
                v = int(value)
            except (TypeError, ValueError):
                raise ValueError(f"{label} field must be an integer, got {value!r}")
            if v < lo or v > hi:
                raise ValueError(
                    f"{label} value {v} out of range [{lo}, {hi}] — would overflow on write"
                )
            return v

        if ftype == BYTE:
            return _check(0, 0xFF, "BYTE") & 0xFF
        if ftype == CHAR:
            v = _check(-128, 127, "CHAR")
            return (v + 256) & 0xFF if v < 0 else v
        if ftype == WORD:
            return _check(0, 0xFFFF, "WORD") & 0xFFFF
        if ftype == SHORT:
            v = _check(-32768, 32767, "SHORT")
            return (v + 65536) & 0xFFFF if v < 0 else v
        if ftype == DWORD:
            return _check(0, 0xFFFFFFFF, "DWORD") & 0xFFFFFFFF
        if ftype == INT:
            v = _check(-2**31, 2**31 - 1, "INT")
            return struct.unpack("<I", struct.pack("<i", v))[0]
        if ftype == FLOAT:
            try:
                return struct.unpack("<I", struct.pack("<f", float(value)))[0]
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"FLOAT value {value!r} not finite")
        if ftype == DWORD64:
            return add_fdata_aligned(struct.pack("<Q", int(value)))
        if ftype == INT64:
            return add_fdata_aligned(struct.pack("<q", int(value)))
        if ftype == DOUBLE:
            return add_fdata_aligned(struct.pack("<d", float(value)))
        if ftype == EXOSTR:
            payload = value.encode("utf-8")
            return add_fdata_aligned(struct.pack("<I", len(payload)) + payload)
        if ftype == RESREF:
            payload = value.encode("utf-8")[:255]
            return add_fdata_aligned(bytes([len(payload)]) + payload)
        if ftype == LOCSTR:
            strref = int(value.get("strref", -1) & 0xFFFFFFFF)
            subs = value.get("substrings", [])
            body = bytearray()
            for s in subs:
                text = s["text"].encode("utf-8")
                body += struct.pack("<II", s["id"], len(text)) + text
            total = 8 + len(body)  # total size excludes the total field itself
            payload = struct.pack("<III", total, strref, len(subs)) + bytes(body)
            return add_fdata_aligned(payload)
        if ftype == VOID:
            payload = bytes.fromhex(value)
            return add_fdata_aligned(struct.pack("<I", len(payload)) + payload)
        if ftype == ORIENTATION:
            return add_fdata_aligned(struct.pack("<4f", *(float(x) for x in value)))
        if ftype == VECTOR:
            return add_fdata_aligned(struct.pack("<3f", *(float(x) for x in value)))
        if ftype == STRUCT:
            return emit_struct(value)
        if ftype == LIST:
            sids = [emit_struct(item) for item in value]
            off = len(lindex)
            lindex.extend(struct.pack("<I", len(sids)))
            for sid in sids:
                lindex.extend(struct.pack("<I", sid))
            return off
        raise ValueError(f"Unknown field type: {ftype}")

    def emit_struct(node: dict) -> int:
        # Reserve this struct's slot before recursing so nested structs get
        # later indices. This is what makes the root struct land at index 0.
        sid = len(structs)
        structs.append((0, 0, 0))  # placeholder, filled in below
        s_type = node.get("_structType", 0)
        field_ids: List[int] = []
        for label, entry in node.items():
            if label.startswith("_"):
                continue
            ftype = entry["type"]
            value = entry["value"]
            # Reserve this field's slot too, for the same reason.
            fid = len(fields)
            fields.append((0, 0, 0))  # placeholder
            dval = write_field_value(ftype, value)
            # Strip our internal duplicate-label suffix before writing
            real_label = label.split("#", 1)[0]
            fields[fid] = (ftype, label_index(real_label), dval)
            field_ids.append(fid)

        if len(field_ids) == 1:
            data_or_off = field_ids[0]
        else:
            data_or_off = len(findex)
            for fid in field_ids:
                findex.extend(struct.pack("<I", fid))

        structs[sid] = (s_type, data_or_off, len(field_ids))
        return sid

    emit_struct(gff["_struct"])

    # Build header + sections in canonical order:
    #   structs, fields, labels, field data, field indices, list indices.
    header_size = 56
    struct_section = b"".join(struct.pack("<III", *s) for s in structs)
    field_section = b"".join(struct.pack("<III", *f) for f in fields)
    label_section = b"".join(
        name.encode("utf-8")[:16].ljust(16, b"\x00") for name in labels
    )

    struct_off = header_size
    field_off = struct_off + len(struct_section)
    label_off = field_off + len(field_section)
    fdata_off = label_off + len(label_section)
    findex_off = fdata_off + len(fdata)
    lindex_off = findex_off + len(findex)

    file_type = gff["_type"].ljust(4)[:4].encode("ascii")
    version = gff["_version"].ljust(4)[:4].encode("ascii")
    header = file_type + version + struct.pack(
        "<12I",
        struct_off, len(structs),
        field_off, len(fields),
        label_off, len(labels),
        fdata_off, len(fdata),
        findex_off, len(findex),
        lindex_off, len(lindex),
    )

    return (
        header
        + struct_section
        + field_section
        + label_section
        + bytes(fdata)
        + bytes(findex)
        + bytes(lindex)
    )
