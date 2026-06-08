"""
KOTOR .2da / .uti / .utc web editor — FastAPI backend.

Endpoints:
    GET  /api/installs                            Detected KOTOR 1/2 installs
    GET  /api/files?install=K1                    .2da/.uti/.utc in Override
    GET  /api/vanilla?install=K1&kind=2da         Vanilla resources in BIFs
    POST /api/extract?install=K1&kind=2da&name=X  Copy a vanilla resource into Override
    GET  /api/file?install=K1&name=classes.2da    Read a .2da or GFF as JSON
    POST /api/file?install=K1&name=classes.2da    Save edits (with timestamped backup)
    GET  /api/schema?name=classes.2da             Column descriptions for a .2da
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import erf as erfmod
import gff as gffmod
import keybif
import tlk as tlkmod
from twoda import TwoDA, load as load_2da, save_ascii


HERE = Path(__file__).resolve().parent
CLIENT_DIR = HERE.parent / "client"
SCHEMAS_DIR = HERE / "schemas"
SCHEMA_PATH = SCHEMAS_DIR / "columns.json"
GFF_SCHEMA_PATH = SCHEMAS_DIR / "gff_fields.json"
ENUMS_PATH = SCHEMAS_DIR / "enums.json"

HOME = Path(os.path.expanduser("~"))
STEAM_COMMON = HOME / "Library" / "Application Support" / "Steam" / "steamapps" / "common"


def _find_saves_dir(game_dir: Path, bundle: Path) -> Path:
    """Probe likely macOS save locations for KOTOR 1 and KOTOR 2."""
    candidates = [
        # Inside the .app bundle (older Aspyr layouts)
        bundle / "Contents" / "KOTOR Data" / "saves",
        bundle / "Contents" / "Resources" / "saves",
        # KOTOR 1 user save locations
        HOME / "Library" / "Application Support" / "Knights of the Old Republic" / "saves",
        HOME / "Documents" / "Knights of the Old Republic" / "saves",
        # KOTOR 2 user save locations (note the "Star Wars" prefix on Mac)
        HOME / "Library" / "Application Support" / "Star Wars Knights of the Old Republic II" / "saves",
        HOME / "Library" / "Application Support" / "Knights of the Old Republic II" / "saves",
        HOME / "Documents" / "Star Wars Knights of the Old Republic II" / "saves",
        HOME / "Documents" / "Knights of the Old Republic II" / "saves",
        # Sandboxed Steam containers
        HOME / "Library" / "Containers" / "com.aspyr.kotor.steam" / "Data" / "Documents" / "saves",
        HOME / "Library" / "Containers" / "com.aspyr.kotor2.steam" / "Data" / "Documents" / "saves",
    ]
    # Use the install folder's game name to bias the probe order — prefer
    # K2 paths if this is the K2 install dir.
    if "II" in game_dir.name or "Sith" in game_dir.name:
        k2_first = [c for c in candidates if "II" in str(c) or "Sith" in str(c)]
        k1_only = [c for c in candidates if c not in k2_first]
        candidates = k2_first + k1_only
    else:
        k1_first = [c for c in candidates if "II" not in str(c) and "Sith" not in str(c)]
        k2_only = [c for c in candidates if c not in k1_first]
        candidates = k1_first + k2_only
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _find_install(game_dir: Path, app_name: str) -> Dict[str, Path]:
    """Resolve the Override and data dirs for a Mac Steam KOTOR install.

    The Aspyr macOS port lays things out as:
        <game>/<App>.app/Contents/Assets/         <- chitin.key, *.bif
        <game>/<App>.app/Contents/KOTOR Data/Override/
    Older / alternate layouts may use:
        <game>/<App>.app/Contents/Resources/data/
        <game>/<App>.app/Contents/Resources/Override/
    We probe both, falling back to the Aspyr layout for paths that don't exist
    so the UI still shows something sensible.
    """
    bundle = game_dir / app_name
    candidates_override = [
        bundle / "Contents" / "KOTOR Data" / "Override",
        bundle / "Contents" / "Resources" / "Override",
        bundle / "Contents" / "Resources" / "override",
    ]
    candidates_data = [
        bundle / "Contents" / "Assets",
        bundle / "Contents" / "Resources" / "data",
    ]
    override = next((p for p in candidates_override if p.exists()), candidates_override[0])
    data = next((p for p in candidates_data if (p / "chitin.key").exists()),
                candidates_data[0])
    saves = _find_saves_dir(game_dir, bundle)
    return {"root": game_dir, "override": override, "data": data, "saves": saves}


INSTALLS: Dict[str, Dict[str, Path]] = {
    "K1": _find_install(STEAM_COMMON / "swkotor", "Knights of the Old Republic.app"),
    "K2": _find_install(
        STEAM_COMMON / "Knights of the Old Republic II",
        "Knights of the Old Republic II.app",
    ),
}

if os.environ.get("KOTOR_EDITOR_ROOT"):
    dev = Path(os.environ["KOTOR_EDITOR_ROOT"]).expanduser()
    INSTALLS = {
        "K1": {"root": dev / "K1", "override": dev / "K1" / "Override",
               "data": dev / "K1" / "data", "saves": dev / "K1" / "saves"},
        "K2": {"root": dev / "K2", "override": dev / "K2" / "Override",
               "data": dev / "K2" / "data", "saves": dev / "K2" / "saves"},
    }


KIND_RESTYPE = {
    "2da": keybif.RESTYPE_2DA,
    "uti": keybif.RESTYPE_UTI,
    "utc": keybif.RESTYPE_UTC,
}
EDITABLE_EXTS = {".2da", ".uti", ".utc", ".bic", ".res", ".ifo"}


app = FastAPI(title="KOTOR mod editor")


class SaveRequest(BaseModel):
    twoda: dict | None = None
    gff: dict | None = None


def _safe_name(name: str) -> None:
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "Invalid filename")


def _resolve_override(install: str, name: str) -> Path:
    if install not in INSTALLS:
        raise HTTPException(404, f"Unknown install: {install}")
    _safe_name(name)
    if Path(name).suffix.lower() not in EDITABLE_EXTS:
        raise HTTPException(400, f"Unsupported extension: {name}")
    return INSTALLS[install]["override"] / name


@app.get("/api/installs")
def list_installs():
    out = []
    for key, paths in INSTALLS.items():
        out.append({
            "key": key,
            "label": "KOTOR 1" if key == "K1" else "KOTOR 2",
            "root": str(paths["root"]),
            "override": str(paths["override"]),
            "data": str(paths["data"]),
            "saves": str(paths.get("saves", "")),
            "exists": paths["override"].exists(),
            "data_exists": (paths["data"] / "chitin.key").exists(),
            "saves_exists": paths.get("saves") and paths["saves"].exists(),
        })
    return out


def _read_savenfo_name(save_dir: Path) -> str | None:
    """Best-effort read of the player-visible save name from savenfo.res.

    savenfo.res is a small GFF with a SAVEGAMENAME (or similar) locstring.
    Field name varies (SAVEGAMENAME, SAVE_GAME_NAME, AREANAME)."""
    candidates = [save_dir / "savenfo.res", save_dir / "SAVENFO.res"]
    for sf in candidates:
        if not sf.exists():
            continue
        try:
            doc = gffmod.load(sf)
        except Exception:
            continue
        root = doc.get("_struct", {})
        for label in ("SAVEGAMENAME", "SAVE_GAME_NAME", "AREANAME", "PCNAME"):
            entry = root.get(label)
            if not entry:
                continue
            val = entry["value"]
            if isinstance(val, dict) and val.get("substrings"):
                return val["substrings"][0]["text"]
            if isinstance(val, str):
                return val
        return None
    return None


SAVE_FILE_KINDS = {
    "savenfo.res": ("Save info", "nfo"),
    "partytable.res": ("Party + credits + XP + journal", "pt"),
    "globalvars.res": ("Plot flags / global variables", "gvt"),
    "pifo.ifo": ("Module info", "ifo"),
    "savegame.sav": ("Save archive (PC, equipment, module state)", "sav"),
}


def _classify_save_file(name: str) -> tuple[str, str]:
    lower = name.lower()
    if lower in SAVE_FILE_KINDS:
        return SAVE_FILE_KINDS[lower]
    if lower.endswith(".sav"):
        return ("Module archive", "sav")
    if lower.endswith(".res"):
        return ("Resource file", "res")
    if lower.endswith(".ifo"):
        return ("Info file", "ifo")
    return ("File", "other")


@app.get("/api/saves")
def list_saves(install: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    saves_dir = INSTALLS[install].get("saves")
    if not saves_dir or not saves_dir.exists():
        return {"saves_dir": str(saves_dir) if saves_dir else "", "saves": [], "exists": False}
    out = []
    for sub in sorted(saves_dir.iterdir()):
        if not sub.is_dir():
            continue
        name = _read_savenfo_name(sub) or sub.name
        out.append({
            "folder": sub.name,
            "display_name": name,
            "mtime": sub.stat().st_mtime,
        })
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return {"saves_dir": str(saves_dir), "saves": out, "exists": True}


PC_LOCATIONS = [
    # (archive_chain, inner_resref, inner_extension, gff_path)
    # archive_chain is a list of entry filenames to descend through inside
    # SAVEGAME.sav. gff_path is the field path INSIDE that resource, e.g.
    # "Mod_PlayerList[0]".
    {"chain": ["END_M01AA.2057"], "entry": "Module", "ext": "ifo",
     "path": "Mod_PlayerList[0]",
     "description": "PC in Module.ifo Mod_PlayerList"},
]


def _module_sav_in(archive: erfmod.ERF) -> str | None:
    """Find the nested module .sav inside SAVEGAME.sav (e.g. END_M01AA.2057)."""
    for e in archive.entries:
        if _is_erf_bytes(e.data):
            return e.filename
    return None


def _resolve_gff_path(root: dict, path: str) -> tuple[dict, str]:
    """Walk dotted path with [i] indexing, return (parent_struct, last_key).
    For "Mod_PlayerList[0]" returns the list at root level and the index "0".
    Actually we return the containing struct and a final-step descriptor.
    """
    # We support: "FieldName" or "FieldName[i]" or "A.B.C" etc.
    parts: list[tuple[str, int | None]] = []
    for raw in path.split("."):
        m = raw.strip()
        idx = None
        if "[" in m and m.endswith("]"):
            base, _, rest = m.partition("[")
            idx = int(rest.rstrip("]"))
            m = base
        parts.append((m, idx))
    node = root
    for i, (label, idx) in enumerate(parts):
        if i == len(parts) - 1:
            return node, label if idx is None else f"{label}[{idx}]"
        field = node[label]
        if idx is None:
            node = field["value"]
        else:
            node = field["value"][idx]
    return node, ""


def _get_at_path(root: dict, path: str) -> dict:
    parent, key = _resolve_gff_path(root, path)
    if "[" in key:
        label, _, rest = key.partition("[")
        idx = int(rest.rstrip("]"))
        return parent[label]["value"][idx]
    return parent[key]["value"] if isinstance(parent[key], dict) and "value" in parent[key] else parent[key]


def _set_at_path(root: dict, path: str, new_struct: dict) -> None:
    parent, key = _resolve_gff_path(root, path)
    if "[" in key:
        label, _, rest = key.partition("[")
        idx = int(rest.rstrip("]"))
        parent[label]["value"][idx] = new_struct
    else:
        parent[key]["value"] = new_struct


def _find_pc_location(install: str, folder: str) -> dict | None:
    """Locate the PC inside a save. Returns a descriptor or None.

    K2 puts the PC as a top-level pc.utc inside SAVEGAME.sav.
    K1 buries it inside the module archive's Module.ifo Mod_PlayerList.
    We try the K2 layout first since it's simpler and more common in TSL.
    """
    saves_dir = INSTALLS[install].get("saves")
    if not saves_dir:
        return None
    save_path = saves_dir / folder / "SAVEGAME.sav"
    if not save_path.exists():
        return None
    try:
        top = erfmod.load(save_path)
    except Exception:
        return None

    # --- K2 layout: top-level pc.utc ---
    pc_entry = next(
        (e for e in top.entries if e.filename.lower() == "pc.utc"), None
    )
    if pc_entry:
        try:
            doc = gffmod.loads(pc_entry.data)
        except Exception:
            return None
        return {
            "layout": "top_level_pc_utc",
            "archive": "SAVEGAME.sav",
            "inner_chain": [],
            "entry_resref": pc_entry.resref,
            "entry_res_type": pc_entry.res_type,
            "entry_extension": pc_entry.extension,
            "gff_path": "",  # whole struct is the PC
            "pc_struct": doc["_struct"],
            "doc_type": doc["_type"],
            "doc_version": doc["_version"],
        }

    # --- K1 layout: Module.ifo Mod_PlayerList[0] inside nested archive ---
    module_archive_name = _module_sav_in(top)
    if not module_archive_name:
        return None
    mod_entry = next((e for e in top.entries if e.filename == module_archive_name), None)
    if not mod_entry:
        return None
    try:
        inner = _erf_loads(mod_entry.data)
    except Exception:
        return None
    ifo = next((e for e in inner.entries if e.filename.lower() == "module.ifo"), None)
    if not ifo:
        return None
    try:
        doc = gffmod.loads(ifo.data)
    except Exception:
        return None
    root = doc["_struct"]
    if "Mod_PlayerList" not in root:
        return None
    player_list = root["Mod_PlayerList"]["value"]
    if not player_list:
        return None
    return {
        "layout": "nested_module_ifo",
        "archive": "SAVEGAME.sav",
        "inner_chain": [module_archive_name],
        "entry_resref": ifo.resref,
        "entry_res_type": ifo.res_type,
        "entry_extension": ifo.extension,
        "gff_path": "Mod_PlayerList[0]",
        "pc_struct": player_list[0],
        "doc_type": doc["_type"],
        "doc_version": doc["_version"],
    }


@app.get("/api/pc")
def get_pc(install: str, folder: str):
    """Return the player character struct, wrapped as a standalone GFF doc
    so the existing editor renders it with the PC quick-edit form."""
    loc = _find_pc_location(install, folder)
    if not loc:
        raise HTTPException(404, "Couldn't find a PC entry in this save")
    # Wrap pc_struct as a top-level GFF doc with type "BIC " so the editor
    # picks up the bic schema (which is what the PC fields match).
    wrapper = {
        "_type": "BIC ",
        "_version": "V3.2",
        "_struct": loc["pc_struct"],
    }
    display = _gff_display_name(install, "bic", wrapper)
    return {
        "kind": "gff",
        "gff_kind": "bic",
        "display_name": display,
        "doc": wrapper,
        "location": {
            "archive": loc["archive"],
            "inner_chain": loc["inner_chain"],
            "entry_resref": loc["entry_resref"],
            "entry_res_type": loc["entry_res_type"],
            "gff_path": loc["gff_path"],
        },
    }


# Earlier the GFF reader silently dropped duplicate-labelled fields
# (BonusForcePoints/AssignedPup/PlayerCreated appear twice in K2 PCs),
# which corrupted K2 saves on write. Fixed in gff.py — duplicates now
# preserved via "#N" internal suffix. Leaving the guard hook in place
# in case we need to disable writes again on short notice.
def _guard_k2_save_write(install: str) -> None:
    pass


@app.post("/api/pc")
def save_pc(install: str, folder: str, body: SaveRequest):
    """Save the edited PC back to disk. Handles K2 (top-level pc.utc) and
    K1 (nested Module.ifo Mod_PlayerList) layouts."""
    if body.gff is None:
        raise HTTPException(400, "Missing gff payload")
    loc = _find_pc_location(install, folder)
    if not loc:
        raise HTTPException(404, "Couldn't find a PC entry in this save")

    save_path = INSTALLS[install]["saves"] / folder / "SAVEGAME.sav"
    top = erfmod.load(save_path)

    if loc["layout"] == "top_level_pc_utc":
        # Wrap the edited struct as a UTC and replace the entry directly.
        new_pc = {
            "_type": loc["doc_type"],
            "_version": loc["doc_version"],
            "_struct": body.gff["_struct"],
        }
        new_bytes = gffmod.dumps(new_pc)
        erfmod.replace_entry(top, loc["entry_resref"], loc["entry_res_type"], new_bytes)
    else:
        # K1: walk into module archive, modify Module.ifo, repack
        module_archive_name = loc["inner_chain"][0]
        module_entry = next(e for e in top.entries if e.filename == module_archive_name)
        inner = _erf_loads(module_entry.data)
        ifo_entry = next(
            e for e in inner.entries
            if e.resref.lower() == loc["entry_resref"].lower()
            and e.res_type == loc["entry_res_type"]
        )
        doc = gffmod.loads(ifo_entry.data)
        _set_at_path(doc["_struct"], loc["gff_path"], body.gff["_struct"])
        new_ifo_bytes = gffmod.dumps(doc)
        erfmod.replace_entry(inner, ifo_entry.resref, ifo_entry.res_type, new_ifo_bytes)
        new_module_bytes = erfmod.dumps(inner)
        erfmod.replace_entry(top, module_entry.resref, module_entry.res_type, new_module_bytes)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(save_path, save_path.with_suffix(save_path.suffix + f".bak.{stamp}"))
    erfmod.save(save_path, top)
    return {"saved": str(save_path)}


@app.get("/api/save_files")
def list_save_files(install: str, folder: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    _safe_name(folder)
    saves_dir = INSTALLS[install].get("saves")
    if not saves_dir:
        raise HTTPException(404, "Saves dir not detected")
    folder_path = saves_dir / folder
    if not folder_path.exists():
        raise HTTPException(404, f"Save folder not found: {folder}")
    files = []
    # Synthesised PC entry at the top — lifts the actual character out of
    # whichever nested archive it lives in.
    loc = _find_pc_location(install, folder)
    if loc:
        wrapper = {
            "_type": "BIC ",
            "_version": "V3.2",
            "_struct": loc["pc_struct"],
        }
        display = _gff_display_name(install, "bic", wrapper) or "Player Character"
        files.append({
            "name": "__PC__",
            "label": f"Player Character — {display}",
            "kind": "pc",
            "size": 0,
            "is_pc": True,
        })
    for p in sorted(folder_path.iterdir()):
        if not p.is_file():
            continue
        label, kind = _classify_save_file(p.name)
        files.append({
            "name": p.name,
            "label": label,
            "kind": kind,
            "size": p.stat().st_size,
        })
    return {"folder": folder, "files": files}


def _resolve_save_file(install: str, folder: str, name: str) -> Path:
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    _safe_name(folder)
    _safe_name(name)
    saves_dir = INSTALLS[install].get("saves")
    if not saves_dir:
        raise HTTPException(404, "Saves dir not detected")
    p = saves_dir / folder / name
    if not p.exists():
        raise HTTPException(404, f"Save file not found: {folder}/{name}")
    return p


def _gff_kind_for_save_file(name: str) -> str:
    """Pick a schema kind based on the file's role inside a save folder."""
    lower = name.lower()
    if lower == "partytable.res":
        return "pt"
    if lower == "globalvars.res":
        return "gvt"
    if lower == "savenfo.res":
        return "nfo"
    if lower == "pifo.ifo":
        return "ifo"
    if lower.endswith(".bic"):
        return "bic"
    if lower.endswith(".utc"):
        return "utc"
    if lower.endswith(".uti"):
        return "uti"
    return lower.rsplit(".", 1)[-1] if "." in lower else "raw"


