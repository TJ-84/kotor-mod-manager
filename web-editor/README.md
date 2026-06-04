# KOTOR .2da Web Editor

A small web app that lets you edit KOTOR / KOTOR 2 `.2da` files (class stats,
feats, items, Force powers, etc.) directly in the game's `Override/` folder.

## What it does

- Auto-detects KOTOR 1 and KOTOR 2 Steam installs on macOS
- Lists every `.2da` file currently in `Override/`
- Opens any of them in a spreadsheet-style editor
- Saves edits back as ASCII `.2da V2.0` (which the game reads fine)
- Makes a timestamped `.bak` of the original before every save

## Run it

```bash
cd web-editor
bash run.sh
```

Then open <http://localhost:8765> in your browser.

The first run creates a `.venv/` and installs FastAPI + uvicorn. After that
startup is instant.

## How editing works

1. Pick **KOTOR 1** or **KOTOR 2** in the top-left dropdown
2. Click a `.2da` from the sidebar (e.g. `classes.2da`)
3. Edit cells in the grid — changed cells turn yellow
4. Hit **Save**

The original file is copied to `<name>.2da.bak.<timestamp>` next to it before
the new version is written, so you can always roll back.

## Common edits

| Effect | File | Column / row |
|---|---|---|
| Soldier HP per level → d20 | `classes.2da` | row `0` (Soldier), col `hitdie` → `20` |
| Free skill points | `classes.2da` | col `skillpointbase` |
| Force points per level | `classes.2da` | col `forcedie` |
| XP curve | `exptable.2da` | edit `XP` column rows |
| Feat prerequisites | `feat.2da` | clear the `prereqfeat1` / `minlevel` cells |
| Force power damage | `spells.2da` | tweak relevant numeric cols |

## Getting `.2da` files into Override

This editor only sees files already in `Override/`. To edit a vanilla game
table, you first need to extract it from `data/2da.bif` using KOTOR Tool (or
HoloPatcher / pykotor) and drop the extracted `.2da` into `Override/`. Then
refresh this editor and it'll show up.

A future iteration could extract straight from `2da.bif`; for the MVP, it's
Override-only to keep the blast radius small.

## Sandbox mode (no game install needed)

For dev / testing without a real Steam install:

```bash
export KOTOR_EDITOR_ROOT=/tmp/kotor-sandbox
mkdir -p "$KOTOR_EDITOR_ROOT/K1/Override" "$KOTOR_EDITOR_ROOT/K2/Override"
# drop some sample .2da files into those Override dirs
bash run.sh
```

## Caveats

- macOS Steam paths only (KOTOR 1 = `swkotor`, KOTOR 2 = `Knights of the Old Republic II`)
- Reads both ASCII (`2DA V2.0`) and binary (`2DA V2.b`) `.2da` formats; always
  writes ASCII back
- Doesn't (yet) edit `.uti` items, `.utc` creatures, or extract from `.bif`
- No schema awareness yet — column meanings are just the raw header names
