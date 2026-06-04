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
from twoda import TwoDA, load as load_2da, save_ascii


HERE = Path(__file__).resolve().parent
CLIENT_DIR = HERE.parent / "client"
SCHEMA_PATH = HERE / "schemas" / "columns.json"

HOME = Path(os.path.expanduser("~"))
STEAM_COMMON = HOME / "Library" / "Application Support" / "Steam" / "steamapps" / "common"

INSTALLS: Dict[str, Dict[str, Path]] = {
    "K1": {
        "root": STEAM_COMMON / "swkotor",
        "override": STEAM_COMMON / "swkotor" / "Knights of the Old Republic.app" / "Contents" / "Resources" / "Override",
        "data": STEAM_COMMON / "swkotor" / "Knights of the Old Republic.app" / "Contents" / "Resources" / "data",
    },
    "K2": {
        "root": STEAM_COMMON / "Knights of the Old Republic II",
        "override": STEAM_COMMON / "Knights of the Old Republic II" / "Knights of the Old Republic II.app" / "Contents" / "Resources" / "override",
        "data": STEAM_COMMON / "Knights of the Old Republic II" / "Knights of the Old Republic II.app" / "Contents" / "Resources" / "data",
    },
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
    files = sorted(
        p.name for p in override.iterdir()
        if p.is_file() and p.suffix.lower() in EDITABLE_EXTS
    )
    return {"override": str(override), "files": files, "exists": True}


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
    names = sorted(f"{resref}.{kind}" for resref in resources.keys())
    return {"kind": kind, "count": len(names), "files": names}


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
            return {"path": str(path), "kind": "gff", "doc": doc}
    except Exception as e:
        raise HTTPException(400, f"Failed to parse {name}: {e}")


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


@app.get("/api/schema")
def get_schema(name: str):
    global _schema_cache
    if _schema_cache is None:
        _schema_cache = json.loads(SCHEMA_PATH.read_text())
    return _schema_cache.get(name.lower(), {})


@app.get("/")
def index():
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
