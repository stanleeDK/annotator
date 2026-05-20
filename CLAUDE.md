# annotater — Claude project context

## What this project is
A PyQt6 desktop app for drawing bounding boxes on shoe images and exporting
Labelbox-compatible NDJSON annotation files. Built by Stan Lee at GOAT.
Single file app: annotator.py (~900 lines). Run with `uv run python annotator.py`.

## Environment
- Always use `uv run` not plain `python` — e.g. `uv run python annotator.py`
- Dependencies managed via pyproject.toml / uv.lock
- Python 3.11+, PyQt6, Pillow, torch (for predictor)

## Key files
- `annotator.py`            — the entire app
- `predictor.py`            — ML model wrapper (D-FINE), plugs into the app
- `schema.json`             — label names, colors, Labelbox feature_schema_ids
- `fix_project_labels.py`   — migration script, rename "project"→"singleshoe" in NDJSONs

## Architecture in one paragraph
Main (QMainWindow) owns everything. It holds image_paths (list of Paths),
index (current position), annotations (dict keyed by filename — the in-memory
database), and _ndjson_all_rows (full backup of loaded file for passthrough on
save). AnnotationView (QGraphicsView) is the canvas — holds BoxItem objects
(QGraphicsRectItem) which each have 8 ResizeHandle children. Canvas and
annotations are kept in sync via load_current() (dict→canvas) and
_stash_current() (canvas→dict).

## Coordinate system — KNOWN ISSUE, NOT YET FIXED
Labelbox exports store bounding box coordinates in RAW image space (pre-EXIF
rotation). The app stores coordinates in DISPLAY space (post-exif_transpose).

Detection: if media_attributes width < height for exif_rotation 6/8 → app
format (display space). If width > height → Labelbox format (raw space).

Transform for EXIF rotation 6 (most common — iPhone portrait photos):
  raw → display:
    display_left   = raw_h - raw_top - raw_height
    display_top    = raw_left
    display_width  = raw_height
    display_height = raw_width

  display → raw (inverse):
    raw_left   = display_top
    raw_top    = raw_h - display_left - display_width
    raw_width  = display_height
    raw_height = display_width

Fix needed in: load_current() (raw→display when painting), _stash_current()
(display→raw when saving), _current_media (store raw dims not display dims).

## Known label bug — FIXED
current_label was initialized to "project" (first key in schema.json) because
the signal connection was made after addItems(). Fixed in _build_toolbar() —
connect signal before addItems. Existing NDJSON files with "name":"project"
boxes can be fixed with fix_project_labels.py.

## NDJSON format
The app reads and writes Labelbox export format (data_row + projects.<id>.labels
[].annotations). _normalize_row() handles both Labelbox export format and the
flat format. On save, _to_export_row() wraps everything back into full Labelbox
format. _ndjson_all_rows preserves all rows (including unvisited images) so they
are passed through unchanged on save.

## schema.json structure
Top-level keys with feature_schema_id = bounding box labels (drives label
dropdown + box color). "classifications" key = sidebar fields (SKU text,
Angle/Location radio). Schema is loaded once at startup into self.schema.

## Box colors
- Green  #00ff00 — drawn by user or predicted by model this session
- Purple #8b5cf6 — loaded from NDJSON file (needs review)

## Pending work
1. EXIF coordinate fix (described above) — most important for Labelbox compat
2. bulk_predict.py — headless CLI to run model on a folder, write NDJSON
3. Audit mode — load bulk predictions, step through, press A to approve

## Verification command (offscreen Qt test)
```bash
QT_QPA_PLATFORM=offscreen uv run python -c "
import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QRectF
import annotator
app = QApplication(sys.argv)
w = annotator.Main()
box_loaded = w.view.add_box(QRectF(0,0,100,100), 'singleshoe', source='loaded')
assert box_loaded.pen().color().name() == '#8b5cf6', box_loaded.pen().color().name()
box_model = w.view.add_box(QRectF(0,0,100,100), 'singleshoe', source='model')
assert box_model.pen().color().name() == '#00ff00', box_model.pen().color().name()
print('colors ok')
"
```

## Data folder
Annotation data lives under:
/Users/stan.lee/Documents/python/twodooh_cleandata/Jake Daniel Stan -Post Clean april 2026/

- App-generated files: annotations.ndjson inside each SKU subfolder
- Labelbox exports: Export project - Basketballshoes - 4_13_2026.ndjson
- Coordinate-converted copy: Export project - Basketballshoes - 4_13_2026_app_coords.ndjson
