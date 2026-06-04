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
    return {"root": game_dir, "override": override, "data": data}


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
        "K1": {"root": dev / "K1", "override": dev / "K1" / "Override", "data": dev / "K1" / "data"},
        "K2": {"root": dev / "K2", "override": dev / "K2" / "Override", "data": dev / "K2" / "data"},
    }


KIND_RESTYPE = {
    "2da": keybif.RESTYPE_2DA,
    "uti": keybif.RESTYPE_UTI,
    "utc": keybif.RESTYPE_UTC,
}
EDITABLE_EXTS = {".2da", ".uti", ".utc"}


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
            "exists": paths["override"].exists(),
            "data_exists": (paths["data"] / "chitin.key").exists(),
        })
    return out


@app.get("/api/files")
def list_files(install: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    override = INSTALLS[install]["override"]
    if not override.exists():
        return {"override": str(override), "files": [], "exists": False}
    files = []
    for p in sorted(override.iterdir()):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in EDITABLE_EXTS:
            continue
        rules = {".uti": UTI_CATEGORIES, ".utc": UTC_CATEGORIES, ".2da": TWODA_CATEGORIES}[ext]
        files.append({"name": p.name, "category": _categorise(p.name, rules)})
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
    files = []
    for resref in sorted(resources.keys()):
        name = f"{resref}.{kind}"
        files.append({"name": name, "category": _categorise(name, rules)})
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
