"""
twoda.py — Read and write KOTOR .2da files.

Supports both formats:
  - 2DA V2.0  (ASCII, whitespace-separated, what mods ship)
  - 2DA V2.b  (binary, what the game's own 2da.bif contains)

Writes are always emitted as V2.0 ASCII so the result is human-diffable and
the game engine accepts both interchangeably from the Override folder.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


DEFAULT_EMPTY = "****"


@dataclass
class TwoDA:
    version: str = "2DA V2.0"
    columns: List[str] = field(default_factory=list)
    # rows[i] is a list aligned with columns; row label is rows[i][0] if you
    # include it as a column, but KOTOR .2da convention puts the row index
    # implicitly as the first whitespace token. We store the row label
    # separately to preserve that convention.
    row_labels: List[str] = field(default_factory=list)
    rows: List[List[str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "columns": self.columns,
            "row_labels": self.row_labels,
            "rows": self.rows,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TwoDA":
        return cls(
            version=data.get("version", "2DA V2.0"),
            columns=list(data["columns"]),
            row_labels=list(data["row_labels"]),
            rows=[list(r) for r in data["rows"]],
        )


def load(path: Path) -> TwoDA:
    path = Path(path)
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"2DA V2.b"):
        return _load_binary(path)
    return _load_ascii(path)


def _load_ascii(path: Path) -> TwoDA:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    # Normalise line endings
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or not lines[0].startswith("2DA"):
        raise ValueError(f"Not a .2da file: {path}")

    version = lines[0].strip()
    # Line 1 is often blank (default value line). KOTOR .2da rarely uses it.
    idx = 1
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines):
        raise ValueError("Truncated .2da: no header row")

    header = lines[idx].split()
    idx += 1

    row_labels: List[str] = []
    rows: List[List[str]] = []
    for line in lines[idx:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 1:
            continue
        # First token is the row index/label; remaining tokens are columns
        label = parts[0]
        values = parts[1:]
        # Pad / truncate to column count
        if len(values) < len(header):
            values = values + [DEFAULT_EMPTY] * (len(header) - len(values))
        elif len(values) > len(header):
            values = values[: len(header)]
        row_labels.append(label)
        rows.append(values)

    return TwoDA(version=version, columns=header, row_labels=row_labels, rows=rows)


def _load_binary(path: Path) -> TwoDA:
    """Parse the binary 2DA V2.b format used inside 2da.bif.

    Layout (little-endian):
        8 bytes : "2DA V2.b"
        1 byte  : 0x0A newline
        columns : NUL-terminated strings, terminator 0x00 ends the list
        4 bytes : row count (uint32)
        rows    : row count NUL-terminated row labels
        cells   : row_count * col_count uint16 offsets into the data block
        2 bytes : data block size (uint16)
        N bytes : data block (NUL-terminated strings indexed by offsets)
    """
    raw = Path(path).read_bytes()
    if not raw.startswith(b"2DA V2.b"):
        raise ValueError(f"Not a binary .2da: {path}")
    pos = 9  # skip header + newline

    def read_cstr_list_until_tab_terminator() -> List[str]:
        # In V2.b, the columns list is terminated by a single 0x00 after the
        # last column's own 0x00 terminator — i.e. an empty string.
        items: List[str] = []
        nonlocal pos
        while True:
            end = raw.index(b"\t", pos)
            token = raw[pos:end].decode("utf-8", errors="replace")
            pos = end + 1
            if token == "":
                break
            items.append(token)
        return items

    # Columns are tab-terminated, list ends with an empty token (just 0x00)
    # Actually in the real format columns are separated by 0x09 (tab) and the
    # list itself ends with a 0x00. Implementations in the wild vary; we read
    # until a 0x00 is found at the current position.
    columns: List[str] = []
    while True:
        if raw[pos] == 0x00:
            pos += 1
            break
        end = raw.index(b"\t", pos)
        columns.append(raw[pos:end].decode("utf-8", errors="replace"))
        pos = end + 1

    (row_count,) = struct.unpack_from("<I", raw, pos)
    pos += 4

    row_labels: List[str] = []
    for _ in range(row_count):
        end = raw.index(b"\t", pos)
        row_labels.append(raw[pos:end].decode("utf-8", errors="replace"))
        pos = end + 1

    col_count = len(columns)
    cell_count = row_count * col_count
    offsets = struct.unpack_from(f"<{cell_count}H", raw, pos)
    pos += cell_count * 2

    (data_size,) = struct.unpack_from("<H", raw, pos)
    pos += 2
    data_block = raw[pos : pos + data_size]

    def read_cell(offset: int) -> str:
        end = data_block.index(b"\x00", offset)
        return data_block[offset:end].decode("utf-8", errors="replace")

    rows: List[List[str]] = []
    for r in range(row_count):
        row = []
        for c in range(col_count):
            off = offsets[r * col_count + c]
            value = read_cell(off)
            row.append(value if value != "" else DEFAULT_EMPTY)
        rows.append(row)

    return TwoDA(
        version="2DA V2.b",
        columns=columns,
        row_labels=row_labels,
        rows=rows,
    )


def save_ascii(twoda: TwoDA, path: Path) -> None:
    """Write a .2da as ASCII V2.0. The game accepts this from Override/.

    Columns are aligned with single tabs — KOTOR Tool's convention. The
    engine tokenises on any whitespace so tab vs space doesn't matter.
    """
    path = Path(path)
    lines: List[str] = []
    lines.append("2DA V2.0")
    lines.append("")  # default value line, blank
    lines.append("\t".join(twoda.columns))
    for label, row in zip(twoda.row_labels, twoda.rows):
        cells = [label] + [c if c != "" else DEFAULT_EMPTY for c in row]
        lines.append("\t".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
