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

import base64
import io
import json
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps
from PyQt6.QtCore import Qt, QRectF, QUrl
from PyQt6.QtGui import (QAction, QBrush, QColor, QImage, QKeySequence,
                         QPainter, QPen, QPixmap)
from PyQt6.QtNetwork import (QNetworkAccessManager, QNetworkReply,
                             QNetworkRequest)
from PyQt6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog,
                             QFormLayout, QFrame, QGraphicsItem,
                             QGraphicsPixmapItem, QGraphicsRectItem,
                             QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
                             QLineEdit, QMainWindow, QMessageBox,
                             QPlainTextEdit, QProgressDialog, QPushButton,
                             QScrollArea, QSizePolicy, QStatusBar, QTabBar,
                             QTabWidget, QToolBar, QVBoxLayout, QWidget)

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
MIN_BOX_SIZE = 5
HANDLE_SIZE = 10
EXIF_ORIENTATION_TAG = 0x0112

PROJECT_ROOT = Path(__file__).parent
SCHEMA_PATH = PROJECT_ROOT / "schema.json"
CONFIG_PATH = PROJECT_ROOT / ".annotater_config.json"
CREDS_PATH = PROJECT_ROOT / ".annotater_creds.json"

GOAT_LOGIN_URL = "https://sell-api.goat.com/api/v1/unstable/users/login"
GOAT_VISUAL_SEARCH_URL = (
    "https://gateway.alias.org/api/v1alpha1/visual-search/resolve-product-template-from-image"
)
GOAT_USER_AGENT = "alias/1.50.0 (iPhone; iOS 26.4.2; Scale/3.00) Locale/en"

DEFAULT_SCHEMA = {
    "singleshoe": {
        "feature_schema_id": "cmnqdth8e1ox70720h2io5rt9",
        "color": "#00ff00",
    }
}


# --- dev: method-call trace ---
_DEBUG_SINK = None  # set by Main when the dock is built

def _dbg(msg: str):
    """Send a line to the debug dock if it's been wired up. No-op otherwise."""
    if _DEBUG_SINK is not None:
        _DEBUG_SINK(msg)

_NOISY_METHODS = {"mouseMoveEvent", "wheelEvent", "_on_thumb_loaded"}  # skip high-frequency events

def _trace_methods(cls):
    """Class decorator: wrap every non-dunder method to log its name on entry."""
    for name, attr in list(cls.__dict__.items()):
        if name.startswith("__") or name in _NOISY_METHODS:
            continue
        # Detect static/class methods so we can unwrap them, wrap the underlying
        # function, and re-decorate with the same wrapper kind. Wrapping a
        # staticmethod naively turns it into an instance method and Python
        # would inject `self` at call time, breaking the signature.
        is_static = isinstance(attr, staticmethod)
        is_class = isinstance(attr, classmethod)
        if is_static or is_class:
            fn = attr.__func__
        elif callable(attr):
            fn = attr
        else:
            continue

        def wrap(n, f):
            # Qt signals (e.g. QAction.triggered → checked: bool) forward extra
            # args when the slot signature looks permissive. Match the original
            # arity so we don't pass Qt-injected extras into a fixed-arg method.
            code = getattr(f, "__code__", None)
            has_varargs = bool(code and (code.co_flags & 0x04))
            argcount = code.co_argcount if code else 999
            def w(*a, **kw):
                _dbg(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {cls.__name__}.{n}")
                if not has_varargs and len(a) > argcount:
                    a = a[:argcount]
                return f(*a, **kw)
            return w

        wrapped = wrap(name, fn)
        if is_static:
            wrapped = staticmethod(wrapped)
        elif is_class:
            wrapped = classmethod(wrapped)
        setattr(cls, name, wrapped)
    return cls


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



def goat_login() -> tuple[dict | None, str]:
  #   curl -i -X POST 'https://sell-api.goat.com/api/v1/unstable/users/login' \
  # -H 'Accept: application/json' \
  # -H 'Content-Type: application/json' \
  # -H 'User-Agent: alias/1.50.0 (iPhone; iOS 26.4.2; Scale/3.00) Locale/en' \
  # --data-raw '{"grant_type":"password","username":"stanleylee13@yahoo.com","password":"33333333"}' \
  # --proxy http://localhost:9090
    """Authenticate with the GOAT sell API using creds from .annotater_creds.json.

    Returns:
        (auth_token_dict, status_message). auth_token_dict is None on failure.
        status_message is a short human-readable explanation for the status bar.
    Called by: Main.__init__() after the UI is built.
    """
    if not CREDS_PATH.exists():
        return None, f"GOAT auth: no {CREDS_PATH.name} found — running offline."
    try:
        creds = json.loads(CREDS_PATH.read_text())
        body = json.dumps({
            "grant_type": "password",
            "username": creds["username"],
            "password": creds["password"],
        }).encode("utf-8")
        req = urllib.request.Request(
            GOAT_LOGIN_URL,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": GOAT_USER_AGENT,
            },
            method="POST",
        )
        # Python's stdlib doesn't read macOS keychain trust roots; corporate / MITM
        # proxies (or even just an outdated CA bundle) cause CERTIFICATE_VERIFY_FAILED
        # here even though curl works. This is a dev-only tool hitting an internal
        # API, so we skip verification deliberately.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": "http://localhost:9090", "https": "http://localhost:9090"}),
            urllib.request.HTTPSHandler(context=ctx),
        )
        with opener.open(req, timeout=10) as resp:
            payload = json.loads(resp.read())
        token = payload.get("auth_token") or {}
        if not token.get("access_token"):
            return None, "GOAT auth: response missing access_token."
        user = payload.get("user") or {}
        who = user.get("email") or user.get("username") or "unknown"
        return token, f"GOAT auth: logged in as {who}."
    except urllib.error.HTTPError as e:
        return None, f"GOAT auth: HTTP {e.code} — running offline."
    except Exception as e:
        return None, f"GOAT auth: {type(e).__name__} — running offline."