@app.get("/api/save_file")
def read_save_file(install: str, folder: str, name: str):
    p = _resolve_save_file(install, folder, name)
    try:
        doc = gffmod.load(p)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse {name}: {e}")
    gff_kind = _gff_kind_for_save_file(name)
    display_name = _gff_display_name(install, gff_kind, doc)
    return {
        "path": str(p),
        "kind": "gff",
        "gff_kind": gff_kind,
        "display_name": display_name,
        "doc": doc,
    }


@app.post("/api/save_file")
def write_save_file(install: str, folder: str, name: str, body: SaveRequest):
    _guard_k2_save_write(install)
    p = _resolve_save_file(install, folder, name)
    if body.gff is None:
        raise HTTPException(400, "Missing gff payload")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, p.with_suffix(p.suffix + f".bak.{stamp}"))
    try:
        gffmod.save(p, body.gff)
    except Exception as e:
        raise HTTPException(400, f"Invalid GFF payload: {e}")
    return {"saved": str(p)}


# ----- ERF archive (.sav) browsing -----

def _is_gff_bytes(data: bytes) -> bool:
    """A GFF starts with 4 bytes of type tag + 4 bytes of version like V3.2."""
    return len(data) >= 8 and data[4:8] == b"V3.2"


def _is_erf_bytes(data: bytes) -> bool:
    return len(data) >= 8 and data[0:4] in (b"ERF ", b"MOD ", b"SAV ") and data[4:8] == b"V1.0"


