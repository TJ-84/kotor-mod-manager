# KOTOR .2da Web Editor

A small web app that lets you edit KOTOR / KOTOR 2 `.2da` files (class stats,
feats, items, Force powers, etc.) directly in the game's `Override/` folder.

## What it does

- Auto-detects KOTOR 1 and KOTOR 2 Steam installs on macOS
- Two file sources in the sidebar:
  - **Override** — `.2da`, `.uti`, `.utc` files currently in your `Override/` folder
  - **Vanilla** — every resource indexed in `chitin.key` / the `.bif` archives, with a one-click **extract** button that copies it into `Override/` so you can edit it
- Spreadsheet-style editor for `.2da` files (class stats, feats, Force powers, etc.)
  - Column tooltips with descriptions for common tables (classes, feat, spells, baseitems, exptable, skills, appearance)
- Tree-style editor for GFF files (`.uti` items, `.utc` creatures) — edit scalar fields and localised names inline
- Saves `.2da` as ASCII `2DA V2.0`, saves GFF back as binary — both are what the game expects
- Makes a timestamped `.bak` next to the original before every save

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

## Getting vanilla files into Override

Click the **Vanilla** tab in the sidebar, pick a resource type (`.2da`, `.uti`,
or `.utc`), find the resource (filter is your friend — `.2da` has ~600 entries,
`.uti` is in the thousands), and hit **extract**. The file is copied into
`Override/` and opened for editing in one go.

Under the hood this parses `chitin.key`, finds the right `.bif` archive,
reads the resource's offset/size, and writes the bytes to disk.

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
- GFF editor exposes scalar fields and the default substring of localised
  strings; struct/list structure is browsable but adding/removing list items
  is not yet supported
- Column-description schema currently covers the most-edited tables
  (classes, feat, spells, baseitems, exptable, skills, appearance) — other
  tables show raw header names with no tooltip
- `.utc` / `.uti` are also packed inside `.rim` / `.mod` module files for
  specific maps; this editor only sees the global ones in `2da.bif` /
  `templates.bif`. Per-module overrides need a different flow.
