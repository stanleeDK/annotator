#!/usr/bin/env python3
"""
annotater — draw bounding boxes on images; export Labelbox-compatible NDJSON.

Controls:
    Left/Right arrows : prev / next image
    Click-drag empty  : draw new box (uses current label)
    Click box         : select (drag body to move, drag handles to resize)
    D / Delete        : delete selected box
    Cmd/Ctrl + wheel  : zoom
    Cmd/Ctrl + S      : save NDJSON
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import (QAction, QBrush, QColor, QImage, QKeySequence,
                         QPainter, QPen, QPixmap)
from PyQt6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog,
                             QFormLayout, QGraphicsItem, QGraphicsPixmapItem,
                             QGraphicsRectItem, QGraphicsScene, QGraphicsView,
                             QLabel, QLineEdit, QMainWindow, QMessageBox,
                             QSizePolicy, QStatusBar, QToolBar, QWidget)

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
MIN_BOX_SIZE = 5
HANDLE_SIZE = 10
EXIF_ORIENTATION_TAG = 0x0112

PROJECT_ROOT = Path(__file__).parent
SCHEMA_PATH = PROJECT_ROOT / "schema.json"
CONFIG_PATH = PROJECT_ROOT / ".annotater_config.json"

DEFAULT_SCHEMA = {
    "singleshoe": {
        "feature_schema_id": "cmnqdth8e1ox70720h2io5rt9",
        "color": "#00ff00",
    }
}


def load_schema() -> dict:
    """Load the annotation schema from schema.json, writing defaults if absent.

    Returns:
        Dict mapping label names to schema metadata (feature_schema_id, color).
    Called by: Main.__init__() at startup.
    """
    if SCHEMA_PATH.exists():
        try:
            return json.loads(SCHEMA_PATH.read_text())
        except json.JSONDecodeError as exc:
            print(f"[warn] schema.json invalid ({exc}); falling back to defaults.")
            return DEFAULT_SCHEMA
    SCHEMA_PATH.write_text(json.dumps(DEFAULT_SCHEMA, indent=2))
    return DEFAULT_SCHEMA


def load_config() -> dict:
    """Load persisted user preferences from .annotater_config.json.

    Returns:
        Dict of saved settings (e.g. last_folder, last_image_dir), or {} if
        the file is missing or unreadable.
    Called by: open_folder(), open_image(), save_ndjson().
    """
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(data: dict):
    """Persist user preferences to .annotater_config.json, silently ignoring I/O errors.

    Args:
        data: Dict of settings to serialize (e.g. last_folder path).
    Called by: open_folder() and open_image() after the user picks a location.
    """
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def new_feature_id() -> str:
    """Generate a placeholder feature ID in Labelbox cuid style.

    Returns:
        A 25-character string beginning with "loc" followed by a random hex
        suffix. Labelbox replaces these with real cuids on import.
    Called by: BoxItem.to_labelbox_object(), _build_classifications(), _to_export_row().
    """
    return "loc" + uuid.uuid4().hex[:22]


def load_oriented(path: Path):
    """Open an image file, apply EXIF orientation, and convert it to a QPixmap.

    Args:
        path: Filesystem path to the image file.
    Returns:
        Tuple of (QPixmap, exif_rotation_tag_int, display_width, display_height, mime_type_str).
        All dimensions are post-rotation (display space).
    Called by: Main.load_current() whenever a new image is shown.
    """
    with Image.open(path) as pil:
        exif_rotation = int(pil.getexif().get(EXIF_ORIENTATION_TAG, 1))
        mime = Image.MIME.get(pil.format or "", "application/octet-stream")
        oriented = ImageOps.exif_transpose(pil).convert("RGBA")
    data = oriented.tobytes("raw", "RGBA")
    qimg = QImage(
        data,
        oriented.width,
        oriented.height,
        oriented.width * 4,
        QImage.Format.Format_RGBA8888,
    )
    return QPixmap.fromImage(qimg.copy()), exif_rotation, oriented.width, oriented.height, mime


class ResizeHandle(QGraphicsRectItem):
    CURSORS = {
        "tl": Qt.CursorShape.SizeFDiagCursor,
        "br": Qt.CursorShape.SizeFDiagCursor,
        "tr": Qt.CursorShape.SizeBDiagCursor,
        "bl": Qt.CursorShape.SizeBDiagCursor,
        "t": Qt.CursorShape.SizeVerCursor,
        "b": Qt.CursorShape.SizeVerCursor,
        "l": Qt.CursorShape.SizeHorCursor,
        "r": Qt.CursorShape.SizeHorCursor,
    }

    def __init__(self, parent: "BoxItem", anchor: str):
        """Create a small square drag handle attached to a BoxItem.

        Args:
            parent: The BoxItem that owns this handle.
            anchor: Two-character position code ("tl", "tr", "bl", "br", "t", "b", "l", "r").
        Called by: BoxItem.__init__() for each of the eight edge/corner positions.
        """
        super().__init__(-HANDLE_SIZE / 2, -HANDLE_SIZE / 2, HANDLE_SIZE, HANDLE_SIZE, parent)
        self.anchor = anchor
        self.setBrush(QBrush(QColor("white")))
        self.setPen(QPen(QColor("black"), 1))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setZValue(3)
        self.setCursor(self.CURSORS[anchor])

    def mousePressEvent(self, ev):
        """Accept the press event so Qt considers this handle the drag owner.

        Called by: Qt event loop when the user clicks a resize handle.
        """
        ev.accept()

    def mouseMoveEvent(self, ev):
        """Delegate drag movement to the parent BoxItem to resize the box.

        Args:
            ev: QGraphicsSceneMouseEvent carrying the current scene position.
        Called by: Qt event loop while the user drags a resize handle.
        """
        self.parentItem().resize_from_handle(self.anchor, ev.scenePos())
        ev.accept()

    def mouseReleaseEvent(self, ev):
        """Accept the release event to complete the resize interaction cleanly.

        Called by: Qt event loop when the user releases a resize handle.
        """
        ev.accept()


class BoxItem(QGraphicsRectItem):
    """Rect is held in local coords; scene position is via pos()."""

    def __init__(self, scene_rect: QRectF, label: str, color: QColor, source: str = "manual"):
        """Create a labeled, movable bounding box in the scene.

        Args:
            scene_rect: Initial position and size in scene coordinates.
            label: Annotation label name (e.g. "singleshoe").
            color: Border and fill tint color for the box.
            source: Origin tag — "manual", "loaded", or "model".
        Called by: AnnotationView.add_box().
        """
        super().__init__(QRectF(0, 0, scene_rect.width(), scene_rect.height()))
        self.setPos(scene_rect.topLeft())
        self.label = label
        self.source = source
        self.setPen(QPen(color, 3))
        fill = QColor(color.red(), color.green(), color.blue(), 40)
        self.setBrush(QBrush(fill))
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setZValue(1)
        self._handles = {
            a: ResizeHandle(self, a)
            for a in ("tl", "t", "tr", "r", "br", "b", "bl", "l")
        }
        self._reposition_handles()

    def _reposition_handles(self):
        """Move each ResizeHandle to its correct edge or corner position on the current rect.

        Called by: __init__() at creation and setRect() whenever the rect changes.
        """
        r = self.rect()
        coords = {
            "tl": (r.left(), r.top()),
            "tr": (r.right(), r.top()),
            "bl": (r.left(), r.bottom()),
            "br": (r.right(), r.bottom()),
            "t": (r.center().x(), r.top()),
            "b": (r.center().x(), r.bottom()),
            "l": (r.left(), r.center().y()),
            "r": (r.right(), r.center().y()),
        }
        for a, (x, y) in coords.items():
            self._handles[a].setPos(x, y)

    def setRect(self, r: QRectF):
        """Update the box's rectangle and keep all resize handles in sync.

        Args:
            r: New local-space QRectF for this item.
        Called by: BoxItem.__init__() implicitly via resize_from_handle(), and
        AnnotationView.mouseMoveEvent() while drawing a new box.
        """
        super().setRect(r)
        self._reposition_handles()

    def resize_from_handle(self, anchor: str, scene_pos):
        """Adjust the box boundaries based on which handle is being dragged.

        Args:
            anchor: Edge/corner code ("tl", "tr", "bl", "br", "t", "b", "l", "r").
            scene_pos: Current drag position in scene coordinates (QPointF).
        Called by: ResizeHandle.mouseMoveEvent() on every mouse-move while resizing.
        """
        local = self.mapFromScene(scene_pos)
        r = QRectF(self.rect())
        if "l" in anchor:
            r.setLeft(local.x())
        if "r" in anchor:
            r.setRight(local.x())
        if "t" in anchor:
            r.setTop(local.y())
        if "b" in anchor:
            r.setBottom(local.y())
        self.setRect(r.normalized())

    def scene_rect(self) -> QRectF:
        """Return the box's bounding rectangle in scene (pixel) coordinates, normalized.

        Returns:
            QRectF with top-left origin and non-negative width/height in scene space.
        Called by: to_labelbox_object() and _stash_current() when serializing boxes.
        """
        return self.mapRectToScene(self.rect()).normalized()

    def to_labelbox_object(self, schema: dict) -> dict:
        """Serialize this box to a Labelbox annotation object dict.

        Args:
            schema: The loaded schema dict mapping label names to metadata.
        Returns:
            Dict with feature_id, feature_schema_id, name, annotation_kind, and
            bounding_box fields suitable for inclusion in an NDJSON export row.
        Called by: Main._stash_current() when saving the current image's boxes.
        """
        r = self.scene_rect()
        schema_id = schema.get(self.label, {}).get("feature_schema_id", "")
        return {
            "feature_id": new_feature_id(),
            "feature_schema_id": schema_id,
            "name": self.label,
            "value": self.label,
            "annotation_kind": "ImageBoundingBox",
            "classifications": [],
            "bounding_box": {
                "top": round(r.top(), 1),
                "left": round(r.left(), 1),
                "height": round(r.height(), 1),
                "width": round(r.width(), 1),
            },
        }


class AnnotationView(QGraphicsView):
    def __init__(self, main: "Main"):
        """Set up the central graphics view used for displaying and annotating images.

        Args:
            main: The owning Main window, referenced for label lookups and schema access.
        Called by: Main.__init__() when building the central widget.
        """
        super().__init__()
        self.main = main
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform)
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._drawing: BoxItem | None = None
        self._start_scene = None

    def clear_all(self):
        """Remove all items from the scene and reset internal state.

        Clears the background pixmap and any in-progress drawing. Called by
        load_pixmap() before loading the next image.
        """
        self._scene.clear()
        self._pixmap_item = None
        self._drawing = None

    def load_pixmap(self, pixmap: QPixmap):
        """Display a new image pixmap, clearing any prior content and resetting the view.

        Args:
            pixmap: The pre-oriented QPixmap to display as the scene background.
        Called by: Main.load_current() after decoding the image file.
        """
        self.clear_all()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setZValue(0)
        self._scene.setSceneRect(QRectF(pixmap.rect()))
        self.resetTransform()
        self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def add_box(self, scene_rect: QRectF, label: str, source: str = "manual") -> BoxItem:
        """Create and add a BoxItem to the scene with the appropriate color for its source.

        Args:
            scene_rect: Initial bounding rectangle in scene coordinates.
            label: Annotation label name to attach to the box.
            source: Origin of the box — "manual" (drawn by user), "loaded" (from NDJSON,
                    shown in purple), or "model" (from predictor).
        Returns:
            The newly created BoxItem added to the scene.
        Called by: mousePressEvent() while drawing, load_current() when restoring annotations,
        and run_predictor() for model suggestions.
        """
        if source == "loaded":
            color = QColor("#8b5cf6")   # purple — came from NDJSON, needs review
        else:
            color = QColor(self.main.schema.get(label, {}).get("color", "#00ff00"))
        item = BoxItem(scene_rect, label, color, source)
        self._scene.addItem(item)
        return item

    def boxes(self) -> list[BoxItem]:
        """Return all BoxItem instances currently in the scene.

        Returns:
            List of BoxItem objects (excludes the background pixmap and handles).
        Called by: Main._stash_current(), Main.refresh_status(), and Main.run_predictor().
        """
        return [i for i in self._scene.items() if isinstance(i, BoxItem)]

    def mousePressEvent(self, ev):
        """Start drawing a new box on left-click in empty scene space.

        If the click lands on an existing box or handle, Qt handles selection/
        movement instead. Called by the Qt event loop on mouse button press.
        """
        if ev.button() != Qt.MouseButton.LeftButton or self._pixmap_item is None:
            return super().mousePressEvent(ev)
        item = self.itemAt(ev.pos())
        if isinstance(item, (BoxItem, ResizeHandle)):
            return super().mousePressEvent(ev)
        # Start drawing a new box.
        self._start_scene = self.mapToScene(ev.pos())
        self._drawing = self.add_box(
            QRectF(self._start_scene, self._start_scene),
            self.main.current_label,
        )
        ev.accept()

    def mouseMoveEvent(self, ev):
        """Expand the in-progress box as the mouse moves, or forward to Qt for panning.

        Called by the Qt event loop on every mouse-move after a press.
        """
        if self._drawing is not None and self._start_scene is not None:
            cur = self.mapToScene(ev.pos())
            rect = QRectF(self._start_scene, cur).normalized()
            self._drawing.setPos(rect.topLeft())
            self._drawing.setRect(QRectF(0, 0, rect.width(), rect.height()))
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        """Finalize a drawn box on mouse release; discard it if it is too small.

        Boxes smaller than MIN_BOX_SIZE in either dimension are removed automatically.
        Called by the Qt event loop when the mouse button is released.
        """
        if self._drawing is not None:
            r = self._drawing.rect()
            if r.width() < MIN_BOX_SIZE or r.height() < MIN_BOX_SIZE:
                self._scene.removeItem(self._drawing)
            self._drawing = None
            self._start_scene = None
            self.main.refresh_status()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        """Zoom the view when Cmd/Ctrl is held; otherwise scroll normally.

        Zooms toward the cursor position using a 1.2x factor per wheel step.
        Called by the Qt event loop on scroll-wheel input.
        """
        if ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier):
            factor = 1.2 if ev.angleDelta().y() > 0 else 1 / 1.2
            self.scale(factor, factor)
            ev.accept()
            return
        super().wheelEvent(ev)

    def keyPressEvent(self, ev):
        """Delete all selected boxes when D, Delete, or Backspace is pressed.

        Forwards any other key event to Qt's default handler.
        Called by the Qt event loop on keyboard input while the view has focus.
        """
        if ev.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace, Qt.Key.Key_D):
            for it in list(self._scene.selectedItems()):
                if isinstance(it, BoxItem):
                    self._scene.removeItem(it)
            self.main.refresh_status()
            ev.accept()
            return
        super().keyPressEvent(ev)


class Main(QMainWindow):
    def __init__(self):
        """Initialize the main application window, loading schema and building all UI components.

        Sets up the annotation view, toolbar, info bar, sidebar, and status bar.
        Restores schema defaults and resets all image/annotation state to empty.
        Called once by main() at application startup.
        """
        super().__init__()
        self.setWindowTitle("annotater")
        self.resize(1280, 860)
        self.schema = load_schema()
        self.current_label = next(iter(self.schema.keys()))
        self.image_paths: list[Path] = []
        self.index = -1
        self.annotations: dict[str, dict] = {}
        self._current_media: dict | None = None
        self._predictor = None
        self._last_sku: str = ""
        # Single-image-correction support: remember every row we loaded from
        # NDJSON, plus the file it came from, so Save can overwrite in place.
        self._ndjson_source_path: Path | None = None
        self._ndjson_all_rows: dict[str, dict] = {}   # keyed by original external_id from file
        self._ndjson_ext_id_map: dict[str, str] = {}  # canonical filename -> original external_id

        self.view = AnnotationView(self)
        self.setCentralWidget(self.view)
        self._build_toolbar()
        self._build_info_bar()
        self._build_sidebar()
        self.setStatusBar(QStatusBar())
        self.refresh_status()
        self._refresh_info_bar()

    def _build_toolbar(self):
        """Construct the main toolbar with file, navigation, label, project, and save actions.

        Adds keyboard shortcuts for Prev (Left), Next (Right), Run Model (R), and
        Save NDJSON (Cmd/Ctrl+S). Called by __init__() during UI setup.
        """
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        act_open = QAction("Open Folder", self)
        act_open.triggered.connect(self.open_folder)
        tb.addAction(act_open)

        act_open_img = QAction("Open Image", self)
        act_open_img.setToolTip("Load a single image for correction — pair with Load NDJSON")
        act_open_img.triggered.connect(self.open_image)
        tb.addAction(act_open_img)

        act_load = QAction("Load NDJSON", self)
        act_load.triggered.connect(self.load_ndjson)
        tb.addAction(act_load)

        tb.addSeparator()

        act_prev = QAction("◀  Prev", self)
        act_prev.setShortcut(QKeySequence("Left"))
        act_prev.triggered.connect(self.prev_image)
        tb.addAction(act_prev)

        act_next = QAction("Next  ▶", self)
        act_next.setShortcut(QKeySequence("Right"))
        act_next.triggered.connect(self.next_image)
        tb.addAction(act_next)

        act_predict = QAction("Run Model", self)
        act_predict.setShortcut(QKeySequence("R"))
        act_predict.triggered.connect(self.run_predictor)
        tb.addAction(act_predict)

        tb.addSeparator()

        tb.addWidget(QLabel(" Label: "))
        self.label_combo = QComboBox()
        object_labels = [
            k for k, v in self.schema.items()
            if isinstance(v, dict) and "feature_schema_id" in v
        ]
        self.label_combo.currentTextChanged.connect(self._on_label_change)
        self.label_combo.addItems(object_labels)
        if object_labels:
            self.current_label = object_labels[0]
        tb.addWidget(self.label_combo)

        tb.addSeparator()

        tb.addWidget(QLabel(" Project: "))
        self.project_id_input = QLineEdit()
        self.project_id_input.setText("my-project")
        self.project_id_input.setFixedWidth(160)
        self.project_id_input.setToolTip("Labelbox project ID — paste the real cuid before saving")
        tb.addWidget(self.project_id_input)

        tb.addSeparator()

        act_save = QAction("Save NDJSON", self)
        act_save.setShortcut(QKeySequence.StandardKey.Save)
        act_save.triggered.connect(self.save_ndjson)
        tb.addAction(act_save)

    def _on_label_change(self, text: str):
        """Update the active annotation label when the toolbar combo selection changes.

        Args:
            text: The newly selected label name from the combo box.
        Called by: label_combo.currentTextChanged signal.
        """
        self.current_label = text

    def _build_info_bar(self):
        """Build a secondary toolbar showing the current image path and matched NDJSON row.

        Displays two labels: the full image path and the external_id + source file for the
        matched NDJSON row. Useful for manual verification. Called by __init__() during setup.
        """
        self.addToolBarBreak()
        info = QToolBar("Info")
        info.setMovable(False)
        self.addToolBar(info)
        self.image_info_label = QLabel("Image: —")
        self.image_info_label.setToolTip("Current image filename and full path")
        self.json_info_label = QLabel("JSON: —")
        self.json_info_label.setToolTip("Matched NDJSON row's external_id and source file path")
        info.addWidget(self.image_info_label)
        info.addSeparator()
        info.addWidget(self.json_info_label)

    def _refresh_info_bar(self):
        """Update the info bar labels to reflect the current image and matched NDJSON row.

        Shows the full file path for the active image and the matched external_id plus
        NDJSON source path, or placeholder dashes when no data is loaded.
        Called by: load_current(), load_ndjson(), open_image(), save_ndjson(), and __init__().
        """
        if self.image_paths and 0 <= self.index < len(self.image_paths):
            p = self.image_paths[self.index]
            self.image_info_label.setText(f"Image:  {p.name}   —   {p}")
        else:
            self.image_info_label.setText("Image: —")

        if self._ndjson_source_path is not None:
            matched_id = None
            if self.image_paths and 0 <= self.index < len(self.image_paths):
                fname = self.image_paths[self.index].name
                entry = self.annotations.get(fname)
                if entry:
                    matched_id = self._ndjson_ext_id_map.get(fname) or entry.get("external_id")
            matched_txt = matched_id if matched_id else "(no matching row)"
            self.json_info_label.setText(
                f"JSON row:  {matched_txt}   —   {self._ndjson_source_path}"
            )
        else:
            self.json_info_label.setText("JSON: —")

    def _build_sidebar(self):
        """Build the right-side dock widget for image-level classifications.

        Creates text input for SKU and combo boxes for Angle and Location, populated
        from the "classifications" section of schema.json. Called by __init__() during setup.
        """
        classif_schema = self.schema.get("classifications", {})

        container = QWidget()
        form = QFormLayout(container)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(10)

        # SKU — free text
        self.sku_input = QLineEdit()
        self.sku_input.setPlaceholderText("e.g. 555088 035")
        self.sku_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        form.addRow("SKU", self.sku_input)

        # Radio fields — built from schema
        self._radio_widgets: dict[str, QComboBox] = {}
        for field_name in ("Angle", "Location"):
            field = classif_schema.get(field_name, {})
            options = [o["name"] for o in field.get("options", [])]
            combo = QComboBox()
            combo.addItem("")           # blank = not set
            combo.addItems(options)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            form.addRow(field_name, combo)
            self._radio_widgets[field_name] = combo

        # Convenience refs used in _build_classifications
        self.angle_combo = self._radio_widgets.get("Angle")
        self.location_combo = self._radio_widgets.get("Location")

        dock = QDockWidget("Classifications", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        dock.setWidget(container)
        dock.setMinimumWidth(200)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _build_classifications(self) -> list[dict]:
        """Serialize the current sidebar widget values into a Labelbox classifications list.

        Returns:
            List of classification dicts (text answer for SKU, radio answers for Angle/Location).
            Empty entries (blank combo or empty SKU) are omitted.
        Called by: _stash_current() when snapshotting the current image's annotations.
        """
        classif_schema = self.schema.get("classifications", {})
        result = []

        sku_text = self.sku_input.text().strip()
        if sku_text:
            s = classif_schema.get("SKU", {})
            result.append({
                "feature_id": new_feature_id(),
                "feature_schema_id": s.get("feature_schema_id", ""),
                "name": "SKU",
                "value": "sku",
                "text_answer": {"content": sku_text, "classifications": []},
            })

        for field_name, value_key, widget in [
            ("Angle",    "angle",    self.angle_combo),
            ("Location", "location", self.location_combo),
        ]:
            if widget is None:
                continue
            option_name = widget.currentText()
            if not option_name:
                continue
            s = classif_schema.get(field_name, {})
            option = next(
                (o for o in s.get("options", []) if o["name"] == option_name), None
            )
            if option:
                result.append({
                    "feature_id": new_feature_id(),
                    "feature_schema_id": s.get("feature_schema_id", ""),
                    "name": field_name,
                    "value": value_key,
                    "radio_answer": {
                        "feature_id": new_feature_id(),
                        "feature_schema_id": option.get("feature_schema_id", ""),
                        "name": option["name"],
                        "value": option["value"],
                        "classifications": [],
                    },
                })
        return result

    def _clear_sidebar(self):
        """Reset all sidebar classification widgets to their blank/default state.

        Clears the SKU text field and sets all combo boxes to index 0 (empty).
        Called by: load_current() before restoring saved classifications for the new image.
        """
        self.sku_input.clear()
        for combo in self._radio_widgets.values():
            combo.setCurrentIndex(0)

    def open_folder(self):
        """Prompt the user to pick an image folder, then load its images as the working set.

        Persists the chosen folder path to config. Replaces any currently loaded images
        and clears all unsaved annotations after stashing the current image first.
        Called by: the "Open Folder" toolbar action.
        """
        start_dir = load_config().get("last_folder", "")
        d = QFileDialog.getExistingDirectory(self, "Pick a folder of images", start_dir)
        if not d:
            return
        folder = Path(d)
        cfg = load_config()
        cfg["last_folder"] = str(folder)
        save_config(cfg)
        imgs = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_EXTS)
        if not imgs:
            QMessageBox.information(self, "No images", "No supported images in that folder.")
            return
        self._stash_current()
        self.image_paths = imgs
        self.annotations = {}
        self.index = 0
        self.load_current()

    def open_image(self):
        """Prompt the user to open a single image, replacing the current image list with just that file.

        If an NDJSON has already been loaded, re-matches its rows against the new single-image
        list so existing annotations carry over. Persists the parent directory to config.
        Called by: the "Open Image" toolbar action.
        """
        start_dir = load_config().get("last_image_dir") or load_config().get("last_folder", "")
        f, _ = QFileDialog.getOpenFileName(
            self, "Open single image", start_dir,
            "Images (*.jpg *.jpeg *.png *.bmp *.webp *.tiff);;All files (*)"
        )
        if not f:
            return
        path = Path(f)
        cfg = load_config()
        cfg["last_image_dir"] = str(path.parent)
        save_config(cfg)
        self._stash_current()
        self.image_paths = [path]
        self.annotations = {}
        self.index = 0
        # If an NDJSON was previously loaded, re-match against this single image.
        if self._ndjson_all_rows:
            self._rematch_ndjson_to_current_folder()
        self.load_current()

    def _rematch_ndjson_to_current_folder(self):
        """Re-match all previously loaded NDJSON rows against the current image path list.

        Performs case-insensitive filename matching and repopulates self.annotations and
        self._ndjson_ext_id_map. Used when the image set changes after an NDJSON is already
        loaded (e.g. after open_image() is called with rows already in memory).
        Called by: open_image() when _ndjson_all_rows is non-empty.
        """
        folder_names_lc = {p.name.lower(): p.name for p in self.image_paths}
        self._ndjson_ext_id_map = {}
        for ext_id, norm in self._ndjson_all_rows.items():
            canonical = folder_names_lc.get(ext_id.lower())
            if canonical is not None:
                self.annotations[canonical] = {**norm, "external_id": canonical}
                self._ndjson_ext_id_map[canonical] = ext_id

    def load_current(self):
        """Load and display the image at the current index, restoring any saved annotations.

        Reads the image file (applying EXIF orientation), loads the pixmap into the view,
        then restores bounding boxes and sidebar classifications from self.annotations if a
        prior stash exists, or runs the predictor and carries forward the last SKU otherwise.
        Called by: open_folder(), open_image(), load_ndjson(), prev_image(), next_image().
        """
        if not self.image_paths:
            return
        path = self.image_paths[self.index]
        try:
            pixmap, exif, w, h, mime = load_oriented(path)
        except Exception as exc:
            QMessageBox.warning(self, "Load error", f"Could not load {path.name}: {exc}")
            return
        self.view.load_pixmap(pixmap)
        self._current_media = {
            "height": h,
            "width": w,
            "asset_type": "image",
            "mime_type": mime,
            "exif_rotation": exif,
        }
        self._clear_sidebar()
        prev = self.annotations.get(path.name)
        if prev:
            for obj in prev["annotations"]["objects"]:
                bb = obj["bounding_box"]
                self.view.add_box(
                    QRectF(bb["left"], bb["top"], bb["width"], bb["height"]),
                    obj["name"],
                    source="loaded",
                )
            # Restore image-level classifications into sidebar
            for c in prev["annotations"].get("classifications", []):
                name = c.get("name")
                if name == "SKU":
                    self.sku_input.setText(c.get("text_answer", {}).get("content", "").strip())
                elif name in self._radio_widgets:
                    radio_name = c.get("radio_answer", {}).get("name", "")
                    self._radio_widgets[name].setCurrentText(radio_name)
        else:
            # No existing annotation — carry forward last SKU and run model
            if self._last_sku:
                self.sku_input.setText(self._last_sku)
            self.run_predictor()
        self.refresh_status()
        self._refresh_info_bar()
        self.sku_input.setFocus()
        self.sku_input.selectAll()

    def _stash_current(self):
        """Snapshot the current image's boxes and sidebar values into self.annotations.

        Serializes all BoxItems to Labelbox object dicts and all sidebar widgets to
        classification dicts, then stores the result keyed by filename. Also records the
        last SKU text for carry-forward to the next unannotated image. No-ops if no image
        is loaded.
        Called by: prev_image(), next_image(), open_folder(), open_image(), save_ndjson().
        """
        if self.index < 0 or self.index >= len(self.image_paths) or self._current_media is None:
            return
        self._last_sku = self.sku_input.text().strip()
        path = self.image_paths[self.index]
        objs = [b.to_labelbox_object(self.schema) for b in self.view.boxes()]
        self.annotations[path.name] = {
            "media_attributes": dict(self._current_media),
            "external_id": path.name,
            "annotations": {
                "objects": objs,
                "classifications": self._build_classifications(),
                "relationships": [],
            },
        }

    def prev_image(self):
        """Stash the current image and navigate to the previous one in the list.

        No-op if already at the first image. Called by: the "Prev" toolbar action
        and the Left arrow key shortcut.
        """
        if self.image_paths and self.index > 0:
            self._stash_current()
            self.index -= 1
            self.load_current()

    def next_image(self):
        """Stash the current image and navigate to the next one in the list.

        No-op if already at the last image. Called by: the "Next" toolbar action
        and the Right arrow key shortcut.
        """
        if self.image_paths and self.index < len(self.image_paths) - 1:
            self._stash_current()
            self.index += 1
            self.load_current()

    def refresh_status(self):
        """Update the status bar with the current image index, filename, box count, and label.

        Shows a prompt to open a folder when no images are loaded.
        Called by: load_current(), mouseReleaseEvent(), keyPressEvent(), run_predictor().
        """
        if not self.image_paths:
            self.statusBar().showMessage("Open a folder to begin.")
            return
        path = self.image_paths[self.index]
        n = len(self.view.boxes())
        self.statusBar().showMessage(
            f"[{self.index + 1}/{len(self.image_paths)}] {path.name}  —  "
            f"{n} box(es)  —  label: {self.current_label}"
        )

    def run_predictor(self):
        """Lazily import predictor.py and run it on the current image, adding suggestion boxes.

        Loads the predict() function on first call and caches it. Each suggestion dict must
        contain a "bounding_box" key (top/left/width/height) and an optional "label" key.
        Silently no-ops if no images are loaded; shows a warning dialog on import or predict errors.
        Called by: load_current() for unannotated images, and the "Run Model" toolbar action (R).
        """
        if not self.image_paths:
            return
        if self._predictor is None:
            try:
                from predictor import predict  # lazy import
                self._predictor = predict
            except Exception as exc:
                QMessageBox.warning(self, "Predictor error", f"Could not load predictor.py: {exc}")
                return
        path = self.image_paths[self.index]
        try:
            suggestions = self._predictor(str(path))
        except Exception as exc:
            QMessageBox.warning(self, "Predictor error", f"predict() raised: {exc}")
            return
        for s in suggestions or []:
            bb = s["bounding_box"]
            self.view.add_box(
                QRectF(bb["left"], bb["top"], bb["width"], bb["height"]),
                s.get("label", self.current_label),
                source="model",
            )
        self.refresh_status()

    @staticmethod
    def _normalize_row(row: dict) -> dict | None:
        """Convert a flat or Labelbox-export NDJSON row into the internal annotation shape.

        Accepts two formats: the full Labelbox export format (with "data_row" and "projects"
        keys) and the flat format this tool writes (with "external_id" and "annotations" keys).
        For Labelbox format, only the first label of the first project is extracted.

        Args:
            row: Parsed JSON dict representing a single NDJSON line.
        Returns:
            Normalized dict with keys external_id, media_attributes, and annotations,
            or None if the row format is unrecognized.
        Called by: load_ndjson() for each line of the loaded NDJSON file.
        """
        # Labelbox export: wrapped in data_row + projects.<id>.labels[*].annotations
        if "data_row" in row and "projects" in row:
            external_id = (row.get("data_row") or {}).get("external_id")
            if not external_id:
                return None
            media = row.get("media_attributes") or {}
            for proj in (row.get("projects") or {}).values():
                for label in proj.get("labels", []):
                    ann = label.get("annotations") or {}
                    return {
                        "external_id": external_id,
                        "media_attributes": media,
                        "annotations": {
                            "objects": ann.get("objects", []),
                            "classifications": ann.get("classifications", []),
                            "relationships": ann.get("relationships", []),
                        },
                    }
            # No labels found — still track with empty annotations
            return {
                "external_id": external_id,
                "media_attributes": media,
                "annotations": {"objects": [], "classifications": [], "relationships": []},
            }
        # Flat format (what this tool writes)
        if "external_id" in row and "annotations" in row:
            return row
        return None

    def load_ndjson(self):
        """Prompt for an NDJSON file, parse it, and merge matching rows into the current annotations.

        Normalizes each row via _normalize_row(), matches rows to images by case-insensitive
        filename, and merges into self.annotations (loaded rows win on conflict). Stores the
        full row set in _ndjson_all_rows so Save can round-trip rows for images not currently
        open. Shows a summary dialog reporting match counts, duplicates, and parse errors.
        Called by: the "Load NDJSON" toolbar action.
        """
        f, _ = QFileDialog.getOpenFileName(
            self, "Load NDJSON", "", "NDJSON (*.ndjson *.jsonl);;All files (*)"
        )
        if not f:
            return

        loaded: dict[str, dict] = {}
        duplicates: list[str] = []
        parse_errors: list[str] = []

        with open(f) as fp:
            for i, line in enumerate(fp, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"line {i}: {exc}")
                    continue
                norm = self._normalize_row(row)
                if norm is None:
                    parse_errors.append(f"line {i}: unrecognized format")
                    continue
                ext_id = norm["external_id"]
                if ext_id in loaded:
                    duplicates.append(ext_id)
                else:
                    loaded[ext_id] = norm

        # Remember the source so Save can overwrite in place, and the full
        # set of rows so rows for images we're not currently looking at are
        # preserved when we write back.
        self._ndjson_source_path = Path(f)
        self._ndjson_all_rows = loaded
        self._ndjson_ext_id_map = {}

        # Match against current folder (case-insensitive for macOS friendliness)
        folder_names_lc = {p.name.lower(): p.name for p in self.image_paths}
        matched: dict[str, dict] = {}
        unmatched_rows: list[str] = []
        for ext_id, norm in loaded.items():
            canonical = folder_names_lc.get(ext_id.lower())
            if canonical is None:
                unmatched_rows.append(ext_id)
            else:
                # Re-key under the folder's actual filename
                matched[canonical] = {**norm, "external_id": canonical}
                self._ndjson_ext_id_map[canonical] = ext_id

        # Merge (loaded wins for matching names)
        self.annotations.update(matched)

        no_annot = [p.name for p in self.image_paths if p.name not in self.annotations]

        # Refresh current image so new boxes appear
        if self.image_paths:
            self.load_current()
        self._refresh_info_bar()

        lines = [
            f"Loaded {len(loaded)} row(s) from {Path(f).name}.",
            f"  Matched to current folder: {len(matched)}",
            f"  No matching image: {len(unmatched_rows)}",
            f"  Images without annotations: {len(no_annot)}",
        ]
        if duplicates:
            lines.append(f"  Duplicate external_ids (kept first): {len(duplicates)}")
        if parse_errors:
            lines.append(f"  Parse errors: {len(parse_errors)}")
            lines.extend(f"    {e}" for e in parse_errors[:5])
            if len(parse_errors) > 5:
                lines.append(f"    …and {len(parse_errors) - 5} more")
        QMessageBox.information(self, "Load summary", "\n".join(lines))

    def _to_export_row(self, filename: str, flat: dict) -> dict:
        """Wrap a flat internal annotation dict into a full Labelbox export-format row.

        Args:
            filename: The external_id / filename to use in the data_row block.
            flat: Internal annotation dict with media_attributes and annotations keys.
        Returns:
            Full Labelbox export dict with data_row, media_attributes, and projects sections,
            using the current project ID from the toolbar input.
        Called by: save_ndjson() for each row being written to the output file.
        """
        proj_id = self.project_id_input.text().strip() or "my-project"
        proj_name = proj_id
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+00:00")
        path = next((p for p in self.image_paths if p.name == filename), None)
        return {
            "data_row": {
                "id": new_feature_id(),
                "external_id": filename,
                "row_data": str(path) if path else filename,
                "details": {
                    "created_at": now,
                    "updated_at": now,
                },
            },
            "media_attributes": flat.get("media_attributes", {}),
            "attachments": [],
            "metadata_fields": [],
            "projects": {
                proj_id: {
                    "name": proj_name,
                    "labels": [
                        {
                            "label_kind": "Default",
                            "version": "1.0.0",
                            "id": new_feature_id(),
                            "label_details": {
                                "created_at": now,
                                "updated_at": now,
                            },
                            "annotations": flat.get("annotations", {}),
                        }
                    ],
                    "project_tags": [],
                }
            },
        }

    def save_ndjson(self):
        """Stash the current image, then write all annotations to an NDJSON file.

        Merges current session edits back into the full row set from _ndjson_all_rows,
        preserving rows for images that were never opened. Defaults the save dialog to the
        originally loaded file (allowing in-place overwrite) or annotations.ndjson in the
        last folder. Updates _ndjson_source_path and the info bar after a successful write.
        Called by: the "Save NDJSON" toolbar action and Cmd/Ctrl+S shortcut.
        """
        self._stash_current()
        if not self.annotations and not self._ndjson_all_rows:
            QMessageBox.information(self, "Nothing to save", "No annotations yet.")
            return

        # Default path: if we loaded from a file, offer to overwrite it.
        if self._ndjson_source_path is not None:
            default_path = str(self._ndjson_source_path)
        else:
            last_folder = load_config().get("last_folder", "")
            default_path = (
                str(Path(last_folder) / "annotations.ndjson")
                if last_folder else "annotations.ndjson"
            )
        f, _ = QFileDialog.getSaveFileName(
            self, "Save NDJSON", default_path, "NDJSON (*.ndjson)"
        )
        if not f:
            return

        # Merge current edits back into the full row set (if any), preserving
        # rows for images we never opened in this session.
        combined: dict[str, dict] = dict(self._ndjson_all_rows)
        for filename, flat in self.annotations.items():
            orig_ext_id = self._ndjson_ext_id_map.get(filename, filename)
            # Store under original external_id to keep file ordering/identity stable.
            combined[orig_ext_id] = {**flat, "external_id": orig_ext_id}

        with open(f, "w") as fp:
            for ext_id, flat in combined.items():
                # _to_export_row takes the filename it should advertise; use the
                # external_id we decided on above.
                row = self._to_export_row(ext_id, flat)
                fp.write(json.dumps(row) + "\n")

        # Remember the file we wrote so subsequent saves default here too.
        self._ndjson_source_path = Path(f)
        self._refresh_info_bar()
        self.statusBar().showMessage(f"Saved {len(combined)} rows to {f}")


def main():
    """Entry point: create the QApplication, show the main window, and start the event loop.

    Called by the __main__ guard at the bottom of this module.
    """
    app = QApplication(sys.argv)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