def _walk_archive(top_path: Path, inner_path: list[str]) -> erfmod.ERF:
    """Open top_path, then descend through each entry name in inner_path."""
    arch = erfmod.load(top_path)
    for step in inner_path:
        # step is "RESREF.ext" — find the entry by filename
        stem, _, ext = step.rpartition(".")
        if not stem:
            stem = ext
            ext = ""
        match = None
        for e in arch.entries:
            if e.filename.lower() == step.lower() or e.resref.lower() == stem.lower():
                match = e
                break
        if not match:
            raise HTTPException(404, f"{step} not found in archive")
        if not _is_erf_bytes(match.data):
            raise HTTPException(400, f"{step} is not a nested archive")
        arch = erfmod.loads(match.data) if hasattr(erfmod, "loads") else _erf_loads(match.data)
    return arch


def _erf_loads(data: bytes) -> erfmod.ERF:
    """Parse an ERF from bytes (we have erfmod.load for files only)."""
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sav") as tf:
        tf.write(data)
        tmp = tf.name
    try:
        return erfmod.load(Path(tmp))
    finally:
        os.unlink(tmp)


def _list_archive_entries(arch: erfmod.ERF, install: str) -> list[dict]:
    entries = []
    for e in arch.entries:
        is_gff = _is_gff_bytes(e.data)
        is_erf = _is_erf_bytes(e.data)
        display = None
        if is_gff:
            try:
                doc = gffmod.loads(e.data)
                # display name pulled from FirstName/LastName for UTC, LocalizedName for UTI
                ext = e.extension if e.extension in ("utc", "uti", "bic") else "utc"
                display = _gff_display_name(install, ext, doc)
            except Exception:
                pass
        entries.append({
            "resref": e.resref,
            "res_type": e.res_type,
            "extension": e.extension,
            "filename": e.filename,
            "size": len(e.data),
            "is_gff": is_gff,
            "is_archive": is_erf,
            "display_name": display,
        })
    entries.sort(key=lambda e: (not (e["is_gff"] or e["is_archive"]),
                                e["extension"], e["resref"]))
    return entries