def goat_visual_search(image_bytes: bytes, auth_token: dict) -> tuple[dict | None, str, int]:
    """POST a JPEG/PNG byte blob as base64 to the visual-search endpoint.

    Args:
        image_bytes: Raw encoded image bytes (e.g. JPEG of a cropped region).
        auth_token: Token dict from goat_login() — must contain "access_token".
    Returns:
        (payload_dict | None, status_message, http_status). http_status is -1 on
        network error. Callers should check for status == 401 to trigger an auth
        refresh + retry.
    Called by: Main._refresh_visual_search() after cropping the current image.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = json.dumps({"base64_data": b64}).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {auth_token.get('access_token','')}",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": GOAT_USER_AGENT,
        "Content-Type": "application/json",
        # x-emb-st = current request timestamp in ms; the mobile client sends this
        # and the origin can be picky about its presence.
        "x-emb-st": str(int(time.time() * 1000)),
        "Connection": "keep-alive",
    }
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": "http://localhost:9090", "https": "http://localhost:9090"}),
        urllib.request.HTTPSHandler(context=ctx),
    )

    # Retry once on 502/503/504 — they're usually transient origin hiccups.
    for attempt in (1, 2):
        req = urllib.request.Request(GOAT_VISUAL_SEARCH_URL, data=body, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=15) as resp:
                return json.loads(resp.read()), "visual search: ok", resp.status
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                err_body = "<no body>"
            snippet = err_body.strip().replace("\n", " ")[:200]
            if e.code in (502, 503, 504) and attempt == 1:
                time.sleep(1.0)
                continue  # retry once
            return None, f"visual search: HTTP {e.code} — {snippet}", e.code
        except Exception as e:
            return None, f"visual search: {type(e).__name__} — {e}", -1
    return None, "visual search: unreachable", -1


@dataclass
class BulkResult:
    """One row of bulk-mode evaluation: a single bounding-box crop and the API ranking it produced.

    Fields:
        path: Source image path.
        gt_sku: Ground-truth SKU for that image (from NDJSON image-level classification).
            All boxes in one image share the same gt_sku.
        rect: (left, top, width, height) display-space pixels of this bounding box.
        items: API's catalog_items in returned order. Each item dict has sku/name/slug/id/
            grid_glow_picture_url etc. Empty on crop or network failure.
        status: Human-readable status string from goat_visual_search() (or "crop failed").
        crop_jpeg: JPEG bytes actually POSTed, retained so the report can display the crop.
    """
    path: Path
    gt_sku: str
    rect: tuple[int, int, int, int]
    items: list[dict]
    status: str
    crop_jpeg: bytes | None = None

    def rank(self) -> int | None:
        """0-based index of gt_sku in items (case-insensitive, trimmed); None if not present
        or if gt_sku is empty. Used by _compute_stats and report rendering."""
        gt = (self.gt_sku or "").strip().casefold()
        if not gt:
            return None
        for i, it in enumerate(self.items):
            if str(it.get("sku", "")).strip().casefold() == gt:
                return i
        return None


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


@_trace_methods
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

    def set_is_search_target(self, on: bool):
        """Paint/unpaint the orange 'this crop was sent to visual search' highlight.

        On True, saves the current pen and applies a thicker orange border.
        On False, restores the saved pen so the box returns to its source color.
        Idempotent — safe to call repeatedly.
        Called by: Main._set_search_box().
        """
        is_target = getattr(self, "_is_search_target", False)
        if on and not is_target:
            self._saved_pen = QPen(self.pen())
            self._saved_brush = QBrush(self.brush())
            # Bright blue, thicker border, slightly more saturated fill for clarity.
            blue = QColor("#1d4ed8")  # tailwind blue-700
            self.setPen(QPen(blue, 6))
            fill = QColor(blue.red(), blue.green(), blue.blue(), 70)
            self.setBrush(QBrush(fill))
            self._is_search_target = True
        elif not on and is_target:
            self.setPen(self._saved_pen)
            self.setBrush(self._saved_brush)
            self._is_search_target = False


@_trace_methods
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
        self._scene.selectionChanged.connect(self._on_selection_changed)

    def _on_selection_changed(self):
        """When the user clicks a BoxItem, notify Main to switch visual-search target.

        Filters to BoxItems only (ResizeHandle selections are ignored).
        Called by: QGraphicsScene.selectionChanged signal.
        """
        selected_boxes = [i for i in self._scene.selectedItems() if isinstance(i, BoxItem)]
        if selected_boxes:
            self.main._on_search_box_clicked(selected_boxes[0])

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


@_trace_methods
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

        self.auth_token: dict | None = None  # populated by goat_login() below if creds exist
        self._search_box: "BoxItem | None" = None  # active visual-search target on canvas

        self.view = AnnotationView(self)
        # Wrap the canvas in a tab widget so bulk-mode results can appear as a
        # second tab. The Annotate tab is non-closeable (close button removed
        # below); closing the Bulk results tab calls _exit_bulk_mode.
        self._canvas_tabs = QTabWidget()
        self._canvas_tabs.setTabsClosable(True)
        self._canvas_tabs.tabCloseRequested.connect(self._on_canvas_tab_close)
        self._canvas_tabs.addTab(self.view, "Annotate")
        try:
            self._canvas_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        except Exception:
            # On some platforms the close button lives on the left side.
            self._canvas_tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)
        self.setCentralWidget(self._canvas_tabs)
        # Bulk-mode state (populated when the user runs Bulk mode):
        self._bulk_tab_index: int | None = None
        self._bulk_report_pixmap: QPixmap | None = None
        self._bulk_report_png_bytes: bytes | None = None
        self._build_toolbar()
        self._build_info_bar()
        self._build_sidebar()
        self.setStatusBar(QStatusBar())

        # --- bottom dock: tabbed pane with Visual search + Debug log ---
        self._carousel_host = QWidget()
        self._carousel_layout = QHBoxLayout(self._carousel_host)
        self._carousel_layout.setContentsMargins(6, 6, 6, 6)
        self._carousel_layout.setSpacing(8)
        self._carousel_layout.addStretch(1)  # keeps cards left-aligned

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self._carousel_host)

        self._debug_text = QPlainTextEdit()
        self._debug_text.setReadOnly(True)
        self._debug_text.setMaximumBlockCount(500)

        # Crop preview tab — shows the exact image bytes being POSTed
        self._crop_preview_tab = QWidget()
        cp_layout = QVBoxLayout(self._crop_preview_tab)
        cp_layout.setContentsMargins(8, 8, 8, 8)
        cp_layout.setSpacing(6)

        # Top row: metadata text on the left, Download button on the right
        cp_top = QHBoxLayout()
        cp_top.setContentsMargins(0, 0, 0, 0)
        self._crop_preview_meta = QLabel("(no crop yet)")
        self._crop_preview_meta.setStyleSheet("font-family: monospace; color: #444;")
        self._crop_download_btn = QPushButton("Download JPEG")
        self._crop_download_btn.setEnabled(False)
        self._crop_download_btn.setToolTip("Save the current cropped image to disk as a JPEG")
        self._crop_download_btn.clicked.connect(self._download_crop)
        cp_top.addWidget(self._crop_preview_meta, 1)
        cp_top.addWidget(self._crop_download_btn, 0)

        self._crop_preview_img = QLabel()
        self._crop_preview_img.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self._crop_preview_img.setStyleSheet("background: #f5f5f5;")
        # The crop can be much taller than the dock — wrap in a scroll area so
        # the user can scroll to see the full image (and confirm nothing's
        # actually being cut off in the JPEG that gets POSTed).
        cp_scroll = QScrollArea()
        cp_scroll.setWidgetResizable(True)
        cp_scroll.setWidget(self._crop_preview_img)
        cp_layout.addLayout(cp_top)
        cp_layout.addWidget(cp_scroll, 1)

        # Cached bytes of the most-recently-generated crop, for the Download button.
        self._last_crop_bytes: bytes | None = None

        self._bottom_tabs = QTabWidget()
        self._bottom_tabs.addTab(scroll, "Visual search")
        self._bottom_tabs.addTab(self._crop_preview_tab, "Crop preview")
        self._bottom_tabs.addTab(self._debug_text, "Debug log")
        self._bottom_tabs.setStyleSheet(
            "QTabBar::tab { padding: 6px 18px; min-width: 110px; }"
            "QTabBar::tab:selected { background: #f0f0f0; font-weight: bold; }"
        )

        bottom_dock = QDockWidget("", self)
        bottom_dock.setTitleBarWidget(QWidget())  # hide native dock title — tabs are the label
        bottom_dock.setWidget(self._bottom_tabs)
        bottom_dock.setMinimumHeight(240)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, bottom_dock)

        # QNetworkAccessManager for async thumbnail downloads
        self._netmgr = QNetworkAccessManager(self)
        self._netmgr.finished.connect(self._on_thumb_loaded)
        # url -> list of target labels (list, not single, because multiple
        # templates can share the same image_url — color variants etc).
        self._pending_thumbs: dict[str, list[QLabel]] = {}

        # Cmd/Ctrl+Shift+D toggles between the two tabs
        self._toolbar.addSeparator()
        toggle = QAction("Debug log", self)
        toggle.setShortcut(QKeySequence("Ctrl+Shift+D"))
        toggle.setToolTip("Switch between Visual search and Debug log tabs (Cmd/Ctrl+Shift+D)")
        toggle.triggered.connect(
            lambda: self._bottom_tabs.setCurrentIndex(
                (self._bottom_tabs.currentIndex() + 1) % self._bottom_tabs.count()
            )
        )
        self._toolbar.addAction(toggle)
        global _DEBUG_SINK
        _DEBUG_SINK = self._debug_text.appendPlainText

        # --- auth: log in to GOAT API (best-effort; does not block the UI) ---
        self.auth_token, auth_msg = goat_login()
        self.statusBar().showMessage(auth_msg, 8000)
        _dbg(f"auth: {auth_msg}")

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
        self._toolbar = tb

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

        # Bulk mode — disabled until both a folder of images and an NDJSON
        # have been loaded. Enabled by _maybe_enable_bulk_mode().
        self._bulk_action = QAction("Bulk mode", self)
        self._bulk_action.setToolTip(
            "Run visual search across every bounding box in the loaded NDJSON "
            "and render a recall report. Requires a folder + NDJSON."
        )
        self._bulk_action.setEnabled(False)
        self._bulk_action.triggered.connect(self._start_bulk_mode)
        tb.addAction(self._bulk_action)

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

        tb.addWidget(QLabel(" Jump to: "))
        self.jump_input = QLineEdit()
        self.jump_input.setPlaceholderText("filename or prefix (e.g. IMG_7168)")
        self.jump_input.setFixedWidth(240)
        self.jump_input.setToolTip(
            "Type a filename, stem, or prefix and press Enter to jump to that image"
        )
        self.jump_input.returnPressed.connect(self._jump_to_filename)
        tb.addWidget(self.jump_input)

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
        self._maybe_enable_bulk_mode()

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
        self._search_box = None  # the old reference is now a destroyed Qt object — drop it
        self._last_crop_bytes = None  # crop from the previous image is no longer relevant
        self._crop_download_btn.setEnabled(False)
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
        self._refresh_visual_search()

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

    def _set_search_box(self, box: "BoxItem | None"):
        """Make `box` the visual-search target, restoring the previous target's color.

        No-op if `box` is already the target.
        Called by: load_current() (initial pick of boxes[0]) and _on_search_box_clicked().
        """
        if self._search_box is box:
            return
        if self._search_box is not None:
            try:
                self._search_box.set_is_search_target(False)
            except RuntimeError:
                pass  # underlying Qt object was destroyed (scene cleared) — ignore
        self._search_box = box
        if box is not None:
            box.set_is_search_target(True)

    def _on_search_box_clicked(self, box: "BoxItem"):
        """User clicked a different box — switch the search target and re-run.

        Called by: AnnotationView._on_selection_changed() via scene's selectionChanged.
        """
        if box is self._search_box:
            return
        self._set_search_box(box)
        self._refresh_visual_search()

    def _crop_image_to_rect_jpeg(
        self,
        path: Path,
        rect_xywh: tuple[int, int, int, int],
        max_dim: int = 1024,
        update_preview: bool = True,
    ) -> bytes | None:
        """Crop `path` to (left, top, width, height) display-space pixels and JPEG-encode.

        Uses PIL with exif_transpose so the crop matches the on-screen pixels.
        Resizes the crop to fit within `max_dim` x `max_dim` so the upload is small
        enough for the visual-search API (large iPhone crops have caused 502s).
        If `update_preview` is True, also pushes the crop into the Crop preview tab
        and arms the Download JPEG button — set to False for bulk mode so dozens of
        crops don't churn the preview UI.
        Returns JPEG bytes, or None on I/O / decode failure.
        Called by: _crop_search_box_to_jpeg() (single-image flow) and _start_bulk_mode().
        """
        left, top, w, h = rect_xywh
        left, top = max(0, int(left)), max(0, int(top))
        right, bottom = left + max(1, int(w)), top + max(1, int(h))
        try:
            with Image.open(path) as pil:
                oriented = ImageOps.exif_transpose(pil).convert("RGB")
        except Exception as e:
            _dbg(f"crop: failed to open {path.name}: {type(e).__name__} — {e}")
            return None
        crop = oriented.crop((left, top, right, bottom))
        orig_w, orig_h = crop.width, crop.height
        # Downscale so the long edge is at most max_dim (preserves aspect ratio).
        if max(crop.width, crop.height) > max_dim:
            crop.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        _dbg(f"crop: {path.name} ({orig_w}x{orig_h} -> {crop.width}x{crop.height})")
        buf = io.BytesIO()
        crop.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()

        if update_preview:
            # Update the Crop preview tab so the user can verify exactly what was sent.
            pm = QPixmap()
            pm.loadFromData(data)
            self._crop_preview_img.setPixmap(pm.scaledToWidth(
                min(pm.width(), 500), Qt.TransformationMode.SmoothTransformation
            ))
            kb = len(data) / 1024
            self._crop_preview_meta.setText(
                f"source crop: {orig_w}×{orig_h}    sent: {crop.width}×{crop.height}    "
                f"size: {len(data):,} bytes ({kb:.1f} KB)"
            )
            # Cache for the Download button.
            self._last_crop_bytes = data
            self._crop_download_btn.setEnabled(True)
        return data

    def _crop_search_box_to_jpeg(self, path: Path, max_dim: int = 1024) -> bytes | None:
        """Back-compat wrapper for the single-image flow.

        Returns the JPEG bytes of `path` cropped to self._search_box's scene rect, or
        None when no search box is set. Called by: _refresh_visual_search().
        """
        if self._search_box is None:
            return None
        r = self._search_box.scene_rect()
        return self._crop_image_to_rect_jpeg(
            path,
            (max(0, int(r.left())), max(0, int(r.top())),
             int(r.width()), int(r.height())),
            max_dim=max_dim,
            update_preview=True,
        )

    def _download_crop(self):
        """Save the most-recently-generated crop JPEG to disk via a file dialog.

        Suggests a filename based on the current image's stem and the box position
        so multiple crops from the same image don't collide. Called by:
        the "Download JPEG" button on the Crop preview tab.
        """
        if not self._last_crop_bytes:
            self.statusBar().showMessage("No crop available to download.", 4000)
            return
        # Build a sensible default filename: <image_stem>_crop.jpg in the image's folder.
        if self.image_paths and 0 <= self.index < len(self.image_paths):
            src = self.image_paths[self.index]
            default = str(src.with_name(f"{src.stem}_crop.jpg"))
        else:
            default = "crop.jpg"
        out, _ = QFileDialog.getSaveFileName(
            self, "Save cropped image", default, "JPEG (*.jpg *.jpeg);;All files (*)"
        )
        if not out:
            return
        try:
            Path(out).write_bytes(self._last_crop_bytes)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", f"Could not write {out}:\n{e}")
            return
        self.statusBar().showMessage(f"Saved crop to {out}", 5000)
        _dbg(f"crop saved: {out} ({len(self._last_crop_bytes):,} bytes)")

    # ------------------------------------------------------------------
    # Bulk mode — iterate every bounding box in the loaded NDJSON, run
    # visual search on each, render a single composite PNG report.
    # ------------------------------------------------------------------

    def _maybe_enable_bulk_mode(self):
        """Enable the Bulk mode toolbar button iff both a folder + NDJSON are loaded.

        Called from open_folder() and load_ndjson() — either order works.
        """
        ready = bool(self.image_paths) and bool(self.annotations)
        self._bulk_action.setEnabled(ready)
        if ready:
            _dbg("bulk mode: enabled (folder + NDJSON loaded).")

    def _object_to_display_rect(self, obj: dict) -> tuple[int, int, int, int] | None:
        """Read an NDJSON object's bounding_box into a (left, top, width, height) tuple.

        Mirrors the rect restoration in load_current(). Returns None if the dict is
        missing required keys or contains non-numeric values.
        Called by: _start_bulk_mode().
        """
        bb = obj.get("bounding_box") or {}
        try:
            return (int(bb["left"]), int(bb["top"]),
                    int(bb["width"]), int(bb["height"]))
        except (KeyError, TypeError, ValueError):
            return None

    def _extract_sku_from_entry(self, entry: dict) -> str:
        """Pull the image-level SKU out of an annotations dict's classifications.

        Returns "" if no SKU classification is present. Called by: _start_bulk_mode().
        """
        for c in (entry.get("annotations") or {}).get("classifications") or []:
            if c.get("name") == "SKU":
                return (c.get("text_answer") or {}).get("content", "").strip()
        return ""

    def _start_bulk_mode(self):
        """Iterate every box in self.annotations, call visual search, build a report.

        Steps:
          1. Build a flat task list from self.annotations (one entry per box).
          2. For each task: crop → POST → on 401 re-auth → record result.
          3. Render a composite report image (PIL) and show it in a new tab.
        Progress is reported via a modal QProgressDialog with a Cancel button.
        Called by: self._bulk_action.triggered.
        """
        if not self.auth_token:
            QMessageBox.warning(
                self, "Bulk mode",
                "Not authenticated — cannot run visual search.\n"
                "Check .annotater_creds.json and restart the app."
            )
            return
        if not self.image_paths or not self.annotations:
            QMessageBox.information(
                self, "Bulk mode",
                "Load a folder of images and an NDJSON first."
            )
            return

        # Save the current image's edits so anything in-memory matches NDJSON.
        self._stash_current()

        # Flatten: one task per bounding box.
        by_name = {p.name: p for p in self.image_paths}
        tasks: list[tuple[Path, str, tuple[int, int, int, int], int]] = []
        for fname, entry in self.annotations.items():
            path = by_name.get(fname)
            if path is None:
                continue
            sku = self._extract_sku_from_entry(entry)
            objs = (entry.get("annotations") or {}).get("objects") or []
            for i, obj in enumerate(objs):
                rect = self._object_to_display_rect(obj)
                if rect is not None:
                    tasks.append((path, sku, rect, i))

        if not tasks:
            QMessageBox.information(
                self, "Bulk mode", "No bounding boxes found in the loaded annotations."
            )
            return

        _dbg(f"bulk mode: {len(tasks)} boxes across {len({t[0] for t in tasks})} images")

        dlg = QProgressDialog(
            "Running visual search…", "Cancel", 0, len(tasks), self
        )
        dlg.setWindowTitle("Bulk mode")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)

        results: list[BulkResult] = []
        for i, (path, sku, rect, oi) in enumerate(tasks):
            if dlg.wasCanceled():
                _dbg(f"bulk mode: canceled after {i}/{len(tasks)} boxes")
                break
            dlg.setLabelText(f"{path.name}  box {oi+1}  ({i+1}/{len(tasks)})")
            dlg.setValue(i)
            QApplication.processEvents()

            crop = self._crop_image_to_rect_jpeg(path, rect, update_preview=False)
            if crop is None:
                results.append(BulkResult(path, sku, rect, [], "crop failed"))
                continue

            payload, msg, code = goat_visual_search(crop, self.auth_token)
            if code == 401:
                _dbg("bulk mode: 401 — refreshing token…")
                new_token, login_msg = goat_login()
                _dbg(f"bulk mode: auth refresh: {login_msg}")
                if new_token:
                    self.auth_token = new_token
                    payload, msg, code = goat_visual_search(crop, self.auth_token)
            items = (payload or {}).get("catalog_items") or []
            results.append(BulkResult(path, sku, rect, items, msg, crop_jpeg=crop))

        dlg.setValue(len(tasks))

        if not results:
            self.statusBar().showMessage("Bulk mode: no results.", 5000)
            return

        self.statusBar().showMessage(
            f"Bulk mode: {len(results)} boxes processed. Rendering report…", 5000
        )
        QApplication.processEvents()
        self._render_bulk_report(results)

    def _compute_stats(self, results: list[BulkResult]) -> dict:
        """Compute the summary stats shown atop the bulk report.

        Returns a dict with: n_boxes, n_images, n_multi_box_images, r_at_k (dict
        for k in 1/3/5/10), mrr, miss_rate, histogram (Counter keyed by rank-int
        or the string "miss"), max_rank (largest 1-indexed rank seen, for sizing
        the histogram).
        Called by: _render_bulk_report().
        """
        n = len(results)
        if n == 0:
            return {
                "n_boxes": 0, "n_images": 0, "n_multi_box_images": 0,
                "r_at_k": {1: 0, 3: 0, 5: 0, 10: 0},
                "mrr": 0.0, "miss_rate": 0.0,
                "histogram": Counter(), "max_rank": 0,
            }
        per_image = Counter(r.path for r in results)
        ranks = [r.rank() for r in results]
        hist = Counter()
        for rk in ranks:
            hist[(rk + 1) if rk is not None else "miss"] += 1
        r_at_k = {}
        for k in (1, 3, 5, 10):
            r_at_k[k] = sum(1 for rk in ranks if rk is not None and rk < k) / n
        mrr = sum(1.0 / (rk + 1) if rk is not None else 0.0 for rk in ranks) / n
        miss = sum(1 for rk in ranks if rk is None) / n
        numeric_ranks = [k for k in hist.keys() if isinstance(k, int)]
        max_rank = max(numeric_ranks) if numeric_ranks else 0
        return {
            "n_boxes": n,
            "n_images": len(per_image),
            "n_multi_box_images": sum(1 for c in per_image.values() if c > 1),
            "r_at_k": r_at_k,
            "mrr": mrr,
            "miss_rate": miss,
            "histogram": hist,
            "max_rank": max_rank,
        }

    def _render_bulk_report(self, results: list[BulkResult]):
        """Build a composite PNG of the bulk results and show it in a new tab.

        Layout: header (stats + histogram) followed by one row per BulkResult:
        the cropped image on the left, then TOP_N catalog-item thumbnails to the
        right. Ground-truth-matching thumbs are outlined in green.
        Called by: _start_bulk_mode().
        """
        TOP_N = 10
        ROW_H = 190  # taller row to fit filename + folder + sku + rank under the crop
        CROP_W, CROP_H = 160, 130
        THUMB_W, THUMB_H = 110, 110
        THUMB_GAP = 8
        ROW_PAD = 10
        SIDE_PAD = 20

        stats = self._compute_stats(results)

        # --- 1. Fetch unique thumbnails (synchronous, with progress dialog) ---
        unique_urls: list[str] = []
        seen_urls: set[str] = set()
        for r in results:
            for it in r.items[:TOP_N]:
                u = it.get("grid_glow_picture_url")
                if u and u not in seen_urls:
                    seen_urls.add(u)
                    unique_urls.append(u)

        thumb_cache: dict[str, Image.Image] = {}
        dlg = QProgressDialog(
            "Downloading thumbnails…", "Cancel", 0, max(1, len(unique_urls)), self
        )
        dlg.setWindowTitle("Bulk mode")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setMinimumDuration(0)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        for i, u in enumerate(unique_urls):
            if dlg.wasCanceled():
                break
            dlg.setValue(i)
            dlg.setLabelText(f"Downloading {i+1}/{len(unique_urls)}…")
            QApplication.processEvents()
            try:
                req = urllib.request.Request(u, headers={"User-Agent": GOAT_USER_AGENT})
                with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                    raw = Image.open(io.BytesIO(resp.read()))
                    # GOAT product PNGs have transparent backgrounds. A naive
                    # .convert("RGB") fills transparency with black; composite
                    # onto white instead so the report doesn't look like an LP.
                    if raw.mode in ("RGBA", "LA") or (
                        raw.mode == "P" and "transparency" in raw.info
                    ):
                        rgba = raw.convert("RGBA")
                        bg = Image.new("RGB", rgba.size, (255, 255, 255))
                        bg.paste(rgba, mask=rgba.split()[-1])
                        img = bg
                    else:
                        img = raw.convert("RGB")
                    thumb_cache[u] = img
            except Exception as e:
                _dbg(f"bulk thumb: failed to fetch {u[:60]}…: {type(e).__name__}")
        dlg.setValue(len(unique_urls))

        # --- 2. Resolve a font (Helvetica on macOS, fallback to default) ---
        def _font(size: int) -> ImageFont.ImageFont:
            for candidate in (
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/Library/Fonts/Arial.ttf",
            ):
                try:
                    return ImageFont.truetype(candidate, size)
                except Exception:
                    continue
            return ImageFont.load_default()
        font_title = _font(22)
        font_h = _font(16)
        font_body = _font(14)
        font_small = _font(11)
        font_mono = _font(13)

        # --- 3. Compute canvas dimensions ---
        # Histogram covers every rank from 1..max_rank plus "miss".
        hist = stats["histogram"]
        max_rank = stats["max_rank"]
        hist_rows = max_rank + (1 if hist.get("miss", 0) else 0)
        if hist_rows == 0:
            hist_rows = 1  # avoid zero-height
        # Count distinct folders for header sizing (matches the rendering below).
        n_dirs = len({str(r.path.parent) for r in results})
        if n_dirs <= 1:
            folder_h = 22
        else:
            folder_h = 20 + 18 * min(n_dirs, 5) + (18 if n_dirs > 5 else 0)
        header_h = 240 + folder_h + 22 * hist_rows + 40
        rows_h = ROW_H * len(results) + ROW_PAD * (len(results) + 1)
        total_h = header_h + rows_h + SIDE_PAD
        total_w = SIDE_PAD + CROP_W + 16 + TOP_N * (THUMB_W + THUMB_GAP) + SIDE_PAD

        canvas = Image.new("RGB", (total_w, total_h), (255, 255, 255))
        draw = ImageDraw.Draw(canvas)

        # --- 4. Header ---
        y = SIDE_PAD
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.text((SIDE_PAD, y), f"Bulk visual-search evaluation     {ts}",
                  fill=(20, 20, 20), font=font_title)
        y += 32
        # Image folder(s) being evaluated — usually one (Open Folder), but if a
        # user mixes images from multiple folders we show each distinct parent.
        result_dirs: list[Path] = []
        seen_dirs: set[str] = set()
        for r in results:
            d = r.path.parent
            ds = str(d)
            if ds not in seen_dirs:
                seen_dirs.add(ds)
                result_dirs.append(d)
        if len(result_dirs) == 1:
            draw.text((SIDE_PAD, y), f"Folder: {result_dirs[0]}",
                      fill=(40, 40, 40), font=font_mono)
            y += 22
        elif result_dirs:
            draw.text((SIDE_PAD, y), f"Folders ({len(result_dirs)}):",
                      fill=(40, 40, 40), font=font_mono)
            y += 20
            for d in result_dirs[:5]:
                draw.text((SIDE_PAD + 16, y), str(d),
                          fill=(60, 60, 60), font=font_mono)
                y += 18
            if len(result_dirs) > 5:
                draw.text((SIDE_PAD + 16, y),
                          f"…and {len(result_dirs) - 5} more",
                          fill=(120, 120, 120), font=font_mono)
                y += 18
        y += 8
        draw.text((SIDE_PAD, y), "Dataset:", fill=(40, 40, 40), font=font_h)
        y += 22
        mpct = (
            f" ({100*stats['n_multi_box_images']/stats['n_images']:.1f}%)"
            if stats["n_images"] else ""
        )
        for line in [
            f"  Images:                          {stats['n_images']}",
            f"  Bounding boxes:                  {stats['n_boxes']}",
            f"  Images with >1 bounding box:     {stats['n_multi_box_images']}{mpct}",
        ]:
            draw.text((SIDE_PAD, y), line, fill=(40, 40, 40), font=font_mono)
            y += 20
        y += 8

        draw.text(
            (SIDE_PAD, y),
            f"Retrieval metrics (denominator = {stats['n_boxes']} boxes):",
            fill=(40, 40, 40), font=font_h,
        )
        y += 22
        rk = stats["r_at_k"]
        draw.text(
            (SIDE_PAD, y),
            f"  R@1: {rk[1]*100:5.1f}%   R@3: {rk[3]*100:5.1f}%   "
            f"R@5: {rk[5]*100:5.1f}%   R@10: {rk[10]*100:5.1f}%",
            fill=(40, 40, 40), font=font_mono,
        )
        y += 20
        draw.text(
            (SIDE_PAD, y),
            f"  MRR: {stats['mrr']:.3f}   Miss rate: {stats['miss_rate']*100:.1f}%",
            fill=(40, 40, 40), font=font_mono,
        )
        y += 26

        draw.text(
            (SIDE_PAD, y),
            "Rank distribution (count of boxes whose GT SKU landed at that rank; "
            "\"miss\" = GT not returned):",
            fill=(40, 40, 40), font=font_body,
        )
        y += 22
        hist_total_max = max([hist[k] for k in hist] or [1])
        bar_max_px = total_w - SIDE_PAD - 200
        for r in range(1, max_rank + 1):
            count = hist.get(r, 0)
            bar = int(bar_max_px * count / hist_total_max) if hist_total_max else 0
            draw.text((SIDE_PAD, y), f"  rank {r:>2}", fill=(40, 40, 40), font=font_mono)
            if bar > 0:
                draw.rectangle(
                    [SIDE_PAD + 110, y + 3, SIDE_PAD + 110 + bar, y + 17],
                    fill=(80, 130, 220),
                )
            draw.text((SIDE_PAD + 120 + bar, y), str(count),
                      fill=(40, 40, 40), font=font_mono)
            y += 22
        if hist.get("miss", 0):
            count = hist["miss"]
            bar = int(bar_max_px * count / hist_total_max) if hist_total_max else 0
            draw.text((SIDE_PAD, y), "  miss  ", fill=(180, 40, 40), font=font_mono)
            if bar > 0:
                draw.rectangle(
                    [SIDE_PAD + 110, y + 3, SIDE_PAD + 110 + bar, y + 17],
                    fill=(220, 90, 90),
                )
            draw.text((SIDE_PAD + 120 + bar, y), str(count),
                      fill=(180, 40, 40), font=font_mono)
            y += 22
        y += 16
        # divider line
        draw.line([(SIDE_PAD, y), (total_w - SIDE_PAD, y)], fill=(200, 200, 200), width=1)
        y += ROW_PAD

        # --- 5. Per-box rows ---
        for r in results:
            row_top = y
            # Crop on the left
            if r.crop_jpeg:
                try:
                    crop_img = Image.open(io.BytesIO(r.crop_jpeg)).convert("RGB")
                    crop_img.thumbnail((CROP_W, CROP_H), Image.Resampling.LANCZOS)
                    cx = SIDE_PAD + (CROP_W - crop_img.width) // 2
                    cy = row_top + (CROP_H - crop_img.height) // 2
                    canvas.paste(crop_img, (cx, cy))
                except Exception as e:
                    draw.text((SIDE_PAD, row_top + 10), "(crop decode error)",
                              fill=(180, 40, 40), font=font_small)
            else:
                draw.rectangle(
                    [SIDE_PAD, row_top, SIDE_PAD + CROP_W, row_top + CROP_H],
                    outline=(220, 220, 220), width=1,
                )
                draw.text((SIDE_PAD + 10, row_top + 10), "(no crop)",
                          fill=(160, 160, 160), font=font_small)

            # Filename + folder + SKU + rank under the crop. Truncate hard to the
            # crop column width so labels never bleed into the thumbnail column.
            def _fit(s: str, max_chars: int = 22) -> str:
                return s if len(s) <= max_chars else s[: max_chars - 1] + "…"

            label_y = row_top + CROP_H + 2
            draw.text((SIDE_PAD, label_y),
                      _fit(r.path.name),
                      fill=(20, 20, 20), font=font_small)
            draw.text((SIDE_PAD, label_y + 14),
                      _fit(r.path.parent.name + "/", 22),
                      fill=(90, 90, 90), font=font_small)
            rk_val = r.rank()
            if rk_val is None:
                rank_text = "miss" if (r.gt_sku or "").strip() else "(no GT)"
                rank_color = (180, 40, 40) if (r.gt_sku or "").strip() else (140, 140, 140)
            else:
                rank_text = f"★ rank {rk_val + 1}"
                rank_color = (20, 140, 60)
            draw.text((SIDE_PAD, label_y + 30),
                      f"SKU: {_fit(r.gt_sku or '(empty)', 18)}",
                      fill=(40, 40, 40), font=font_small)
            draw.text((SIDE_PAD, label_y + 44), rank_text,
                      fill=rank_color, font=font_small)

            # Thumbnails on the right
            gt = (r.gt_sku or "").strip().casefold()
            tx_start = SIDE_PAD + CROP_W + 16
            for ti in range(TOP_N):
                tx = tx_start + ti * (THUMB_W + THUMB_GAP)
                if ti >= len(r.items):
                    # empty placeholder
                    draw.rectangle(
                        [tx, row_top, tx + THUMB_W, row_top + THUMB_H],
                        outline=(230, 230, 230), width=1,
                    )
                    continue
                item = r.items[ti]
                item_sku = str(item.get("sku", "")).strip()
                is_match = bool(gt) and item_sku.casefold() == gt
                url = item.get("grid_glow_picture_url")
                thumb = thumb_cache.get(url) if url else None
                # Paste thumbnail (or grey box on failure)
                if thumb is not None:
                    t = thumb.copy()
                    t.thumbnail((THUMB_W, THUMB_H), Image.Resampling.LANCZOS)
                    px = tx + (THUMB_W - t.width) // 2
                    py = row_top + (THUMB_H - t.height) // 2
                    canvas.paste(t, (px, py))
                else:
                    draw.rectangle(
                        [tx, row_top, tx + THUMB_W, row_top + THUMB_H],
                        fill=(245, 245, 245), outline=(220, 220, 220), width=1,
                    )
                    draw.text((tx + 6, row_top + THUMB_H // 2 - 6),
                              "no img", fill=(160, 160, 160), font=font_small)
                # SKU label under the thumb
                draw.text((tx, row_top + THUMB_H + 2),
                          item_sku[:18] if item_sku else "(no sku)",
                          fill=(40, 40, 40) if is_match else (90, 90, 90),
                          font=font_small)
                # Green outline for match
                if is_match:
                    draw.rectangle(
                        [tx - 2, row_top - 2, tx + THUMB_W + 2, row_top + THUMB_H + 2],
                        outline=(20, 160, 60), width=3,
                    )
            # Row separator
            sep_y = row_top + ROW_H - 4
            draw.line(
                [(SIDE_PAD, sep_y), (total_w - SIDE_PAD, sep_y)],
                fill=(235, 235, 235), width=1,
            )
            y = row_top + ROW_H + ROW_PAD

        # --- 6. Encode to PNG bytes (for Download) and prepare on-screen tiles ---
        # Qt's QPixmap caps individual textures at ~16384 px per side on macOS
        # Metal — a tall bulk report (hundreds of rows) silently fails to load
        # as a single QPixmap. Save the full-res PNG bytes for Download, and
        # slice the canvas into vertical tiles for in-app display.
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        self._bulk_report_png_bytes = buf.getvalue()
        # Build a single QPixmap when the canvas fits, otherwise leave it None
        # and let the tile path handle display. Download uses the PNG bytes.
        TILE_MAX_H = 8000  # comfortably under Qt's 16384 cap, leaves room for HiDPI
        pm_full = QPixmap()
        pm_full.loadFromData(self._bulk_report_png_bytes)
        if pm_full.isNull() or canvas.height > TILE_MAX_H:
            self._bulk_report_pixmap = None
        else:
            self._bulk_report_pixmap = pm_full

        # Replace any existing bulk tab.
        if self._bulk_tab_index is not None:
            self._exit_bulk_mode()

        tab = QWidget()
        v = QVBoxLayout(tab)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(QLabel(
            f"<b>Bulk results</b> — {stats['n_boxes']} boxes / {stats['n_images']} images"
        ), 1)
        dl_btn = QPushButton("Download PNG")
        dl_btn.clicked.connect(self._download_bulk_report)
        top.addWidget(dl_btn, 0)
        v.addLayout(top)

        # Build the scrollable display: either a single QLabel, or a vertical
        # stack of tile labels when the canvas is too tall for one QPixmap.
        inner = QWidget()
        inner_v = QVBoxLayout(inner)
        inner_v.setContentsMargins(0, 0, 0, 0)
        inner_v.setSpacing(0)
        if self._bulk_report_pixmap is not None:
            lbl = QLabel()
            lbl.setPixmap(self._bulk_report_pixmap)
            lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            inner_v.addWidget(lbl)
        else:
            # Slice canvas vertically and render each tile as its own QLabel.
            y0 = 0
            n_tiles = 0
            while y0 < canvas.height:
                y1 = min(y0 + TILE_MAX_H, canvas.height)
                tile = canvas.crop((0, y0, canvas.width, y1))
                tbuf = io.BytesIO()
                tile.save(tbuf, format="PNG")
                tpm = QPixmap()
                tpm.loadFromData(tbuf.getvalue())
                lbl = QLabel()
                lbl.setPixmap(tpm)
                lbl.setAlignment(
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                )
                inner_v.addWidget(lbl)
                y0 = y1
                n_tiles += 1
            _dbg(f"bulk mode: report sliced into {n_tiles} tiles (Qt pixmap cap)")
        inner_v.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        idx = self._canvas_tabs.addTab(tab, "Bulk results")
        self._bulk_tab_index = idx
        self._canvas_tabs.setCurrentIndex(idx)
        _dbg(
            f"bulk mode: report rendered ({canvas.width}x{canvas.height} px, "
            f"R@1={stats['r_at_k'][1]*100:.1f}%, miss={stats['miss_rate']*100:.1f}%)"
        )

    def _download_bulk_report(self):
        """Save the most-recent bulk report image to disk via a file dialog.

        Writes the full-resolution PNG bytes generated by PIL (not the on-screen
        pixmap), so reports too tall for Qt's pixmap cap still save at full
        quality. Called by: the "Download PNG" button on the Bulk results tab.
        """
        png_bytes = getattr(self, "_bulk_report_png_bytes", None)
        if not png_bytes:
            self.statusBar().showMessage("No bulk report to download.", 4000)
            return
        default = f"bulk_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        out, _ = QFileDialog.getSaveFileName(
            self, "Save bulk report", default, "PNG (*.png);;JPEG (*.jpg)"
        )
        if not out:
            return
        try:
            Path(out).write_bytes(png_bytes)
        except OSError as e:
            QMessageBox.warning(self, "Save failed", f"Could not write {out}:\n{e}")
            return
        self.statusBar().showMessage(f"Saved bulk report to {out}", 5000)
        _dbg(f"bulk report saved: {out} ({len(png_bytes):,} bytes)")

    def _on_canvas_tab_close(self, index: int):
        """Handle tab-close clicks on the central QTabWidget.

        Only the Bulk results tab is closeable (Annotate has no close button), so
        any close click means "exit bulk mode". Called by:
        self._canvas_tabs.tabCloseRequested.
        """
        if self._canvas_tabs.tabText(index) == "Bulk results":
            self._exit_bulk_mode()

    def _exit_bulk_mode(self):
        """Remove the Bulk results tab and clear bulk state.

        Called by: _on_canvas_tab_close() and _render_bulk_report() (when re-running).
        """
        if self._bulk_tab_index is not None:
            w = self._canvas_tabs.widget(self._bulk_tab_index)
            self._canvas_tabs.removeTab(self._bulk_tab_index)
            if w is not None:
                w.deleteLater()
        self._bulk_tab_index = None
        self._bulk_report_pixmap = None
        self._bulk_report_png_bytes = None
        self._canvas_tabs.setCurrentIndex(0)

    def _refresh_visual_search(self):
        """Crop the active search box, POST to visual-search, populate the carousel.

        If no search box is set yet (first run for a new image), defaults to the first
        box on the canvas (highest-confidence predictor result, or first NDJSON object).
        Handles HTTP 401 transparently by calling goat_login() once and retrying.
        Called by: load_current() at the end, and _on_search_box_clicked().
        """
        self._clear_carousel()
        boxes = self.view.boxes()
        _dbg(f"visual search check: image_paths={len(self.image_paths)} "
             f"auth_token={'yes' if self.auth_token else 'NO'} "
             f"boxes={len(boxes)}")
        if not self.image_paths:
            self.statusBar().showMessage("Visual search skipped — no image loaded.", 5000)
            _dbg("visual search: no image loaded.")
            return
        if not boxes:
            self.statusBar().showMessage("Visual search skipped — no bounding box on canvas.", 5000)
            _dbg("visual search: no boxes on canvas.")
            return
        if not self.auth_token:
            self.statusBar().showMessage(
                "Visual search skipped — not authenticated. Check .annotater_creds.json.", 8000
            )
            _dbg("visual search: no auth token (login failed earlier).")
            return
        if self._search_box is None:
            self._set_search_box(boxes[0])
        path = self.image_paths[self.index]

        self.statusBar().showMessage("Cropping image to bounding box…")
        QApplication.processEvents()
        r = self._search_box.scene_rect()
        _dbg(f"visual search: cropping rect=({int(r.left())},{int(r.top())},"
             f"{int(r.width())}x{int(r.height())})")
        crop_bytes = self._crop_search_box_to_jpeg(path)
        if crop_bytes is None:
            msg = "Visual search skipped — crop failed."
            self.statusBar().showMessage(msg, 5000)
            _dbg(msg)
            return
        _dbg(f"visual search: crop is {len(crop_bytes)} bytes; posting…")

        self.statusBar().showMessage("Calling visual search API…")
        QApplication.processEvents()
        payload, msg, code = goat_visual_search(crop_bytes, self.auth_token)
        # Surface FULL response detail in the debug log; status bar gets the short version.
        _dbg(f"visual search response: code={code} msg={msg}")

        if code == 401:
            warn = f"Auth expired ({msg}) — refreshing token…"
            self.statusBar().showMessage(warn)
            _dbg(warn)
            QApplication.processEvents()
            self.auth_token, login_msg = goat_login()
            _dbg(f"auth refresh: {login_msg}")
            if not self.auth_token:
                self.statusBar().showMessage(f"Re-login failed: {login_msg}", 8000)
                return
            payload, msg, code = goat_visual_search(crop_bytes, self.auth_token)

        if payload is None:
            self.statusBar().showMessage(msg, 5000)
            _dbg(msg)
            return
        items = payload.get("catalog_items") or []
        self.statusBar().showMessage(f"Visual search: {len(items)} matches.", 3000)
        _dbg(f"visual search: {len(items)} catalog items returned.")
        _dbg("visual search payload:\n" + json.dumps(payload, indent=2))
        self._populate_carousel(items)

    def _clear_carousel(self):
        """Remove all cards from the carousel and abort pending image downloads."""
        self._pending_thumbs.clear()
        while self._carousel_layout.count() > 1:
            item = self._carousel_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _populate_carousel(self, items: list[dict]):
        """Build one card per catalog_item in array order (left-to-right).

        Each card: QFrame with QVBoxLayout — name, sku, slug, id labels above two
        stacked image labels (grid_glow_picture_url on top, main_glow_picture_url
        below). Image downloads are kicked off async via QNetworkAccessManager.
        Duplicate image URLs (e.g. multiple cards sharing the same grid image)
        are tracked as a list so every card's thumbnail gets populated.
        """
        _dbg(f"visual search: building {len(items)} cards")
        for item in items:
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card.setFixedWidth(170)
            v = QVBoxLayout(card)
            v.setContentsMargins(6, 6, 6, 6)
            v.setSpacing(2)

            name = QLabel(str(item.get("name", "")))
            name.setWordWrap(True)
            name.setStyleSheet("font-weight: bold;")
            sku = QLabel("SKU: " + str(item.get("sku", "")))
            sku.setStyleSheet("color: #1d4ed8; font-size: 11px; font-weight: bold;")
            sku.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            sku.setCursor(Qt.CursorShape.IBeamCursor)
            sku.setToolTip("Click and drag to select, then Cmd/Ctrl+C to copy")
            slug = QLabel(str(item.get("slug", "")))
            slug.setStyleSheet("color: #666; font-size: 10px;")
            id_lbl = QLabel(str(item.get("id", "")))
            id_lbl.setStyleSheet("color: #888; font-size: 9px;")

            grid_lbl = self._make_thumb_label()
            main_lbl = self._make_thumb_label()

            v.addWidget(name)
            v.addWidget(sku)
            v.addWidget(slug)
            v.addWidget(id_lbl)
            v.addWidget(QLabel("grid:"))
            v.addWidget(grid_lbl)
            v.addWidget(QLabel("main:"))
            v.addWidget(main_lbl)

            self._carousel_layout.insertWidget(self._carousel_layout.count() - 1, card)

            for url, label in (
                (item.get("grid_glow_picture_url"), grid_lbl),
                (item.get("main_glow_picture_url"), main_lbl),
            ):
                if not url:
                    label.setText("(no url)")
                    continue
                existing = self._pending_thumbs.setdefault(url, [])
                existing.append(label)
                # Only fire the GET once per unique URL; reuse the bytes
                # for any additional cards sharing this image_url.
                if len(existing) == 1:
                    self._netmgr.get(QNetworkRequest(QUrl(url)))

    def _make_thumb_label(self) -> QLabel:
        """Create a placeholder QLabel used to host a downloaded carousel thumbnail."""
        lbl = QLabel("loading…")
        lbl.setFixedSize(150, 100)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("background:#eee; color:#888;")
        return lbl

    def _on_thumb_loaded(self, reply: "QNetworkReply"):
        """QNetworkAccessManager.finished slot — set downloaded image onto ALL labels sharing this URL."""
        url = reply.request().url().toString()
        labels = self._pending_thumbs.pop(url, [])
        if not labels:
            reply.deleteLater()
            return
        if reply.error() != QNetworkReply.NetworkError.NoError:
            for label in labels:
                label.setText("(image error)")
        else:
            data = reply.readAll()
            pm = QPixmap()
            if pm.loadFromData(data):
                scaled = pm.scaled(
                    150, 100,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                for label in labels:
                    label.setPixmap(scaled)
            else:
                for label in labels:
                    label.setText("(decode error)")
        reply.deleteLater()

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

    def _jump_to_filename(self):
        """Skip to the first image whose filename matches the Jump-to input.

        Matching is case-insensitive and tries, in order: exact name, exact stem
        (without extension), prefix of name, substring of name. Useful for
        resuming work where you left off without pressing Next many times.
        Called by: the Jump-to QLineEdit's returnPressed signal (Enter key).
        """
        if not self.image_paths:
            self.statusBar().showMessage("Open a folder first.", 4000)
            return
        query = self.jump_input.text().strip().lower()
        if not query:
            return
        names_lc = [p.name.lower() for p in self.image_paths]
        stems_lc = [p.stem.lower() for p in self.image_paths]

        idx = None
        if query in names_lc:
            idx = names_lc.index(query)
        elif query in stems_lc:
            idx = stems_lc.index(query)
        else:
            for i, n in enumerate(names_lc):
                if n.startswith(query):
                    idx = i
                    break
            if idx is None:
                for i, n in enumerate(names_lc):
                    if query in n:
                        idx = i
                        break

        if idx is None:
            self.statusBar().showMessage(f"No image matches '{query}'.", 4000)
            _dbg(f"jump: no match for '{query}'")
            return

        self._stash_current()
        self.index = idx
        self.load_current()
        self.jump_input.clear()

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
        self._maybe_enable_bulk_mode()

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
