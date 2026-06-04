"""
KOTOR .2da web editor — FastAPI backend.

Endpoints:
    GET  /api/installs          List detected KOTOR 1/2 installs on macOS
    GET  /api/files?install=K1  List .2da files in that install's Override
    GET  /api/file?install=K1&name=classes.2da   Read a .2da as JSON
    POST /api/file?install=K1&name=classes.2da   Save edits (backs up first)
    GET  /                      Serve the single-page editor
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from twoda import TwoDA, load, save_ascii


HERE = Path(__file__).resolve().parent
CLIENT_DIR = HERE.parent / "client"

HOME = Path(os.path.expanduser("~"))
STEAM_COMMON = HOME / "Library" / "Application Support" / "Steam" / "steamapps" / "common"

# Override folder lives inside the .app bundle on macOS
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

# Dev override: KOTOR_EDITOR_ROOT=/some/path lets you point at a sandbox dir
# containing K1/ and K2/ subfolders for testing without a real install.
if os.environ.get("KOTOR_EDITOR_ROOT"):
    dev = Path(os.environ["KOTOR_EDITOR_ROOT"]).expanduser()
    INSTALLS = {
        "K1": {"root": dev / "K1", "override": dev / "K1" / "Override", "data": dev / "K1" / "data"},
        "K2": {"root": dev / "K2", "override": dev / "K2" / "Override", "data": dev / "K2" / "data"},
    }


app = FastAPI(title="KOTOR .2da editor")


class SaveRequest(BaseModel):
    twoda: dict


def _resolve(install: str, name: str) -> Path:
    if install not in INSTALLS:
        raise HTTPException(404, f"Unknown install: {install}")
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "Invalid filename")
    if not name.lower().endswith(".2da"):
        raise HTTPException(400, "Not a .2da file")
    override = INSTALLS[install]["override"]
    return override / name


@app.get("/api/installs")
def list_installs():
    out = []
    for key, paths in INSTALLS.items():
        out.append({
            "key": key,
            "label": "KOTOR 1" if key == "K1" else "KOTOR 2",
            "root": str(paths["root"]),
            "override": str(paths["override"]),
            "exists": paths["override"].exists(),
        })
    return out


@app.get("/api/files")
def list_files(install: str):
    if install not in INSTALLS:
        raise HTTPException(404, "Unknown install")
    override = INSTALLS[install]["override"]
    if not override.exists():
        return {"override": str(override), "files": [], "exists": False}
    files = sorted(p.name for p in override.iterdir() if p.suffix.lower() == ".2da")
    return {"override": str(override), "files": files, "exists": True}


@app.get("/api/file")
def read_file(install: str, name: str):
    path = _resolve(install, name)
    if not path.exists():
        raise HTTPException(404, f"File not found in Override: {name}")
    try:
        twoda = load(path)
    except Exception as e:
        raise HTTPException(400, f"Failed to parse {name}: {e}")
    return {"path": str(path), **twoda.to_dict()}


@app.post("/api/file")
def write_file(install: str, name: str, body: SaveRequest):
    path = _resolve(install, name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_suffix(path.suffix + f".bak.{stamp}")
        shutil.copy2(path, backup)

    try:
        twoda = TwoDA.from_dict(body.twoda)
    except Exception as e:
        raise HTTPException(400, f"Invalid .2da payload: {e}")

    save_ascii(twoda, path)
    return {"saved": str(path), "rows": len(twoda.rows), "columns": len(twoda.columns)}


# Serve the single-page client. Mounted last so /api routes win.
@app.get("/")
def index():
    return FileResponse(CLIENT_DIR / "index.html")


app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