def _split_inner(path_q: str) -> list[str]:
    """Inner-archive path is sent as `a.sav|b.sav|...`; split safely."""
    if not path_q:
        return []
    return [p for p in path_q.split("|") if p]


@app.get("/api/save_archive")
def list_save_archive(install: str, folder: str, name: str, inner: str = ""):
    """List contents of a .sav (or nested archive inside it)."""
    p = _resolve_save_file(install, folder, name)
    try:
        arch = _walk_archive(p, _split_inner(inner))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Failed to parse archive: {e}")
    return {
        "archive": str(p),
        "inner": inner,
        "type": arch.file_type,
        "count": len(arch.entries),
        "entries": _list_archive_entries(arch, install),
    }


@app.get("/api/save_archive_entry")
def read_save_archive_entry(
    install: str, folder: str, archive: str, resref: str, res_type: int,
    inner: str = "",
):
    p = _resolve_save_file(install, folder, archive)
    try:
        arch = _walk_archive(p, _split_inner(inner))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Failed to parse archive: {e}")
    entry = arch.find(resref, res_type)
    if not entry:
        raise HTTPException(404, f"{resref} (type {res_type}) not in archive")
    if not _is_gff_bytes(entry.data):
        raise HTTPException(400, "This resource isn't a GFF — only GFF entries are editable here")
    try:
        doc = gffmod.loads(entry.data)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse entry: {e}")
    kind = entry.extension
    display = _gff_display_name(install, kind, doc)
    return {
        "archive": str(p),
        "resref": resref,
        "res_type": res_type,
        "extension": entry.extension,
        "kind": "gff",
        "gff_kind": kind,
        "display_name": display,
        "doc": doc,
    }


@app.post("/api/save_archive_entry")
def write_save_archive_entry(
    install: str, folder: str, archive: str, resref: str, res_type: int,
    body: SaveRequest, inner: str = "",
):
    _guard_k2_save_write(install)
    p = _resolve_save_file(install, folder, archive)
    if body.gff is None:
        raise HTTPException(400, "Missing gff payload")
    inner_path = _split_inner(inner)
    # Load every level so we can rewrite all the way back up
    try:
        levels: list[erfmod.ERF] = [erfmod.load(p)]
        for step in inner_path:
            parent = levels[-1]
            match = next(
                (e for e in parent.entries if e.filename.lower() == step.lower()
                 or e.resref.lower() == step.lower().rsplit(".", 1)[0]),
                None,
            )
            if not match:
                raise HTTPException(404, f"{step} not found")
            levels.append(_erf_loads(match.data))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Failed to parse archive chain: {e}")

    target = levels[-1]
    if not target.find(resref, res_type):
        raise HTTPException(404, "Entry not found")
    try:
        new_bytes = gffmod.dumps(body.gff)
    except Exception as e:
        raise HTTPException(400, f"Invalid GFF payload: {e}")
    erfmod.replace_entry(target, resref, res_type, new_bytes)

    # Roll the changes back up: rewrite each level into its parent
    for i in range(len(levels) - 1, 0, -1):
        child_bytes = erfmod.dumps(levels[i])
        parent = levels[i - 1]
        step = inner_path[i - 1]
        # find the entry that corresponds to this nested archive
        stem = step.lower().rsplit(".", 1)[0]
        for entry in parent.entries:
            if entry.filename.lower() == step.lower() or entry.resref.lower() == stem:
                entry.data = child_bytes
                break

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(p, p.with_suffix(p.suffix + f".bak.{stamp}"))
    try:
        erfmod.save(p, levels[0])
    except Exception as e:
        raise HTTPException(500, f"Failed to write archive: {e}")
    return {"saved": str(p), "entry": f"{resref}.{erfmod.RES_EXT.get(res_type, res_type)}"}


@app.get("/api/files")
def list_files(install: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    override = INSTALLS[install]["override"]
    if not override.exists():
        return {"override": str(override), "files": [], "exists": False}
    # Resolve display names per kind once, then enrich each entry
    names_by_kind = {
        "uti": _names_for(install, "override", "uti"),
        "utc": _names_for(install, "override", "utc"),
    }
    files = []
    for p in sorted(override.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in EDITABLE_EXTS:
            continue
        rules = {".uti": UTI_CATEGORIES, ".utc": UTC_CATEGORIES, ".2da": TWODA_CATEGORIES}[ext]
        names = names_by_kind.get(ext.lstrip("."), {})
        files.append({
            "name": p.name,
            "category": _categorise(p.name, rules),
            "display_name": names.get(p.stem.lower()),
        })
    return {"override": str(override), "files": files, "exists": True}


UTI_CATEGORIES = [
    ("g_w_lghtsbr", "Lightsabers"),
    ("g_w_dblsbr", "Lightsabers (double)"),
    ("g_w_shortsbr", "Lightsabers (short)"),
    ("g_w_blstr", "Blasters"),
    ("g_w_rptngblstr", "Repeating blasters"),
    ("g_w_hvyblstr", "Heavy weapons"),
    ("g_w_rifl", "Rifles"),
    ("g_w_qtrstff", "Quarterstaves"),
    ("g_w_vbroswrd", "Vibroswords"),
    ("g_w_vbrblde", "Vibroblades"),
    ("g_w_vbrdblswd", "Vibro double-blades"),
    ("g_w_", "Other weapons"),
    ("g_a_", "Armor"),
    ("g_i_belt", "Belts"),
    ("g_i_implant", "Implants"),
    ("g_i_mask", "Masks / headgear"),
    ("g_i_gauntlet", "Gauntlets"),
    ("g_i_glove", "Gloves"),
    ("g_i_frarmbnd", "Armbands"),
    ("g_i_drdupgrd", "Droid upgrades"),
    ("g_i_drdutl", "Droid utilities"),
    ("g_i_", "Other items"),
    ("g_", "Misc"),
]

UTC_CATEGORIES = [
    ("n_", "Named NPCs"),
    ("p_", "Party / PCs"),
    ("c_", "Creatures"),
    ("g_", "Generic NPCs"),
    ("k_", "Plot NPCs"),
    ("m_", "Monsters"),
    ("t_", "Tutorial / test"),
]

TWODA_CATEGORIES = [
    ("cls_", "Class progression tables"),
    ("cre_", "Creature tables"),
    ("item", "Items"),
    ("baseitem", "Items"),
    ("appearance", "Appearance / models"),
    ("feat", "Feats"),
    ("spell", "Force powers"),
    ("skill", "Skills"),
    ("port", "Portraits"),
    ("amb", "Ambient / audio"),
    ("sound", "Audio"),
    ("vfx", "Visual FX"),
    ("anim", "Animations"),
    ("plac", "Placeables"),
    ("door", "Doors"),
]


def _categorise(name: str, rules: list[tuple[str, str]]) -> str:
    lower = name.lower()
    for prefix, cat in rules:
        if lower.startswith(prefix):
            return cat
    return "Other"


@app.get("/api/vanilla")
def list_vanilla(install: str, kind: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    if kind not in KIND_RESTYPE:
        raise HTTPException(400, f"Unsupported kind: {kind}")
    data_dir = INSTALLS[install]["data"]
    if not (data_dir / "chitin.key").exists():
        raise HTTPException(404, f"chitin.key not found in {data_dir}")
    try:
        resources = keybif.vanilla_resources(data_dir, KIND_RESTYPE[kind])
    except Exception as e:
        raise HTTPException(500, f"Failed to read chitin.key: {e}")
    rules = {"uti": UTI_CATEGORIES, "utc": UTC_CATEGORIES, "2da": TWODA_CATEGORIES}[kind]
    names = _names_for(install, "vanilla", kind)
    files = []
    for resref in sorted(resources.keys()):
        name = f"{resref}.{kind}"
        files.append({
            "name": name,
            "category": _categorise(name, rules),
            "display_name": names.get(resref.lower()),
        })
    return {"kind": kind, "count": len(files), "files": files}


@app.post("/api/extract")
def extract_vanilla(install: str, kind: str, name: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    if kind not in KIND_RESTYPE:
        raise HTTPException(400, f"Unsupported kind: {kind}")
    _safe_name(name)
    if not name.lower().endswith("." + kind):
        raise HTTPException(400, f"Name must end with .{kind}")

    data_dir = INSTALLS[install]["data"]
    override = INSTALLS[install]["override"]
    override.mkdir(parents=True, exist_ok=True)

    resref = Path(name).stem.lower()
    try:
        resources = keybif.vanilla_resources(data_dir, KIND_RESTYPE[kind])
    except Exception as e:
        raise HTTPException(500, f"Failed to read chitin.key: {e}")

    if resref not in resources:
        raise HTTPException(404, f"{name} not found in vanilla resources")
    bif_path, res_index = resources[resref]
    try:
        data, _ = keybif.extract_resource(bif_path, res_index)
    except Exception as e:
        raise HTTPException(500, f"Extract failed: {e}")

    target = override / name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(target, target.with_suffix(target.suffix + f".bak.{stamp}"))
    target.write_bytes(data)
    return {"extracted": str(target), "size": len(data)}


@app.get("/api/file")
def read_file(install: str, name: str):
    path = _resolve_override(install, name)
    if not path.exists():
        raise HTTPException(404, f"File not found in Override: {name}")
    ext = path.suffix.lower()
    try:
        if ext == ".2da":
            twoda = load_2da(path)
            return {"path": str(path), "kind": "2da", **twoda.to_dict()}
        else:
            doc = gffmod.load(path)
            display_name = _gff_display_name(install, ext.lstrip("."), doc)
            return {
                "path": str(path),
                "kind": "gff",
                "gff_kind": ext.lstrip("."),
                "display_name": display_name,
                "doc": doc,
            }
    except Exception as e:
        raise HTTPException(400, f"Failed to parse {name}: {e}")


def _gff_display_name(install: str, kind: str, doc: dict) -> str | None:
    """Pull a human-readable display name out of a GFF, resolving StrRefs."""
    global _gff_schema_cache
    if _gff_schema_cache is None:
        _gff_schema_cache = json.loads(GFF_SCHEMA_PATH.read_text())
    sch = _gff_schema_cache.get(kind, {})
    name_fields = sch.get("_displayName", [])
    tlk = _tlk_for_install(install)
    parts: list[str] = []
    root = doc.get("_struct", {})
    for fname in name_fields:
        entry = root.get(fname)
        if not entry:
            continue
        val = entry["value"]
        if isinstance(val, dict) and "substrings" in val:
            # LocalizedName: prefer the inline substring, else resolve strref
            subs = val.get("substrings") or []
            if subs and subs[0].get("text"):
                parts.append(subs[0]["text"])
                continue
            strref = val.get("strref", 0xFFFFFFFF)
            if tlk and strref != 0xFFFFFFFF:
                text = tlk.get(strref)
                if text:
                    parts.append(text)
        elif isinstance(val, str):
            parts.append(val)
    return " ".join(p for p in parts if p) or None


# (install, kind) -> { resref: display_name }
_name_cache: dict[tuple[str, str, str], dict[str, str]] = {}


def _names_for(install: str, source: str, kind: str) -> dict[str, str]:
    """Resolve display names for every file of `kind` in `source` (override or
    vanilla). Cached because scanning thousands of GFFs is not free."""
    key = (install, source, kind)
    if key in _name_cache:
        return _name_cache[key]
    if kind not in ("uti", "utc"):
        _name_cache[key] = {}
        return _name_cache[key]

    out: dict[str, str] = {}
    if source == "override":
        override = INSTALLS[install]["override"]
        if override.exists():
            for p in override.iterdir():
                if p.is_file() and p.suffix.lower() == "." + kind:
                    try:
                        doc = gffmod.load(p)
                        name = _gff_display_name(install, kind, doc)
                        if name:
                            out[p.stem.lower()] = name
                    except Exception:
                        continue
    else:  # vanilla
        data_dir = INSTALLS[install]["data"]
        if not (data_dir / "chitin.key").exists():
            _name_cache[key] = {}
            return _name_cache[key]
        try:
            resources = keybif.vanilla_resources(data_dir, KIND_RESTYPE[kind])
        except Exception:
            _name_cache[key] = {}
            return _name_cache[key]
        # Group by bif file so we only read each BIF once
        by_bif: dict[Path, list[tuple[str, int]]] = {}
        for resref, (bif_path, ridx) in resources.items():
            by_bif.setdefault(bif_path, []).append((resref, ridx))
        import struct as _struct
        for bif_path, entries in by_bif.items():
            try:
                raw = bif_path.read_bytes()
            except Exception:
                continue
            if not raw.startswith(b"BIFFV1"):
                continue
            var_count, _, var_off = _struct.unpack_from("<III", raw, 8)
            for resref, ridx in entries:
                if ridx >= var_count:
                    continue
                _, offset, size, _ = _struct.unpack_from(
                    "<IIII", raw, var_off + ridx * 16
                )
                data = raw[offset : offset + size]
                try:
                    doc = gffmod.loads(data)
                    name = _gff_display_name(install, kind, doc)
                    if name:
                        out[resref.lower()] = name
                except Exception:
                    continue
    _name_cache[key] = out
    return out


@app.post("/api/file")
def write_file(install: str, name: str, body: SaveRequest):
    path = _resolve_override(install, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()

    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak.{stamp}"))

    if ext == ".2da":
        if body.twoda is None:
            raise HTTPException(400, "Missing twoda payload")
        try:
            twoda = TwoDA.from_dict(body.twoda)
        except Exception as e:
            raise HTTPException(400, f"Invalid .2da payload: {e}")
        save_ascii(twoda, path)
        return {"saved": str(path), "rows": len(twoda.rows), "columns": len(twoda.columns)}

    if body.gff is None:
        raise HTTPException(400, "Missing gff payload")
    try:
        gffmod.save(path, body.gff)
    except Exception as e:
        raise HTTPException(400, f"Invalid GFF payload: {e}")
    return {"saved": str(path)}


_schema_cache: dict | None = None
_gff_schema_cache: dict | None = None
_enums_cache: dict | None = None


@app.get("/api/schema")
def get_schema(name: str):
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text())
    return _schema_cache.get(name.lower(), {})


@app.get("/api/gff_schema")
def get_gff_schema(kind: str):
    global _gff_schema_cache, _enums_cache
    if _gff_schema_cache is None:
        _gff_schema_cache = json.loads(GFF_SCHEMA_PATH.read_text())
    if _enums_cache is None:
        _enums_cache = json.loads(ENUMS_PATH.read_text())
    schema = _gff_schema_cache.get(kind.lower(), {})
    return {"schema": schema, "enums": _enums_cache}


def _tlk_for_install(install: str) -> tlkmod.TLK | None:
    if install not in INSTALLS:
        return None
    bundle_contents = INSTALLS[install]["data"].parent
    p = tlkmod.find_dialog_tlk(bundle_contents)
    if not p:
        return None
    try:
        return tlkmod.load_cached(p)
    except Exception:
        return None


@app.get("/api/strref")
def resolve_strref(install: str, strref: int):
    tlk = _tlk_for_install(install)
    if not tlk:
        return {"strref": strref, "text": None, "error": "dialog.tlk not found"}
    return {"strref": strref, "text": tlk.get(strref)}


@app.get("/")
def index():
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
